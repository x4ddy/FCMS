import os
import secrets
import sqlite3
import unittest
from contextlib import closing
from math import degrees
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from backend.api_gateway.app import app as gateway_app
from backend.services.familycart_service.app import app as familycart_app
from backend.services.familycart_service.db import SQL_DIR
from backend.services.maps_service.app import EARTH_RADIUS_KM, STORES, app as maps_app
from backend.services.refresh_service.service import create_session, revoke_session
from backend.shared.tokens import Identity, issue_token, verify_token

DUBAI = {"latitude": 25.2048, "longitude": 55.2708}
AL_AIN = {"latitude": 24.2075, "longitude": 55.7447}
ADDRESS = {"label": "Home", "recipient_name": "Customer", "address_line1": "Demo Building", "city": "Dubai"}
PERSONAL = "/api/v1/addresses"
HOUSEHOLD = "/api/v1/households/demo-household"
SHARED = f"{HOUSEHOLD}/addresses"
MAPS = "/api/v1/maps"


class CoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maps = self.enterContext(TestClient(maps_app))

    def test_stores_overlap_boundary_and_outside(self) -> None:
        stores = self.maps.get(f"{MAPS}/stores").json()
        self.assertEqual({store["store_id"] for store in stores}, {
            "uae-abu-dhabi", "uae-dubai", "uae-al-ain", "uae-ras-al-khaimah",
        })
        for store in stores:
            pin = {key: store[key] for key in DUBAI}
            result = self.maps.post(f"{MAPS}/resolve", json=pin).json()
            self.assertEqual((result["status"], result["store_id"], result["distance_km"]),
                             ("DELIVERABLE", store["store_id"], 0))
            self.assertEqual(store["radius_km"], 50)

        overlap = self.maps.post(f"{MAPS}/resolve", json={"latitude": 25.50, "longitude": 55.57}).json()
        self.assertEqual(overlap["store_id"], "uae-dubai")
        def south(km: float) -> dict[str, float]:
            return {**AL_AIN, "latitude": AL_AIN["latitude"] - degrees(km / EARTH_RADIUS_KM)}

        edge = self.maps.post(f"{MAPS}/resolve", json=south(50)).json()
        self.assertEqual((edge["store_id"], edge["distance_km"]), ("uae-al-ain", 50))
        outside = self.maps.post(f"{MAPS}/resolve", json=south(50.001)).json()
        self.assertEqual((outside["status"], outside["store_id"]), ("OUTSIDE_DELIVERY_AREA", None))

    def test_ties_use_store_id(self) -> None:
        dubai, al_ain = STORES[1:3]
        twin = al_ain.model_copy(update=DUBAI)
        with patch("backend.services.maps_service.app.STORES", (dubai, twin)):
            result = self.maps.post(f"{MAPS}/resolve", json=DUBAI).json()
        self.assertEqual(result["store_id"], "uae-al-ain")

    def test_invalid_coordinates_are_422(self) -> None:
        for raw in ('{"latitude":NaN,"longitude":0}', '{"latitude":1e309,"longitude":0}',
                    '{"latitude":0,"longitude":181}', '{"latitude":true,"longitude":0}',
                    '{"latitude":"25","longitude":55}', '{"latitude":25}'):
            with self.subTest(raw=raw):
                self.assertEqual(self.maps.post(f"{MAPS}/resolve", content=raw,
                                               headers={"Content-Type": "application/json"}).status_code, 422)


class SavedAddressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(self.enterContext(TemporaryDirectory()))
        self.db = self.folder / "familycart.sqlite3"
        self.secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_DB_PATH": str(self.db), "FAMILYCART_DATABASE_URL": "",
            "FAMILYCART_JWT_SECRET": self.secret,
        }))
        self.owner = self.headers("user-primary")
        self.member = self.headers("user-adult")
        self.maps = self.enterContext(TestClient(maps_app))
        self.upstream = self.enterContext(httpx.Client(transport=httpx.MockTransport(self.proxy_maps)))
        self.requests = []

    def headers(self, user_id: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {issue_token(Identity(user_id, True), self.secret)}"}

    def proxy_maps(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.maps.post(request.url.path, content=request.content, headers={"Content-Type": "application/json"})

    def test_legacy_upgrade_and_pin_survival_across_service_restart(self) -> None:
        schema = (SQL_DIR / "schema.sql").read_text(encoding="utf-8")
        with closing(sqlite3.connect(self.db)) as db, db:
            db.executescript(schema.replace("    latitude REAL,\n    longitude REAL,\n", ""))
            db.executescript((SQL_DIR / "seed.sql").read_text(encoding="utf-8"))
            db.execute("""INSERT INTO user_addresses (id, user_id, label, recipient_name, address_line1, city,
                          created_at, updated_at) VALUES ('legacy', 'user-primary', 'Home', 'Customer',
                          'Demo Building', 'Dubai', 'seed', 'seed')""")
            db.execute("""INSERT INTO household_addresses (id, household_id, label, recipient_name, address_line1,
                          city, created_by, created_at, updated_at) VALUES ('legacy-shared', 'demo-household', 'Home',
                          'Customer', 'Demo Building', 'Dubai', 'user-primary', 'seed', 'seed')""")

        path = f"{PERSONAL}/legacy"
        with TestClient(familycart_app) as cart:
            personal = cart.get(PERSONAL, headers=self.owner).json()[0]
            self.assertEqual((personal["latitude"], personal["longitude"]), (None, None))
            shared = cart.get(SHARED, headers=self.member).json()[0]
            self.assertEqual((shared["latitude"], shared["longitude"]), (None, None))
            missing = cart.post(f"{path}/resolve-store", headers=self.owner)
            self.assertEqual((missing.status_code, missing.json()["detail"]), (409, "ADDRESS_NEEDS_PIN"))
            self.assertEqual(cart.patch(path, headers=self.owner, json=DUBAI).status_code, 200)

        with TestClient(familycart_app) as cart, patch.object(familycart_app.state, "maps_http", self.upstream):
            saved = cart.get(PERSONAL, headers=self.owner).json()[0]
            self.assertEqual({key: saved[key] for key in ADDRESS | DUBAI}, ADDRESS | DUBAI)
            result = cart.post(f"{path}/resolve-store", headers=self.owner, json={"store_id": "uae-al-ain"})
            self.assertEqual(result.json()["store_id"], "uae-dubai")
            self.assertNotIn("authorization", self.requests[-1].headers)
            self.assertEqual(self.requests[-1].content, b'{"latitude":25.2048,"longitude":55.2708}')
            self.assertEqual(cart.patch(path, headers=self.owner, json=AL_AIN).status_code, 200)
            self.assertEqual(cart.post(f"{path}/resolve-store", headers=self.owner).json()["store_id"], "uae-al-ain")

    def test_address_permissions_and_unavailable_maps(self) -> None:
        with TestClient(familycart_app) as cart, patch.object(familycart_app.state, "maps_http", self.upstream):
            incomplete = cart.post(PERSONAL, headers=self.owner, json={**ADDRESS, "latitude": 25})
            self.assertEqual(incomplete.status_code, 422)
            personal = cart.post(PERSONAL, headers=self.owner, json=ADDRESS | DUBAI).json()
            path = f"{PERSONAL}/{personal['id']}"
            self.assertEqual(cart.get(PERSONAL, headers=self.member).json(), [])
            self.assertEqual(cart.patch(path, headers=self.member, json=AL_AIN).status_code, 404)
            self.assertEqual(cart.post(f"{path}/resolve-store", headers=self.member).status_code, 404)
            self.assertEqual(cart.post(f"{path}/resolve-store").status_code, 401)
            self.assertFalse(self.requests)

            shared = cart.post(SHARED, headers=self.owner, json=ADDRESS | DUBAI).json()
            shared_path = f"{SHARED}/{shared['id']}"
            self.assertEqual(cart.patch(shared_path, headers=self.member, json=AL_AIN).status_code, 403)
            self.assertEqual(cart.post(f"{shared_path}/resolve-store", headers=self.member).json()["store_id"], "uae-dubai")
            mine = cart.post(SHARED, headers=self.member, json=ADDRESS).json()
            self.assertEqual(cart.patch(f"{SHARED}/{mine['id']}", headers=self.member, json=AL_AIN).status_code, 200)
            self.assertEqual(cart.patch(f"{SHARED}/{mine['id']}", headers=self.owner, json=DUBAI).status_code, 200)

            with patch.object(self.upstream, "post", side_effect=httpx.ConnectError("down")):
                response = cart.post(f"{shared_path}/resolve-store", headers=self.member)
                self.assertEqual((response.status_code, response.json()["detail"]), (503, "MAPS_UNAVAILABLE"))
            self.assertEqual(cart.delete(f"{HOUSEHOLD}/members/user-adult", headers=self.owner).status_code, 204)
            self.assertEqual(cart.post(f"{shared_path}/resolve-store", headers=self.member).status_code, 404)
            self.assertEqual(cart.patch(shared_path, headers=self.member, json=AL_AIN).status_code, 404)


class GatewayMapsTests(unittest.TestCase):
    def test_maps_auth_privacy_and_failure(self) -> None:
        db = Path(self.enterContext(TemporaryDirectory())) / "users.sqlite3"
        secret = secrets.token_hex(32)
        self.enterContext(patch.dict(os.environ, {
            "FAMILYCART_USER_DB_PATH": str(db), "FAMILYCART_USER_DATABASE_URL": "", "FAMILYCART_JWT_SECRET": secret,
        }))
        gateway = self.enterContext(TestClient(gateway_app))
        requests = []
        maps_transport = httpx.ASGITransport(app=maps_app)

        async def forward(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return await maps_transport.handle_async_request(request)

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(forward))
        self.addCleanup(gateway.portal.call, upstream.aclose)
        gateway_app.state.maps_http = upstream

        self.assertEqual(gateway.get(f"{MAPS}/stores").status_code, 401)
        session = create_session(db, secret, Identity("user", True), "Customer")
        authenticated = {"Authorization": f"Bearer {session.access_token}", "Cookie": "private=session"}
        self.assertEqual(gateway.get(f"{MAPS}/stores?user_id=private", headers=authenticated).status_code, 200)
        result = gateway.post(f"{MAPS}/resolve", headers=authenticated, json=DUBAI)
        self.assertEqual(result.json()["store_id"], "uae-dubai")
        self.assertEqual(gateway.post(f"{MAPS}/resolve", headers=authenticated,
                                      json={**DUBAI, "recipient_name": "private"}).status_code, 422)
        self.assertEqual(len(requests), 2)
        for request in requests:
            self.assertFalse(request.url.query)
            self.assertNotIn("cookie", request.headers)
            self.assertNotIn("authorization", request.headers)
        with patch.object(upstream, "request", side_effect=httpx.ConnectError("down")):
            failed = gateway.get(f"{MAPS}/stores", headers=authenticated)
            self.assertEqual((failed.status_code, failed.json()["detail"]), (503, "MAPS_UNAVAILABLE"))
        revoke_session(db, verify_token(session.access_token, secret).session_id)
        self.assertEqual(gateway.get(f"{MAPS}/stores", headers=authenticated).status_code, 401)
