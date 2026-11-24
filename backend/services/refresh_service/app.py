import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from jwt import InvalidTokenError
from pydantic import BaseModel

from backend.services.user_service.db import database_path, initialize_database
from backend.shared.api import API_PREFIX
from backend.shared.tokens import load_jwt_secret, verify_token

from .cookies import REFRESH_COOKIE, clear_refresh_cookie, set_refresh_cookie
from .service import (
    InvalidRefreshToken,
    refresh_session_id,
    revoke_session,
    rotate_session,
)


class SignedInUser(BaseModel):
    id: str
    display_name: str
    is_adult: bool
    age_setup_required: bool


class RefreshResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: SignedInUser


def _check_origin(origin: str | None) -> None:
    if not origin:
        return
    configured = os.environ.get(
        "FAMILYCART_ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    )
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="Origin is not allowed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    app.state.user_db_path = database_path()
    initialize_database(app.state.user_db_path)
    yield


app = FastAPI(title="FamilyCart Refresh Service", lifespan=lifespan)


@app.post(f"{API_PREFIX}/sessions/refresh")
def refresh(
    request: Request,
    response: Response,
    origin: str | None = Header(default=None),
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
) -> RefreshResponse:
    _check_origin(origin)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    try:
        tokens = rotate_session(
            request.app.state.user_db_path, request.app.state.jwt_secret, refresh_token
        )
    except InvalidRefreshToken as error:
        clear_refresh_cookie(response)
        raise HTTPException(status_code=401, detail=str(error)) from None
    set_refresh_cookie(response, tokens.refresh_token)
    response.headers["Cache-Control"] = "no-store"
    return RefreshResponse(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        user=SignedInUser(id=tokens.user_id, display_name=tokens.display_name, is_adult=tokens.is_adult,
                          age_setup_required=tokens.age_setup_required),
    )


@app.post(f"{API_PREFIX}/sessions/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    origin: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE),
) -> None:
    _check_origin(origin)
    session_ids: set[str] = set()
    if refresh_token:
        try:
            session_ids.add(refresh_session_id(refresh_token, request.app.state.jwt_secret))
        except InvalidRefreshToken:
            pass
    if authorization and authorization.startswith("Bearer "):
        try:
            identity = verify_token(authorization[7:], request.app.state.jwt_secret)
            if identity.session_id:
                session_ids.add(identity.session_id)
        except InvalidTokenError:
            pass
    for session_id in session_ids:
        revoke_session(request.app.state.user_db_path, session_id)
    clear_refresh_cookie(response)
    response.headers["Cache-Control"] = "no-store"
