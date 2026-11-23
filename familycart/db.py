import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

SQL_DIR = Path(__file__).parent


def initialize_database(path: Path) -> None:
    schema = (SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    seed = (SQL_DIR / "seed.sql").read_text(encoding="utf-8")
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(f"BEGIN IMMEDIATE;\n{schema}\n{seed}\nCOMMIT;")


@contextmanager
def transaction(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        # Writers reserve the write lock before reading membership or grants.
        with db:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db


def active_member(
    db: sqlite3.Connection, household_id: str, user_id: str
) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT role FROM household_members
        WHERE household_id = ? AND user_id = ? AND active = 1
        """,
        (household_id, user_id),
    ).fetchone()


def load_carts(
    db: sqlite3.Connection, household_id: str, user_id: str, cart_id: str | None = None
) -> sqlite3.Cursor:
    query = """
        SELECT carts.id, carts.name, carts.creator_id, carts.adults_only,
               cart_creator_revocations.cart_id IS NOT NULL AS creator_revoked,
               cart_grants.member_id IS NOT NULL AS has_grant
        FROM carts
        LEFT JOIN cart_grants ON cart_grants.cart_id = carts.id AND cart_grants.member_id = ?
        LEFT JOIN cart_creator_revocations ON cart_creator_revocations.cart_id = carts.id
        WHERE carts.household_id = ?
            AND NOT EXISTS (SELECT 1 FROM deleted_carts WHERE deleted_carts.cart_id = carts.id)
    """
    parameters = [user_id, household_id]
    if cart_id is not None:
        query += " AND carts.id = ?"
        parameters.append(cart_id)
    return db.execute(query + " ORDER BY carts.id", parameters)


def delete_cart(db: sqlite3.Connection, cart_id: str) -> None:
    # Keep the cart ID for pending sync events and prevent reseeding deleted demo carts.
    db.execute("INSERT INTO deleted_carts (cart_id) VALUES (?)", (cart_id,))
    db.execute("DELETE FROM cart_item_contributions WHERE cart_id = ?", (cart_id,))
    db.execute("DELETE FROM cart_grants WHERE cart_id = ?", (cart_id,))
    db.execute("DELETE FROM cart_creator_revocations WHERE cart_id = ?", (cart_id,))


def contribution_quantity(db: sqlite3.Connection, cart_id: str, sku: str, contributor_id: str) -> int:
    row = db.execute(
        "SELECT quantity FROM cart_item_contributions WHERE cart_id = ? AND sku = ? AND contributor_id = ?",
        (cart_id, sku, contributor_id),
    ).fetchone()
    return row["quantity"] if row else 0


def set_contribution(
    db: sqlite3.Connection,
    household_id: str,
    cart_id: str,
    sku: str,
    contributor_id: str,
    quantity: int,
) -> None:
    if quantity == 0:
        db.execute(
            "DELETE FROM cart_item_contributions WHERE cart_id = ? AND sku = ? AND contributor_id = ?",
            (cart_id, sku, contributor_id),
        )
        return

    db.execute(
        """
        INSERT INTO cart_item_contributions (household_id, cart_id, sku, contributor_id, quantity)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (cart_id, sku, contributor_id) DO UPDATE SET quantity = excluded.quantity
        """,
        (household_id, cart_id, sku, contributor_id, quantity),
    )


def load_contributions(db: sqlite3.Connection, cart_id: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT sku, contributor_id, quantity FROM cart_item_contributions
        WHERE cart_id = ? ORDER BY sku, contributor_id
        """,
        (cart_id,),
    )
    return [dict(row) for row in rows]
