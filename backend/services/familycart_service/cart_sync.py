import asyncio
import json
import logging
from collections import defaultdict
from hashlib import sha256

from redis import Redis
from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from backend.shared.database import DatabaseConnection, DatabaseTarget, database_error_types

from .db import load_contributions, transaction

SYNC_INTERVAL = 60
STAGING_INTERVAL = 0.25
logger = logging.getLogger(__name__)


def latest_revision(db: DatabaseConnection) -> int:
    if db.dialect == "postgresql":
        row = db.execute("SELECT last_value, is_called FROM cart_sync_events_id_seq").fetchone()
        return row["last_value"] if row["is_called"] else 0
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'cart_sync_events'").fetchone()
    return row["seq"] if row else 0


SYNC_ERRORS = (RedisError,) + database_error_types()


class CartSync:
    def __init__(self, db_path: DatabaseTarget, redis_url: str):
        self.db_path = db_path
        namespace = sha256(str(db_path).encode()).hexdigest()[:16]
        self.namespace = f"familycart:{namespace}"
        self.stash = f"{self.namespace}:pending"
        options = {"decode_responses": True, "socket_connect_timeout": 1, "socket_timeout": 3}
        self.redis = Redis.from_url(redis_url, **options)
        self.subscriber = AsyncRedis.from_url(redis_url, **options)
        self.staged_through = 0

    def channel(self, household_id: str) -> str:
        return f"{self.namespace}:household:{household_id}"

    def stage(self, after: int) -> tuple[int, dict[str, dict[str, str | None]]]:
        with transaction(self.db_path) as db:
            events = db.execute(
                "SELECT id, household_id, cart_id, actor_id FROM cart_sync_events WHERE id > ? ORDER BY id",
                (after,),
            ).fetchall()
            if not events:
                return after, {}
            carts_by_household = defaultdict(dict)
            staged = {}
            for event in events:
                # Events are ordered, so this keeps the actor responsible for
                # the most recent visible change to each cart in this batch.
                carts_by_household[event["household_id"]][event["cart_id"]] = event["actor_id"]
            for cart_ids in carts_by_household.values():
                for cart_id in cart_ids:
                    staged[cart_id] = json.dumps(load_contributions(db, cart_id))
        self.redis.hset(self.stash, mapping=staged)
        return events[-1]["id"], carts_by_household

    def flush(self) -> None:
        # Rebuild from committed events so Redis restarts cannot drop a pending update.
        cutoff, households = self.stage(after=0)
        if not households:
            return
        cart_ids = [cart_id for carts in households.values() for cart_id in carts]
        snapshots = dict(zip(cart_ids, self.redis.hmget(self.stash, cart_ids), strict=True))
        if any(snapshot is None for snapshot in snapshots.values()):
            return
        with self.redis.pipeline(transaction=True) as batch:
            for household_id, actors in households.items():
                payload = {"revision": cutoff, "carts": {
                    cart_id: json.loads(snapshots[cart_id]) for cart_id in actors
                }, "actors": actors}
                batch.publish(self.channel(household_id), json.dumps(payload))
            batch.hdel(self.stash, *cart_ids)
            batch.execute()
        with transaction(self.db_path, write=True) as db:
            db.execute("DELETE FROM cart_sync_events WHERE id <= ?", (cutoff,))
        self.staged_through = cutoff

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SYNC_INTERVAL
        failed = False
        while True:
            await asyncio.sleep(min(STAGING_INTERVAL, max(0, deadline - loop.time())))
            flush_due = loop.time() >= deadline
            if flush_due:
                deadline += SYNC_INTERVAL
            try:
                if flush_due:
                    await asyncio.to_thread(self.flush)
                else:
                    self.staged_through, _ = await asyncio.to_thread(self.stage, self.staged_through)
                failed = False
            except SYNC_ERRORS as error:
                if not failed:
                    logger.warning("Cart synchronization will retry: %s", error)
                failed = True

    async def close(self) -> None:
        self.redis.close()
        await self.subscriber.aclose()
