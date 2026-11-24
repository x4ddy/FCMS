"""Login-session validation for the Gateway.

    memory (5 s, NORMAL routes only) -> Redis -> user database

The database is authoritative. Redis holds two kinds of entry per session:
"active" (short-lived, written only if no entry exists) and "revoked" (written
by logout). Write-if-absent means a slow check can never undo a logout.

A Redis error is never read as "logged out": the database decides. If no
source can answer, the request is refused with AuthUnavailable (HTTP 503).
"""

import asyncio
import hashlib
import logging
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from backend.services.refresh_service.service import SessionStatus, recently_revoked, session_status
from backend.shared.cache import (
    REVOCATION_CHANNEL, REVOKED_SECONDS, REVOKED_VALUE, RedisError, session_cache_seconds, session_hash, session_key,
)
from backend.shared.tokens import Identity

from .route_policy import Level

logger = logging.getLogger(__name__)

LOCAL_TTL = 5.0
MAX_LOCAL_ENTRIES = 10_000
DB_CONCURRENCY = 5
DB_MAX_WAITING = 50
DB_TIMEOUT = 2.0
RECOVERY_RAMP = 5.0
# Longer than any "active" entry can live in Redis, so recovery covers every stale one.
RECONCILE_SECONDS = 120
REDIS_FAILURES = (RedisError, OSError, asyncio.TimeoutError)


class SessionEnded(Exception):
    """The session is definitely not active."""


class AuthUnavailable(Exception):
    """Nothing could say whether the session is active."""


def _binding(identity: Identity) -> str:
    return hashlib.sha256(f"{identity.session_id}:{identity.user_id}".encode()).hexdigest()


class LocalSessionCache:
    """Bounded memory of recent "active" answers. Stores hashes and times only."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, limit: int = MAX_LOCAL_ENTRIES) -> None:
        self.clock = clock
        self.limit = limit
        self._active: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._revoked: OrderedDict[str, float] = OrderedDict()

    def __len__(self) -> int:
        return len(self._active)

    def get(self, key: str, binding: str) -> bool:
        entry = self._active.get(key)
        if entry is None:
            return False
        if entry[1] <= self.clock():
            del self._active[key]
            return False
        return entry[0] == binding

    def put(self, key: str, binding: str, expires: float) -> None:
        # A check that was running while logout arrived must not be remembered.
        if self.is_revoked(key) or expires <= self.clock():
            return
        self._active[key] = (binding, expires)
        self._active.move_to_end(key)
        while len(self._active) > self.limit:
            self._active.popitem(last=False)

    def revoke(self, key: str) -> None:
        self._active.pop(key, None)
        self._revoked[key] = self.clock() + REVOKED_SECONDS
        self._revoked.move_to_end(key)
        while len(self._revoked) > self.limit:
            self._revoked.popitem(last=False)

    def is_revoked(self, key: str) -> bool:
        until = self._revoked.get(key)
        return until is not None and until > self.clock()

    def clear_active(self) -> None:
        self._active.clear()

    def sweep(self) -> None:
        now = self.clock()
        for key in [key for key, (_, expires) in self._active.items() if expires <= now]:
            del self._active[key]
        for key in [key for key, until in self._revoked.items() if until <= now]:
            del self._revoked[key]


class SessionValidator:
    """One per Gateway process. ``redis`` answers checks, ``subscriber`` listens for logouts."""

    def __init__(
        self, db_path, redis, subscriber, *,
        status: Callable = session_status, revocations: Callable = recently_revoked,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.db_path = db_path
        self.redis = redis
        self.subscriber = subscriber
        self.status = status
        self.revocations = revocations
        self.clock = clock
        self.local = LocalSessionCache(clock)
        self.stats = {"local_hits": 0, "redis_checks": 0, "db_checks": 0, "unavailable": 0}
        self._ready_since: float | None = None
        self._inflight: dict[str, asyncio.Task] = {}
        self._sockets: dict[str, set[asyncio.Event]] = {}
        self._db_slots = asyncio.Semaphore(DB_CONCURRENCY)
        self._db_waiting = 0
        self._executor = ThreadPoolExecutor(DB_CONCURRENCY, thread_name_prefix="session-db")
        self._listener: asyncio.Task | None = None

    # -- lifecycle ------------------------------------------------------------
    def start(self) -> None:
        self._listener = asyncio.create_task(self._listen())

    async def close(self) -> None:
        if self._listener:
            self._listener.cancel()
            with suppress(asyncio.CancelledError):
                await self._listener
        for task in list(self._inflight.values()):
            task.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)
        await self.redis.aclose()
        await self.subscriber.aclose()

    # -- Redis health -----------------------------------------------------------
    @property
    def redis_ready(self) -> bool:
        return self._ready_since is not None

    def _redis_up(self) -> None:
        if self._ready_since is None:
            logger.info("Session checks are using Redis")
            self._ready_since = self.clock()

    def _redis_down(self, error: BaseException) -> None:
        if self._ready_since is not None:
            logger.warning("Session checks are using the database: Redis %s", type(error).__name__)
        self._ready_since = None

    def _use_redis(self) -> bool:
        """False during an outage. After recovery, traffic returns over a few seconds."""
        if self._ready_since is None or session_cache_seconds() == 0:
            return False
        elapsed = self.clock() - self._ready_since
        return elapsed >= RECOVERY_RAMP or random.random() < elapsed / RECOVERY_RAMP

    # -- logout messages ----------------------------------------------------------
    def on_revoked(self, key: str) -> None:
        self.local.revoke(key)
        for event in self._sockets.get(key, ()):
            event.set()

    async def _listen(self) -> None:
        delay = 1.0
        while True:
            try:
                async with self.subscriber.pubsub() as pubsub:
                    await pubsub.subscribe(REVOCATION_CHANNEL)
                    confirmed = await pubsub.get_message(timeout=5)
                    if not confirmed or confirmed["type"] != "subscribe":
                        raise RedisError("Subscription was not confirmed")
                    # Messages sent before now were missed, so forget what memory
                    # says and repair Redis before trusting it again.
                    self.local.clear_active()
                    await self._reconcile()
                    self._redis_up()
                    delay = 1.0
                    while self.redis_ready:
                        message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                        if message and message["type"] == "message":
                            self.on_revoked(str(message["data"]))
            except (*REDIS_FAILURES, AuthUnavailable) as error:
                self._redis_down(error)
            except Exception as error:  # the listener must survive anything
                logger.error("Logout listener restarting after %s", type(error).__name__)
                self._redis_down(error)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)

    async def _reconcile(self) -> None:
        """Copy recent logouts from the database into Redis and memory."""
        session_ids = await self._in_database(self.revocations, self.db_path, RECONCILE_SECONDS)
        for session_id in session_ids:
            self.on_revoked(session_hash(session_id))
            await self.redis.set(session_key(session_id), REVOKED_VALUE, ex=REVOKED_SECONDS)

    # -- database, protected --------------------------------------------------------
    async def _in_database(self, function: Callable, *args):
        if self._db_waiting >= DB_MAX_WAITING:
            raise AuthUnavailable
        self._db_waiting += 1
        try:
            await asyncio.wait_for(self._db_slots.acquire(), DB_TIMEOUT)
        except asyncio.TimeoutError:
            raise AuthUnavailable from None
        finally:
            self._db_waiting -= 1
        try:
            future = asyncio.get_running_loop().run_in_executor(self._executor, function, *args)
        except RuntimeError:
            self._db_slots.release()
            raise AuthUnavailable from None
        # The slot is returned when the thread really finishes, not when we stop waiting.
        future.add_done_callback(self._release_slot)
        try:
            return await asyncio.wait_for(asyncio.shield(future), DB_TIMEOUT)
        except asyncio.TimeoutError:
            raise AuthUnavailable from None
        except Exception as error:
            logger.warning("Session database check failed: %s", type(error).__name__)
            raise AuthUnavailable from None

    def _release_slot(self, future: asyncio.Future) -> None:
        self._db_slots.release()
        if not future.cancelled():
            future.exception()  # mark as seen even if the caller stopped waiting

    async def _from_database(self, identity: Identity) -> float:
        """Return the session's end time, or raise."""
        self.stats["db_checks"] += 1
        result: SessionStatus = await self._in_database(
            self.status, self.db_path, identity.session_id, identity.user_id,
        )
        if result.active:
            return result.ends_at
        if result.revoked:
            key = session_hash(identity.session_id)
            self.on_revoked(key)
            if self.redis_ready:
                try:
                    await self.redis.set(session_key(identity.session_id), REVOKED_VALUE, ex=REVOKED_SECONDS)
                except REDIS_FAILURES as error:
                    self._redis_down(error)
        raise SessionEnded

    # -- Redis, then database ---------------------------------------------------------
    def _read_entry(self, value: str | None, identity: Identity) -> float | None:
        if value == REVOKED_VALUE:
            self.on_revoked(session_hash(identity.session_id))
            raise SessionEnded
        try:
            state, binding, ends_at = (value or "").split(":")
            if state == "active" and binding == _binding(identity) and float(ends_at) > time.time():
                return float(ends_at)
        except ValueError:
            pass
        return None  # missing, for someone else, or unreadable: let the database decide

    async def _lookup(self, identity: Identity) -> float:
        key = session_key(identity.session_id)
        use_redis = self._use_redis()
        if use_redis:
            self.stats["redis_checks"] += 1
            try:
                value = await self.redis.get(key)
            except REDIS_FAILURES as error:
                self._redis_down(error)
                use_redis = False
            else:
                ends_at = self._read_entry(value, identity)
                if ends_at is not None:
                    return ends_at

        ends_at = await self._from_database(identity)
        ttl = min(session_cache_seconds(), int(ends_at - time.time()))
        if use_redis and ttl > 0:
            try:
                stored = await self.redis.set(
                    key, f"active:{_binding(identity)}:{ends_at}", ex=ttl, nx=True,
                )
                if not stored and await self.redis.get(key) == REVOKED_VALUE:
                    # Logout finished while we were asking the database.
                    self.on_revoked(session_hash(identity.session_id))
                    raise SessionEnded
            except REDIS_FAILURES as error:
                self._redis_down(error)
        return ends_at

    async def _timed_lookup(self, identity: Identity) -> tuple[float, float]:
        started = self.clock()
        return started, await self._lookup(identity)

    async def _shared_lookup(self, identity: Identity) -> tuple[float, float]:
        """Combine simultaneous NORMAL lookups of one session into one. Returns (started, ends_at)."""
        key = _binding(identity)
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._timed_lookup(identity))
            self._inflight[key] = task

            def finished(done: asyncio.Task) -> None:
                self._inflight.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def validate(self, identity: Identity, level: Level) -> None:
        if not identity.session_id:
            raise SessionEnded
        key, binding = session_hash(identity.session_id), _binding(identity)
        if self.local.is_revoked(key):
            raise SessionEnded
        local_ttl = min(LOCAL_TTL, session_cache_seconds())
        if level is Level.NORMAL and self.local.get(key, binding):
            self.stats["local_hits"] += 1
            return
        # The 5 seconds count from the start of the check, not its end.
        started = 0.0
        try:
            if level is Level.CRITICAL:
                ends_at = await self._from_database(identity)
            elif level is Level.SENSITIVE:
                # Never joins another check: that one may have started before a logout.
                ends_at = await self._lookup(identity)
            else:
                started, ends_at = await self._shared_lookup(identity)
        except AuthUnavailable:
            self.stats["unavailable"] += 1
            raise
        if self.local.is_revoked(key):
            raise SessionEnded
        remaining = ends_at - time.time()
        if remaining <= 0:
            raise SessionEnded
        if level is Level.NORMAL:
            self.local.put(key, binding, started + min(local_ttl, remaining))

    # -- WebSockets -----------------------------------------------------------------
    def watch(self, identity: Identity) -> asyncio.Event:
        event = asyncio.Event()
        self._sockets.setdefault(session_hash(identity.session_id), set()).add(event)
        return event

    def unwatch(self, identity: Identity, event: asyncio.Event) -> None:
        key = session_hash(identity.session_id)
        watchers = self._sockets.get(key)
        if watchers:
            watchers.discard(event)
            if not watchers:
                del self._sockets[key]

    async def recheck_socket(self, identity: Identity) -> None:
        """Catch a missed logout message. Reads only the Redis marker, so idle screens cost no database work."""
        if self.redis_ready:
            try:
                if await self.redis.get(session_key(identity.session_id)) == REVOKED_VALUE:
                    self.on_revoked(session_hash(identity.session_id))
                    raise SessionEnded
                return
            except REDIS_FAILURES as error:
                self._redis_down(error)
        await self._from_database(identity)
