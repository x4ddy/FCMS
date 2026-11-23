import asyncio
import hashlib
import json
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timedelta, timezone
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path as PathParameter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, StringConstraints
from redis.exceptions import RedisError
from starlette.requests import HTTPConnection

from backend.shared.api import API_PREFIX
from backend.shared.database import DatabaseConnection
from backend.shared.demo_members import MEMBERS_BY_ID
from backend.shared.tokens import Identity, load_jwt_secret, verify_token

from .cart_sync import CartSync, latest_revision
from .catalogue import Product, load_catalogue
from .db import (
    active_member, contribution_quantity, database_target, delete_cart, initialize_database, load_carts,
    load_contributions, set_contribution, transaction,
)

bearer = HTTPBearer(auto_error=False)
HOUSEHOLD = f"{API_PREFIX}/households/{{household_id}}"
SKU = Annotated[str, PathParameter(min_length=1, max_length=100, pattern=r"^\S+$")]
MAX_QUANTITY = 2**63 - 1


class CreateCart(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    adults_only: bool = Field(default=False, strict=True)


class SetQuantity(BaseModel):
    quantity: int = Field(strict=True, ge=0, le=MAX_QUANTITY)


class AdjustQuantity(BaseModel):
    delta: int = Field(strict=True, ge=-1, le=1)


class CartResponse(BaseModel):
    id: str
    name: str
    adults_only: bool
    can_edit_items: bool
    can_manage_access: bool


class Contribution(BaseModel):
    sku: str
    contributor_id: str
    quantity: int


class ContributionUpdate(Contribution):
    revision: int


class SharedItemUpdate(BaseModel):
    sku: str
    quantity: int
    revision: int


class CartDetail(CartResponse):
    contributions: list[Contribution]
    can_delete_cart: bool


class MemberResponse(BaseModel):
    id: str
    display_name: str
    role: str | None = None
    is_adult: bool | None = None


class HouseholdCapabilities(BaseModel):
    can_create_adults_only_cart: bool


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class CreateHousehold(BaseModel):
    name: Name
    display_name: Name


class HouseholdResponse(BaseModel):
    id: str
    name: str
    role: str
    member_count: int


class CreateInvitationResponse(BaseModel):
    invitation_code: str
    expires_at: str


class AcceptInvitation(BaseModel):
    invitation_code: str = Field(min_length=20, max_length=200)
    display_name: Name


class AddressInput(BaseModel):
    label: Name
    recipient_name: Name
    address_line1: ShortText
    address_line2: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=160)] = None
    city: Name
    postal_code: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=24)] = None
    phone: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=24)] = None


class AddressResponse(AddressInput):
    id: str
    created_by: str


class PersonalCartItem(BaseModel):
    sku: str
    quantity: int


class PersonalCartResponse(BaseModel):
    items: list[PersonalCartItem]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    app.state.db_path = database_target()
    initialize_database(app.state.db_path)
    app.state.products = load_catalogue()
    sync = CartSync(app.state.db_path, os.environ.get("FAMILYCART_REDIS_URL", "redis://127.0.0.1:6379/0"))
    app.state.cart_sync = sync
    worker = asyncio.create_task(sync.run())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await sync.close()


app = FastAPI(title="FamilyCart", lifespan=lifespan)


def require_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    if credentials is not None:
        try:
            return verify_token(credentials.credentials, request.app.state.jwt_secret)
        except InvalidTokenError:
            pass

    raise HTTPException(
        status_code=401,
        detail="Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


Caller = Annotated[Identity, Depends(require_identity)]


@contextmanager
def household_access(
    request: HTTPConnection, household_id: str, caller: Identity, *, write: bool = False
) -> Iterator[tuple[DatabaseConnection, bool]]:
    with transaction(request.app.state.db_path, write=write) as db:
        member = active_member(db, household_id, caller.user_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Household not found")
        # Ownership is immutable: only the account that created the household
        # is the primary, regardless of mutable membership role data.
        yield db, member["owner_id"] == caller.user_id


@contextmanager
def change_cart(
    request: Request, household_id: str, cart_id: str, caller: Identity
) -> Iterator[tuple[DatabaseConnection, bool]]:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        yield db, is_primary
        record_cart_change(db, household_id, cart_id)


def record_cart_change(db: DatabaseConnection, household_id: str, cart_id: str) -> int:
    if db.dialect == "postgresql":
        row = db.execute(
            "INSERT INTO cart_sync_events (household_id, cart_id) VALUES (?, ?) RETURNING id",
            (household_id, cart_id),
        ).fetchone()
        return row["id"]
    cursor = db.execute(
        "INSERT INTO cart_sync_events (household_id, cart_id) VALUES (?, ?)",
        (household_id, cart_id),
    )
    return cursor.lastrowid


def can_create_adults_only_cart(caller: Identity, is_primary: bool) -> bool:
    return is_primary and caller.is_adult


def cart_access(cart, caller: Identity, is_primary: bool) -> CartResponse | None:
    adults_only = bool(cart["adults_only"])
    if adults_only and not caller.is_adult:
        return None
    if not is_primary:
        if cart["creator_id"] == caller.user_id:
            if cart["creator_revoked"]:
                return None
        elif not cart["has_grant"]:
            return None
    return CartResponse(
        id=cart["id"],
        name=cart["name"],
        adults_only=adults_only,
        can_edit_items=True,
        can_manage_access=is_primary,
    )


def require_visible_cart(
    db: DatabaseConnection, household_id: str, cart_id: str, caller: Identity, is_primary: bool
) -> CartResponse:
    row = load_carts(db, household_id, caller.user_id, cart_id).fetchone()
    cart = cart_access(row, caller, is_primary) if row is not None else None
    if cart is None:
        raise HTTPException(status_code=404, detail="Cart not found")
    return cart


def require_primary(is_primary: bool) -> None:
    if not is_primary:
        raise HTTPException(status_code=403, detail="Only the household creator can make this change")


def can_delete_cart(db: DatabaseConnection, cart: CartResponse, caller: Identity) -> bool:
    return cart.can_manage_access


def household_members(db: DatabaseConnection, household_id: str, *, include_roles: bool) -> list[MemberResponse]:
    rows = db.execute(
        """
        SELECT members.user_id, household.created_by AS owner_id,
               profiles.display_name, profiles.is_adult
        FROM household_members AS members
        JOIN household_profiles AS household ON household.household_id = members.household_id
        LEFT JOIN household_member_profiles AS profiles
          ON profiles.household_id = members.household_id
         AND profiles.user_id = members.user_id
        WHERE members.household_id = ? AND members.active = 1
        ORDER BY members.user_id
        """,
        (household_id,),
    )
    members = []
    for row in rows:
        member_id = row["user_id"]
        fixture = MEMBERS_BY_ID.get(member_id)
        members.append(MemberResponse(
            id=member_id,
            display_name=row["display_name"] or (fixture.display_name if fixture else member_id),
            role=("primary" if row["user_id"] == row["owner_id"] else "member") if include_roles else None,
            is_adult=(
                bool(row["is_adult"])
                if include_roles and row["is_adult"] is not None
                else bool(fixture and fixture.identity.is_adult) if include_roles else None
            ),
        ))
    return members


def household_response(db: DatabaseConnection, row) -> HouseholdResponse:
    member_count = db.execute(
        "SELECT COUNT(*) AS member_count FROM household_members WHERE household_id = ? AND active = 1",
        (row["household_id"],),
    ).fetchone()
    return HouseholdResponse(
        id=row["household_id"],
        name=row["name"] or "My Household",
        role=row["role"],
        member_count=member_count["member_count"],
    )


@app.get(f"{API_PREFIX}/personal-cart")
def get_personal_cart(request: Request, caller: Caller) -> PersonalCartResponse:
    with transaction(request.app.state.db_path) as db:
        rows = db.execute(
            """SELECT items.sku, items.quantity
               FROM personal_carts AS cart
               JOIN personal_cart_items AS items ON items.cart_id = cart.id
               WHERE cart.user_id = ? ORDER BY items.sku""",
            (caller.user_id,),
        )
        return PersonalCartResponse(items=[PersonalCartItem(**dict(row)) for row in rows])


@app.patch(f"{API_PREFIX}/personal-cart/items/{{sku}}")
def adjust_personal_cart_item(
    sku: SKU, body: AdjustQuantity, request: Request, caller: Caller
) -> PersonalCartItem:
    if body.delta == 0:
        raise HTTPException(status_code=422, detail="Quantity adjustment must be 1 or -1")
    visible_product(request, sku, caller)
    now = datetime.now(timezone.utc).isoformat()
    with transaction(request.app.state.db_path, write=True) as db:
        cart = db.execute(
            "SELECT id FROM personal_carts WHERE user_id = ?", (caller.user_id,)
        ).fetchone()
        if cart is None:
            cart_id = str(uuid4())
            db.execute(
                "INSERT INTO personal_carts (id, user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (cart_id, caller.user_id, now, now),
            )
        else:
            cart_id = cart["id"]
        current = db.execute(
            "SELECT quantity FROM personal_cart_items WHERE cart_id = ? AND sku = ?",
            (cart_id, sku),
        ).fetchone()
        quantity = max(0, (current["quantity"] if current else 0) + body.delta)
        if quantity > MAX_QUANTITY:
            raise HTTPException(status_code=409, detail="Maximum quantity reached")
        if quantity == 0:
            db.execute(
                "DELETE FROM personal_cart_items WHERE cart_id = ? AND sku = ?", (cart_id, sku)
            )
        else:
            db.execute(
                """INSERT INTO personal_cart_items (cart_id, sku, quantity, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (cart_id, sku) DO UPDATE SET
                     quantity = excluded.quantity, updated_at = excluded.updated_at""",
                (cart_id, sku, quantity, now),
            )
        db.execute("UPDATE personal_carts SET updated_at = ? WHERE id = ?", (now, cart_id))
        return PersonalCartItem(sku=sku, quantity=quantity)


@app.get(f"{API_PREFIX}/households")
def list_households(request: Request, caller: Caller) -> list[HouseholdResponse]:
    with transaction(request.app.state.db_path) as db:
        rows = db.execute(
            """
            SELECT members.household_id,
                   CASE WHEN profiles.created_by = members.user_id THEN 'primary' ELSE 'member' END AS role,
                   profiles.name
            FROM household_members AS members
            LEFT JOIN household_profiles AS profiles ON profiles.household_id = members.household_id
            WHERE members.user_id = ? AND members.active = 1
            ORDER BY profiles.created_at, members.household_id
            """,
            (caller.user_id,),
        ).fetchall()
        return [household_response(db, row) for row in rows]


@app.post(f"{API_PREFIX}/households", status_code=201)
def create_household(body: CreateHousehold, request: Request, caller: Caller) -> HouseholdResponse:
    household_id = str(uuid4())
    cart_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with transaction(request.app.state.db_path, write=True) as db:
        existing = db.execute(
            "SELECT 1 FROM household_members WHERE user_id = ? AND active = 1",
            (caller.user_id,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="You already belong to a household")
        db.execute("INSERT INTO households (id) VALUES (?)", (household_id,))
        db.execute(
            "INSERT INTO household_members (household_id, user_id, role) VALUES (?, ?, 'primary')",
            (household_id, caller.user_id),
        )
        db.execute(
            "INSERT INTO household_profiles (household_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
            (household_id, body.name, caller.user_id, now),
        )
        db.execute(
            """INSERT INTO household_member_profiles
               (household_id, user_id, display_name, is_adult) VALUES (?, ?, ?, ?)""",
            (household_id, caller.user_id, body.display_name, int(caller.is_adult)),
        )
        db.execute(
            """INSERT INTO carts (id, household_id, creator_id, name, adults_only)
               VALUES (?, ?, ?, 'Shared shopping cart', 0)""",
            (cart_id, household_id, caller.user_id),
        )
        record_cart_change(db, household_id, cart_id)
        row = db.execute(
            "SELECT ? AS household_id, 'primary' AS role, ? AS name",
            (household_id, body.name),
        ).fetchone()
        return household_response(db, row)


@app.post(f"{HOUSEHOLD}/invitations", status_code=201)
def create_invitation(
    household_id: str, request: Request, caller: Caller
) -> CreateInvitationResponse:
    code = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(code.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        db.execute(
            """INSERT INTO household_invitations
               (id, household_id, token_hash, invited_by, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                str(uuid4()), household_id, token_hash, caller.user_id,
                expires_at.isoformat(), now.isoformat(),
            ),
        )
    return CreateInvitationResponse(invitation_code=code, expires_at=expires_at.isoformat())


@app.post(f"{API_PREFIX}/household-invitations/accept")
def accept_invitation(
    body: AcceptInvitation, request: Request, caller: Caller
) -> HouseholdResponse:
    token_hash = hashlib.sha256(body.invitation_code.strip().encode()).hexdigest()
    now = datetime.now(timezone.utc)
    with transaction(request.app.state.db_path, write=True) as db:
        invitation = db.execute(
            """SELECT invitation.*, profiles.name
               FROM household_invitations AS invitation
               LEFT JOIN household_profiles AS profiles
                 ON profiles.household_id = invitation.household_id
               WHERE invitation.token_hash = ?""",
            (token_hash,),
        ).fetchone()
        if (
            invitation is None
            or invitation["accepted_at"] is not None
            or invitation["revoked_at"] is not None
            or datetime.fromisoformat(invitation["expires_at"]) <= now
        ):
            raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
        existing = db.execute(
            "SELECT household_id FROM household_members WHERE user_id = ? AND active = 1",
            (caller.user_id,),
        ).fetchone()
        if existing:
            raise HTTPException(status_code=409, detail="You already belong to a household")
        household_id = invitation["household_id"]
        db.execute(
            """INSERT INTO household_members (household_id, user_id, role, active)
               VALUES (?, ?, 'member', 1)
               ON CONFLICT (household_id, user_id) DO UPDATE SET active = 1, role = 'member'""",
            (household_id, caller.user_id),
        )
        db.execute(
            """INSERT INTO household_member_profiles
               (household_id, user_id, display_name, is_adult) VALUES (?, ?, ?, ?)
               ON CONFLICT (household_id, user_id) DO UPDATE SET
                 display_name = excluded.display_name, is_adult = excluded.is_adult""",
            (household_id, caller.user_id, body.display_name, int(caller.is_adult)),
        )
        db.execute(
            """INSERT INTO cart_grants (household_id, cart_id, member_id)
               SELECT household_id, id, ? FROM carts
               WHERE household_id = ? AND adults_only = 0
               ON CONFLICT (cart_id, member_id) DO NOTHING""",
            (caller.user_id, household_id),
        )
        db.execute(
            "UPDATE household_invitations SET accepted_by = ?, accepted_at = ? WHERE id = ?",
            (caller.user_id, now.isoformat(), invitation["id"]),
        )
        for cart in db.execute("SELECT id FROM carts WHERE household_id = ?", (household_id,)):
            record_cart_change(db, household_id, cart["id"])
        row = db.execute(
            "SELECT ? AS household_id, 'member' AS role, ? AS name",
            (household_id, invitation["name"]),
        ).fetchone()
        return household_response(db, row)


def visible_carts(
    db: DatabaseConnection, household_id: str, caller: Identity, is_primary: bool
) -> list[CartResponse]:
    visible = []
    for row in load_carts(db, household_id, caller.user_id):
        cart = cart_access(row, caller, is_primary)
        if cart is not None:
            visible.append(cart)
    return visible


@app.get(f"{API_PREFIX}/products")
def list_products(request: Request, caller: Caller, q: str = "") -> list[Product]:
    query = q.strip().casefold()
    return [
        product for product in request.app.state.products.values()
        if product.visible_to(caller)
        and query in f"{product.name} {product.category} {product.sku}".casefold()
    ]


def visible_product(request: Request, sku: str, caller: Identity) -> Product:
    product = request.app.state.products.get(sku)
    if product is None or not product.visible_to(caller):
        raise HTTPException(status_code=404, detail="Product not found")
    return product


def visible_contributions(connection: HTTPConnection, caller: Identity, rows: list[dict]) -> list[dict]:
    products = connection.app.state.products
    return [
        row for row in rows
        if (product := products.get(row["sku"])) is None or product.visible_to(caller)
    ]


@app.get(f"{API_PREFIX}/products/{{sku}}")
def get_product(request: Request, sku: SKU, caller: Caller) -> Product:
    return visible_product(request, sku, caller)


@app.get(f"{HOUSEHOLD}/carts")
def list_carts(
    household_id: str, request: Request, caller: Caller
) -> list[CartResponse]:
    with household_access(request, household_id, caller) as (db, is_primary):
        return visible_carts(db, household_id, caller, is_primary)


@app.get(f"{HOUSEHOLD}/capabilities")
def household_capabilities(household_id: str, request: Request, caller: Caller) -> HouseholdCapabilities:
    with household_access(request, household_id, caller) as (_, is_primary):
        return HouseholdCapabilities(can_create_adults_only_cart=can_create_adults_only_cart(caller, is_primary))


@app.post(f"{HOUSEHOLD}/carts", status_code=201)
def create_cart(
    household_id: str, body: CreateCart, request: Request, caller: Caller
) -> CartResponse:
    cart_id = str(uuid4())
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        require_primary(is_primary)
        if body.adults_only and not can_create_adults_only_cart(caller, is_primary):
            raise HTTPException(status_code=403, detail="Only an adult household primary can create an adults-only cart")
        db.execute(
            "INSERT INTO carts (id, household_id, creator_id, name, adults_only) VALUES (?, ?, ?, ?, ?)",
            (cart_id, household_id, caller.user_id, body.name, body.adults_only),
        )
        db.execute(
            """INSERT INTO cart_grants (household_id, cart_id, member_id)
               SELECT members.household_id, ?, members.user_id
               FROM household_members AS members
               LEFT JOIN household_member_profiles AS profiles
                 ON profiles.household_id = members.household_id
                AND profiles.user_id = members.user_id
               WHERE members.household_id = ? AND members.active = 1
                 AND members.user_id <> ?
                 AND (? = 0 OR profiles.is_adult = 1)
               ON CONFLICT (cart_id, member_id) DO NOTHING""",
            (cart_id, household_id, caller.user_id, int(body.adults_only)),
        )
        return require_visible_cart(db, household_id, cart_id, caller, is_primary)


@app.get(f"{HOUSEHOLD}/carts/{{cart_id}}")
def get_cart(household_id: str, cart_id: str, request: Request, caller: Caller) -> CartDetail:
    with household_access(request, household_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        rows = visible_contributions(request, caller, load_contributions(db, cart_id))
        return CartDetail(**cart.model_dump(), contributions=rows, can_delete_cart=can_delete_cart(db, cart, caller))


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}", status_code=204)
def remove_cart(household_id: str, cart_id: str, request: Request, caller: Caller) -> Response:
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(is_primary)
        delete_cart(db, cart_id)
    return Response(status_code=204)


@app.get(f"{HOUSEHOLD}/members", response_model_exclude_none=True)
def list_members(household_id: str, request: Request, caller: Caller, include_roles: bool = False) -> list[MemberResponse]:
    with household_access(request, household_id, caller) as (db, is_primary):
        require_primary(is_primary)
        return household_members(db, household_id, include_roles=include_roles)


@app.put(f"{HOUSEHOLD}/members/{{member_id}}/primary", status_code=204)
def promote_member(household_id: str, member_id: str, request: Request, caller: Caller) -> Response:
    with household_access(request, household_id, caller) as (_, is_primary):
        require_primary(is_primary)
        raise HTTPException(status_code=409, detail="The household creator is the permanent primary")


@app.delete(f"{HOUSEHOLD}/members/{{member_id}}", status_code=204)
def revoke_member(
    household_id: str, member_id: str, request: Request, caller: Caller
) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        if member_id == caller.user_id:
            raise HTTPException(status_code=409, detail="A primary cannot revoke their own access")
        member = active_member(db, household_id, member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if member["owner_id"] == member_id:
            raise HTTPException(status_code=409, detail="Another primary cannot be revoked")
        db.execute(
            "UPDATE household_members SET active = 0 WHERE household_id = ? AND user_id = ?",
            (household_id, member_id),
        )
        db.execute(
            "DELETE FROM cart_grants WHERE household_id = ? AND member_id = ?",
            (household_id, member_id),
        )
        for cart in db.execute("SELECT id FROM carts WHERE household_id = ?", (household_id,)):
            record_cart_change(db, household_id, cart["id"])
    return Response(status_code=204)


@app.get(f"{HOUSEHOLD}/addresses")
def list_addresses(
    household_id: str, request: Request, caller: Caller
) -> list[AddressResponse]:
    with household_access(request, household_id, caller) as (db, _):
        rows = db.execute(
            """SELECT id, label, recipient_name, address_line1, address_line2,
                      city, postal_code, phone, created_by
               FROM household_addresses WHERE household_id = ? ORDER BY created_at, id""",
            (household_id,),
        )
        return [AddressResponse(**dict(row)) for row in rows]


@app.post(f"{HOUSEHOLD}/addresses", status_code=201)
def create_address(
    household_id: str, body: AddressInput, request: Request, caller: Caller
) -> AddressResponse:
    address_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with household_access(request, household_id, caller, write=True) as (db, _):
        db.execute(
            """INSERT INTO household_addresses
               (id, household_id, label, recipient_name, address_line1, address_line2,
                city, postal_code, phone, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                address_id, household_id, body.label, body.recipient_name,
                body.address_line1, body.address_line2 or None, body.city,
                body.postal_code or None, body.phone or None, caller.user_id, now, now,
            ),
        )
    return AddressResponse(id=address_id, created_by=caller.user_id, **body.model_dump())


@app.delete(f"{HOUSEHOLD}/addresses/{{address_id}}", status_code=204)
def delete_address(
    household_id: str, address_id: str, request: Request, caller: Caller
) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        address = db.execute(
            "SELECT created_by FROM household_addresses WHERE household_id = ? AND id = ?",
            (household_id, address_id),
        ).fetchone()
        if address is None:
            raise HTTPException(status_code=404, detail="Address not found")
        if not is_primary and address["created_by"] != caller.user_id:
            raise HTTPException(status_code=403, detail="You can only remove an address you added")
        db.execute("DELETE FROM household_addresses WHERE id = ?", (address_id,))
    return Response(status_code=204)


def cart_grantees(db: DatabaseConnection, cart: CartResponse, *, include_creator: bool) -> list[str]:
    rows = db.execute(
        """
        SELECT members.user_id
        FROM carts JOIN household_members AS members ON members.household_id = carts.household_id
        LEFT JOIN cart_grants ON cart_grants.cart_id = carts.id AND cart_grants.member_id = members.user_id
        LEFT JOIN cart_creator_revocations ON cart_creator_revocations.cart_id = carts.id
        WHERE carts.id = ? AND members.active = 1 AND members.role <> 'primary'
            AND (cart_grants.member_id IS NOT NULL OR (
                ? AND members.user_id = carts.creator_id AND cart_creator_revocations.cart_id IS NULL
            ))
        ORDER BY members.user_id
        """,
        (cart.id, include_creator),
    )
    ids = [row["user_id"] for row in rows]
    if include_creator and cart.adults_only:
        adult_rows = db.execute(
            "SELECT user_id FROM household_member_profiles WHERE household_id = ? AND is_adult = 1",
            (db.execute("SELECT household_id FROM carts WHERE id = ?", (cart.id,)).fetchone()["household_id"],),
        )
        adult_ids = {row["user_id"] for row in adult_rows}
        adult_ids.update(
            user_id for user_id, fixture in MEMBERS_BY_ID.items() if fixture.identity.is_adult
        )
        ids = [user_id for user_id in ids if user_id in adult_ids]
    return ids


@app.get(f"{HOUSEHOLD}/carts/{{cart_id}}/grants")
def list_grants(
    household_id: str, cart_id: str, request: Request, caller: Caller, include_creator: bool = False
) -> list[str]:
    with household_access(request, household_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(cart.can_manage_access)
        return cart_grantees(db, cart, include_creator=include_creator)


@app.put(f"{HOUSEHOLD}/carts/{{cart_id}}/grants/{{member_id}}", status_code=204)
def grant_access(
    household_id: str, cart_id: str, member_id: str, request: Request, caller: Caller
) -> Response:
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(cart.can_manage_access)
        member = active_member(db, household_id, member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if member["owner_id"] == member_id:
            return Response(status_code=204)
        profile = db.execute(
            "SELECT is_adult FROM household_member_profiles WHERE household_id = ? AND user_id = ?",
            (household_id, member_id),
        ).fetchone()
        fixture = MEMBERS_BY_ID.get(member_id)
        target_is_adult = bool(profile["is_adult"]) if profile else bool(
            fixture and fixture.identity.is_adult
        )
        if cart.adults_only and not target_is_adult:
            raise HTTPException(
                status_code=403, detail="Adults-only grants require a verified adult member"
            )
        db.execute(
            "DELETE FROM cart_creator_revocations WHERE cart_id IN (SELECT id FROM carts WHERE id = ? AND creator_id = ?)",
            (cart_id, member_id),
        )
        db.execute(
            """
            INSERT INTO cart_grants (household_id, cart_id, member_id) VALUES (?, ?, ?)
            ON CONFLICT (cart_id, member_id) DO NOTHING
            """,
            (household_id, cart_id, member_id),
        )
    return Response(status_code=204)


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/grants/{{member_id}}", status_code=204)
def revoke_access(
    household_id: str, cart_id: str, member_id: str, request: Request, caller: Caller
) -> Response:
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(cart.can_manage_access)
        db.execute(
            "DELETE FROM cart_grants WHERE cart_id = ? AND member_id = ?", (cart_id, member_id)
        )
        db.execute(
            """
            INSERT INTO cart_creator_revocations (cart_id)
            SELECT carts.id FROM carts JOIN household_members AS members
                ON members.household_id = carts.household_id AND members.user_id = carts.creator_id
            JOIN household_profiles AS household ON household.household_id = carts.household_id
            WHERE carts.id = ? AND carts.creator_id = ? AND household.created_by <> carts.creator_id
            ON CONFLICT (cart_id) DO NOTHING
            """,
            (cart_id, member_id),
        )
    return Response(status_code=204)


def require_product_for_cart(request: Request, sku: str, caller: Identity, cart: CartResponse) -> None:
    product = visible_product(request, sku, caller)
    if product.min_age and not cart.adults_only:
        raise HTTPException(status_code=403, detail="This product requires an adults-only cart")


def save_quantity(
    db: DatabaseConnection, household_id: str, cart_id: str, sku: str, contributor_id: str, quantity: int
) -> ContributionUpdate:
    set_contribution(db, household_id, cart_id, sku, contributor_id, quantity)
    revision = record_cart_change(db, household_id, cart_id)
    return ContributionUpdate(sku=sku, contributor_id=contributor_id, quantity=quantity, revision=revision)


def shared_quantity(db: DatabaseConnection, cart_id: str, sku: str) -> int:
    row = db.execute(
        "SELECT COALESCE(SUM(quantity), 0) AS quantity FROM cart_item_contributions WHERE cart_id = ? AND sku = ?",
        (cart_id, sku),
    ).fetchone()
    return row["quantity"]


@app.patch(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}")
def adjust_shared_quantity(
    household_id: str, cart_id: str, sku: SKU, body: AdjustQuantity, request: Request, caller: Caller
) -> SharedItemUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if body.delta == 0:
            raise HTTPException(status_code=422, detail="Quantity adjustment must be 1 or -1")
        total = shared_quantity(db, cart_id, sku)
        if body.delta > 0:
            require_product_for_cart(request, sku, caller, cart)
            if total >= MAX_QUANTITY:
                raise HTTPException(status_code=409, detail="Maximum quantity reached")
            contributor_id = caller.user_id
            quantity = contribution_quantity(db, cart_id, sku, contributor_id) + 1
        elif total == 0:
            return SharedItemUpdate(sku=sku, quantity=0, revision=latest_revision(db))
        else:
            if is_primary:
                contribution = db.execute(
                    """SELECT contributor_id, quantity FROM cart_item_contributions
                       WHERE cart_id = ? AND sku = ?
                       ORDER BY CASE WHEN contributor_id = ? THEN 0 ELSE 1 END, contributor_id
                       LIMIT 1""",
                    (cart_id, sku, caller.user_id),
                ).fetchone()
            else:
                contribution = db.execute(
                    """SELECT contributor_id, quantity FROM cart_item_contributions
                       WHERE cart_id = ? AND sku = ? AND contributor_id = ?""",
                    (cart_id, sku, caller.user_id),
                ).fetchone()
                if contribution is None:
                    return SharedItemUpdate(sku=sku, quantity=total, revision=latest_revision(db))
            contributor_id = contribution["contributor_id"]
            quantity = contribution["quantity"] - 1
        set_contribution(db, household_id, cart_id, sku, contributor_id, quantity)
        revision = record_cart_change(db, household_id, cart_id)
        return SharedItemUpdate(sku=sku, quantity=total + body.delta, revision=revision)


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}")
def remove_shared_item(
    household_id: str, cart_id: str, sku: SKU, request: Request, caller: Caller
) -> SharedItemUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(is_primary)
        db.execute(
            "DELETE FROM cart_item_contributions WHERE cart_id = ? AND sku = ?",
            (cart_id, sku),
        )
        revision = record_cart_change(db, household_id, cart_id)
        return SharedItemUpdate(sku=sku, quantity=0, revision=revision)


@app.put(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}/contributions/me")
def update_quantity(
    household_id: str, cart_id: str, sku: SKU, body: SetQuantity, request: Request, caller: Caller
) -> ContributionUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if body.quantity > 0:
            require_product_for_cart(request, sku, caller, cart)
        return save_quantity(db, household_id, cart_id, sku, caller.user_id, body.quantity)


@app.patch(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}/contributions/me")
def adjust_quantity(
    household_id: str, cart_id: str, sku: SKU, body: AdjustQuantity, request: Request, caller: Caller
) -> ContributionUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if body.delta == 0:
            raise HTTPException(status_code=422, detail="Quantity adjustment must be 1 or -1")
        if body.delta > 0:
            require_product_for_cart(request, sku, caller, cart)
        quantity = max(0, contribution_quantity(db, cart_id, sku, caller.user_id) + body.delta)
        if quantity > MAX_QUANTITY:
            raise HTTPException(status_code=409, detail="Maximum quantity reached")
        return save_quantity(db, household_id, cart_id, sku, caller.user_id, quantity)


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}/contributions/{{contributor_id}}")
def remove_contribution(
    household_id: str, cart_id: str, sku: SKU, contributor_id: str, request: Request, caller: Caller
) -> ContributionUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if contributor_id != caller.user_id and not is_primary:
            raise HTTPException(status_code=403, detail="You can only remove your own items")
        return save_quantity(db, household_id, cart_id, sku, contributor_id, 0)


def live_view(
    socket: WebSocket, household_id: str, caller: Identity, cart_id: str | None, batch: dict | None
) -> dict:
    with household_access(socket, household_id, caller) as (db, is_primary):
        carts = visible_carts(db, household_id, caller, is_primary)
        revision = batch["revision"] if batch else latest_revision(db)
        view = {
            "revision": revision,
            "carts": [cart.model_dump() for cart in carts],
            "can_create_adults_only_cart": can_create_adults_only_cart(caller, is_primary),
            # Every household member may see display names so cart activity can
            # be attributed. Roles and age flags remain primary-only fields.
            "members": [
                member.model_dump()
                for member in household_members(db, household_id, include_roles=is_primary)
            ],
        }
        if cart_id is None:
            return view
        selected = next((cart for cart in carts if cart.id == cart_id), None)
        if selected is None:
            view["cart"] = None
        elif batch is None or cart_id in batch["carts"]:
            contributions = batch["carts"][cart_id] if batch else load_contributions(db, cart_id)
            view["cart"] = {
                **selected.model_dump(),
                "can_delete_cart": can_delete_cart(db, selected, caller),
                "contributions": visible_contributions(socket, caller, contributions),
            }
            if selected.can_manage_access:
                view["grants"] = cart_grantees(db, selected, include_creator=True)
        return view


@app.websocket(f"{HOUSEHOLD}/live")
async def live_carts(socket: WebSocket, household_id: str, cart_id: str | None = None) -> None:
    origin = socket.headers.get("origin")
    host = socket.headers.get("host")
    if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
        await socket.close(code=4403)
        return
    await socket.accept()
    sync = socket.app.state.cart_sync
    secret = socket.app.state.jwt_secret
    try:
        authorization = await asyncio.wait_for(socket.receive(), timeout=5)
        if authorization["type"] == "websocket.disconnect":
            return
        auth_text = authorization.get("text")
        auth = json.loads(auth_text) if auth_text is not None else None
        token = auth.get("token") if isinstance(auth, dict) else None
        if not isinstance(token, str):
            raise InvalidTokenError("Expected token")
        caller = verify_token(token, secret)
        async with sync.subscriber.pubsub() as pubsub:
            channel = sync.channel(household_id)
            await pubsub.subscribe(channel)
            subscribed = await pubsub.get_message(timeout=5)
            if not subscribed or subscribed["type"] != "subscribe":
                await socket.close(code=1013)
                return
            view = await asyncio.to_thread(live_view, socket, household_id, caller, cart_id, None)
            revision = view["revision"]
            await socket.send_json(view)
            disconnect = asyncio.create_task(socket.receive())
            try:
                while not disconnect.done():
                    message = await pubsub.get_message(timeout=1)
                    verify_token(token, secret)
                    if not message:
                        continue
                    batch = json.loads(message["data"]) if message["type"] == "message" else None
                    if batch is not None and batch["revision"] <= revision:
                        continue
                    view = await asyncio.to_thread(live_view, socket, household_id, caller, cart_id, batch)
                    revision = view["revision"]
                    await socket.send_json(view)
            finally:
                disconnect.cancel()
                with suppress(asyncio.CancelledError):
                    await disconnect
    except WebSocketDisconnect:
        pass
    except (InvalidTokenError, ValueError, TimeoutError):
        await socket.close(code=4401)
    except HTTPException:
        await socket.close(code=4404)
    except RedisError:
        await socket.close(code=1013)
