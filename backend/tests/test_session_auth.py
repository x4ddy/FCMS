import asyncio
import os
import secrets
import sqlite3
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from starlette.websockets import WebSocketDisconnect

import backend.api_gateway.app as gateway
import backend.shared.cache as cache
from backend.api_gateway.route_policy import Level, classify, is_ambiguous
from backend.api_gateway.session_auth import (
    DB_CONCURRENCY, AuthUnavailable, LocalSessionCache, SessionEnded, SessionValidator, _binding,
)
from backend.services.refresh_service.app import app as refresh_app
from backend.services.refresh_service.cookies import REFRESH_COOKIE
from backend.services.refresh_service.service import (
    create_session, recently_revoked, revoke_session, rotate_session, session_status,
)
from backend.services.user_service.db import initialize_database, transaction
from backend.shared.tokens import Identity, verify_token


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeRedis:
    """Shared by several validators, like one real Redis."""

    def __init__(self, clock) -> None:
        self.clock, self.data, self.down, self.reads = clock, {}, None, 0
        self.listeners: list[asyncio.Queue] = []
        self.deliver = True

    def _check(self):
        if self.down:
            raise self.down

    async def get(self, key):
        self._check()
        self.reads += 1
        value, until = self.data.get(key, (None, 0))
        return value if until > self.clock() else None

    async def set(self, key, value, ex=None, nx=False):
        self._check()
        if nx and await self.get(key) is not None:
            return None
        self.data[key] = (value, self.clock() + ex)
        return True

    def logout(self, session_id):
        """What announce_revocation() does against the real Redis."""
        self._check()
        self.data[cache.session_key(session_id)] = (cache.REVOKED_VALUE, self.clock() + cache.REVOKED_SECONDS)
        if self.deliver:
            for queue in self.listeners:
                queue.put_nowait({"type": "message", "data": cache.session_hash(session_id)})

    async def aclose(self):
        pass

    def pubsub(self):
        fake = self

        class PubSub:
            async def __aenter__(self):
                fake._check()
                self.queue = asyncio.Queue()
                return self

            async def __aexit__(self, *exc):
                if self.queue in fake.listeners:
                    fake.listeners.remove(self.queue)

            async def subscribe(self, channel):
                fake._check()
                fake.listeners.append(self.queue)
                self.queue.put_nowait({"type": "subscribe"})

            async def get_message(self, ignore_subscribe_messages=False, timeout=0):
                fake._check()
                try:
                    return await asyncio.wait_for(self.queue.get(), 0.02)
                except asyncio.TimeoutError:
                    return None

        return PubSub()


class ValidatorCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.db = Path(folder) / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        initialize_database(self.db)
        self.clock = Clock()
        self.redis = FakeRedis(self.clock)
        self.validators = []
        self.gateway = self.make()

    async def asyncTearDown(self) -> None:
        for validator in self.validators:
            await validator.close()

    def make(self, *, ready=True, **options) -> SessionValidator:
        validator = SessionValidator(self.db, self.redis, self.redis, clock=self.clock, **options)
        if ready:
            validator._ready_since = self.clock() - 100
        self.validators.append(validator)
        return validator

    def login(self, user="user-1") -> Identity:
        tokens = create_session(self.db, self.secret, Identity(user, True), "Test User")
        return verify_token(tokens.access_token, self.secret)

    def logout(self, identity, *, redis=True) -> None:
        with patch("backend.services.refresh_service.service.announce_revocation") as announce:
            if redis:
                announce.side_effect = self.redis.logout
            revoke_session(self.db, identity.session_id)

    async def accepted(self, identity, level=Level.NORMAL, on=None) -> bool:
        try:
            await (on or self.gateway).validate(identity, level)
            return True
        except SessionEnded:
            return False

    async def until(self, condition, seconds=3):
        end = time.monotonic() + seconds
        while not condition():
            self.assertLess(time.monotonic(), end, "condition not reached")
            await asyncio.sleep(0.02)


class Scenarios(ValidatorCase):
    async def test_01_first_request_without_local_cache_uses_database_and_fills_caches(self):
        identity = self.login()
        self.assertTrue(await self.accepted(identity))
        self.assertEqual((self.gateway.stats["redis_checks"], self.gateway.stats["db_checks"]), (1, 1))
        self.assertEqual(len(self.gateway.local), 1)
        self.assertIsNotNone(await self.redis.get(cache.session_key(identity.session_id)))

    async def test_02_local_hit_needs_no_redis_or_database(self):
        identity = self.login()
        await self.accepted(identity)
        before = dict(self.gateway.stats)
        for _ in range(20):
            self.assertTrue(await self.accepted(identity))
        self.assertEqual(self.gateway.stats["local_hits"], 20)
        self.assertEqual(self.gateway.stats["redis_checks"], before["redis_checks"])
        self.assertEqual(self.gateway.stats["db_checks"], before["db_checks"])

    async def test_03_local_entry_expires_after_five_seconds(self):
        identity = self.login()
        await self.accepted(identity)
        self.clock.now += 4.9
        await self.accepted(identity)
        self.assertEqual(self.gateway.stats["redis_checks"], 1)
        self.clock.now += 0.2
        await self.accepted(identity)
        self.assertEqual(self.gateway.stats["redis_checks"], 2)
        self.assertEqual(self.gateway.stats["db_checks"], 1, "Redis answered the second time")

    async def test_04_redis_miss_with_valid_database_session(self):
        identity = self.login()
        self.assertTrue(await self.accepted(identity, Level.SENSITIVE))
        self.assertEqual(self.gateway.stats["db_checks"], 1)

    async def test_05_redis_miss_with_invalid_database_session(self):
        identity = self.login()
        self.logout(identity, redis=False)
        self.assertFalse(await self.accepted(identity))
        self.assertEqual(await self.redis.get(cache.session_key(identity.session_id)), cache.REVOKED_VALUE)
        unknown = Identity("user-1", True, "00000000-0000-4000-8000-000000000000")
        self.assertFalse(await self.accepted(unknown))

    async def test_06_sensitive_bypasses_local_and_critical_bypasses_redis_too(self):
        identity = self.login()
        await self.accepted(identity)
        reads = self.gateway.stats["redis_checks"]
        await self.accepted(identity, Level.SENSITIVE)
        self.assertEqual(self.gateway.stats["redis_checks"], reads + 1)
        database = self.gateway.stats["db_checks"]
        await self.accepted(identity, Level.CRITICAL)
        self.assertEqual(self.gateway.stats["redis_checks"], reads + 1)
        self.assertEqual(self.gateway.stats["db_checks"], database + 1)

    async def test_06b_critical_refuses_a_logout_that_redis_never_heard_about(self):
        identity = self.login()
        await self.accepted(identity)
        self.logout(identity, redis=False)
        self.assertTrue(await self.accepted(identity, Level.SENSITIVE), "the documented Redis gap")
        self.assertFalse(await self.accepted(identity, Level.CRITICAL))
        self.assertFalse(await self.accepted(identity), "and the Gateway now knows")

    async def test_07_and_08_logout_reaches_every_gateway_with_a_cached_session(self):
        second = self.make(ready=False)
        self.gateway._ready_since = None
        for validator in (self.gateway, second):
            validator.start()
        await self.until(lambda: self.gateway.redis_ready and second.redis_ready)
        identity = self.login()
        self.assertTrue(await self.accepted(identity))
        self.assertTrue(await self.accepted(identity, on=second))
        self.logout(identity)
        await self.until(lambda: not len(self.gateway.local) and not len(second.local))
        self.assertFalse(await self.accepted(identity))
        self.assertFalse(await self.accepted(identity, on=second))

    async def test_09_missed_message_is_bounded_by_the_local_lifetime(self):
        identity = self.login()
        await self.accepted(identity)
        self.redis.deliver = False
        self.logout(identity)
        self.assertTrue(await self.accepted(identity), "still in memory, as documented")
        self.assertFalse(await self.accepted(identity, Level.SENSITIVE), "sensitive routes see it at once")
        other = self.make()
        identity2 = self.login()
        await self.accepted(identity2, on=other)
        self.logout(identity2)
        self.clock.now += 5.1
        self.assertFalse(await self.accepted(identity2, on=other))

    async def test_10_redis_errors_fall_back_to_the_database_and_are_not_logouts(self):
        for error in (RedisConnectionError("down"), RedisTimeoutError("slow")):
            with self.subTest(error=type(error).__name__):
                validator = self.make()
                identity, ended = self.login(), self.login()
                self.logout(ended, redis=False)
                self.redis.down = error
                self.assertTrue(await self.accepted(identity, Level.SENSITIVE, validator))
                self.assertFalse(validator.redis_ready)
                self.assertFalse(await self.accepted(ended, Level.SENSITIVE, validator))
                self.redis.down = None

    async def test_11_both_down_fails_closed(self):
        def broken(*_):
            raise sqlite3.OperationalError("down")

        validator = self.make(status=broken)
        identity = self.login()
        self.redis.down = RedisConnectionError("down")
        for level in Level:
            with self.assertRaises(AuthUnavailable):
                await validator.validate(identity, level)

    async def test_11b_only_database_down_redis_still_serves_but_not_critical(self):
        identity = self.login()
        await self.accepted(identity)

        def broken(*_):
            raise sqlite3.OperationalError("down")

        self.gateway.status = broken
        self.assertTrue(await self.accepted(identity, Level.SENSITIVE))
        with self.assertRaises(AuthUnavailable):
            await self.gateway.validate(identity, Level.CRITICAL)

    async def test_12_recovery_repairs_redis_before_trusting_it(self):
        self.gateway._ready_since = None
        self.gateway.start()
        await self.until(lambda: self.gateway.redis_ready)
        self.clock.now += 100
        identity = self.login()
        await self.accepted(identity)
        self.redis.down = RedisConnectionError("down")
        await self.until(lambda: not self.gateway.redis_ready)
        self.logout(identity, redis=False)  # Redis keeps its old "active" entry
        self.redis.down = None
        self.assertNotEqual(await self.redis.get(cache.session_key(identity.session_id)), cache.REVOKED_VALUE)
        await self.until(lambda: self.gateway.redis_ready, 8)
        self.assertEqual(await self.redis.get(cache.session_key(identity.session_id)), cache.REVOKED_VALUE)
        self.assertFalse(await self.accepted(identity, Level.SENSITIVE))

    async def test_12b_redis_traffic_returns_gradually(self):
        self.gateway._ready_since = self.clock()
        with patch("backend.api_gateway.session_auth.random.random", return_value=0.5):
            self.assertFalse(self.gateway._use_redis())
            self.clock.now += 3
            self.assertTrue(self.gateway._use_redis())
        self.clock.now += 10
        with patch("backend.api_gateway.session_auth.random.random", return_value=0.999):
            self.assertTrue(self.gateway._use_redis())

    async def test_13_logout_during_a_check_is_not_undone(self):
        """The race that the previous design lost, at every level."""
        for level in Level:
            with self.subTest(level=level.name):
                identity = self.login()
                validator = None

                def status_then_logout(*args):
                    result = session_status(*args)
                    self.logout(identity)
                    return result

                validator = self.make(status=status_then_logout)
                # The check itself overlapped the logout; afterwards nothing may be accepted.
                await self.accepted(identity, level, validator)
                validator.status = session_status
                for later in Level:
                    self.assertFalse(await self.accepted(identity, later, validator), later)
                self.assertEqual(await self.redis.get(cache.session_key(identity.session_id)), cache.REVOKED_VALUE)

    async def test_13b_message_arriving_mid_check_rejects_that_check(self):
        identity = self.login()
        entered, release = threading.Event(), threading.Event()

        def slow(*args):
            result = session_status(*args)
            entered.set()
            release.wait(2)
            return result

        validator = self.make(status=slow)
        task = asyncio.create_task(self.accepted(identity, Level.CRITICAL, validator))
        await self.until(entered.is_set)
        validator.on_revoked(cache.session_hash(identity.session_id))
        release.set()
        self.assertFalse(await task)

    async def test_13c_simultaneous_normal_requests_share_one_check_sensitive_do_not(self):
        calls = []

        def slow(*args):
            calls.append(1)
            time.sleep(0.1)
            return session_status(*args)

        validator = self.make(status=slow)
        identity = self.login()
        self.assertEqual(await asyncio.gather(*(self.accepted(identity, on=validator) for _ in range(30))), [True] * 30)
        self.assertEqual(len(calls), 1)
        await asyncio.gather(*(self.accepted(identity, Level.CRITICAL, validator) for _ in range(3)))
        self.assertEqual(len(calls), 4)
        self.redis.data.clear()
        await asyncio.gather(*(self.accepted(identity, Level.SENSITIVE, validator) for _ in range(3)))
        self.assertEqual(len(calls), 7)

    async def test_13d_joining_a_check_does_not_extend_its_lifetime(self):
        identity = self.login()
        entered, release = threading.Event(), threading.Event()

        def slow(*args):
            entered.set()
            release.wait(2)
            return session_status(*args)

        validator = self.make(status=slow)
        first = asyncio.create_task(self.accepted(identity, on=validator))
        await self.until(entered.is_set)
        self.clock.now += 3
        second = asyncio.create_task(self.accepted(identity, on=validator))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.gather(first, second)
        self.clock.now += 2.1  # 5.1 s after the check began
        self.assertFalse(validator.local.get(cache.session_hash(identity.session_id), _binding(identity)))

    async def test_14_expired_locked_and_other_users_sessions_are_refused(self):
        expired, locked, owner = self.login("a"), self.login("b"), self.login("c")
        with transaction(self.db) as database:
            database.execute("UPDATE refresh_sessions SET absolute_expires_at = '2020-01-01T00:00:00+00:00' WHERE session_id = ?",
                             (expired.session_id,))
            database.execute("UPDATE users SET status = 'LOCKED' WHERE user_id = 'b'")
        self.assertTrue(await self.accepted(owner))
        stranger = Identity("someone-else", True, owner.session_id)
        for identity in (expired, locked, stranger, Identity("c", True, None)):
            for level in Level:
                self.assertFalse(await self.accepted(identity, level))
        self.assertTrue(await self.accepted(owner), "the owner is unaffected by the stranger's attempt")

    async def test_rotation_keeps_the_session_and_reuse_ends_it(self):
        tokens = create_session(self.db, self.secret, Identity("u", True), "U")
        identity = verify_token(tokens.access_token, self.secret)
        await self.accepted(identity)
        rotated = rotate_session(self.db, self.secret, tokens.refresh_token)
        self.assertTrue(await self.accepted(verify_token(rotated.access_token, self.secret)))
        with patch("backend.services.refresh_service.service.announce_revocation", self.redis.logout):
            with self.assertRaises(ValueError):
                rotate_session(self.db, self.secret, tokens.refresh_token)
        self.assertFalse(await self.accepted(identity, Level.SENSITIVE))

    async def test_database_work_is_limited_and_a_hang_becomes_unavailable(self):
        running, peak, lock, release = 0, 0, threading.Lock(), threading.Event()

        def hang(*_):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            release.wait(10)
            with lock:
                running -= 1
            raise sqlite3.OperationalError

        validator = self.make(status=hang)
        with patch("backend.api_gateway.session_auth.DB_TIMEOUT", 0.2):
            results = await asyncio.gather(
                *(validator.validate(self.login(f"u{n}"), Level.CRITICAL) for n in range(70)), return_exceptions=True,
            )
        release.set()
        self.assertTrue(all(isinstance(result, AuthUnavailable) for result in results))
        self.assertLessEqual(peak, DB_CONCURRENCY)

    async def test_idle_socket_recheck_costs_no_database_work_and_finds_a_missed_logout(self):
        identity = self.login()
        await self.gateway.recheck_socket(identity)
        self.assertEqual(self.gateway.stats["db_checks"], 0)
        event = self.gateway.watch(identity)
        self.redis.deliver = False
        self.logout(identity)
        with self.assertRaises(SessionEnded):
            await self.gateway.recheck_socket(identity)
        self.assertTrue(event.is_set())
        self.gateway.unwatch(identity, event)
        self.assertEqual(self.gateway._sockets, {})


class LocalCacheTests(unittest.TestCase):
    def test_a_revoked_session_is_never_remembered_again(self):
        clock = Clock()
        local = LocalSessionCache(clock)
        local.revoke("k")
        local.put("k", "binding", clock() + 5)
        self.assertEqual(len(local), 0)

    def test_bounded_swept_and_holds_no_readable_identifiers(self):
        clock = Clock()
        local = LocalSessionCache(clock, limit=100)
        for n in range(1000):
            local.put(cache.session_hash(str(n)), "binding", clock() + 5)
            local.revoke(cache.session_hash(f"r{n}"))
        self.assertEqual((len(local._active), len(local._revoked)), (100, 100))
        clock.now += 6
        local.sweep()
        self.assertEqual((len(local._active), len(local._revoked)), (0, 100))
        clock.now += cache.REVOKED_SECONDS
        local.sweep()
        self.assertEqual(len(local._revoked), 0)


class AnnouncementTests(unittest.TestCase):
    def test_a_failed_announcement_is_retried_even_after_breaker_failures(self):
        attempts = []

        def write(session_id):
            attempts.append(session_id)
            if len(attempts) < 3:
                raise RedisConnectionError("down")

        cache.cache_breaker.failed(RedisConnectionError("earlier blip"))
        self.addCleanup(setattr, cache.cache_breaker, "_open_until", 0.0)
        with patch.object(cache, "_write_revocation", write), patch.object(cache.time, "sleep", lambda _: threading.Event().wait(0.01)):
            cache.announce_revocation("session-1")
            end = time.monotonic() + 3
            while len(attempts) < 3 and time.monotonic() < end:
                threading.Event().wait(0.02)
        self.assertEqual(attempts, ["session-1"] * 3)


class RoutePolicyTests(unittest.TestCase):
    def test_levels(self):
        h = "households/h1"
        for method, path in (("GET", "products"), ("GET", f"{h}/carts/c1"), ("PATCH", f"{h}/carts/c1/items/milk"),
                             ("GET", "orders"), ("POST", f"{h}/lists/l1/items/bulk")):
            self.assertIs(classify(method, path), Level.NORMAL, path)
        for method, path in (("POST", f"{h}/invitations"), ("PUT", f"{h}/carts/c1/grants/m1"), ("DELETE", f"{h}/carts/c1"),
                             ("POST", f"{h}/addresses"), ("PATCH", f"{h}/addresses/a1"), ("DELETE", f"{h}/lists/l1"),
                             ("POST", "checkout/quotes"), ("GET", "something/new"), ("GET", "auth/age")):
            self.assertIs(classify(method, path), Level.SENSITIVE, path)
        self.assertIs(classify("PUT", f"{h}/members/m1/age"), Level.CRITICAL)
        for method, path in (("DELETE", f"{h}/members/m1"), ("PUT", f"{h}/members/m1/primary"),
                             ("POST", "checkout/orders"), ("POST", "auth/age")):
            self.assertIs(classify(method, path), Level.CRITICAL, path)

    def test_every_changing_route_outside_the_reviewed_list_is_not_normal(self):
        """A new route in FamilyCart must be classified on purpose to become NORMAL."""
        from backend.services.familycart_service.app import app as familycart

        normal = set()
        for route in familycart.routes:
            path = getattr(route, "path", "")
            if not path.startswith("/api/v1/"):
                continue
            for method in getattr(route, "methods", None) or ():
                if method not in {"GET", "HEAD"} and classify(method, path[8:].replace("{", "").replace("}", "")) is Level.NORMAL:
                    normal.add(f"{method} {path[8:]}")
        h = "households/{household_id}"
        item = f"{h}/carts/{{cart_id}}/items/{{sku}}"
        shopping_list = f"{h}/lists/{{list_id}}"
        self.assertEqual(normal, {
            "PATCH personal-cart/items/{sku}", f"PATCH {item}", f"DELETE {item}",
            f"PUT {item}/contributions/me", f"PATCH {item}/contributions/me", f"DELETE {item}/contributions/{{contributor_id}}",
            f"POST {h}/lists", f"PATCH {shopping_list}", f"POST {shopping_list}/items", f"POST {shopping_list}/items/bulk",
            f"POST {shopping_list}/items/clear-checked", f"POST {shopping_list}/move-to-cart",
            f"PATCH {shopping_list}/items/{{item_id}}", f"DELETE {shopping_list}/items/{{item_id}}",
            "POST my-lists", "PATCH my-lists/{list_id}", "POST my-lists/{list_id}/items", "POST my-lists/{list_id}/items/bulk",
            "POST my-lists/{list_id}/items/clear-checked", "POST my-lists/{list_id}/move-to-cart",
            "PATCH my-lists/{list_id}/items/{item_id}", "DELETE my-lists/{list_id}/items/{item_id}",
            f"POST {h}/addresses/{{address_id}}/resolve-store",
        })

    def test_paths_that_could_mean_another_route_are_refused(self):
        for path in ("households/h/carts/c#/items/x", "households/h/members/m?x", "households/h/x/../members/m", "a/%2e%2e/b", "a\\b", "a\nb"):
            self.assertTrue(is_ambiguous(path), path)
        self.assertFalse(is_ambiguous("households/h/carts/c/items/gala-apples"))


class FakeUpstream:
    def __init__(self):
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return httpx.Response(200, json={"ok": True})

    async def aclose(self):
        pass


class GatewayHttpTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.db = Path(folder) / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_USER_DB_PATH": str(self.db), "FAMILYCART_JWT_SECRET": self.secret,
            "FAMILYCART_REDIS_URL": "redis://127.0.0.1:1/0", "FAMILYCART_CACHE_REDIS_URL": "",
        }))
        initialize_database(self.db)
        self.client = self.enterContext(TestClient(gateway.app, raise_server_exceptions=False))
        gateway.app.state.http = self.upstream = FakeUpstream()
        self.tokens = create_session(self.db, self.secret, Identity("u", True), "U")
        self.identity = verify_token(self.tokens.access_token, self.secret)
        self.headers = {"Authorization": f"Bearer {self.tokens.access_token}"}

    def test_redis_down_uses_the_database(self):
        for method, path in (("GET", "products"), ("POST", "households/h/addresses"), ("DELETE", "households/h/members/m")):
            self.assertEqual(self.client.request(method, f"/api/v1/{path}", headers=self.headers).status_code, 200)

    def test_both_down_is_503_and_nothing_is_forwarded(self):
        def broken(*_):
            raise sqlite3.OperationalError("down")

        gateway.app.state.sessions.status = broken
        response = self.client.get("/api/v1/products", headers=self.headers)
        self.assertEqual((response.status_code, response.headers["retry-after"]), (503, "5"))
        self.assertEqual(self.upstream.requests, [])

    def test_a_silent_or_missing_service_is_not_an_internal_error(self):
        for error, status in ((httpx.ReadTimeout("slow"), 504), (httpx.ConnectError("refused"), 503)):
            async def fail(*args, **kwargs):
                raise error

            self.upstream.request = fail
            self.assertEqual(self.client.get("/api/v1/products", headers=self.headers).status_code, status)

    def test_path_tricks_are_rejected_before_forwarding(self):
        for path in ("households/h/carts/c%23/items/x", "households/h/carts/c%3F/items/x"):
            self.assertEqual(self.client.delete(f"/api/v1/{path}", headers=self.headers).status_code, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_rate_limiter_forgets_old_entries(self):
        limiter = gateway.SlidingWindowLimiter()
        with patch.object(gateway, "MAX_LIMITER_ENTRIES", 100):
            for n in range(500):
                limiter.allow(str(n), 5)
        self.assertEqual(len(limiter.requests), 100)
        with patch.object(gateway.time, "monotonic", lambda: time.perf_counter() + 1e9):
            limiter.sweep()
        self.assertEqual(len(limiter.requests), 0)

    def test_logout_closes_an_open_websocket_and_outage_asks_to_retry(self):
        class Upstream:
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): pass
            async def send(self, _): pass
            def __aiter__(self): return self
            async def __anext__(self): await asyncio.sleep(60)

        with patch.object(gateway.websockets, "connect", lambda *_: Upstream()):
            with self.client.websocket_connect("/api/v1/households/h/live") as socket:
                socket.send_json({"token": self.tokens.access_token})
                end = time.monotonic() + 3
                while not gateway.app.state.sessions._sockets and time.monotonic() < end:
                    time.sleep(0.02)
                loop_call = self.client.portal.call
                loop_call(gateway.app.state.sessions.on_revoked, cache.session_hash(self.identity.session_id))
                with self.assertRaises(WebSocketDisconnect) as closed:
                    socket.receive_json()
                self.assertEqual(closed.exception.code, 4401)
        fresh = create_session(self.db, self.secret, Identity("u", True), "U")
        gateway.app.state.sessions.status = lambda *_: (_ for _ in ()).throw(sqlite3.OperationalError())
        with self.client.websocket_connect("/api/v1/households/h/live") as socket:
            socket.send_json({"token": fresh.access_token})
            with self.assertRaises(WebSocketDisconnect) as closed:
                socket.receive_json()
        self.assertEqual(closed.exception.code, 1013)


class RefreshServiceOutageTests(unittest.TestCase):
    def setUp(self):
        folder = self.enterContext(TemporaryDirectory())
        self.db = Path(folder) / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {"FAMILYCART_USER_DB_PATH": str(self.db), "FAMILYCART_JWT_SECRET": self.secret}))
        self.client = self.enterContext(TestClient(refresh_app, raise_server_exceptions=False))
        self.tokens = create_session(self.db, self.secret, Identity("u", True), "U")

    def test_database_failure_is_503_and_keeps_the_cookie(self):
        for route, function in (("refresh", "rotate_session"), ("logout", "revoke_session")):
            with patch(f"backend.services.refresh_service.app.{function}", side_effect=sqlite3.OperationalError("down")):
                response = self.client.post(f"/api/v1/sessions/{route}", cookies={REFRESH_COOKIE: self.tokens.refresh_token})
            self.assertEqual(response.status_code, 503, route)
            self.assertNotIn("set-cookie", response.headers)

    def test_logout_stands_when_redis_is_down(self):
        with patch.object(cache, "_write_revocation", side_effect=RedisConnectionError("down")), \
                patch.object(cache, "_start_retry_thread"):
            response = self.client.post("/api/v1/sessions/logout", cookies={REFRESH_COOKIE: self.tokens.refresh_token})
        self.assertEqual(response.status_code, 204)
        session_id = self.tokens.refresh_token.split(".")[0]
        self.assertTrue(session_status(self.db, session_id, "u").revoked)
        self.assertEqual(recently_revoked(self.db, 60), [session_id])
        while not cache._retries.empty():
            cache._retries.get_nowait()


if __name__ == "__main__":
    unittest.main()
