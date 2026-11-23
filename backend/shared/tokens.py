import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import jwt

ISSUER = "familycart-auth"
AUDIENCE = "familycart-api"
ALGORITHM = "HS256"
ACCESS_TOKEN_LIFETIME = timedelta(minutes=10)
# Kept as an alias for callers that imported the original prototype constant.
TOKEN_LIFETIME = ACCESS_TOKEN_LIFETIME


@dataclass(frozen=True)
class Identity:
    user_id: str
    is_adult: bool
    session_id: str | None = None


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
            "jti": str(uuid4()),
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + ACCESS_TOKEN_LIFETIME,
            **({"sid": identity.session_id} if identity.session_id else {}),
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
            options={"require": ["sub", "is_adult", "jti", "iss", "aud", "iat", "exp"]},
        )
    except (TypeError, OverflowError) as error:
        raise jwt.InvalidTokenError("Invalid token claims") from error

    if not claims["sub"] or not isinstance(claims["is_adult"], bool):
        raise jwt.InvalidTokenError("Invalid identity claims")
    session_id = claims.get("sid")
    if session_id is not None and not isinstance(session_id, str):
        raise jwt.InvalidTokenError("Invalid session claim")
    return Identity(user_id=claims["sub"], is_adult=claims["is_adult"], session_id=session_id)
