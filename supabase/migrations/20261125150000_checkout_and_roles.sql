-- Checkout sessions, items waiting for the next cart, and requests to add members.
-- Run as the project administrator, in one transaction, after 20261125120000_family_updates.sql.
-- Safe to run more than once. The previous application version keeps working after it.

SET LOCAL search_path = familycart_shopping;

-- The cart that took over when a checked-out cart was retired.
ALTER TABLE deleted_carts ADD COLUMN IF NOT EXISTS next_cart_id TEXT;

-- While a member is checking out a cart its contents stand still and new items wait beside it.
CREATE TABLE IF NOT EXISTS checkout_sessions (
    cart_id TEXT PRIMARY KEY REFERENCES carts(id),
    household_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS checkout_sessions_household_idx ON checkout_sessions (household_id);

CREATE TABLE IF NOT EXISTS cart_waiting_items (
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL REFERENCES carts(id),
    sku TEXT NOT NULL,
    contributor_id TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (cart_id, sku, contributor_id)
);

CREATE INDEX IF NOT EXISTS cart_waiting_items_household_idx ON cart_waiting_items (household_id);

-- Primary users ask the admin to let someone join.
CREATE TABLE IF NOT EXISTS member_requests (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    requested_by TEXT NOT NULL,
    name TEXT NOT NULL,
    note TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'USED')),
    created_at TEXT NOT NULL,
    decided_at TEXT
);

CREATE INDEX IF NOT EXISTS member_requests_household_idx ON member_requests (household_id, created_at);

REVOKE ALL ON checkout_sessions, cart_waiting_items, member_requests FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON checkout_sessions, cart_waiting_items, member_requests TO familycart_shopping;
