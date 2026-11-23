# FamilyCart · Shared carts and catalogue

## Run the demo

Run the commands from the repository root. Use Python 3.13 and Node.js 22.12+ (or Node.js 20.19+ on the 20.x line).

```powershell
python -m pip install -r requirements.txt
python -c "import secrets; print(secrets.token_hex(32))"
```

Start Redis locally. From a separate terminal with Docker running:

```powershell
docker run --rm --name familycart-redis -p 127.0.0.1:6379:6379 redis:7.4-alpine
```

Set the same generated secret in both API terminals. Start Auth on port 8001.

```powershell
$env:FAMILYCART_JWT_SECRET = '<generated secret>'
python -m uvicorn auth.app:app --host 127.0.0.1 --port 8001
```

Start FamilyCart on port 8002. A fresh SQLite filename starts with the original demo carts. An existing database is upgraded at startup and keeps its carts, grants, and contributions. The demo uses one FamilyCart server worker per database.

```powershell
$env:FAMILYCART_JWT_SECRET = '<same generated secret>'
$env:FAMILYCART_DB_PATH = 'stage2-demo.sqlite3'
$env:FAMILYCART_REDIS_URL = 'redis://127.0.0.1:6379/0'
python -m uvicorn familycart.app:app --host 127.0.0.1 --port 8002
```

Start React in the third terminal, then open [FamilyCart](http://127.0.0.1:5173).

```powershell
cd frontend
npm.cmd ci
npm.cmd run dev
```

The sign-in form has buttons that fill the credentials for each demo account. All three accounts belong to `demo-household`.

| User | Identifier | Local demo password |
|---|---|---|
| Primary | `primary@example.test` | `primary-demo-2026` |
| Child | `demo-child` | `child-demo-2026` |
| Adult member | `adult@example.test` | `adult-demo-2026` |

## Try the grant and revoke flow

1. Sign in as primary in a regular browser window. Sign in as child in an incognito window. The child starts with an empty list. Create **School snacks** as the child and save a milk quantity.
2. As primary, open **School snacks** once it appears in the list. The household primary can view, edit, and manage all age-eligible household carts automatically.
3. As primary, open **Regular Cart** and grant access to **Demo Child**. To check the age rule, try granting the child access to **Adults-Only Cart** first. The API returns `403`.
4. Open **Regular Cart** in both windows. Set a milk quantity or remove your contribution. The edit appears in your tab as soon as the API saves it. The other tab sees the final result on the next 10-second WebSocket update.
5. Leave **School snacks** open as its creator in the child window. As primary, open its **Cart access** panel and revoke the child's access. The child's next write receives `404` immediately, and the live view returns to their cart list on the next update. Use **Grant** to restore access.
6. As primary, choose **Adults only** in **Cart type** when creating a cart. Add or remove the fictional cigarette SKU there. The child cannot open the new cart. New carts default to **Regular**.
7. Use **+** or **−** to change your own contribution by one. **Remove** clears a contribution. An authorized member can remove their own contributions, and a primary can remove anyone's.
8. Return to **Household carts** and use **Make primary** next to **Demo Adult**. The adult receives household-wide primary permissions. To test deletion, create a disposable cart and use **Delete list** as its creator or a primary.

The same routes are available in [Auth Swagger](http://127.0.0.1:8001/docs) and [FamilyCart Swagger](http://127.0.0.1:8002/docs). Use `POST /auth/login` to get an access token and paste it into **Authorize** in FamilyCart Swagger. Each browser profile can hold a different token.

## Read the code in request order

1. **`auth/app.py` and `shared/tokens.py`**. `POST /auth/login` verifies the password and signs a JWT with the caller's ID and age eligibility.
2. **`familycart/app.py`**. `cart_access()` is shared by REST and WebSocket views. `change_cart()` records a sync event in the same SQLite transaction as each successful write. `product/catalog.json` backs the authenticated catalogue endpoints.
3. **`familycart/cart_sync.py`**. The worker stages the latest snapshot for each changed cart in a Redis hash. Every ten seconds it publishes one batch per affected household and clears the staged snapshots. SQLite retains pending events until publication, so Redis can restart without losing accepted edits. The WebSocket handler checks current cart access before sending a viewer any contributions.
4. **`frontend/src/api.js`, `App.jsx` and `cartState.js`**. Vite proxies HTTP and WebSocket requests. React sends the JWT as the first socket message. It applies the submitting tab's confirmed edit immediately and keeps it visible until a snapshot reaches the edit's revision. After a disconnect, it reconnects and receives a fresh snapshot.

Run the repeatable checks with `python -m unittest discover -s tests -v`. They use a temporary SQLite database and their own JWT secret.

From `frontend`, run `node --test tests/cartState.test.js` to check how local edits reconcile with WebSocket snapshots.
