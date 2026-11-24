"""Shared Redis access for caches and live-update bookkeeping.

All features currently use one Redis instance, but each purpose has its own
setting so they can be split later by changing configuration only:

    FAMILYCART_REDIS_URL        live updates (Pub/Sub, changed-household notes)
    FAMILYCART_CACHE_REDIS_URL  caches (session checks, who is online);
                                falls back to FAMILYCART_REDIS_URL

Redis is an accelerator here, never the source of truth. Every caller has a
database fallback, and a circuit breaker stops a Redis outage from adding a
connection timeout to every request.
"""

import hashlib
import logging
import os
import queue
import threading
import time

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/0"
CACHE_PREFIX = "familycart:cache"
_OPTIONS = {"decode_responses": True, "socket_connect_timeout": 0.5, "socket_timeout": 1}


def live_redis_url() -> str:
    return os.environ.get("FAMILYCART_REDIS_URL") or DEFAULT_REDIS_URL


def cache_redis_url() -> str:
    return os.environ.get("FAMILYCART_CACHE_REDIS_URL") or live_redis_url()


def session_cache_seconds() -> int:
    return max(0, int(os.environ.get("FAMILYCART_SESSION_CACHE_SECONDS", "30")))


class Breaker:
    """Skip Redis for a cool-down period after it fails, instead of timing out every call."""

    def __init__(self, name: str, cool_down: float = 15) -> None:
        self.name = name
        self.cool_down = cool_down
        self._open_until = 0.0
        self._lock = threading.Lock()

    def available(self) -> bool:
        return time.monotonic() >= self._open_until

    def failed(self, error: Exception) -> None:
        with self._lock:
            if self.available():
                logger.warning("Redis (%s) unavailable, using fallback for %ss: %s", self.name, self.cool_down, error)
            self._open_until = time.monotonic() + self.cool_down


_sync_clients: dict[str, Redis] = {}
_clients_lock = threading.Lock()
cache_breaker = Breaker("cache")


def sync_client(url: str) -> Redis:
    client = _sync_clients.get(url)
    if client is None:
        with _clients_lock:
            client = _sync_clients.setdefault(url, Redis.from_url(url, **_OPTIONS))
    return client


def async_client(url: str, **options) -> AsyncRedis:
    return AsyncRedis.from_url(url, **{**_OPTIONS, **options})


# ---------------------------------------------------------------------------
# Login sessions. The Gateway stores "active" answers; logout stores "revoked".
# ---------------------------------------------------------------------------
REVOKED_VALUE = "revoked"
# Longer than the 10-minute access token, so a late "active" write cannot replace it.
REVOKED_SECONDS = 900
REVOCATION_CHANNEL = f"{CACHE_PREFIX}:session-revoked"
# Every "active" entry expires within the session cache time, so retrying beyond it is pointless.
_RETRY_SECONDS = 60
_retries: queue.Queue[tuple[str, float]] = queue.Queue(maxsize=10_000)
_retry_lock = threading.Lock()
_retry_thread: threading.Thread | None = None


def session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


def session_key(session_id: str) -> str:
    return f"{CACHE_PREFIX}:session:v2:{session_hash(session_id)}"


def _write_revocation(session_id: str) -> None:
    with sync_client(cache_redis_url()).pipeline(transaction=True) as pipe:
        # Replace rather than delete: a deleted entry could be re-created as
        # "active" by a check that read the database just before the logout.
        pipe.set(session_key(session_id), REVOKED_VALUE, ex=REVOKED_SECONDS)
        pipe.publish(REVOCATION_CHANNEL, session_hash(session_id))
        pipe.execute()


def announce_revocation(session_id: str) -> None:
    """Tell Redis and every Gateway that a session ended. Call after the database commit.

    Always tries, whatever the circuit breaker says. If Redis cannot be reached the
    logout still stands (the database is authoritative) and this is retried.
    """
    try:
        _write_revocation(session_id)
    except (RedisError, OSError) as error:
        logger.warning("Logout saved; telling Redis will be retried: %s", type(error).__name__)
        try:
            _retries.put_nowait((session_id, time.monotonic() + _RETRY_SECONDS))
        except queue.Full:
            logger.error("Too many logouts are waiting for Redis; one will rely on cache expiry")
            return
        _start_retry_thread()


def _start_retry_thread() -> None:
    global _retry_thread
    with _retry_lock:
        if _retry_thread is None or not _retry_thread.is_alive():
            _retry_thread = threading.Thread(target=_retry_loop, name="logout-retry", daemon=True)
            _retry_thread.start()


def _retry_loop() -> None:
    while True:
        session_id, deadline = _retries.get()
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                _write_revocation(session_id)
                break
            except (RedisError, OSError):
                continue


__all__ = [
    "Breaker", "CACHE_PREFIX", "REVOCATION_CHANNEL", "REVOKED_SECONDS", "REVOKED_VALUE", "RedisError",
    "announce_revocation", "async_client", "cache_breaker", "cache_redis_url", "live_redis_url",
    "session_cache_seconds", "session_hash", "session_key", "sync_client",
]
