import json
import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi.testclient import TestClient

from backend.api_gateway.app import app as gateway_app
from backend.services.auth_service.app import app as auth_app
from backend.services.familycart_service import checkout
from backend.services.familycart_service.app import app as cart_app, live_view, resolve_address_pin
from backend.services.familycart_service.db import initialize_database, transaction
from backend.services.maps_service.app import app as maps_app
from backend.services.refresh_service.service import create_session, rotate_session
from backend.services.user_service.db import initialize_database as initialize_users
from backend.shared.database import DatabaseConnection, connect_database
from backend.shared.tokens import Identity

API = "/api/v1"
HOUSEHOLD = f"{API}/households/demo-household"
REGULAR_CART = f"{HOUSEHOLD}/carts/regular-cart"
DUBAI = {"latitude": 25.2048, "longitude": 55.2708}
AL_AIN = {"latitude": 24.2075, "longitude": 55.7447}
SHARED = {"kind": "household", "household_id": "demo-household", "cart_id": "regular-cart"}


def authorization(tokens):
    return {"Authorization": f"Bearer {tokens.access_token}"}


class CheckoutTests(unittest.TestCase):
    def isolated_postgres(self, url):
        schema = f"checkout_test_{secrets.token_hex(8)}"
        admin = connect_database(url)
        self.addCleanup(admin.close)
        with admin:
            admin.execute(f"CREATE SCHEMA {schema}")
        def cleanup():
            with admin:
                admin.execute(f"DROP SCHEMA {schema} CASCADE")
        self.addCleanup(cleanup)
        parts = urlsplit(url)
        options = dict(parse_qsl(parts.query))
        options["options"] = f"-csearch_path={schema}"
        return urlunsplit(parts._replace(query=urlencode(options)))

    def setUp(self):
        folder = Path(self.enterContext(TemporaryDirectory()))
        postgres = os.environ.get("FAMILYCART_CHECKOUT_TEST_DATABASE_URL")
        self.db = self.isolated_postgres(postgres) if postgres else folder / "carts.sqlite3"
        self.users = self.isolated_postgres(postgres) if postgres else folder / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(self.db), "FAMILYCART_DATABASE_URL": self.db if isinstance(self.db, str) else "",
            "FAMILYCART_USER_DB_PATH": str(self.users),
            "FAMILYCART_USER_DATABASE_URL": self.users if isinstance(self.users, str) else "",
            "FAMILYCART_JWT_SECRET": self.secret, "FAMILYCART_DEMO_CHECKOUT": "1",
            "FAMILYCART_GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
        }))
        initialize_users(self.users)
        self.owner = self.user("user-primary")
        self.member = self.user("user-adult")
        self.minor = self.user("user-child", adult=False)
        self.maps = self.enterContext(TestClient(maps_app))
        self.cart = self.enterContext(TestClient(cart_app, raise_server_exceptions=False))
        self.map_calls = []
        self.maps_barrier = None
        upstream = self.enterContext(httpx.Client(transport=httpx.MockTransport(self.forward_maps)))
        self.enterContext(patch.object(cart_app.state, "maps_http", upstream))
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE carts SET inventory_store_id = 'uae-dubai' WHERE household_id = 'demo-household'")

    def user(self, user_id, adult=True):
        return authorization(create_session(self.users, self.secret, Identity(user_id, adult), user_id))

    def forward_maps(self, request):
        self.map_calls.append(request)
        if self.maps_barrier:
            self.maps_barrier.wait(timeout=10)
        return self.maps.post(request.url.path, content=request.content, headers={"Content-Type": "application/json"})

    def address(self, pin=DUBAI, *, headers=None, **changes):
        response = self.cart.post(f"{HOUSEHOLD}/addresses", headers=headers or self.owner, json={
            "label": "Home", "recipient_name": "Demo Customer", "address_line1": "10 Demo Street", "city": "UAE",
            **pin, **changes,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return {"kind": "household", "address_id": response.json()["id"]}

    def set_store(self, address, *, cart=REGULAR_CART, headers=None):
        return self.cart.put(f"{cart}/inventory-store", headers=headers or self.owner, json=address)

    def item(self, sku="milk", *, headers=None, quantity=1, cart=REGULAR_CART):
        response = self.cart.patch(f"{cart}/items/{sku}", headers=headers or self.owner, json={"delta": quantity})
        self.assertEqual(response.status_code, 200, response.text)

    def contents(self, cart=REGULAR_CART, headers=None):
        rows = self.cart.get(cart, headers=headers or self.owner).json()["contributions"]
        return [(row["sku"], row["quantity"]) for row in rows]

    def second_cart(self):
        """Another household cart, which every member may use."""
        response = self.cart.post(f"{HOUSEHOLD}/carts", headers=self.owner, json={"name": "Second"})
        self.assertEqual(response.status_code, 201, response.text)
        cart_id = response.json()["id"]
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE carts SET inventory_store_id = 'uae-dubai' WHERE id = ?", (cart_id,))
        return f"{HOUSEHOLD}/carts/{cart_id}", {**SHARED, "cart_id": cart_id}

    def review(self, address, *, cart=SHARED, overrides=None, headers=None, client=None):
        body = {"cart": cart, "default_address": address}
        if overrides is not None:
            body["overrides"] = overrides
        return (client or self.cart).post(f"{API}/checkout/quotes", headers=headers or self.owner, json=body)

    def quote(self, *args, **kwargs):
        response = self.review(*args, **kwargs)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def place(self, quote, key="submission", *, outcome=None, headers=None, client=None):
        body = {"quote_id": quote["quote_id"]}
        if outcome:
            body["mock_outcome"] = outcome
        return (client or self.cart).post(
            f"{API}/checkout/orders", headers={**(headers or self.owner), "Idempotency-Key": key}, json=body,
        )

    def assert_problem(self, response, status, code):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["detail"]["code"], code)

    def stock(self, sku="milk", store="uae-dubai"):
        with transaction(self.db) as db:
            return db.execute(
                "SELECT quantity FROM store_inventory WHERE store_id = ? AND sku = ?", (store, sku),
            ).fetchone()["quantity"]

    def set_stock(self, sku, quantity, store="uae-dubai"):
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE store_inventory SET quantity = ? WHERE store_id = ? AND sku = ?",
                       (quantity, store, sku))

    def grant_member(self):
        response = self.cart.put(f"{REGULAR_CART}/grants/user-adult", headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)

    def test_each_store_seeds_all_products_with_independent_stock(self):
        stores = {}
        with transaction(self.db) as db:
            for row in db.execute("SELECT store_id, sku, quantity FROM store_inventory"):
                stores.setdefault(row["store_id"], {})[row["sku"]] = row["quantity"]

        self.assertEqual(set(stores), {"uae-abu-dhabi", "uae-al-ain", "uae-dubai", "uae-ras-al-khaimah"})
        for quantities in stores.values():
            self.assertEqual(quantities.keys(), cart_app.state.products.keys())
            self.assertTrue(all(10 <= quantity <= 20 for quantity in quantities.values()))
        self.assertEqual(len({tuple(sorted(quantities.items())) for quantities in stores.values()}), 4)

    def test_order_through_gateway_replays_and_survives_restart(self):
        starting_stock = self.stock()
        address = self.address()
        self.item(quantity=2)
        gateway = self.enterContext(TestClient(gateway_app))
        transport = httpx.ASGITransport(app=cart_app)
        async def forward(request):
            return await transport.handle_async_request(request)
        upstream = httpx.AsyncClient(transport=httpx.MockTransport(forward))
        self.addCleanup(gateway.portal.call, upstream.aclose)
        self.enterContext(patch.object(gateway_app.state, "http", upstream))

        self.assertEqual(gateway.post(f"{API}/checkout/quotes", json={}).status_code, 401)
        quote = self.quote(address, client=gateway)
        self.assertEqual((quote["currency"], quote["total_fils"], quote["delivery_fils"]), ("AED", 1500, 0))
        self.assertEqual(gateway.post(f"{API}/checkout/orders", headers=self.owner,
                                     json={"quote_id": quote["quote_id"]}).status_code, 422)
        first = self.place(quote, client=gateway)
        self.assertEqual(first.status_code, 201, first.text)
        placed = first.json()
        path = f"{API}/orders/{placed['order_id']}"
        self.assertEqual((placed["status"], self.stock()), ("PLACED", starting_stock - 2))
        self.assertEqual(self.contents(), [])
        self.assertEqual(gateway.get(path, headers=self.member).status_code, 404)
        self.address(recipient_name="Second address")  # a member always keeps one
        self.assertEqual(self.cart.delete(f"{HOUSEHOLD}/addresses/{address['address_id']}",
                                          headers=self.owner).status_code, 204)
        initialize_database(self.db)
        with transaction(self.db, write=True) as db:
            db.execute("INSERT INTO store_inventory (store_id, sku, quantity) VALUES (?, ?, ?)",
                       ("uae-dubai", "retired-product", 100))
            checkout.seed_inventory(db, cart_app.state.products)
            self.assertIsNone(db.execute("SELECT sku FROM store_inventory WHERE sku = ?",
                                         ("retired-product",)).fetchone())
            db.execute("UPDATE checkout_quotes SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", quote["quote_id"]))
        self.assertEqual(gateway.get(path, headers=self.owner).json(), placed)
        self.assertEqual(gateway.get(f"{API}/orders", headers=self.owner).json(), {"orders": [{
            "order_id": placed["order_id"], "order_number": placed["order_number"], "placed_at": placed["placed_at"],
            "currency": "AED", "total_fils": 1500, "status": "PLACED", "is_shared": True, "delivery_count": 1,
        }], "next_cursor": None})
        self.assertEqual(gateway.get(f"{API}/orders", headers=self.member).json(), {"orders": [], "next_cursor": None})
        self.assertEqual(gateway.get(f"{API}/orders").status_code, 401)
        calls = len(self.map_calls)
        replay = self.place(quote, client=gateway)
        self.assertEqual((replay.status_code, replay.json()), (200, placed))
        self.assertEqual(len(self.map_calls), calls)
        self.assertEqual(self.stock(), starting_stock - 2)
        self.assert_problem(self.place(quote, "changed-key"), 409, "QUOTE_ALREADY_USED")
        self.assert_problem(self.place(quote, outcome="decline"), 409, "IDEMPOTENCY_KEY_REUSED")

    def test_new_quotes_require_one_address_and_existing_split_quote_can_finish(self):
        dubai_stock = self.stock()
        al_ain_stock = self.stock(store="uae-al-ain")
        self.enterContext(patch.object(cart_app.state.live_sync, "publish_now"))  # leave the event to look at
        self.grant_member()
        dubai = self.address()
        al_ain = self.address(AL_AIN, headers=self.member, recipient_name="Al Ain Recipient")
        self.item(quantity=2)
        self.item(headers=self.member)
        combined = self.quote(dubai, cart=SHARED)["delivery_groups"]
        self.assertEqual(len(combined), 1)
        self.assertEqual({line["contributor_id"]: line["quantity"] for line in combined[0]["lines"]},
                         {"user-primary": 2, "user-adult": 1})
        overrides = [{"contributor_id": "user-adult", "address": al_ain}]
        self.assert_problem(self.review(dubai, overrides=overrides), 422, "SINGLE_ADDRESS_REQUIRED")

        # Simulate a still-valid split quote persisted before the store assignment column existed.
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE carts SET inventory_store_id = NULL WHERE id = 'regular-cart'")
        selection = checkout.QuoteInput.model_validate({"cart": SHARED, "default_address": dubai, "overrides": overrides})
        caller = Identity("user-primary", True)
        with transaction(self.db) as db:
            state = checkout.snapshot(db, selection, caller, cart_app.state.products, lock=False)
        request = SimpleNamespace(app=cart_app)
        groups = checkout.resolve_groups(state["groups"], lambda lat, lon: resolve_address_pin(request, lat, lon))
        with transaction(self.db, write=True) as db:
            state.pop("inventory_store_id")
            expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            db.execute("INSERT INTO checkout_quotes VALUES (?, ?, ?, ?)", (
                "legacy-quote", caller.user_id, expires, json.dumps({"state": state, "groups": groups}),
            ))
        quote = {"quote_id": "legacy-quote"}

        first = self.place(quote)
        self.assertEqual(first.status_code, 201, first.text)
        placed = first.json()
        groups = {group["store_id"]: group for group in placed["delivery_groups"]}
        self.assertEqual({key: group["subtotal_fils"] for key, group in groups.items()},
                         {"uae-al-ain": 750, "uae-dubai": 1500})
        self.assertEqual(groups["uae-al-ain"]["address"]["recipient_name"], "Al Ain Recipient")
        self.assertEqual(placed["total_fils"], 2250)
        self.assertEqual((self.stock(), self.stock(store="uae-al-ain")), (dubai_stock - 2, al_ain_stock - 1))
        self.assertEqual(self.cart.get(REGULAR_CART, headers=self.member).json()["contributions"], [])
        with transaction(self.db) as db:
            event = db.execute("SELECT cart_id, actor_id, reason FROM cart_sync_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(dict(event), {"cart_id": "regular-cart", "actor_id": "user-primary", "reason": "order"})
        summary = self.cart.get(f"{API}/orders", headers=self.owner).json()["orders"][0]
        self.assertTrue(summary["is_shared"])
        self.assertEqual(summary["delivery_count"], 2)

    def test_only_selected_products_are_ordered(self):
        address = self.address()
        self.grant_member()
        self.item(quantity=2)
        self.item("eggs")
        self.item("bread", headers=self.member)

        def review(skus):
            return self.cart.post(f"{API}/checkout/quotes", headers=self.owner, json={
                "cart": SHARED, "default_address": address, "skus": skus,
            })

        self.assertEqual(review([]).status_code, 422)
        self.assert_problem(review(["milk", "honey"]), 409, "SELECTION_CHANGED")
        quote = review(["milk", "bread"]).json()
        lines = [line["sku"] for group in quote["delivery_groups"] for line in group["lines"]]
        self.assertEqual((sorted(lines), quote["total_fils"]), (["bread", "milk"], 1500 + 390))
        stale = review(["bread"]).json()
        placed = self.place(quote)
        self.assertEqual(placed.status_code, 201, placed.text)
        left = self.cart.get(REGULAR_CART, headers=self.owner).json()["contributions"]
        self.assertEqual([(row["sku"], row["quantity"]) for row in left], [("eggs", 1)])
        # The other review names a product that has been ordered since.
        self.assert_problem(self.place(stale, "second"), 409, "QUOTE_STALE")

    def test_order_history_newest_first_cursor_is_purchaser_scoped(self):
        address = self.address()
        order_ids = []
        for index in range(3):
            self.item()
            order = self.place(self.quote(address), key=f"order-{index}")
            self.assertEqual(order.status_code, 201, order.text)
            order_ids.append(order.json()["order_id"])
        cursor = None
        for expected in reversed(order_ids):
            params = {"limit": 1, **({"before": cursor} if cursor else {})}
            page = self.cart.get(f"{API}/orders", headers=self.owner, params=params).json()
            self.assertEqual([order["order_id"] for order in page["orders"]], [expected])
            cursor = page["next_cursor"]
            self.assertEqual(cursor, expected if expected != order_ids[0] else None)
        self.assert_problem(self.cart.get(f"{API}/orders", headers=self.member, params={"before": order_ids[0]}),
                            422, "INVALID_ORDER_CURSOR")

    def test_checkout_permissions_and_address_concealment(self):
        self.grant_member()
        self.item()
        dubai = self.address()
        private = self.address(headers=self.member)
        quote = self.quote(dubai, cart=SHARED)
        self.assert_problem(self.review(dubai, cart=SHARED, headers=self.member), 404, "ADDRESS_NOT_FOUND")
        self.assert_problem(self.review(dubai, cart=SHARED, headers=self.minor), 403, "AGE_ELIGIBILITY_REQUIRED")
        self.assert_problem(self.review(dubai, cart=SHARED, headers=self.user("unrelated")), 404, "CART_NOT_FOUND")
        # The admin may deliver to any member's address; members only to their own (checked above).
        self.assertEqual(self.review(private, cart=SHARED).status_code, 201)
        self.assert_problem(self.place(quote, headers=self.member), 404, "QUOTE_NOT_FOUND")
        self.assert_problem(self.place(quote, headers=self.minor), 403, "AGE_ELIGIBILITY_REQUIRED")
        body = {"cart": SHARED, "default_address": dubai, "is_adult": True}
        self.assertEqual(self.cart.post(f"{API}/checkout/quotes", headers=self.minor, json=body).status_code, 422)
        self.assertEqual(self.cart.put(f"{HOUSEHOLD}/members/user-adult/primary", headers=self.owner).status_code, 200)
        self.assertEqual(self.cart.delete(f"{HOUSEHOLD}/members/user-primary", headers=self.member).status_code, 204)
        self.assert_problem(self.place(quote), 404, "CART_NOT_FOUND")

    def test_invalid_coverage_identifies_selected_address(self):
        starting_stock = self.stock()
        self.grant_member()
        self.item()
        self.item(headers=self.member)
        for pin, status, code in [
            ({}, 409, "ADDRESS_NEEDS_PIN"),
            ({"latitude": 0, "longitude": 0}, 409, "OUTSIDE_DELIVERY_AREA"),
        ]:
            outside = self.address(pin)
            error = self.review(outside)
            self.assert_problem(error, status, code)
            groups = error.json()["detail"]["delivery_groups"]
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["address_ref"], outside)
            self.assertEqual(groups[0]["problem"], code)
        quote = self.quote(self.address())
        with patch.object(cart_app.state.maps_http, "post", side_effect=httpx.ConnectError("Maps offline")):
            self.assert_problem(self.place(quote), 503, "MAPS_UNAVAILABLE")
        self.assertEqual(self.stock(), starting_stock)

    def test_stale_inputs_expiry_and_second_quote(self):
        address = self.address()
        self.item()
        edited = self.quote(address)
        # Restoring the same quantities still advances the cart revision.
        self.item()
        self.item(quantity=-1)
        self.assert_problem(self.place(edited), 409, "QUOTE_STALE")
        moved = self.quote(address)
        target = f"{HOUSEHOLD}/addresses/{address['address_id']}"
        self.assertEqual(self.cart.patch(target, headers=self.owner, json=AL_AIN).status_code, 200)
        self.assert_problem(self.place(moved), 409, "QUOTE_STALE")
        self.assert_problem(self.review(address), 409, "CART_STORE_MISMATCH")
        switched = self.set_store(address)
        self.assertEqual(switched.status_code, 200, switched.text)
        priced = self.quote(address)
        product = cart_app.state.products["milk"]
        with patch.dict(cart_app.state.products, {"milk": product.model_copy(update={"price_fils": 999})}):
            self.assert_problem(self.place(priced), 409, "QUOTE_STALE")
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE checkout_quotes SET expires_at = ? WHERE id = ?",
                       ("2000-01-01T00:00:00+00:00", priced["quote_id"]))
        first, second = self.quote(address), self.quote(address)
        self.assert_problem(self.place(priced), 409, "QUOTE_EXPIRED")
        self.assertEqual(self.place(first).status_code, 201)
        self.assert_problem(self.place(second, "new-key"), 409, "QUOTE_STALE")

    def test_unrelated_cart_edits_do_not_invalidate_quote(self):
        self.grant_member()
        other, _ = self.second_cart()
        self.item()
        address = self.address()
        quote = self.quote(address)
        self.item(cart=other)
        self.item(cart=other, headers=self.member)
        self.assertEqual(self.place(quote).status_code, 201)

    def test_decline_and_fault_after_first_deduction_rollback(self):
        starting_stock = (self.stock(), self.stock("eggs"))
        self.item("eggs")
        self.item()
        quote = self.quote(self.address())
        self.assert_problem(self.place(quote, "decline", outcome="decline"), 402, "MOCK_PAYMENT_DECLINED")
        self.assertEqual((self.stock(), self.stock("eggs")), starting_stock)
        original = DatabaseConnection.execute
        updates = []
        def fail_second(db, query, parameters=None):
            if query.lstrip().startswith("UPDATE store_inventory SET quantity = quantity -"):
                updates.append(parameters[2])
                if len(updates) == 2:
                    raise RuntimeError("Injected inventory write failure")
            return original(db, query, parameters)
        with patch.object(DatabaseConnection, "execute", fail_second):
            self.assertEqual(self.place(quote, "retry").status_code, 500)
        self.assertEqual(updates, ["eggs", "milk"])
        self.assertEqual((self.stock(), self.stock("eggs")), starting_stock)
        self.assertEqual(len(self.contents()), 2)
        self.assertEqual(self.place(quote, "retry").status_code, 201)
        self.assertEqual((self.stock(), self.stock("eggs")), tuple(quantity - 1 for quantity in starting_stock))

    def test_store_inventory_covers_all_household_contributions(self):
        self.set_stock("honey", 17)
        self.grant_member()
        self.item("honey", quantity=10)
        result = self.cart.patch(f"{REGULAR_CART}/items/honey", headers=self.member, json={"delta": 8})
        self.assert_problem(result, 409, "OUT_OF_STOCK")
        self.assertEqual(result.json()["detail"] | {"message": None}, {
            "code": "OUT_OF_STOCK", "message": None, "sku": "honey", "store_id": "uae-dubai",
            "requested": 18, "available": 17,
        })
        self.item("honey", quantity=7, headers=self.member)
        self.set_stock("honey", 16)
        result = self.review(self.address())
        self.assert_problem(result, 409, "OUT_OF_STOCK")
        shortages = result.json()["detail"]["shortages"]
        self.assertEqual([(row["group_number"], row["requested"], row["available"]) for row in shortages], [(1, 17, 16)])
        self.assertEqual(self.stock("honey"), 16)
        self.assertEqual(len(self.cart.get(REGULAR_CART, headers=self.owner).json()["contributions"]), 2)

    def test_delivery_location_and_inventory_follow_cart_and_address_access(self):
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE carts SET inventory_store_id = NULL WHERE id = 'regular-cart'")
        self.grant_member()
        self.cart.put(f"{REGULAR_CART}/grants/user-child", headers=self.owner).raise_for_status()
        inventory = f"{REGULAR_CART}/inventory"
        self.assertEqual(self.cart.get(inventory, headers=self.minor).json(),
                         {"store_id": None, "store_name": None, "stock": {}})
        item = f"{REGULAR_CART}/items/milk"
        for method, path, body in [
            ("PATCH", item, {"delta": 1}),
            ("PUT", f"{item}/contributions/me", {"quantity": 1}),
            ("PATCH", f"{item}/contributions/me", {"delta": 1}),
        ]:
            self.assert_problem(self.cart.request(method, path, headers=self.owner, json=body), 409, "CART_STORE_REQUIRED")
        address = self.address()
        self.assert_problem(self.set_store(address, headers=self.member), 404, "ADDRESS_NOT_FOUND")
        self.assertEqual(self.set_store(address, headers=self.minor).status_code, 403)
        self.assert_problem(self.set_store(self.address({})), 409, "ADDRESS_NEEDS_PIN")
        outside = self.address({"latitude": 0, "longitude": 0})
        self.assert_problem(self.set_store(outside), 409, "OUTSIDE_DELIVERY_AREA")
        saved = self.set_store(address)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["inventory_store_id"], "uae-dubai")
        connection = SimpleNamespace(app=cart_app)
        live = live_view(connection, "demo-household", Identity("user-child", False), "regular-cart")
        self.assertEqual(live["cart"]["inventory_store_name"], "Dubai")
        self.assertNotIn(address["address_id"], [row["id"] for row in live["addresses"]])
        stock = self.cart.get(inventory, headers=self.minor).json()
        self.assertEqual((stock["store_name"], stock["stock"]["milk"]), ("Dubai", self.stock()))
        self.assertNotIn("cigarette-pack-20", stock["stock"])
        adult_cart = f"{HOUSEHOLD}/carts/adults-only-cart"
        self.assertEqual(self.cart.get(f"{adult_cart}/inventory", headers=self.minor).status_code, 404)
        self.assertIn("cigarette-pack-20", self.cart.get(f"{adult_cart}/inventory", headers=self.owner).json()["stock"])
        self.item(headers=self.minor)
        with transaction(self.db, write=True) as db:
            db.execute("DELETE FROM store_inventory WHERE store_id = 'uae-dubai' AND sku = 'milk'")
        self.assertEqual(self.cart.get(inventory, headers=self.minor).json()["stock"]["milk"], 0)
        self.assert_problem(self.cart.patch(item, headers=self.minor, json={"delta": 1}), 409, "OUT_OF_STOCK")

    def test_legacy_overstock_can_be_reduced_and_other_items_can_be_ordered(self):
        self.set_stock("milk", 17)
        self.set_stock("eggs", 10)
        address = self.address()
        with transaction(self.db, write=True) as db:
            db.execute("UPDATE carts SET inventory_store_id = NULL WHERE id = 'regular-cart'")
            db.execute("""INSERT INTO cart_item_contributions (household_id, cart_id, sku, contributor_id, quantity)
                          VALUES ('demo-household', 'regular-cart', 'milk', 'user-primary', 22)""")
        self.assert_problem(self.review(address), 409, "OUT_OF_STOCK")
        self.set_store(address).raise_for_status()
        reduced = self.cart.put(f"{REGULAR_CART}/items/milk/contributions/me", headers=self.owner, json={"quantity": 21})
        self.assertEqual(reduced.status_code, 200, reduced.text)
        self.item("eggs", quantity=10)
        quote = self.cart.post(f"{API}/checkout/quotes", headers=self.owner, json={
            "cart": SHARED, "default_address": address, "skus": ["eggs"],
        })
        self.assertEqual(quote.status_code, 201, quote.text)
        self.assertEqual(self.place(quote.json()).status_code, 201)
        self.assertEqual((self.contents(), self.stock("eggs"), self.stock()), ([("milk", 21)], 0, 17))

    def test_stock_limit_and_deduction_apply_per_product(self):
        self.set_stock("milk", 17)
        self.set_stock("eggs", 10)
        self.item("milk", quantity=12)
        self.item("eggs", quantity=10)
        quote = self.quote(self.address())
        self.set_stock("milk", 11)
        self.assert_problem(self.place(quote), 409, "OUT_OF_STOCK")
        self.assertEqual((self.stock("eggs"), len(self.contents())), (10, 2))
        self.assertEqual(self.cart.get(f"{API}/orders", headers=self.owner).json()["orders"], [])
        self.set_stock("milk", 17)
        placed = self.place(quote)
        self.assertEqual(placed.status_code, 201, placed.text)
        self.assertEqual((self.stock(), self.stock("eggs"), self.contents()), (5, 0, []))
        with transaction(self.db, write=True) as db:
            checkout.seed_inventory(db, cart_app.state.products)
        self.assertEqual(self.stock("eggs"), 0)

    def test_shared_and_private_list_transfers_only_mark_rows_that_fit(self):
        self.set_stock("milk", 17)
        self.item(quantity=17)
        for base in (f"{HOUSEHOLD}/lists", f"{API}/my-lists"):
            with self.subTest(base=base):
                created = self.cart.post(base, headers=self.owner, json={"name": "Stock test"})
                created.raise_for_status()
                path = f"{base}/{created.json()['id']}"
                items = [self.cart.post(f"{path}/items", headers=self.owner, json={"sku": sku}).json()
                         for sku in ("milk", "eggs")]
                result = self.cart.post(f"{path}/move-to-cart", headers=self.owner, json={
                    "target": "regular-cart", "item_ids": [item["id"] for item in items],
                    **({"household_id": "demo-household"} if base.endswith("my-lists") else {}),
                })
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["moved"], [items[1]["id"]])
                self.assertEqual([row["id"] for row in result.json()["skipped"]], [items[0]["id"]])
                self.assertIn("Out of stock", result.json()["skipped"][0]["reason"])
                detail = self.cart.get(path, headers=self.owner).json()
                self.assertEqual({row["sku"]: row["checked"] for row in detail["items"]}, {"milk": False, "eggs": True})

    def test_concurrent_same_key_returns_same_order(self):
        starting_stock = self.stock()
        self.item()
        quote = self.quote(self.address())
        self.maps_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(lambda _: self.place(quote), range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 201])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(self.stock(), starting_stock - 1)

    def test_concurrent_carts_compete_for_last_unit(self):
        self.set_stock("honey", 1)
        second = self.cart.post(f"{API}/households", headers=self.member, json={
            "name": "Other Household", "display_name": "Member",
            "address": {"label": "Home", "recipient_name": "Member", "address_line1": "1 Street", "city": "Dubai", **DUBAI},
        })
        self.assertEqual(second.status_code, 201, second.text)
        household_id = second.json()["id"]
        household = f"{API}/households/{household_id}"
        [cart] = self.cart.get(f"{household}/carts", headers=self.member).json()
        other = f"{household}/carts/{cart['id']}"
        other_ref = {**SHARED, "household_id": household_id, "cart_id": cart["id"]}
        [address] = self.cart.get(f"{household}/addresses", headers=self.member).json()
        member_address = {"kind": "household", "address_id": address["id"]}
        assigned = self.set_store(member_address, cart=other, headers=self.member)
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.item("honey")
        self.item("honey", headers=self.member, cart=other)
        owner_quote = self.quote(self.address())
        member_quote = self.quote(member_address, cart=other_ref, headers=self.member)
        self.maps_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as workers:
            owner = workers.submit(self.place, owner_quote)
            member = workers.submit(self.place, member_quote, headers=self.member)
            responses = [owner.result(), member.result()]
        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        lose, headers = [(REGULAR_CART, self.owner), (other, self.member)][
            next(i for i, response in enumerate(responses) if response.status_code == 409)
        ]
        self.assert_problem(next(response for response in responses if response.status_code == 409), 409, "OUT_OF_STOCK")
        self.assertEqual(self.stock("honey"), 0)
        self.assertEqual(self.contents(lose, headers=headers), [("honey", 1)])

    def test_authoritative_age_and_disabled_mock_override(self):
        tokens = create_session(self.users, self.secret, Identity("new-google-user", False), "New user",
                                google_subject="new-google-user")
        headers = authorization(tokens)
        # A member from before age checks existed, so only the account's own age decides.
        with transaction(self.db, write=True) as db:
            db.execute("""INSERT INTO household_members (household_id, user_id, display_name)
                          VALUES ('demo-household', 'new-google-user', 'New user')""")
        self.assertEqual(self.cart.put(f"{REGULAR_CART}/grants/new-google-user", headers=self.owner).status_code, 200)
        address = self.address(headers=headers)
        self.item(headers=headers)
        self.assert_problem(self.review(address, headers=headers), 403, "AGE_ELIGIBILITY_REQUIRED")
        with TestClient(auth_app) as auth:
            self.assertEqual(auth.get(f"{API}/auth/age", headers=headers).json(), {"is_adult": False, "age_recorded": False})
            self.assertEqual(auth.post(f"{API}/auth/age", headers=headers,
                                      json={"date_of_birth": "2000-01-01"}).json(), {"is_adult": True, "age_recorded": True})
            self.assertEqual(auth.post(f"{API}/auth/age", headers=headers,
                                      json={"date_of_birth": "2001-01-01"}).status_code, 409)
        self.assert_problem(self.review(address, headers=headers), 403, "AGE_ELIGIBILITY_REQUIRED")
        refreshed = authorization(rotate_session(self.users, self.secret, tokens.refresh_token))
        quote = self.quote(address, headers=refreshed)
        returning = self.user("new-google-user", adult=False)
        self.quote(address, headers=returning)
        with patch.object(cart_app.state, "demo_checkout", False):
            self.assert_problem(self.place(quote, outcome="decline", headers=refreshed), 422, "MOCK_OUTCOME_DISABLED")
            self.assertEqual(self.place(quote, headers=refreshed).status_code, 201)
