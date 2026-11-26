import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from jwt import InvalidTokenError
from pydantic import ValidationError
from starlette.responses import Response

from backend.services.user_service.db import database_path, initialize_database
from backend.shared.api import API_PREFIX
from backend.shared.cache import async_client, cache_redis_url, session_hash
from backend.shared.maps import Coordinates, MAPS_UNAVAILABLE, maps_service_url
from backend.shared.tokens import Identity, load_jwt_secret, verify_token

from .route_policy import Level, classify, is_ambiguous
from .session_auth import AuthUnavailable, SessionEnded, SessionValidator


LOGGER = logging.getLogger(__name__)
MAX_BODY_BYTES = 1024 * 1024
MAX_LIMITER_ENTRIES = 50_000
SWEEP_SECONDS = 5
SOCKET_RECHECK_SECONDS = 30
PUBLIC_AUTH = {"auth/google", "auth/google/config"}
MAP_ROUTES = {"maps/stores": "GET", "maps/resolve": "POST"}
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length",
}


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self.requests: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.monotonic()
        bucket = self.requests[key]
        while bucket and bucket[0] <= now - window_seconds:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        if len(self.requests) > MAX_LIMITER_ENTRIES:
            self.requests.pop(next(iter(self.requests)))
        return True

    def sweep(self, window_seconds: int = 60) -> None:
        cutoff = time.monotonic() - window_seconds
        for key in [key for key, bucket in self.requests.items() if not bucket or bucket[-1] <= cutoff]:
            del self.requests[key]


def _target_for(path: str, app: FastAPI) -> str:
    if path.startswith("auth/"):
        return app.state.auth_url
    if path.startswith("sessions/"):
        return app.state.refresh_url
    if path in MAP_ROUTES:
        return app.state.maps_url
    if (
        path == "households"
        or path.startswith("households/")
        or path.startswith("household-invitations/")
        or path == "my-lists"
        or path.startswith("my-lists/")
        or path == "products"
        or path.startswith("products/")
        or path in {"checkout/quotes", "checkout/orders"}
        or path == "orders"
        or path.startswith("orders/")
    ):
        return app.state.familycart_url
    raise HTTPException(status_code=404, detail="Route not found")


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return authorization[7:]


async def _validate_access(app: FastAPI, token: str, level: Level) -> Identity:
    try:
        identity = verify_token(token, app.state.jwt_secret)
    except InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    try:
        await app.state.sessions.validate(identity, level)
    except SessionEnded:
        raise HTTPException(
            status_code=401,
            detail="Session has ended",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    except AuthUnavailable:
        raise HTTPException(
            status_code=503,
            detail="Your sign-in could not be checked. Try again shortly.",
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        ) from None
    return identity


async def _sweep(app: FastAPI) -> None:
    while True:
        await asyncio.sleep(SWEEP_SECONDS)
        app.state.limiter.sweep()
        app.state.sessions.local.sweep()


def _validate_origin(request: Request) -> None:
    origin = request.headers.get("origin")
    if not origin:
        return
    configured = os.environ.get(
        "FAMILYCART_ALLOWED_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173"
    )
    allowed = {item.strip() for item in configured.split(",") if item.strip()}
    if origin not in allowed:
        raise HTTPException(status_code=403, detail="Origin is not allowed")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.jwt_secret = load_jwt_secret()
    app.state.user_db_path = database_path()
    initialize_database(app.state.user_db_path)
    app.state.auth_url = os.environ.get("FAMILYCART_AUTH_URL", "http://127.0.0.1:8001")
    app.state.familycart_url = os.environ.get("FAMILYCART_API_URL", "http://127.0.0.1:8002")
    app.state.refresh_url = os.environ.get("FAMILYCART_REFRESH_URL", "http://127.0.0.1:8003")
    app.state.maps_url = maps_service_url()
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3))
    maps_http = app.state.maps_http = httpx.AsyncClient(timeout=httpx.Timeout(4, connect=1), trust_env=False)
    app.state.limiter = SlidingWindowLimiter()
    subscriber = async_client(cache_redis_url(), socket_timeout=5, health_check_interval=5)
    app.state.sessions = SessionValidator(app.state.user_db_path, async_client(cache_redis_url()), subscriber)
    app.state.sessions.start()
    sweeper = asyncio.create_task(_sweep(app))
    try:
        yield
    finally:
        await app.state.http.aclose()
        sweeper.cancel()
        with suppress(asyncio.CancelledError):
            await sweeper
        await app.state.sessions.close()
        await maps_http.aclose()


app = FastAPI(title="FamilyCart API Gateway", lifespan=lifespan)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.api_route(
    f"{API_PREFIX}/{{path:path}}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy(path: str, request: Request) -> Response:
    if is_ambiguous(path):
        raise HTTPException(status_code=400, detail="Invalid request path")
    client_ip = request.client.host if request.client else "unknown"
    sensitive = path in {
        "auth/google", "auth/age", "sessions/refresh",
        "household-invitations/accept",
    }
    limit = 10 if sensitive else 120
    if not request.app.state.limiter.allow(f"{client_ip}:{path}", limit):
        return Response(
            content=json.dumps({"detail": "Too many requests"}),
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": "60"},
        )

    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        _validate_origin(request)

    if path not in PUBLIC_AUTH and not path.startswith("sessions/"):
        await _validate_access(
            request.app, _bearer_token(request.headers.get("authorization")), classify(request.method, path),
        )

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from None
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large")

    target = _target_for(path, request.app)
    if path in MAP_ROUTES:
        return await proxy_maps(path, request, target, body)
    headers = {
        name: value for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP and name.lower() != "host"
    }
    headers["X-Forwarded-For"] = client_ip
    headers["X-Request-ID"] = request.headers.get("x-request-id") or str(uuid4())
    try:
        upstream = await request.app.state.http.request(
            request.method,
            f"{target}{API_PREFIX}/{path}",
            params=request.query_params,
            content=body,
            headers=headers,
        )
    except httpx.ReadTimeout:
        # The service received it, so whether it took effect is unknown.
        raise HTTPException(status_code=504, detail="The service took too long to answer. Try again.") from None
    except httpx.RequestError as error:
        LOGGER.warning("Service unavailable: %s", type(error).__name__)
        raise HTTPException(
            status_code=503, detail="The service is temporarily unavailable. Try again shortly.",
            headers={"Retry-After": "5"},
        ) from None
    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name.lower() not in HOP_BY_HOP and name.lower() in {
            "content-type", "cache-control", "set-cookie", "www-authenticate", "retry-after"
        }
    }
    return Response(upstream.content, upstream.status_code, headers=response_headers)


async def proxy_maps(path: str, request: Request, target: str, body: bytes) -> Response:
    method = MAP_ROUTES[path]
    if request.method != method:
        raise HTTPException(status_code=405, detail="Method not allowed", headers={"Allow": method})
    payload = None
    if method == "POST":
        try:
            payload = Coordinates.model_validate_json(body).model_dump()
        except ValidationError:
            raise HTTPException(status_code=422, detail="A valid latitude and longitude are required") from None
    try:
        upstream = await request.app.state.maps_http.request(method, f"{target}{API_PREFIX}/{path}", json=payload)
        upstream.raise_for_status()
    except httpx.HTTPError:
        raise HTTPException(status_code=503, detail=MAPS_UNAVAILABLE) from None
    return Response(upstream.content, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.websocket(f"{API_PREFIX}/households/{{path:path}}")
async def websocket_proxy(websocket: WebSocket, path: str) -> None:
    await websocket.accept()
    try:
        first_message = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        payload = json.loads(first_message)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise ValueError
        if is_ambiguous(path):
            raise ValueError
        identity = await _validate_access(websocket.app, token, Level.SENSITIVE)
    except WebSocketDisconnect:
        return  # the browser left before identifying itself
    except HTTPException as error:
        # 4401 makes the browser renew its token; 1013 means "try again later".
        await websocket.close(code=1013 if error.status_code == 503 else 4401)
        return
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError):
        await websocket.close(code=4401)
        return

    target = websocket.app.state.familycart_url.replace("http://", "ws://").replace("https://", "wss://")
    query = f"?{websocket.url.query}" if websocket.url.query else ""
    sessions: SessionValidator = websocket.app.state.sessions
    revoked = sessions.watch(identity)

    async def close_when_session_ends() -> None:
        while True:
            try:
                await asyncio.wait_for(revoked.wait(), SOCKET_RECHECK_SECONDS)
                code = 4401
            except asyncio.TimeoutError:
                # A logout message may have been missed.
                try:
                    await sessions.recheck_socket(identity)
                    continue
                except SessionEnded:
                    code = 4401
                except AuthUnavailable:
                    code = 1013
            with suppress(RuntimeError):
                await websocket.close(code=code)
            return

    try:
        # A logout between the check and watch() is caught by this second look.
        if sessions.local.is_revoked(session_hash(identity.session_id)):
            revoked.set()
        async with websockets.connect(
            f"{target}{API_PREFIX}/households/{path}{query}"
        ) as upstream:
            await upstream.send(first_message)

            async def client_to_upstream() -> None:
                while True:
                    await upstream.send(await websocket.receive_text())

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        await websocket.send_text(message)
                except websockets.ConnectionClosed as error:
                    # Preserve application close codes such as 4401 so the
                    # browser can refresh an expired access token instead of
                    # treating every upstream closure as a generic failure.
                    code = error.code if error.code not in {1005, 1006, 1015} else 1011
                    with suppress(RuntimeError):
                        await websocket.close(code=code, reason=error.reason)

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client()),
                     asyncio.create_task(close_when_session_ends())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with suppress(WebSocketDisconnect, asyncio.CancelledError):
                    task.result()
    except (OSError, websockets.WebSocketException, WebSocketDisconnect):
        with suppress(RuntimeError):
            await websocket.close(code=1011)
    finally:
        sessions.unwatch(identity, revoked)
