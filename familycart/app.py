import asyncio
import json
import os
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Path as PathParameter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, StringConstraints
from redis.exceptions import RedisError
from starlette.requests import HTTPConnection

from shared.demo_members import MEMBERS_BY_ID
from shared.tokens import Identity, load_jwt_secret, verify_token

from .cart_sync import CartSync, latest_revision
from .catalogue import Product, load_catalogue
from .db import (
    active_member, contribution_quantity, delete_cart, initialize_database, load_carts,
    load_contributions, set_contribution, transaction,
)

bearer = HTTPBearer(auto_error=False)
HOUSEHOLD = "/households/{household_id}"
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    app.state.db_path = Path(os.environ.get("FAMILYCART_DB_PATH", "familycart.sqlite3"))
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
) -> Iterator[tuple[sqlite3.Connection, bool]]:
    with transaction(request.app.state.db_path, write=write) as db:
        member = active_member(db, household_id, caller.user_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Household not found")
        yield db, member["role"] == "primary"


@contextmanager
def change_cart(
    request: Request, household_id: str, cart_id: str, caller: Identity
) -> Iterator[tuple[sqlite3.Connection, bool]]:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        yield db, is_primary
        record_cart_change(db, household_id, cart_id)


def record_cart_change(db: sqlite3.Connection, household_id: str, cart_id: str) -> int:
    cursor = db.execute(
        "INSERT INTO cart_sync_events (household_id, cart_id) VALUES (?, ?)",
        (household_id, cart_id),
    )
    return cursor.lastrowid


def can_create_adults_only_cart(caller: Identity, is_primary: bool) -> bool:
    return is_primary and caller.is_adult


def cart_access(cart: sqlite3.Row, caller: Identity, is_primary: bool) -> CartResponse | None:
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
    db: sqlite3.Connection, household_id: str, cart_id: str, caller: Identity, is_primary: bool
) -> CartResponse:
    row = load_carts(db, household_id, caller.user_id, cart_id).fetchone()
    cart = cart_access(row, caller, is_primary) if row is not None else None
    if cart is None:
        raise HTTPException(status_code=404, detail="Cart not found")
    return cart


def require_primary(is_primary: bool) -> None:
    if not is_primary:
        raise HTTPException(status_code=403, detail="Only the household primary can manage access")


def can_delete_cart(db: sqlite3.Connection, cart: CartResponse, caller: Identity) -> bool:
    if cart.can_manage_access:
        return True
    row = db.execute("SELECT creator_id FROM carts WHERE id = ?", (cart.id,)).fetchone()
    return row["creator_id"] == caller.user_id


def household_members(db: sqlite3.Connection, household_id: str, *, include_roles: bool) -> list[MemberResponse]:
    rows = db.execute(
        "SELECT user_id, role FROM household_members WHERE household_id = ? AND active = 1 ORDER BY user_id",
        (household_id,),
    )
    members = []
    for row in rows:
        member_id = row["user_id"]
        fixture = MEMBERS_BY_ID.get(member_id)
        members.append(MemberResponse(
            id=member_id,
            display_name=fixture.display_name if fixture else member_id,
            role=row["role"] if include_roles else None,
            is_adult=bool(fixture and fixture.identity.is_adult) if include_roles else None,
        ))
    return members


def visible_carts(
    db: sqlite3.Connection, household_id: str, caller: Identity, is_primary: bool
) -> list[CartResponse]:
    visible = []
    for row in load_carts(db, household_id, caller.user_id):
        cart = cart_access(row, caller, is_primary)
        if cart is not None:
            visible.append(cart)
    return visible


@app.get("/products")
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


@app.get("/products/{sku}")
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
        if body.adults_only and not can_create_adults_only_cart(caller, is_primary):
            raise HTTPException(status_code=403, detail="Only an adult household primary can create an adults-only cart")
        db.execute(
            "INSERT INTO carts (id, household_id, creator_id, name, adults_only) VALUES (?, ?, ?, ?, ?)",
            (cart_id, household_id, caller.user_id, body.name, body.adults_only),
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
        if not can_delete_cart(db, cart, caller):
            raise HTTPException(status_code=403, detail="Only a primary or the cart creator can delete this cart")
        delete_cart(db, cart_id)
    return Response(status_code=204)


@app.get(f"{HOUSEHOLD}/members", response_model_exclude_none=True)
def list_members(household_id: str, request: Request, caller: Caller, include_roles: bool = False) -> list[MemberResponse]:
    with household_access(request, household_id, caller) as (db, is_primary):
        require_primary(is_primary)
        return household_members(db, household_id, include_roles=include_roles)


@app.put(f"{HOUSEHOLD}/members/{{member_id}}/primary", status_code=204)
def promote_member(household_id: str, member_id: str, request: Request, caller: Caller) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary and caller.is_adult)
        member = active_member(db, household_id, member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        target = MEMBERS_BY_ID.get(member_id)
        if target is None or not target.identity.is_adult:
            raise HTTPException(status_code=403, detail="Only an adult member can become a primary")
        if member["role"] == "primary":
            return Response(status_code=204)
        db.execute(
            "UPDATE household_members SET role = 'primary' WHERE household_id = ? AND user_id = ?",
            (household_id, member_id),
        )
        db.execute(
            "INSERT INTO cart_sync_events (household_id, cart_id) SELECT household_id, id FROM carts WHERE household_id = ?",
            (household_id,),
        )
    return Response(status_code=204)


def cart_grantees(db: sqlite3.Connection, cart: CartResponse, *, include_creator: bool) -> list[str]:
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
        ids = [user_id for user_id in ids if user_id in MEMBERS_BY_ID and MEMBERS_BY_ID[user_id].identity.is_adult]
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
        if member["role"] == "primary":
            return Response(status_code=204)
        target = MEMBERS_BY_ID.get(member_id)
        if cart.adults_only and (target is None or not target.identity.is_adult):
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
            INSERT OR IGNORE INTO cart_creator_revocations (cart_id)
            SELECT carts.id FROM carts JOIN household_members AS members
                ON members.household_id = carts.household_id AND members.user_id = carts.creator_id
            WHERE carts.id = ? AND carts.creator_id = ? AND members.role <> 'primary'
            """,
            (cart_id, member_id),
        )
    return Response(status_code=204)


def require_product_for_cart(request: Request, sku: str, caller: Identity, cart: CartResponse) -> None:
    product = visible_product(request, sku, caller)
    if product.min_age and not cart.adults_only:
        raise HTTPException(status_code=403, detail="This product requires an adults-only cart")


def save_quantity(
    db: sqlite3.Connection, household_id: str, cart_id: str, sku: str, contributor_id: str, quantity: int
) -> ContributionUpdate:
    set_contribution(db, household_id, cart_id, sku, contributor_id, quantity)
    revision = record_cart_change(db, household_id, cart_id)
    return ContributionUpdate(sku=sku, contributor_id=contributor_id, quantity=quantity, revision=revision)


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
        }
        if is_primary:
            view["members"] = [member.model_dump() for member in household_members(db, household_id, include_roles=True)]
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
