from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.services.refresh_service.cookies import set_refresh_cookie
from backend.services.refresh_service.service import create_session
from backend.services.user_service.db import database_path, initialize_database
from backend.shared.api import API_PREFIX
from backend.shared.tokens import Identity, load_jwt_secret

from .google_auth import (
    InvalidGoogleCredential,
    google_client_id,
    verify_google_credential,
)


class GoogleLoginRequest(BaseModel):
    credential: str = Field(min_length=100, max_length=8192)


class GoogleConfiguration(BaseModel):
    client_id: str


class SignedInUser(BaseModel):
    id: str
    display_name: str
    email: str


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


@app.get(f"{API_PREFIX}/auth/google/config", response_model=GoogleConfiguration)
def google_configuration(request: Request) -> GoogleConfiguration:
    # OAuth client IDs are public identifiers. Returning the backend's configured
    # value guarantees the browser and token verifier use the same audience.
    return GoogleConfiguration(client_id=request.app.state.google_client_id)


@app.post(f"{API_PREFIX}/auth/google")
def google_login(body: GoogleLoginRequest, request: Request, response: Response) -> LoginResponse:
    try:
        google_user = verify_google_credential(body.credential, request.app.state.google_client_id)
        tokens = create_session(
            request.app.state.auth_db_path,
            request.app.state.jwt_secret,
            Identity(user_id=google_user.subject, is_adult=google_user.is_adult),
            google_user.display_name,
            email=google_user.email,
            google_subject=google_user.subject,
            user_agent=request.headers.get("user-agent"),
            ip_address=request.client.host if request.client else None,
        )
    except InvalidGoogleCredential as error:
        raise HTTPException(status_code=401, detail=str(error)) from None
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
        ),
    )
