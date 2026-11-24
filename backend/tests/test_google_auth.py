import os
import secrets
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.auth_service.google_auth import GoogleIdentity
from backend.services.auth_service.app import app as auth_app
from backend.services.user_service.db import transaction
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

    def sign_in(self, birth=None):
        return self.client.post("/api/v1/auth/google", json={"credential": "x" * 100, "date_of_birth": birth})

    def test_verified_google_account_creates_a_reusable_session(self) -> None:
        needs_birth = self.sign_in()
        self.assertEqual(needs_birth.status_code, 422)
        self.assertEqual(needs_birth.json()["detail"]["code"], "DATE_OF_BIRTH_REQUIRED")
        self.assertNotIn("set-cookie", needs_birth.headers)
        with transaction(self.db_path) as db:
            for table in ("users", "refresh_sessions"):
                self.assertEqual(db.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()["count"], 0)
        first = self.sign_in("2000-01-01")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["user"], {
            "id": "google-sub-1", "display_name": "Person", "email": "person@gmail.com",
            "is_adult": True, "age_setup_required": False,
        })
        identity = verify_token(first.json()["access_token"], self.secret)
        self.assertEqual(identity.user_id, "google-sub-1")
        self.assertTrue(identity.is_adult)

        second = self.sign_in()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["user"]["id"], "google-sub-1")

    def test_invalid_and_underage_registration_and_returning_age(self) -> None:
        self.assertEqual(self.sign_in("2999-01-01").status_code, 422)
        self.assertEqual(self.sign_in(0).status_code, 422)
        first = self.sign_in(f"{date.today().year - 10}-01-01")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertFalse(verify_token(first.json()["access_token"], self.secret).is_adult)
        later = self.sign_in("2000-01-01")
        self.assertEqual(later.status_code, 200)
        self.assertFalse(later.json()["user"]["is_adult"])
        self.assertFalse(later.json()["user"]["age_setup_required"])

    def test_raw_google_credential_is_never_stored(self) -> None:
        self.assertEqual(self.sign_in("2000-01-01").status_code, 200)
        raw = self.db_path.read_bytes()
        self.assertNotIn(("x" * 100).encode(), raw)

