import hashlib
import hmac
import secrets
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from backend.services.user_service.db import connect, transaction, upsert_user
from backend.shared.database import DatabaseTarget
from backend.shared.tokens import ACCESS_TOKEN_LIFETIME, Identity, issue_token


REFRESH_TOKEN_LIFETIME = timedelta(days=30)
REFRESH_FAMILY_LIFETIME = timedelta(days=90)


class InvalidRefreshToken(ValueError):
    pass


class RefreshTokenReuse(InvalidRefreshToken):
    pass


class DateOfBirthRequired(ValueError):
    pass


@dataclass(frozen=True)
class SessionTokens:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str
    display_name: str
    is_adult: bool
    age_setup_required: bool


def _hash_token(session_id: str, secret_part: str, signing_secret: str) -> str:
    value = f"refresh:{session_id}:{secret_part}".encode()
    return hmac.new(signing_secret.encode(), value, hashlib.sha256).hexdigest()


def _new_refresh_token(session_id: str, signing_secret: str) -> tuple[str, str]:
    secret_part = secrets.token_urlsafe(48)
    return f"{session_id}.{secret_part}", _hash_token(session_id, secret_part, signing_secret)


def _parse_refresh_token(token: str, signing_secret: str) -> tuple[str, str]:
    try:
        session_id, secret_part = token.split(".", 1)
        uuid4_value = UUID(session_id)
        if str(uuid4_value) != session_id or len(secret_part) < 48:
            raise ValueError
    except (AttributeError, ValueError):
        raise InvalidRefreshToken("Invalid or expired session") from None
    return session_id, _hash_token(session_id, secret_part, signing_secret)


def refresh_session_id(token: str, signing_secret: str) -> str:
    return _parse_refresh_token(token, signing_secret)[0]


def create_session(
    path: DatabaseTarget,
    signing_secret: str,
    identity: Identity,
    display_name: str,
    *,
    email: str | None = None,
    google_subject: str | None = None,
    date_of_birth: str | None = None,
    require_date_of_birth: bool = False,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> SessionTokens:
    now = datetime.now(timezone.utc)
    session_id = str(uuid4())
    refresh_token, token_hash = _new_refresh_token(session_id, signing_secret)
    with transaction(path) as database:
        if require_date_of_birth and date_of_birth is None and database.execute(
            "SELECT 1 FROM users WHERE user_id = ? AND date_of_birth IS NOT NULL", (identity.user_id,),
        ).fetchone() is None:
            raise DateOfBirthRequired
        profile = upsert_user(
            database, identity.user_id, display_name, identity.is_adult, now.isoformat(),
            email, google_subject, date_of_birth,
        )
        database.execute(
            """INSERT INTO refresh_sessions
               (session_id, user_id, token_hash, expires_at, absolute_expires_at,
                created_at, last_used_at, revoked_at, user_agent, ip_address)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
            (
                session_id,
                identity.user_id,
                token_hash,
                (now + REFRESH_TOKEN_LIFETIME).isoformat(),
                (now + REFRESH_FAMILY_LIFETIME).isoformat(),
                now.isoformat(),
                now.isoformat(),
                user_agent,
                ip_address,
            ),
        )
    access_identity = Identity(identity.user_id, bool(profile["is_adult"]), session_id)
    return SessionTokens(
        issue_token(access_identity, signing_secret),
        refresh_token,
        int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        identity.user_id,
        display_name,
        access_identity.is_adult,
        bool(profile["age_setup_required"]),
    )


def rotate_session(path: DatabaseTarget, signing_secret: str, presented_token: str) -> SessionTokens:
    session_id, presented_hash = _parse_refresh_token(presented_token, signing_secret)
    now = datetime.now(timezone.utc)
    with transaction(path) as database:
        session_query = (
            """SELECT s.*, u.display_name, u.is_adult, u.status,
                      u.google_subject IS NOT NULL AND u.date_of_birth IS NULL AS age_setup_required
               FROM refresh_sessions s JOIN users u ON u.user_id = s.user_id
               WHERE s.session_id = ?"""
            + (" FOR UPDATE" if database.dialect == "postgresql" else "")
        )
        session = database.execute(session_query, (session_id,)).fetchone()
        if session is None:
            raise InvalidRefreshToken("Invalid or expired session")

        matches_current = hmac.compare_digest(session["token_hash"], presented_hash)
        if not matches_current:
            reused = database.execute(
                "SELECT 1 FROM refresh_token_history WHERE session_id = ? AND token_hash = ?",
                (session_id, presented_hash),
            ).fetchone()
            if reused:
                database.execute(
                    "UPDATE refresh_sessions SET revoked_at = ? WHERE session_id = ? AND revoked_at IS NULL",
                    (now.isoformat(), session_id),
                )
                database.commit()
                raise RefreshTokenReuse("Invalid or expired session")
            raise InvalidRefreshToken("Invalid or expired session")

        if (
            session["revoked_at"] is not None
            or session["status"] != "ACTIVE"
            or datetime.fromisoformat(session["expires_at"]) <= now
            or datetime.fromisoformat(session["absolute_expires_at"]) <= now
        ):
            raise InvalidRefreshToken("Invalid or expired session")

        refresh_token, token_hash = _new_refresh_token(session_id, signing_secret)
        absolute_expiry = datetime.fromisoformat(session["absolute_expires_at"])
        expires_at = min(now + REFRESH_TOKEN_LIFETIME, absolute_expiry)
        database.execute(
            "INSERT INTO refresh_token_history (session_id, token_hash, rotated_at) VALUES (?, ?, ?)",
            (session_id, session["token_hash"], now.isoformat()),
        )
        database.execute(
            """UPDATE refresh_sessions
               SET token_hash = ?, expires_at = ?, last_used_at = ?
               WHERE session_id = ?""",
            (token_hash, expires_at.isoformat(), now.isoformat(), session_id),
        )

    identity = Identity(session["user_id"], bool(session["is_adult"]), session_id)
    return SessionTokens(
        issue_token(identity, signing_secret),
        refresh_token,
        int(ACCESS_TOKEN_LIFETIME.total_seconds()),
        session["user_id"],
        session["display_name"],
        identity.is_adult,
        bool(session["age_setup_required"]),
    )


def revoke_session(path: DatabaseTarget, session_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with transaction(path) as database:
        database.execute(
            "UPDATE refresh_sessions SET revoked_at = COALESCE(revoked_at, ?) WHERE session_id = ?",
            (now, session_id),
        )


def session_is_active(path: DatabaseTarget, session_id: str, user_id: str) -> bool:
    return session_profile(path, session_id, user_id) is not None


def session_profile(path: DatabaseTarget, session_id: str, user_id: str):
    now = datetime.now(timezone.utc).isoformat()
    with closing(connect(path)) as database:
        return database.execute(
            """SELECT u.is_adult, u.date_of_birth FROM refresh_sessions s JOIN users u ON u.user_id = s.user_id
               WHERE s.session_id = ? AND s.user_id = ? AND s.revoked_at IS NULL
                 AND s.absolute_expires_at > ? AND u.status = 'ACTIVE'""",
            (session_id, user_id, now),
        ).fetchone()
