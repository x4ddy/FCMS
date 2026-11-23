# FamilyCart

FamilyCart is a React frontend backed by versioned FastAPI services behind a single API Gateway.

## Repository layout

```text
frontend/                         React and Vite application
backend/
  api_gateway/                    Public HTTP and WebSocket proxy
  services/
    auth_service/                 Google-only login and registration
    refresh_service/              Refresh rotation and logout
    user_service/                 User and session persistence
    familycart_service/           Household, cart, catalogue, and live-sync APIs
    product_service/              Product catalogue data
  shared/                         Shared API version and token utilities
  tests/                          Backend integration and security tests
  requirements.txt               Python dependencies
docs/                             Architecture and legacy implementation notes
```

All browser-facing application endpoints start with `/api/v1`. The browser talks only to the Gateway on port `8000`; service ports should remain private outside local development.

## Install

Run these commands from the repository root:

```powershell
python -m pip install -r backend/requirements.txt
python -c "import secrets; print(secrets.token_hex(32))"
cd frontend
npm.cmd ci
cd ..
```

Copy `.env.example` values into your shell or environment manager. Use the same `FAMILYCART_JWT_SECRET` for Auth, Refresh, FamilyCart, and the Gateway.

## Docker data services

Local development uses two PostgreSQL databases and one persistent Redis instance:

| Container | Purpose | Host port |
|---|---|---|
| `user-db` | Google identities and refresh sessions | `5433` |
| `familycart-db` | Households, addresses, personal carts, and shared carts | `5434` |
| `redis` | Buffered live-cart synchronization and Pub/Sub | `6379` |

Start all data services from the repository root:

```powershell
docker compose up -d
docker compose ps
```

The named Docker volumes preserve data when containers restart. Stop the containers without deleting data using:

```powershell
docker compose down
```

Do not add `-v` unless you intentionally want to delete all local database data.

Set these values in every backend terminal before starting a service:

```powershell
$env:FAMILYCART_USER_DATABASE_URL='postgresql://familycart_user:familycart_local_password@127.0.0.1:5433/familycart_users'
$env:FAMILYCART_DATABASE_URL='postgresql://familycart_app:familycart_local_password@127.0.0.1:5434/familycart'
$env:FAMILYCART_REDIS_URL='redis://127.0.0.1:6379/0'
$env:FAMILYCART_JWT_SECRET='replace-with-the-same-secret-of-at-least-32-bytes'
$env:FAMILYCART_GOOGLE_CLIENT_ID='your-web-client-id.apps.googleusercontent.com'
```

The checked-in credentials are for localhost development only. Production credentials must be supplied through the hosting provider's secret manager.

## Run locally

Open a separate terminal for each backend process and run commands from the repository root:

```powershell
# Auth service: port 8001
python -m uvicorn backend.services.auth_service.app:app --host 127.0.0.1 --port 8001

# FamilyCart service: port 8002
python -m uvicorn backend.services.familycart_service.app:app --host 127.0.0.1 --port 8002

# Refresh service: port 8003
python -m uvicorn backend.services.refresh_service.app:app --host 127.0.0.1 --port 8003

# Public API Gateway: port 8000
python -m uvicorn backend.api_gateway.app:app --host 127.0.0.1 --port 8000
```

Then run the frontend:

```powershell
cd frontend
npm.cmd run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173).

Copy `.env.example` to `.env`, then set `FAMILYCART_GOOGLE_CLIENT_ID` and `VITE_GOOGLE_CLIENT_ID` to the same Google OAuth Web Client ID. The frontend is configured to read the repository-root `.env` file.

## API v1

| Capability | Public route |
|---|---|
| Google login/register | `POST /api/v1/auth/google` |
| Rotate refresh token | `POST /api/v1/sessions/refresh` |
| Logout and revoke session | `POST /api/v1/sessions/logout` |
| Products | `GET /api/v1/products` |
| Personal cart | `GET /api/v1/personal-cart` |
| Adjust a personal-cart item | `PATCH /api/v1/personal-cart/items/{sku}` |
| List or create a household | `GET/POST /api/v1/households` |
| Create a household invitation | `POST /api/v1/households/{household_id}/invitations` |
| Accept an invitation | `POST /api/v1/household-invitations/accept` |
| Shared delivery addresses | `GET/POST /api/v1/households/{household_id}/addresses` |
| Revoke a household member | `DELETE /api/v1/households/{household_id}/members/{member_id}` |
| Household resources | `/api/v1/households/{household_id}/...` |
| Live household socket | `WS /api/v1/households/{household_id}/live` |

The unversioned `GET /health` endpoint is intentionally limited to Gateway infrastructure health checks.

## Authentication behavior

- Access tokens expire after 10 minutes.
- Rotating refresh tokens expire after 30 days, with a 90-day absolute family lifetime.
- Refresh tokens are opaque and stored in an HttpOnly cookie.
- Logout revokes the server-side session and clears the refresh cookie.
- The Gateway validates both the JWT and its active server session before proxying protected requests.

## Verification

```powershell
python -m unittest discover -s backend/tests -v
cd frontend
node --test tests/cartState.test.js
npm.cmd run build
```

The FastAPI service schemas remain available locally at ports `8001`, `8002`, and `8003` under `/docs`.
