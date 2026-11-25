"""Check the hosted database or transfer the two legacy Compose databases."""

import os
import sqlite3
import subprocess
import sys
from contextlib import ExitStack, closing

import psycopg
from psycopg import sql

from backend.services.familycart_service.catalogue import load_catalogue
from backend.services.familycart_service.checkout import seed_inventory
from backend.services.familycart_service.db import SQL_DIR, initialize_database as initialize_shopping
from backend.services.user_service.db import initialize_database as initialize_identity
from backend.shared.database import DatabaseConnection, close_pools


# Parent rows must exist before their children can be inserted.
STORES = {
    "familycart_identity": (
        "FAMILYCART_USER_DATABASE_URL", "familycart_users", "familycart_user", 5433,
        "users refresh_sessions refresh_token_history".split(),
    ),
    "familycart_shopping": (
        "FAMILYCART_DATABASE_URL", "familycart", "familycart_app", 5434,
        ("households household_members household_profiles household_member_profiles carts "
         "household_invitations household_addresses user_addresses personal_carts personal_cart_items "
         "cart_grants cart_creator_revocations deleted_carts cart_item_contributions cart_sync_events "
         "shopping_lists shopping_list_items personal_lists personal_list_items store_inventory checkout_cart_revisions checkout_quotes "
         "orders order_delivery_groups order_lines").split(),
    ),
}

# Columns added after the baseline. Missing ones mean a migration has not been applied.
ADDED_COLUMNS = {"household_member_profiles": ["age_status"], "cart_sync_events": ["reason"]}
# Columns an unreleased build left in some local databases. Nothing reads them.
RETIRED_COLUMNS = {"household_addresses": {"source_address_id"}}


def names(items):
    return sql.SQL(", ").join(map(sql.Identifier, items))


def check_hosted(connection, role, tables):
    if connection.execute("SELECT current_user, current_schema()").fetchone() != (role, role):
        raise RuntimeError(f"The {role} login reached the wrong role or schema.")
    try:
        for table in tables:
            connection.execute(sql.SQL("SELECT {} FROM {} LIMIT 0").format(
                names(ADDED_COLUMNS[table]) if table in ADDED_COLUMNS else sql.SQL("1"), sql.Identifier(table)))
    except (psycopg.errors.UndefinedTable, psycopg.errors.UndefinedColumn) as error:
        raise RuntimeError(
            f"The hosted {role} schema is out of date ({error.diag.message_primary}). Ask the project "
            "administrator to apply the files in supabase/migrations in order."
        ) from None
    print(f"Connected as {role}. Verified access to {len(tables)} tables.", flush=True)


def read_rows(connection, table, columns, key_indexes):
    query = sql.SQL("SELECT {} FROM {}").format(names(columns), sql.Identifier(table))
    # Text results can round map coordinates differently on the two servers.
    return {tuple(row[i] for i in key_indexes): row for row in connection.execute(query, binary=True)}


def seed_reference(connection):
    for filename in ("schema.sql", "checkout_schema.sql", "seed.sql"):
        connection.executescript((SQL_DIR / filename).read_text(encoding="utf-8"))
    seed_inventory(DatabaseConnection(connection, "sqlite"), load_catalogue())


def copy_schema(source, target, role, tables, reference):
    expected = set(tables)
    schema_query = "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    for db in (source, target):
        actual = {name for name, in db.execute(schema_query)}
        if actual != expected:
            raise RuntimeError(f"{role} table layout differs from the migration: {sorted(actual ^ expected)}")

    # Hold the destination stable while comparing, writing and verifying.
    target.execute(sql.SQL("LOCK TABLE {} IN SHARE ROW EXCLUSIVE MODE").format(names(tables)))
    # A tailored demo seed may replace our fresh seed only on an unused destination.
    plans = []
    pristine = role == "familycart_shopping"
    for table in tables:
        column_query = sql.SQL("SELECT * FROM {} LIMIT 0").format(sql.Identifier(table))
        columns = [c.name for c in target.execute(column_query).description]
        source_columns = {c.name for c in source.execute(column_query).description}
        if set(columns) != source_columns - RETIRED_COLUMNS.get(table, set()):
            raise RuntimeError(f"Column mismatch in {role}.{table}.")
        keys = [r[0] for r in target.execute("""
            SELECT a.attname FROM pg_index i
            CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(num,pos)
            JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.num
            WHERE i.indrelid=to_regclass(%s) AND i.indisprimary ORDER BY k.pos
        """, (table,))]
        if not keys:
            raise RuntimeError(f"Missing primary key in {table}.")
        indexes = [columns.index(k) for k in keys]
        local = read_rows(source, table, columns, indexes)
        hosted = read_rows(target, table, columns, indexes)
        if pristine:
            # Names come from the application schema, and both engines use SQL identifier quoting.
            projection = names(columns).as_string(target)
            fresh = set(reference.execute(f'SELECT {projection} FROM "{table}"'))
            pristine = set(hosted.values()) == fresh
        plans.append((table, columns, keys, indexes, local, hosted))

    for table, columns, keys, indexes, local, hosted in plans:
        inserts, updates = [], []
        fields = [c for c in columns if c not in keys]
        for key, row in local.items():
            if key not in hosted:
                inserts.append(row)
            elif row != hosted[key]:
                if not pristine:
                    raise RuntimeError(f"Conflicting hosted row in {role}.{table}. Transfer rolled back.")
                updates.append(tuple(row[columns.index(c)] for c in fields) + key)
        with target.cursor() as cur:
            if inserts:
                cur.executemany(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                    sql.Identifier(table), names(columns),
                    sql.SQL(", ").join(sql.Placeholder(format=sql.PyFormat.BINARY) for _ in columns)), inserts)
            if updates:
                assignment = sql.SQL("{} = %b")
                cur.executemany(sql.SQL("UPDATE {} SET {} WHERE {}").format(
                    sql.Identifier(table),
                    sql.SQL(", ").join(assignment.format(sql.Identifier(c)) for c in fields),
                    sql.SQL(" AND ").join(assignment.format(sql.Identifier(c)) for c in keys)), updates)
        after = read_rows(target, table, columns, indexes)
        expected_rows = {**hosted, **local}
        if after.keys() != expected_rows.keys():
            missing = len(expected_rows.keys() - after.keys())
            unexpected = len(after.keys() - expected_rows.keys())
            raise RuntimeError(f"Row verification failed for {role}.{table}: {missing} missing, {unexpected} unexpected.")
        for key, row in expected_rows.items():
            if row != after[key]:
                changed = [name for name, a, b in zip(columns, row, after[key]) if a != b]
                raise RuntimeError(f"Row verification failed for {role}.{table}, columns: {', '.join(changed)}.")
        print(f"{role}.{table}: verified {len(local)} source rows.")

    if role == "familycart_shopping":
        # Imported event IDs are explicit. The runtime role has USAGE on this
        # sequence, so advance it without granting it administrator privileges.
        highest, = target.execute("SELECT max(id) FROM cart_sync_events").fetchone()
        if highest is not None:
            sequence = "cart_sync_events_id_seq"
            current, = target.execute("SELECT nextval(%s)", (sequence,)).fetchone()
            if current < highest:
                target.execute("SELECT count(nextval(%s)) FROM generate_series(1::bigint,%s)",
                               (sequence, highest - current)).fetchone()


def local_url(role):
    _, dbname, user, port, _ = STORES[role]
    return f"postgresql://{user}:familycart_local_password@127.0.0.1:{port}/{dbname}"


def upgrade_local_databases():
    """Give the local databases today's columns and tables, as starting the app locally always did."""
    mode = os.environ.pop("FAMILYCART_DB_MODE", None)
    try:
        initialize_identity(local_url("familycart_identity"))
        initialize_shopping(local_url("familycart_shopping"))
    finally:
        if mode is not None:
            os.environ["FAMILYCART_DB_MODE"] = mode
        close_pools()


def run(migrate):
    if migrate:
        print(f"Verifying source rows in binary format ({__file__}).", flush=True)
    with ExitStack() as stack:
        targets = {}
        for role, (key, _, _, _, tables) in STORES.items():
            target = stack.enter_context(psycopg.connect(os.environ[key], connect_timeout=6))
            target.execute("SET LOCAL lock_timeout = '10s'")
            check_hosted(target, role, tables)
            targets[role] = target
        if not migrate:
            return

        subprocess.run(["docker", "compose", "--profile", "legacy-postgres", "start", "--wait",
                        "--wait-timeout", "60", "user-db", "familycart-db"], check=True)
        upgrade_local_databases()
        with ExitStack() as snapshots:
            sources = {}
            for role, (_, dbname, user, port, tables) in STORES.items():
                source = snapshots.enter_context(psycopg.connect(host="127.0.0.1", port=port,
                    dbname=dbname, user=user, password="familycart_local_password", connect_timeout=5))
                source.execute("SET LOCAL lock_timeout = '10s'")
                source.execute(sql.SQL("LOCK TABLE {} IN SHARE MODE").format(names(tables)))
                sources[role] = source
            with closing(sqlite3.connect(":memory:")) as reference:
                seed_reference(reference)
                for role, (_, _, _, _, tables) in STORES.items():
                    copy_schema(sources[role], targets[role], role, tables, reference)
            # Both stores passed verification. A retry also accepts previously committed identical rows.
            for target in targets.values():
                target.commit()
    print("Migration committed. Both databases' source rows were verified.")


if __name__ == "__main__":
    try:
        run(sys.argv[1:] == ["migrate"])
    except psycopg.Error as exc:
        # Database error details may include customer records. Connection diagnostics contain no row data.
        message = str(exc) if isinstance(exc, psycopg.OperationalError) and not exc.sqlstate else exc.diag.message_primary
        sys.exit(f"Database check failed: {message or type(exc).__name__}")
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
