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

import logging
import os
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


def async_client(url: str) -> AsyncRedis:
    return AsyncRedis.from_url(url, **_OPTIONS)


def session_key(session_id: str) -> str:
    return f"{CACHE_PREFIX}:session:{session_id}"


def forget_session(session_id: str) -> None:
    """Drop a cached "session is active" answer so a logout takes effect immediately."""
    if not cache_breaker.available():
        return
    try:
        sync_client(cache_redis_url()).delete(session_key(session_id))
    except RedisError as error:
        cache_breaker.failed(error)


__all__ = [
    "Breaker", "CACHE_PREFIX", "RedisError", "async_client", "cache_breaker", "cache_redis_url",
    "forget_session", "live_redis_url", "session_cache_seconds", "session_key", "sync_client",
]
