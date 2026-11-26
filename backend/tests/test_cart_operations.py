"""Cart changes that are sent more than once, and what a browser needs to count each of them once.

Set FAMILYCART_CHECKOUT_TEST_DATABASE_URL to run this on PostgreSQL, where row locks and isolation are real.
"""

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from backend.services.familycart_service.app import app as cart_app, live_view
from backend.services.familycart_service.db import lock_household, transaction
from backend.services.familycart_service.live_sync import build_snapshots
from backend.services.familycart_service.operations import forget_old
from backend.shared.database import DatabaseConnection
from backend.shared.tokens import Identity
from backend.tests import test_checkout as base

API, HOUSEHOLD, CART, SHARED = base.API, base.HOUSEHOLD, base.REGULAR_CART, base.SHARED
OWNER, MEMBER = Identity("user-primary", True), Identity("user-adult", True)


class CartOperationTests(base.CheckoutTests):
    """Reuses the checkout fixtures; the inherited tests are not run again."""

    def setUp(self):
        super().setUp()
        cart_app.state.live_sync.publish_now = lambda: None
        self.grant_member()

    def call(self, method, path, who, key=None, expected=200, **body):
        headers = {**who, "Idempotency-Key": key} if key else who
        response = self.cart.request(method, path, headers=headers, json=body or None)
        if expected:
            self.assertEqual(response.status_code, expected, response.text)
        return response.json() if response.content and response.status_code < 500 else None

    def tap(self, key, delta=1, who=None, sku="milk", expected=200, cart=CART):
        return self.call("PATCH", f"{cart}/items/{sku}", who or self.owner, key, expected, delta=delta)

    def get(self, who=None, pending=(), cart=CART):
        response = self.cart.get(cart, headers=who or self.owner, params={"pending": ",".join(pending)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def quantity(self, sku="milk", cart=CART):
        return sum(row["quantity"] for row in self.get(cart=cart)["contributions"] if row["sku"] == sku)

    def rows(self, query, *parameters):
        with transaction(self.db) as db:
            return [dict(row) for row in db.execute(query, parameters).fetchall()]

    def together(self, *calls):
        with ThreadPoolExecutor(len(calls)) as pool:
            return [future.result() for future in [pool.submit(call) for call in calls]]

    # -- the same change twice -------------------------------------------------
    def test_a_lost_reply_is_answered_with_the_first_result(self):
        self.tap("start", 5)
        first = self.tap("abc")
        self.tap("other", 1, self.member)  # somebody else changes it meanwhile
        self.assertEqual(self.tap("abc"), first)
        self.assertEqual((first["quantity"], self.quantity()), (6, 7))
        self.tap("new-action")
        self.assertEqual(self.quantity(), 8)

    def test_removing_twice_and_removing_a_product_twice(self):
        self.tap("a", 5)
        self.tap("minus", -2)
        self.tap("minus", -2)
        self.assertEqual(self.quantity(), 3)
        gone = self.call("DELETE", f"{CART}/items/milk", self.owner, "trash")
        self.tap("again", 4)  # put back afterwards: an old retry must not remove it
        self.assertEqual(self.call("DELETE", f"{CART}/items/milk", self.owner, "trash"), gone)
        self.assertEqual(self.quantity(), 4)
        self.call("DELETE", f"{CART}/items/milk", self.owner, "trash-2")
        self.assertEqual(self.tap("nothing-left", -1)["quantity"], 0)
        self.tap("back", 1)
        self.tap("nothing-left", -1)
        self.assertEqual(self.quantity(), 1)

    def test_a_key_belongs_to_one_change_of_one_person(self):
        self.tap("k", 1)
        self.tap("k", 2, expected=409)
        self.tap("k", 1, sku="eggs", expected=409)
        self.tap("k", 1, self.member)
        self.assertEqual(self.quantity(), 2)

    def test_without_a_key_nothing_changes_for_older_clients(self):
        self.tap(None)
        self.tap(None)
        self.assertEqual(self.quantity(), 2)
        self.assertEqual(self.rows("SELECT * FROM cart_operations"), [])

    def test_the_key_and_the_change_are_saved_together_or_not_at_all(self):
        execute = DatabaseConnection.execute

        def fail(db, query, parameters=None):
            if "INSERT INTO cart_operations" in query:
                raise RuntimeError("lost connection")
            return execute(db, query, parameters)

        with patch.object(DatabaseConnection, "execute", fail):
            self.tap("k", expected=500)
        self.assertEqual((self.quantity(), self.rows("SELECT * FROM cart_operations")), (0, []))
        self.tap("k")
        self.tap("k")
        self.assertEqual(self.quantity(), 1)

    def test_a_refused_change_leaves_no_key(self):
        self.tap("a", 1)
        self.tap("k", -1, self.member, expected=403)
        self.assertEqual(self.rows("SELECT request_id FROM cart_operations WHERE request_id = 'k'"), [])

    def test_retry_after_the_cart_was_checked_out_and_replaced(self):
        self.tap("a", 1)
        address = self.address()
        self.call("POST", f"{CART}/checkout", self.owner)
        waiting = self.tap("w", 2, self.member)
        self.assertTrue(waiting["waiting"])
        self.assertEqual([row["quantity"] for row in waiting["lines"]], [2])
        quote = self.call("POST", f"{API}/checkout/quotes", self.owner, None, 201, cart=SHARED, default_address=address)
        following = self.place(quote).json()["next_cart"]["id"]
        self.assertEqual(self.tap("w", 2, self.member), waiting)
        late = self.tap("late", 1, self.member)  # meant for the old cart
        self.assertEqual(late["cart_id"], following)
        self.assertEqual(self.quantity(cart=f"{HOUSEHOLD}/carts/{following}"), 3)

    def test_creating_a_cart_twice(self):
        first = self.call("POST", f"{HOUSEHOLD}/carts", self.owner, "cart", 201, name="Party", adults_only=False)
        again = self.call("POST", f"{HOUSEHOLD}/carts", self.owner, "cart", 201, name="Party", adults_only=False)
        self.assertEqual(first, again)
        self.assertEqual(len(self.rows("SELECT id FROM carts WHERE name = 'Party'")), 1)
        self.call("POST", f"{HOUSEHOLD}/carts", self.owner, "cart", 409, name="Other", adults_only=False)
        self.call("POST", f"{HOUSEHOLD}/carts", self.owner, "cart-2", 201, name="Party", adults_only=False)
        self.assertEqual(len(self.rows("SELECT id FROM carts WHERE name = 'Party'")), 2)

    def make_list(self, path):
        made = self.call("POST", path, self.owner, None, 201, name="Weekly")["id"]
        item = self.call("POST", f"{path}/{made}/items", self.owner, None, 201, sku="milk", quantity=3)["id"]
        return f"{path}/{made}", item

    def test_moving_a_household_list_twice(self):
        path, item = self.make_list(f"{HOUSEHOLD}/lists")
        move = lambda key: self.call("POST", f"{path}/move-to-cart", self.owner, key,
                                     target="regular-cart", item_ids=[item])
        self.assertEqual(move("m"), move("m"))
        self.assertEqual(self.quantity(), 3)
        move("m2")
        self.assertEqual(self.quantity(), 6)

    def test_moving_a_private_list_twice(self):
        path, item = self.make_list(f"{API}/my-lists")
        move = lambda key: self.call("POST", f"{path}/move-to-cart", self.owner, key,
                                     target="regular-cart", item_ids=[item], household_id="demo-household")
        self.assertEqual(move("m"), move("m"))
        self.assertEqual(self.quantity(), 3)

    # -- at the same moment ----------------------------------------------------
    def test_simultaneous_copies_apply_once(self):
        results = self.together(*[lambda: self.tap("same") for _ in range(8)])
        self.assertEqual(len({str(result) for result in results}), 1)
        self.assertEqual(self.quantity(), 1)
        path, item = self.make_list(f"{HOUSEHOLD}/lists")
        mine, my_item = self.make_list(f"{API}/my-lists")
        create = lambda: self.call("POST", f"{HOUSEHOLD}/carts", self.owner, "c", 201, name="Once", adults_only=False)
        move = lambda: self.call("POST", f"{path}/move-to-cart", self.owner, "m",
                                 target="regular-cart", item_ids=[item])
        move_mine = lambda: self.call("POST", f"{mine}/move-to-cart", self.owner, "n", target="regular-cart",
                                      item_ids=[my_item], household_id="demo-household")
        self.together(*[call for call in (create, move, move_mine) for _ in range(4)])
        self.assertEqual(len(self.rows("SELECT id FROM carts WHERE name = 'Once'")), 1)
        self.assertEqual(self.quantity(), 7)

    def test_simultaneous_members_lose_nothing(self):
        calls = [lambda who=who, key=f"{name}-{n}": self.tap(key, 1, who)
                 for n in range(10) for name, who in (("o", self.owner), ("m", self.member))]
        self.together(*calls)
        lines = {row["contributor_id"]: row["quantity"] for row in self.get()["contributions"]}
        self.assertEqual(lines, {"user-primary": 10, "user-adult": 10})

    def test_contents_revision_and_applied_keys_belong_to_one_moment(self):
        done = Event()

        def write():
            try:
                for n in range(25):
                    self.tap(f"t{n}")
            finally:
                done.set()

        problems = []

        def read():
            last = 0
            while not done.is_set():
                cart = self.get()
                view = live_view(self.connection(), "demo-household", OWNER, "regular-cart")
                for seen in (cart, {**view["cart"], "revision": view["revision"], "applied": view["applied"]}):
                    total = sum(row["quantity"] for row in seen["contributions"])
                    if total != len(seen["applied"]) or seen["revision"] < last:
                        problems.append((total, len(seen["applied"]), seen["revision"], last))
                    last = seen["revision"]

        self.together(write, read, read)
        self.assertEqual(problems, [])
        self.assertEqual(self.quantity(), 25)

    def test_a_stuck_database_is_a_retry_and_not_a_verdict(self):
        if not isinstance(self.db, str):
            self.skipTest("row locks need PostgreSQL")
        with transaction(self.db, write=True) as db:
            lock_household(db, "demo-household")
            started = time.monotonic()
            problem = self.cart.patch(f"{CART}/items/milk", json={"delta": 1},
                                      headers={**self.owner, "Idempotency-Key": "k"})
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual((problem.status_code, problem.headers["Retry-After"]), (503, "1"))
        self.tap("k")
        self.assertEqual(self.quantity(), 1)

    # -- what each browser is told ----------------------------------------------
    def connection(self):
        return SimpleNamespace(app=cart_app)

    def test_everyone_is_told_only_about_their_own_changes(self):
        self.tap("mine")
        self.tap("theirs", 1, self.member)
        self.assertEqual(self.get()["applied"], ["mine"])
        self.assertEqual(self.get(self.member, ["mine"])["applied"], ["theirs"])  # asking does not help
        with transaction(self.db) as db:
            batch = build_snapshots(db, ["demo-household"])[0]["demo-household"]
        for who, key in ((OWNER, "mine"), (MEMBER, "theirs")):
            for view in (live_view(self.connection(), "demo-household", who, "regular-cart", batch),
                         live_view(self.connection(), "demo-household", who, "regular-cart")):
                self.assertEqual(view["applied"], [key])
                self.assertNotIn("operations", view)

    def test_an_old_change_is_confirmed_when_asked_about_by_name(self):
        self.tap("old")
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE cart_operations SET created_at = ?",
                       ((datetime.now(timezone.utc) - timedelta(days=6)).isoformat(),))
        self.assertEqual(self.get()["applied"], [])
        self.assertEqual(self.get(pending=["old", "never-arrived"])["applied"], ["old"])
        view = live_view(self.connection(), "demo-household", OWNER, "regular-cart", None, ["old"])
        self.assertEqual(view["applied"], ["old"])

    def test_keys_are_kept_for_thirty_days(self):
        for key, days in (("kept", 29), ("gone", 31)):
            self.tap(key)
            with transaction(self.db, write=True) as db:
                db.execute("UPDATE cart_operations SET created_at = ? WHERE request_id = ?",
                           ((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(), key))
        with transaction(self.db, write=True) as db:
            forget_old(db)
        self.assertEqual(self.rows("SELECT request_id FROM cart_operations"), [{"request_id": "kept"}])

    def test_revisions_keep_rising_while_items_wait_even_after_events_are_deleted(self):
        self.tap("a")
        self.call("POST", f"{CART}/checkout", self.owner)
        before = self.get()["revision"]
        waiting = self.tap("w", 1, self.member)
        self.assertGreater(waiting["revision"], before)
        with transaction(self.db, write=True) as db:
            db.execute("DELETE FROM cart_sync_events")  # published
        self.assertEqual(self.get()["revision"], waiting["revision"])
        # The revision an open checkout review is compared with did not move.
        self.assertEqual(self.rows("SELECT revision FROM carts WHERE id = 'regular-cart'"),
                         [{"revision": before}])

    def test_a_worker_that_fails_after_the_commit_sends_the_change_later(self):
        sync = cart_app.state.live_sync

        class Pipe(list):
            fail = True
            __enter__ = lambda self: self
            __exit__ = lambda self, *exc: False
            smembers = delete = hset = expire = sadd = lambda self, *args, **kwargs: None
            publish = lambda self, channel, message: self.append(message)

            def execute(self):
                if Pipe.fail:
                    raise base_error("redis went away")
                return [{"demo-household"}, 1]

        from redis.exceptions import RedisError as base_error
        pipe = Pipe()
        redis = SimpleNamespace(pipeline=lambda **_: pipe, sadd=lambda *_: None,
                                mget=lambda keys: ["1"] * len(keys), hmget=lambda key, fields: [None] * len(fields))
        self.tap("k")
        with patch.multiple(sync, redis=redis, cache=redis, _take_dirty=lambda: {"demo-household"}):
            with self.assertRaises(base_error):
                sync.tick()
            self.assertEqual(Counter(row["request_id"] for row in self.rows("SELECT * FROM cart_sync_events"))["k"], 1)
            pipe.clear()
            Pipe.fail = False
            sync.tick()
        self.assertEqual(len(pipe), 1)
        self.assertIn('["user-primary", "k"]', pipe[0])
        self.assertEqual(self.rows("SELECT * FROM cart_sync_events"), [])
        self.assertEqual(self.quantity(), 1)

    def test_a_socket_counts_as_online_before_it_reads(self):
        order = []
        sync = cart_app.state.live_sync

        async def mark_online(household_id):
            order.append("online")

        class PubSub:
            async def __aenter__(self): return self
            async def __aexit__(self, *exc): return False
            async def subscribe(self, channel): order.append("subscribe")
            async def get_message(self, timeout=0):
                import asyncio
                await asyncio.sleep(0.05)
                return {"type": "subscribe"}

        def view(*args):
            order.append(("read", args[-1]))
            return {"revision": 0}

        token = self.owner["Authorization"][7:]
        with patch.object(sync, "mark_online", mark_online), \
                patch.object(sync, "subscriber", SimpleNamespace(pubsub=PubSub, aclose=sync.subscriber.aclose)), \
                patch("backend.services.familycart_service.app.live_view", view), \
                self.cart.websocket_connect(f"{HOUSEHOLD}/live?cart_id=regular-cart") as socket:
            socket.send_json({"token": token, "pending": ["k", 7]})
            socket.receive_json()
        self.assertEqual(order[:3], ["subscribe", "online", ("read", ["k"])])


for name in [name for name in dir(base.CheckoutTests) if name.startswith("test_")]:
    if name not in vars(CartOperationTests):
        setattr(CartOperationTests, name, None)
del name
