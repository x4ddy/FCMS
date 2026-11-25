"""What happens to a shared cart while somebody is checking it out.

From the moment a member opens checkout until they order or leave, the cart's
contents stand still. Anything added meanwhile waits beside the cart. When the
order is placed the waiting items start a new cart; if checkout is cancelled or
abandoned they join the cart they were meant for.

Callers hold the household lock (``household_access(write=True)``).
"""

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from backend.shared.database import DatabaseConnection

from .db import delete_cart, set_contribution
from .live_sync import notify_changed

# The browser renews every minute, so a closed tab frees the cart soon after this.
SESSION_LENGTH = timedelta(minutes=3)
MAX_NAME = 100


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def expired(session, now: datetime | None = None) -> bool:
    return datetime.fromisoformat(session["expires_at"]) <= (now or now_utc())


def stored_session(db: DatabaseConnection, cart_id: str):
    """The session row as saved, which may have run out."""
    return db.execute(
        "SELECT cart_id, user_id, expires_at FROM checkout_sessions WHERE cart_id = ?", (cart_id,),
    ).fetchone()


def record_waiting_change(db: DatabaseConnection, household_id: str, cart_id: str, actor_id: str | None) -> int:
    """Tell live viewers, without changing the cart revision the open review is checked against."""
    notify_changed(db, household_id)
    return db.execute(
        "INSERT INTO cart_sync_events (household_id, cart_id, actor_id) VALUES (?, ?, ?) RETURNING id",
        (household_id, cart_id, actor_id),
    ).fetchone()["id"]


def current_session(db: DatabaseConnection, household_id: str, cart_id: str):
    """The running session, or None. A session that ran out is ended here."""
    session = stored_session(db, cart_id)
    if session is None:
        return None
    if expired(session):
        end_session(db, household_id, cart_id)
        return None
    return session


def save_session(db: DatabaseConnection, household_id: str, cart_id: str, user_id: str) -> None:
    now = now_utc()
    db.execute(
        """INSERT INTO checkout_sessions (cart_id, household_id, user_id, started_at, expires_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (cart_id) DO UPDATE SET user_id = excluded.user_id, expires_at = excluded.expires_at""",
        (cart_id, household_id, user_id, now.isoformat(), (now + SESSION_LENGTH).isoformat()),
    )


def waiting_rows(db: DatabaseConnection, cart_id: str) -> list[dict]:
    return [dict(row) for row in db.execute(
        """SELECT sku, contributor_id, quantity FROM cart_waiting_items
           WHERE cart_id = ? ORDER BY sku, contributor_id""", (cart_id,),
    )]


def set_waiting(
    db: DatabaseConnection, household_id: str, cart_id: str, sku: str, contributor_id: str, quantity: int,
) -> None:
    if quantity <= 0:
        db.execute(
            "DELETE FROM cart_waiting_items WHERE cart_id = ? AND sku = ? AND contributor_id = ?",
            (cart_id, sku, contributor_id),
        )
        return
    db.execute(
        """INSERT INTO cart_waiting_items (household_id, cart_id, sku, contributor_id, quantity)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT (cart_id, sku, contributor_id) DO UPDATE SET quantity = excluded.quantity""",
        (household_id, cart_id, sku, contributor_id, quantity),
    )


def move_waiting(db: DatabaseConnection, household_id: str, source: str, target: str, maximum: int) -> None:
    for row in waiting_rows(db, source):
        current = db.execute(
            "SELECT quantity FROM cart_item_contributions WHERE cart_id = ? AND sku = ? AND contributor_id = ?",
            (target, row["sku"], row["contributor_id"]),
        ).fetchone()
        total = min(maximum, (current["quantity"] if current else 0) + row["quantity"])
        set_contribution(db, household_id, target, row["sku"], row["contributor_id"], total)
    db.execute("DELETE FROM cart_waiting_items WHERE cart_id = ?", (source,))


def end_session(db: DatabaseConnection, household_id: str, cart_id: str, actor_id: str | None = None) -> None:
    """Checkout ended without an order: waiting items join the cart."""
    from .app import MAX_QUANTITY, record_cart_change

    db.execute("DELETE FROM checkout_sessions WHERE cart_id = ?", (cart_id,))
    move_waiting(db, household_id, cart_id, cart_id, MAX_QUANTITY)
    record_cart_change(db, household_id, cart_id, actor_id)


def unused_name(db: DatabaseConnection, household_id: str, name: str) -> str:
    taken = {row["name"] for row in db.execute(
        """SELECT name FROM carts WHERE household_id = ?
           AND NOT EXISTS (SELECT 1 FROM deleted_carts WHERE deleted_carts.cart_id = carts.id)""", (household_id,),
    )}
    number = 2
    while True:
        suffix = f" {number}"
        candidate = name[:MAX_NAME - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        number += 1


def finish_session(db: DatabaseConnection, household_id: str, cart_id: str) -> dict | None:
    """The order was placed: waiting items start a new cart with the same people. Returns it, if one was needed.

    The old cart is retired when nothing is left in it, and the new one takes its name.
    """
    from .app import MAX_QUANTITY, record_cart_change

    db.execute("DELETE FROM checkout_sessions WHERE cart_id = ?", (cart_id,))
    if not waiting_rows(db, cart_id):
        return None
    old = db.execute("SELECT creator_id, name, adults_only FROM carts WHERE id = ?", (cart_id,)).fetchone()
    emptied = db.execute(
        "SELECT 1 FROM cart_item_contributions WHERE cart_id = ? LIMIT 1", (cart_id,),
    ).fetchone() is None
    new_id = str(uuid4())
    name = old["name"] if emptied else unused_name(db, household_id, old["name"])
    db.execute(
        "INSERT INTO carts (id, household_id, creator_id, name, adults_only) VALUES (?, ?, ?, ?, ?)",
        (new_id, household_id, old["creator_id"], name, old["adults_only"]),
    )
    db.execute(
        """INSERT INTO cart_grants (household_id, cart_id, member_id)
           SELECT household_id, ?, member_id FROM cart_grants WHERE cart_id = ?""", (new_id, cart_id),
    )
    db.execute(
        "INSERT INTO cart_creator_revocations (cart_id) SELECT ? FROM cart_creator_revocations WHERE cart_id = ?",
        (new_id, cart_id),
    )
    move_waiting(db, household_id, cart_id, new_id, MAX_QUANTITY)
    if emptied:
        delete_cart(db, cart_id)
        db.execute("UPDATE deleted_carts SET next_cart_id = ? WHERE cart_id = ?", (new_id, cart_id))
    record_cart_change(db, household_id, new_id)
    return {"id": new_id, "name": name, "replaces": emptied}


def open_cart_id(db: DatabaseConnection, cart_id: str) -> str:
    """Follow a cart that was checked out to the new cart that took its place."""
    for _ in range(20):
        row = db.execute("SELECT next_cart_id FROM deleted_carts WHERE cart_id = ?", (cart_id,)).fetchone()
        if row is None or row["next_cart_id"] is None:
            return cart_id
        cart_id = row["next_cart_id"]
    return cart_id
