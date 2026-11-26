import os
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from backend.shared.database import (
    DatabaseConnection, DatabaseTarget, connect_database, is_postgres_target, uses_supabase_schema,
)

SQL_DIR = Path(__file__).parent
ADDRESS_TABLES = ("household_addresses",)
ADDRESS_FIELDS = "id, label, recipient_name, address_line1, address_line2, city, postal_code, phone, latitude, longitude"

# A household address belongs to the member who added it, and only counts while they are a member.
# The member sees their own; the current primary sees everybody's.
ACTIVE_ADDRESS = """EXISTS (SELECT 1 FROM household_members AS owner
    WHERE owner.household_id = household_addresses.household_id
      AND owner.user_id = household_addresses.created_by AND owner.active = 1)"""


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
            db.execute_script(f"BEGIN IMMEDIATE;\n{schema}\n{checkout_schema}")
            with db:
                add_sqlite_columns(db, "household_invitations", {"child_user_id": "TEXT", "redemption_key_hash": "TEXT"})
                add_sqlite_columns(db, "cart_sync_events", {"actor_id": "TEXT", "reason": "TEXT", "request_id": "TEXT"})
                add_sqlite_columns(db, "carts", {"inventory_store_id": "TEXT"})
                for table in ADDRESS_TABLES:
                    add_sqlite_columns(db, table, {"latitude": "REAL", "longitude": "REAL"})
                merge_tables(db)
                db.execute_script(seed)
        else:
            db.execute("BEGIN")
            try:
                db.execute_script(schema)
                db.execute("ALTER TABLE household_invitations ADD COLUMN IF NOT EXISTS child_user_id TEXT, "
                           "ADD COLUMN IF NOT EXISTS redemption_key_hash TEXT")
                db.execute("ALTER TABLE cart_sync_events ADD COLUMN IF NOT EXISTS actor_id TEXT")
                db.execute("ALTER TABLE cart_sync_events ADD COLUMN IF NOT EXISTS reason TEXT")
                db.execute("ALTER TABLE cart_sync_events ADD COLUMN IF NOT EXISTS request_id TEXT")
                db.execute("ALTER TABLE carts ADD COLUMN IF NOT EXISTS inventory_store_id TEXT")
                for table in ADDRESS_TABLES:
                    db.execute(f"""ALTER TABLE {table}
                        ADD COLUMN IF NOT EXISTS latitude DOUBLE PRECISION,
                        ADD COLUMN IF NOT EXISTS longitude DOUBLE PRECISION""")
                db.execute_script(checkout_schema)
                merge_tables(db)
                db.execute_script(seed)
            except Exception:
                db.rollback()
                raise
            else:
                db.commit()


# Columns that replaced eight tables, as (table, column, definition).
MERGED_COLUMNS = (
    ("households", "name", "TEXT"),
    ("households", "admin_id", "TEXT"),
    ("households", "created_at", "TEXT"),
    ("household_members", "display_name", "TEXT"),
    ("household_members", "is_adult", "SMALLINT NOT NULL DEFAULT 0"),
    ("household_members", "age_status", "TEXT NOT NULL DEFAULT 'APPROVED'"),
    ("carts", "deleted", "SMALLINT NOT NULL DEFAULT 0"),
    ("carts", "next_cart_id", "TEXT"),
    ("carts", "creator_revoked", "SMALLINT NOT NULL DEFAULT 0"),
    ("carts", "revision", "BIGINT NOT NULL DEFAULT 0"),
    ("carts", "live_revision", "BIGINT NOT NULL DEFAULT 0"),
)
RETIRED_TABLES = (
    "household_member_profiles", "household_profiles", "deleted_carts", "cart_creator_revocations",
    "checkout_cart_revisions", "user_addresses", "personal_cart_items", "personal_carts",
)


def merge_tables(db: DatabaseConnection) -> None:
    """Bring a database from before the merge up to date. Does nothing on a new or already merged one.

    The hosted database gets the same change from supabase/migrations/20261126120000_merge_tables.sql.
    """
    if db.dialect == "sqlite":
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        columns = lambda table: {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    else:
        tables = {row["tablename"] for row in db.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
        columns = lambda table: {row["column_name"] for row in db.execute(
            """SELECT column_name FROM information_schema.columns
               WHERE table_schema = current_schema() AND table_name = ?""", (table,))}
    for table, column, definition in MERGED_COLUMNS:
        if column not in columns(table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    if "household_profiles" in tables:
        db.execute("""UPDATE households SET
            name = (SELECT name FROM household_profiles WHERE household_id = households.id),
            admin_id = (SELECT created_by FROM household_profiles WHERE household_id = households.id),
            created_at = (SELECT created_at FROM household_profiles WHERE household_id = households.id)""")
    if "household_member_profiles" in tables:
        profile = "FROM household_member_profiles AS p WHERE p.household_id = household_members.household_id " \
                  "AND p.user_id = household_members.user_id"
        # Before age checks existed this column was missing, and everybody kept their access.
        status = "p.age_status" if "age_status" in columns("household_member_profiles") else "NULL"
        db.execute(f"""UPDATE household_members SET
            display_name = (SELECT p.display_name {profile}),
            is_adult = COALESCE((SELECT p.is_adult {profile}), 0),
            age_status = COALESCE((SELECT {status} {profile}), 'APPROVED')""")
    if "deleted_carts" in tables:
        db.execute("""UPDATE carts SET deleted = 1,
            next_cart_id = (SELECT next_cart_id FROM deleted_carts WHERE cart_id = carts.id)
            WHERE id IN (SELECT cart_id FROM deleted_carts)""")
    if "cart_creator_revocations" in tables:
        db.execute("UPDATE carts SET creator_revoked = 1 WHERE id IN (SELECT cart_id FROM cart_creator_revocations)")
    if "checkout_cart_revisions" in tables:
        revision = "FROM checkout_cart_revisions AS r WHERE r.kind = 'household' AND r.cart_id = carts.id"
        live = "live_revision" if "live_revision" in columns("checkout_cart_revisions") else "0"
        db.execute(f"""UPDATE carts SET
            revision = COALESCE((SELECT r.revision {revision}), 0),
            live_revision = COALESCE((SELECT r.{live} {revision}), 0)""")
    if "role" in columns("household_members"):
        db.execute("""UPDATE households SET admin_id = (SELECT user_id FROM household_members
                      WHERE household_id = households.id AND role = 'primary' LIMIT 1) WHERE admin_id IS NULL""")
        db.execute("ALTER TABLE household_members DROP COLUMN role")
    # Anything without the old details still gets usable values.
    db.execute("UPDATE households SET name = 'My Household' WHERE name IS NULL")
    db.execute("UPDATE households SET created_at = '2026-01-01T00:00:00+00:00' WHERE created_at IS NULL")
    db.execute("UPDATE household_members SET display_name = user_id WHERE display_name IS NULL")
    for table in RETIRED_TABLES:
        if table in tables:
            db.execute(f"DROP TABLE {table}")


def add_sqlite_columns(db: DatabaseConnection, table: str, definitions: dict[str, str]) -> None:
    columns = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
    for column, data_type in definitions.items():
        if column not in columns:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {data_type}")


@contextmanager
def transaction(path: DatabaseTarget, *, write: bool = False, snapshot: bool = False) -> Iterator[DatabaseConnection]:
    """``snapshot`` makes every query see the database at one moment, so contents and revision belong together."""
    with closing(connect_database(path)) as db:
        # Writers reserve the write lock before reading membership or grants.
        if db.dialect == "sqlite":
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        # psycopg opens the PostgreSQL transaction itself on the first statement,
        # so an explicit BEGIN would only cost an extra round trip.
        try:
            if snapshot and db.dialect == "postgresql":
                db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            yield db
        except Exception:
            db.rollback()
            raise
        else:
            db.commit()


def active_member(
    db: DatabaseConnection, household_id: str, user_id: str, *, lock: bool = False
):
    row_lock = "FOR SHARE OF members, household" if lock and db.dialect == "postgresql" else ""
    return db.execute(
        f"""
        SELECT household.admin_id AS owner_id, members.age_status, members.is_adult
        FROM household_members AS members
        JOIN households AS household ON household.id = members.household_id
        WHERE members.household_id = ? AND members.user_id = ? AND members.active = 1
        {row_lock}
        """,
        (household_id, user_id),
    ).fetchone()


def lock_household(db: DatabaseConnection, household_id: str, user_id: str | None = None):
    """Every household writer takes this lock before reading roles or changing carts.

    Given ``user_id``, also returns that person's active membership (like active_member), or None.
    On PostgreSQL both come from one query, which saves a trip to the database on every change.
    """
    if db.dialect == "postgresql" and user_id is not None:
        row = db.execute(
            """SELECT households.admin_id AS owner_id, members.age_status, members.is_adult,
                      members.user_id AS member, limits.*
               FROM households
               CROSS JOIN (
                   SELECT set_config('lock_timeout', '3500ms', true), set_config('statement_timeout', '4500ms', true)
                   OFFSET 0
               ) AS limits(lock_wait, statement_limit)
               LEFT JOIN household_members AS members
                 ON members.household_id = households.id AND members.user_id = ? AND members.active = 1
               WHERE households.id = ? FOR NO KEY UPDATE OF households""", (user_id, household_id),
        ).fetchone()
        return row if row is not None and row["member"] is not None else None
    if db.dialect == "postgresql":
        # The same query limits how long this transaction waits, so work a browser has
        # stopped waiting for (5 seconds) does not keep holding connections.
        db.execute(
            """SELECT households.id, limits.* FROM households, (
                   SELECT set_config('lock_timeout', '3500ms', true), set_config('statement_timeout', '4500ms', true)
                   OFFSET 0
               ) AS limits(lock_wait, statement_limit)
               WHERE households.id = ? FOR NO KEY UPDATE OF households""", (household_id,),
        ).fetchone()
    return active_member(db, household_id, user_id) if user_id is not None else None


def record_event(
    db: DatabaseConnection, household_id: str, cart_id: str, actor_id: str | None, reason: str | None,
    request_id: str | None, *, column: str,
) -> int:
    """Add a "this cart changed" event and store its number in carts.revision or carts.live_revision."""
    values = (household_id, cart_id, actor_id, reason, request_id)
    insert = """INSERT INTO cart_sync_events (household_id, cart_id, actor_id, reason, request_id)
                VALUES (?, ?, ?, ?, ?) RETURNING id"""
    if db.dialect == "postgresql":
        # One statement instead of two.
        return db.execute(
            f"""WITH event AS ({insert})
                UPDATE carts SET {column} = event.id FROM event WHERE carts.id = ? RETURNING event.id""",
            (*values, cart_id),
        ).fetchone()["id"]
    event = db.execute(insert, values).fetchone()["id"]
    db.execute(f"UPDATE carts SET {column} = ? WHERE id = ?", (event, cart_id))
    return event


def household_member(db: DatabaseConnection, household_id: str, user_id: str):
    return db.execute(
        """
        SELECT members.active, household.admin_id AS owner_id
        FROM household_members AS members
        JOIN households AS household ON household.id = members.household_id
        WHERE members.household_id = ? AND members.user_id = ?
        """,
        (household_id, user_id),
    ).fetchone()


def load_carts(
    db: DatabaseConnection, household_id: str, user_id: str, cart_id: str | None = None
):
    query = """
        SELECT carts.id, carts.name, carts.creator_id, carts.adults_only,
           carts.creator_revoked, carts.inventory_store_id,
               cart_grants.member_id IS NOT NULL AS has_grant
        FROM carts
        LEFT JOIN cart_grants ON cart_grants.cart_id = carts.id AND cart_grants.member_id = ?
        WHERE carts.household_id = ? AND carts.deleted = 0
    """
    parameters = [user_id, household_id]
    if cart_id is not None:
        query += " AND carts.id = ?"
        parameters.append(cart_id)
    return db.execute(query + " ORDER BY carts.id", parameters)


def delete_cart(db: DatabaseConnection, cart_id: str) -> None:
    # Keep the cart row for pending sync events and old records, and so demo carts are not seeded again.
    db.execute("UPDATE carts SET deleted = 1, creator_revoked = 0 WHERE id = ?", (cart_id,))
    db.execute("DELETE FROM cart_item_contributions WHERE cart_id = ?", (cart_id,))
    db.execute("DELETE FROM cart_grants WHERE cart_id = ?", (cart_id,))


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
    LEFT JOIN household_members AS adder
      ON adder.household_id = items.household_id AND adder.user_id = items.added_by
    LEFT JOIN household_members AS checker
      ON checker.household_id = items.household_id AND checker.user_id = items.checked_by
"""
