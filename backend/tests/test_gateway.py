import os
import secrets
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.api_gateway.app import app as gateway_app
from backend.services.refresh_service.service import create_session, revoke_session
from backend.services.user_service.db import initialize_database
from backend.shared.tokens import Identity, verify_token


class FakeUpstream:
    def __init__(self) -> None:
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        return httpx.Response(200, json={"proxied": True}, headers={"Cache-Control": "no-store"})

    async def aclose(self):
        pass


class GatewaySecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = self.enterContext(TemporaryDirectory())
        self.db_path = Path(folder) / "users.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_USER_DB_PATH": str(self.db_path),
            "FAMILYCART_JWT_SECRET": self.secret,
        }))
        initialize_database(self.db_path)
        self.client = self.enterContext(TestClient(gateway_app))
        self.fake = FakeUpstream()
        gateway_app.state.http = self.fake

    def token(self):
        return create_session(
            self.db_path, self.secret, Identity("gateway-user", True), "Gateway User"
        )

    def test_protected_route_requires_an_active_server_session(self) -> None:
        self.assertEqual(self.client.get("/api/v1/products").status_code, 401)
        session = self.token()
        headers = {"Authorization": f"Bearer {session.access_token}"}
        allowed = self.client.get("/api/v1/products", headers=headers)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json(), {"proxied": True})
        identity = verify_token(session.access_token, self.secret)
        revoke_session(self.db_path, identity.session_id)
        self.assertEqual(self.client.get("/api/v1/products", headers=headers).status_code, 401)

    def test_gateway_adds_security_headers_and_routes_public_auth(self) -> None:
        response = self.client.post("/api/v1/auth/google", json={"credential": "x" * 100})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.fake.requests[-1][1],
            "http://127.0.0.1:8001/api/v1/auth/google",
        )
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertTrue(response.headers["x-request-id"])

    def test_gateway_routes_the_versioned_household_collection(self) -> None:
        session = self.token()
        headers = {"Authorization": f"Bearer {session.access_token}"}
        response = self.client.get("/api/v1/households", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.fake.requests[-1][1],
            "http://127.0.0.1:8002/api/v1/households",
        )
        orders = self.client.get("/api/v1/orders", headers=headers)
        self.assertEqual(orders.status_code, 200)
        self.assertEqual(
            self.fake.requests[-1][1],
            "http://127.0.0.1:8002/api/v1/orders",
        )
        self.assertEqual(self.client.get("/api/v1/personal-cart", headers=headers).status_code, 404)
