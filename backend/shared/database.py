import sqlite3
from pathlib import Path
from typing import Any, TypeAlias

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # SQLite-only unit tests can run before optional production dependencies are installed.
    psycopg = None
    dict_row = None


DatabaseTarget: TypeAlias = Path | str


def is_postgres_target(target: DatabaseTarget) -> bool:
    return isinstance(target, str) and target.startswith(("postgresql://", "postgres://"))


def _postgres_query(query: str) -> str:
    # Application SQL uses DB-API qmark parameters. PostgreSQL's driver uses %s.
    return query.replace("?", "%s")


class DatabaseConnection:
    def __init__(self, raw: Any, dialect: str):
        self.raw = raw
        self.dialect = dialect

    def execute(self, query: str, parameters: Any = None):
        sql = _postgres_query(query) if self.dialect == "postgresql" else query
        return self.raw.execute(sql, parameters or ())

    def execute_script(self, script: str) -> None:
        if self.dialect == "postgresql":
            self.raw.execute(script)
        else:
            self.raw.executescript(script)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        if exception_type is None:
            self.commit()
        else:
            self.rollback()


def connect_database(target: DatabaseTarget) -> DatabaseConnection:
    if is_postgres_target(target):
        if psycopg is None:
            raise RuntimeError("PostgreSQL requires: python -m pip install -r backend/requirements.txt")
        return DatabaseConnection(psycopg.connect(str(target), row_factory=dict_row), "postgresql")

    database = sqlite3.connect(Path(target), timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return DatabaseConnection(database, "sqlite")


def database_error_types() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (sqlite3.OperationalError,)
    if psycopg is not None:
        errors += (psycopg.Error,)
    return errors
