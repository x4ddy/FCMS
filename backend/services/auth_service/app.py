import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from re import fullmatch
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Header, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import InvalidTokenError
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, UUID4

from backend.services.familycart_service import child_codes
from backend.services.familycart_service.db import database_target, transaction as shopping_transaction
from backend.services.familycart_service.live_sync import notify_changed
from backend.services.refresh_service.responses import RefreshResponse, signed_in_response
from backend.services.refresh_service.cookies import set_refresh_cookie
from backend.services.refresh_service.service import (
    DateOfBirthRequired, InvalidRefreshToken, child_session, create_session, session_profile,
)
from backend.services.user_service.db import database_path, initialize_database, transaction
from backend.shared.api import API_PREFIX
from backend.shared.database import database_unavailable_types
from backend.shared.tokens import Identity, is_child_account, load_jwt_secret, verify_token

from .google_auth import (
    InvalidGoogleCredential,
    google_client_id,
    verify_google_credential,
)

logger = logging.getLogger(__name__)
DATABASE_UNAVAILABLE = database_unavailable_types()


def iso_birth_date(value):
    if not isinstance(value, str) or not fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Enter a date of birth in YYYY-MM-DD format")
    return value


BirthDate = Annotated[date, BeforeValidator(iso_birth_date)]


class GoogleLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    credential: str = Field(min_length=100, max_length=8192)
    date_of_birth: BirthDate | None = None


class GoogleConfiguration(BaseModel):
    client_id: str


class SignedInUser(BaseModel):
    id: str
    display_name: str
    email: str
    is_adult: bool
    age_setup_required: bool


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: SignedInUser


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    app.state.auth_db_path = database_path()
    app.state.google_client_id = google_client_id()
    initialize_database(app.state.auth_db_path)
    yield


app = FastAPI(title="FamilyCart Auth", lifespan=lifespan)
bearer = HTTPBearer(auto_error=False)


class AgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date_of_birth: BirthDate


class AgeStatus(BaseModel):
    is_adult: bool
    age_recorded: bool
    session_refresh_required: bool = False


def session_identity(
    request: Request, credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Identity:
    if credentials:
        try:
            caller = verify_token(credentials.credentials, request.app.state.jwt_secret)
            if caller.session_id:
                return caller
        except InvalidTokenError:
            pass
    raise HTTPException(status_code=401, detail="Session has ended")


SessionCaller = Annotated[Identity, Depends(session_identity)]


class ProfileName(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: Annotated[str, StringConstraints(
        strip_whitespace=True, min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$",
    )]


@app.patch(f"{API_PREFIX}/auth/profile")
def update_profile(body: ProfileName, request: Request, response: Response, caller: SessionCaller) -> ProfileName:
    if is_child_account(caller.user_id):
        raise HTTPException(status_code=403, detail="Child accounts cannot change their username")
    now = datetime.now(timezone.utc).isoformat()
    shopping_path = database_target()
    try:
        with transaction(request.app.state.auth_db_path) as db:
            saved = db.execute(
                """UPDATE users SET display_name = ?, updated_at = ?
                   WHERE user_id = ? AND status = 'ACTIVE'
                     AND EXISTS (SELECT 1 FROM refresh_sessions
                                 WHERE session_id = ? AND user_id = users.user_id
                                   AND revoked_at IS NULL AND absolute_expires_at > ?)
                   RETURNING display_name""",
                (body.display_name, now, caller.user_id, caller.session_id, now),
            ).fetchone()
            if saved is None:
                raise HTTPException(status_code=401, detail="Session has ended")
            # Keep concurrent renames ordered by holding the account row until the memberships are updated.
            with shopping_transaction(shopping_path, write=True) as shopping:
                households = shopping.execute(
                    """UPDATE household_members SET display_name = ?
                       WHERE user_id = ? AND active = 1 AND (display_name IS NULL OR display_name <> ?)
                       RETURNING household_id""",
                    (saved["display_name"], caller.user_id, saved["display_name"]),
                ).fetchall()
                for household in households:
                    notify_changed(shopping, household["household_id"], db_path=shopping_path)
    except DATABASE_UNAVAILABLE as error:
        logger.warning("Could not update profile: %s", type(error).__name__)
        raise HTTPException(status_code=503, detail="Your username could not be saved. Try again shortly.",
                            headers={"Retry-After": "1", "Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    return ProfileName(display_name=saved["display_name"])


@app.get(f"{API_PREFIX}/auth/age", response_model_exclude_defaults=True)
def age_status(request: Request, response: Response, caller: SessionCaller) -> AgeStatus:
    profile = session_profile(request.app.state.auth_db_path, caller.session_id, caller.user_id)
    if profile is None:
        raise HTTPException(status_code=401, detail="Session has ended")
    response.headers["Cache-Control"] = "no-store"
    adult = bool(profile["is_adult"])
    return AgeStatus(is_adult=adult, age_recorded=profile["date_of_birth"] is not None,
                     session_refresh_required=adult != caller.is_adult)


@app.post(f"{API_PREFIX}/auth/age", response_model_exclude_defaults=True)
def set_age(body: AgeInput, request: Request, response: Response, caller: SessionCaller) -> AgeStatus:
    adult = adult_from_birth_date(body.date_of_birth)
    birth = body.date_of_birth.isoformat()
    path = request.app.state.auth_db_path
    now = datetime.now(timezone.utc).isoformat()
    with transaction(path) as db:
        profile = db.execute(
            """UPDATE users SET date_of_birth = ?, is_adult = ?, updated_at = ?
               WHERE user_id = ? AND status = 'ACTIVE' AND google_subject IS NOT NULL AND date_of_birth IS NULL
                 AND EXISTS (SELECT 1 FROM refresh_sessions
                             WHERE session_id = ? AND user_id = users.user_id
                               AND revoked_at IS NULL AND absolute_expires_at > ?)
               RETURNING is_adult""",
            (birth, int(adult), now, caller.user_id, caller.session_id, now),
        ).fetchone()
    if profile is None:
        if session_profile(path, caller.session_id, caller.user_id) is None:
            raise HTTPException(status_code=401, detail="Session has ended")
        raise HTTPException(status_code=409, detail="A date of birth is already saved for this account")
    response.headers["Cache-Control"] = "no-store"
    return AgeStatus(is_adult=bool(profile["is_adult"]), age_recorded=True)


def adult_from_birth_date(birth: date) -> bool:
    today = datetime.now(timezone.utc).date()
    age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
    if birth > today or age > 125:
        raise HTTPException(status_code=422, detail="Enter a valid date of birth")
    return age >= 18


@app.get(f"{API_PREFIX}/auth/google/config", response_model=GoogleConfiguration)
def google_configuration(request: Request) -> GoogleConfiguration:
    # OAuth client IDs are public identifiers. Returning the backend's configured
    # value guarantees the browser and token verifier use the same audience.
    return GoogleConfiguration(client_id=request.app.state.google_client_id)


@app.post(f"{API_PREFIX}/auth/google")
def google_login(body: GoogleLoginRequest, request: Request, response: Response) -> LoginResponse:
    birth = body.date_of_birth
    adult = adult_from_birth_date(birth) if birth is not None else False
    try:
        google_user = verify_google_credential(body.credential, request.app.state.google_client_id)
        tokens = create_session(
            request.app.state.auth_db_path,
            request.app.state.jwt_secret,
            Identity(user_id=google_user.subject, is_adult=adult),
            google_user.display_name,
            email=google_user.email,
            google_subject=google_user.subject,
            date_of_birth=birth.isoformat() if birth else None,
            require_date_of_birth=True,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except InvalidGoogleCredential as error:
        raise HTTPException(status_code=401, detail=str(error)) from None
    except DateOfBirthRequired:
        raise HTTPException(status_code=422, detail={
            "code": "DATE_OF_BIRTH_REQUIRED", "message": "Enter your date of birth to finish account setup.",
        }, headers={"Cache-Control": "no-store"}) from None
    except Exception as error:
        # A unique-email conflict means this Gmail is already bound to a different
        # Google subject. Do not leak database details to the caller.
        if "unique" in str(error).casefold() or "duplicate" in str(error).casefold():
            raise HTTPException(status_code=409, detail="This Gmail account is already registered") from None
        raise

    set_refresh_cookie(response, tokens.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        user=SignedInUser(
            id=tokens.user_id,
            display_name=tokens.display_name,
            email=google_user.email,
            is_adult=tokens.is_adult,
            age_setup_required=tokens.age_setup_required,
        ),
    )


class ChildLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=6, max_length=100)
    display_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None


@app.post(f"{API_PREFIX}/auth/child", response_model_exclude_none=True)
def child_login(body: ChildLoginRequest, request: Request, response: Response,
                attempt: Annotated[UUID4, Header(alias="Idempotency-Key")]) -> RefreshResponse:
    key = str(attempt)
    session_id, user_id, name = child_codes.claim(database_target(), body.code.strip(), key, body.display_name,
                                                 request.app.state.jwt_secret)
    try:
        tokens = child_session(request.app.state.auth_db_path, request.app.state.jwt_secret, session_id, user_id,
                               name, key, request.headers.get("user-agent"), request.client.host if request.client else None)
    except InvalidRefreshToken as error:
        raise HTTPException(400, str(error)) from None
    return signed_in_response(response, tokens)
