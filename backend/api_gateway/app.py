import asyncio
import json
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
from starlette.responses import Response

from backend.services.refresh_service.service import session_is_active
from backend.services.user_service.db import database_path, initialize_database
from backend.shared.api import API_PREFIX
from backend.shared.tokens import load_jwt_secret, verify_token


MAX_BODY_BYTES = 1024 * 1024
PUBLIC_PREFIXES = ("auth/", "sessions/")
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
        return True


def _target_for(path: str, app: FastAPI) -> str:
    if path.startswith("auth/"):
        return app.state.auth_url
    if path.startswith("sessions/"):
        return app.state.refresh_url
    if (
        path == "households"
        or path.startswith("households/")
        or path.startswith("household-invitations/")
        or path == "personal-cart"
        or path.startswith("personal-cart/")
        or path == "addresses"
        or path.startswith("addresses/")
        or path == "products"
        or path.startswith("products/")
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


def _validate_access(app: FastAPI, token: str) -> None:
    try:
        identity = verify_token(token, app.state.jwt_secret)
    except InvalidTokenError:
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if not identity.session_id or not session_is_active(
        app.state.user_db_path, identity.session_id, identity.user_id
    ):
        raise HTTPException(
            status_code=401,
            detail="Session has ended",
            headers={"WWW-Authenticate": "Bearer"},
        )


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
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3))
    app.state.limiter = SlidingWindowLimiter()
    try:
        yield
    finally:
        await app.state.http.aclose()


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
    client_ip = request.client.host if request.client else "unknown"
    sensitive = path in {
        "auth/google", "sessions/refresh",
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

    if not path.startswith(PUBLIC_PREFIXES):
        _validate_access(request.app, _bearer_token(request.headers.get("authorization")))

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
    headers = {
        name: value for name, value in request.headers.items()
        if name.lower() not in HOP_BY_HOP and name.lower() != "host"
    }
    headers["X-Forwarded-For"] = client_ip
    headers["X-Request-ID"] = request.headers.get("x-request-id") or str(uuid4())
    upstream = await request.app.state.http.request(
        request.method,
        f"{target}{API_PREFIX}/{path}",
        params=request.query_params,
        content=body,
        headers=headers,
    )
    response_headers = {
        name: value for name, value in upstream.headers.items()
        if name.lower() not in HOP_BY_HOP and name.lower() in {
            "content-type", "cache-control", "set-cookie", "www-authenticate", "retry-after"
        }
    }
    return Response(upstream.content, upstream.status_code, headers=response_headers)


@app.websocket(f"{API_PREFIX}/households/{{path:path}}")
async def websocket_proxy(websocket: WebSocket, path: str) -> None:
    await websocket.accept()
    try:
        first_message = await asyncio.wait_for(websocket.receive_text(), timeout=5)
        payload = json.loads(first_message)
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str):
            raise ValueError
        _validate_access(websocket.app, token)
    except (asyncio.TimeoutError, json.JSONDecodeError, ValueError, HTTPException):
        await websocket.close(code=4401)
        return

    target = websocket.app.state.familycart_url.replace("http://", "ws://").replace("https://", "wss://")
    query = f"?{websocket.url.query}" if websocket.url.query else ""
    try:
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

            tasks = [asyncio.create_task(client_to_upstream()), asyncio.create_task(upstream_to_client())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with suppress(WebSocketDisconnect, asyncio.CancelledError):
                    task.result()
    except (OSError, websockets.WebSocketException, WebSocketDisconnect):
        with suppress(RuntimeError):
            await websocket.close(code=1011)
