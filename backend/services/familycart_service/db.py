import os
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from backend.shared.database import DatabaseConnection, DatabaseTarget, connect_database, is_postgres_target

SQL_DIR = Path(__file__).parent


def database_target() -> DatabaseTarget:
    return os.environ.get("FAMILYCART_DATABASE_URL") or Path(
        os.environ.get("FAMILYCART_DB_PATH", "familycart.sqlite3")
    )


def initialize_database(path: DatabaseTarget) -> None:
    schema_name = "postgres_schema.sql" if is_postgres_target(path) else "schema.sql"
    schema = (SQL_DIR / schema_name).read_text(encoding="utf-8")
    seed = (SQL_DIR / "seed.sql").read_text(encoding="utf-8")
    with closing(connect_database(path)) as db:
        if db.dialect == "sqlite":
            db.execute_script(f"BEGIN IMMEDIATE;\n{schema}\n{seed}\nCOMMIT;")
        else:
            db.execute("BEGIN")
            try:
                db.execute_script(schema)
                db.execute_script(seed)
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()


@contextmanager
def transaction(path: DatabaseTarget, *, write: bool = False) -> Iterator[DatabaseConnection]:
    with closing(connect_database(path)) as db:
        # Writers reserve the write lock before reading membership or grants.
        db.execute("BEGIN IMMEDIATE" if write and db.dialect == "sqlite" else "BEGIN")
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()


def active_member(
    db: DatabaseConnection, household_id: str, user_id: str
):
    return db.execute(
        """
        SELECT members.role, profiles.created_by AS owner_id
        FROM household_members AS members
        JOIN household_profiles AS profiles ON profiles.household_id = members.household_id
        WHERE members.household_id = ? AND members.user_id = ? AND members.active = 1
        """,
        (household_id, user_id),
    ).fetchone()


def load_carts(
    db: DatabaseConnection, household_id: str, user_id: str, cart_id: str | None = None
):
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


def delete_cart(db: DatabaseConnection, cart_id: str) -> None:
    # Keep the cart ID for pending sync events and prevent reseeding deleted demo carts.
    db.execute("INSERT INTO deleted_carts (cart_id) VALUES (?)", (cart_id,))
    db.execute("DELETE FROM cart_item_contributions WHERE cart_id = ?", (cart_id,))
    db.execute("DELETE FROM cart_grants WHERE cart_id = ?", (cart_id,))
    db.execute("DELETE FROM cart_creator_revocations WHERE cart_id = ?", (cart_id,))


def contribution_quantity(db: DatabaseConnection, cart_id: str, sku: str, contributor_id: str) -> int:
    row = db.execute(
        "SELECT quantity FROM cart_item_contributions WHERE cart_id = ? AND sku = ? AND contributor_id = ?",
        (cart_id, sku, contributor_id),
    ).fetchone()
    return row["quantity"] if row else 0


def set_contribution(
    db: DatabaseConnection,
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


def load_contributions(db: DatabaseConnection, cart_id: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT sku, contributor_id, quantity FROM cart_item_contributions
        WHERE cart_id = ? ORDER BY sku, contributor_id
        """,
        (cart_id,),
    )
    return [dict(row) for row in rows]
