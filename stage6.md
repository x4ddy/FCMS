# FamilyCart Stage 6 — Checkout and order placement

## Goal

An eligible adult can turn a personal cart into a saved order. A household’s eligible adult primary can place one order for a shared cart and assign different delivery addresses to its contributors. FamilyCart validates coverage, prices, and stock on the server, then completes a mock payment and shows an order confirmation.

Stage 6 creates the order records that Stage 7 will use for Order History. The Phase 5 map stays a 50 km delivery check. This stage uses demo stock and simulated payment, with no real card input, charge, courier dispatch, or payment provider integration.

## Starting point and scope

The current app has separate personal and shared cart APIs, contributor quantities in shared carts, product prices in AED fils, saved personal and household addresses, and a Maps service that resolves a pin to one of four stores. The public API is versioned under `/api/v1` at the existing Gateway. Auth issues and refreshes the customer session. FamilyCart uses PostgreSQL at runtime and supports SQLite for tests.

Implement Checkout and the demo inventory as modules inside FamilyCart. Keep stock deduction, order creation, cart cleanup, and the shared cart change event in one FamilyCart database transaction. Maps keeps ownership of store coverage. Auth keeps ownership of age eligibility. There is no additional service process to launch for this increment.

## 6A · Personal checkout and eligibility

1. Add **Checkout** to the signed-in customer’s personal cart. Require a non-empty cart and an eligible adult account before review or placement.
2. Let the customer choose one pinned personal address they own. Reuse the existing address book and pin flow to fix an unpinned address. Do not accept a typed address, pin, or store ID in place of the saved address reference.
3. Resolve the address through the Phase 5 Maps flow. Missing pins, outside coverage, and a temporarily unavailable Maps service get distinct messages. An address not visible to the caller returns the same response as an unknown address.
4. Build the review from the database cart and server-side catalogue. Use integer AED fils for unit prices, line totals, and totals. Display AED 0.00 delivery for the demo. There is no fee or discount calculation in this increment.

### Age gate

The current Google sign-in implementation does not set adult eligibility to true. Treat unknown eligibility as ineligible for checkout. Use adult fixtures for automated tests, then add an Auth-owned demo age setup before enabling checkout for normal Google sign-ins. The user supplies a DOB through Auth, the server derives the eligibility flag, and the session is refreshed before continuing. This is self-reported age for the demo. FamilyCart takes the current eligibility from trusted Auth state and checks it on placement. A household role, DOB in a checkout request, or browser-supplied `is_adult` flag cannot unlock checkout.

## 6B · Shared checkout and split delivery

- Require an active household membership, visibility of the specified cart, and the household’s current adult primary. A member with edit access can still build the cart. Only the eligible primary sees payment controls and can review or place a shared order.
- Preselect a default address for every contributor. Show the contributors and quantities from the saved cart, with a selector to override the address for each contributor. One contributor uses one delivery address in this increment.
- The primary can choose their own saved personal addresses and addresses visible in this household. A member can add a separate address record to the household address book to make it available for shared checkout. Their personal address record stays private. Reject address references from other households or private address books.
- For a review with split addresses, resolve each distinct address through Maps. Combine contributors using the same address and store into one delivery group while retaining who contributed each quantity. Show the store, recipient, address, lines, and subtotal separately for each group.
- Validate that every purchased line belongs to a contributor in the current cart. Derive quantities and contributor IDs from the cart. Support partial address overrides only through the explicit default address. If a group cannot be fulfilled, keep all groups in review and let the primary fix the problem before placing the order.
- Apply existing product age restrictions to checkout. Restricted SKUs remain limited to eligible adults and adults-only shared carts. Never let a hidden or invalid line silently disappear from an otherwise successful review.

Example: the primary’s two cartons of milk go to their Dubai address. Another member’s one carton goes to an Al Ain household address. The screen shows two delivery groups and one order total. Moving both contributors to the same address gives one group and keeps both quantities attributable.

## 6C · Review, inventory, and mock payment

### Review contract

A quote is a short-lived, server-owned checkout review. Give it an opaque `quote_id`, bind it to the signed-in purchaser and cart, and use a **10-minute expiry** for the first implementation. Persist enough data to compare the reviewed cart, prices, addresses, and store assignments with the placement request. A quote does not place an order, charge a customer, or hold stock.

The quote response includes delivery groups, item summaries, quantities, store names, total in fils, currency, and expiry. Use a cart-specific revision or server-side fingerprint so an unrelated cart edit does not invalidate the quote. When the selected address or contributors change, refresh the review. Keep the cart intact through all review and validation failures.

### Stock and placement

- Seed demo inventory by `(store_id, sku)` using the four existing Maps IDs: `uae-abu-dhabi`, `uae-dubai`, `uae-al-ain`, and `uae-ras-al-khaimah`. Seed only missing inventory rows so restarting the launcher does not restore sold stock. Unseeded inventory counts as unavailable. Include a low-stock SKU for the concurrency demo.
- At placement, resolve each selected address again using the saved pin. Call Maps before opening the stock write transaction. Inside the transaction, recheck that the purchaser still has access and that the saved address, pin, cart, and pricing inputs match the reviewed version. If they changed, require a new review. A role transfer, removal, or ownership change must take effect before the next placement attempt.
- Group all requested quantities by `(store_id, sku)` before stock validation. Two contributor groups drawing from the same store must share that store’s stock. If any group falls short, fail the entire checkout and identify the affected group and items to the authorized purchaser.
- Use one consistent lock order for placement. Serialize checkout against edits to the same cart, using a lock or revision guard that **every** cart mutation also respects. Lock or conditionally decrement inventory rows in stable `(store_id, sku)` order. Recheck the reviewed quantities and current stock after acquiring the relevant locks. PostgreSQL transaction and row-locking behavior is covered in the [PostgreSQL locking documentation](https://www.postgresql.org/docs/current/explicit-locking.html).
- Simulate approval or decline in local demo mode. The mock adapter has no external side effect. A decline saves no order and leaves cart contents and inventory intact. Trying again after a known decline uses a fresh idempotency key. Default to mock approval. In local demo mode, accept an optional mock outcome of approve or decline. Use a configured demo-only gate for that override. Mock controls accept no payment-card data.

### Duplicate submissions

Require an `Idempotency-Key` header on placement. Scope it to the authenticated purchaser and persist the request fingerprint with the successful order. Include the quote ID and any demo payment outcome in the fingerprint. Use a unique database constraint to enforce one successful placement per purchaser/key and another to prevent the same quote being consumed twice.

- A retry of a successful request with the same key and payload returns the original order, even if the quote has expired or the source cart is now empty. Check for the completed attempt before making a new Maps request, and recheck under the placement transaction before writing.
- The same key with different inputs returns `409 IDEMPOTENCY_KEY_REUSED`.
- A second key cannot place an already consumed quote or repurchase the same reviewed cart quantities through a second quote. Return a conflict or the existing result as defined by the quote-consumption rule. The recommended response for a different key on a consumed quote is `409 QUOTE_ALREADY_USED`.
- After a timeout or connection drop, retry with the original key. A disabled button helps prevent double-clicks, and the database constraints cover duplicate requests from browsers or retries.

## 6D · Save the order and show confirmation

Persist one order with the purchaser ID, optional household ID, order number, timestamp, currency, total in fils, and `PLACED` status. Store delivery groups and item lines as snapshots:

| Record | Keep for confirmation and later history |
|---|---|
| Order | Purchaser, household if applicable, order number, placement time, currency, total, and mock payment outcome. |
| Delivery group | Stable store ID, store name, saved address reference, and the address/recipient text at placement. |
| Order line | Delivery group, SKU, display name, unit price in fils, quantity, and contributor ID where applicable. |

The transaction commits the order, inventory changes, removal of the purchased cart quantities, the idempotency result, and a normal shared-cart update event together. A failure rolls the set back. Use the existing cart sync after commit. The initiating screen refreshes immediately. Other authorized users see the updated shared cart through the current batched live flow.

Show the purchaser an order confirmation with the order number, total, and separate delivery groups. Provide an authorized detail route so the purchaser can reopen it. Direct order IDs do not grant access. Do not expose another purchaser’s order or other households’ delivery details. Stage 7 will add a history list and its individual visibility rules using these snapshots.

## Proposed API contract

Route these endpoints through the existing API Gateway and session validation. Use the authenticated identity for ownership checks and never accept client-computed totals or role flags.

| Endpoint | Input and result |
|---|---|
| `POST /api/v1/checkout/quotes` | Cart reference and saved address selection. Returns `201` with the quote, delivery groups, totals, and expiry. |
| `POST /api/v1/checkout/orders` | `quote_id` and `Idempotency-Key`. Returns `201` for a new order and `200` for a replay of the same successful submission. |
| `GET /api/v1/orders/{order_id}` | Returns `200` with the purchaser’s saved confirmation. An inaccessible order returns `404`. |

Use a cart reference with `kind: "personal"` or `kind: "household"`. The household form includes `household_id` and `cart_id`. An address reference has `kind: "personal"` or `kind: "household"` and `address_id`. For shared checkout, support `default_address` and an optional list of `{ contributor_id, address }` overrides. Validate that each override is unique and corresponds to a contributor in the current cart.

| Error | Client behaviour |
|---|---|
| `401` missing or ended session | Sign in again. |
| `403 CHECKOUT_FORBIDDEN` or `AGE_ELIGIBILITY_REQUIRED` | Explain the current role or age gate without opening payment controls. |
| `404` resource unavailable to this caller | Return to the cart or address selection. |
| `409 ADDRESS_NEEDS_PIN` or `OUTSIDE_DELIVERY_AREA` | Fix the pin or choose a different address for the affected group. |
| `409 QUOTE_STALE`, `QUOTE_EXPIRED`, or `QUOTE_ALREADY_USED` | Refresh checkout review or open the existing confirmation if available. |
| `409 OUT_OF_STOCK` | Show the affected items and let the purchaser edit the cart. |
| `409 IDEMPOTENCY_KEY_REUSED` | Start a fresh attempt only after checking whether the previous request placed an order. |
| `402 MOCK_PAYMENT_DECLINED` | Keep the review and cart visible. Offer a new mock attempt while the quote is still valid. |
| `422` invalid body, address roles, or incomplete selection | Highlight the affected field or contributor. |
| `503 MAPS_UNAVAILABLE` | Retry coverage validation. No stock, cart, or order changes are committed. |

## Implementation order

1. **6A** — Build the adult personal-cart review behind the Gateway. Test existing address permission and Maps coverage through the API before connecting React.
2. **6B** — Extend review to shared carts and prove that contributor assignments yield the expected groups.
3. **6C** — Add stock, mock outcome, and placement. Verify atomic rollback, quote invalidation, and replay behaviour before exposing **Place order**.
4. **6D** — Wire up the confirmation screen and existing shared-cart refresh. Keep the one-click launcher and existing data working after the migration.

## Completion checks

1. **Personal success.** An eligible adult places an order from their own cart using a reachable saved address. The confirmation is readable after restart. The correct store stock decreases once and the purchased quantities leave the cart.
2. **Split success.** A primary assigns one contributor to Dubai and another to Al Ain and places one order with two delivery groups. Each group has the correct store, address, items, and subtotal. The totals add up in integer fils.
3. **Authorization.** A minor, a member with edit access, a revoked primary, and an unrelated household caller cannot place a shared order. Guessing another user’s private address or order ID reveals nothing. Checkout does not become available through a client-supplied role or age.
4. **Revalidation.** Edit the cart, move or delete an address pin, or change a reviewed price after review. Placement requests a fresh quote and commits no order. Unpinned, uncovered, and temporarily unavailable Maps cases give different messages.
5. **Atomic failure.** Mock a decline and a stock shortage. In both cases, all delivery groups, cart quantities, and inventory remain unchanged. Force an error after the first stock update and verify that earlier updates roll back.
6. **Concurrency and retry.** Double-submit the same key, retry after a simulated lost response, and submit two different carts for the last unit at one store. The duplicate returns one order. Exactly one competing cart receives the unit. Confirm that two delivery groups within one order cannot oversell stock at the same store.
7. **Regression.** Preserve existing PostgreSQL data and verify SQLite test migrations. Run backend tests, frontend unit tests, and the production frontend build. Run the contention check against PostgreSQL and use two browser sessions to verify the shared cart clears through the existing live update flow.

**Demo finish line:** split one shared order across Dubai and Al Ain, see both deliveries on the confirmation, retry placement and see the same order number, then show that a regular member cannot place the shared order. Stage 7 starts from the saved orders.
