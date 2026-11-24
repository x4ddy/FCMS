# FamilyCart Stage 5 — Maps and delivery coverage

## Goal

Let a signed-in customer place a pin on a personal or household delivery address and see which of four fictional UAE stores can deliver there. Leaflet shows the address pin, the stores, and a **50 km delivery circle** around each store. When coverage overlaps, the nearest eligible store wins.

Phase 5 resolves delivery coverage and store assignment. Checkout and Order History come next. Driving routes, ETA, and address autocomplete are outside this phase.

## Starting point

- React talks to the API Gateway under `/api/v1`.
- FamilyCart already stores personal addresses at `/api/v1/addresses` and household addresses at `/api/v1/households/{household_id}/addresses`. Both flows save address text today.
- Keep the Gateway’s existing session checks and FamilyCart’s current address permissions. Personal addresses remain owner-only. Household addresses follow the current household membership and address management rules.
- PostgreSQL is the local runtime database. The existing SQLite path supports tests. Keep both schema paths compatible with old address records.
- Extend the existing address screens and Windows launcher. Do not create a second address book or change the cart sync flow.

## 5A · Resolve the store in the Maps service

Add a small private FastAPI Maps service. It owns the store fixtures and the distance calculation. It receives coordinates from the application and returns coverage and store metadata. Store definitions and coverage radii are demo data.

| Store ID | Demo location | Latitude | Longitude | Coverage radius |
|---|---|---:|---:|---:|
| `uae-abu-dhabi` | Abu Dhabi | 24.4539 | 54.3773 | 50 km |
| `uae-dubai` | Dubai | 25.2048 | 55.2708 | 50 km |
| `uae-al-ain` | Al Ain | 24.2075 | 55.7447 | 50 km |
| `uae-ras-al-khaimah` | Ras Al Khaimah | 25.7895 | 55.9432 | 50 km |

Validate latitude and longitude as finite numbers in their valid ranges. Calculate great-circle distance with Haversine. Keep stores whose unrounded distance is **at most 50 km**, then choose the shortest distance. Use `store_id` as a stable tie-breaker. Round distance for display only. If no store qualifies, return `OUTSIDE_DELIVERY_AREA` with no selected store.

Keep store selection independent of tile availability. Maps does not need names, phone numbers, address text, user IDs, or a connection to either account database.

## 5B · Add address pins and connect the API

- Add nullable `latitude` and `longitude` to personal and household addresses. Save both together or leave both empty. Add an upgrade that preserves existing rows in PostgreSQL and SQLite.
- Accept pins when creating an address and add a way to pin or move an existing address without deleting it. Use the current address ownership rules for pin changes. Legacy addresses stay readable and get an **Add map pin** action.
- For a saved address, FamilyCart checks access and reads the stored pin before asking Maps to resolve it. A caller who only has permission to read an address can check its coverage. They cannot move its pin without edit permission.
- Resolve again when a saved pin changes. Treat the current pin and store configuration as authoritative. Do not trust a store ID sent by the browser as evidence of coverage.

Proposed public contract through the Gateway:

| Endpoint | Purpose |
|---|---|
| `GET /api/v1/maps/stores` | Returns the four demo stores and their coverage radii for Leaflet. |
| `POST /api/v1/maps/resolve` | Previews a pin from `{ latitude, longitude }` without saving an address. |
| `POST /api/v1/addresses/{address_id}/resolve-store` | FamilyCart resolves a saved personal address after checking ownership. |
| `POST /api/v1/households/{household_id}/addresses/{address_id}/resolve-store` | FamilyCart resolves a saved household address after checking active membership and address access. |
| `PATCH /api/v1/addresses/{address_id}` and `PATCH /api/v1/households/{household_id}/addresses/{address_id}` | Save or update the pin under the existing address permissions. |

A valid lookup returns `200` with `status: "DELIVERABLE"` and the selected `store_id`, name, and `distance_km`, or `status: "OUTSIDE_DELIVERY_AREA"` with no store. Missing or invalid credentials return `401`. An address outside the caller’s visibility returns `404`. An accessible legacy address without a pin returns `409` with `ADDRESS_NEEDS_PIN`. Invalid coordinates return `422`. A Maps service failure returns `503` and the UI offers a retry.

## 5C · Build the Leaflet experience

- Add Leaflet to the existing address form. Draw four store markers and their 50 km circles. The user clicks the map or drags a marker to set the address pin. **Use my location** is optional and permission is requested only after the user clicks it.
- Show **Deliverable**, the selected store name, and the straight-line distance in text. Highlight the chosen store on the map. Show **Outside delivery area** when the pin sits outside every circle.
- Keep loading, missing-pin, provider failure, and outside-coverage states distinct. Clear the previous selection when the pin moves and ignore stale responses if it changes again while a request is in flight.
- Use a configurable tile source with visible attribution. Provide coordinate fields as a fallback so the demo can still check coverage if tiles fail to load. See the [Leaflet quick start](https://leafletjs.com/examples/quick-start/) and the [OpenStreetMap tile usage policy](https://operations.osmfoundation.org/policies/tiles/) if using OSM tiles.

## 5D · Integrate and verify

Wire the Maps service into the Gateway and `Run-FamilyCart.cmd` launcher. Include its health check in startup readiness. Keep a single local launch command and keep the Maps port private to the app. Update the README with the address-pin and coverage demo.

Stage 5 is complete when:

1. **Store selection:** a pin near each of the four stores selects the expected store. A pin in overlapping coverage selects only the closest. A point 50 km away is eligible. A point just beyond every store’s radius is outside coverage.
2. **Address persistence:** add a pin to an existing address, restart, and reopen it. Its text and pin are intact. Moving it from Dubai to Al Ain changes the next store result.
3. **Permissions:** another account cannot read, resolve, or move someone’s private pin by guessing the address ID. An active household member can resolve an accessible shared address. Membership revocation blocks subsequent resolution.
4. **React flow:** choose a personal address, then a shared one. Both display the correct coverage status. Move a pin quickly and verify only the latest result is shown. Check the tile-failure fallback and the outside-coverage message.
5. **Regression checks:** run the backend test suite, the frontend unit tests, and the production frontend build. Verify the one-click startup and repeat one live cart interaction.

## Handoff to Checkout

Checkout receives the selected `address_id` and uses Maps to recheck coverage and resolve a current `store_id`. Coverage confirms only that the address is within the mock delivery zone. Per-store stock, split deliveries, payment, completed orders, and order history remain future work.
