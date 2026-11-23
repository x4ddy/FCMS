import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.auth_service.google_auth import GoogleIdentity
from backend.services.auth_service.app import app as auth_app
from backend.shared.tokens import verify_token


class GoogleAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.secret = secrets.token_hex(32)
        self.db_path = Path(folder) / "users.sqlite3"
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_USER_DB_PATH": str(self.db_path),
            "FAMILYCART_JWT_SECRET": self.secret,
            "FAMILYCART_GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
        }))
        self.verify = self.enterContext(patch(
            "backend.services.auth_service.app.verify_google_credential",
            return_value=GoogleIdentity("google-sub-1", "person@gmail.com", "Person"),
        ))
        self.client = self.enterContext(TestClient(auth_app))

    def sign_in(self):
        return self.client.post("/api/v1/auth/google", json={"credential": "x" * 100})

    def test_verified_google_account_creates_a_reusable_session(self) -> None:
        first = self.sign_in()
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["user"], {
            "id": "google-sub-1", "display_name": "Person", "email": "person@gmail.com",
        })
        identity = verify_token(first.json()["access_token"], self.secret)
        self.assertEqual(identity.user_id, "google-sub-1")

        second = self.sign_in()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["user"]["id"], "google-sub-1")

    def test_raw_google_credential_is_never_stored(self) -> None:
        self.sign_in()
        raw = self.db_path.read_bytes()
        self.assertNotIn(("x" * 100).encode(), raw)

