import os
import secrets
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from httpx import Response
from fastapi.testclient import TestClient

from backend.services.auth_service.app import app as auth_app
from backend.services.auth_service.google_auth import GoogleIdentity
from backend.services.familycart_service.app import app as familycart_app, live_view
from backend.services.familycart_service.db import initialize_database
from backend.shared.demo_members import CHILD
from backend.shared.tokens import Identity, issue_token

HOUSEHOLD = "/api/v1/households/demo-household"
CARTS = f"{HOUSEHOLD}/carts"
REGULAR = f"{CARTS}/regular-cart"
ADULTS_ONLY = f"{CARTS}/adults-only-cart"
CHILD_GRANT = f"{REGULAR}/grants/user-child"


class StageTwoTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.db_path = Path(folder) / "test.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(self.db_path),
            "FAMILYCART_JWT_SECRET": self.secret,
            "FAMILYCART_GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
        }))
        identities = {
            "primary@example.test": GoogleIdentity("user-primary", "primary@gmail.com", "Primary Customer", True),
            "demo-child": GoogleIdentity("user-child", "child@gmail.com", "Child Member"),
            "adult@example.test": GoogleIdentity("user-adult", "adult@gmail.com", "Adult Member", True),
        }
        self.enterContext(patch(
            "backend.services.auth_service.app.verify_google_credential",
            side_effect=lambda credential, _client_id: identities[credential[:credential.index("|")]],
        ))
        self.auth = self.enterContext(TestClient(auth_app))
        self.cart = self.enterContext(TestClient(familycart_app))
        self.primary = self.sign_in("primary@example.test", "primary-demo-2026")
        self.child = self.sign_in("demo-child", "child-demo-2026")

    def sign_in(self, identifier: str, password: str) -> dict[str, str]:
        response = self.auth.post(
            "/api/v1/auth/google",
            json={"credential": f"{identifier}|" + "x" * 100},
        )
        self.assertEqual(response.status_code, 200)
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    def set_milk(self, cart: str, headers: dict[str, str], quantity: object) -> Response:
        return self.cart.put(
            f"{cart}/items/milk/contributions/me", headers=headers, json={"quantity": quantity}
        )

    def test_grant_and_revoke_with_the_same_child_token(self) -> None:
        self.assertEqual(self.cart.get(CARTS, headers=self.child).json(), [])
        for _ in range(2):
            grant = self.cart.put(CHILD_GRANT, headers=self.primary)
            self.assertEqual((grant.status_code, grant.content), (204, b""))
        self.assertEqual(self.cart.get(f"{REGULAR}/grants", headers=self.primary).json(), ["user-child"])
        [cart] = self.cart.get(CARTS, headers=self.child).json()
        self.assertEqual(cart["id"], "regular-cart")
        self.assertTrue(cart["can_edit_items"])
        self.assertFalse(cart["can_manage_access"])
        self.assertEqual(self.set_milk(REGULAR, self.child, 2).status_code, 200)
        self.assertEqual(self.set_milk(REGULAR, self.primary, 1).status_code, 200)
        expected = [
            {"sku": "milk", "contributor_id": "user-child", "quantity": 2},
            {"sku": "milk", "contributor_id": "user-primary", "quantity": 1},
        ]
        self.assertEqual(self.cart.get(REGULAR, headers=self.primary).json()["contributions"], expected)
        self.assertEqual(self.cart.get(REGULAR, headers=self.child).json()["contributions"], expected)

        for _ in range(2):
            revoke = self.cart.delete(CHILD_GRANT, headers=self.primary)
            self.assertEqual((revoke.status_code, revoke.content), (204, b""))
        self.assertEqual(self.set_milk(REGULAR, self.child, 9).status_code, 404)
        self.assertEqual(self.cart.get(REGULAR, headers=self.child).status_code, 404)
        self.assertEqual(self.cart.get(CARTS, headers=self.child).json(), [])
        self.assertEqual(self.cart.get(REGULAR, headers=self.primary).json()["contributions"], expected)
        self.assertEqual(self.set_milk(REGULAR, self.primary, 0).json()["quantity"], 0)
        self.assertEqual(self.cart.get(REGULAR, headers=self.primary).json()["contributions"], expected[:1])

    def test_child_cart_and_primary_capabilities(self) -> None:
        created = self.cart.post(CARTS, headers=self.child, json={"name": "  School snacks  "})
        self.assertEqual(created.status_code, 403)
        created = self.cart.post(CARTS, headers=self.primary, json={"name": "  School snacks  "})
        self.assertEqual(created.status_code, 201)
        child_cart = f"{CARTS}/{created.json()['id']}"
        self.assertEqual(created.json()["name"], "School snacks")
        self.assertFalse(created.json()["adults_only"])
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 204)
        self.assertEqual(self.cart.delete(CHILD_GRANT, headers=self.primary).status_code, 204)
        self.assertEqual(self.cart.put(f"{child_cart}/grants/user-child", headers=self.primary).status_code, 204)
        self.assertEqual(self.set_milk(child_cart, self.child, 2).status_code, 200)
        primary_view = self.cart.get(child_cart, headers=self.primary).json()
        self.assertTrue(primary_view["can_manage_access"])
        self.assertTrue(primary_view["can_edit_items"])
        self.assertEqual(
            self.cart.get(f"{child_cart}/grants", headers=self.primary).json(),
            ["user-adult", "user-child"],
        )
        effective = self.cart.get(f"{child_cart}/grants?include_creator=true", headers=self.primary)
        self.assertEqual(effective.json(), ["user-adult", "user-child"])
        self.assertEqual(self.set_milk(child_cart, self.primary, 2).status_code, 200)
        self.assertEqual(self.cart.delete(f"{child_cart}/grants/user-primary", headers=self.primary).status_code, 204)
        self.assertEqual(self.set_milk(child_cart, self.primary, 1).status_code, 200)
        self.assertEqual(self.cart.get(f"{child_cart}/grants", headers=self.child).status_code, 403)
        self.assertEqual(self.cart.put(f"{child_cart}/grants/user-primary", headers=self.child).status_code, 403)
        self.assertEqual(self.cart.get(f"{HOUSEHOLD}/members", headers=self.child).status_code, 403)
        members = self.cart.get(f"{HOUSEHOLD}/members", headers=self.primary).json()
        self.assertIn({"id": "user-child", "display_name": "Demo Child"}, members)

    def test_primary_can_revoke_and_restore_creator_access(self) -> None:
        created = self.cart.post(CARTS, headers=self.primary, json={"name": "School snacks"}).json()
        cart_id = created["id"]
        cart = f"{CARTS}/{cart_id}"
        grant = f"{cart}/grants/user-child"
        self.assertEqual(self.cart.put(grant, headers=self.primary).status_code, 204)
        edit = self.set_milk(cart, self.primary, 2).json()
        self.assertEqual(self.cart.delete(grant, headers=self.primary).status_code, 204)
        self.assertEqual(self.cart.delete(grant, headers=self.primary).status_code, 204)
        self.assertEqual(
            self.cart.get(f"{cart}/grants?include_creator=true", headers=self.primary).json(),
            ["user-adult"],
        )
        self.assertEqual(self.cart.get(CARTS, headers=self.child).json(), [])
        self.assertEqual(self.cart.get(cart, headers=self.child).status_code, 404)
        self.assertEqual(self.set_milk(cart, self.child, 9).status_code, 404)
        view = live_view(
            SimpleNamespace(app=familycart_app), "demo-household", CHILD.identity, cart_id,
            {"revision": edit["revision"], "carts": {cart_id: [edit]}},
        )
        self.assertEqual(view["carts"], [])
        self.assertIsNone(view["cart"])

        initialize_database(self.db_path)
        self.assertEqual(self.cart.get(cart, headers=self.child).status_code, 404)
        self.assertEqual(self.cart.get(cart, headers=self.primary).json()["contributions"][0]["quantity"], 2)
        self.assertEqual(self.cart.put(grant, headers=self.primary).status_code, 204)
        self.assertEqual(
            self.cart.get(f"{cart}/grants", headers=self.primary).json(),
            ["user-adult", "user-child"],
        )
        self.assertEqual(self.set_milk(cart, self.child, 3).status_code, 200)
        self.assertEqual(
            self.cart.get(cart, headers=self.child).json()["contributions"],
            self.cart.get(cart, headers=self.primary).json()["contributions"],
        )

    def test_only_adult_primaries_can_create_adults_only_carts(self) -> None:
        capabilities = f"{HOUSEHOLD}/capabilities"
        self.assertTrue(self.cart.get(capabilities, headers=self.primary).json()["can_create_adults_only_cart"])
        self.assertFalse(self.cart.get(capabilities, headers=self.child).json()["can_create_adults_only_cart"])
        payload = {"name": "Adult shopping", "adults_only": True}
        self.assertEqual(self.cart.post(CARTS, headers=self.child, json=payload).status_code, 403)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO household_members VALUES ('demo-household', 'adult-member', 'member', 1)")
            db.execute("INSERT INTO household_members VALUES ('demo-household', 'minor-primary', 'primary', 1)")
        for identity in (Identity("adult-member", True), Identity("minor-primary", False)):
            headers = {"Authorization": f"Bearer {issue_token(identity, self.secret)}"}
            self.assertFalse(self.cart.get(capabilities, headers=headers).json()["can_create_adults_only_cart"])
            self.assertEqual(self.cart.post(CARTS, headers=headers, json=payload).status_code, 403)
        created = self.cart.post(CARTS, headers=self.primary, json=payload)
        self.assertEqual(created.status_code, 201)
        self.assertTrue(created.json()["adults_only"])
        cart = f"{CARTS}/{created.json()['id']}"
        self.assertEqual(self.cart.get(cart, headers=self.child).status_code, 404)
        self.assertEqual(self.cart.put(f"{cart}/grants/user-child", headers=self.primary).status_code, 403)
        restricted_item = f"{cart}/items/cigarette-pack-20/contributions/me"
        self.assertEqual(self.cart.put(restricted_item, headers=self.primary, json={"quantity": 1}).status_code, 200)

    def test_visibility_membership_and_input_validation(self) -> None:
        self.assertEqual(self.cart.get(REGULAR).status_code, 401)
        self.assertEqual(self.cart.get(ADULTS_ONLY, headers=self.child).status_code, 404)
        self.assertEqual(self.cart.put(f"{ADULTS_ONLY}/grants/user-child", headers=self.primary).status_code, 403)
        self.assertEqual(self.cart.put(f"{REGULAR}/grants/unknown", headers=self.primary).status_code, 404)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO households VALUES ('elsewhere')")
            db.execute("INSERT INTO household_members VALUES ('elsewhere', 'user-primary', 'primary', 1)")
            db.execute("INSERT INTO carts VALUES ('outside-cart', 'elsewhere', 'user-primary', 'Outside', 0)")
            db.execute("INSERT INTO household_members VALUES ('demo-household', 'unknown', 'member', 1)")
        self.assertEqual(self.cart.put(f"{ADULTS_ONLY}/grants/unknown", headers=self.primary).status_code, 403)
        self.assertEqual(self.cart.get(f"{CARTS}/outside-cart", headers=self.primary).status_code, 404)
        outside = "/api/v1/households/elsewhere/carts/outside-cart"
        self.assertEqual(self.cart.get(outside, headers=self.child).status_code, 404)
        self.assertEqual(self.set_milk(outside, self.child, 1).status_code, 404)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("INSERT INTO cart_grants VALUES ('demo-household', 'adults-only-cart', 'user-child')")
        self.assertEqual(self.set_milk(ADULTS_ONLY, self.child, 1).status_code, 404)
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 204)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("UPDATE household_members SET active = 0 WHERE user_id = 'user-child'")
        self.assertEqual(self.set_milk(REGULAR, self.child, 1).status_code, 404)
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 404)
        for quantity in (-1, 1.5, 1.0, True, "2", 2**63):
            with self.subTest(quantity=quantity):
                self.assertEqual(self.set_milk(REGULAR, self.primary, quantity).status_code, 422)
        self.assertEqual(self.cart.post(CARTS, headers=self.primary, json={"name": " "}).status_code, 422)

    def test_stage_one_database_upgrades_without_replacing_existing_data(self) -> None:
        # Reconstruct the Stage 1 table set and keep a pre-existing cart change.
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute("DROP TABLE cart_item_contributions")
            db.execute("DROP TABLE cart_grants")
            db.execute("DROP INDEX IF EXISTS carts_household_id_idx")
            db.execute("UPDATE carts SET name = 'Existing name' WHERE id = 'regular-cart'")
        initialize_database(self.db_path)
        self.assertEqual(self.cart.get(REGULAR, headers=self.primary).json()["name"], "Existing name")
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 204)
        self.assertEqual(self.set_milk(REGULAR, self.child, 4).status_code, 200)
        initialize_database(self.db_path)
        self.assertEqual(self.cart.get(REGULAR, headers=self.child).json()["contributions"], [
            {"sku": "milk", "contributor_id": "user-child", "quantity": 4},
        ])

    def test_quantity_adjustment_and_removal_permissions(self) -> None:
        item = f"{REGULAR}/items/milk/contributions"
        shared_item = f"{REGULAR}/items/milk"
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 204)
        for delta, quantity in ((1, 1), (1, 2), (-1, 1), (-1, 0), (-1, 0), (1, 1)):
            result = self.cart.patch(shared_item, headers=self.primary, json={"delta": delta})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()["quantity"], quantity)
        self.assertEqual(self.cart.patch(shared_item, headers=self.child, json={"delta": 1}).json()["quantity"], 2)
        self.assertEqual(self.cart.patch(shared_item, headers=self.child, json={"delta": -1}).json()["quantity"], 1)
        self.assertEqual(self.cart.patch(shared_item, headers=self.child, json={"delta": 1}).json()["quantity"], 2)
        self.assertEqual(self.cart.patch(shared_item, headers=self.primary, json={"delta": -1}).json()["quantity"], 1)
        self.assertEqual(self.cart.patch(shared_item, headers=self.primary, json={"delta": 0}).status_code, 422)
        self.assertEqual(self.cart.delete(f"{item}/user-primary", headers=self.child).status_code, 403)
        self.assertEqual(self.cart.delete(shared_item, headers=self.child).status_code, 403)
        removed = self.cart.delete(shared_item, headers=self.primary)
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(removed.json()["quantity"], 0)
        self.assertEqual(self.cart.get(REGULAR, headers=self.child).json()["contributions"], [])

    def test_household_creator_is_the_permanent_primary(self) -> None:
        adult = self.sign_in("adult@example.test", "adult-demo-2026")
        members = f"{HOUSEHOLD}/members"
        promote = f"{members}/user-adult/primary"
        self.assertEqual(self.cart.get(CARTS, headers=adult).json(), [])
        self.assertEqual(self.cart.put(promote, headers=adult).status_code, 403)
        self.assertEqual(self.cart.put(f"{members}/user-child/primary", headers=self.primary).status_code, 409)
        self.assertEqual(self.cart.put(f"{members}/missing/primary", headers=self.primary).status_code, 409)
        self.assertEqual(self.cart.put(promote, headers=self.primary).status_code, 409)
        initialize_database(self.db_path)
        self.assertEqual(self.cart.put(promote, headers=self.primary).status_code, 409)
        self.assertEqual(self.cart.get(ADULTS_ONLY, headers=adult).status_code, 404)
        self.assertFalse(self.cart.get(f"{HOUSEHOLD}/capabilities", headers=adult).json()["can_create_adults_only_cart"])
        listed = self.cart.get(f"{members}?include_roles=true", headers=self.primary).json()
        self.assertIn({"id": "user-adult", "display_name": "Demo Adult", "role": "member", "is_adult": True}, listed)

    def test_cart_deletion_requires_primary_or_creator_and_survives_restart(self) -> None:
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 204)
        self.assertFalse(self.cart.get(REGULAR, headers=self.child).json()["can_delete_cart"])
        self.assertEqual(self.cart.delete(REGULAR, headers=self.child).status_code, 403)
        created = self.cart.post(CARTS, headers=self.primary, json={"name": "Disposable cart"}).json()
        own_cart = f"{CARTS}/{created['id']}"
        own_grant = f"{own_cart}/grants/user-child"
        self.assertEqual(self.cart.put(own_grant, headers=self.primary).status_code, 204)
        self.assertEqual(self.set_milk(own_cart, self.child, 4).status_code, 200)
        self.assertEqual(self.cart.delete(own_grant, headers=self.primary).status_code, 204)
        self.assertEqual(self.cart.delete(own_cart, headers=self.child).status_code, 404)
        self.assertEqual(self.cart.put(own_grant, headers=self.primary).status_code, 204)
        self.assertFalse(self.cart.get(own_cart, headers=self.child).json()["can_delete_cart"])
        self.assertEqual(self.cart.delete(own_cart, headers=self.child).status_code, 403)
        self.assertEqual(self.cart.delete(own_cart, headers=self.primary).status_code, 204)
        self.assertEqual(self.cart.delete(REGULAR, headers=self.primary).status_code, 204)
        initialize_database(self.db_path)
        for deleted in (own_cart, REGULAR):
            self.assertEqual(self.cart.get(deleted, headers=self.primary).status_code, 404)
            self.assertEqual(self.set_milk(deleted, self.primary, 1).status_code, 404)
        view = live_view(SimpleNamespace(app=familycart_app), "demo-household", CHILD.identity,
                         created["id"], {"revision": 1, "carts": {created["id"]: []}})
        self.assertIsNone(view["cart"])

    def test_catalogue_items_can_be_added_and_removed_with_age_rules(self) -> None:
        products = self.cart.get("/api/v1/products", headers=self.primary).json()
        self.assertEqual(len(products), 20)
        self.assertEqual(len(self.cart.get("/api/v1/products", headers=self.child).json()), 19)
        self.assertEqual(
            self.cart.get(
                "/api/v1/products", params={"q": "cigarette"}, headers=self.child
            ).json(),
            [],
        )
        restricted = next(product for product in products if product["min_age"] == 18)
        self.assertEqual(
            self.cart.get(
                f"/api/v1/products/{restricted['sku']}", headers=self.child
            ).status_code,
            404,
        )
        self.assertEqual(self.cart.put(CHILD_GRANT, headers=self.primary).status_code, 204)
        restricted_path = f"{REGULAR}/items/{restricted['sku']}/contributions/me"
        self.assertEqual(self.cart.put(restricted_path, headers=self.child, json={"quantity": 1}).status_code, 404)
        self.assertEqual(self.cart.put(restricted_path, headers=self.primary, json={"quantity": 1}).status_code, 403)
        for product in products:
            with self.subTest(sku=product["sku"]):
                cart = ADULTS_ONLY if product["min_age"] else REGULAR
                url = f"{cart}/items/{product['sku']}/contributions/me"
                self.assertEqual(self.cart.put(url, headers=self.primary, json={"quantity": 2}).status_code, 200)
                self.assertEqual(self.cart.put(url, headers=self.primary, json={"quantity": 0}).status_code, 200)
        self.assertEqual(self.cart.get(REGULAR, headers=self.primary).json()["contributions"], [])
        self.assertEqual(self.cart.get(ADULTS_ONLY, headers=self.primary).json()["contributions"], [])
