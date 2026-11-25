import os
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from backend.shared.database import (
    DatabaseConnection, DatabaseTarget, connect_database, is_postgres_target, uses_supabase_schema,
)

SQL_DIR = Path(__file__).parent
ADDRESS_TABLES = ("user_addresses", "household_addresses")
ADDRESS_FIELDS = "id, label, recipient_name, address_line1, address_line2, city, postal_code, phone, latitude, longitude"


def database_target() -> DatabaseTarget:
    return os.environ.get("FAMILYCART_DATABASE_URL") or Path(
        os.environ.get("FAMILYCART_DB_PATH", "familycart.sqlite3")
    )


def initialize_database(path: DatabaseTarget) -> None:
    if uses_supabase_schema(path, "familycart_shopping"):
        return
    schema_name = "postgres_schema.sql" if is_postgres_target(path) else "schema.sql"
    schema = (SQL_DIR / schema_name).read_text(encoding="utf-8")
    seed = (SQL_DIR / "seed.sql").read_text(encoding="utf-8")
    checkout_schema = (SQL_DIR / "checkout_schema.sql").read_text(encoding="utf-8")
    with closing(connect_database(path)) as db:
        if db.dialect == "sqlite":
            db.execute_script(f"BEGIN IMMEDIATE;\n{schema}\n{seed}\n{checkout_schema}")
            with db:
                add_sqlite_columns(db, "cart_sync_events", {"actor_id": "TEXT"})
                for table in ADDRESS_TABLES:
                    add_sqlite_columns(db, table, {"latitude": "REAL", "longitude": "REAL"})
        else:
            db.execute("BEGIN")
            try:
                db.execute_script(schema)
                db.execute("ALTER TABLE cart_sync_events ADD COLUMN IF NOT EXISTS actor_id TEXT")
                for table in ADDRESS_TABLES:
                    db.execute(f"""ALTER TABLE {table}
                        ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION""")
                db.execute_script(seed)
                db.execute_script(checkout_schema)
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()


def add_sqlite_columns(db: DatabaseConnection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for column, data_type in definitions.items():
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")


@contextmanager
def transaction(path: DatabaseTarget, *, write: bool = False) -> Iterator[DatabaseConnection]:
    with closing(connect_database(path)) as db:
        # Writers reserve the write lock before reading membership or grants.
        if db.dialect == "sqlite":
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        # psycopg opens the PostgreSQL transaction itself on the first statement,
        # so an explicit BEGIN would only cost an extra round trip.
        try:
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()


def active_member(
    db: DatabaseConnection, household_id: str, user_id: str, *, lock: bool = False
):
    row_lock = "FOR SHARE OF members, profiles" if lock and db.dialect == "postgresql" else ""
    return db.execute(
        f"""
        SELECT members.role, profiles.created_by AS owner_id
        FROM household_members AS members
        JOIN household_profiles AS profiles ON profiles.household_id = members.household_id
        WHERE members.household_id = ? AND members.user_id = ? AND members.active = 1
        {row_lock}
        """,
        (household_id, user_id),
    ).fetchone()


def lock_household(db: DatabaseConnection, household_id: str) -> None:
    # Every household writer takes this lock before reading roles or changing carts.
    if db.dialect == "postgresql":
        db.execute("SELECT id FROM households WHERE id = ? FOR NO KEY UPDATE", (household_id,)).fetchone()


def personal_cart(db: DatabaseConnection, user_id: str, *, lock: bool = False):
    row_lock = " FOR NO KEY UPDATE" if lock and db.dialect == "postgresql" else ""
    return db.execute(
        "SELECT id FROM personal_carts WHERE user_id = ?" + row_lock, (user_id,)
    ).fetchone()


def increment_personal_revision(db: DatabaseConnection, cart_id: str, updated_at: str) -> None:
    db.execute("UPDATE personal_carts SET updated_at = ? WHERE id = ?", (updated_at, cart_id))
    db.execute(
        """INSERT INTO checkout_cart_revisions (kind, cart_id, revision) VALUES ('personal', ?, 1)
           ON CONFLICT (kind, cart_id) DO UPDATE SET revision = checkout_cart_revisions.revision + 1""", (cart_id,),
    )


def household_member(db: DatabaseConnection, household_id: str, user_id: str):
    return db.execute(
        """
        SELECT members.role, members.active, profiles.created_by AS owner_id
        FROM household_members AS members
        JOIN household_profiles AS profiles ON profiles.household_id = members.household_id
        WHERE members.household_id = ? AND members.user_id = ?
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


# Shared shopping-list columns, used by the list routes and the live-update worker.
LIST_COLUMNS = """
    lists.id, lists.name, lists.created_by, lists.updated_at,
    COALESCE(creator.display_name, lists.created_by) AS created_by_name
"""
ITEM_COLUMNS = """
    items.id, items.list_id, items.sku, items.custom_name, items.quantity, items.note,
    items.added_by, items.checked, items.checked_by, items.created_at, items.updated_at,
    COALESCE(adder.display_name, items.added_by) AS added_by_name,
    checker.display_name AS checked_by_name
"""
ITEM_JOINS = """
    LEFT JOIN household_member_profiles AS adder
      ON adder.household_id = items.household_id AND adder.user_id = items.added_by
    LEFT JOIN household_member_profiles AS checker
      ON checker.household_id = items.household_id AND checker.user_id = items.checked_by
"""
