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
4. Set the two Supabase service connection URLs in the root `.env` as described under [Supabase PostgreSQL](#supabase-postgresql).

Double-click **Run-FamilyCart.cmd**, or run this from the repository root:

```powershell
.\Run-FamilyCart.cmd
```

The launcher loads the root `.env`, connects the backend to Supabase PostgreSQL, starts Redis through Docker Compose, and opens [FamilyCart](http://127.0.0.1:5173) when the gateway, services, and frontend are ready. The first launch may install packages and pull Docker images. To run without opening a browser, pass `--no-browser`.

Keep the launcher window open while using the app. Press **Ctrl+C** to stop the app processes. The Redis container stays running. If you update the backend code or change `.env`, restart the launcher to load the changes.

## Manual setup

Run these commands from the repository root:

```powershell
python -m pip install -r backend/requirements.txt
cd frontend
npm.cmd ci
cd ..
```

Set up the root `.env` as shown above. When launching services manually, export the backend settings to each service’s terminal. Vite reads frontend settings from the root `.env`.

## Local Redis

Docker Compose runs Redis on `127.0.0.1:6379` for live cart updates and session caching. For a manual launch, start Redis from the repository root:

```powershell
docker compose up -d --wait redis
```

The former PostgreSQL services are available under the `legacy-postgres` Compose profile for accessing existing local data.

## Supabase PostgreSQL

The FamilyCart Supabase project in Mumbai hosts both stores. The `familycart_identity` schema holds users and refresh sessions. The `familycart_shopping` schema holds households, carts, lists, addresses, inventory, checkout, and orders. Each backend login can read and write its own schema. Keep both schemas private in the project's Data API settings. Google sign-in still goes through FastAPI, and Redis handles live updates.

1. For a new project, run [the FamilyCart baseline](supabase/migrations/20261125055724_familycart.sql) as the project administrator in one transaction. With an administrator connection open in `psql` from the repository root, run the following:

   ```text
   \set ON_ERROR_STOP on
   BEGIN;
   \i supabase/migrations/20261125055724_familycart.sql
   COMMIT;
   BEGIN;
   \i supabase/migrations/20261125120000_family_updates.sql
   COMMIT;
   BEGIN;
   \i supabase/migrations/20261125150000_checkout_and_roles.sql
   COMMIT;
   BEGIN;
   \i supabase/migrations/20261126090000_cart_operations.sql
   COMMIT;
   BEGIN;
   \i supabase/migrations/20261126120000_merge_tables.sql
   \i supabase/migrations/20261126172148_cart_inventory_store.sql
   \i supabase/migrations/20261126220000_child_codes.sql
   COMMIT;
   \password familycart_identity
   \password familycart_shopping
   ```

   Choose a different generated password for each login. The migration creates the tables and demo household. FamilyCart seeds store inventory on startup. Existing local users and orders stay in their original database.

   For an existing project, apply any pending migrations in filename order before starting this version. The [cart inventory migration](supabase/migrations/20261126172148_cart_inventory_store.sql) adds a nullable store to each cart. Existing carts keep their items.

2. For an IPv4 connection, in the Supabase dashboard choose **Connect → Session pooler**. Copy its host and project reference into the root `.env` using the two service logins below. Percent-encode any reserved URL characters in passwords. These settings belong to the backend.

   ```dotenv
   FAMILYCART_DB_MODE=supabase
   FAMILYCART_USER_DATABASE_URL=postgresql://familycart_identity.PROJECT_REF:IDENTITY_PASSWORD@POOLER_HOST:5432/postgres?sslmode=require
   FAMILYCART_DATABASE_URL=postgresql://familycart_shopping.PROJECT_REF:SHOPPING_PASSWORD@POOLER_HOST:5432/postgres?sslmode=require
   FAMILYCART_DB_POOL_MIN=1
   FAMILYCART_DB_POOL_MAX=3
   ```

   A direct connection uses `db.PROJECT_REF.supabase.co:5432` and the bare service role as the username. Use the session pooler on IPv4-only networks.

3. Start `.\Run-FamilyCart.cmd`. The launcher reads both service URLs from the root `.env`. Each backend checks its database role and schema at startup. Apply future schema changes as administrator migrations and grant the appropriate service role access to new tables or sequences.

### Verify connectivity and import existing data

If Windows reports `[Errno 11001] getaddrinfo failed` for the direct database address, run `.\Run-FamilyCart.cmd --connect-db` from the project folder. Sign in through the Supabase CLI when prompted. The launcher retrieves the assigned session pooler, tests both existing service logins on port 5432, and saves the verified hostname for subsequent launches. It uses npx if the Supabase CLI is unavailable.

If the root `.env` already contains direct Supabase URLs, set `FAMILYCART_SESSION_POOLER_HOST` to the hostname from **Connect → Session pooler** to use IPv4. The launcher adds the project reference to each login and preserves its password and TLS settings. Run `.\Run-FamilyCart.cmd --check-db` to verify both service logins and all 31 tables from your machine.

For a one-time transfer, stop the running FamilyCart launcher and start Docker Desktop. Run `.\Run-FamilyCart.cmd --migrate-local` from the project folder. It starts the existing legacy PostgreSQL containers, holds their tables stable, copies the data in dependency order and compares every source row with the hosted copy. It accepts identical rows on retry and reports conflicts with existing hosted data. A fresh demo seed can be updated to match the source. A successful run prints the verified row counts. Then start FamilyCart normally.

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

## Live updates and database load

Changes are saved immediately, but other members' screens are updated in batches:

1. After a change **commits**, the service drops a "household changed" note in Redis.
2. Every `FAMILYCART_SYNC_INTERVAL_SECONDS` (default 20) one worker collects the notes,
   skips households where nobody has the app open, loads the changed carts and lists
   with a few bulk queries, and publishes **one snapshot per household**.
3. Each open WebSocket filters that snapshot for its viewer **in memory** and pushes it.
   Sockets make one database read when they connect, and none per update.

When nothing changes, the worker does not touch the database at all. A slow sweep
(`FAMILYCART_SYNC_SWEEP_SECONDS`, default 300) recovers any note lost during a Redis outage.

Other load savers: PostgreSQL connections are pooled (`FAMILYCART_DB_POOL_MIN/MAX`),
the gateway caches "session is active" (see Login session checks), fast `+`/`−` taps are combined into one request, and pasted
lists are added with one bulk request.

One Redis serves everything today. Caches can be moved to a separate instance later by
setting `FAMILYCART_CACHE_REDIS_URL`; no code changes are needed.

## Login session checks

The Gateway checks every access token's signature, then whether its session is still active:

| Level | Examples | Checked in |
|---|---|---|
| Normal | View, cart items, list items | Gateway memory (5 s), then Redis, then `familycart_identity` |
| Sensitive | Invitations, cart access, addresses, checkout review, WebSockets, **any unlisted route** | Redis, then `familycart_identity` |
| Critical | Remove member, transfer ownership, place order, save date of birth | `familycart_identity` |

Routes are classified in `backend/api_gateway/route_policy.py`. Services still check permissions themselves.

Logout commits to `familycart_identity`, writes a "revoked" marker to Redis for 15 minutes and publishes a message.
Each Gateway then forgets the session and closes its WebSockets. Pub/Sub can miss a Gateway, so its
5-second memory is the backstop. If Redis could not be told, sensitive routes may accept the session
for up to `FAMILYCART_SESSION_CACHE_SECONDS` (default 30) and normal routes 5 seconds longer; critical
routes refuse it at once. With Redis down the database decides; with both down the Gateway returns 503.

## API v1

| Capability | Public route |
|---|---|
| Create a child signup code or issue a new sign-in code for an existing child | `POST /api/v1/households/{household_id}/child-codes` |
| Redeem a child code with a per-attempt UUIDv4 in `Idempotency-Key` | `POST /api/v1/auth/child` |
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
| Private lists (My lists) | `GET/POST /api/v1/my-lists` and `/api/v1/my-lists/{list_id}/...` |
| Approve or reject a member's age | `PUT /api/v1/households/{household_id}/members/{member_id}/age` |
| Shared shopping lists | `GET/POST /api/v1/households/{household_id}/lists` |
| Read, rename, or delete a list | `GET/PATCH/DELETE /api/v1/households/{household_id}/lists/{list_id}` |
| Add, edit, tick, or remove list items | `POST .../lists/{list_id}/items`, `PATCH/DELETE .../items/{item_id}` |
| Add many list items at once | `POST .../lists/{list_id}/items/bulk` |
| Several list changes at once (add, set, remove) | `POST .../lists/{list_id}/changes`, `POST /api/v1/my-lists/{list_id}/changes` |
| Clear ticked items | `POST .../lists/{list_id}/items/clear-checked` |
| Send list items to a cart | `POST .../lists/{list_id}/move-to-cart` |
| Live list changes (WebSocket) | `/api/v1/households/{household_id}/lists/live` |
| Household resources | `/api/v1/households/{household_id}/...` |
| Live household socket | `WS /api/v1/households/{household_id}/live` |

The Gateway exposes `GET /health` for infrastructure checks.

In **Household → Members**, the admin can choose **Add Child** and share the six-character, case-sensitive alphanumeric code privately. The child selects **Login / Register → Use a child code**, enters the code, and chooses a display name. The code expires after 24 hours. The first browser to redeem it joins the household. A retry from that browser can recover the initial session. After logout, the admin can choose **Get sign-in code** beside the child's name to sign them back into the same account. Children can add age-appropriate products to shared carts they can access. Only the household admin can remove a child from the household. Adult products, checkout, address creation and household creation are restricted.

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
2. Create or join a household and save a pinned delivery address. Open the cart and choose **Set delivery location** with an eligible adult account. FamilyCart resolves the delivery store and checks each product against its stock across all household contributions. Add products, select the items to order, and choose **Checkout**.
3. Choose one address served by the cart's store, then select **Review order**. To use a different store, return to the cart and change its delivery location. Check the delivery details and total before selecting **Place order**. The review is valid for ten minutes. Orders use mock payment and zero delivery fees.
4. Open **Order history** from the account menu to see the orders you placed, newest first. Select any order to reopen its confirmation and delivery details. Order history and confirmations are private to the purchaser.

FamilyCart rechecks the cart, addresses, prices, and inventory when you place the order. A successful placement deducts stock, saves the order, and clears the purchased cart items in one transaction. If a connection drops during submission, use **Resume checkout** to retrieve the result of the same attempt.

Set `FAMILYCART_DEMO_CHECKOUT=1` in the local `.env` to show **Simulate a payment decline** when using the launcher. A decline leaves the cart and stock intact. The four stores share one inventory table with one stock row per store and SKU. New rows start with 10 to 20 units. Orders and remaining stock persist across restarts.

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
