import os
import hashlib
import secrets
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient

from backend.api_gateway.app import app as gateway_app
from backend.services.auth_service.app import app as auth_app
from backend.services.auth_service.google_auth import GoogleIdentity
from backend.services.familycart_service.app import app as shopping_app
from backend.services.familycart_service.db import transaction
from backend.services.refresh_service.app import app as refresh_app
from backend.shared.tokens import Identity, issue_token


API = "/api/v1"
HOME = {"label": "Home", "recipient_name": "Parent", "address_line1": "1 Street", "city": "Dubai"}


class ChildAccountTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.shopping = root / "shopping.sqlite"
        self.users = root / "users.sqlite"
        secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_MODE": "local", "FAMILYCART_DATABASE_URL": "", "FAMILYCART_USER_DATABASE_URL": "",
            "FAMILYCART_DB_PATH": str(self.shopping), "FAMILYCART_USER_DB_PATH": str(self.users),
            "FAMILYCART_JWT_SECRET": secret, "FAMILYCART_GOOGLE_CLIENT_ID": "test",
        }))
        # Session rows are real. Keep logout notices for this temporary database out of the shared Redis retry queue.
        self.enterContext(patch('backend.services.refresh_service.service.announce_revocation'))
        self.auth = self.enterContext(TestClient(auth_app))
        self.cart = self.enterContext(TestClient(shopping_app))
        self.refresh = self.enterContext(TestClient(refresh_app))
        self.outsider = {"Authorization": "Bearer " + issue_token(Identity("outsider", True), secret)}
        self.admin = {"Authorization": "Bearer " + issue_token(Identity("parent", True), secret)}
        self.family_body = {"name": "Family", "display_name": "Parent", "address": HOME}
        created = self.cart.post(f"{API}/households", headers=self.admin, json=self.family_body)
        self.household = created.json()["id"]
        self.root = f"{API}/households/{self.household}"
        self.code = self.issue()

    def issue(self, member_id=None, key=None):
        headers = {**self.admin, "Idempotency-Key": key} if key else self.admin
        response = self.cart.post(f"{self.root}/child-codes", headers=headers, json={"member_id": member_id})
        self.assertEqual(response.status_code, 201, response.text)
        code = response.json()["invitation_code"]
        self.assertRegex(code, r"^[A-Za-z0-9]{6}$")
        return code

    def redeem(self, code, key=None, name="Robin"):
        return self.auth.post(f"{API}/auth/child", headers={"Idempotency-Key": key or str(uuid4())},
                              json={"code": code, "display_name": name})

    def test_gateway_isolates_admin_and_child_browser_sessions(self):
        # Keep the real pooled gateway client and cookie handling. Replace only its network transport.
        async def forward(transport, request):
            service = refresh_app if request.url.path.startswith(f"{API}/sessions/") else auth_app
            return await httpx.ASGITransport(app=service).handle_async_request(request)

        self.enterContext(patch.object(httpx.AsyncHTTPTransport, "handle_async_request", forward))
        self.enterContext(patch("backend.services.auth_service.app.verify_google_credential",
                                return_value=GoogleIdentity("parent", "parent@gmail.com", "Parent")))
        gateway = self.enterContext(TestClient(gateway_app))
        refresh = f"{API}/sessions/refresh"

        async def two_browsers():
            def browser():
                return httpx.AsyncClient(transport=httpx.ASGITransport(app=gateway_app), base_url="https://familycart.test")

            async with browser() as admin, browser() as child:
                login = await admin.post(f"{API}/auth/google", json={"credential": "x" * 100, "date_of_birth": "1980-01-01"})
                login.raise_for_status()
                self.assertEqual((await child.post(refresh)).status_code, 401)
                signed_in = await child.post(f"{API}/auth/child", headers={"Idempotency-Key": str(uuid4())},
                                             json={"code": self.code, "display_name": "Robin"})
                signed_in.raise_for_status()
                child_id = signed_in.json()["user"]["id"]
                self.assertNotEqual(child_id, "parent")
                admin_user = (await admin.post(refresh)).json()["user"]
                self.assertEqual(admin_user["id"], "parent")
                self.assertEqual(admin_user["email"], "parent@gmail.com")
                child_user = (await child.post(refresh)).json()["user"]
                self.assertEqual(child_user["id"], child_id)
                self.assertNotIn("email", child_user)
                (await child.post(f"{API}/sessions/logout")).raise_for_status()
                self.assertEqual((await admin.post(refresh)).json()["user"]["id"], "parent")
                self.assertEqual((await child.post(refresh)).status_code, 401)
                self.assertFalse(gateway_app.state.http.cookies)

        gateway.portal.call(two_browsers)

    def test_profile_rename_persists_and_updates_household_names(self):
        with patch("backend.services.auth_service.app.verify_google_credential",
                   return_value=GoogleIdentity("parent", "parent@gmail.com", "Parent")):
            login_body = {"credential": "x" * 100, "date_of_birth": "1980-01-01"}
            login = self.auth.post(f"{API}/auth/google", json=login_body)
            login.raise_for_status()
            headers = {"Authorization": "Bearer " + login.json()["access_token"]}
            other = self.cart.post(f"{API}/households", headers=headers, json=self.family_body)
            other.raise_for_status()
            profile = f"{API}/auth/profile"
            change = {"display_name": "  Vinesh  "}

            self.assertEqual(self.auth.patch(profile, json=change).status_code, 401)
            self.assertEqual(self.auth.patch(profile, headers=headers, json={"display_name": "  "}).status_code, 422)
            with patch("backend.services.auth_service.app.shopping_transaction", side_effect=sqlite3.OperationalError):
                self.assertEqual(self.auth.patch(profile, headers=headers, json=change).status_code, 503)
            self.assertEqual(self.auth.post(f"{API}/auth/google", json=login_body).json()["user"]["display_name"], "Parent")

            saved = self.auth.patch(profile, headers=headers, json=change)
            self.assertEqual(saved.json(), {"display_name": "Vinesh"})
            for household in (self.household, other.json()["id"]):
                members = self.cart.get(f"{API}/households/{household}/members", headers=headers).json()
                self.assertEqual(next(member["display_name"] for member in members if member["id"] == "parent"), "Vinesh")

            self.refresh.cookies.update(login.cookies)
            restored = self.refresh.post(f"{API}/sessions/refresh").json()["user"]
            self.assertEqual(restored["display_name"], "Vinesh")
            self.assertEqual(restored["email"], "parent@gmail.com")
            again = self.auth.post(f"{API}/auth/google", json=login_body).json()["user"]
            self.assertEqual(again["display_name"], "Vinesh")

        child = self.redeem(self.code)
        child.raise_for_status()
        headers = {"Authorization": "Bearer " + child.json()["access_token"]}
        self.assertEqual(self.auth.patch(profile, headers=headers, json=change).status_code, 403)

    def test_two_claimants_one_account_and_one_session(self):
        barrier = Barrier(2)
        def submit(key):
            barrier.wait()
            return self.redeem(self.code, key)
        keys = [str(uuid4()), str(uuid4())]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(submit, keys))
        self.assertEqual(sorted(r.status_code for r in responses), [200, 400])
        winner = next(i for i, r in enumerate(responses) if r.status_code == 200)
        first = responses[winner]
        retry = self.redeem(self.code, keys[winner])
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.cookies["familycart_refresh"], first.cookies["familycart_refresh"])
        self.assertEqual(retry.json()["user"]["id"], first.json()["user"]["id"])
        with transaction(self.shopping) as db:
            row = db.execute("SELECT created_at, expires_at, token_hash FROM household_invitations").fetchone()
            self.assertEqual(datetime.fromisoformat(row["expires_at"]) - datetime.fromisoformat(row["created_at"]),
                             timedelta(hours=24))
            self.assertNotIn(self.code, row["token_hash"])
            self.assertEqual(db.execute("SELECT count(*) FROM household_members WHERE household_id = ?",
                                        (self.household,)).fetchone()[0], 2)
        with transaction(self.users) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM users").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM refresh_sessions").fetchone()[0], 1)
        self.refresh.cookies.update(first.cookies)
        restored = self.refresh.post(f"{API}/sessions/refresh")
        self.assertTrue(restored.json()["user"]["is_child_account"])
        self.assertFalse(restored.json()["user"]["is_adult"])
        self.assertEqual(self.redeem(self.code, keys[winner]).status_code, 400)

    def test_signup_recovery_and_new_code_after_logout(self):
        key = str(uuid4())
        unnamed = self.redeem(self.code, key, name=None)
        self.assertEqual(unnamed.status_code, 422)
        self.assertEqual(unnamed.json()["detail"]["code"], "CHILD_NAME_REQUIRED")
        self.assertEqual(self.redeem(self.code, key, name="  ").status_code, 422)
        with patch("backend.services.auth_service.app.child_session", side_effect=RuntimeError("identity offline")):
            with self.assertRaisesRegex(RuntimeError, "identity offline"):
                self.redeem(self.code, key)
        self.assertEqual(self.redeem(self.code).status_code, 400)
        first = self.redeem(self.code, key)
        self.assertEqual(first.status_code, 200, first.text)
        user = first.json()["user"]
        self.refresh.cookies.update(first.cookies)
        self.assertEqual(self.refresh.post(f"{API}/sessions/logout").status_code, 204)
        self.assertEqual(self.redeem(self.code, key).status_code, 400)
        replaced = self.issue(user["id"])
        current = self.issue(user["id"])
        self.assertEqual(self.redeem(replaced).status_code, 400)
        again = self.redeem(current, name=None)
        self.assertEqual(again.json()["user"], user)
        outstanding = self.issue(user["id"])
        self.cart.delete(f"{self.root}/members/{user['id']}", headers=self.admin).raise_for_status()
        self.assertEqual(self.redeem(outstanding).status_code, 400)

    def test_expiry_and_separation_from_normal_invitations(self):
        self.assertEqual(self.cart.post(f"{self.root}/child-codes", json={}, headers=self.outsider).status_code, 404)
        normal = self.cart.post(f"{self.root}/invitations", headers=self.admin).json()["invitation_code"]
        self.assertEqual(self.redeem(normal).status_code, 400)
        payload = {"invitation_code": self.code, "display_name": "Sam", "address": HOME}
        self.assertEqual(self.cart.post(f"{API}/household-invitations/accept", headers=self.admin, json=payload).status_code, 422)
        joined = self.cart.post(f"{API}/household-invitations/accept", headers=self.outsider,
                                json={**payload, "invitation_code": normal})
        joined.raise_for_status()
        self.assertEqual(self.cart.post(f"{self.root}/child-codes", json={}, headers=self.outsider).status_code, 403)
        with transaction(self.shopping, write=True) as db:
            db.execute("UPDATE household_invitations SET expires_at = '2000-01-01T00:00:00+00:00'")
        self.assertEqual(self.redeem(self.code).status_code, 400)

    def test_short_code_collision_keeps_both_invitations_usable(self):
        second = "Q7b9Xa" if self.code != "Q7b9Xa" else "v3R6Tu"
        key = str(uuid4())
        with patch("backend.services.familycart_service.child_codes.new_code", side_effect=[self.code, second] * 2):
            self.assertEqual(self.issue(key=key), second)
            self.assertEqual(self.issue(key=key), second)
        first = self.redeem(self.code).json()["user"]["id"]
        self.assertNotEqual(self.redeem(second).json()["user"]["id"], first)

    def test_repeated_issuance_requests_share_one_code(self):
        key = str(uuid4())
        barrier = Barrier(2)

        def submit():
            barrier.wait()
            return self.issue(key=key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = [future.result() for future in (pool.submit(submit), pool.submit(submit))]
        self.assertEqual(first, second)
        with transaction(self.shopping) as db:
            count = db.execute("SELECT count(*) FROM household_invitations WHERE household_id = ?", (self.household,)).fetchone()[0]
            self.assertEqual(count, 2)  # One setup code and one for the repeated request.
        child_id = self.redeem(first).json()["user"]["id"]
        returning_key = str(uuid4())
        returning = self.issue(child_id, returning_key)
        self.assertEqual(self.issue(child_id, returning_key), returning)
        self.assertEqual(self.redeem(returning, name=None).json()["user"]["id"], child_id)

    def test_previously_issued_codes_still_work(self):
        old = "JOIN-" + secrets.token_urlsafe(24)
        with transaction(self.shopping, write=True) as db:
            db.execute("UPDATE household_invitations SET token_hash = ? WHERE household_id = ?",
                       ("child:" + hashlib.sha256(old.encode()).hexdigest(), self.household))
        self.assertEqual(self.redeem(old).status_code, 200)

    def test_restrictions_are_enforced_by_the_services(self):
        login = self.redeem(self.code)
        self.assertEqual(login.status_code, 200, login.text)
        user = login.json()["user"]
        self.assertEqual(user["display_name"], "Robin")
        child = {"Authorization": "Bearer " + login.json()["access_token"]}
        self.assertEqual([h["id"] for h in self.cart.get(f"{API}/households", headers=child).json()], [self.household])
        self.assertEqual(self.cart.post(f"{self.root}/leave", headers=child).status_code, 403)
        products = self.cart.get(f"{API}/products", headers=child).json()
        self.assertTrue(products)
        self.assertTrue(all(p["min_age"] == 0 for p in products))
        restricted = "cigarette-pack-20"
        self.assertEqual(self.cart.get(f"{API}/products/{restricted}", headers=child).status_code, 404)
        adult_cart = self.cart.post(f"{self.root}/carts", headers=self.admin,
                                    json={"name": "Adults only", "adults_only": True})
        adult_cart.raise_for_status()
        self.assertEqual(self.cart.get(f"{self.root}/carts/{adult_cart.json()['id']}", headers=child).status_code, 404)
        members = self.cart.get(f"{self.root}/members?include_roles=true", headers=self.admin).json()
        self.assertTrue(next(m for m in members if m["id"] == user["id"])["is_child_account"])
        carts = self.cart.get(f"{self.root}/carts", headers=child).json()
        self.assertEqual(len(carts), 1)
        cart = carts[0]
        self.assertTrue(cart["can_edit_items"])
        self.assertFalse(cart["can_checkout"])
        base = f"{self.root}/carts/{cart['id']}"
        with transaction(self.shopping, write=True) as db:
            db.execute("UPDATE carts SET inventory_store_id = 'uae-dubai' WHERE id = ?", (cart["id"],))
        sku = products[0]["sku"]
        # Exercise the actual storefront ADD request and verify what the admin sees.
        tap = {**child, "Idempotency-Key": str(uuid4())}
        for _ in range(2):
            self.cart.patch(f"{base}/items/{sku}", headers=tap, json={"delta": 1}).raise_for_status()
        lines = self.cart.get(base, headers=self.admin).json()["contributions"]
        self.assertEqual([(row["contributor_id"], row["quantity"]) for row in lines if row["sku"] == sku],
                         [(user["id"], 1)])
        self.assertEqual(self.cart.put(f"{base}/items/{restricted}/contributions/me", headers=child,
                                       json={"quantity": 1}).status_code, 404)
        quote = {"cart": {"kind": "household", "household_id": self.household, "cart_id": cart["id"]},
                 "default_address": {"kind": "household", "address_id": "any"}}
        for path, body in [(f"{API}/households", self.family_body), (f"{self.root}/addresses", HOME),
                           (f"{self.root}/child-codes", {}), (f"{base}/checkout", {}), (f"{API}/checkout/quotes", quote),
                           (f"{API}/checkout/orders", {"quote_id": "any"})]:
            with self.subTest(path=path):
                result = self.cart.post(path, headers={**child, "Idempotency-Key": str(uuid4())}, json=body)
                self.assertEqual(result.status_code, 403, result.text)
        self.assertEqual(self.cart.put(f"{self.root}/members/{user['id']}/primary", headers=self.admin).status_code, 409)
        self.assertEqual(self.auth.post(f"{API}/auth/age", headers=child, json={"date_of_birth": "2000-01-01"}).status_code, 409)
