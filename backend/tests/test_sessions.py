import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend.services.auth_service.app import app as auth_app
from backend.services.auth_service.google_auth import GoogleIdentity
from backend.services.refresh_service.app import app as refresh_app
from backend.services.refresh_service.cookies import REFRESH_COOKIE
from backend.services.refresh_service.service import session_is_active
from backend.shared.tokens import verify_token


class SessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.db_path = Path(folder) / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_USER_DB_PATH": str(self.db_path),
            "FAMILYCART_JWT_SECRET": self.secret,
            "FAMILYCART_GOOGLE_CLIENT_ID": "test.apps.googleusercontent.com",
        }))
        self.enterContext(patch(
            "backend.services.auth_service.app.verify_google_credential",
            return_value=GoogleIdentity("user-primary", "primary@gmail.com", "Primary Customer", True),
        ))
        self.auth = self.enterContext(TestClient(auth_app))
        self.refresh = self.enterContext(TestClient(refresh_app))

    def login(self) -> tuple[dict, str]:
        response = self.auth.post(
            "/api/v1/auth/google", json={"credential": "x" * 100, "date_of_birth": "2000-01-01"},
        )
        self.assertEqual(response.status_code, 200)
        return response.json(), response.cookies[REFRESH_COOKIE]

    def test_refresh_rotates_token_and_reuse_revokes_the_family(self) -> None:
        login, first_refresh = self.login()
        identity = verify_token(login["access_token"], self.secret)
        self.assertIsNotNone(identity.session_id)
        self.assertTrue(session_is_active(self.db_path, identity.session_id, identity.user_id))

        rotated = self.refresh.post("/api/v1/sessions/refresh", cookies={REFRESH_COOKIE: first_refresh})
        self.assertEqual(rotated.status_code, 200)
        second_refresh = rotated.cookies[REFRESH_COOKIE]
        self.assertNotEqual(first_refresh, second_refresh)
        self.assertEqual(rotated.json()["expires_in"], 600)
        self.assertTrue(rotated.json()["user"]["is_adult"])
        self.assertFalse(rotated.json()["user"]["age_setup_required"])

        replay = self.refresh.post("/api/v1/sessions/refresh", cookies={REFRESH_COOKIE: first_refresh})
        self.assertEqual(replay.status_code, 401)
        self.assertFalse(session_is_active(self.db_path, identity.session_id, identity.user_id))
        self.assertEqual(
            self.refresh.post("/api/v1/sessions/refresh", cookies={REFRESH_COOKIE: second_refresh}).status_code,
            401,
        )

    def test_logout_revokes_access_session_and_clears_cookie(self) -> None:
        login, refresh_token = self.login()
        identity = verify_token(login["access_token"], self.secret)
        response = self.refresh.post(
            "/api/v1/sessions/logout",
            headers={"Authorization": f"Bearer {login['access_token']}"},
            cookies={REFRESH_COOKIE: refresh_token},
        )
        self.assertEqual(response.status_code, 204)
        self.assertFalse(session_is_active(self.db_path, identity.session_id, identity.user_id))
        self.assertIn("familycart_refresh=", response.headers["set-cookie"])
        self.assertIn("Max-Age=0", response.headers["set-cookie"])

    def test_cross_site_refresh_is_rejected(self) -> None:
        _, refresh_token = self.login()
        response = self.refresh.post(
            "/api/v1/sessions/refresh",
            headers={"Origin": "https://attacker.example"},
            cookies={REFRESH_COOKIE: refresh_token},
        )
        self.assertEqual(response.status_code, 403)
