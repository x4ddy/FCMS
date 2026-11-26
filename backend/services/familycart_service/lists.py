"""Shared household shopping lists.

Lists are planning tools: any active household member can view them, add
items, edit quantities and tick items off. Catalogue items can then be moved
into a shared cart, which applies the existing
cart access and adults-only rules.

This module is imported at the bottom of ``app.py`` (after the shared access
helpers are defined) and registers its routes on ``router``.
"""

import asyncio
import json
import logging
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, StringConstraints, model_validator
from redis.exceptions import RedisError

from backend.shared.api import API_PREFIX
from backend.shared.database import DatabaseConnection
from backend.shared.tokens import Identity, verify_token

from .app import (
    Caller, CartResponse, household_access, record_cart_change, require_product_for_cart,
    change_waiting, require_visible_cart, shared_quantity, viewer_age, visible_product,
)
from . import checkout, checkout_session
from .operations import Operation, OperationKey
from .db import ITEM_COLUMNS, ITEM_JOINS, LIST_COLUMNS, contribution_quantity, set_contribution
from .live_sync import AccessEnded, notify_changed, viewer_role

logger = logging.getLogger(__name__)
router = APIRouter()
LISTS = f"{API_PREFIX}/households/{{household_id}}/lists"
LIST = f"{LISTS}/{{list_id}}"
MAX_LIST_QUANTITY = 999

ListName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
ItemName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)]


class CreateList(BaseModel):
    name: ListName


class RenameList(BaseModel):
    name: ListName


class AddItem(BaseModel):
    sku: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    custom_name: ItemName | None = None
    quantity: int = Field(default=1, strict=True, ge=1, le=MAX_LIST_QUANTITY)
    note: Note | None = None

    @model_validator(mode="after")
    def exactly_one_kind(self) -> "AddItem":
        if (self.sku is None) == (self.custom_name is None):
            raise ValueError("Provide either a catalogue sku or a custom_name")
        return self


class AddItems(BaseModel):
    items: list[AddItem] = Field(min_length=1, max_length=50)


class UpdateItem(BaseModel):
    quantity: int | None = Field(default=None, strict=True, ge=1, le=MAX_LIST_QUANTITY)
    note: Note | None = None
    checked: bool | None = Field(default=None, strict=True)


ItemId = Annotated[str, StringConstraints(min_length=1, max_length=100)]


class SetItem(UpdateItem):
    id: ItemId


class ListChanges(BaseModel):
    """Several quick edits sent as one request. Applied in this order, in one transaction."""

    add: list[AddItem] = Field(default_factory=list, max_length=50)
    set: list[SetItem] = Field(default_factory=list, max_length=100)
    remove: list[ItemId] = Field(default_factory=list, max_length=100)


class MoveToCart(BaseModel):
    # A household cart ID.
    target: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    item_ids: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        min_length=1, max_length=200
    )


class ListItem(BaseModel):
    id: str
    sku: str | None
    custom_name: str | None
    quantity: int
    note: str | None
    added_by: str
    added_by_name: str
    checked: bool
    checked_by: str | None
    checked_by_name: str | None
    created_at: str
    updated_at: str
    can_delete: bool


class ListSummary(BaseModel):
    id: str
    name: str
    created_by: str
    created_by_name: str
    item_count: int
    checked_count: int
    updated_at: str
    can_manage: bool


class ListDetail(ListSummary):
    items: list[ListItem]


class SkippedItem(BaseModel):
    id: str
    reason: str


class ChangeResult(BaseModel):
    list: ListDetail
    skipped: list[SkippedItem]


class MoveResult(BaseModel):
    moved: list[str]
    skipped: list[SkippedItem]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def item_visible(request: Request, caller: Identity, sku: str | None) -> bool:
    if sku is None:
        return True
    product = request.app.state.products.get(sku)
    # Products removed from the catalogue stay visible so nobody loses an item.
    return product is None or product.visible_to(caller)


def to_item(row, caller: Identity, is_primary: bool) -> ListItem:
    return ListItem(
        id=row["id"], sku=row["sku"], custom_name=row["custom_name"], quantity=row["quantity"],
        note=row["note"], added_by=row["added_by"], added_by_name=row["added_by_name"],
        checked=bool(row["checked"]), checked_by=row["checked_by"],
        checked_by_name=row["checked_by_name"], created_at=row["created_at"], updated_at=row["updated_at"],
        can_delete=is_primary or row["added_by"] == caller.user_id,
    )


def summarize(row, items: list[ListItem], caller: Identity, is_primary: bool) -> ListSummary:
    return ListSummary(
        id=row["id"], name=row["name"], created_by=row["created_by"],
        created_by_name=row["created_by_name"], item_count=len(items),
        checked_count=sum(item.checked for item in items), updated_at=row["updated_at"],
        can_manage=is_primary or row["created_by"] == caller.user_id,
    )


def load_items(
    db: DatabaseConnection, request: Request, household_id: str, caller: Identity, is_primary: bool,
    list_id: str | None = None,
) -> list[tuple[str, ListItem]]:
    query = f"""SELECT {ITEM_COLUMNS} FROM shopping_list_items AS items {ITEM_JOINS}
                WHERE items.household_id = ?"""
    parameters: list = [household_id]
    if list_id is not None:
        query += " AND items.list_id = ?"
        parameters.append(list_id)
    rows = db.execute(query + " ORDER BY items.created_at, items.id", parameters)
    return [
        (row["list_id"], to_item(row, caller, is_primary))
        for row in rows if item_visible(request, caller, row["sku"])
    ]


def require_list(db: DatabaseConnection, household_id: str, list_id: str):
    row = db.execute(
        f"""SELECT {LIST_COLUMNS} FROM shopping_lists AS lists
            LEFT JOIN household_members AS creator
              ON creator.household_id = lists.household_id AND creator.user_id = lists.created_by
            WHERE lists.household_id = ? AND lists.id = ? AND lists.archived_at IS NULL""",
        (household_id, list_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="List not found")
    return row


def require_item(db: DatabaseConnection, list_id: str, item_id: str):
    row = db.execute(
        f"""SELECT {ITEM_COLUMNS} FROM shopping_list_items AS items {ITEM_JOINS}
            WHERE items.list_id = ? AND items.id = ?""",
        (list_id, item_id),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Item not found")
    return row


def list_detail(
    db: DatabaseConnection, request: Request, household_id: str, list_id: str,
    caller: Identity, is_primary: bool,
) -> ListDetail:
    row = require_list(db, household_id, list_id)
    items = [item for _, item in load_items(db, request, household_id, caller, is_primary, list_id)]
    return ListDetail(**summarize(row, items, caller, is_primary).model_dump(), items=items)


def touch(db: DatabaseConnection, household_id: str, list_id: str, now: str) -> None:
    db.execute("UPDATE shopping_lists SET updated_at = ? WHERE id = ?", (now, list_id))
    notify_changed(db, household_id)


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------
@router.get(LISTS)
def get_lists(household_id: str, request: Request, caller: Caller) -> list[ListSummary]:
    with household_access(request, household_id, caller) as (db, is_primary):
        rows = db.execute(
            f"""SELECT {LIST_COLUMNS} FROM shopping_lists AS lists
                LEFT JOIN household_members AS creator
                  ON creator.household_id = lists.household_id AND creator.user_id = lists.created_by
                WHERE lists.household_id = ? AND lists.archived_at IS NULL
                ORDER BY lists.updated_at DESC, lists.id""",
            (household_id,),
        ).fetchall()
        items_by_list: dict[str, list[ListItem]] = {}
        for list_id, item in load_items(db, request, household_id, caller, is_primary):
            items_by_list.setdefault(list_id, []).append(item)
        return [summarize(row, items_by_list.get(row["id"], []), caller, is_primary) for row in rows]


@router.post(LISTS, status_code=201)
def create_list(household_id: str, body: CreateList, request: Request, caller: Caller) -> ListDetail:
    list_id = str(uuid4())
    now = now_iso()
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        db.execute(
            """INSERT INTO shopping_lists (id, household_id, name, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (list_id, household_id, body.name, caller.user_id, now, now),
        )
        notify_changed(db, household_id)
        detail = list_detail(db, request, household_id, list_id, caller, is_primary)
    return detail


@router.get(LIST)
def get_list(household_id: str, list_id: str, request: Request, caller: Caller) -> ListDetail:
    with household_access(request, household_id, caller) as (db, is_primary):
        return list_detail(db, request, household_id, list_id, caller, is_primary)


def require_list_manager(row, caller: Identity, is_primary: bool) -> None:
    if not is_primary and row["created_by"] != caller.user_id:
        raise HTTPException(
            status_code=403, detail="Only the list creator or the household admin can change this list",
        )


@router.patch(LIST)
def rename_list(
    household_id: str, list_id: str, body: RenameList, request: Request, caller: Caller
) -> ListDetail:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list_manager(require_list(db, household_id, list_id), caller, is_primary)
        db.execute(
            "UPDATE shopping_lists SET name = ?, updated_at = ? WHERE id = ?",
            (body.name, now_iso(), list_id),
        )
        notify_changed(db, household_id)
        detail = list_detail(db, request, household_id, list_id, caller, is_primary)
    return detail


@router.delete(LIST, status_code=204)
def delete_list(household_id: str, list_id: str, request: Request, caller: Caller) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list_manager(require_list(db, household_id, list_id), caller, is_primary)
        # Keep the list row as a tombstone so the next live batch can tell open
        # screens the list is gone; its items are removed straight away.
        now = now_iso()
        db.execute("DELETE FROM shopping_list_items WHERE list_id = ?", (list_id,))
        db.execute(
            "UPDATE shopping_lists SET archived_at = ?, updated_at = ? WHERE id = ?", (now, now, list_id),
        )
        notify_changed(db, household_id)
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------
def insert_item(
    db: DatabaseConnection, request: Request, household_id: str, list_id: str, body: AddItem,
    caller: Identity, now: str,
) -> str:
    """Add one item, or bump the quantity of the same product if it is already waiting on the list."""
    existing = None
    if body.sku is not None:
        visible_product(request, body.sku, caller)
        existing = db.execute(
            """SELECT id, quantity FROM shopping_list_items
               WHERE list_id = ? AND sku = ? AND checked = 0
               ORDER BY created_at LIMIT 1""",
            (list_id, body.sku),
        ).fetchone()
    if existing is not None:
        item_id = existing["id"]
        db.execute(
            "UPDATE shopping_list_items SET quantity = ?, updated_at = ? WHERE id = ?",
            (min(MAX_LIST_QUANTITY, existing["quantity"] + body.quantity), now, item_id),
        )
        if body.note:
            db.execute("UPDATE shopping_list_items SET note = ? WHERE id = ?", (body.note, item_id))
        return item_id
    item_id = str(uuid4())
    db.execute(
        """INSERT INTO shopping_list_items
           (id, list_id, household_id, sku, custom_name, quantity, note, added_by,
            checked, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
        (
            item_id, list_id, household_id, body.sku, body.custom_name, body.quantity,
            body.note or None, caller.user_id, now, now,
        ),
    )
    return item_id


@router.post(f"{LIST}/items", status_code=201)
def add_item(
    household_id: str, list_id: str, body: AddItem, request: Request, caller: Caller
) -> ListItem:
    now = now_iso()
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list(db, household_id, list_id)
        item_id = insert_item(db, request, household_id, list_id, body, caller, now)
        touch(db, household_id, list_id, now)
        return to_item(require_item(db, list_id, item_id), caller, is_primary)


@router.post(f"{LIST}/items/bulk", status_code=201)
def add_items(
    household_id: str, list_id: str, body: AddItems, request: Request, caller: Caller
) -> ListDetail:
    """Add up to 50 items in one request and one transaction (e.g. a pasted list)."""
    started = datetime.now(timezone.utc)
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list(db, household_id, list_id)
        for index, item in enumerate(body.items):
            # One microsecond apart so the list keeps the pasted order.
            created = (started + timedelta(microseconds=index)).isoformat()
            insert_item(db, request, household_id, list_id, item, caller, created)
        touch(db, household_id, list_id, (started + timedelta(microseconds=len(body.items))).isoformat())
        return list_detail(db, request, household_id, list_id, caller, is_primary)


@router.post(f"{LIST}/changes")
def change_items(
    household_id: str, list_id: str, body: ListChanges, request: Request, caller: Caller
) -> ChangeResult:
    started = datetime.now(timezone.utc)
    now = (started + timedelta(microseconds=len(body.add))).isoformat()
    skipped: list[SkippedItem] = []
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list(db, household_id, list_id)
        for index, item in enumerate(body.add):
            try:
                insert_item(db, request, household_id, list_id, item, caller,
                            (started + timedelta(microseconds=index)).isoformat())
            except HTTPException as error:
                skipped.append(SkippedItem(id=item.sku or "", reason=str(error.detail)))
        for item in body.set:
            try:
                apply_item_update(db, request, list_id, item.id, item, caller, now)
            except HTTPException as error:
                skipped.append(SkippedItem(id=item.id, reason=str(error.detail)))
        for item_id in body.remove:
            try:
                apply_item_removal(db, request, list_id, item_id, caller, is_primary)
            except HTTPException as error:
                # Already gone is what the caller wanted.
                if error.status_code != 404:
                    skipped.append(SkippedItem(id=item_id, reason=str(error.detail)))
        touch(db, household_id, list_id, now)
        return ChangeResult(
            list=list_detail(db, request, household_id, list_id, caller, is_primary), skipped=skipped,
        )


@router.patch(f"{LIST}/items/{{item_id}}")
def update_item(
    household_id: str, list_id: str, item_id: str, body: UpdateItem, request: Request, caller: Caller
) -> ListItem:
    now = now_iso()
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list(db, household_id, list_id)
        apply_item_update(db, request, list_id, item_id, body, caller, now)
        touch(db, household_id, list_id, now)
        return to_item(require_item(db, list_id, item_id), caller, is_primary)


def apply_item_update(
    db: DatabaseConnection, request: Request, list_id: str, item_id: str, body: UpdateItem, caller: Identity, now: str,
) -> None:
    row = require_item(db, list_id, item_id)
    if not item_visible(request, caller, row["sku"]):
        raise HTTPException(status_code=404, detail="Item not found")
    if body.quantity is not None:
        db.execute("UPDATE shopping_list_items SET quantity = ? WHERE id = ?", (body.quantity, item_id))
    if "note" in body.model_fields_set:
        db.execute("UPDATE shopping_list_items SET note = ? WHERE id = ?", (body.note or None, item_id))
    if body.checked is not None and body.checked != bool(row["checked"]):
        db.execute(
            "UPDATE shopping_list_items SET checked = ?, checked_by = ?, checked_at = ? WHERE id = ?",
            (
                int(body.checked), caller.user_id if body.checked else None,
                now if body.checked else None, item_id,
            ),
        )
    db.execute("UPDATE shopping_list_items SET updated_at = ? WHERE id = ?", (now, item_id))


def apply_item_removal(
    db: DatabaseConnection, request: Request, list_id: str, item_id: str, caller: Identity, is_primary: bool,
) -> None:
    row = require_item(db, list_id, item_id)
    if not item_visible(request, caller, row["sku"]):
        raise HTTPException(status_code=404, detail="Item not found")
    if not is_primary and row["added_by"] != caller.user_id:
        raise HTTPException(status_code=403, detail="You can only remove items that you added.")
    db.execute("DELETE FROM shopping_list_items WHERE id = ?", (item_id,))


@router.delete(f"{LIST}/items/{{item_id}}", status_code=204)
def delete_item(
    household_id: str, list_id: str, item_id: str, request: Request, caller: Caller
) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list(db, household_id, list_id)
        apply_item_removal(db, request, list_id, item_id, caller, is_primary)
        touch(db, household_id, list_id, now_iso())
    return Response(status_code=204)


@router.post(f"{LIST}/items/clear-checked")
def clear_checked(household_id: str, list_id: str, request: Request, caller: Caller) -> ListDetail:
    """Remove ticked items the caller is allowed to remove (all of them for the primary)."""
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_list(db, household_id, list_id)
        query = "DELETE FROM shopping_list_items WHERE list_id = ? AND checked = 1"
        parameters: list = [list_id]
        if not is_primary:
            query += " AND added_by = ?"
            parameters.append(caller.user_id)
        db.execute(query, parameters)
        touch(db, household_id, list_id, now_iso())
        detail = list_detail(db, request, household_id, list_id, caller, is_primary)
    return detail


# ---------------------------------------------------------------------------
# Move to cart
# ---------------------------------------------------------------------------
def add_rows_to_cart(
    db: DatabaseConnection, request: Request, household_id: str, cart: CartResponse, caller: Identity,
    item_ids: list[str], now: str, *, find: str, list_id: str,
) -> MoveResult:
    """Put the chosen list items in a cart.

    Items that are not allowed are skipped with a reason instead of failing the rest.
    """
    moved: list[str] = []
    skipped: list[SkippedItem] = []
    session = checkout_session.current_session(db, household_id, cart.id)
    for item_id in dict.fromkeys(item_ids):
        row = db.execute(find, (list_id, item_id)).fetchone()
        if row is None or not item_visible(request, caller, row["sku"]):
            skipped.append(SkippedItem(id=item_id, reason="Item not found"))
            continue
        if row["sku"] is None:
            skipped.append(SkippedItem(id=item_id, reason="Not a catalogue product"))
            continue
        try:
            require_product_for_cart(request, row["sku"], caller, cart)
            in_cart = shared_quantity(db, cart.id, row["sku"])
            if session is not None:
                change_waiting(
                    db, household_id, cart, row["sku"], row["quantity"], caller, False, session, in_cart=in_cart,
                )
            else:
                checkout.require_stock(db, cart.inventory_store_id, row["sku"], in_cart + row["quantity"])
                current = contribution_quantity(db, cart.id, row["sku"], caller.user_id)
                set_contribution(
                    db, household_id, cart.id, row["sku"], caller.user_id,
                    current + row["quantity"],
                )
        except HTTPException as error:
            detail = error.detail
            reason = detail["message"] if isinstance(detail, dict) else str(detail)
            if isinstance(detail, dict) and detail.get("code") == "OUT_OF_STOCK":
                reason = f"Out of stock. {reason}"
            skipped.append(SkippedItem(id=item_id, reason=reason))
            continue
        moved.append(item_id)
    if moved and session is not None:
        checkout_session.record_waiting_change(db, household_id, cart.id, caller.user_id)
    elif moved:
        record_cart_change(db, household_id, cart.id, caller.user_id)
    return MoveResult(moved=moved, skipped=skipped)


@router.post(f"{LIST}/move-to-cart")
def move_to_cart(
    household_id: str, list_id: str, body: MoveToCart, request: Request, caller: Caller, key: OperationKey = None,
) -> MoveResult:
    now = now_iso()
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        operation = Operation(
            db, household_id, caller.user_id, key, ("move", household_id, list_id, body.model_dump()),
        )
        if operation.previous:
            return MoveResult(**operation.previous)
        require_list(db, household_id, list_id)
        cart = require_visible_cart(
            db, household_id, checkout_session.open_cart_id(db, body.target), caller, is_primary,
        )
        result = add_rows_to_cart(
            db, request, household_id, cart, caller, body.item_ids, now,
            find="SELECT id, sku, quantity FROM shopping_list_items WHERE list_id = ? AND id = ?", list_id=list_id,
        )
        for item_id in result.moved:
            db.execute(
                """UPDATE shopping_list_items SET checked = 1, checked_by = ?, checked_at = ?, updated_at = ?
                   WHERE id = ?""",
                (caller.user_id, now, now, item_id),
            )
        if result.moved:
            touch(db, household_id, list_id, now)
        return operation.save(result)


# ---------------------------------------------------------------------------
# Live channel: the worker publishes one snapshot per household every few
# seconds; each socket filters it for its own viewer in memory.
# ---------------------------------------------------------------------------
def lists_view(connection, snapshot: dict, caller: Identity) -> dict:
    is_primary = viewer_role(snapshot, caller.user_id)
    caller, _ = viewer_age(snapshot, caller)
    changed = []
    for entry in snapshot["lists"]:
        if entry["archived"]:
            changed.append({"id": entry["id"], "deleted": True})
            continue
        items = [
            to_item(row, caller, is_primary)
            for row in entry["items"] if item_visible(connection, caller, row["sku"])
        ]
        changed.append(ListDetail(**summarize(entry, items, caller, is_primary).model_dump(), items=items).model_dump())
    return {"type": "lists_update", "lists": changed}


def check_membership(socket: WebSocket, household_id: str, caller: Identity) -> None:
    with household_access(socket, household_id, caller):
        pass


@router.websocket(f"{LISTS}/live")
async def live_lists(socket: WebSocket, household_id: str) -> None:
    origin = socket.headers.get("origin")
    host = socket.headers.get("host")
    if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
        await socket.close(code=4403)
        return
    await socket.accept()
    sync = socket.app.state.live_sync
    secret = socket.app.state.jwt_secret
    try:
        first = await asyncio.wait_for(socket.receive(), timeout=5)
        if first["type"] == "websocket.disconnect":
            return
        auth = json.loads(first.get("text") or "null")
        token = auth.get("token") if isinstance(auth, dict) else None
        if not isinstance(token, str):
            raise InvalidTokenError("Expected token")
        caller = verify_token(token, secret)
        # The only database read this socket makes. The page loads its data over REST.
        await asyncio.to_thread(check_membership, socket, household_id, caller)
        async with sync.subscriber.pubsub() as pubsub:
            await pubsub.subscribe(sync.channel(household_id))
            await socket.send_json({"type": "ready", "interval": sync.interval})
            await sync.mark_online(household_id)
            last_presence = asyncio.get_running_loop().time()
            disconnect = asyncio.create_task(socket.receive())
            try:
                while not disconnect.done():
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                    verify_token(token, secret)
                    now = asyncio.get_running_loop().time()
                    if now - last_presence >= sync.interval / 2:
                        await sync.mark_online(household_id)
                        last_presence = now
                    if not message or message["type"] != "message":
                        continue
                    update = lists_view(socket, json.loads(message["data"]), caller)
                    if update["lists"]:
                        await socket.send_json(update)
            finally:
                disconnect.cancel()
                with suppress(asyncio.CancelledError):
                    await disconnect
    except WebSocketDisconnect:
        pass
    except (InvalidTokenError, ValueError, TimeoutError):
        await socket.close(code=4401)
    except HTTPException as error:
        await socket.close(code=4404, reason=str(error.detail)[:123])
    except AccessEnded as ended:
        await socket.close(code=4404, reason=ended.reason[:123])
    except RedisError:
        await socket.close(code=1013)
