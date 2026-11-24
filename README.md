# FamilyCart

FamilyCart is a household grocery app. Customers can keep a personal cart, invite people to a household, and shop together in shared carts. An adult household primary can split a shared order across contributors’ delivery addresses. The purchaser can reopen saved confirmations from **Order history**.

## Repository layout

```text
frontend/                         React and Vite application
backend/
  api_gateway/                    Public HTTP and WebSocket proxy
  services/
    auth_service/                 Google login, registration, and age setup
    refresh_service/              Refresh rotation and logout
    user_service/                 User and session persistence
    familycart_service/           Households, carts, addresses, checkout, orders, and live sync
    maps_service/                 UAE store fixtures and delivery coverage
    product_service/              Product catalogue data
  shared/                         Shared API version and token utilities
  tests/                          Backend integration and security tests
  requirements.txt               Python dependencies
docs/                             Architecture and legacy implementation notes
```

The frontend uses `/api/v1` for HTTP and WebSocket requests. In development, Vite on port `5173` proxies `/api` to the Gateway on port `8000`. Auth, Refresh, FamilyCart, and Maps run on private loopback ports.

## One-click launch on Windows

Install Python 3.13, Node.js 22.12+ (or 20.19+ on the 20.x line), and Docker Desktop in Linux containers mode.

1. Create a root `.env` from [`.env.example`](.env.example) if you are setting up the project for the first time.
2. Run `python -c "import secrets; print(secrets.token_hex(32))"` and put the generated value in `FAMILYCART_JWT_SECRET`.
3. Set `FAMILYCART_GOOGLE_CLIENT_ID` and `VITE_GOOGLE_CLIENT_ID` to the same Google OAuth **Web application** client ID. Add `http://127.0.0.1:5173` to that client’s authorized JavaScript origins. [Google Sign-In setup](https://developers.google.com/identity/gsi/web/guides/get-google-api-clientid)

Double-click **Run-FamilyCart.cmd**, or run this from the repository root:

```powershell
.\Run-FamilyCart.cmd
```

The launcher loads the root `.env`, starts the PostgreSQL and Redis containers through Docker Compose, and opens [FamilyCart](http://127.0.0.1:5173) when the gateway, services, and frontend are ready. The first launch may install packages and pull Docker images. To run without opening a browser, pass `--no-browser`.

Keep the launcher window open while using the app. Press **Ctrl+C** to stop the app processes. The data containers stay running. If you update the backend code or change `.env`, restart the launcher to load the changes.

## Manual setup

Run these commands from the repository root:

```powershell
python -m pip install -r backend/requirements.txt
cd frontend
npm.cmd ci
cd ..
```

Set up the root `.env` as shown above. When launching services manually, export the backend settings to each service’s terminal. Vite reads frontend settings from the root `.env`.

## Docker data services

Local development uses two PostgreSQL databases and one persistent Redis instance:

| Container | Purpose | Host port |
|---|---|---|
| `user-db` | Google identities and refresh sessions | `5433` |
| `familycart-db` | Households, addresses, carts, quotes, inventory, and placed orders | `5434` |
| `redis` | Buffered live-cart synchronization and Pub/Sub | `6379` |

Start all data services from the repository root:

```powershell
docker compose up -d --wait
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

# Private Maps service: port 8004
python -m uvicorn backend.services.maps_service.app:app --host 127.0.0.1 --port 8004

# Public API Gateway: port 8000
python -m uvicorn backend.api_gateway.app:app --host 127.0.0.1 --port 8000
```

Then run the frontend:

```powershell
cd frontend
npm.cmd run dev
```

Open [http://127.0.0.1:5173](http://127.0.0.1:5173).

For the simulated payment-decline control during a manual run, set `FAMILYCART_DEMO_CHECKOUT=1` in the FamilyCart service terminal before starting it.

## API v1

| Capability | Public route |
|---|---|
| Google client configuration | `GET /api/v1/auth/google/config` |
| Google login or registration | `POST /api/v1/auth/google` |
| Complete age setup for an existing account | `GET/POST /api/v1/auth/age` |
| Rotate refresh token | `POST /api/v1/sessions/refresh` |
| Logout and revoke session | `POST /api/v1/sessions/logout` |
| Products | `GET /api/v1/products` |
| Personal cart | `GET /api/v1/personal-cart` |
| Adjust a personal-cart item | `PATCH /api/v1/personal-cart/items/{sku}` |
| List or create a household | `GET/POST /api/v1/households` |
| List or create a shared cart | `GET/POST /api/v1/households/{household_id}/carts` |
| Create a household invitation | `POST /api/v1/households/{household_id}/invitations` |
| Accept an invitation | `POST /api/v1/household-invitations/accept` |
| Personal delivery addresses | `GET/POST /api/v1/addresses` |
| Shared delivery addresses | `GET/POST /api/v1/households/{household_id}/addresses` |
| Delivery stores and preview | `GET /api/v1/maps/stores`, `POST /api/v1/maps/resolve` |
| Save an address pin | `PATCH /api/v1/addresses/{address_id}` or the corresponding household address route |
| Check a saved address | `POST /api/v1/addresses/{address_id}/resolve-store` or the corresponding household address route |
| Review checkout | `POST /api/v1/checkout/quotes` |
| Place or retry an order | `POST /api/v1/checkout/orders` with `Idempotency-Key` |
| List your placed orders | `GET /api/v1/orders` with optional `limit` and `before` |
| Reopen an order confirmation | `GET /api/v1/orders/{order_id}` |
| Revoke a household member | `DELETE /api/v1/households/{household_id}/members/{member_id}` |
| Household resources | `/api/v1/households/{household_id}/...` |
| Live household socket | `WS /api/v1/households/{household_id}/live` |

The Gateway exposes `GET /health` for infrastructure checks.

Google registration accepts `credential` and `date_of_birth` in `YYYY-MM-DD` format. If a date is required, Auth returns `422 DATE_OF_BIRTH_REQUIRED` and the client can resubmit with the date. Checkout placement takes a `quote_id` and an `Idempotency-Key` header. Send the same quote and key to recover a successful submission. To fetch another page of Order History, pass the returned `next_cursor` as `before`.

## Delivery coverage demo

Open **Addresses** for personal addresses or **Shared delivery addresses** under **Household account**. Click the map, drag the marker, or enter coordinates to set the pin. Select **Continue to full address** to enter the street and recipient details. Use **Check coverage** on a saved address to see its nearest eligible store and straight-line distance.

| Demo pin | Latitude | Longitude |
|---|---:|---:|
| Dubai | 25.2048 | 55.2708 |
| Al Ain | 24.2075 | 55.7447 |
| Outside coverage | 23.0 | 54.0 |

The four demo stores are in Abu Dhabi, Dubai, Al Ain, and Ras Al Khaimah. Each covers a 50 km radius. Use **Add map pin** to update an older address before checkout. The Gateway and FamilyCart service use `FAMILYCART_MAPS_URL` to reach Maps.

The browser loads a pinned copy of Leaflet 1.9.4 from unpkg. The basemap uses OpenStreetMap tiles with visible attribution. Set `VITE_MAP_TILE_URL` and `VITE_MAP_TILE_ATTRIBUTION` together in `.env` to use another provider. Set `VITE_MAP_TILE_URL=` to demo with the basemap disabled. Coordinate entry and delivery coverage work when the basemap or Leaflet is unavailable.

## Checkout demo

1. Choose **Login / Register** and continue with Google. New users enter a self-reported date of birth before FamilyCart creates the account. Returning Google users with a saved date can sign in directly. Existing accounts missing a date complete setup when they next sign in or restore a session.
2. Before joining or creating a household, save a pinned personal address and add a product from the storefront. Open **My cart** and select **Checkout**. Personal checkout requires an adult account.
3. To try split delivery, create a household and use **Invite member** to get a code for a second account. Have both members add items to the same shared cart. Save a Dubai personal address for the primary and an Al Ain address in the household address book. Open the shared cart as the adult primary and select **Checkout**. Choose Dubai as the default and assign the other contributor to Al Ain.
4. Select **Review order**, check the delivery groups and total, then select **Place order**. The review is valid for ten minutes. Orders use mock payment and zero delivery fees.
5. Open **Order history** in the account header. It lists personal and shared orders you placed, newest first. Select any order to reopen its confirmation and delivery details. Order history and confirmations are private to the purchaser.

FamilyCart rechecks the cart, addresses, prices, and inventory when you place the order. A successful placement deducts stock, saves the order, and clears the purchased cart items in one transaction. If a connection drops during submission, use **Resume checkout** to retrieve the result of the same attempt.

Set `FAMILYCART_DEMO_CHECKOUT=1` in the local `.env` to show **Simulate a payment decline** when using the launcher. A decline leaves the cart and stock intact. Each store starts with one unit of honey for testing the last-unit case. Stock and orders persist across launcher restarts.

## Authentication behavior

- Auth derives checkout eligibility from the saved date of birth. Checkout requires an account aged 18 or older.
- Access tokens expire after 10 minutes.
- Rotating refresh tokens expire after 30 days, with a 90-day absolute family lifetime.
- Refresh tokens are opaque and stored in an HttpOnly cookie.
- Logout revokes the server-side session and clears the refresh cookie.
- The Gateway validates both the JWT and its active server session before proxying protected requests.

## Verification

```powershell
python -m unittest discover -s backend/tests -v
cd frontend
node --test tests/*.test.js
npm.cmd run build
```

To run the checkout suite against PostgreSQL, set `FAMILYCART_CHECKOUT_TEST_DATABASE_URL` to a test database where the test account can create schemas, then run `python -m unittest discover -s backend/tests -p test_checkout.py -v`. The suite uses temporary schemas for its cart and user fixtures.

FastAPI documentation is available locally under `/docs` on the Auth (`8001`), FamilyCart (`8002`), Refresh (`8003`), and Maps (`8004`) services.
