"""Household-owned invitations for code-based child sign-in."""
import hashlib
import hmac
import string
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException

from backend.shared.tokens import CHILD_ID_PREFIX, is_child_account
from .db import active_member, lock_household, record_event, transaction
from .live_sync import notify_changed

ALPHABET = string.ascii_letters + string.digits
CODE_ATTEMPTS = 8


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def code_hash(code: str, signing_secret: str) -> str:
    if len(code) == 6:
        return "child:" + hmac.new(signing_secret.encode(), f"child-code:{code}".encode(), hashlib.sha256).hexdigest()
    # Previously issued long codes keep their original expiry and digest.
    return "child:" + digest(code)


def new_code(invitation_id: str, attempt: int, signing_secret: str) -> str:
    # Recreate the code on retry while storing only its hash.
    digest = hmac.digest(signing_secret.encode(), f"issue-child-code:{invitation_id}:{attempt}".encode(), "sha256")
    number = int.from_bytes(digest, "big")
    code = []
    for _ in range(6):
        number, digit = divmod(number, len(ALPHABET))
        code.append(ALPHABET[digit])
    return "".join(code)


def issue(db, household_id: str, admin_id: str, member_id: str | None, signing_secret: str,
          request_key: UUID | None = None) -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(hours=24)).isoformat()
    invitation_id = str(uuid5(request_key, f"{household_id}/{admin_id}/{member_id or ''}") if request_key else uuid4())
    if member_id is not None:
        if not is_child_account(member_id) or not active_member(db, household_id, member_id):
            raise HTTPException(404, "Child account not found in this household")

    if request_key:
        previous = db.execute("""SELECT token_hash, expires_at, accepted_at, revoked_at
                                 FROM household_invitations WHERE id = ?""", (invitation_id,)).fetchone()
        if previous:
            if previous["accepted_at"] or previous["revoked_at"] or previous["expires_at"] <= now.isoformat():
                raise HTTPException(409, "This request's child code is no longer active. Generate a new code.")
            for attempt in range(CODE_ATTEMPTS):
                code = new_code(invitation_id, attempt, signing_secret)
                if hmac.compare_digest(previous["token_hash"], code_hash(code, signing_secret)):
                    return code, previous["expires_at"]
            raise HTTPException(409, "Generate a new child code.")

    if member_id is not None:
        db.execute("""UPDATE household_invitations SET revoked_at = ?
                      WHERE household_id = ? AND child_user_id = ? AND accepted_at IS NULL AND revoked_at IS NULL""",
                   (now.isoformat(), household_id, member_id))
    user_id = member_id or f"{CHILD_ID_PREFIX}{uuid4()}"
    for attempt in range(CODE_ATTEMPTS):
        code = new_code(invitation_id, attempt, signing_secret)
        saved = db.execute("""INSERT INTO household_invitations
                              (id, household_id, token_hash, invited_by, expires_at, created_at, child_user_id)
                              VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT (token_hash) DO NOTHING RETURNING id""",
                           (invitation_id, household_id, code_hash(code, signing_secret), admin_id, expires,
                            now.isoformat(), user_id)).fetchone()
        if saved:
            return code, expires
    raise HTTPException(503, "Couldn't generate a child code. Try again.")


def invalid_code():
    return HTTPException(400, "This child code is invalid, expired, or already used")


def claim(path, code: str, key: str, name: str | None, signing_secret: str) -> tuple[str, str, str]:
    """Commit the membership once. Only the winning browser can resume session issuance."""
    token_hash = code_hash(code, signing_secret)
    key_hash = digest(key)
    with transaction(path, write=True) as db:
        target = db.execute("SELECT household_id FROM household_invitations WHERE token_hash = ? AND child_user_id IS NOT NULL",
                            (token_hash,)).fetchone()
        if target is None:
            raise invalid_code()
        household = target["household_id"]
        lock_household(db, household)
        invite = db.execute("SELECT * FROM household_invitations WHERE token_hash = ?", (token_hash,)).fetchone()
        now = datetime.now(timezone.utc)
        if invite["revoked_at"] or datetime.fromisoformat(invite["expires_at"]) <= now:
            raise invalid_code()
        issuer = active_member(db, household, invite["invited_by"])
        if not issuer or issuer["owner_id"] != invite["invited_by"]:
            raise invalid_code()
        user_id = invite["child_user_id"]
        member = db.execute("SELECT active, display_name FROM household_members WHERE household_id = ? AND user_id = ?",
                            (household, user_id)).fetchone()
        if member is not None and not member["active"]:
            raise invalid_code()
        if invite["accepted_at"]:
            if not member or not hmac.compare_digest(invite["redemption_key_hash"], key_hash):
                raise invalid_code()
        if member and name is not None and name != member["display_name"]:
            raise HTTPException(409, "This child account already has a name")
        if invite["accepted_at"]:
            return invite["id"], user_id, member["display_name"]

        if member is None and not name:
            raise HTTPException(422, {"code": "CHILD_NAME_REQUIRED", "message": "Choose a name to join your household."})
        updated = db.execute("""UPDATE household_invitations SET accepted_by = ?, accepted_at = ?, redemption_key_hash = ?
                                WHERE id = ? AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > ?""",
                             (user_id, now.isoformat(), key_hash, invite["id"], now.isoformat()))
        if updated.rowcount != 1:
            raise invalid_code()
        if member:
            return invite["id"], user_id, member["display_name"]
        db.execute("""INSERT INTO household_members (household_id, user_id, active, display_name, is_adult, age_status)
                      VALUES (?, ?, 1, ?, 0, 'NONE')""", (household, user_id, name))
        db.execute("""INSERT INTO cart_grants (household_id, cart_id, member_id)
                      SELECT household_id, id, ? FROM carts WHERE household_id = ? AND adults_only = 0 AND deleted = 0""",
                   (user_id, household))
        for cart in db.execute("SELECT id FROM carts WHERE household_id = ? AND deleted = 0", (household,)).fetchall():
            record_event(db, household, cart["id"], user_id, None, None, column="revision")
        notify_changed(db, household, db_path=path)
        return invite["id"], user_id, name
