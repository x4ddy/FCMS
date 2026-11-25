"""Batched live updates for carts and shopping lists.

How it works, in short:

1. A request saves its change in the database as usual. After the transaction
   COMMITS, it drops a tiny "household X changed" note in a Redis set.
2. Every ``FAMILYCART_SYNC_INTERVAL_SECONDS`` (default 30) one worker:
     - takes all notes from Redis (no database work at all if there are none),
     - skips households where nobody has the app open ("who's online" keys),
     - loads the changed carts and lists of the remaining households with a
       handful of bulk queries, however many people are watching,
     - publishes ONE snapshot per household on Redis Pub/Sub.
3. Each open WebSocket filters that snapshot for its own viewer in memory
   (adults-only products, permissions, membership) and pushes it. Sockets do
   not query the database for updates.

Safety nets: cart changes are also recorded in ``cart_sync_events`` and only
deleted after they were published, and a slow periodic sweep of the database
finds anything whose Redis note was lost (for example during a Redis outage).
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from redis.asyncio import Redis as AsyncRedis
from redis.exceptions import RedisError

from backend.shared.cache import CACHE_PREFIX, Breaker, sync_client
from backend.shared.database import DatabaseConnection, DatabaseTarget, database_error_types
from backend.shared.demo_members import MEMBERS_BY_ID

from .db import ADDRESS_FIELDS, ITEM_COLUMNS, ITEM_JOINS, LIST_COLUMNS, transaction

logger = logging.getLogger(__name__)
SYNC_ERRORS = (RedisError,) + database_error_types()
# Lists edited this long before the previous batch are looked at again, so a
# transaction that was still committing while that batch ran is never missed.
LIST_OVERLAP = timedelta(seconds=5)


def sync_interval() -> float:
    return max(1.0, float(os.environ.get("FAMILYCART_SYNC_INTERVAL_SECONDS", "30")))


def sweep_interval() -> float:
    return max(sync_interval(), float(os.environ.get("FAMILYCART_SYNC_SWEEP_SECONDS", "300")))


def latest_revision(db: DatabaseConnection) -> int:
    if db.dialect == "postgresql":
        row = db.execute("SELECT last_value, is_called FROM cart_sync_events_id_seq").fetchone()
        return row["last_value"] if row["is_called"] else 0
    row = db.execute("SELECT seq FROM sqlite_sequence WHERE name = 'cart_sync_events'").fetchone()
    return row["seq"] if row else 0


def household_revision(db: DatabaseConnection, household_id: str) -> int:
    """The latest cart revision in a household (the numbering checkout quotes are checked against)."""
    return db.execute(
        """SELECT COALESCE(MAX(revisions.revision), 0) AS revision
           FROM carts JOIN checkout_cart_revisions AS revisions
             ON revisions.cart_id = carts.id AND revisions.kind = 'household'
           WHERE carts.household_id = ?""",
        (household_id,),
    ).fetchone()["revision"]


# ---------------------------------------------------------------------------
# "Household changed" notes, sent only after the transaction commits.
# ---------------------------------------------------------------------------
_listener: Callable[[str], None] | None = None


def notify_changed(db: DatabaseConnection, household_id: str) -> None:
    """Mark a household as changed once the current transaction commits."""
    pending = getattr(db, "_live_changed", None)
    if pending is None:
        pending = db._live_changed = set()

        def flush() -> None:
            households = list(pending)
            pending.clear()
            listener = _listener
            if listener is not None:
                for household in households:
                    listener(household)

        db.after_commit.append(flush)
    pending.add(household_id)


# ---------------------------------------------------------------------------
# Snapshot building: a few bulk queries for any number of households.
# ---------------------------------------------------------------------------
def _placeholders(values: Iterable) -> str:
    return ", ".join("?" for _ in values)


def build_snapshots(
    db: DatabaseConnection,
    household_ids: list[str],
    *,
    lists_since: dict[str, str | None] | None = None,
    include_events: bool = True,
    carts_to_load: dict[str, set[str]] | None = None,
) -> tuple[dict[str, dict], list[int]]:
    """Build one snapshot per household and return them with the cart event IDs they cover.

    ``lists_since`` maps a household to the time of its previous batch; lists
    updated after that (minus a small overlap) are included. ``None`` skips lists.
    """
    if not household_ids:
        return {}, []
    marks = _placeholders(household_ids)
    snapshots: dict[str, dict] = {}

    for row in db.execute(
        f"""SELECT members.household_id, members.user_id, members.active,
                   household.created_by AS owner_id, profiles.display_name, profiles.is_adult,
                   profiles.age_status
            FROM household_members AS members
            JOIN household_profiles AS household ON household.household_id = members.household_id
            LEFT JOIN household_member_profiles AS profiles
              ON profiles.household_id = members.household_id AND profiles.user_id = members.user_id
            WHERE members.household_id IN ({marks})
            ORDER BY members.household_id, members.user_id""",
        household_ids,
    ):
        snapshot = snapshots.setdefault(row["household_id"], {
            "household_id": row["household_id"], "owner_id": row["owner_id"],
            "owner_name": "the primary account holder", "revision": None, "members": [],
            "carts": [], "changed_carts": {}, "actors": {}, "cart_actors": {}, "lists": [],
            "addresses": [],
        })
        fixture = MEMBERS_BY_ID.get(row["user_id"])
        fixture_adult = bool(fixture and fixture.identity.is_adult)
        profile_adult = row["is_adult"]
        snapshot["members"].append({
            "id": row["user_id"],
            "active": bool(row["active"]),
            "display_name": row["display_name"] or (fixture.display_name if fixture else row["user_id"]),
            # Same fallbacks as the REST member list and grant checks.
            "is_adult": bool(profile_adult) if profile_adult is not None else fixture_adult,
            "grant_adult": profile_adult == 1 or fixture_adult,
            "age_status": row["age_status"] or "APPROVED",
        })
        if row["user_id"] == row["owner_id"] and row["display_name"]:
            snapshot["owner_name"] = row["display_name"]

    present = list(snapshots)
    if not present:
        return {}, []
    marks = _placeholders(present)

    for row in db.execute(
        f"""SELECT household_id, {ADDRESS_FIELDS}, created_by, source_address_id FROM household_addresses
            WHERE household_id IN ({marks}) ORDER BY created_at, id""",
        present,
    ):
        address = dict(row)
        snapshots[address.pop("household_id")]["addresses"].append(address)

    carts_by_id = {}
    for row in db.execute(
        f"""SELECT carts.household_id, carts.id, carts.name, carts.creator_id, carts.adults_only,
                   deleted_carts.cart_id IS NOT NULL AS deleted,
                   cart_creator_revocations.cart_id IS NOT NULL AS creator_revoked
            FROM carts
            LEFT JOIN deleted_carts ON deleted_carts.cart_id = carts.id
            LEFT JOIN cart_creator_revocations ON cart_creator_revocations.cart_id = carts.id
            WHERE carts.household_id IN ({marks}) ORDER BY carts.id""",
        present,
    ):
        cart = {
            "id": row["id"], "name": row["name"], "creator_id": row["creator_id"],
            "adults_only": bool(row["adults_only"]), "deleted": bool(row["deleted"]),
            "creator_revoked": bool(row["creator_revoked"]), "grants": [],
        }
        carts_by_id[row["id"]] = cart
        snapshots[row["household_id"]]["carts"].append(cart)

    for row in db.execute(
        f"SELECT cart_id, member_id FROM cart_grants WHERE household_id IN ({marks}) ORDER BY member_id",
        present,
    ):
        if row["cart_id"] in carts_by_id:
            carts_by_id[row["cart_id"]]["grants"].append(row["member_id"])

    event_ids: list[int] = []
    changed: dict[str, set[str]] = defaultdict(set)
    for household, cart_ids in (carts_to_load or {}).items():
        changed[household].update(cart_ids)
    if include_events:
        for row in db.execute(
            f"""SELECT id, household_id, cart_id, actor_id FROM cart_sync_events
                WHERE household_id IN ({marks}) ORDER BY id""",
            present,
        ):
            snapshot = snapshots[row["household_id"]]
            event_ids.append(row["id"])
            changed[row["household_id"]].add(row["cart_id"])
            # Events are ordered, so the last one wins: the most recent actor per cart.
            snapshot["actors"][row["cart_id"]] = row["actor_id"]
            # Everyone who changed this cart in the batch, in order. Several members
            # can edit the same cart within one interval; notifications need them all.
            people = snapshot["cart_actors"].setdefault(row["cart_id"], [])
            if row["actor_id"] and row["actor_id"] not in people:
                people.append(row["actor_id"])
            snapshot["revision"] = row["id"]

    changed_cart_ids = sorted({cart for carts in changed.values() for cart in carts})
    if changed_cart_ids:
        for household, carts in changed.items():
            for cart_id in carts:
                if household in snapshots:
                    snapshots[household]["changed_carts"][cart_id] = []
        owner_of = {cart_id: household for household, carts in changed.items() for cart_id in carts}
        for row in db.execute(
            f"""SELECT cart_id, sku, contributor_id, quantity FROM cart_item_contributions
                WHERE cart_id IN ({_placeholders(changed_cart_ids)}) ORDER BY sku, contributor_id""",
            changed_cart_ids,
        ):
            household = owner_of.get(row["cart_id"])
            if household in snapshots:
                snapshots[household]["changed_carts"][row["cart_id"]].append({
                    "sku": row["sku"], "contributor_id": row["contributor_id"], "quantity": row["quantity"],
                })

    if lists_since is not None:
        cutoffs = {household: lists_since.get(household) for household in present}
        earliest = min((value for value in cutoffs.values() if value), default=None)
        query = f"""SELECT lists.household_id, {LIST_COLUMNS}, lists.archived_at IS NOT NULL AS archived
                    FROM shopping_lists AS lists
                    LEFT JOIN household_member_profiles AS creator
                      ON creator.household_id = lists.household_id AND creator.user_id = lists.created_by
                    WHERE lists.household_id IN ({marks})"""
        parameters: list = list(present)
        if earliest is not None and all(cutoffs.values()):
            query += " AND lists.updated_at > ?"
            parameters.append(earliest)
        lists_by_id = {}
        for row in db.execute(query + " ORDER BY lists.updated_at DESC, lists.id", parameters):
            cutoff = cutoffs[row["household_id"]]
            if cutoff and row["updated_at"] <= cutoff:
                continue
            entry = {
                "id": row["id"], "name": row["name"], "created_by": row["created_by"],
                "created_by_name": row["created_by_name"], "updated_at": row["updated_at"],
                "archived": bool(row["archived"]), "items": [],
            }
            lists_by_id[row["id"]] = entry
            snapshots[row["household_id"]]["lists"].append(entry)
        live_lists = [list_id for list_id, entry in lists_by_id.items() if not entry["archived"]]
        if live_lists:
            for row in db.execute(
                f"""SELECT {ITEM_COLUMNS} FROM shopping_list_items AS items {ITEM_JOINS}
                    WHERE items.list_id IN ({_placeholders(live_lists)})
                    ORDER BY items.created_at, items.id""",
                live_lists,
            ):
                item = dict(row)
                item["checked"] = bool(item["checked"])
                lists_by_id[item.pop("list_id")]["items"].append(item)

    return snapshots, event_ids


# ---------------------------------------------------------------------------
# Viewer helpers shared by the cart and list sockets.
# ---------------------------------------------------------------------------
class AccessEnded(Exception):
    """The viewer is no longer an active member; carries the socket close reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def viewer_role(snapshot: dict, user_id: str) -> bool:
    """Return True if the viewer is the primary; raise AccessEnded if they are not an active member."""
    member = next((member for member in snapshot["members"] if member["id"] == user_id), None)
    if member is None:
        raise AccessEnded("Household not found")
    if not member["active"]:
        raise AccessEnded(
            f"Your household access was revoked. Contact {snapshot['owner_name']}, the primary account holder."
        )
    return snapshot["owner_id"] == user_id


# ---------------------------------------------------------------------------
# The worker.
# ---------------------------------------------------------------------------
class LiveSync:
    def __init__(self, db_path: DatabaseTarget, redis_url: str, cache_url: str) -> None:
        self.db_path = db_path
        namespace = sha256(str(db_path).encode()).hexdigest()[:16]
        self.namespace = f"familycart:{namespace}"
        self.dirty_key = f"{self.namespace}:dirty"
        self.cursor_key = f"{self.namespace}:list-cursor"
        self.online_prefix = f"{CACHE_PREFIX}:online:{namespace}"
        self.interval = sync_interval()
        self.sweep_every = sweep_interval()
        self.redis = sync_client(redis_url)
        self.cache = sync_client(cache_url)
        options = {"decode_responses": True, "socket_connect_timeout": 0.5, "socket_timeout": 3}
        self.subscriber = AsyncRedis.from_url(redis_url, **options)
        self.async_cache = AsyncRedis.from_url(cache_url, **options)
        self.breaker = Breaker("live-sync")
        self._marker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-dirty")
        self._force_sweep = True  # catch up on anything left from before a restart
        self._last_sweep_at: str | None = None
        self._last_sweep = 0.0
        # list_id -> updated_at already published. The look-back window can find a
        # list again; if it has not changed since, it is not sent twice.
        self._sent_lists: dict[str, str] = {}
        self.stats = {"ticks": 0, "idle_ticks": 0, "households_published": 0, "households_skipped_offline": 0}
        global _listener
        _listener = self.mark_dirty

    def channel(self, household_id: str) -> str:
        return f"{self.namespace}:household:{household_id}"

    def online_key(self, household_id: str) -> str:
        return f"{self.online_prefix}:{household_id}"

    # -- write path ---------------------------------------------------------
    def mark_dirty(self, household_id: str) -> None:
        """Called after a commit. Never blocks the request: the Redis write happens on a worker thread."""
        def write() -> None:
            if not self.breaker.available():
                self._force_sweep = True
                return
            try:
                with self.redis.pipeline(transaction=False) as pipe:
                    pipe.sadd(self.dirty_key, household_id)
                    # Notes left by a worker that is gone expire; the database sweep covers them.
                    pipe.expire(self.dirty_key, int(self.sweep_every * 2))
                    pipe.execute()
            except RedisError as error:
                self.breaker.failed(error)
                self._force_sweep = True

        self._marker.submit(write)

    async def mark_online(self, household_id: str) -> None:
        """Called by open sockets; the key expires on its own when nobody refreshes it."""
        try:
            await self.async_cache.set(self.online_key(household_id), "1", ex=int(self.interval * 2 + 5))
        except RedisError as error:
            self.breaker.failed(error)

    # -- worker ---------------------------------------------------------------
    def _take_dirty(self) -> set[str]:
        if not self.breaker.available():
            self._force_sweep = True
            return set()
        try:
            with self.redis.pipeline(transaction=True) as pipe:
                pipe.smembers(self.dirty_key)
                pipe.delete(self.dirty_key)
                members, _ = pipe.execute()
            return set(members)
        except RedisError as error:
            self.breaker.failed(error)
            self._force_sweep = True
            return set()

    def _restore_dirty(self, households: Iterable[str]) -> None:
        households = list(households)
        if not households:
            return
        try:
            self.redis.sadd(self.dirty_key, *households)
        except RedisError:
            self._force_sweep = True

    def _online(self, households: list[str]) -> set[str]:
        try:
            values = self.cache.mget([self.online_key(household) for household in households])
            return {household for household, value in zip(households, values) if value}
        except RedisError as error:
            # Without presence information, assume everyone is watching (safe, just more work).
            self.breaker.failed(error)
            return set(households)

    def _list_cursors(self, households: list[str], started: datetime) -> dict[str, str]:
        fallback = (started - timedelta(seconds=self.interval) - LIST_OVERLAP).isoformat()
        try:
            stored = self.cache.hmget(self.cursor_key, households) if households else []
        except RedisError:
            stored = [None] * len(households)
        cursors = {}
        for household, value in zip(households, stored):
            # Look back a little past the previous batch in case a transaction was still committing.
            cursors[household] = (datetime.fromisoformat(value) - LIST_OVERLAP).isoformat() if value else fallback
        return cursors

    def _sweep(self, db: DatabaseConnection) -> set[str]:
        """Slow safety net: find changes whose Redis note was lost."""
        found = {row["household_id"] for row in db.execute("SELECT DISTINCT household_id FROM cart_sync_events")}
        since = self._last_sweep_at
        query = "SELECT DISTINCT household_id FROM shopping_lists"
        parameters: tuple = ()
        if since:
            query += " WHERE updated_at > ?"
            parameters = ((datetime.fromisoformat(since) - LIST_OVERLAP).isoformat(),)
        found.update(row["household_id"] for row in db.execute(query, parameters))
        return found

    def tick(self) -> None:
        """One batch. Does no database work at all when nothing changed."""
        self.stats["ticks"] += 1
        started = datetime.now(timezone.utc)
        dirty = self._take_dirty()
        sweep_due = self._force_sweep or time.monotonic() - self._last_sweep >= self.sweep_every
        if not dirty and not sweep_due:
            self.stats["idle_ticks"] += 1
            return

        published: list[str] = []
        households: list[str] = []
        # Where the previous successful sweep got to. It only moves forward once a
        # whole batch has succeeded, so a failed batch is fully retried next time.
        swept_until = self._last_sweep_at
        try:
            with transaction(self.db_path, write=True) as db:
                if sweep_due:
                    dirty |= self._sweep(db)
                if dirty:
                    households = sorted(dirty)
                    online = self._online(households)
                    watched = [household for household in households if household in online]
                    self.stats["households_skipped_offline"] += len(households) - len(watched)
                    cursors = self._list_cursors(watched, started)
                    if sweep_due:
                        # Changes found by the sweep may be older than the last batch
                        # (e.g. made during a Redis outage): look back to the previous
                        # sweep, or include every list if this is the first one.
                        lookback = (
                            (datetime.fromisoformat(swept_until) - LIST_OVERLAP).isoformat() if swept_until else None
                        )
                        cursors = {
                            household: (min(cursor, lookback) if lookback else None)
                            for household, cursor in cursors.items()
                        }
                    snapshots, event_ids = build_snapshots(db, watched, lists_since=cursors)
                else:
                    online, snapshots, event_ids = set(), {}, []
                for snapshot in snapshots.values():
                    snapshot["lists"] = [
                        entry for entry in snapshot["lists"]
                        if self._sent_lists.get(entry["id"]) != entry["updated_at"]
                    ]
                if snapshots:
                    with self.redis.pipeline(transaction=False) as pipe:
                        for household, snapshot in snapshots.items():
                            pipe.publish(self.channel(household), json.dumps(snapshot))
                        pipe.execute()
                    published = list(snapshots)

                # Cart events are deleted only after publishing. Offline households'
                # events are dropped too: their members load fresh data when they open the app.
                offline = [household for household in households if household not in online]
                if offline:
                    db.execute(
                        f"DELETE FROM cart_sync_events WHERE household_id IN ({_placeholders(offline)})", offline,
                    )
                for start in range(0, len(event_ids), 500):
                    chunk = event_ids[start:start + 500]
                    db.execute(f"DELETE FROM cart_sync_events WHERE id IN ({_placeholders(chunk)})", chunk)
        except SYNC_ERRORS:
            self._restore_dirty(dirty)
            if sweep_due:
                self._force_sweep = True  # retry the sweep from the same point
            raise

        if sweep_due:
            self._force_sweep = False
            self._last_sweep = time.monotonic()
            self._last_sweep_at = started.isoformat()

        if households:
            try:
                with self.cache.pipeline(transaction=False) as pipe:
                    pipe.hset(self.cursor_key, mapping={household: started.isoformat() for household in households})
                    pipe.expire(self.cursor_key, 86400)
                    pipe.execute()
            except RedisError:
                pass
        self.stats["households_published"] += len(published)
        for household in published:
            for entry in snapshots[household]["lists"]:
                self._sent_lists[entry["id"]] = entry["updated_at"]
        # Forget entries older than the look-back window; they can no longer be found again.
        horizon = (started - timedelta(seconds=self.interval) - LIST_OVERLAP * 2).isoformat()
        self._sent_lists = {key: value for key, value in self._sent_lists.items() if value > horizon}

    async def run(self) -> None:
        failed = False
        while True:
            await asyncio.sleep(self.interval)
            try:
                await asyncio.to_thread(self.tick)
                failed = False
            except SYNC_ERRORS as error:
                if not failed:
                    logger.warning("Live updates will retry: %s", error)
                failed = True

    async def close(self) -> None:
        global _listener
        if _listener == self.mark_dirty:
            _listener = None
        self._marker.shutdown(wait=False)
        await self.subscriber.aclose()
        await self.async_cache.aclose()
