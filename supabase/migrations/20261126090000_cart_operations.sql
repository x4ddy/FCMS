-- Cart changes that can be retried safely, and revisions for items waiting for the next cart.
-- Run as the project administrator, in one transaction, after 20261125150000_checkout_and_roles.sql.
-- Safe to run more than once. The previous application version keeps working after it.

SET LOCAL search_path = familycart_shopping;

-- One row per accepted change, saved in the same transaction as the change. A retry finds its result here.
CREATE TABLE IF NOT EXISTS cart_operations (
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    household_id TEXT NOT NULL REFERENCES households(id),
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, request_id)
);

CREATE INDEX IF NOT EXISTS cart_operations_recent_idx ON cart_operations (user_id, household_id, created_at);
CREATE INDEX IF NOT EXISTS cart_operations_created_idx ON cart_operations (created_at);

-- Which change an update contains, so its author does not count it twice.
ALTER TABLE cart_sync_events ADD COLUMN IF NOT EXISTS request_id TEXT;

-- Advances for waiting items too. "revision" stays what checkout reviews are compared with.
ALTER TABLE checkout_cart_revisions ADD COLUMN IF NOT EXISTS live_revision BIGINT NOT NULL DEFAULT 0;

REVOKE ALL ON cart_operations FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON cart_operations TO familycart_shopping;
