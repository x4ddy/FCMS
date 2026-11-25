import os
import secrets
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from fastapi.testclient import TestClient

from backend.api_gateway.app import app as gateway_app
from backend.services.auth_service.app import app as auth_app
from backend.services.familycart_service import checkout
from backend.services.familycart_service.app import app as cart_app
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
PERSONAL = {"kind": "personal"}
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

    def user(self, user_id, adult=True):
        return authorization(create_session(self.users, self.secret, Identity(user_id, adult), user_id))

    def forward_maps(self, request):
        self.map_calls.append(request)
        if self.maps_barrier:
            self.maps_barrier.wait(timeout=10)
        return self.maps.post(request.url.path, content=request.content, headers={"Content-Type": "application/json"})

    def address(self, pin=DUBAI, *, headers=None, kind="personal", **changes):
        path = (API if kind == "personal" else HOUSEHOLD) + "/addresses"
        response = self.cart.post(path, headers=headers or self.owner, json={
            "label": "Home", "recipient_name": "Demo Customer", "address_line1": "10 Demo Street", "city": "UAE",
            **pin, **changes,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return {"kind": kind, "address_id": response.json()["id"]}

    def item(self, sku="milk", *, headers=None, shared=False, quantity=1):
        path = f"{REGULAR_CART}/items/{sku}/contributions/me" if shared else f"{API}/personal-cart/items/{sku}"
        if shared:
            response = self.cart.put(path, headers=headers or self.owner, json={"quantity": quantity})
            self.assertEqual(response.status_code, 200, response.text)
        else:
            for _ in range(quantity):
                response = self.cart.patch(path, headers=headers or self.owner, json={"delta": 1})
                self.assertEqual(response.status_code, 200, response.text)

    def review(self, address, *, cart=PERSONAL, overrides=None, headers=None, client=None):
        return (client or self.cart).post(f"{API}/checkout/quotes", headers=headers or self.owner, json={
            "cart": cart, "default_address": address, "overrides": overrides or [],
        })

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

    def grant_member(self):
        response = self.cart.put(f"{REGULAR_CART}/grants/user-adult", headers=self.owner)
        self.assertEqual(response.status_code, 204, response.text)

    def test_personal_order_through_gateway_replays_and_survives_restart(self):
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
        self.assertEqual((placed["status"], self.stock()), ("PLACED", 98))
        self.assertEqual(self.cart.get(f"{API}/personal-cart", headers=self.owner).json()["items"], [])
        self.assertEqual(gateway.get(path, headers=self.member).status_code, 404)
        self.assertEqual(self.cart.delete(f"{API}/addresses/{address['address_id']}", headers=self.owner).status_code, 204)
        initialize_database(self.db)
        with transaction(self.db, write=True) as db:
            checkout.seed_inventory(db, cart_app.state.products)
            db.execute("UPDATE checkout_quotes SET expires_at = ? WHERE id = ?", ("2000-01-01T00:00:00+00:00", quote["quote_id"]))
        self.assertEqual(gateway.get(path, headers=self.owner).json(), placed)
        self.assertEqual(gateway.get(f"{API}/orders", headers=self.owner).json(), {"orders": [{
            "order_id": placed["order_id"], "order_number": placed["order_number"], "placed_at": placed["placed_at"],
            "currency": "AED", "total_fils": 1500, "status": "PLACED", "is_shared": False, "delivery_count": 1,
        }], "next_cursor": None})
        self.assertEqual(gateway.get(f"{API}/orders", headers=self.member).json(), {"orders": [], "next_cursor": None})
        self.assertEqual(gateway.get(f"{API}/orders").status_code, 401)
        calls = len(self.map_calls)
        replay = self.place(quote, client=gateway)
        self.assertEqual((replay.status_code, replay.json()), (200, placed))
        self.assertEqual(len(self.map_calls), calls)
        self.assertEqual(self.stock(), 98)
        self.assert_problem(self.place(quote, "changed-key"), 409, "QUOTE_ALREADY_USED")
        self.assert_problem(self.place(quote, outcome="decline"), 409, "IDEMPOTENCY_KEY_REUSED")

    def test_shared_split_grouping_and_committed_event(self):
        self.grant_member()
        dubai = self.address()
        al_ain = self.address(AL_AIN, kind="household", headers=self.member, recipient_name="Al Ain Recipient")
        self.item(shared=True, quantity=2)
        self.item(shared=True, headers=self.member)
        combined = self.quote(dubai, cart=SHARED)["delivery_groups"]
        self.assertEqual(len(combined), 1)
        self.assertEqual({line["contributor_id"]: line["quantity"] for line in combined[0]["lines"]},
                         {"user-primary": 2, "user-adult": 1})
        quote = self.quote(dubai, cart=SHARED, overrides=[{"contributor_id": "user-adult", "address": al_ain}])
        first = self.place(quote)
        self.assertEqual(first.status_code, 201, first.text)
        placed = first.json()
        groups = {group["store_id"]: group for group in placed["delivery_groups"]}
        self.assertEqual({key: group["subtotal_fils"] for key, group in groups.items()},
                         {"uae-al-ain": 750, "uae-dubai": 1500})
        self.assertEqual(groups["uae-al-ain"]["address"]["recipient_name"], "Al Ain Recipient")
        self.assertEqual(placed["total_fils"], 2250)
        self.assertEqual((self.stock(), self.stock(store="uae-al-ain")), (98, 99))
        self.assertEqual(self.cart.get(REGULAR_CART, headers=self.member).json()["contributions"], [])
        with transaction(self.db) as db:
            event = db.execute("SELECT cart_id, actor_id, reason FROM cart_sync_events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(dict(event), {"cart_id": "regular-cart", "actor_id": "user-primary", "reason": "order"})
        summary = self.cart.get(f"{API}/orders", headers=self.owner).json()["orders"][0]
        self.assertTrue(summary["is_shared"])
        self.assertEqual(summary["delivery_count"], 2)

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
        self.item(shared=True)
        dubai = self.address()
        private = self.address(headers=self.member)
        quote = self.quote(dubai, cart=SHARED)
        self.assert_problem(self.review(dubai, cart=SHARED, headers=self.member), 403, "CHECKOUT_FORBIDDEN")
        self.assert_problem(self.review(dubai, cart=SHARED, headers=self.minor), 403, "AGE_ELIGIBILITY_REQUIRED")
        self.assert_problem(self.review(dubai, cart=SHARED, headers=self.user("unrelated")), 404, "CART_NOT_FOUND")
        self.assert_problem(self.review(private, cart=SHARED), 404, "ADDRESS_NOT_FOUND")
        self.assert_problem(self.place(quote, headers=self.member), 404, "QUOTE_NOT_FOUND")
        self.assert_problem(self.place(quote, headers=self.minor), 403, "AGE_ELIGIBILITY_REQUIRED")
        body = {"cart": SHARED, "default_address": dubai, "is_adult": True}
        self.assertEqual(self.cart.post(f"{API}/checkout/quotes", headers=self.minor, json=body).status_code, 422)
        self.assertEqual(self.cart.put(f"{HOUSEHOLD}/members/user-adult/primary", headers=self.owner).status_code, 200)
        self.assert_problem(self.place(quote), 403, "CHECKOUT_FORBIDDEN")
        self.assertEqual(self.cart.delete(f"{HOUSEHOLD}/members/user-primary", headers=self.member).status_code, 204)
        self.assert_problem(self.place(quote), 404, "CART_NOT_FOUND")

    def test_invalid_coverages_keep_all_delivery_groups(self):
        self.grant_member()
        self.item(shared=True)
        self.item(shared=True, headers=self.member)
        dubai = self.address()
        for pin, status, code in [
            ({}, 409, "ADDRESS_NEEDS_PIN"),
            ({"latitude": 0, "longitude": 0}, 409, "OUTSIDE_DELIVERY_AREA"),
        ]:
            outside = self.address(pin, kind="household")
            error = self.review(dubai, cart=SHARED, overrides=[{"contributor_id": "user-adult", "address": outside}])
            self.assert_problem(error, status, code)
            groups = error.json()["detail"]["delivery_groups"]
            self.assertEqual(len(groups), 2)
            self.assertEqual({group.get("problem") for group in groups}, {None, code})
        invalid = [{"contributor_id": "user-adult", "address": dubai}] * 2
        self.assert_problem(self.review(dubai, cart=SHARED, overrides=invalid), 422, "INVALID_CONTRIBUTOR")
        quote = self.quote(dubai, cart=SHARED)
        with patch.object(cart_app.state.maps_http, "post", side_effect=httpx.ConnectError("Maps offline")):
            self.assert_problem(self.place(quote), 503, "MAPS_UNAVAILABLE")
        self.assertEqual(self.stock(), 100)

    def test_stale_inputs_expiry_and_second_quote(self):
        address = self.address()
        self.item()
        edited = self.quote(address)
        # Restoring the same quantities still advances the cart revision.
        self.item()
        self.cart.patch(f"{API}/personal-cart/items/milk", headers=self.owner, json={"delta": -1})
        self.assert_problem(self.place(edited), 409, "QUOTE_STALE")
        moved = self.quote(address)
        target = f"{API}/addresses/{address['address_id']}"
        self.assertEqual(self.cart.patch(target, headers=self.owner, json=AL_AIN).status_code, 200)
        self.assert_problem(self.place(moved), 409, "QUOTE_STALE")
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
        self.item()
        address = self.address()
        quote = self.quote(address)
        self.item(shared=True)
        self.item(headers=self.member)
        self.assertEqual(self.place(quote).status_code, 201)

    def test_decline_and_fault_after_first_deduction_rollback(self):
        self.item("eggs")
        self.item()
        quote = self.quote(self.address())
        self.assert_problem(self.place(quote, "decline", outcome="decline"), 402, "MOCK_PAYMENT_DECLINED")
        self.assertEqual((self.stock(), self.stock("eggs")), (100, 100))
        original = DatabaseConnection.execute
        updates = []
        def fail_second(db, query, parameters=None):
            if query.startswith("UPDATE store_inventory SET quantity = quantity -"):
                updates.append(parameters[2])
                if len(updates) == 2:
                    raise RuntimeError("Injected inventory write failure")
            return original(db, query, parameters)
        with patch.object(DatabaseConnection, "execute", fail_second):
            self.assertEqual(self.place(quote, "retry").status_code, 500)
        self.assertEqual(updates, ["eggs", "milk"])
        self.assertEqual((self.stock(), self.stock("eggs")), (100, 100))
        self.assertEqual(len(self.cart.get(f"{API}/personal-cart", headers=self.owner).json()["items"]), 2)
        self.assertEqual(self.place(quote, "retry").status_code, 201)
        self.assertEqual((self.stock(), self.stock("eggs")), (99, 99))

    def test_same_store_groups_share_inventory(self):
        self.grant_member()
        self.item("honey", shared=True)
        self.item("honey", shared=True, headers=self.member)
        default = self.address()
        secondary = self.address(kind="household", headers=self.member)
        result = self.review(default, cart=SHARED, overrides=[{"contributor_id": "user-adult", "address": secondary}])
        self.assert_problem(result, 409, "OUT_OF_STOCK")
        shortages = result.json()["detail"]["shortages"]
        self.assertEqual([(row["group_number"], row["requested"], row["available"]) for row in shortages], [(1, 2, 1), (2, 2, 1)])
        self.assertEqual(self.stock("honey"), 1)
        self.assertEqual(len(self.cart.get(REGULAR_CART, headers=self.owner).json()["contributions"]), 2)

    def test_concurrent_same_key_returns_same_order(self):
        self.item()
        quote = self.quote(self.address())
        self.maps_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as workers:
            responses = list(workers.map(lambda _: self.place(quote), range(2)))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 201])
        self.assertEqual(responses[0].json(), responses[1].json())
        self.assertEqual(self.stock(), 99)

    def test_concurrent_carts_compete_for_last_unit(self):
        self.item("honey")
        self.item("honey", headers=self.member)
        owner_quote = self.quote(self.address())
        member_quote = self.quote(self.address(headers=self.member), headers=self.member)
        self.maps_barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as workers:
            owner = workers.submit(self.place, owner_quote)
            member = workers.submit(self.place, member_quote, headers=self.member)
            responses = [owner.result(), member.result()]
        self.assertEqual(sorted(response.status_code for response in responses), [201, 409])
        lose = [self.owner, self.member][next(i for i, response in enumerate(responses) if response.status_code == 409)]
        self.assert_problem(next(response for response in responses if response.status_code == 409), 409, "OUT_OF_STOCK")
        self.assertEqual(self.stock("honey"), 0)
        self.assertEqual(self.cart.get(f"{API}/personal-cart", headers=lose).json()["items"], [{"sku": "honey", "quantity": 1}])

    def test_authoritative_age_and_disabled_mock_override(self):
        tokens = create_session(self.users, self.secret, Identity("new-google-user", False), "New user",
                                google_subject="new-google-user")
        headers = authorization(tokens)
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
