"""Cart changes that can be sent again without being applied again.

The browser gives every change a key and repeats the key when it retries. The
key and the result are saved in the transaction that makes the change, so a
timeout whose change was committed is answered with that first result.

Callers hold the household lock, so two copies of a request cannot run together.
The primary key settles anything that gets past that.
"""

import json
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Annotated, TypeVar

from fastapi import Header, HTTPException
from pydantic import BaseModel

from backend.shared.database import DatabaseConnection

# Browsers give up after 7 days, long before a key is forgotten.
KEPT_FOR = timedelta(days=30)
# A reader is told about changes this recent without naming them.
RECENT = timedelta(minutes=10)
MAX_NAMED = 100

OperationKey = Annotated[
    str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[!-~]+$"),
]
Result = TypeVar("Result", bound=BaseModel)


class Operation:
    def __init__(self, db: DatabaseConnection, household_id: str, user_id: str, key: str | None, parts: tuple) -> None:
        self.db, self.household_id, self.user_id, self.key = db, household_id, user_id, key
        self.fingerprint = sha256(json.dumps(parts).encode()).hexdigest()
        self.previous: dict | None = None
        if key is None:
            return
        row = db.execute(
            "SELECT fingerprint, result FROM cart_operations WHERE user_id = ? AND request_id = ?", (user_id, key),
        ).fetchone()
        if row is not None:
            if row["fingerprint"] != self.fingerprint:
                raise key_reused()
            self.previous = json.loads(row["result"])
        db._operation = key  # named in the events this transaction records

    def save(self, result: Result) -> Result:
        if self.key is None:
            return result
        saved = self.db.execute(
            """INSERT INTO cart_operations (user_id, request_id, household_id, fingerprint, result, created_at)
               VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (user_id, request_id) DO NOTHING RETURNING request_id""",
            (self.user_id, self.key, self.household_id, self.fingerprint, result.model_dump_json(),
             datetime.now(timezone.utc).isoformat()),
        ).fetchone()
        if saved is None:
            raise key_reused()  # rolls the change back
        return result


def key_reused() -> HTTPException:
    return HTTPException(status_code=409, detail="This request key was already used for a different change.")


def event_key(db: DatabaseConnection) -> str | None:
    return getattr(db, "_operation", None)


def applied_keys(db: DatabaseConnection, household_id: str, user_id: str, named: Iterable[str] = ()) -> list[str]:
    """This person's own saved changes: the recent ones, and any they ask about by name."""
    named = list(named)[:MAX_NAMED]
    since = (datetime.now(timezone.utc) - RECENT).isoformat()
    asked = f" OR request_id IN ({', '.join('?' * len(named))})" if named else ""
    return [row["request_id"] for row in db.execute(
        f"""SELECT request_id FROM cart_operations
            WHERE user_id = ? AND household_id = ? AND (created_at > ?{asked}) ORDER BY request_id""",
        (user_id, household_id, since, *named),
    ).fetchall()]


def forget_old(db: DatabaseConnection) -> None:
    db.execute(
        "DELETE FROM cart_operations WHERE created_at < ?", ((datetime.now(timezone.utc) - KEPT_FOR).isoformat(),),
    )
