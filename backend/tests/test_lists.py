import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.familycart_service.app import app
from backend.shared.tokens import Identity, issue_token

HOME = {"label": "Home", "recipient_name": "R", "address_line1": "1 Street", "city": "Dubai",
        "latitude": 25.2, "longitude": 55.27}


class SharedShoppingListTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(Path(folder) / "lists.sqlite3"),
            "FAMILYCART_JWT_SECRET": self.secret,
            # Nothing listens here, so list notifications must fail quietly.
            "FAMILYCART_REDIS_URL": "redis://127.0.0.1:1/0",
        }))
        self.client = self.enterContext(TestClient(app))
        self.primary = self.headers("list-primary")
        self.member = self.headers("list-member")
        self.child = self.headers("list-child", adult=False)
        household = self.client.post("/api/v1/households", headers=self.primary, json={
            "name": "List Family", "display_name": "Pat Primary", "address": HOME,
        }).json()
        self.root = f"/api/v1/households/{household['id']}"
        for headers, name in ((self.member, "Morgan Member"), (self.child, "Casey Child")):
            code = self.client.post(f"{self.root}/invitations", headers=self.primary).json()["invitation_code"]
            joined = self.client.post("/api/v1/household-invitations/accept", headers=headers, json={
                "invitation_code": code, "display_name": name, "address": HOME,
            })
            self.assertEqual(joined.status_code, 200)
        [self.shared_cart] = self.client.get(f"{self.root}/carts", headers=self.primary).json()

    def headers(self, user_id: str, *, adult: bool = True) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(Identity(user_id, adult), self.secret)}"}

    def new_list(self, headers=None, name="Weekly shop") -> str:
        response = self.client.post(f"{self.root}/lists", headers=headers or self.primary, json={"name": name})
        self.assertEqual(response.status_code, 201)
        return f"{self.root}/lists/{response.json()['id']}"

    def add(self, list_path: str, headers, **body):
        return self.client.post(f"{list_path}/items", headers=headers, json=body)

    def test_members_share_lists_and_attribution(self) -> None:
        path = self.new_list(self.member)
        self.assertEqual(self.add(path, self.member, sku="milk", quantity=2).status_code, 201)
        free_text = self.add(path, self.primary, custom_name="Birthday candles", note="Blue ones")
        self.assertEqual(free_text.status_code, 201)
        self.assertEqual(free_text.json()["added_by_name"], "Pat Primary")

        # Adding the same product again bumps the quantity instead of duplicating it.
        bumped = self.add(path, self.primary, sku="milk", quantity=3)
        self.assertEqual(bumped.json()["quantity"], 5)
        self.assertEqual(bumped.json()["added_by_name"], "Morgan Member")

        detail = self.client.get(path, headers=self.child).json()
        self.assertEqual(len(detail["items"]), 2)
        self.assertFalse(detail["can_manage"])

        ticked = self.client.patch(
            f"{path}/items/{free_text.json()['id']}", headers=self.child, json={"checked": True}
        )
        self.assertEqual(ticked.status_code, 200)
        self.assertEqual(ticked.json()["checked_by_name"], "Casey Child")
        [summary] = self.client.get(f"{self.root}/lists", headers=self.primary).json()
        self.assertEqual((summary["item_count"], summary["checked_count"]), (2, 1))
        self.assertEqual(summary["created_by_name"], "Morgan Member")

    def test_item_and_list_permissions(self) -> None:
        path = self.new_list(self.member)
        primary_item = self.add(path, self.primary, custom_name="Bread").json()
        member_item = self.add(path, self.member, custom_name="Jam").json()

        denied = self.client.delete(f"{path}/items/{primary_item['id']}", headers=self.member)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.delete(f"{path}/items/{member_item['id']}", headers=self.member).status_code, 204)

        self.assertEqual(self.client.patch(path, headers=self.child, json={"name": "Mine"}).status_code, 403)
        self.assertEqual(self.client.delete(path, headers=self.child).status_code, 403)
        self.assertEqual(self.client.patch(path, headers=self.member, json={"name": "Renamed"}).json()["name"], "Renamed")
        self.assertEqual(self.client.delete(path, headers=self.primary).status_code, 204)
        self.assertEqual(self.client.get(path, headers=self.member).status_code, 404)

    def test_validation(self) -> None:
        path = self.new_list()
        self.assertEqual(self.add(path, self.primary).status_code, 422)
        self.assertEqual(self.add(path, self.primary, sku="milk", custom_name="Milk").status_code, 422)
        self.assertEqual(self.add(path, self.primary, sku="milk", quantity=0).status_code, 422)
        self.assertEqual(self.add(path, self.primary, sku="not-a-product").status_code, 404)
        self.assertEqual(self.client.post(f"{self.root}/lists", headers=self.primary, json={"name": "  "}).status_code, 422)

    def test_adult_products_hidden_from_children(self) -> None:
        adult_sku = next(sku for sku, product in app.state.products.items() if product.min_age)
        path = self.new_list()
        self.assertEqual(self.add(path, self.child, sku=adult_sku).status_code, 404)
        adult_item = self.add(path, self.primary, sku=adult_sku).json()
        self.add(path, self.primary, sku="milk")
        self.assertEqual([item["sku"] for item in self.client.get(path, headers=self.child).json()["items"]], ["milk"])
        self.assertEqual(self.client.get(f"{self.root}/lists", headers=self.child).json()[0]["item_count"], 1)
        self.assertEqual(
            self.client.patch(f"{path}/items/{adult_item['id']}", headers=self.child, json={"checked": True}).status_code,
            404,
        )

    def test_move_to_shared_cart(self) -> None:
        adult_sku = next(sku for sku, product in app.state.products.items() if product.min_age)
        path = self.new_list()
        milk = self.add(path, self.member, sku="milk", quantity=2).json()
        eggs = self.add(path, self.member, sku="eggs").json()
        note = self.add(path, self.member, custom_name="Ask about cake").json()
        adult = self.add(path, self.primary, sku=adult_sku).json()

        moved = self.client.post(f"{path}/move-to-cart", headers=self.member, json={
            "target": self.shared_cart["id"], "item_ids": [milk["id"], note["id"]],
        }).json()
        self.assertEqual(moved["moved"], [milk["id"]])
        self.assertEqual(moved["skipped"], [{"id": note["id"], "reason": "Not a catalogue product"}])
        cart = self.client.get(f"{self.root}/carts/{self.shared_cart['id']}", headers=self.primary).json()
        self.assertIn({"sku": "milk", "contributor_id": "list-member", "quantity": 2}, cart["contributions"])
        detail = self.client.get(path, headers=self.member).json()
        self.assertTrue(next(item for item in detail["items"] if item["id"] == milk["id"])["checked"])

        # Restricted products still need an adults-only cart.
        refused = self.client.post(f"{path}/move-to-cart", headers=self.primary, json={
            "target": self.shared_cart["id"], "item_ids": [adult["id"]],
        }).json()
        self.assertEqual(refused["moved"], [])
        self.assertEqual(refused["skipped"][0]["reason"], "This product requires an adults-only cart")

        # There is no personal cart any more; "personal" is just an unknown cart.
        personal = self.client.post(f"{path}/move-to-cart", headers=self.member, json={
            "target": "personal", "item_ids": [eggs["id"]],
        })
        self.assertEqual(personal.status_code, 404)

    def test_move_requires_cart_access(self) -> None:
        private = self.client.post(f"{self.root}/carts", headers=self.primary, json={"name": "Private"}).json()
        self.client.delete(f"{self.root}/carts/{private['id']}/grants/list-member", headers=self.primary)
        path = self.new_list()
        milk = self.add(path, self.member, sku="milk").json()
        response = self.client.post(f"{path}/move-to-cart", headers=self.member, json={
            "target": private["id"], "item_ids": [milk["id"]],
        })
        self.assertEqual(response.status_code, 403)

    def test_clear_checked_respects_ownership(self) -> None:
        path = self.new_list()
        mine = self.add(path, self.member, custom_name="Mine").json()
        theirs = self.add(path, self.primary, custom_name="Theirs").json()
        for item in (mine, theirs):
            self.client.patch(f"{path}/items/{item['id']}", headers=self.member, json={"checked": True})
        remaining = self.client.post(f"{path}/items/clear-checked", headers=self.member).json()["items"]
        self.assertEqual([item["id"] for item in remaining], [theirs["id"]])
        self.assertEqual(self.client.post(f"{path}/items/clear-checked", headers=self.primary).json()["items"], [])

    def test_revoked_member_loses_list_access_and_other_households_are_isolated(self) -> None:
        path = self.new_list()
        self.add(path, self.member, custom_name="Left behind")
        self.assertEqual(self.client.delete(f"{self.root}/members/list-member", headers=self.primary).status_code, 204)
        self.assertEqual(self.client.get(path, headers=self.member).status_code, 403)
        self.assertEqual(self.add(path, self.member, custom_name="Sneaky").status_code, 403)
        # The removed member's item stays on the list with their name.
        [item] = self.client.get(path, headers=self.primary).json()["items"]
        self.assertEqual(item["added_by_name"], "Morgan Member")

        outsider = self.headers("outsider")
        self.assertEqual(self.client.get(f"{self.root}/lists", headers=outsider).status_code, 404)
        self.assertEqual(self.client.get(path, headers=outsider).status_code, 404)


if __name__ == "__main__":
    unittest.main()
