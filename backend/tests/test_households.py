import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.familycart_service.app import app, live_view
from backend.shared.tokens import Identity, issue_token

HOME = {"label": "Home", "recipient_name": "R", "address_line1": "1 Street", "city": "Dubai",
        "latitude": 25.2, "longitude": 55.27}


class HouseholdAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.db_path = Path(folder) / "households.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(self.db_path),
            "FAMILYCART_JWT_SECRET": self.secret,
        }))
        self.client = self.enterContext(TestClient(app))

    def headers(self, user_id: str, *, adult: bool = True) -> dict[str, str]:
        token = issue_token(Identity(user_id, adult), self.secret)
        return {"Authorization": f"Bearer {token}"}

    def test_household_invitation_shared_cart_and_member_revocation(self) -> None:
        primary = self.headers("new-primary")
        member = self.headers("new-member")
        created = self.client.post("/api/v1/households", headers=primary, json={
            "name": "New Family", "display_name": "Primary User", "address": HOME,
        })
        self.assertEqual(created.status_code, 201)
        household = created.json()
        root = f"/api/v1/households/{household['id']}"
        [shared_cart] = self.client.get(f"{root}/carts", headers=primary).json()

        invitation = self.client.post(f"{root}/invitations", headers=primary).json()
        accepted = self.client.post("/api/v1/household-invitations/accept", headers=member, json={
            "invitation_code": invitation["invitation_code"], "display_name": "Invited User", "address": HOME,
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["id"], household["id"])
        self.assertEqual(
            self.client.post("/api/v1/household-invitations/accept", headers=self.headers("replay"), json={
                "invitation_code": invitation["invitation_code"], "display_name": "Replay User", "address": HOME,
            }).status_code,
            400,
        )
        self.assertEqual(len(self.client.get(f"{root}/carts", headers=member).json()), 1)
        shared_item = f"{root}/carts/{shared_cart['id']}/items/milk"
        self.assertEqual(self.client.patch(shared_item, headers=primary, json={"delta": 1}).status_code, 200)
        cannot_remove = self.client.patch(shared_item, headers=member, json={"delta": -1})
        self.assertEqual(cannot_remove.status_code, 403)
        self.assertEqual(cannot_remove.json()["detail"], "You can only remove items that you added.")
        self.assertEqual(self.client.patch(shared_item, headers=member, json={"delta": 1}).status_code, 200)
        self.assertEqual(self.client.patch(shared_item, headers=member, json={"delta": 1}).json()["quantity"], 3)
        primary_cart = self.client.get(f"{root}/carts/{shared_cart['id']}", headers=primary).json()
        member_cart = self.client.get(f"{root}/carts/{shared_cart['id']}", headers=member).json()
        self.assertEqual(primary_cart["contributions"], member_cart["contributions"])
        self.assertTrue(primary_cart["can_edit_items"])
        self.assertTrue(member_cart["can_edit_items"])

        members = self.client.get(f"{root}/members?include_roles=true", headers=primary).json()
        self.assertIn("Invited User", [row["display_name"] for row in members])
        self.assertEqual(self.client.delete(f"{root}/members/new-member", headers=primary).status_code, 204)
        revoked = self.client.patch(shared_item, headers=member, json={"delta": 1})
        self.assertEqual(revoked.status_code, 403)
        self.assertEqual(
            revoked.json()["detail"],
            "Your household access was revoked. Contact Primary User, the household admin.",
        )

    def test_members_share_addresses_with_owner_and_primary_delete_permissions(self) -> None:
        primary = self.headers("address-primary")
        member = self.headers("address-member", adult=False)
        household = self.client.post("/api/v1/households", headers=primary, json={
            "name": "Address Family", "display_name": "Address Primary", "address": HOME,
        }).json()
        root = f"/api/v1/households/{household['id']}"
        code = self.client.post(f"{root}/invitations", headers=primary).json()["invitation_code"]
        self.client.post("/api/v1/household-invitations/accept", headers=member, json={
            "invitation_code": code, "display_name": "Address Member", "address": HOME,
        })
        created = self.client.post(f"{root}/addresses", headers=member, json={
            "label": "Home", "recipient_name": "Address Member", "address_line1": "12 Market Road",
            "address_line2": "Flat 4", "city": "Delhi", "postal_code": "110001", "phone": "9876543210",
        })
        self.assertEqual(created.status_code, 201)
        address_id = created.json()["id"]
        # The primary sees everyone's; the member only their own two.
        self.assertEqual(len(self.client.get(f"{root}/addresses", headers=primary).json()), 3)
        self.assertEqual(self.client.delete(f"{root}/addresses/{address_id}", headers=primary).status_code, 204)
        self.assertEqual(len(self.client.get(f"{root}/addresses", headers=member).json()), 1)

    def test_primary_can_create_multiple_households_and_multiple_carts(self) -> None:
        primary = self.headers("multi-primary")
        first = self.client.post("/api/v1/households", headers=primary, json={
            "name": "First Family", "display_name": "Multi Primary", "address": HOME,
        })
        second = self.client.post("/api/v1/households", headers=primary, json={
            "name": "Second Family", "display_name": "Multi Primary", "address": HOME,
        })
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(len(self.client.get("/api/v1/households", headers=primary).json()), 2)

        root = f"/api/v1/households/{first.json()['id']}"
        groceries = self.client.post(f"{root}/carts", headers=primary, json={
            "name": "Weekly groceries", "adults_only": False,
        })
        party = self.client.post(f"{root}/carts", headers=primary, json={
            "name": "Party supplies", "adults_only": False,
        })
        self.assertEqual(groceries.status_code, 201, groceries.text)
        self.assertEqual(party.status_code, 201, party.text)
        self.assertEqual(len(self.client.get(f"{root}/carts", headers=primary).json()), 3)

    def test_the_unused_personal_cart_and_address_routes_are_gone(self) -> None:
        user = self.headers("route-check")
        for method, path in (("GET", "/api/v1/personal-cart"), ("PATCH", "/api/v1/personal-cart/items/milk"),
                             ("GET", "/api/v1/addresses"), ("POST", "/api/v1/addresses")):
            self.assertEqual(self.client.request(method, path, headers=user, json={}).status_code, 404, path)

    def test_household_transfer_moves_all_primary_cart_controls(self) -> None:
        primary = self.headers("transfer-primary")
        successor = self.headers("transfer-successor")
        household = self.client.post("/api/v1/households", headers=primary, json={
            "name": "Transfer Family", "display_name": "Original Primary", "address": HOME,
        }).json()
        root = f"/api/v1/households/{household['id']}"
        code = self.client.post(f"{root}/invitations", headers=primary).json()["invitation_code"]
        accepted = self.client.post("/api/v1/household-invitations/accept", headers=successor, json={
            "invitation_code": code, "display_name": "New Primary", "address": HOME,
        })
        self.assertEqual(accepted.status_code, 200)
        [cart] = self.client.get(f"{root}/carts", headers=primary).json()
        cart_path = f"{root}/carts/{cart['id']}"
        item_path = f"{cart_path}/items/milk"
        self.assertEqual(self.client.patch(item_path, headers=successor, json={"delta": 1}).status_code, 200)

        self.assertEqual(self.client.put(
            f"{root}/members/transfer-successor/age", headers=primary, json={"approved": True},
        ).status_code, 204)
        transferred_household = self.client.put(
            f"{root}/members/transfer-successor/primary", headers=primary,
        )
        self.assertEqual(transferred_household.status_code, 200, transferred_household.text)
        self.assertEqual(
            self.client.get("/api/v1/households", headers=primary).json()[0]["role"],
            "member",
        )
        self.assertEqual(
            self.client.get("/api/v1/households", headers=successor).json()[0]["role"],
            "primary",
        )
        live = live_view(
            SimpleNamespace(app=app), household["id"], Identity("transfer-successor", True),
            cart["id"], None,
        )
        self.assertEqual(live["household_role"], "primary")
        self.assertTrue(live["can_create_adults_only_cart"])

        self.assertFalse(self.client.get(cart_path, headers=primary).json()["can_manage_access"])
        self.assertTrue(self.client.get(cart_path, headers=successor).json()["can_manage_access"])
        self.assertEqual(self.client.post(f"{root}/invitations", headers=primary).status_code, 403)
        self.assertEqual(self.client.post(f"{root}/invitations", headers=successor).status_code, 201)
        self.assertEqual(
            self.client.post(f"{root}/carts", headers=primary, json={
                "name": "Forbidden old-primary cart", "adults_only": False,
            }).status_code,
            403,
        )
        new_cart = self.client.post(f"{root}/carts", headers=successor, json={
            "name": "New primary cart", "adults_only": False,
        })
        self.assertEqual(new_cart.status_code, 201, new_cart.text)
        self.assertEqual(self.client.delete(item_path, headers=primary).status_code, 403)
        removed = self.client.delete(item_path, headers=successor)
        self.assertEqual(removed.status_code, 200, removed.text)
        self.assertEqual(removed.json()["quantity"], 0)
        self.assertEqual(self.client.patch(item_path, headers=primary, json={"delta": 1}).status_code, 200)
        self.assertEqual(self.client.delete(item_path, headers=successor).status_code, 200)
        self.assertEqual(self.client.delete(cart_path, headers=primary).status_code, 403)
        new_cart_path = f"{root}/carts/{new_cart.json()['id']}"
        self.assertEqual(self.client.delete(new_cart_path, headers=successor).status_code, 204)


if __name__ == "__main__":
    unittest.main()
