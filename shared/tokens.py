import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import jwt

ISSUER = "familycart-auth"
AUDIENCE = "familycart-api"
ALGORITHM = "HS256"
TOKEN_LIFETIME = timedelta(minutes=15)


@dataclass(frozen=True)
class Identity:
    user_id: str
    is_adult: bool


def load_jwt_secret() -> str:
    secret = os.environ.get("FAMILYCART_JWT_SECRET", "")
    if len(secret.encode("utf-8")) < 32:
        raise RuntimeError("Set FAMILYCART_JWT_SECRET to a shared secret of at least 32 bytes")
    return secret


def issue_token(identity: Identity, secret: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": identity.user_id,
            "is_adult": identity.is_adult,
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + TOKEN_LIFETIME,
        },
        secret,
        algorithm=ALGORITHM,
    )


def verify_token(token: str, secret: str) -> Identity:
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            audience=AUDIENCE,
            options={"require": ["sub", "is_adult", "iss", "aud", "iat", "exp"]},
        )
    except (TypeError, OverflowError) as error:
        raise jwt.InvalidTokenError("Invalid token claims") from error

    if not claims["sub"] or not isinstance(claims["is_adult"], bool):
        raise jwt.InvalidTokenError("Invalid identity claims")
    return Identity(user_id=claims["sub"], is_adult=claims["is_adult"])
