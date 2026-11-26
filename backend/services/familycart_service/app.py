import asyncio
import hashlib
import json
import logging
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from random import uniform
from typing import Annotated
from uuid import uuid4

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Path as PathParameter, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pydantic import BaseModel, Field, StringConstraints, UUID4, ValidationError, model_validator
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse

from backend.shared.api import API_PREFIX
from backend.shared.cache import cache_redis_url, live_redis_url
from backend.shared.database import DatabaseConnection, database_error_types, database_unavailable_types
from backend.shared.demo_members import MEMBERS_BY_ID
from backend.shared.maps import (
    Coordinates, CoverageResult, Latitude, Longitude, MAPS_UNAVAILABLE, address_validation_error,
    coverage_result, maps_service_url,
)
from backend.shared.tokens import Identity, is_child_account, load_jwt_secret, verify_token
from backend.services.refresh_service.service import session_profile
from backend.services.user_service.db import birth_dates, database_path as user_database_path

from . import checkout, checkout_session, child_codes
from .operations import Operation, OperationKey, applied_keys, event_key
from .live_sync import (
    HOUSEHOLD_DELETED, AccessEnded, LiveSync, build_snapshots, household_revision, notify_changed, viewer_role,
)
from .catalogue import Product, load_catalogue
from .db import (
    ACTIVE_ADDRESS, ADDRESS_FIELDS, active_member, contribution_quantity, database_target, delete_cart,
    initialize_database, load_carts,
    household_member, load_contributions, lock_household, record_event, set_contribution, transaction,
)

logger = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
HOUSEHOLD = f"{API_PREFIX}/households/{{household_id}}"
HOUSEHOLD_ADDRESS = f"{HOUSEHOLD}/addresses/{{address_id}}"
SKU = Annotated[str, PathParameter(min_length=1, max_length=100, pattern=r"^\S+$")]
MAX_QUANTITY = 2**63 - 1


class CreateCart(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    adults_only: bool = Field(default=False, strict=True)


class SetQuantity(BaseModel):
    quantity: int = Field(strict=True, ge=0, le=MAX_QUANTITY)


class AdjustQuantity(BaseModel):
    # Up to 99 steps at once, so the app can combine several fast taps into one request.
    delta: int = Field(strict=True, ge=-99, le=99)


class CartResponse(BaseModel):
    id: str
    name: str
    adults_only: bool
    can_edit_items: bool
    can_manage_access: bool
    can_checkout: bool
    inventory_store_id: str | None = None
    inventory_store_name: str | None = None


class Contribution(BaseModel):
    sku: str
    contributor_id: str
    quantity: int


class ContributionUpdate(Contribution):
    revision: int


class SharedItemUpdate(BaseModel):
    sku: str
    # In the cart and waiting for the next one, together.
    quantity: int
    revision: int
    waiting: bool = False
    # Where the change landed (a checked-out cart may have been replaced) and the product's lines there.
    cart_id: str = ""
    lines: list[Contribution] = []


class CheckoutSession(BaseModel):
    user_id: str
    expires_at: str
    display_name: str


class CheckoutClaim(BaseModel):
    started_at: str


class CartDetail(CartResponse):
    contributions: list[Contribution]
    can_delete_cart: bool
    # Set while somebody is checking this cart out; items added meanwhile wait for the next cart.
    checkout: CheckoutSession | None = None
    waiting: list[Contribution] = []
    # Read together with the contents: how new they are, and which of the caller's own changes they include.
    revision: int = 0
    applied: list[str] = []


class MemberRequestInput(BaseModel):
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    note: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=200)] = None


class MemberRequest(BaseModel):
    id: str
    requested_by: str
    name: str
    note: str | None = None
    status: str
    created_at: str


class MemberRequestDecision(BaseModel):
    approved: bool = Field(strict=True)


class MemberResponse(BaseModel):
    is_child_account: bool | None = None
    id: str
    display_name: str
    role: str | None = None
    is_adult: bool | None = None
    # Only sent to the primary, who decides.
    age_status: str | None = None
    claimed_age: int | None = None


class AgeDecision(BaseModel):
    approved: bool = Field(strict=True)


class HouseholdCapabilities(BaseModel):
    can_create_adults_only_cart: bool
    is_adult: bool
    age_status: str


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=160)]


class HouseholdResponse(BaseModel):
    id: str
    name: str
    role: str
    member_count: int


class CreateInvitationResponse(BaseModel):
    invitation_code: str
    expires_at: str


class AddressInput(BaseModel):
    label: Name
    recipient_name: Name
    address_line1: ShortText
    address_line2: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=160)] = None
    city: Name
    postal_code: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=24)] = None
    phone: Annotated[str | None, StringConstraints(strip_whitespace=True, max_length=24)] = None
    latitude: Latitude | None = None
    longitude: Longitude | None = None

    @model_validator(mode="after")
    def require_coordinate_pair(self):
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("Provide both latitude and longitude, or leave both empty")
        return self


class CreateHousehold(BaseModel):
    name: Name
    display_name: Name
    # Everyone in a household, primary or member, gives at least one delivery address.
    address: AddressInput


class AcceptInvitation(BaseModel):
    invitation_code: str = Field(min_length=20, max_length=200)
    display_name: Name
    address: AddressInput


class AddressResponse(AddressInput):
    id: str
    created_by: str


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    app.state.db_path = database_target()
    initialize_database(app.state.db_path)
    app.state.products = load_catalogue()
    app.state.user_db_path = user_database_path()
    app.state.demo_checkout = os.environ.get("FAMILYCART_DEMO_CHECKOUT", "0") == "1"
    with transaction(app.state.db_path, write=True) as db:
        checkout.seed_inventory(db, app.state.products)
    app.state.maps_url = maps_service_url()
    app.state.maps_http = httpx.Client(timeout=httpx.Timeout(4, connect=1), trust_env=False)
    sync = LiveSync(app.state.db_path, live_redis_url(), cache_redis_url())
    app.state.live_sync = sync
    worker = asyncio.create_task(sync.run())
    try:
        yield
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        await sync.close()
        app.state.maps_http.close()


app = FastAPI(title="FamilyCart", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(request: Request, error: RequestValidationError) -> Response:
    route = request.scope["route"].path
    if route.startswith((f"{API_PREFIX}/addresses", f"{HOUSEHOLD}/addresses", f"{API_PREFIX}/checkout/")):
        return address_validation_error(error)
    return await request_validation_exception_handler(request, error)


async def database_unavailable(request: Request, error: Exception) -> Response:
    # A database problem is not a verdict on the request, and it may even have been committed. It can be retried.
    logger.warning("Database unavailable: %s", type(error).__name__)
    return JSONResponse(
        {"detail": "The service is busy. Try again shortly."}, status_code=503,
        headers={"Retry-After": "1", "Cache-Control": "no-store"},
    )


for error_type in database_unavailable_types():
    app.add_exception_handler(error_type, database_unavailable)


@dataclass
class RequestIdentity:
    """The caller of one request.

    ``is_adult`` starts as what the account says. household_access() lowers it when
    that household's primary has not approved it, so every later rule sees the result.
    """

    user_id: str
    is_adult: bool
    session_id: str | None = None


def age_allowed(age_status: str | None) -> bool:
    return age_status in (None, "APPROVED")


def apply_account_age(db: DatabaseConnection, caller) -> None:
    """Outside any one household: a member still waiting everywhere does not count as an adult."""
    if not caller.is_adult:
        return
    statuses = [row["age_status"] for row in db.execute(
        "SELECT age_status FROM household_members WHERE user_id = ? AND active = 1",
        (caller.user_id,),
    ).fetchall()]
    if statuses and not any(age_allowed(status) for status in statuses):
        caller.is_adult = False


def require_identity(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> RequestIdentity:
    if credentials is not None:
        try:
            identity = verify_token(credentials.credentials, request.app.state.jwt_secret)
            return RequestIdentity(identity.user_id, identity.is_adult, identity.session_id)
        except InvalidTokenError:
            pass

    raise HTTPException(
        status_code=401,
        detail="Missing or invalid bearer token",
        headers={"WWW-Authenticate": "Bearer"},
    )


Caller = Annotated[RequestIdentity, Depends(require_identity)]


@contextmanager
def household_access(
    request: HTTPConnection, household_id: str, caller: Identity, *, write: bool = False,
    conceal_revoked: bool = False, lock_membership: bool = False, snapshot: bool = False,
) -> Iterator[tuple[DatabaseConnection, bool]]:
    with transaction(request.app.state.db_path, write=write, snapshot=snapshot) as db:
        if write and not lock_membership:
            member = lock_household(db, household_id, caller.user_id)
        else:
            if write:
                lock_household(db, household_id)
            member = active_member(db, household_id, caller.user_id, lock=lock_membership)
        if member is None:
            membership = None if conceal_revoked else household_member(db, household_id, caller.user_id)
            if membership is not None and not bool(membership["active"]):
                primary_name = household_primary_name(db, household_id)
                if not bool(household_member(db, household_id, membership["owner_id"])["active"]):
                    raise HTTPException(status_code=404, detail=HOUSEHOLD_DELETED)
                raise HTTPException(
                    status_code=403,
                    detail=f"Your household access was revoked. Contact {primary_name}, the household admin.",
                )
            raise HTTPException(status_code=404, detail="Household not found")
        if isinstance(caller, RequestIdentity) and not age_allowed(member["age_status"]):
            caller.is_adult = False
        # The household profile identifies the current primary, including after a transfer.
        yield db, member["owner_id"] == caller.user_id


@contextmanager
def change_cart(
    request: Request, household_id: str, cart_id: str, caller: Identity
) -> Iterator[tuple[DatabaseConnection, bool]]:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        yield db, is_primary
        record_cart_change(db, household_id, cart_id, caller.user_id)


def record_cart_change(
    db: DatabaseConnection, household_id: str, cart_id: str, actor_id: str | None = None, *, reason: str | None = None
) -> int:
    notify_changed(db, household_id)
    return record_event(db, household_id, cart_id, actor_id, reason, event_key(db), column="revision")


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
    store_id = cart["inventory_store_id"] if "inventory_store_id" in cart.keys() else None
    return CartResponse(
        id=cart["id"],
        name=cart["name"],
        adults_only=adults_only,
        can_edit_items=True,
        can_manage_access=is_primary,
        can_checkout=checkout.eligible(caller),
        inventory_store_id=store_id,
        inventory_store_name=checkout.STORE_NAMES.get(store_id),
    )


def household_primary_name(db: DatabaseConnection, household_id: str) -> str:
    row = db.execute(
        """SELECT admin.display_name
           FROM households AS household
           LEFT JOIN household_members AS admin
             ON admin.household_id = household.id AND admin.user_id = household.admin_id
           WHERE household.id = ?""",
        (household_id,),
    ).fetchone()
    return row["display_name"] if row and row["display_name"] else "the household admin"


def cart_access_revoked_message(db: DatabaseConnection, household_id: str) -> str:
    primary_name = household_primary_name(db, household_id)
    return f"You no longer have access to this cart. Contact {primary_name}, the household admin."


def require_visible_cart(
    db: DatabaseConnection, household_id: str, cart_id: str, caller: Identity, is_primary: bool
) -> CartResponse:
    row = load_carts(db, household_id, caller.user_id, cart_id).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Cart not found")
    if bool(row["adults_only"]) and not caller.is_adult:
        raise HTTPException(status_code=404, detail="Cart not found")
    cart = cart_access(row, caller, is_primary)
    if cart is None:
        raise HTTPException(status_code=403, detail=cart_access_revoked_message(db, household_id))
    return cart


def require_primary(is_primary: bool) -> None:
    if not is_primary:
        raise HTTPException(status_code=403, detail="Only the household admin can make this change")


def can_delete_cart(db: DatabaseConnection, cart: CartResponse, caller: Identity) -> bool:
    return cart.can_manage_access


def household_members(db: DatabaseConnection, household_id: str, *, include_roles: bool) -> list[MemberResponse]:
    rows = db.execute(
        """
        SELECT members.user_id, household.admin_id AS owner_id,
               members.display_name, members.is_adult, members.age_status
        FROM household_members AS members
        JOIN households AS household ON household.id = members.household_id
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
            id=member_id, is_child_account=True if is_child_account(member_id) else None,
            display_name=row["display_name"] or (fixture.display_name if fixture else member_id),
            role=("primary" if row["user_id"] == row["owner_id"] else "member") if include_roles else None,
            is_adult=(
                bool(row["is_adult"])
                if include_roles and row["is_adult"] is not None
                else bool(fixture and fixture.identity.is_adult) if include_roles else None
            ),
            age_status=(row["age_status"] or "APPROVED") if include_roles else None,
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


@app.get(f"{API_PREFIX}/households")
def list_households(request: Request, caller: Caller) -> list[HouseholdResponse]:
    with transaction(request.app.state.db_path) as db:
        rows = db.execute(
            """
            SELECT members.household_id,
                   CASE WHEN household.admin_id = members.user_id THEN 'primary' ELSE 'member' END AS role,
                   household.name
            FROM household_members AS members
            JOIN households AS household ON household.id = members.household_id
            WHERE members.user_id = ? AND members.active = 1
            ORDER BY household.created_at, members.household_id
            """,
            (caller.user_id,),
        ).fetchall()
        return [household_response(db, row) for row in rows]


@app.post(f"{API_PREFIX}/households", status_code=201)
def create_household(body: CreateHousehold, request: Request, caller: Caller) -> HouseholdResponse:
    require_normal_account(caller)
    household_id = str(uuid4())
    cart_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    with transaction(request.app.state.db_path, write=True) as db:
        db.execute(
            "INSERT INTO households (id, name, admin_id, created_at) VALUES (?, ?, ?, ?)",
            (household_id, body.name, caller.user_id, now),
        )
        db.execute(
            "INSERT INTO household_members (household_id, user_id, display_name, is_adult) VALUES (?, ?, ?, ?)",
            (household_id, caller.user_id, body.display_name, int(caller.is_adult)),
        )
        insert_household_address(db, household_id, body.address, caller.user_id)
        db.execute(
            """INSERT INTO carts (id, household_id, creator_id, name, adults_only)
               VALUES (?, ?, ?, 'Shared shopping cart', 0)""",
            (cart_id, household_id, caller.user_id),
        )
        record_cart_change(db, household_id, cart_id, caller.user_id)
        row = db.execute(
            "SELECT ? AS household_id, 'primary' AS role, ? AS name",
            (household_id, body.name),
        ).fetchone()
        return household_response(db, row)


@app.post(f"{HOUSEHOLD}/invitations", status_code=201)
def create_invitation(
    household_id: str, request: Request, caller: Caller
) -> CreateInvitationResponse:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        return new_invitation(db, household_id, caller.user_id)


class ChildCodeRequest(BaseModel):
    member_id: str | None = Field(default=None, max_length=150)


def require_normal_account(caller: Identity) -> None:
    if is_child_account(caller.user_id):
        raise HTTPException(status_code=403, detail="This action requires a normal account")


@app.post(f"{HOUSEHOLD}/child-codes", status_code=201)
def issue_child_code(household_id: str, body: ChildCodeRequest, request: Request, response: Response,
                     caller: Caller, request_key: Annotated[UUID4 | None, Header(alias="Idempotency-Key")] = None,
                     ) -> CreateInvitationResponse:
    require_normal_account(caller)
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        code, expires = child_codes.issue(db, household_id, caller.user_id, body.member_id,
                                          request.app.state.jwt_secret, request_key)
    response.headers["Cache-Control"] = "no-store"
    return CreateInvitationResponse(invitation_code=code, expires_at=expires)


def new_invitation(db: DatabaseConnection, household_id: str, invited_by: str) -> CreateInvitationResponse:
    code = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=7)
    db.execute(
        """INSERT INTO household_invitations
           (id, household_id, token_hash, invited_by, expires_at, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (str(uuid4()), household_id, hashlib.sha256(code.encode()).hexdigest(), invited_by,
         expires_at.isoformat(), now.isoformat()),
    )
    return CreateInvitationResponse(invitation_code=code, expires_at=expires_at.isoformat())


MEMBER_REQUESTS = f"{HOUSEHOLD}/member-requests"
REQUEST_COLUMNS = "id, requested_by, name, note, status, created_at"


def require_primary_user(caller: Identity) -> None:
    if not caller.is_adult:
        raise HTTPException(status_code=403, detail="Child users cannot ask for new members")


@app.get(MEMBER_REQUESTS)
def list_member_requests(household_id: str, request: Request, caller: Caller) -> list[MemberRequest]:
    """Requests waiting for the admin, and the caller's own."""
    with household_access(request, household_id, caller) as (db, is_primary):
        if not caller.is_adult and not is_primary:
            return []
        rows = db.execute(
            f"""SELECT {REQUEST_COLUMNS} FROM member_requests
                WHERE household_id = ? AND ((? AND status = 'PENDING') OR (requested_by = ? AND status IN ('PENDING', 'APPROVED')))
                ORDER BY created_at, id""",
            (household_id, is_primary, caller.user_id),
        ).fetchall()
        return [MemberRequest(**dict(row)) for row in rows]


@app.post(MEMBER_REQUESTS, status_code=201)
def request_member(household_id: str, body: MemberRequestInput, request: Request, caller: Caller) -> MemberRequest:
    """A primary user asks the admin to let somebody join."""
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        if is_primary:
            raise HTTPException(status_code=409, detail="You are the admin. Invite the member directly.")
        require_primary_user(caller)
        waiting = db.execute(
            """SELECT COUNT(*) AS n FROM member_requests
               WHERE household_id = ? AND requested_by = ? AND status = 'PENDING'""", (household_id, caller.user_id),
        ).fetchone()["n"]
        if waiting >= 10:
            raise HTTPException(status_code=409, detail="You already have 10 requests waiting for the admin")
        created = MemberRequest(
            id=str(uuid4()), requested_by=caller.user_id, name=body.name, note=body.note or None, status="PENDING",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        db.execute(
            f"INSERT INTO member_requests (household_id, {REQUEST_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (household_id, created.id, created.requested_by, created.name, created.note, created.status, created.created_at),
        )
        notify_changed(db, household_id)
        return created


@app.put(f"{MEMBER_REQUESTS}/{{request_id}}", status_code=204)
def decide_member_request(
    household_id: str, request_id: str, body: MemberRequestDecision, request: Request, caller: Caller
) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        updated = db.execute(
            """UPDATE member_requests SET status = ?, decided_at = ?
               WHERE id = ? AND household_id = ? AND status = 'PENDING'""",
            ("APPROVED" if body.approved else "REJECTED", datetime.now(timezone.utc).isoformat(), request_id, household_id),
        )
        if not updated.rowcount:
            raise HTTPException(status_code=404, detail="This request is no longer waiting")
        notify_changed(db, household_id)
    return Response(status_code=204)


@app.post(f"{MEMBER_REQUESTS}/{{request_id}}/invitation", status_code=201)
def collect_requested_invitation(
    household_id: str, request_id: str, request: Request, caller: Caller
) -> CreateInvitationResponse:
    """The one-time code for an approved request, for the person who asked."""
    with household_access(request, household_id, caller, write=True) as (db, _):
        require_primary_user(caller)
        used = db.execute(
            """UPDATE member_requests SET status = 'USED'
               WHERE id = ? AND household_id = ? AND requested_by = ? AND status = 'APPROVED'""",
            (request_id, household_id, caller.user_id),
        )
        if not used.rowcount:
            raise HTTPException(status_code=404, detail="This request has no invitation to collect")
        admin = active_member(db, household_id, caller.user_id)["owner_id"]
        notify_changed(db, household_id)
        return new_invitation(db, household_id, admin)


@app.post(f"{API_PREFIX}/household-invitations/accept")
def accept_invitation(
    body: AcceptInvitation, request: Request, caller: Caller
) -> HouseholdResponse:
    require_normal_account(caller)
    token_hash = hashlib.sha256(body.invitation_code.strip().encode()).hexdigest()
    now = datetime.now(timezone.utc)
    with transaction(request.app.state.db_path, write=True) as db:
        invitation = db.execute(
            """SELECT invitation.*, household.name
               FROM household_invitations AS invitation
               LEFT JOIN households AS household ON household.id = invitation.household_id
               WHERE invitation.token_hash = ? AND invitation.child_user_id IS NULL""",
            (token_hash,),
        ).fetchone()
        if (
            invitation is None
            or invitation["accepted_at"] is not None
            or invitation["revoked_at"] is not None
            or datetime.fromisoformat(invitation["expires_at"]) <= now
        ):
            raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
        household_id = invitation["household_id"]
        lock_household(db, household_id)
        if active_member(db, household_id, caller.user_id) is not None:
            raise HTTPException(status_code=409, detail="You are already a member of this household")
        claimed = db.execute(
            """UPDATE household_invitations SET accepted_by = ?, accepted_at = ?
               WHERE id = ? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?""",
            (caller.user_id, now.isoformat(), invitation["id"], now.isoformat()),
        )
        if not claimed.rowcount:
            raise HTTPException(status_code=400, detail="Invitation is invalid or expired")
        db.execute(
            """INSERT INTO household_members (household_id, user_id, active, display_name, is_adult, age_status)
               VALUES (?, ?, 1, ?, 0, ?)
               ON CONFLICT (household_id, user_id) DO UPDATE SET
                 active = 1, display_name = excluded.display_name, is_adult = 0, age_status = excluded.age_status""",
            # Saying "18 or over" is only a claim until this household's primary approves it.
            (household_id, caller.user_id, body.display_name, "PENDING" if caller.is_adult else "NONE"),
        )
        insert_household_address(db, household_id, body.address, caller.user_id)
        db.execute(
            """INSERT INTO cart_grants (household_id, cart_id, member_id)
               SELECT household_id, id, ? FROM carts
               WHERE household_id = ? AND adults_only = 0
               ON CONFLICT (cart_id, member_id) DO NOTHING""",
            (caller.user_id, household_id),
        )
        for cart in db.execute("SELECT id FROM carts WHERE household_id = ?", (household_id,)):
            record_cart_change(db, household_id, cart["id"], caller.user_id)
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
def list_products(request: Request, caller: Caller, q: str = "", household_id: str | None = None) -> list[Product]:
    if household_id is not None:
        with household_access(request, household_id, caller):
            pass
    elif caller.is_adult:
        with transaction(request.app.state.db_path) as db:
            apply_account_age(db, caller)
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
    with transaction(request.app.state.db_path) as db:
        apply_account_age(db, caller)
    return visible_product(request, sku, caller)


@app.get(f"{HOUSEHOLD}/carts")
def list_carts(
    household_id: str, request: Request, caller: Caller
) -> list[CartResponse]:
    with household_access(request, household_id, caller) as (db, is_primary):
        return visible_carts(db, household_id, caller, is_primary)


@app.get(f"{HOUSEHOLD}/capabilities")
def household_capabilities(household_id: str, request: Request, caller: Caller) -> HouseholdCapabilities:
    with household_access(request, household_id, caller) as (db, is_primary):
        return HouseholdCapabilities(
            can_create_adults_only_cart=can_create_adults_only_cart(caller, is_primary), is_adult=caller.is_adult,
            age_status=active_member(db, household_id, caller.user_id)["age_status"] or "APPROVED",
        )


@app.post(f"{HOUSEHOLD}/carts", status_code=201)
def create_cart(
    household_id: str, body: CreateCart, request: Request, caller: Caller, key: OperationKey = None,
) -> CartResponse:
    cart_id = str(uuid4())
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        operation = Operation(db, household_id, caller.user_id, key, ("create-cart", household_id, body.model_dump()))
        if operation.previous:
            return CartResponse(**operation.previous)
        require_primary(is_primary)
        if body.adults_only and not can_create_adults_only_cart(caller, is_primary):
            raise HTTPException(status_code=403, detail="Only an adult household admin can create an adults-only cart")
        db.execute(
            "INSERT INTO carts (id, household_id, creator_id, name, adults_only) VALUES (?, ?, ?, ?, ?)",
            # PostgreSQL stores this flag as SMALLINT, so bind an integer rather
            # than a Python bool (psycopg correctly treats bool as BOOLEAN).
            (cart_id, household_id, caller.user_id, body.name, int(body.adults_only)),
        )
        db.execute(
            """INSERT INTO cart_grants (household_id, cart_id, member_id)
               SELECT members.household_id, ?, members.user_id
               FROM household_members AS members
               WHERE members.household_id = ? AND members.active = 1
                 AND members.user_id <> ?
                 AND (? = 0 OR members.is_adult = 1)
               ON CONFLICT (cart_id, member_id) DO NOTHING""",
            (cart_id, household_id, caller.user_id, int(body.adults_only)),
        )
        created = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        record_cart_change(db, household_id, cart_id, caller.user_id)
        return operation.save(created)


# Keys of the caller's changes whose outcome their browser does not know yet.
PendingKeys = Annotated[str, Query(max_length=8000)]


@app.get(f"{HOUSEHOLD}/carts/{{cart_id}}")
def get_cart(
    household_id: str, cart_id: str, request: Request, caller: Caller, pending: PendingKeys = "",
) -> CartDetail:
    named = [key for key in pending.split(",") if key]
    with household_access(request, household_id, caller, snapshot=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        session = checkout_session.stored_session(db, cart_id)
        if session is None or not checkout_session.expired(session):
            return cart_detail(request, db, cart, caller, household_id, named)
    # Somebody left checkout without saying so. Give their waiting items back to the cart.
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        checkout_session.current_session(db, household_id, cart_id)
        return cart_detail(request, db, cart, caller, household_id, named)


def cart_detail(
    request: Request, db: DatabaseConnection, cart: CartResponse, caller: Identity, household_id: str,
    named: list[str] | None = None,
) -> CartDetail:
    session = checkout_session.stored_session(db, cart.id)
    return CartDetail(
        **cart.model_dump(), can_delete_cart=can_delete_cart(db, cart, caller),
        contributions=visible_contributions(request, caller, load_contributions(db, cart.id)),
        waiting=visible_contributions(request, caller, checkout_session.waiting_rows(db, cart.id)),
        checkout=describe_checkout(db, household_id, session) if session else None,
        revision=household_revision(db, household_id),
        applied=applied_keys(db, household_id, caller.user_id, named or ()),
    )


@app.get(f"{HOUSEHOLD}/carts/{{cart_id}}/inventory")
def cart_inventory(household_id: str, cart_id: str, request: Request, response: Response, caller: Caller) -> dict:
    response.headers["Cache-Control"] = "no-store"
    with household_access(request, household_id, caller, snapshot=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        stock = {}
        if cart.inventory_store_id:
            quantities = {row["sku"]: row["quantity"] for row in db.execute(
                "SELECT sku, quantity FROM store_inventory WHERE store_id = ?", (cart.inventory_store_id,),
            )}
            stock = {
                sku: quantities.get(sku, 0) for sku, product in request.app.state.products.items()
                if product.visible_to(caller) and (cart.adults_only or not product.min_age)
            }
        return {"store_id": cart.inventory_store_id, "store_name": cart.inventory_store_name, "stock": stock}


@app.put(f"{HOUSEHOLD}/carts/{{cart_id}}/inventory-store")
def set_cart_store(
    household_id: str, cart_id: str, body: checkout.AddressRef, request: Request, caller: Caller,
) -> CartDetail:
    reference = checkout.HouseholdCart(kind="household", household_id=household_id, cart_id=cart_id)
    with household_access(request, household_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if not cart.can_checkout:
            raise checkout.error(403, "AGE_ELIGIBILITY_REQUIRED", "Setting delivery requires an adult account.")
        address = checkout.saved_address(db, reference, body, caller.user_id, lock=False)
    require_checkout_adult(request, caller, reference)
    resolved, = checkout.resolve_groups([{"address": address}], lambda lat, lon: resolve_address_pin(request, lat, lon))
    store_id = resolved["store_id"]
    changed = False
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if not cart.can_checkout:
            raise checkout.error(403, "AGE_ELIGIBILITY_REQUIRED", "Setting delivery requires an adult account.")
        if checkout.saved_address(db, reference, body, caller.user_id, lock=True) != address:
            raise checkout.error(409, "ADDRESS_CHANGED", "This address changed. Choose the delivery location again.")
        if cart.inventory_store_id != store_id:
            if checkout_session.current_session(db, household_id, cart_id) is not None:
                raise checkout.error(409, "CHECKOUT_IN_PROGRESS", "End checkout before changing the delivery store.")
            db.execute("UPDATE carts SET inventory_store_id = ? WHERE id = ?", (store_id, cart_id))
            record_cart_change(db, household_id, cart_id, caller.user_id)
            changed = True
            cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        detail = cart_detail(request, db, cart, caller, household_id)
    if changed:
        request.app.state.live_sync.publish_now()
    return detail


def member_name(db: DatabaseConnection, household_id: str, user_id: str) -> str:
    row = db.execute(
        "SELECT display_name FROM household_members WHERE household_id = ? AND user_id = ?",
        (household_id, user_id),
    ).fetchone()
    return row["display_name"] if row else "Another member"


def describe_checkout(db: DatabaseConnection, household_id: str, session) -> CheckoutSession:
    return CheckoutSession(user_id=session["user_id"], expires_at=session["expires_at"],
                           display_name=member_name(db, household_id, session["user_id"]))


def checkout_in_progress(db: DatabaseConnection, household_id: str, session, caller: Identity) -> HTTPException:
    detail = describe_checkout(db, household_id, session)
    person = "You are" if detail.user_id == caller.user_id else f"{detail.display_name} is"
    return checkout.error(409, "CHECKOUT_IN_PROGRESS", f"{person} already checking out this cart.",
                          checkout=detail.model_dump(), revision=household_revision(db, household_id))


def being_checked_out(db: DatabaseConnection, household_id: str, session) -> HTTPException:
    name = member_name(db, household_id, session["user_id"])
    return HTTPException(
        status_code=409, detail=f"{name} is checking out this cart. Its items cannot be changed until they finish.",
    )


def refuse_other_checkout(db: DatabaseConnection, reference, caller: Identity) -> None:
    """One person checks out a shared cart at a time."""
    if reference.kind != "household":
        return
    session = checkout_session.stored_session(db, reference.cart_id)
    if (session is None or session["household_id"] != reference.household_id
            or session["user_id"] == caller.user_id or checkout_session.expired(session)):
        return
    raise checkout_in_progress(db, reference.household_id, session, caller)


def checkout_claim(db: DatabaseConnection, household_id: str, cart_id: str, caller: Identity, key: str | None) -> Operation:
    return Operation(db, household_id, caller.user_id, key, ("start_checkout", household_id, cart_id))


def claim_matches(session, caller: Identity, claim: Operation) -> bool:
    return (session is not None and session["user_id"] == caller.user_id and claim.previous is not None
            and claim.previous["started_at"] == session["started_at"] and not checkout_session.expired(session))


@app.post(f"{HOUSEHOLD}/carts/{{cart_id}}/checkout")
async def start_checkout(
    household_id: str, cart_id: str, request: Request, caller: Caller, key: OperationKey = None, renew: bool = False,
) -> CartDetail:
    detail, started = await run_in_threadpool(claim_checkout, household_id, cart_id, request, caller, key, renew)
    if key is not None and started:
        # The winner is committed before this delay and the household lock is free.
        await asyncio.sleep(uniform(0.250, 0.600))
    return detail


def claim_checkout(
    household_id: str, cart_id: str, request: Request, caller: Identity, key: str | None, renew: bool,
) -> tuple[CartDetail, bool]:
    reference = checkout.HouseholdCart(kind="household", household_id=household_id, cart_id=cart_id)
    with household_access(request, household_id, caller):
        pass  # a stranger learns nothing about ages or carts
    require_checkout_adult(request, caller, reference)
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if not cart.can_checkout:
            raise checkout.error(403, "AGE_ELIGIBILITY_REQUIRED", "Checkout requires an adult account.")
        claim = checkout_claim(db, household_id, cart_id, caller, key)
        if renew or claim.previous is not None:
            active = checkout_session.stored_session(db, cart_id)
            if not claim_matches(active, caller, claim):
                raise checkout.error(409, "CHECKOUT_ENDED", "This checkout has ended. Return to the cart to start again.")
            if renew:
                checkout_session.save_session(db, household_id, cart_id, caller.user_id)
            return cart_detail(request, db, cart, caller, household_id), False

        active = checkout_session.current_session(db, household_id, cart_id)
        if active is not None and (key is not None or active["user_id"] != caller.user_id):
            raise checkout_in_progress(db, household_id, active, caller)
        if active is None and not load_contributions(db, cart_id):
            raise checkout.error(409, "EMPTY_CART", "Add something to your cart before checkout.")
        started_at = checkout_session.save_session(db, household_id, cart_id, caller.user_id)
        claim.save(CheckoutClaim(started_at=started_at))
        if active is None:
            record_cart_change(db, household_id, cart_id, caller.user_id)
        detail = cart_detail(request, db, cart, caller, household_id)
    if active is None:
        # Everybody's cart is brought up to date now instead of with the next batch.
        request.app.state.live_sync.publish_now()
    return detail, active is None


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/checkout", status_code=204)
def leave_checkout(household_id: str, cart_id: str, request: Request, caller: Caller, key: OperationKey = None) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        session = checkout_session.stored_session(db, cart_id)
        if session is None or session["household_id"] != household_id:
            return Response(status_code=204)
        if session["user_id"] != caller.user_id and not is_primary:
            raise HTTPException(status_code=403, detail="Only the person checking out or the admin can end this checkout")
        if key is not None and not claim_matches(session, caller, checkout_claim(db, household_id, cart_id, caller, key)):
            return Response(status_code=204)
        checkout_session.end_session(db, household_id, cart_id, caller.user_id)
    request.app.state.live_sync.publish_now()
    return Response(status_code=204)


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}", status_code=204)
def remove_cart(household_id: str, cart_id: str, request: Request, caller: Caller) -> Response:
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(is_primary)
        session = checkout_session.current_session(db, household_id, cart_id)
        if session is not None:
            raise being_checked_out(db, household_id, session)
        delete_cart(db, cart_id)
    return Response(status_code=204)


@app.get(f"{HOUSEHOLD}/members", response_model_exclude_none=True)
def list_members(household_id: str, request: Request, caller: Caller, include_roles: bool = False) -> list[MemberResponse]:
    with household_access(request, household_id, caller) as (db, is_primary):
        require_primary(is_primary)
        members = household_members(db, household_id, include_roles=include_roles)
    claims = [member for member in members if member.age_status in ("PENDING", "REJECTED")]
    if claims:
        today = datetime.now(timezone.utc).date()
        try:
            births = birth_dates(request.app.state.user_db_path, [member.id for member in claims])
        except database_error_types():
            births = {}  # the list still works without the claimed ages
        for member in claims:
            if member.id in births:
                born = date.fromisoformat(births[member.id])
                member.claimed_age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return members


@app.put(f"{HOUSEHOLD}/members/{{member_id}}/age", status_code=204)
def decide_member_age(
    household_id: str, member_id: str, body: AgeDecision, request: Request, caller: Caller
) -> Response:
    """The primary confirms or refuses a member's claim to be 18 or over, for this household only."""
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        if not caller.is_adult:
            raise HTTPException(status_code=403, detail="Only an admin aged 18 or over can approve ages")
        if member_id == caller.user_id:
            raise HTTPException(status_code=409, detail="You cannot approve your own age")
        member = active_member(db, household_id, member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if member["age_status"] == "NONE":
            raise HTTPException(status_code=409, detail="This member said they are under 18")
        db.execute(
            """UPDATE household_members SET is_adult = ?, age_status = ?
               WHERE household_id = ? AND user_id = ?""",
            (int(body.approved), "APPROVED" if body.approved else "REJECTED", household_id, member_id),
        )
        if not body.approved:
            db.execute(
                """DELETE FROM cart_grants WHERE household_id = ? AND member_id = ?
                   AND cart_id IN (SELECT id FROM carts WHERE household_id = ? AND adults_only = 1)""",
                (household_id, member_id, household_id),
            )
        for cart in db.execute("SELECT id FROM carts WHERE household_id = ?", (household_id,)).fetchall():
            record_cart_change(db, household_id, cart["id"], caller.user_id)
    return Response(status_code=204)


@app.put(f"{HOUSEHOLD}/members/{{member_id}}/primary")
def promote_member(
    household_id: str, member_id: str, request: Request, caller: Caller
) -> HouseholdResponse:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        if member_id == caller.user_id:
            raise HTTPException(status_code=409, detail="This member already owns the household")
        target = active_member(db, household_id, member_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if is_child_account(member_id):
            raise HTTPException(status_code=409, detail="A child account cannot manage a household")
        if target["age_status"] == "PENDING":
            # Nobody could approve them once they are the primary.
            raise HTTPException(status_code=409, detail="Approve or reject this member's age first")

        db.execute("UPDATE households SET admin_id = ? WHERE id = ?", (member_id, household_id))
        # The previous household owner becomes a regular member and keeps normal
        # access to eligible carts. Primary cart controls follow household ownership.
        db.execute(
            """INSERT INTO cart_grants (household_id, cart_id, member_id)
               SELECT carts.household_id, carts.id, ?
               FROM carts
               JOIN household_members AS profile
                 ON profile.household_id = carts.household_id AND profile.user_id = ?
               WHERE carts.household_id = ?
                 AND (carts.adults_only = 0 OR profile.is_adult = 1)
               ON CONFLICT (cart_id, member_id) DO NOTHING""",
            (caller.user_id, caller.user_id, household_id),
        )
        for cart in db.execute("SELECT id FROM carts WHERE household_id = ?", (household_id,)):
            record_cart_change(db, household_id, cart["id"], caller.user_id)
        row = db.execute(
            "SELECT id AS household_id, 'primary' AS role, name FROM households WHERE id = ?",
            (household_id,),
        ).fetchone()
        return household_response(db, row)


@app.delete(f"{HOUSEHOLD}/members/{{member_id}}", status_code=204)
def revoke_member(
    household_id: str, member_id: str, request: Request, caller: Caller
) -> Response:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        if member_id == caller.user_id:
            raise HTTPException(status_code=409, detail="The admin cannot remove themselves")
        member = active_member(db, household_id, member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if member["owner_id"] == member_id:
            raise HTTPException(status_code=409, detail="The admin cannot be removed")
        end_membership(db, household_id, member_id, caller.user_id)
    return Response(status_code=204)


def end_membership(db: DatabaseConnection, household_id: str, member_id: str, actor_id: str) -> None:
    db.execute(
        "UPDATE household_members SET active = 0 WHERE household_id = ? AND user_id = ?", (household_id, member_id),
    )
    for row in db.execute(
        "SELECT cart_id FROM checkout_sessions WHERE household_id = ? AND user_id = ?", (household_id, member_id),
    ).fetchall():
        checkout_session.end_session(db, household_id, row["cart_id"], actor_id)
    db.execute("DELETE FROM cart_grants WHERE household_id = ? AND member_id = ?", (household_id, member_id))
    db.execute("DELETE FROM member_requests WHERE household_id = ? AND requested_by = ?", (household_id, member_id))
    for cart in db.execute("SELECT id FROM carts WHERE household_id = ?", (household_id,)).fetchall():
        record_cart_change(db, household_id, cart["id"], actor_id)


@app.post(f"{HOUSEHOLD}/leave", status_code=204)
def leave_household(household_id: str, request: Request, caller: Caller) -> Response:
    """A member exits. What they put in carts and lists stays; the admin can invite them again."""
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        if is_primary:
            raise HTTPException(status_code=409, detail="Make someone else the admin first, or delete the household")
        if not caller.is_adult:
            raise HTTPException(status_code=403, detail="Only the household admin can remove a child member")
        end_membership(db, household_id, caller.user_id, caller.user_id)
    return Response(status_code=204)


@app.delete(HOUSEHOLD, status_code=204)
def delete_household(household_id: str, request: Request, caller: Caller) -> Response:
    """The admin ends the household for everyone. Placed orders stay in each buyer's history.

    Carts, lists, addresses, requests and invitations are erased. The household and member
    rows stay, inactive, because orders and other records point at them.
    """
    now = datetime.now(timezone.utc).isoformat()
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        require_primary(is_primary)
        for table in ("checkout_sessions", "cart_waiting_items", "cart_item_contributions", "member_requests",
                      "shopping_list_items", "household_addresses"):
            db.execute(f"DELETE FROM {table} WHERE household_id = ?", (household_id,))
        db.execute(
            "UPDATE shopping_lists SET archived_at = ?, updated_at = ? WHERE household_id = ? AND archived_at IS NULL",
            (now, now, household_id),
        )
        for cart in db.execute(
            "SELECT id FROM carts WHERE household_id = ? AND deleted = 0", (household_id,),
        ).fetchall():
            delete_cart(db, cart["id"])
            record_cart_change(db, household_id, cart["id"], caller.user_id)
        db.execute(
            """UPDATE household_invitations SET revoked_at = ?
               WHERE household_id = ? AND accepted_at IS NULL AND revoked_at IS NULL""", (now, household_id),
        )
        db.execute("UPDATE household_members SET active = 0 WHERE household_id = ?", (household_id,))
        notify_changed(db, household_id)
    request.app.state.live_sync.publish_now()
    return Response(status_code=204)


def resolve_address_pin(request: Request, latitude: float | None, longitude: float | None) -> CoverageResult:
    if latitude is None or longitude is None:
        raise HTTPException(status_code=409, detail="ADDRESS_NEEDS_PIN")
    try:
        maps_response = request.app.state.maps_http.post(
            f"{request.app.state.maps_url}{API_PREFIX}/maps/resolve",
            json={"latitude": latitude, "longitude": longitude},
        )
        maps_response.raise_for_status()
        return coverage_result.validate_json(maps_response.content)
    except (httpx.HTTPError, ValidationError):
        raise HTTPException(status_code=503, detail=MAPS_UNAVAILABLE) from None


def checkout_profile(request: Request, caller: Identity):
    profile = session_profile(request.app.state.user_db_path, caller.session_id, caller.user_id) if caller.session_id else None
    if profile is None:
        raise HTTPException(status_code=401, detail="Session has ended")
    return profile


def require_checkout_adult(request: Request, caller: Identity, reference) -> None:
    profile = checkout_profile(request, caller)
    with transaction(request.app.state.db_path) as db:
        member = active_member(db, reference.household_id, caller.user_id)
        if member is not None and not age_allowed(member["age_status"]):
            caller.is_adult = False
    if not checkout.eligible(caller) or not profile["is_adult"]:
        raise checkout.error(403, "AGE_ELIGIBILITY_REQUIRED", "Checkout requires an adult account.")


@app.post(f"{API_PREFIX}/checkout/quotes", status_code=201)
def review_checkout(body: checkout.QuoteInput, request: Request, response: Response, caller: Caller) -> dict:
    require_checkout_adult(request, caller, body.cart)
    # Previously issued quotes can still be placed until their original expiry.
    if body.overrides:
        raise checkout.error(422, "SINGLE_ADDRESS_REQUIRED", "Choose one delivery address for the order.")
    path = request.app.state.db_path
    products = request.app.state.products
    with transaction(path, write=True) as db:
        checkout.lock_cart(db, body.cart, caller)
        state = checkout.snapshot(db, body, caller, products, lock=True)
        refuse_other_checkout(db, body.cart, caller)
    groups = checkout.resolve_groups(state["groups"], lambda lat, lon: resolve_address_pin(request, lat, lon))
    with transaction(path, write=True) as db:
        checkout.lock_cart(db, body.cart, caller)
        refuse_other_checkout(db, body.cart, caller)
        checkout.revalidate(db, state, body, caller, products, lock=True)
        quote = checkout.save_quote(db, caller.user_id, state, groups, request.app.state.demo_checkout)
    response.headers["Cache-Control"] = "no-store"
    return quote


@app.post(f"{API_PREFIX}/checkout/orders", status_code=201)
def place_order(
    body: checkout.OrderInput, request: Request, response: Response, caller: Caller,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$")],
) -> dict:
    checkout_profile(request, caller)
    path = request.app.state.db_path
    products = request.app.state.products
    attempt = checkout.fingerprint(body)
    response.headers["Cache-Control"] = "no-store"

    def replay(db):
        existing = checkout.successful_attempt(db, caller.user_id, key, attempt)
        if existing:
            response.status_code = 200
        return existing

    try:
        with transaction(path) as db:
            existing = replay(db)
            if existing:
                return existing
            if not checkout.eligible(caller) or not checkout_profile(request, caller)["is_adult"]:
                raise checkout.error(403, "AGE_ELIGIBILITY_REQUIRED", "Checkout requires an adult account.")
            if "mock_outcome" in body.model_fields_set and not request.app.state.demo_checkout:
                raise checkout.error(422, "MOCK_OUTCOME_DISABLED", "Mock payment overrides are disabled.")
            reviewed, expires_at = checkout.load_quote(db, body.quote_id, caller.user_id)
            selection = checkout.QuoteInput.model_validate(reviewed["state"]["selection"])
            require_checkout_adult(request, caller, selection.cart)
            state = checkout.revalidate(db, reviewed["state"], selection, caller, products, lock=False)
        groups = checkout.resolve_groups(state["groups"], lambda lat, lon: resolve_address_pin(request, lat, lon))
        if groups != reviewed["groups"]:
            raise checkout.stale()
    except HTTPException:
        # Another request may have placed this order while this one checked coverage.
        with transaction(path) as db:
            existing = replay(db)
            if existing:
                return existing
            checkout.require_unused_quote(db, body.quote_id, caller.user_id)
        raise

    with transaction(path, write=True) as db:
        checkout.lock_cart(db, selection.cart, caller)
        existing = replay(db)
        if existing:
            return existing
        refuse_other_checkout(db, selection.cart, caller)
        checkout.load_quote(db, body.quote_id, caller.user_id)
        checkout.revalidate(db, reviewed["state"], selection, caller, products, lock=True)
        require_checkout_adult(request, caller, selection.cart)
        order_id = checkout.claim_order(db, caller.user_id, key, attempt, body, selection, groups)
        if order_id is None:
            existing = replay(db)
            if existing:
                return existing
            checkout.require_unused_quote(db, body.quote_id, caller.user_id)
            raise RuntimeError("Could not allocate a unique order ID")

        requested = checkout.check_stock(db, groups, lock=True)
        checkout.require_unexpired(expires_at)
        if body.mock_outcome == "decline":
            raise checkout.error(402, "MOCK_PAYMENT_DECLINED", "The mock payment declined. Try again when ready.")
        checkout.fulfill_order(db, order_id, groups, requested)
        next_cart = None
        # Only what was ordered leaves the cart.
        ordered = sorted(set(selection.skus or ()))
        only = f" AND sku IN ({', '.join('?' * len(ordered))})" if ordered else ""
        db.execute(
            f"DELETE FROM cart_item_contributions WHERE cart_id = ?{only}", (selection.cart.cart_id, *ordered),
        )
        record_cart_change(
            db, selection.cart.household_id, selection.cart.cart_id, caller.user_id, reason="order",
        )
        next_cart = checkout_session.finish_session(db, selection.cart.household_id, selection.cart.cart_id)
        placed = checkout.order_detail(db, order_id, caller.user_id)
    request.app.state.live_sync.publish_now()
    return {**placed, "next_cart": next_cart} if next_cart else placed


@app.get(f"{API_PREFIX}/orders")
def list_orders(
    request: Request, response: Response, caller: Caller,
    limit: Annotated[int, Query(ge=1, le=50)] = 20,
    before: Annotated[str | None, Query(min_length=1, max_length=128)] = None,
) -> dict:
    checkout_profile(request, caller)
    response.headers["Cache-Control"] = "no-store"
    with transaction(request.app.state.db_path) as db:
        return checkout.order_history(db, caller.user_id, limit, before)


@app.get(f"{API_PREFIX}/orders/{{order_id}}")
def get_order(order_id: str, request: Request, response: Response, caller: Caller) -> dict:
    checkout_profile(request, caller)
    response.headers["Cache-Control"] = "no-store"
    with transaction(request.app.state.db_path) as db:
        return checkout.order_detail(db, order_id, caller.user_id)


def insert_household_address(db: DatabaseConnection, household_id: str, body: AddressInput, user_id: str) -> str:
    address_id = str(uuid4())
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT INTO household_addresses
           (id, household_id, label, recipient_name, address_line1, address_line2,
            city, postal_code, phone, latitude, longitude, created_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            address_id, household_id, body.label, body.recipient_name, body.address_line1, body.address_line2 or None,
            body.city, body.postal_code or None, body.phone or None, body.latitude, body.longitude, user_id, now, now,
        ),
    )
    return address_id


@app.get(f"{HOUSEHOLD}/addresses")
def list_addresses(
    household_id: str, request: Request, caller: Caller
) -> list[AddressResponse]:
    with household_access(request, household_id, caller) as (db, is_primary):
        rows = db.execute(
            f"""SELECT {ADDRESS_FIELDS}, created_by FROM household_addresses
                WHERE household_id = ? AND (? OR created_by = ?) AND {ACTIVE_ADDRESS} ORDER BY created_at, id""",
            (household_id, is_primary, caller.user_id),
        )
        return [AddressResponse(**dict(row)) for row in rows]


@app.post(f"{HOUSEHOLD}/addresses", status_code=201)
def create_address(
    household_id: str, body: AddressInput, request: Request, caller: Caller
) -> AddressResponse:
    require_normal_account(caller)
    with household_access(request, household_id, caller, write=True) as (db, _):
        address_id = insert_household_address(db, household_id, body, caller.user_id)
        notify_changed(db, household_id)
    return AddressResponse(id=address_id, created_by=caller.user_id, **body.model_dump())


@app.patch(HOUSEHOLD_ADDRESS)
def pin_household_address(
    household_id: str, address_id: str, body: Coordinates, request: Request, caller: Caller,
) -> AddressResponse:
    with household_access(
        request, household_id, caller, write=True, conceal_revoked=True, lock_membership=True,
    ) as (db, is_primary):
        address = db.execute(
            f"""UPDATE household_addresses SET latitude = ?, longitude = ?, updated_at = ?
                WHERE household_id = ? AND id = ? AND (? OR created_by = ?)
                RETURNING {ADDRESS_FIELDS}, created_by""",
            (body.latitude, body.longitude, datetime.now(timezone.utc).isoformat(),
             household_id, address_id, is_primary, caller.user_id),
        ).fetchone()
        if address is None:
            exists = db.execute(
                "SELECT 1 FROM household_addresses WHERE household_id = ? AND id = ?",
                (household_id, address_id),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="Address not found")
            raise HTTPException(status_code=403, detail="You can only move the pin for an address you added")
        notify_changed(db, household_id)
        return AddressResponse(**dict(address))


@app.post(f"{HOUSEHOLD_ADDRESS}/resolve-store")
def resolve_household_address(
    household_id: str, address_id: str, request: Request, caller: Caller,
) -> CoverageResult:
    with household_access(request, household_id, caller, conceal_revoked=True) as (db, is_primary):
        address = db.execute(
            f"""SELECT latitude, longitude FROM household_addresses
                WHERE household_id = ? AND id = ? AND (? OR created_by = ?) AND {ACTIVE_ADDRESS}""",
            (household_id, address_id, is_primary, caller.user_id),
        ).fetchone()
        if address is None:
            raise HTTPException(status_code=404, detail="Address not found")
    return resolve_address_pin(request, address["latitude"], address["longitude"])


@app.delete(HOUSEHOLD_ADDRESS, status_code=204)
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
        others = db.execute(
            "SELECT COUNT(*) AS n FROM household_addresses WHERE household_id = ? AND created_by = ? AND id <> ?",
            (household_id, address["created_by"], address_id),
        ).fetchone()["n"]
        if others == 0 and active_member(db, household_id, address["created_by"]) is not None:
            raise HTTPException(status_code=409, detail="Every member needs at least one address. Add another one first.")
        db.execute("DELETE FROM household_addresses WHERE id = ?", (address_id,))
        notify_changed(db, household_id)
    return Response(status_code=204)


def cart_grantees(db: DatabaseConnection, cart: CartResponse, *, include_creator: bool) -> list[str]:
    rows = db.execute(
        """
        SELECT members.user_id, members.is_adult
        FROM carts JOIN household_members AS members ON members.household_id = carts.household_id
        LEFT JOIN cart_grants ON cart_grants.cart_id = carts.id AND cart_grants.member_id = members.user_id
        JOIN households AS household ON household.id = carts.household_id
        WHERE carts.id = ? AND members.active = 1 AND members.user_id <> household.admin_id
            AND (cart_grants.member_id IS NOT NULL OR (
                ? AND members.user_id = carts.creator_id AND carts.creator_revoked = 0
            ))
        ORDER BY members.user_id
        """,
        (cart.id, include_creator),
    )
    rows = rows.fetchall()
    if include_creator and cart.adults_only:
        fixtures = {user_id for user_id, fixture in MEMBERS_BY_ID.items() if fixture.identity.is_adult}
        rows = [row for row in rows if row["is_adult"] == 1 or row["user_id"] in fixtures]
    return [row["user_id"] for row in rows]


@app.get(f"{HOUSEHOLD}/carts/{{cart_id}}/grants")
def list_grants(
    household_id: str, cart_id: str, request: Request, caller: Caller, include_creator: bool = False
) -> list[str]:
    with household_access(request, household_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(cart.can_manage_access)
        return cart_grantees(db, cart, include_creator=include_creator)


@app.put(f"{HOUSEHOLD}/carts/{{cart_id}}/grants/{{member_id}}")
def grant_access(
    household_id: str, cart_id: str, member_id: str, request: Request, caller: Caller
) -> list[str]:
    """Give a member access. Answers with who may use the cart now, as saved."""
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(cart.can_manage_access)
        member = active_member(db, household_id, member_id)
        if member is None:
            raise HTTPException(status_code=404, detail="Member not found")
        if member["owner_id"] == member_id:
            return cart_grantees(db, cart, include_creator=True)
        if cart.adults_only and not member["is_adult"]:
            raise HTTPException(
                status_code=403, detail="Adults-only grants require a verified adult member"
            )
        db.execute(
            "UPDATE carts SET creator_revoked = 0 WHERE id = ? AND creator_id = ?",
            (cart_id, member_id),
        )
        db.execute(
            """
            INSERT INTO cart_grants (household_id, cart_id, member_id) VALUES (?, ?, ?)
            ON CONFLICT (cart_id, member_id) DO NOTHING
            """,
            (household_id, cart_id, member_id),
        )
        return cart_grantees(db, cart, include_creator=True)


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/grants/{{member_id}}")
def revoke_access(
    household_id: str, cart_id: str, member_id: str, request: Request, caller: Caller
) -> list[str]:
    """Take a member's access away. Answers with who may use the cart now, as saved."""
    with change_cart(request, household_id, cart_id, caller) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(cart.can_manage_access)
        db.execute(
            "DELETE FROM cart_grants WHERE cart_id = ? AND member_id = ?", (cart_id, member_id)
        )
        db.execute(
            """UPDATE carts SET creator_revoked = 1
               WHERE id = ? AND creator_id = ?
                 AND creator_id <> (SELECT admin_id FROM households WHERE households.id = carts.household_id)""",
            (cart_id, member_id),
        )
        return cart_grantees(db, cart, include_creator=True)


def require_product_for_cart(request: Request, sku: str, caller: Identity, cart: CartResponse) -> None:
    product = visible_product(request, sku, caller)
    if product.min_age and not cart.adults_only:
        raise HTTPException(status_code=403, detail="This product requires an adults-only cart")


def save_quantity(
    db: DatabaseConnection, request: Request, household_id: str, cart: CartResponse, sku: str,
    contributor_id: str, quantity: int, caller: Identity,
) -> ContributionUpdate:
    cart_id = cart.id
    session = checkout_session.current_session(db, household_id, cart_id)
    in_cart = contribution_quantity(db, cart_id, sku, contributor_id)
    waiting = [row for row in checkout_session.waiting_rows(db, cart_id) if row["sku"] == sku] if session else []
    own_waiting = next((row["quantity"] for row in waiting if row["contributor_id"] == contributor_id), 0)
    increase = quantity - in_cart - own_waiting
    if increase > 0:
        require_product_for_cart(request, sku, caller, cart)
        requested = shared_quantity(db, cart_id, sku) + sum(row["quantity"] for row in waiting) + increase
        checkout.require_stock(db, cart.inventory_store_id, sku, requested)
    if session is not None:
        if quantity < in_cart:
            raise being_checked_out(db, household_id, session)
        checkout_session.set_waiting(db, household_id, cart_id, sku, contributor_id, quantity - in_cart)
        revision = checkout_session.record_waiting_change(db, household_id, cart_id, caller.user_id)
        return ContributionUpdate(sku=sku, contributor_id=contributor_id, quantity=quantity, revision=revision)
    set_contribution(db, household_id, cart_id, sku, contributor_id, quantity)
    revision = record_cart_change(db, household_id, cart_id, caller.user_id)
    return ContributionUpdate(sku=sku, contributor_id=contributor_id, quantity=quantity, revision=revision)


def shared_quantity(db: DatabaseConnection, cart_id: str, sku: str) -> int:
    rows = db.execute(
        "SELECT quantity FROM cart_item_contributions WHERE cart_id = ? AND sku = ?",
        (cart_id, sku),
    )
    return sum(row["quantity"] for row in rows)


def change_waiting(
    db: DatabaseConnection, household_id: str, cart: CartResponse, sku: str, delta: int, caller: Identity,
    is_primary: bool, session, *, in_cart: int,
) -> int:
    """Change what is waiting for the next cart, and return how many units of the product now wait."""
    cart_id = cart.id
    rows = [row for row in checkout_session.waiting_rows(db, cart_id) if row["sku"] == sku]
    total = sum(row["quantity"] for row in rows)
    if delta > 0:
        checkout.require_stock(db, cart.inventory_store_id, sku, in_cart + total + delta)
        own = next((row["quantity"] for row in rows if row["contributor_id"] == caller.user_id), 0)
        checkout_session.set_waiting(db, household_id, cart_id, sku, caller.user_id, own + delta)
        return total + delta
    removable = sorted(
        (row for row in rows if is_primary or row["contributor_id"] == caller.user_id),
        key=lambda row: (row["contributor_id"] != caller.user_id, row["contributor_id"]),
    )
    if not removable:
        raise being_checked_out(db, household_id, session)
    remaining = -delta
    for row in removable:
        taken = min(remaining, row["quantity"])
        checkout_session.set_waiting(db, household_id, cart_id, sku, row["contributor_id"], row["quantity"] - taken)
        remaining -= taken
        total -= taken
    return total


def item_update(
    request: Request, db: DatabaseConnection, caller: Identity, cart_id: str, sku: str, quantity: int, revision: int,
    *, waiting: bool = False,
) -> SharedItemUpdate:
    rows = checkout_session.waiting_rows(db, cart_id) if waiting else load_contributions(db, cart_id)
    lines = visible_contributions(request, caller, [row for row in rows if row["sku"] == sku])
    return SharedItemUpdate(sku=sku, quantity=quantity, revision=revision, waiting=waiting, cart_id=cart_id, lines=lines)


@app.patch(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}")
def adjust_shared_quantity(
    household_id: str, cart_id: str, sku: SKU, body: AdjustQuantity, request: Request, caller: Caller,
    key: OperationKey = None,
) -> SharedItemUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        operation = Operation(db, household_id, caller.user_id, key, ("adjust", household_id, cart_id, sku, body.delta))
        if operation.previous:
            return SharedItemUpdate(**operation.previous)
        # A tap can arrive just after the cart was checked out and replaced.
        cart_id = checkout_session.open_cart_id(db, cart_id)
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if body.delta == 0:
            raise HTTPException(status_code=422, detail="Quantity adjustment cannot be 0")
        session = checkout_session.current_session(db, household_id, cart_id)
        total = shared_quantity(db, cart_id, sku)
        if session is not None:
            if body.delta > 0:
                require_product_for_cart(request, sku, caller, cart)
            waiting = change_waiting(
                db, household_id, cart, sku, body.delta, caller, is_primary, session, in_cart=total,
            )
            revision = checkout_session.record_waiting_change(db, household_id, cart_id, caller.user_id)
            return operation.save(item_update(request, db, caller, cart_id, sku, total + waiting, revision, waiting=True))
        if body.delta > 0:
            require_product_for_cart(request, sku, caller, cart)
            checkout.require_stock(db, cart.inventory_store_id, sku, total + body.delta)
            own = contribution_quantity(db, cart_id, sku, caller.user_id)
            set_contribution(db, household_id, cart_id, sku, caller.user_id, own + body.delta)
            total += body.delta
        elif total == 0:
            return operation.save(item_update(request, db, caller, cart_id, sku, 0, household_revision(db, household_id)))
        else:
            # Several fast taps arrive as one request; apply the one-step rule for each.
            # The primary removes their own units first, then other members'; a member
            # may only remove units they added themselves.
            removed = 0
            for _ in range(-body.delta):
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
                    if removed == 0 and not is_primary:
                        raise HTTPException(status_code=403, detail="You can only remove items that you added.")
                    break
                set_contribution(
                    db, household_id, cart_id, sku, contribution["contributor_id"], contribution["quantity"] - 1,
                )
                removed += 1
                total -= 1
        revision = record_cart_change(db, household_id, cart_id, caller.user_id)
        return operation.save(item_update(request, db, caller, cart_id, sku, total, revision))


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}")
def remove_shared_item(
    household_id: str, cart_id: str, sku: SKU, request: Request, caller: Caller, key: OperationKey = None,
) -> SharedItemUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        operation = Operation(db, household_id, caller.user_id, key, ("remove", household_id, cart_id, sku))
        if operation.previous:
            return SharedItemUpdate(**operation.previous)
        require_visible_cart(db, household_id, cart_id, caller, is_primary)
        require_primary(is_primary)
        session = checkout_session.current_session(db, household_id, cart_id)
        if session is not None:
            waiting = [row for row in checkout_session.waiting_rows(db, cart_id) if row["sku"] == sku]
            if not waiting:
                raise being_checked_out(db, household_id, session)
            for row in waiting:
                checkout_session.set_waiting(db, household_id, cart_id, sku, row["contributor_id"], 0)
            revision = checkout_session.record_waiting_change(db, household_id, cart_id, caller.user_id)
            return operation.save(item_update(request, db, caller, cart_id, sku, shared_quantity(db, cart_id, sku), revision, waiting=True))
        db.execute(
            "DELETE FROM cart_item_contributions WHERE cart_id = ? AND sku = ?",
            (cart_id, sku),
        )
        revision = record_cart_change(db, household_id, cart_id, caller.user_id)
        return operation.save(item_update(request, db, caller, cart_id, sku, 0, revision))


@app.put(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}/contributions/me")
def update_quantity(
    household_id: str, cart_id: str, sku: SKU, body: SetQuantity, request: Request, caller: Caller
) -> ContributionUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        return save_quantity(db, request, household_id, cart, sku, caller.user_id, body.quantity, caller)


@app.patch(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}/contributions/me")
def adjust_quantity(
    household_id: str, cart_id: str, sku: SKU, body: AdjustQuantity, request: Request, caller: Caller
) -> ContributionUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if body.delta == 0:
            raise HTTPException(status_code=422, detail="Quantity adjustment cannot be 0")
        waiting = next((
            row["quantity"] for row in checkout_session.waiting_rows(db, cart_id)
            if row["sku"] == sku and row["contributor_id"] == caller.user_id
        ), 0)
        quantity = max(0, contribution_quantity(db, cart_id, sku, caller.user_id) + waiting + body.delta)
        if quantity > MAX_QUANTITY:
            raise HTTPException(status_code=409, detail="Maximum quantity reached")
        return save_quantity(db, request, household_id, cart, sku, caller.user_id, quantity, caller)


@app.delete(f"{HOUSEHOLD}/carts/{{cart_id}}/items/{{sku}}/contributions/{{contributor_id}}")
def remove_contribution(
    household_id: str, cart_id: str, sku: SKU, contributor_id: str, request: Request, caller: Caller
) -> ContributionUpdate:
    with household_access(request, household_id, caller, write=True) as (db, is_primary):
        cart = require_visible_cart(db, household_id, cart_id, caller, is_primary)
        if contributor_id != caller.user_id and not is_primary:
            raise HTTPException(status_code=403, detail="You can only remove your own items")
        return save_quantity(db, request, household_id, cart, sku, contributor_id, 0, caller)


def viewer_age(snapshot: dict, caller: Identity) -> tuple[Identity, str]:
    """The viewer as this household sees them: an adult only once their age is approved here."""
    member = next(member for member in snapshot["members"] if member["id"] == caller.user_id)
    status = member.get("age_status") or "APPROVED"
    if caller.is_adult and not age_allowed(status):
        caller = replace(caller, is_adult=False)
    return caller, status


def view_for(
    connection: HTTPConnection, snapshot: dict, caller: Identity, cart_id: str | None, *, batch: bool
) -> dict:
    """What one viewer's cart page should show, worked out in memory from a household snapshot.

    Mirrors the REST rules: cart_access() decides visibility, the member list hides
    roles and age flags from non-primaries, and only the primary sees cart grants.
    """
    is_primary = viewer_role(snapshot, caller.user_id)
    caller, age_status = viewer_age(snapshot, caller)
    members = sorted((member for member in snapshot["members"] if member["active"]), key=lambda member: member["id"])
    carts = []
    for cart in snapshot["carts"]:
        if cart["deleted"]:
            continue
        visible = cart_access({**cart, "has_grant": caller.user_id in cart["grants"]}, caller, is_primary)
        if visible is not None:
            carts.append(visible)
    view = {
        "revision": snapshot["revision"],
        "carts": [cart.model_dump() for cart in carts],
        "household_role": "primary" if is_primary else "member",
        "can_create_adults_only_cart": can_create_adults_only_cart(caller, is_primary),
        # Every household member may see display names so cart activity can
        # be attributed. Roles and age flags remain primary-only fields.
        "members": [
            MemberResponse(
                id=member["id"], display_name=member["display_name"],
                is_child_account=True if is_primary and is_child_account(member["id"]) else None,
                role=("primary" if member["id"] == snapshot["owner_id"] else "member") if is_primary else None,
                is_adult=member["is_adult"] if is_primary else None,
                age_status=member.get("age_status") if is_primary else None,
            ).model_dump()
            for member in members
        ],
        # Worked out from the current owner, so the view moves with the ownership.
        "addresses": [
            address for address in snapshot.get("addresses", [])
            if is_primary or address["created_by"] == caller.user_id
        ],
        "is_adult": caller.is_adult,
        "age_status": age_status,
        "member_requests": [
            item for item in snapshot.get("member_requests", [])
            if (is_primary and item["status"] == "PENDING")
            or (caller.is_adult and item["requested_by"] == caller.user_id)
        ],
    }
    if cart_id is None:
        return view
    # Which of this viewer's own changes the contents below already include.
    view["applied"] = (
        sorted({key for actor, key in snapshot.get("operations", []) if actor == caller.user_id})
        if batch else snapshot.get("applied", [])
    )
    selected = next((cart for cart in carts if cart.id == cart_id), None)
    if selected is None:
        view["cart"] = None
        replaced = {cart["id"]: cart.get("next_cart_id") for cart in snapshot["carts"] if cart["deleted"]}
        following = cart_id
        while following in replaced:
            following = replaced[following]
        if following != cart_id and any(cart.id == following for cart in carts):
            # Checked out and replaced by a new cart holding what was added meanwhile.
            view["next_cart_id"] = following
            view["ordered_by"] = snapshot.get("ordered", {}).get(cart_id) if batch else None
        row = next((cart for cart in snapshot["carts"] if cart["id"] == cart_id and not cart["deleted"]), None)
        if row is not None and not (row["adults_only"] and not caller.is_adult):
            owner = snapshot["owner_name"]
            view["access_notice"] = f"You no longer have access to this cart. Contact {owner}, the household admin."
    elif not batch or cart_id in snapshot["changed_carts"]:
        view["actor_id"] = snapshot["actors"].get(cart_id) if batch else None
        view["actor_ids"] = snapshot.get("cart_actors", {}).get(cart_id, []) if batch else []
        view["ordered_by"] = snapshot.get("ordered", {}).get(cart_id) if batch else None
        session = snapshot.get("checkouts", {}).get(cart_id)
        if session:
            name = next((member["display_name"] for member in members if member["id"] == session["user_id"]), "Another member")
            session = {**session, "display_name": name}
        view["cart"] = {
            **selected.model_dump(),
            "can_delete_cart": selected.can_manage_access,
            "contributions": visible_contributions(connection, caller, snapshot["changed_carts"].get(cart_id, [])),
            "waiting": visible_contributions(connection, caller, snapshot.get("waiting", {}).get(cart_id, [])),
            "checkout": session,
        }
        if selected.can_manage_access:
            view["grants"] = snapshot_grantees(snapshot, cart_id)
    return view


def snapshot_grantees(snapshot: dict, cart_id: str) -> list[str]:
    """In-memory version of cart_grantees(include_creator=True) for live updates."""
    cart = next(cart for cart in snapshot["carts"] if cart["id"] == cart_id)
    ids = [
        member["id"] for member in sorted(snapshot["members"], key=lambda member: member["id"])
        if member["active"] and member["id"] != snapshot["owner_id"]
        and (member["id"] in cart["grants"] or (member["id"] == cart["creator_id"] and not cart["creator_revoked"]))
    ]
    if cart["adults_only"]:
        adults = {member["id"] for member in snapshot["members"] if member["grant_adult"]}
        ids = [member_id for member_id in ids if member_id in adults]
    return ids


def initial_snapshot(db_path, household_id: str, cart_id: str | None, user_id: str, pending: list[str]) -> dict:
    with transaction(db_path, snapshot=True) as db:
        snapshots, _ = build_snapshots(
            db, [household_id], include_events=False,
            carts_to_load={household_id: {cart_id}} if cart_id else None,
        )
        if household_id not in snapshots:
            raise AccessEnded("Household not found")
        snapshot = snapshots[household_id]
        snapshot["revision"] = household_revision(db, household_id)
        # Read in the same moment as the contents, and only for this viewer.
        snapshot["applied"] = applied_keys(db, household_id, user_id, pending)
        return snapshot


def live_view(
    connection: HTTPConnection, household_id: str, caller: Identity, cart_id: str | None, batch: dict | None = None,
    pending: list[str] | None = None,
) -> dict:
    """The cart page view for one viewer: from a published batch, or freshly from the database."""
    try:
        snapshot = batch if batch is not None else initial_snapshot(
            connection.app.state.db_path, household_id, cart_id, caller.user_id, pending or [],
        )
        return view_for(connection, snapshot, caller, cart_id, batch=batch is not None)
    except AccessEnded as ended:
        status = 404 if ended.reason == "Household not found" else 403
        raise HTTPException(status_code=status, detail=ended.reason) from None


@app.websocket(f"{HOUSEHOLD}/live")
async def live_carts(socket: WebSocket, household_id: str, cart_id: str | None = None) -> None:
    origin = socket.headers.get("origin")
    host = socket.headers.get("host")
    if origin is not None and origin not in (f"http://{host}", f"https://{host}"):
        await socket.close(code=4403)
        return
    await socket.accept()
    sync = socket.app.state.live_sync
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
        named = auth.get("pending")
        pending = [key for key in named if isinstance(key, str)] if isinstance(named, list) else []
        async with sync.subscriber.pubsub() as pubsub:
            await pubsub.subscribe(sync.channel(household_id))
            subscribed = await pubsub.get_message(timeout=5)
            if not subscribed or subscribed["type"] != "subscribe":
                await socket.close(code=1013)
                return
            # Online before reading: a change saved right after the read must not count as unwatched.
            await sync.mark_online(household_id)
            # The only database read this socket makes: its starting view.
            view = await asyncio.to_thread(live_view, socket, household_id, caller, cart_id, None, pending)
            revision = view["revision"]
            await socket.send_json(view)
            last_presence = asyncio.get_running_loop().time()
            disconnect = asyncio.create_task(socket.receive())
            try:
                while not disconnect.done():
                    message = await pubsub.get_message(timeout=1)
                    verify_token(token, secret)
                    now = asyncio.get_running_loop().time()
                    if now - last_presence >= sync.interval / 2:
                        await sync.mark_online(household_id)
                        last_presence = now
                    if not message or message["type"] != "message":
                        continue
                    batch = json.loads(message["data"])
                    if batch["revision"] is None or batch["revision"] <= revision:
                        # No cart contents changed, but members, ages or shared addresses
                        # may have. This also notices a viewer who was removed.
                        await socket.send_json(live_view(socket, household_id, caller, None, batch))
                        continue
                    # Built in memory from the shared snapshot: no database query per viewer.
                    view = live_view(socket, household_id, caller, cart_id, batch)
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
    except HTTPException as error:
        await socket.close(code=4404, reason=str(error.detail)[:123])
    except RedisError:
        await socket.close(code=1013)


# Shopping-list routes reuse the access helpers above, so they are registered
# after everything they import has been defined.
from . import lists, my_lists  # noqa: E402

app.include_router(lists.router)
app.include_router(my_lists.router)
