"""Age approval, shared addresses, live address updates and private lists."""

import os
import secrets
import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.familycart_service import live_sync
from backend.services.familycart_service.app import app, live_view
from backend.services.familycart_service.db import initialize_database, transaction
from backend.services.refresh_service.service import create_session
from backend.services.user_service.db import initialize_database as initialize_users
from backend.shared.tokens import Identity, issue_token

API = "/api/v1"


class FamilyCase(unittest.TestCase):
    def setUp(self) -> None:
        folder = Path(self.enterContext(TemporaryDirectory()))
        self.users = folder / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(folder / "family.sqlite3"), "FAMILYCART_USER_DB_PATH": str(self.users),
            "FAMILYCART_JWT_SECRET": self.secret, "FAMILYCART_REDIS_URL": "redis://127.0.0.1:1/0",
            "FAMILYCART_SYNC_INTERVAL_SECONDS": "3600",
        }))
        initialize_users(self.users)
        self.client = self.enterContext(TestClient(app))
        self.changed: list[str] = []
        live_sync._listener = self.changed.append
        self.p, self.m, self.c = self.h("p"), self.h("m"), self.h("c", adult=False)
        self.root = self.household(self.p, "Pat")
        self.join(self.root, self.p, self.m, "Morgan")
        self.join(self.root, self.p, self.c, "Casey")
        self.adult_sku = next(sku for sku, product in app.state.products.items() if product.min_age)
        self.changed.clear()

    def h(self, user: str, *, adult: bool = True) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(Identity(user, adult), self.secret)}"}

    def call(self, method, path, headers, status=200, **json):
        response = self.client.request(method, API + path, headers=headers, json=json or None)
        self.assertEqual(response.status_code, status, response.text)
        return response.json() if response.content else None

    def household(self, headers, name):
        return "/households/" + self.call("POST", "/households", headers, 201, name=f"{name} home", display_name=name,
                                          address=self.address(name))["id"]

    def join(self, root, primary, headers, name, status=200):
        code = self.call("POST", f"{root}/invitations", primary, 201)["invitation_code"]
        return self.call("POST", "/household-invitations/accept", headers, status, invitation_code=code, display_name=name,
                         address=self.address(name))

    def members(self):
        return {row["id"]: row for row in self.call("GET", f"{self.root}/members?include_roles=true", self.p)}

    def decide(self, member, approved, headers=None, status=204):
        self.call("PUT", f"{self.root}/members/{member}/age", headers or self.p, status, approved=approved)

    def snapshot(self):
        with transaction(app.state.db_path) as db:
            snapshots, _ = live_sync.build_snapshots(db, [self.root.split("/")[-1]], lists_since={})
        return next(iter(snapshots.values()))

    def live(self, user, adult=True):
        return live_view(SimpleNamespace(app=app), self.root.split("/")[-1], Identity(user, adult), None, self.snapshot())

    def address(self, label="Home"):
        return dict(label=label, recipient_name=label, address_line1="1 Street", city="Dubai", latitude=25.2, longitude=55.27)


class AgeApprovalTests(FamilyCase):
    def sees_adult_products(self, headers):
        rows = self.call("GET", f"/products?household_id={self.root.split('/')[-1]}", headers)
        return self.adult_sku in [row["sku"] for row in rows]

    def test_joining_is_only_a_claim_until_the_primary_approves(self):
        self.assertEqual({k: v["age_status"] for k, v in self.members().items()}, {"p": "APPROVED", "m": "PENDING", "c": "NONE"})
        self.assertTrue(self.sees_adult_products(self.p))
        self.assertFalse(self.sees_adult_products(self.m))
        self.assertEqual(self.call("GET", f"{self.root}/capabilities", self.m)["age_status"], "PENDING")
        adults_only = self.call("POST", f"{self.root}/carts", self.p, 201, name="Party", adults_only=True)["id"]
        self.call("PUT", f"{self.root}/carts/{adults_only}/grants/m", self.p, 403)

        self.decide("m", True)
        self.assertTrue(self.sees_adult_products(self.m))
        self.call("PUT", f"{self.root}/carts/{adults_only}/grants/m", self.p, 204)
        self.call("PATCH", f"{self.root}/carts/{adults_only}/items/{self.adult_sku}", self.m, delta=1)

        self.decide("m", False)
        self.assertFalse(self.sees_adult_products(self.m))
        self.call("GET", f"{self.root}/carts/{adults_only}", self.m, 404)
        self.assertEqual(self.members()["m"]["age_status"], "REJECTED")

    def test_who_may_decide(self):
        self.decide("m", True, self.m, 403)
        self.decide("m", True, self.c, 403)
        self.decide("m", True, self.h("stranger"), 404)
        self.decide("p", True, status=409)
        self.decide("c", True, status=409)
        self.decide("nobody", True, status=404)
        young = self.h("young", adult=False)
        root = self.household(young, "Young")
        self.join(root, young, self.m, "Morgan")
        self.call("PUT", f"{root}/members/m/age", young, 403, approved=True)

    def test_creating_your_own_household_does_not_approve_you_elsewhere(self):
        own = self.household(self.m, "Morgan")
        self.assertTrue(self.call("GET", f"{own}/capabilities", self.m)["is_adult"])
        self.assertFalse(self.call("GET", f"{self.root}/capabilities", self.m)["is_adult"])
        self.assertFalse(self.sees_adult_products(self.m))

    def test_a_waiting_member_is_not_an_adult_outside_the_household_either(self):
        self.call("GET", f"/products/{self.adult_sku}", self.m, 404)
        self.call("PATCH", f"/personal-cart/items/{self.adult_sku}", self.m, 404, delta=1)
        self.call("GET", f"/products/{self.adult_sku}", self.h("no-household"))

    def test_joining_again_cannot_clear_a_decision(self):
        self.decide("m", False)
        self.join(self.root, self.p, self.m, "Morgan", 409)
        self.assertEqual(self.members()["m"]["age_status"], "REJECTED")
        self.call("DELETE", f"{self.root}/members/m", self.p, 204)
        self.join(self.root, self.p, self.m, "Morgan")
        self.assertEqual(self.members()["m"]["age_status"], "PENDING")

    def test_ownership_waits_for_the_age_decision(self):
        self.call("PUT", f"{self.root}/members/m/primary", self.p, 409)
        self.decide("m", True)
        self.call("PUT", f"{self.root}/members/m/primary", self.p)
        self.assertTrue(self.call("GET", f"{self.root}/capabilities", self.m)["is_adult"])

    def test_the_primary_sees_the_claimed_age_and_an_account_outage_does_not_break_the_list(self):
        create_session(self.users, self.secret, Identity("m", True), "Morgan", date_of_birth="2000-01-01", google_subject="m")
        self.assertGreaterEqual(self.members()["m"]["claimed_age"], 24)
        with patch("backend.services.familycart_service.app.birth_dates", side_effect=sqlite3.OperationalError("down")):
            self.assertNotIn("claimed_age", self.members()["m"])
        self.call("GET", f"{self.root}/members?include_roles=true", self.m, 403)

    def test_live_screens_follow_the_decision_and_hide_it_from_members(self):
        self.assertEqual((self.live("m")["is_adult"], self.live("m")["age_status"]), (False, "PENDING"))
        self.assertTrue(all(row["age_status"] is None for row in self.live("m")["members"]))
        self.decide("m", True)
        self.assertEqual(set(self.changed), {self.root.split("/")[-1]})
        self.assertTrue(self.live("m")["is_adult"])

    def test_waiting_member_cannot_check_out_or_order(self):
        tokens = create_session(self.users, self.secret, Identity("m", True), "Morgan", date_of_birth="2000-01-01", google_subject="m")
        headers = {"Authorization": f"Bearer {tokens.access_token}"}
        body = {"cart": {"kind": "personal"}, "default_address": {"kind": "personal", "address_id": "x"}}
        response = self.client.post(f"{API}/checkout/quotes", headers=headers, json=body)
        self.assertEqual((response.status_code, response.json()["detail"]["code"]), (403, "AGE_ELIGIBILITY_REQUIRED"))

    def test_people_who_were_members_before_keep_their_access(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "old.sqlite3"
            initialize_database(path)
            with closing(sqlite3.connect(path)) as raw:
                raw.execute("CREATE TABLE keep AS SELECT household_id, user_id, display_name, is_adult FROM household_member_profiles")
                raw.execute("DROP TABLE household_member_profiles")
                raw.execute("ALTER TABLE keep RENAME TO household_member_profiles")
                raw.execute("CREATE UNIQUE INDEX k ON household_member_profiles (household_id, user_id)")
                raw.commit()
            initialize_database(path)
            initialize_database(path)
            with transaction(path) as db:
                self.assertEqual({row["age_status"] for row in db.execute("SELECT age_status FROM household_member_profiles")}, {"APPROVED"})


class FamilyAddressTests(FamilyCase):
    def labels(self, headers):
        return sorted(row["label"] for row in self.call("GET", f"{self.root}/addresses", headers))

    def test_nobody_creates_or_joins_without_an_address(self):
        code = self.call("POST", f"{self.root}/invitations", self.p, 201)["invitation_code"]
        x = self.h("x")
        self.call("POST", "/households", x, 422, name="X", display_name="X")
        self.call("POST", "/household-invitations/accept", x, 422, invitation_code=code, display_name="X")
        self.call("POST", "/household-invitations/accept", x, 422, invitation_code=code, display_name="X", address={"label": "Home"})
        self.assertEqual(self.call("GET", "/households", x), [])
        self.call("POST", "/household-invitations/accept", x, invitation_code=code, display_name="X", address=self.address("X home"))
        self.assertEqual(self.labels(x), ["X home"])

    def test_members_see_their_own_and_the_primary_sees_all(self):
        self.call("POST", f"{self.root}/addresses", self.m, 201, **self.address("Office"))
        self.assertEqual(self.labels(self.m), ["Morgan", "Office"])
        self.assertEqual(self.labels(self.c), ["Casey"])
        self.assertEqual(self.labels(self.p), ["Casey", "Morgan", "Office", "Pat"])
        theirs = self.call("GET", f"{self.root}/addresses", self.p)
        morgan = next(row["id"] for row in theirs if row["label"] == "Morgan")
        self.call("POST", f"{self.root}/addresses/{morgan}/resolve-store", self.c, 404)
        self.call("DELETE", f"{self.root}/addresses/{morgan}", self.c, 403)

    def test_the_view_moves_with_ownership(self):
        self.decide("m", True)
        self.call("PUT", f"{self.root}/members/m/primary", self.p)
        self.assertEqual(self.labels(self.m), ["Casey", "Morgan", "Pat"])
        self.assertEqual(self.labels(self.p), ["Pat"])
        live = lambda user: sorted(row["label"] for row in self.live(user)["addresses"])
        self.assertEqual((live("m"), live("p")), (["Casey", "Morgan", "Pat"], ["Pat"]))

    def test_live_updates_show_each_viewer_only_what_they_may_see(self):
        self.changed.clear()
        added = self.call("POST", f"{self.root}/addresses", self.m, 201, **self.address("Flat"))
        self.call("PATCH", f"{self.root}/addresses/{added['id']}", self.m, latitude=24.0, longitude=54.0)
        live = lambda user: sorted(row["label"] for row in self.live(user)["addresses"])
        self.assertEqual((live("p"), live("m"), live("c")), (["Casey", "Flat", "Morgan", "Pat"], ["Flat", "Morgan"], ["Casey"]))
        self.call("DELETE", f"{self.root}/addresses/{added['id']}", self.p, 204)
        self.assertEqual(live("m"), ["Morgan"])
        self.assertEqual(len(self.changed), 3)

    def test_the_last_address_cannot_be_removed(self):
        for headers in (self.m, self.p):
            mine = next(row for row in self.call("GET", f"{self.root}/addresses", self.p) if row["created_by"] == "m")
            self.call("DELETE", f"{self.root}/addresses/{mine['id']}", headers, 409)
        self.call("POST", f"{self.root}/addresses", self.m, 201, **self.address("New"))
        self.call("DELETE", f"{self.root}/addresses/{mine['id']}", self.m, 204)
        self.assertEqual(self.labels(self.m), ["New"])

    def test_a_removed_members_addresses_go_with_them(self):
        self.call("DELETE", f"{self.root}/members/m", self.p, 204)
        self.assertEqual(self.labels(self.p), ["Casey", "Pat"])
        self.assertNotIn("Morgan", [row["label"] for row in self.snapshot()["addresses"]])

    def test_addresses_stay_inside_their_household(self):
        other = self.household(self.m, "Elsewhere")
        self.assertEqual([row["label"] for row in self.call("GET", f"{other}/addresses", self.m)], ["Elsewhere"])
        self.assertNotIn("Elsewhere", self.labels(self.p))

    def test_personal_addresses_are_not_reachable_through_the_gateway(self):
        from fastapi import HTTPException

        from backend.api_gateway.app import _target_for
        for path in ("addresses", "addresses/x", "addresses/x/resolve-store"):
            with self.assertRaises(HTTPException):
                _target_for(path, None)


class MyListTests(FamilyCase):
    def test_private_to_the_account_and_separate_from_household_lists(self):
        mine = self.call("POST", "/my-lists", self.m, 201, name="Mine")
        self.call("POST", f"{self.root}/lists", self.m, 201, name="Ours")
        self.assertEqual([row["name"] for row in self.call("GET", "/my-lists", self.m)], ["Mine"])
        self.assertEqual([row["name"] for row in self.call("GET", f"{self.root}/lists", self.m)], ["Ours"])
        self.assertEqual(self.call("GET", "/my-lists", self.p), [])
        for method, body in (("GET", {}), ("PATCH", {"name": "x"}), ("DELETE", {})):
            self.call(method, f"/my-lists/{mine['id']}", self.p, 404, **body)
        self.call("POST", f"/my-lists/{mine['id']}/items", self.p, 404, custom_name="x")
        self.assertNotIn("Mine", str(self.snapshot()))

    def test_items_without_any_household(self):
        solo = self.h("solo")
        path = f"/my-lists/{self.call('POST', '/my-lists', solo, 201, name='Solo')['id']}"
        detail = self.call("POST", f"{path}/items/bulk", solo, 201, items=[{"sku": "milk"}, {"custom_name": "Candles"}])
        self.assertEqual([row.get("custom_name") or row["sku"] for row in detail["items"]], ["milk", "Candles"])
        self.assertEqual(self.call("POST", f"{path}/items", solo, 201, sku="milk", quantity=2)["quantity"], 3)
        item = detail["items"][1]["id"]
        self.assertTrue(self.call("PATCH", f"{path}/items/{item}", solo, checked=True)["checked"])
        self.assertEqual(self.call("GET", "/my-lists", solo)[0]["checked_count"], 1)
        self.assertEqual(len(self.call("POST", f"{path}/items/clear-checked", solo)["items"]), 1)
        self.call("DELETE", f"{path}/items/{detail['items'][0]['id']}", solo, 204)
        self.call("DELETE", path, solo, 204)
        self.call("GET", path, solo, 404)

    def test_only_the_chosen_items_go_to_the_household_cart(self):
        cart = self.call("GET", f"{self.root}/carts", self.p)[0]["id"]
        for base, extra in (("/my-lists", {"household_id": self.root.split("/")[-1]}), (f"{self.root}/lists", {})):
            path = f"{base}/{self.call('POST', base, self.m, 201, name='L')['id']}"
            rows = self.call("POST", f"{path}/items/bulk", self.m, 201, items=[{"sku": "milk"}, {"sku": "eggs"}, {"sku": "bread"}])["items"]
            result = self.call("POST", f"{path}/move-to-cart", self.m, target=cart, item_ids=[rows[1]["id"]], **extra)
            self.assertEqual(result["moved"], [rows[1]["id"]])
            self.assertEqual([row["checked"] for row in self.call("GET", path, self.m)["items"]], [False, True, False])
        contributions = self.call("GET", f"{self.root}/carts/{cart}", self.p)["contributions"]
        self.assertEqual(contributions, [{"sku": "eggs", "contributor_id": "m", "quantity": 2}])

    def test_many_quick_changes_are_one_request_and_one_announcement(self):
        for base in ("/my-lists", f"{self.root}/lists"):
            path = f"{base}/{self.call('POST', base, self.m, 201, name='L')['id']}"
            milk, eggs, bread = self.call("POST", f"{path}/items/bulk", self.m, 201,
                                          items=[{"sku": "milk"}, {"sku": "eggs"}, {"sku": "bread"}])["items"]
            self.changed.clear()
            result = self.call("POST", f"{path}/changes", self.m, add=[{"sku": "milk", "quantity": 2}, {"custom_name": "Tape"}, {"sku": "honey"}],
                               set=[{"id": eggs["id"], "quantity": 5}, {"id": milk["id"], "checked": True}, {"id": "gone", "quantity": 2}],
                               remove=[bread["id"], "already-gone"])
            rows = {row.get("custom_name") or row["sku"]: (row["quantity"], row["checked"]) for row in result["list"]["items"]}
            self.assertEqual(rows, {"milk": (3, True), "eggs": (5, False), "Tape": (1, False), "honey": (1, False)})
            self.assertEqual(result["skipped"], [{"id": "gone", "reason": "Item not found"}])
            self.assertLessEqual(len(self.changed), 1)
            self.assertEqual(self.call("GET", path, self.m)["items"], result["list"]["items"])

    def test_batched_changes_keep_the_rules(self):
        path = f"{self.root}/lists/{self.call('POST', self.root + '/lists', self.p, 201, name='L')['id']}"
        theirs = self.call("POST", f"{path}/items", self.p, 201, sku="milk")["id"]
        result = self.call("POST", f"{path}/changes", self.c, add=[{"sku": self.adult_sku}, {"sku": "eggs"}], remove=[theirs])
        self.assertEqual([row["sku"] for row in result["list"]["items"]], ["milk", "eggs"])
        self.assertEqual({row["reason"] for row in result["skipped"]}, {"Product not found", "You can only remove items that you added."})
        self.call("POST", f"{path}/changes", self.h("stranger"), 404, remove=[theirs])
        self.call("POST", f"{path}/changes", self.p, 422, set=[{"id": theirs, "quantity": 0}])
        mine = f"/my-lists/{self.call('POST', '/my-lists', self.m, 201, name='Mine')['id']}"
        self.call("POST", f"{mine}/changes", self.p, 404, add=[{"custom_name": "x"}])

    def test_sending_needs_membership_and_access(self):
        solo = self.h("solo")
        path = f"/my-lists/{self.call('POST', '/my-lists', solo, 201, name='Solo')['id']}"
        item = self.call("POST", f"{path}/items", solo, 201, sku="milk")["id"]
        cart = self.call("GET", f"{self.root}/carts", self.p)[0]["id"]
        self.call("POST", f"{path}/move-to-cart", solo, 404, target=cart, item_ids=[item], household_id=self.root.split("/")[-1])


if __name__ == "__main__":
    unittest.main()
