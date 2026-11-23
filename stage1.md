# FamilyCart — Stage 1

## Goal

Prove that household membership and cart access are separate. A primary holder can grant a child access to a cart, the child can add or edit an item, and revoking that grant blocks the next edit **in the same signed-in session**. FamilyCart enforces this in the API.

## Scope

- Run **Auth** and **FamilyCart** as separate FastAPI apps. Use seeded accounts and a fixture product. Add the Product service in a later increment.
- Auth signs in seeded users and issues short-lived access JWTs. Use the same `identifier` field for an adult's email or a child's username. Return a generic sign-in error for invalid credentials.
- FamilyCart owns household membership, primary holders, carts, access grants, and item contributions. It verifies the token and uses the caller's identity and age eligibility from Auth. It reads current household roles and cart grants from its own data on each request.
- React needs sign-in, a visible-carts view, a cart view with item editing, and a primary-only access panel. Use separate browser profiles to demonstrate both users.

## Seed data

| Fixture | State |
|---|---|
| Adult primary | Signs in by email. Owns the demo household and its two initial carts. |
| Child member | Signs in by username. Already belongs to the household and has zero initial cart grants. |
| Household carts | One regular cart and one adults-only cart. |
| Product | One known fixture SKU, such as milk. |

Give demo accounts local-only credentials and store their password hashes in Auth. React holds the Stage 1 access token in memory and sends it as a bearer token. JWTs carry identity and age eligibility, while cart permissions stay in FamilyCart.

## Permission rules

- Every cart action requires active membership in that cart's household. Store at most one active grant per cart and member.
- A primary can view all household carts and manage access. A cart creator or an explicitly granted member can also edit items. A primary's ability to view a cart does not by itself make them an editor.
- An adults-only cart blocks minors from viewing or editing it. The API also rejects attempts to grant a minor access to an adults-only cart.
- A child can create a cart of their own. The primary can see it through their household role.
- Store item quantities per product and contributor, so two members adding the same SKU do not overwrite attribution. FamilyCart takes the contributor ID from the authenticated request.

## API contract

FamilyCart routes are under `/households/{household_id}`. Auth routes are served separately.

| Endpoint | Request | Success |
|---|---|---|
| `POST /auth/login` | `identifier`, `password` | `200` with `access_token` and the signed-in user's ID and display name. |
| `GET /carts` | — | `200` with visible carts and UI capabilities such as `can_edit_items` and `can_manage_access`. |
| `POST /carts` | `name` | `201` with the new regular cart. |
| `GET /carts/{cart_id}` | — | `200` with cart metadata, capabilities, and item contributions. |
| `GET /members` | — | `200` with the household member IDs and display names for the primary's access panel. |
| `GET /carts/{cart_id}/grants` | — | `200` with active grants for the primary's access panel. |
| `PUT /carts/{cart_id}/grants/{member_id}` | — | `204`. Grants the member view and edit access. Repeating the request has the same effect. |
| `DELETE /carts/{cart_id}/grants/{member_id}` | — | `204`. Removes the grant. Repeating the request has the same effect. |
| `PUT /carts/{cart_id}/items/{sku}/contributions/me` | `quantity` (non-negative integer) | `200` with the updated item quantity and contributor. Zero removes the caller's contribution. |

Use `401` for missing or invalid credentials, `403` when a caller who can see a resource lacks permission for the action, `404` for households or carts outside the caller's visibility, and `422` for invalid payloads. A revoked cart grant makes the cart invisible to that member, so an edit from their stale view returns `404`. The UI refreshes the visible cart list after such a denial.

## Demo and acceptance checks

1. Sign in as the primary and child in separate browser profiles. The primary sees both seeded carts. The child sees neither.
2. The child creates a regular cart. Both the child and the primary can see it. The child can edit it.
3. The primary grants the child access to the seeded regular cart. The child can open it, add milk, and change the quantity. The primary sees the contribution and who made it.
4. The primary attempts to grant access to the adults-only cart. The API rejects the grant, and the child still cannot open the cart.
5. Leave the regular cart open in the child's browser. Revoke the grant as primary and wait for the success response. With the same child token, retry the edit from the open cart. The API returns `404`, and the primary confirms the stored quantity did not change.
6. With that same token, the child can still list carts and edit their own cart. This confirms that revocation targeted the grant.

## Later increments

Standalone signup, email verification, guardian approval and setup codes, password recovery, household management and invitations, the Product catalogue, shared shopping lists and addresses, and individual history and preferences. Store selection, checkout, split delivery, payments, and invoices are deferred.
