import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.familycart_service.app import app
from backend.shared.tokens import Identity, issue_token


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
            "name": "New Family", "display_name": "Primary User",
        })
        self.assertEqual(created.status_code, 201)
        household = created.json()
        root = f"/api/v1/households/{household['id']}"
        [shared_cart] = self.client.get(f"{root}/carts", headers=primary).json()

        invitation = self.client.post(f"{root}/invitations", headers=primary).json()
        accepted = self.client.post("/api/v1/household-invitations/accept", headers=member, json={
            "invitation_code": invitation["invitation_code"], "display_name": "Invited User",
        })
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["id"], household["id"])
        self.assertEqual(
            self.client.post("/api/v1/household-invitations/accept", headers=self.headers("replay"), json={
                "invitation_code": invitation["invitation_code"], "display_name": "Replay User",
            }).status_code,
            400,
        )
        self.assertEqual(len(self.client.get(f"{root}/carts", headers=member).json()), 1)
        shared_item = f"{root}/carts/{shared_cart['id']}/items/milk"
        self.assertEqual(self.client.patch(shared_item, headers=member, json={"delta": 1}).status_code, 200)
        self.assertEqual(self.client.patch(shared_item, headers=member, json={"delta": 1}).json()["quantity"], 2)
        primary_cart = self.client.get(f"{root}/carts/{shared_cart['id']}", headers=primary).json()
        member_cart = self.client.get(f"{root}/carts/{shared_cart['id']}", headers=member).json()
        self.assertEqual(primary_cart["contributions"], member_cart["contributions"])
        self.assertTrue(primary_cart["can_edit_items"])
        self.assertTrue(member_cart["can_edit_items"])

        members = self.client.get(f"{root}/members?include_roles=true", headers=primary).json()
        self.assertIn("Invited User", [row["display_name"] for row in members])
        self.assertEqual(self.client.delete(f"{root}/members/new-member", headers=primary).status_code, 204)
        self.assertEqual(self.client.get(f"{root}/carts", headers=member).status_code, 404)

    def test_members_share_addresses_with_owner_and_primary_delete_permissions(self) -> None:
        primary = self.headers("address-primary")
        member = self.headers("address-member", adult=False)
        household = self.client.post("/api/v1/households", headers=primary, json={
            "name": "Address Family", "display_name": "Address Primary",
        }).json()
        root = f"/api/v1/households/{household['id']}"
        code = self.client.post(f"{root}/invitations", headers=primary).json()["invitation_code"]
        self.client.post("/api/v1/household-invitations/accept", headers=member, json={
            "invitation_code": code, "display_name": "Address Member",
        })
        created = self.client.post(f"{root}/addresses", headers=member, json={
            "label": "Home", "recipient_name": "Address Member", "address_line1": "12 Market Road",
            "address_line2": "Flat 4", "city": "Delhi", "postal_code": "110001", "phone": "9876543210",
        })
        self.assertEqual(created.status_code, 201)
        address_id = created.json()["id"]
        self.assertEqual(len(self.client.get(f"{root}/addresses", headers=primary).json()), 1)
        self.assertEqual(self.client.delete(f"{root}/addresses/{address_id}", headers=primary).status_code, 204)
        self.assertEqual(self.client.get(f"{root}/addresses", headers=member).json(), [])

    def test_personal_carts_are_isolated_per_user(self) -> None:
        first = self.headers("personal-one")
        second = self.headers("personal-two")
        item = "/api/v1/personal-cart/items/milk"
        self.assertEqual(self.client.get("/api/v1/personal-cart", headers=first).json(), {"items": []})
        self.assertEqual(self.client.patch(item, headers=first, json={"delta": 1}).json()["quantity"], 1)
        self.assertEqual(self.client.patch(item, headers=first, json={"delta": 1}).json()["quantity"], 2)
        self.assertEqual(self.client.get("/api/v1/personal-cart", headers=second).json(), {"items": []})
        self.assertEqual(
            self.client.get("/api/v1/personal-cart", headers=first).json(),
            {"items": [{"sku": "milk", "quantity": 2}]},
        )
        self.assertEqual(self.client.patch(item, headers=first, json={"delta": -1}).json()["quantity"], 1)
        self.assertEqual(self.client.patch(item, headers=first, json={"delta": -1}).json()["quantity"], 0)
        self.assertEqual(self.client.get("/api/v1/personal-cart", headers=first).json(), {"items": []})


if __name__ == "__main__":
    unittest.main()
