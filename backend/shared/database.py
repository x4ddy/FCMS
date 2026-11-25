import atexit
import logging
import os
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, TypeAlias

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
except ImportError:  # SQLite-only unit tests can run before optional production dependencies are installed.
    psycopg = None
    dict_row = None
    ConnectionPool = None


DatabaseTarget: TypeAlias = Path | str
logger = logging.getLogger(__name__)


def is_postgres_target(target: DatabaseTarget) -> bool:
    return isinstance(target, str) and target.startswith(("postgresql://", "postgres://"))


def uses_supabase_schema(target: DatabaseTarget, schema: str) -> bool:
    if os.environ.get("FAMILYCART_DB_MODE", "local") != "supabase":
        return False
    if not is_postgres_target(target):
        raise RuntimeError("Supabase mode requires both PostgreSQL connection URLs.")
    # The migration owns DDL. Check the service login and its search path before
    # allowing any unqualified application queries on a shared database.
    with closing(connect_database(target)) as db:
        row = db.execute("SELECT current_user AS login, current_schema() AS namespace").fetchone()
    if row["login"] != schema or row["namespace"] != schema:
        raise RuntimeError(f"Supabase connection must use the {schema} role and schema.")
    return True


def _postgres_query(query: str) -> str:
    # Application SQL uses DB-API qmark parameters. PostgreSQL's driver uses %s.
    return query.replace("?", "%s")


class DatabaseConnection:
    def __init__(self, raw: Any, dialect: str, release: Callable[[Any], None] | None = None):
        self.raw = raw
        self.dialect = dialect
        self._release = release
        # Callbacks that must only run once the current transaction is durable,
        # e.g. telling the live-update worker that a household changed.
        self.after_commit: list[Callable[[], None]] = []

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
        callbacks, self.after_commit = self.after_commit, []
        for callback in callbacks:
            try:
                callback()
            except Exception:  # A notification must never turn a committed write into an error.
                logger.exception("After-commit callback failed")

    def rollback(self) -> None:
        self.raw.rollback()
        self.after_commit = []

    def close(self) -> None:
        if self._release is not None:
            # Pooled PostgreSQL connection: hand it back instead of closing it.
            # Closing without commit always meant "discard"; do that ourselves so
            # read-only callers do not trigger the pool's rollback warning.
            release, self._release = self._release, None
            try:
                if self.raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    self.raw.rollback()
            except psycopg.Error:
                pass  # a broken connection: the pool notices and replaces it
            self.after_commit = []
            release(self.raw)
        else:
            self.raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback) -> None:
        if exception_type is None:
            self.commit()
        else:
            self.rollback()


# ---------------------------------------------------------------------------
# PostgreSQL connection pools: one per database URL per process. Opening a
# connection (network handshake, password check, a new server process) costs
# far more than a typical query, so connections are opened once and reused.
# ---------------------------------------------------------------------------
_pools: dict[str, Any] = {}
_pools_lock = threading.Lock()


def _pool_for(url: str):
    pool = _pools.get(url)
    if pool is not None:
        return pool
    with _pools_lock:
        pool = _pools.get(url)
        if pool is None:
            pool = ConnectionPool(
                url,
                min_size=int(os.environ.get("FAMILYCART_DB_POOL_MIN", "1")),
                max_size=int(os.environ.get("FAMILYCART_DB_POOL_MAX", "10")),
                # How long a request waits for a free connection before failing clearly.
                timeout=float(os.environ.get("FAMILYCART_DB_POOL_TIMEOUT", "10")),
                # Recycle connections periodically and drop ones that went stale.
                max_lifetime=1800,
                max_idle=300,
                check=ConnectionPool.check_connection,
                # Give up on a dead database quickly and let the next request try again.
                # The default keeps doubling its delay for five minutes, so a service
                # could stay broken long after the database came back.
                reconnect_timeout=5,
                kwargs={"row_factory": dict_row, "connect_timeout": 3},
                name=f"familycart-{len(_pools) + 1}",
                open=True,
            )
            _pools[url] = pool
        return pool


def close_pools() -> None:
    with _pools_lock:
        pools = list(_pools.values())
        _pools.clear()
    for pool in pools:
        pool.close()


atexit.register(close_pools)


def connect_database(target: DatabaseTarget) -> DatabaseConnection:
    if is_postgres_target(target):
        if psycopg is None or ConnectionPool is None:
            raise RuntimeError("PostgreSQL requires: python -m pip install -r backend/requirements.txt")
        pool = _pool_for(str(target))
        return DatabaseConnection(pool.getconn(), "postgresql", release=pool.putconn)

    # SQLite is a local file with no network or login, so connections are cheap
    # and pooling would add nothing.
    database = sqlite3.connect(Path(target), timeout=10)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    return DatabaseConnection(database, "sqlite")


def database_error_types() -> tuple[type[BaseException], ...]:
    errors: tuple[type[BaseException], ...] = (sqlite3.OperationalError,)
    if psycopg is not None:
        errors += (psycopg.Error,)
    return errors
