import os
import secrets
import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.auth_service.app import app as auth_app
from backend.services.auth_service.google_auth import GoogleIdentity, InvalidGoogleCredential
from backend.services.familycart_service.app import app as familycart_app
from backend.shared.tokens import Identity, issue_token

CARTS_URL = "/api/v1/households/demo-household/carts"


class FirstSliceTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.db_path = Path(folder) / "test.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(
            patch.dict(
                os.environ,
                {
                    "FAMILYCART_DB_PATH": str(self.db_path),
                    "FAMILYCART_JWT_SECRET": self.secret,
                    "FAMILYCART_GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
                },
            )
        )
        identities = {
            "primary@example.test": GoogleIdentity("user-primary", "primary@gmail.com", "Primary Customer", True),
            "demo-child": GoogleIdentity("user-child", "child@gmail.com", "Child Member"),
            "adult@example.test": GoogleIdentity("user-adult", "adult@gmail.com", "Adult Member", True),
        }
        self.enterContext(patch(
            "backend.services.auth_service.app.verify_google_credential",
            side_effect=lambda credential, _client_id: identities.get(credential[:credential.index("|")])
            or (_ for _ in ()).throw(InvalidGoogleCredential("Google sign-in could not be verified")),
        ))
        self.auth = self.enterContext(TestClient(auth_app))
        self.familycart = self.enterContext(TestClient(familycart_app))

    def sign_in(self, identifier: str, password: str, user_id: str) -> dict[str, str]:
        birth = f"{datetime.now(timezone.utc).year - 10}-01-01" if user_id == "user-child" else "2000-01-01"
        response = self.auth.post(
            "/api/v1/auth/google", json={"credential": f"{identifier}|" + "x" * 100, "date_of_birth": birth},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user"]["id"], user_id)
        self.assertTrue(body["user"]["display_name"])
        return {"Authorization": f"Bearer {body['access_token']}"}

    def test_primary_sees_both_seeded_carts(self) -> None:
        headers = self.sign_in("primary@example.test", "primary-demo-2026", "user-primary")
        response = self.familycart.get(CARTS_URL, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            [
                {
                    "id": "adults-only-cart",
                    "name": "Adults-Only Cart",
                    "adults_only": True,
                    "can_edit_items": True,
                    "can_manage_access": True,
                    "can_checkout": True,
                },
                {
                    "id": "regular-cart",
                    "name": "Regular Cart",
                    "adults_only": False,
                    "can_edit_items": True,
                    "can_manage_access": True,
                    "can_checkout": True,
                },
            ],
        )

    def test_child_is_a_member_with_no_visible_carts(self) -> None:
        headers = self.sign_in("demo-child", "child-demo-2026", "user-child")
        response = self.familycart.get(CARTS_URL, headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_missing_or_invalid_token_returns_401(self) -> None:
        identity = Identity(user_id="user-primary", is_adult=True)
        wrong_signature = issue_token(identity, secrets.token_hex(32))
        with patch("backend.shared.tokens.datetime") as clock:
            clock.now.return_value = datetime.now(timezone.utc) - timedelta(hours=1)
            expired_token = issue_token(identity, self.secret)

        cases = {
            "missing header": None,
            "wrong scheme": "Basic example",
            "malformed token": "Bearer invalid",
            "wrong signature": f"Bearer {wrong_signature}",
            "expired token": f"Bearer {expired_token}",
        }
        for case, authorization in cases.items():
            with self.subTest(case=case):
                headers = {"Authorization": authorization} if authorization else {}
                response = self.familycart.get(CARTS_URL, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")

    def test_invalid_google_credential_is_rejected(self) -> None:
        response = self.auth.post(
            "/api/v1/auth/google", json={"credential": "unknown-user|" + "x" * 100}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"detail": "Google sign-in could not be verified"})

    def test_current_membership_is_checked_with_the_same_token(self) -> None:
        headers = self.sign_in("primary@example.test", "primary-demo-2026", "user-primary")
        self.assertEqual(self.familycart.get(CARTS_URL, headers=headers).status_code, 200)
        with closing(sqlite3.connect(self.db_path)) as db, db:
            db.execute(
                "UPDATE household_members SET active = 0 WHERE household_id = ? AND user_id = ?",
                ("demo-household", "user-primary"),
            )
        self.assertEqual(self.familycart.get(CARTS_URL, headers=headers).status_code, 404)
