# FamilyCart — Stage 2

## Goal

Turn Stage 1's read-only cart list into a working shared-cart demo. A primary grants a child access, the child edits a cart item, and the primary revokes the grant. The child's next edit fails **using the same JWT**, and their own cart still works.

## Starting point

Keep the two FastAPI apps, seeded Auth users, and SQLite database. `POST /auth/login` already issues a token. FamilyCart's `require_identity()` verifies it, and `list_carts()` looks up current household membership. In `demo-household`, the primary sees `regular-cart` and `adults-only-cart`. The child starts with `[]`. Keep those Stage 1 outcomes on a clean database.

## Permissions

| Caller in the cart's household | View cart | Edit items | Manage cart access |
|---|---|---|---|
| Primary with no creator or grant access | Yes | No | Yes |
| Cart creator | Yes | Yes | Only if primary |
| Member with a cart grant | Yes | Yes | Only if primary |
| Other member | No | No | No |

Active household membership is required for every action. The adults-only flag overrides any creator or grant access for callers under 18. A primary can grant access only to an active member of the same household. Refuse a grant to a minor on an adults-only cart. Use the JWT for the caller's identity and age eligibility. For grant targets in this increment, use server-side eligibility from the seed fixtures and reject unknown eligibility for adults-only grants. Stage 3 will supply eligibility for real members from Auth.

Both seeded users can create a regular cart. A child can edit their own cart, and the primary sees it through their household role. Keep the adults-only cart as a seeded fixture for this increment.

## SQLite changes

- Add `cart_grants` with one row per cart and member. The identity of the grantee comes from the route, and FamilyCart verifies their membership in the cart's household.
- Add `cart_item_contributions` with one row per cart, SKU, and contributor. Use the authenticated caller as the contributor when setting a quantity. Setting quantity to zero removes that contribution. Start with a fixture SKU such as `milk`. Two people adding milk keep separate quantities even if React displays a combined total.
- Extend `familycart/schema.sql` so starting the app against an existing Stage 1 database adds the new tables and preserves its data. Keep `seed.sql` repeatable. Use a separate demo or test database for clean runs.
- Use the same cart policy for list, detail, and writes. Check current membership, grant, and age rules inside the same SQLite transaction as an item change. Serialize permission changes and item writes so an edit started after a successful revoke cannot commit using stale access.

## API contract

The paths below are relative to `/households/{household_id}`. Keep Stage 1 login and bearer token handling as they are.

| Method and path | Input and success |
|---|---|
| `GET /carts` | `200` with visible carts and the existing capability fields. Include carts with an active grant. |
| `POST /carts` | `{ "name": "School snacks" }` → `201` with the created cart. The caller becomes its creator. |
| `GET /carts/{cart_id}` | `200` with cart metadata, capabilities, and contributions. Each contribution has `sku`, `contributor_id`, and `quantity`. |
| `GET /members` | `200` with active member IDs and display names for the primary's access panel. |
| `GET /carts/{cart_id}/grants` | `200` with the IDs of currently granted members. Primary only. |
| `PUT /carts/{cart_id}/grants/{member_id}` | `204` with no body. Grant view and edit access. Repeating it has the same effect. |
| `DELETE /carts/{cart_id}/grants/{member_id}` | `204` with no body. Remove the grant. Repeating it has the same effect. |
| `PUT /carts/{cart_id}/items/{sku}/contributions/me` | `{ "quantity": 2 }` → `200` with the updated contribution. Use a non-negative integer. A zero quantity response confirms removal. |

Return `401` for missing or invalid credentials, `403` for a visible resource when the action is forbidden, `404` when the household or cart is outside the caller's visibility, and `422` for invalid input. An adults-only grant to a minor returns `403`. After a grant is revoked, a child editing from a stale screen receives `404` because the cart is no longer visible to them.

## Build order and code walkthrough

1. **Cart creation and detail.** Start with `familycart/schema.sql` and `familycart/db.py`. Add the new tables and cart operations. Trace the current `list_carts()` in `familycart/app.py` and reuse its membership and visibility rules for detail. Run the Stage 1 tests.
2. **Grant, edit, and revoke.** Add the primary-only grant routes, then the quantity route. Follow one request from `require_identity()` through the cart permission check to the SQLite write. Prove revocation using two tokens in Swagger or API tests before connecting React.
3. **React demo.** Add sign-in, a cart list with a create action, a cart detail with a quantity editor, and the primary's access panel. Keep the JWT in memory. Use the API capability fields for controls and handle a denied edit even if the UI still had the cart open. On that denial, take the user back to their refreshed cart list.

## Done when

- The original `tests/test_slice.py` still passes. Run `python -m unittest discover -s tests -v` with repeatable checks against a temporary SQLite database.
- A child starts with no visible carts, creates a cart, edits it, and the primary can see it. The primary's auto-visible access alone does not allow item edits on the child's cart.
- A primary grants the child access to `regular-cart`. The child adds or updates milk. When both users add the same SKU, the primary sees both contributions with their correct attribution.
- A grant to the adults-only cart fails for the child. A child cannot manage grants, and a valid token cannot read or change carts outside its household.
- In two browser profiles, leave the regular cart open as the child. Revoke access as primary and wait for `204`. The child's next edit with the same JWT returns `404`, the quantity is unchanged, and the child can still edit their own cart.

Keep real onboarding and household invitations for Stage 3. The Product service, shared lists and addresses, personal history and preferences, checkout, and deployment changes are outside this increment.
