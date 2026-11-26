import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from backend.services.maps_service.app import STORES
from backend.shared.database import DatabaseConnection

from .db import ACTIVE_ADDRESS, ADDRESS_FIELDS, active_member, load_carts, load_contributions, lock_household


class CheckoutInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


ReferenceID = Annotated[str, Field(min_length=1, max_length=128)]


class HouseholdCart(CheckoutInput):
    kind: Literal["household"]
    household_id: ReferenceID
    cart_id: ReferenceID


class AddressRef(CheckoutInput):
    kind: Literal["household"]
    address_id: ReferenceID


class AddressOverride(CheckoutInput):
    contributor_id: ReferenceID
    address: AddressRef


class QuoteInput(CheckoutInput):
    cart: HouseholdCart
    default_address: AddressRef
    overrides: list[AddressOverride] = Field(default_factory=list)
    # Products to order. Everything else stays in the cart. None orders the whole cart.
    skus: Annotated[list[ReferenceID], Field(min_length=1, max_length=500)] | None = None


class OrderInput(CheckoutInput):
    quote_id: ReferenceID
    mock_outcome: Literal["approve", "decline"] = "approve"


def error(status: int, code: str, message: str, **details) -> HTTPException:
    return HTTPException(status, detail={"code": code, "message": message, **details},
                         headers={"Cache-Control": "no-store"})


def stale() -> HTTPException:
    return error(409, "QUOTE_STALE", "The cart, delivery address, or pricing changed. Review checkout again.")


def seed_inventory(db: DatabaseConnection, products) -> None:
    # The last jar of honey at each store is available for the contention demo.
    values = [(store.store_id, sku, 1 if sku == "honey" else 100) for store in STORES for sku in sorted(products)]
    if values:
        placeholders = ", ".join(["(?, ?, ?)"] * len(values))
        db.execute(
            f"INSERT INTO store_inventory (store_id, sku, quantity) VALUES {placeholders} "
            "ON CONFLICT (store_id, sku) DO NOTHING",
            tuple(value for row in values for value in row),
        )


def lock_cart(db, reference, caller) -> None:
    lock_household(db, reference.household_id)


def cart_state(db, reference, caller):
    household_id = reference.household_id
    member = active_member(db, household_id, caller.user_id)
    if member is None:
        raise error(404, "CART_NOT_FOUND", "Cart not found.")
    is_admin = member["owner_id"] == caller.user_id
    if not is_admin and not caller.is_adult:
        raise error(403, "CHECKOUT_FORBIDDEN", "Child users cannot check out.")
    cart = load_carts(db, household_id, caller.user_id, reference.cart_id).fetchone()
    granted = cart is not None and (is_admin or (
        not cart["creator_revoked"] if cart["creator_id"] == caller.user_id else cart["has_grant"]
    ))
    if not granted:
        raise error(404, "CART_NOT_FOUND", "Cart not found.")
    names = {row["user_id"]: row["display_name"] for row in db.execute(
        "SELECT user_id, display_name FROM household_members WHERE household_id = ?", (household_id,),
    )}
    return cart, load_contributions(db, cart["id"]), names


def saved_address(db, reference, address, user_id: str, *, lock: bool) -> dict:
    parameters = [address.address_id, reference.household_id]
    row_lock = " FOR SHARE" if lock and db.dialect == "postgresql" else ""
    # An address leaves with the member who added it.
    active = f" AND {ACTIVE_ADDRESS}"
    # Same rule as the address list: only the admin can use other members' addresses.
    member = active_member(db, reference.household_id, user_id)
    if member is None or member["owner_id"] != user_id:
        active += " AND created_by = ?"
        parameters.append(user_id)
    row = db.execute(
        f"SELECT {ADDRESS_FIELDS}, updated_at FROM household_addresses WHERE id = ? AND household_id = ?"
        + active + row_lock,
        parameters,
    ).fetchone()
    if row is None:
        raise error(404, "ADDRESS_NOT_FOUND", "Address not found.")
    return dict(row)


def snapshot(db, selection: QuoteInput, caller, products, *, lock: bool) -> dict:
    reference = selection.cart
    cart, rows, names = cart_state(db, reference, caller)
    if not rows:
        raise error(409, "EMPTY_CART", "Add something to your cart before checkout.")
    if selection.skus is not None:
        chosen = set(selection.skus)
        if chosen - {row["sku"] for row in rows}:
            raise error(409, "SELECTION_CHANGED", "A selected item is no longer in the cart. Review your selection.")
        rows = [row for row in rows if row["sku"] in chosen]
    contributors = {row.get("contributor_id") for row in rows}
    overrides = {}
    for override in selection.overrides:
        if override.contributor_id not in contributors or override.contributor_id in overrides:
            raise error(422, "INVALID_CONTRIBUTOR", "Each override must name a different contributor in this cart.")
        overrides[override.contributor_id] = override.address

    def address_key(address):
        return address.kind, address.address_id

    addresses = {address_key(address): address for address in [selection.default_address, *overrides.values()]}
    # Always validate the supplied default, even if every contributor has an override.
    address_data = {
        key: saved_address(db, reference, addresses[key], caller.user_id, lock=lock) for key in sorted(addresses)
    }
    groups = {}
    for row in rows:
        sku, quantity = row["sku"], row["quantity"]
        product = products.get(sku)
        restricted = product and product.min_age and reference.kind == "household" and not cart["adults_only"]
        if not product or product.currency != "AED" or not product.visible_to(caller) or restricted:
            raise error(409, "ITEM_UNAVAILABLE", "Remove the unavailable item before checking out.", sku=sku)
        contributor = row.get("contributor_id")
        address = overrides.get(contributor, selection.default_address)
        key = address_key(address)
        group = groups.setdefault(key, {
            "address_ref": address.model_dump(), "address": address_data[key], "lines": [], "subtotal_fils": 0,
        })
        total = product.price_fils * quantity
        group["lines"].append({
            "sku": sku, "name": product.name, "unit_price_fils": product.price_fils,
            "line_total_fils": total, "quantity": quantity, "contributor_id": contributor,
            "contributor_name": names.get(contributor),
        })
        group["subtotal_fils"] += total
    revision = db.execute(
        "SELECT revision FROM carts WHERE id = ?", (cart["id"],),
    ).fetchone()
    return {
        "cart_id": cart["id"], "cart_revision": revision["revision"] if revision else 0,
        "selection": selection.model_dump(), "default_address": address_data[address_key(selection.default_address)],
        "groups": [groups[key] for key in sorted(groups)],
    }


def resolve_groups(groups, resolve_pin) -> list[dict]:
    def resolve(group):
        address = group["address"]
        try:
            result = resolve_pin(address["latitude"], address["longitude"])
        except HTTPException as issue:
            if issue.detail not in ("ADDRESS_NEEDS_PIN", "MAPS_UNAVAILABLE"):
                raise
            return {**group, "problem": issue.detail}
        if result.status == "OUTSIDE_DELIVERY_AREA":
            return {**group, "problem": result.status}
        return {**group, "store_id": result.store_id, "store_name": result.store_name}

    if len(groups) == 1:
        resolved = [resolve(groups[0])]
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(groups))) as pool:
            resolved = list(pool.map(resolve, groups))
    issues = {group.get("problem") for group in resolved}
    messages = {
        "MAPS_UNAVAILABLE": (503, "Delivery coverage is temporarily unavailable. Try again shortly."),
        "ADDRESS_NEEDS_PIN": (409, "Add a map pin to the highlighted saved address."),
        "OUTSIDE_DELIVERY_AREA": (409, "Choose a covered address for the highlighted delivery."),
    }
    for code, (status, message) in messages.items():
        if code in issues:
            raise error(status, code, message, delivery_groups=resolved)
    return resolved


def check_stock(db, groups, *, lock: bool) -> Counter:
    requested = Counter()
    for group in groups:
        for line in group["lines"]:
            requested[group["store_id"], line["sku"]] += line["quantity"]
    unavailable = {}
    row_lock = " FOR UPDATE" if lock and db.dialect == "postgresql" else ""
    for (store_id, sku), quantity in sorted(requested.items()):
        row = db.execute(
            "SELECT quantity FROM store_inventory WHERE store_id = ? AND sku = ?" + row_lock, (store_id, sku),
        ).fetchone()
        available = row["quantity"] if row else 0
        if quantity > available:
            unavailable[store_id, sku] = available
    if unavailable:
        shortages = [
            {"group_number": index, "store_id": group["store_id"], "sku": sku,
             "requested": requested[group["store_id"], sku], "available": unavailable[group["store_id"], sku]}
            for index, group in enumerate(groups, 1)
            for sku in sorted({line["sku"] for line in group["lines"]})
            if (group["store_id"], sku) in unavailable
        ]
        raise error(409, "OUT_OF_STOCK", "Some items exceed stock at their assigned store.",
                    delivery_groups=groups, shortages=shortages)
    return requested


def quote_response(quote_id, expires_at, groups, demo_enabled) -> dict:
    return {
        "quote_id": quote_id, "expires_at": expires_at, "currency": "AED", "delivery_fils": 0,
        "total_fils": sum(group["subtotal_fils"] for group in groups), "delivery_groups": groups,
        "mock_outcome_enabled": demo_enabled,
    }


def save_quote(db, purchaser_id, state, groups, demo_enabled) -> dict:
    check_stock(db, groups, lock=False)
    quote_id = str(uuid4())
    now = datetime.now(timezone.utc)
    expires_at = (now + timedelta(minutes=10)).isoformat()
    db.execute("INSERT INTO checkout_quotes VALUES (?, ?, ?, ?)", (
        quote_id, purchaser_id, expires_at, json.dumps({"state": state, "groups": groups}),
    ))
    return quote_response(quote_id, expires_at, groups, demo_enabled)


def fingerprint(body: OrderInput) -> str:
    return sha256(body.model_dump_json().encode()).hexdigest()


def order_history(db, purchaser_id: str, limit: int, before: str | None) -> dict:
    cursor_filter = ""
    parameters = [purchaser_id]
    if before is not None:
        anchor = db.execute(
            "SELECT placed_at FROM orders WHERE purchaser_id = ? AND id = ?", (purchaser_id, before),
        ).fetchone()
        if anchor is None:
            raise error(422, "INVALID_ORDER_CURSOR", "This page is unavailable. Reload your order history.")
        cursor_filter = "AND (placed_at, id) < (?, ?)"
        parameters.extend((anchor["placed_at"], before))
    rows = db.execute(f"""
        SELECT id AS order_id, order_number, placed_at, currency, total_fils, status,
               household_id IS NOT NULL AS is_shared,
               (SELECT COUNT(*) FROM order_delivery_groups AS delivery WHERE delivery.order_id = orders.id)
                   AS delivery_count
        FROM orders WHERE purchaser_id = ? {cursor_filter}
        ORDER BY placed_at DESC, id DESC LIMIT ?
    """, (*parameters, limit + 1)).fetchall()
    orders = [{**dict(row), "is_shared": bool(row["is_shared"])} for row in rows[:limit]]
    return {"orders": orders, "next_cursor": orders[-1]["order_id"] if len(rows) > limit else None}


def order_detail(db, order_id: str, purchaser_id: str) -> dict:
    row = db.execute(
        """SELECT id AS order_id, purchaser_id, household_id, order_number, placed_at,
                  currency, total_fils, status, mock_payment_outcome
           FROM orders WHERE id = ? AND purchaser_id = ?""", (order_id, purchaser_id),
    ).fetchone()
    if row is None:
        raise error(404, "ORDER_NOT_FOUND", "Order not found.")
    groups = {}
    for group in db.execute("SELECT * FROM order_delivery_groups WHERE order_id = ? ORDER BY group_number", (order_id,)):
        groups[group["group_number"]] = {
            "store_id": group["store_id"], "store_name": group["store_name"],
            "address_ref": {"kind": group["address_kind"], "address_id": group["address_id"]},
            "address": json.loads(group["address_snapshot"]), "subtotal_fils": group["subtotal_fils"], "lines": [],
        }
    for line in db.execute(
        """SELECT group_number, sku, name, unit_price_fils, quantity, contributor_id, contributor_name
           FROM order_lines WHERE order_id = ? ORDER BY group_number, line_number""", (order_id,),
    ):
        item = dict(line)
        number = item.pop("group_number")
        item["line_total_fils"] = item["unit_price_fils"] * item["quantity"]
        groups[number]["lines"].append(item)
    return {**dict(row), "delivery_fils": 0, "delivery_groups": list(groups.values())}


def successful_attempt(db, purchaser_id, key, attempt):
    row = db.execute(
        "SELECT id, request_fingerprint FROM orders WHERE purchaser_id = ? AND idempotency_key = ?",
        (purchaser_id, key),
    ).fetchone()
    if row is None:
        return None
    if row["request_fingerprint"] != attempt:
        raise error(409, "IDEMPOTENCY_KEY_REUSED", "This submission key belongs to a different checkout attempt.",
                    order_id=row["id"])
    return order_detail(db, row["id"], purchaser_id)


def require_unused_quote(db, quote_id: str, purchaser_id: str) -> None:
    order = db.execute(
        "SELECT id FROM orders WHERE quote_id = ? AND purchaser_id = ?", (quote_id, purchaser_id),
    ).fetchone()
    if order:
        raise error(409, "QUOTE_ALREADY_USED", "An order has already been placed for this review.", order_id=order["id"])


def load_quote(db, quote_id: str, purchaser_id: str):
    require_unused_quote(db, quote_id, purchaser_id)
    row = db.execute(
        "SELECT expires_at, snapshot FROM checkout_quotes WHERE id = ? AND purchaser_id = ?", (quote_id, purchaser_id),
    ).fetchone()
    if row is None:
        raise error(404, "QUOTE_NOT_FOUND", "This checkout review is no longer available.")
    expires_at = datetime.fromisoformat(row["expires_at"])
    require_unexpired(expires_at)
    return json.loads(row["snapshot"]), expires_at


def require_unexpired(expires_at) -> None:
    if expires_at <= datetime.now(timezone.utc):
        raise error(409, "QUOTE_EXPIRED", "This checkout review expired. Review checkout again.")


def revalidate(db, reviewed_state, selection, caller, products, *, lock: bool) -> dict:
    try:
        state = snapshot(db, selection, caller, products, lock=lock)
    except HTTPException as issue:
        if issue.detail["code"] in {"EMPTY_CART", "SELECTION_CHANGED", "ADDRESS_NOT_FOUND", "ITEM_UNAVAILABLE", "INVALID_CONTRIBUTOR"}:
            raise stale() from None
        raise
    if state != reviewed_state:
        raise stale()
    return state


def claim_order(db, purchaser_id, key, attempt, body, selection, groups):
    order_id = uuid4()
    inserted = db.execute(
        """INSERT INTO orders (id, order_number, purchaser_id, household_id, placed_at, currency,
                               total_fils, status, mock_payment_outcome, quote_id, idempotency_key, request_fingerprint)
           VALUES (?, ?, ?, ?, ?, 'AED', ?, 'PLACED', 'approve', ?, ?, ?)
           ON CONFLICT DO NOTHING RETURNING id""",
        (str(order_id), f"FC-{order_id.hex.upper()}", purchaser_id, getattr(selection.cart, "household_id", None),
         datetime.now(timezone.utc).isoformat(), sum(group["subtotal_fils"] for group in groups),
         body.quote_id, key, attempt),
    ).fetchone()
    return inserted["id"] if inserted else None


def fulfill_order(db, order_id, groups, requested) -> None:
    for (store_id, sku), quantity in sorted(requested.items()):
        db.execute(
            "UPDATE store_inventory SET quantity = quantity - ? WHERE store_id = ? AND sku = ?",
            (quantity, store_id, sku),
        )
    for number, group in enumerate(groups, 1):
        address_ref = group["address_ref"]
        db.execute(
            """INSERT INTO order_delivery_groups
               (order_id, group_number, store_id, store_name, address_kind, address_id, address_snapshot, subtotal_fils)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (order_id, number, group["store_id"], group["store_name"], address_ref["kind"], address_ref["address_id"],
             json.dumps(group["address"]), group["subtotal_fils"]),
        )
        for index, item in enumerate(group["lines"], 1):
            db.execute(
                """INSERT INTO order_lines
                   (order_id, group_number, line_number, sku, name, unit_price_fils, quantity, contributor_id,
                    contributor_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (order_id, number, index, item["sku"], item["name"], item["unit_price_fils"], item["quantity"],
                 item["contributor_id"], item["contributor_name"]),
            )
