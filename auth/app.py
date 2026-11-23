from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from shared.demo_members import ADULT, CHILD, PRIMARY, DemoMember
from shared.tokens import issue_token, load_jwt_secret


@dataclass(frozen=True)
class SeedAccount:
    member: DemoMember
    password_hash: str


ACCOUNTS = {
    "primary@example.test": SeedAccount(
        member=PRIMARY,
        password_hash=(
            "$argon2id$v=19$m=65536,t=3,p=4$DG9qt1UxHr0nos6dzFNdDA"
            "$lyXKEA/c1jBLT7QJjJsrMR0B3Eim3HmGKkuM25MNFJA"
        ),
    ),
    "demo-child": SeedAccount(
        member=CHILD,
        password_hash=(
            "$argon2id$v=19$m=65536,t=3,p=4$ov3Ec5qr5Y+I3nEZDmNCfw"
            "$U2WPY9n6Hd3iS91W/Pg3m//CvwPmYDmr+2pUU1S+BKA"
        ),
    ),
    "adult@example.test": SeedAccount(
        member=ADULT,
        password_hash=(
            "$argon2id$v=19$m=65536,t=3,p=4$hOLGe1/Ts+++khT92LymoQ"
            "$OiwQSfQVrnPcDDd4/a0Mtt6tSg6RMVFCtqrCMcT68tY"
        ),
    ),
}
PASSWORD_HASHER = PasswordHasher()
# Use the same hashing work when the identifier is unknown.
FALLBACK_HASH = ACCOUNTS["primary@example.test"].password_hash


class LoginRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class SignedInUser(BaseModel):
    id: str
    display_name: str


class LoginResponse(BaseModel):
    access_token: str
    user: SignedInUser


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    yield


app = FastAPI(title="FamilyCart Auth", lifespan=lifespan)


@app.post("/auth/login")
def login(body: LoginRequest, request: Request) -> LoginResponse:
    account = ACCOUNTS.get(body.identifier.strip().casefold())
    password_hash = account.password_hash if account else FALLBACK_HASH
    try:
        PASSWORD_HASHER.verify(password_hash, body.password)
    except VerifyMismatchError:
        raise HTTPException(status_code=401, detail="Invalid credentials") from None

    if account is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    member = account.member
    return LoginResponse(
        access_token=issue_token(member.identity, request.app.state.jwt_secret),
        user=SignedInUser(id=member.identity.user_id, display_name=member.display_name),
    )
