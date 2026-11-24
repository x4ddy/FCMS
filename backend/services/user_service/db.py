import os
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from backend.shared.database import DatabaseConnection, DatabaseTarget, connect_database, is_postgres_target


def database_path() -> DatabaseTarget:
    database_url = os.environ.get("FAMILYCART_USER_DATABASE_URL")
    if database_url:
        return database_url
    configured = os.environ.get("FAMILYCART_USER_DB_PATH") or os.environ.get("FAMILYCART_AUTH_DB_PATH")
    if configured:
        return Path(configured)
    cart_database = os.environ.get("FAMILYCART_DB_PATH")
    if cart_database:
        return Path(cart_database).with_name("familycart-users.sqlite3")
    return Path("familycart-users.sqlite3")


def connect(path: DatabaseTarget) -> DatabaseConnection:
    return connect_database(path)


def initialize_database(path: DatabaseTarget) -> None:
    if is_postgres_target(path):
        schema = (Path(__file__).parent / "postgres_schema.sql").read_text(encoding="utf-8")
        with closing(connect(path)) as database:
            database.execute_script(schema)
            database.commit()
        return

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(path)) as database, database:
        database.execute("PRAGMA journal_mode = WAL")
        database.execute_script(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                email TEXT UNIQUE,
                google_subject TEXT UNIQUE,
                display_name TEXT NOT NULL,
                is_adult INTEGER NOT NULL DEFAULT 0 CHECK (is_adult IN (0, 1)),
                status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'LOCKED', 'DISABLED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS refresh_sessions (
                session_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL REFERENCES users(user_id),
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                absolute_expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used_at TEXT NOT NULL,
                revoked_at TEXT,
                user_agent TEXT,
                ip_address TEXT
            );

            CREATE INDEX IF NOT EXISTS refresh_sessions_user_idx
                ON refresh_sessions(user_id, revoked_at);

            CREATE TABLE IF NOT EXISTS refresh_token_history (
                session_id TEXT NOT NULL REFERENCES refresh_sessions(session_id),
                token_hash TEXT NOT NULL,
                rotated_at TEXT NOT NULL,
                PRIMARY KEY (session_id, token_hash)
            );
            """
        )
        columns = {row["name"] for row in database.execute("PRAGMA table_info(users)").fetchall()}
        if "email" not in columns:
            database.execute("ALTER TABLE users ADD COLUMN email TEXT")
            database.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_idx ON users(email)")
        if "google_subject" not in columns:
            database.execute("ALTER TABLE users ADD COLUMN google_subject TEXT")
            database.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS users_google_subject_idx ON users(google_subject)"
            )
        if "date_of_birth" not in columns:
            database.execute("ALTER TABLE users ADD COLUMN date_of_birth TEXT")


@contextmanager
def transaction(path: DatabaseTarget) -> Iterator[DatabaseConnection]:
    with closing(connect(path)) as database:
        if database.dialect == "sqlite":
            database.execute("BEGIN IMMEDIATE")
        # psycopg opens the PostgreSQL transaction itself on the first statement.
        try:
            yield database
        except Exception:
            database.rollback()
            raise
        else:
            database.commit()


def upsert_user(
    database: DatabaseConnection,
    user_id: str,
    display_name: str,
    is_adult: bool,
    now: str,
    email: str | None = None,
    google_subject: str | None = None,
    date_of_birth: str | None = None,
):
    return database.execute(
        """INSERT INTO users
           (user_id, email, google_subject, display_name, is_adult, status, created_at, updated_at, date_of_birth)
           VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET
               display_name = excluded.display_name,
               is_adult = CASE WHEN users.date_of_birth IS NULL THEN excluded.is_adult ELSE users.is_adult END,
               updated_at = excluded.updated_at,
               email = COALESCE(excluded.email, users.email),
               google_subject = COALESCE(excluded.google_subject, users.google_subject),
               date_of_birth = COALESCE(users.date_of_birth, excluded.date_of_birth)
           RETURNING is_adult, google_subject IS NOT NULL AND date_of_birth IS NULL AS age_setup_required""",
        (user_id, email, google_subject, display_name, int(is_adult), now, now, date_of_birth),
    ).fetchone()
