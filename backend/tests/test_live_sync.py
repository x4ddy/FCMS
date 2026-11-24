import json
import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.familycart_service import live_sync
from backend.services.familycart_service.app import app, live_view
from backend.services.familycart_service.lists import lists_view
from backend.shared.database import DatabaseConnection
from backend.shared.tokens import Identity, issue_token


class FakeRedis:
    """Just enough of Redis for the worker: sets, hashes, plain keys and publish."""

    def __init__(self) -> None:
        self.sets: dict[str, set] = {}
        self.hashes: dict[str, dict] = {}
        self.values: dict[str, str] = {}
        self.published: list[tuple[str, dict]] = []

    def sadd(self, key, *values):
        self.sets.setdefault(key, set()).update(values)

    def smembers(self, key):
        return set(self.sets.get(key, set()))

    def delete(self, key):
        self.sets.pop(key, None)

    def publish(self, channel, message):
        self.published.append((channel, json.loads(message)))

    def mget(self, keys):
        return [self.values.get(key) for key in keys]

    def hmget(self, key, fields):
        return [self.hashes.get(key, {}).get(field) for field in fields]

    def expire(self, key, seconds):
        pass

    def hset(self, key, mapping):
        self.hashes.setdefault(key, {}).update(mapping)

    def pipeline(self, transaction=False):
        fake = self

        class Pipeline:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def __getattr__(self, name):
                return lambda *args, **kwargs: self.calls.append((name, args, kwargs))

            def execute(self):
                return [getattr(fake, name)(*args, **kwargs) for name, args, kwargs in self.calls]

        return Pipeline()


class QueryCounter:
    def __init__(self) -> None:
        self.count = 0
        self.original = DatabaseConnection.execute

    def __enter__(self):
        counter = self

        def counted(connection, query, parameters=None):
            counter.count += 1
            return counter.original(connection, query, parameters)

        self.patch = patch.object(DatabaseConnection, "execute", counted)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


class LiveSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(Path(folder) / "live.sqlite3"),
            "FAMILYCART_JWT_SECRET": self.secret,
            "FAMILYCART_REDIS_URL": "redis://127.0.0.1:1/0",
            "FAMILYCART_SYNC_INTERVAL_SECONDS": "3600",  # the test drives tick() itself
        }))
        self.client = self.enterContext(TestClient(app))
        self.sync = app.state.live_sync
        self.fake = FakeRedis()
        self.sync.redis = self.sync.cache = self.fake
        self.sync.breaker._open_until = 0
        # Mark dirty synchronously so the test does not race the background thread.
        self.sync.mark_dirty = lambda household: self.fake.sadd(self.sync.dirty_key, household)
        live_sync._listener = self.sync.mark_dirty

        self.primary_id, self.member_id, self.child_id = "live-primary", "live-member", "live-child"
        self.primary = self.headers(self.primary_id)
        self.member = self.headers(self.member_id)
        self.child = self.headers(self.child_id, adult=False)
        household = self.client.post("/api/v1/households", headers=self.primary, json={
            "name": "Live Family", "display_name": "Pat Primary",
        }).json()
        self.household_id = household["id"]
        self.root = f"/api/v1/households/{self.household_id}"
        for headers, name in ((self.member, "Morgan Member"), (self.child, "Casey Child")):
            code = self.client.post(f"{self.root}/invitations", headers=self.primary).json()["invitation_code"]
            self.client.post("/api/v1/household-invitations/accept", headers=headers, json={
                "invitation_code": code, "display_name": name,
            })
        [self.cart] = self.client.get(f"{self.root}/carts", headers=self.primary).json()
        self.go_online()
        self.sync.tick()  # flush setup changes
        self.fake.published.clear()

    def headers(self, user_id: str, *, adult: bool = True) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(Identity(user_id, adult), self.secret)}"}

    def go_online(self, household_id=None):
        self.fake.values[self.sync.online_key(household_id or self.household_id)] = "1"

    def published_snapshot(self) -> dict:
        [(channel, snapshot)] = self.fake.published
        self.assertEqual(channel, self.sync.channel(self.household_id))
        return snapshot

    def test_idle_batches_do_not_touch_the_database(self) -> None:
        self.sync._last_sweep = __import__("time").monotonic()  # the safety sweep is not due
        with QueryCounter() as counter:
            for _ in range(5):
                self.sync.tick()
        self.assertEqual(counter.count, 0)
        self.assertEqual(self.fake.published, [])

    def test_one_batch_for_many_changes_and_constant_cost_for_any_number_of_viewers(self) -> None:
        item = f"{self.root}/carts/{self.cart['id']}/items/milk"
        for _ in range(10):
            self.assertEqual(self.client.patch(item, headers=self.member, json={"delta": 1}).status_code, 200)
        self.sync._last_sweep = __import__("time").monotonic()
        with QueryCounter() as counter:
            self.sync.tick()
        snapshot = self.published_snapshot()
        self.assertLessEqual(counter.count, 10, "a batch should cost a handful of bulk queries")
        self.assertEqual(snapshot["changed_carts"][self.cart["id"]], [
            {"sku": "milk", "contributor_id": self.member_id, "quantity": 10},
        ])

        # Every viewer's update is built in memory: zero queries, however many there are.
        connection = SimpleNamespace(app=app)
        with QueryCounter() as counter:
            for _ in range(100):
                for user_id, adult in ((self.primary_id, True), (self.member_id, True), (self.child_id, False)):
                    live_view(connection, self.household_id, Identity(user_id, adult), self.cart["id"], snapshot)
        self.assertEqual(counter.count, 0)

        # Processed cart events are cleared; nothing is published twice.
        self.fake.published.clear()
        self.sync.tick()
        self.assertEqual(self.fake.published, [])

    def test_in_memory_view_matches_the_database_view(self) -> None:
        adults_only = self.client.post(f"{self.root}/carts", headers=self.primary, json={
            "name": "Party", "adults_only": True,
        }).json()
        self.client.delete(f"{self.root}/carts/{adults_only['id']}/grants/{self.member_id}", headers=self.primary)
        self.client.patch(f"{self.root}/carts/{self.cart['id']}/items/milk", headers=self.child, json={"delta": 1})
        self.sync.tick()
        snapshot = self.published_snapshot()
        connection = SimpleNamespace(app=app)
        for user_id, adult in ((self.primary_id, True), (self.member_id, True), (self.child_id, False)):
            for cart_id in (self.cart["id"], adults_only["id"], None):
                with self.subTest(user=user_id, cart=cart_id):
                    caller = Identity(user_id, adult)
                    from_db = live_view(connection, self.household_id, caller, cart_id, None)
                    from_batch = live_view(connection, self.household_id, caller, cart_id, snapshot)
                    for key in ("carts", "household_role", "can_create_adults_only_cart", "members",
                                "access_notice", "grants"):
                        self.assertEqual(from_batch.get(key), from_db.get(key), key)
                    if cart_id in snapshot["changed_carts"] and from_db.get("cart"):
                        self.assertEqual(from_batch["cart"], from_db["cart"])

    def test_removed_member_is_cut_off_by_the_next_batch(self) -> None:
        self.client.delete(f"{self.root}/members/{self.member_id}", headers=self.primary)
        self.sync.tick()
        snapshot = self.published_snapshot()
        with self.assertRaises(Exception) as raised:
            live_view(SimpleNamespace(app=app), self.household_id, Identity(self.member_id, True), None, snapshot)
        self.assertIn("revoked. Contact Pat Primary", str(raised.exception.detail))
        with self.assertRaises(live_sync.AccessEnded):
            lists_view(SimpleNamespace(app=app), snapshot, Identity(self.member_id, True))

    def test_offline_households_are_skipped_and_their_events_cleared(self) -> None:
        self.fake.values.clear()  # nobody has the app open
        self.client.patch(f"{self.root}/carts/{self.cart['id']}/items/milk", headers=self.member, json={"delta": 1})
        self.sync.tick()
        self.assertEqual(self.fake.published, [])
        self.assertEqual(self.sync.stats["households_skipped_offline"], 1)
        with QueryCounter():
            from backend.services.familycart_service.db import transaction
            with transaction(app.state.db_path) as db:
                left = db.execute("SELECT COUNT(*) AS n FROM cart_sync_events").fetchone()["n"]
        self.assertEqual(left, 0)

    def test_list_batches_carry_only_changed_lists_filtered_per_viewer(self) -> None:
        adult_sku = next(sku for sku, product in app.state.products.items() if product.min_age)
        quiet = self.client.post(f"{self.root}/lists", headers=self.primary, json={"name": "Quiet"}).json()
        busy = self.client.post(f"{self.root}/lists", headers=self.primary, json={"name": "Busy"}).json()
        self.sync.tick()
        self.fake.published.clear()

        added = self.client.post(f"{self.root}/lists/{busy['id']}/items/bulk", headers=self.member, json={"items": [
            {"sku": "milk", "quantity": 2}, {"custom_name": "Candles"}, {"sku": adult_sku},
        ]})
        self.assertEqual(added.status_code, 201)
        self.assertEqual([item.get("custom_name") or item["sku"] for item in added.json()["items"]], ["milk", "Candles", adult_sku])
        self.sync.tick()
        snapshot = self.published_snapshot()
        self.assertEqual([entry["id"] for entry in snapshot["lists"]], [busy["id"]])

        connection = SimpleNamespace(app=app)
        adult_view = lists_view(connection, snapshot, Identity(self.member_id, True))["lists"][0]
        child_view = lists_view(connection, snapshot, Identity(self.child_id, False))["lists"][0]
        self.assertEqual(adult_view["item_count"], 3)
        self.assertEqual(child_view["item_count"], 2)
        self.assertNotIn(adult_sku, [item["sku"] for item in child_view["items"]])

        self.fake.published.clear()
        self.client.delete(f"{self.root}/lists/{quiet['id']}", headers=self.primary)
        self.sync.tick()
        deleted = lists_view(connection, self.published_snapshot(), Identity(self.member_id, True))
        self.assertEqual(deleted["lists"], [{"id": quiet["id"], "deleted": True}])
        self.assertEqual(self.client.get(f"{self.root}/lists/{quiet['id']}", headers=self.primary).status_code, 404)

    def test_changes_are_announced_only_after_commit(self) -> None:
        self.fake.sets.clear()
        failed = self.client.post(f"{self.root}/lists", headers=self.headers("outsider"), json={"name": "Nope"})
        self.assertEqual(failed.status_code, 404)
        self.assertEqual(self.fake.sets.get(self.sync.dirty_key, set()), set())
        self.client.post(f"{self.root}/lists", headers=self.member, json={"name": "Yes"})
        self.assertEqual(self.fake.sets[self.sync.dirty_key], {self.household_id})

    def test_combined_taps_follow_the_same_rules_as_single_taps(self) -> None:
        item = f"{self.root}/carts/{self.cart['id']}/items/milk"
        self.assertEqual(self.client.patch(item, headers=self.member, json={"delta": 3}).json()["quantity"], 3)
        self.assertEqual(self.client.patch(item, headers=self.primary, json={"delta": 2}).json()["quantity"], 5)
        # A member can only take back their own 3, even when asking for 5.
        self.assertEqual(self.client.patch(item, headers=self.member, json={"delta": -5}).json()["quantity"], 2)
        self.assertEqual(self.client.patch(item, headers=self.member, json={"delta": -1}).status_code, 403)
        self.assertEqual(self.client.patch(item, headers=self.primary, json={"delta": -9}).json()["quantity"], 0)
        self.assertEqual(self.client.patch(item, headers=self.primary, json={"delta": 100}).status_code, 422)
        personal = "/api/v1/personal-cart/items/milk"
        self.assertEqual(self.client.patch(personal, headers=self.member, json={"delta": 4}).json()["quantity"], 4)
        self.assertEqual(self.client.patch(personal, headers=self.member, json={"delta": -9}).json()["quantity"], 0)

    def test_changes_made_during_a_redis_outage_are_delivered_when_it_returns(self) -> None:
        from datetime import datetime, timedelta, timezone

        from redis.exceptions import ConnectionError as RedisConnectionError

        from backend.services.familycart_service.db import transaction

        self.sync.interval = 2  # small batch window, so old changes fall outside it
        self.sync._last_sweep = __import__("time").monotonic()
        previous_sweep = datetime.now(timezone.utc) - timedelta(minutes=20)
        self.sync._last_sweep_at = previous_sweep.isoformat()
        listed = self.client.post(f"{self.root}/lists", headers=self.member, json={"name": "Outage"}).json()
        self.sync.tick()
        self.fake.published.clear()

        # Redis goes down: every call fails, like the real client would.
        healthy = {name: getattr(self.fake, name) for name in ("sadd", "smembers", "publish", "pipeline")}

        def down(*args, **kwargs):
            raise RedisConnectionError("Redis is down")

        for name in healthy:
            setattr(self.fake, name, down)

        def mark_dirty_like_production(household):
            try:
                self.fake.sadd(self.sync.dirty_key, household)
            except RedisConnectionError:
                self.sync._force_sweep = True

        self.sync.mark_dirty = mark_dirty_like_production
        live_sync._listener = mark_dirty_like_production

        self.client.post(f"{self.root}/lists/{listed['id']}/items", headers=self.member, json={"custom_name": "Torch"})
        self.client.patch(f"{self.root}/carts/{self.cart['id']}/items/milk", headers=self.member, json={"delta": 2})
        # Make the list change older than the batch window, as in a long outage.
        ten_minutes_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        with transaction(app.state.db_path, write=True) as db:
            db.execute("UPDATE shopping_lists SET updated_at = ? WHERE id = ?", (ten_minutes_ago, listed["id"]))
        self.assertTrue(self.sync._force_sweep)

        with self.assertRaises(RedisConnectionError):
            self.sync.tick()
        # The failed batch changed nothing: the sweep point and the cart events are kept.
        self.assertEqual(self.sync._last_sweep_at, previous_sweep.isoformat())
        self.assertTrue(self.sync._force_sweep)
        with transaction(app.state.db_path) as db:
            self.assertGreater(db.execute("SELECT COUNT(*) AS n FROM cart_sync_events").fetchone()["n"], 0)

        # Redis is back.
        for name, method in healthy.items():
            setattr(self.fake, name, method)
        self.sync.breaker._open_until = 0
        self.sync.tick()
        snapshot = self.published_snapshot()
        [outage_list] = [entry for entry in snapshot["lists"] if entry["id"] == listed["id"]]
        self.assertEqual([item["custom_name"] for item in outage_list["items"]], ["Torch"])
        self.assertEqual(snapshot["changed_carts"][self.cart["id"]][0]["quantity"], 2)
        self.assertFalse(self.sync._force_sweep)

    def test_lost_notes_are_recovered_by_the_sweep(self) -> None:
        self.client.post(f"{self.root}/lists", headers=self.member, json={"name": "Lost note"})
        self.fake.sets.clear()  # simulate Redis losing the "household changed" note
        self.sync._force_sweep = True
        self.sync.tick()
        snapshot = self.published_snapshot()
        self.assertEqual([entry["name"] for entry in snapshot["lists"]], ["Lost note"])


if __name__ == "__main__":
    unittest.main()
