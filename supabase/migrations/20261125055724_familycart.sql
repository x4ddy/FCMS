-- One-time baseline for an empty FamilyCart deployment. Run as the project administrator.

-- Each service login receives a password separately after this migration.

CREATE ROLE familycart_identity LOGIN;
CREATE ROLE familycart_shopping LOGIN;

CREATE SCHEMA familycart_identity;
REVOKE ALL ON SCHEMA familycart_identity FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA familycart_identity TO familycart_identity;
ALTER ROLE familycart_identity SET search_path = familycart_identity;
SET LOCAL search_path = familycart_identity;

-- Baseline from backend/services/user_service/postgres_schema.sql

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    google_subject TEXT UNIQUE,
    display_name TEXT NOT NULL,
    is_adult SMALLINT NOT NULL DEFAULT 0 CHECK (is_adult IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'LOCKED', 'DISABLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Migrate databases created by the former phone/OTP authentication flow.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_subject TEXT UNIQUE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS date_of_birth TEXT;

CREATE TABLE IF NOT EXISTS refresh_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT,
    ip_address TEXT
);

CREATE INDEX IF NOT EXISTS refresh_sessions_user_idx
    ON refresh_sessions(user_id, revoked_at);

CREATE TABLE IF NOT EXISTS refresh_token_history (
    session_id TEXT NOT NULL REFERENCES refresh_sessions(session_id),
    token_hash TEXT NOT NULL,
    rotated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, token_hash)
);

REVOKE ALL ON ALL TABLES IN SCHEMA familycart_identity FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA familycart_identity FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA familycart_identity TO familycart_identity;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA familycart_identity TO familycart_identity;

CREATE SCHEMA familycart_shopping;
REVOKE ALL ON SCHEMA familycart_shopping FROM PUBLIC, anon, authenticated, service_role;
GRANT USAGE ON SCHEMA familycart_shopping TO familycart_shopping;
ALTER ROLE familycart_shopping SET search_path = familycart_shopping;
SET LOCAL search_path = familycart_shopping;

-- Baseline from backend/services/familycart_service/postgres_schema.sql

CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS household_members (
    household_id TEXT NOT NULL REFERENCES households(id),
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('primary', 'member')),
    active SMALLINT NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    PRIMARY KEY (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS carts (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    name TEXT NOT NULL,
    adults_only SMALLINT NOT NULL CHECK (adults_only IN (0, 1)),
    UNIQUE (household_id, id),
    FOREIGN KEY (household_id, creator_id)
        REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS household_profiles (
    household_id TEXT PRIMARY KEY REFERENCES households(id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS household_member_profiles (
    household_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    is_adult SMALLINT NOT NULL CHECK (is_adult IN (0, 1)),
    PRIMARY KEY (household_id, user_id),
    FOREIGN KEY (household_id, user_id)
        REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS household_invitations (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    token_hash TEXT NOT NULL UNIQUE,
    invited_by TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    accepted_by TEXT,
    accepted_at TEXT,
    revoked_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (household_id, invited_by)
        REFERENCES household_members (household_id, user_id)
);

CREATE INDEX IF NOT EXISTS household_invitations_household_idx
    ON household_invitations (household_id, expires_at);

CREATE TABLE IF NOT EXISTS household_addresses (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    label TEXT NOT NULL,
    recipient_name TEXT NOT NULL,
    address_line1 TEXT NOT NULL,
    address_line2 TEXT,
    city TEXT NOT NULL,
    postal_code TEXT,
    phone TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (household_id, created_by)
        REFERENCES household_members (household_id, user_id)
);

CREATE INDEX IF NOT EXISTS household_addresses_household_idx
    ON household_addresses (household_id, created_at);

CREATE TABLE IF NOT EXISTS user_addresses (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    label TEXT NOT NULL,
    recipient_name TEXT NOT NULL,
    address_line1 TEXT NOT NULL,
    address_line2 TEXT,
    city TEXT NOT NULL,
    postal_code TEXT,
    phone TEXT,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS user_addresses_user_idx ON user_addresses (user_id, created_at);

CREATE TABLE IF NOT EXISTS personal_carts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS personal_cart_items (
    cart_id TEXT NOT NULL REFERENCES personal_carts(id) ON DELETE CASCADE,
    sku TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (cart_id, sku)
);

CREATE INDEX IF NOT EXISTS carts_household_idx ON carts (household_id);

CREATE TABLE IF NOT EXISTS cart_grants (
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    PRIMARY KEY (cart_id, member_id),
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id),
    FOREIGN KEY (household_id, member_id) REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS cart_creator_revocations (
    cart_id TEXT PRIMARY KEY REFERENCES carts(id)
);

CREATE TABLE IF NOT EXISTS deleted_carts (
    cart_id TEXT PRIMARY KEY REFERENCES carts(id)
);

CREATE TABLE IF NOT EXISTS cart_item_contributions (
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    contributor_id TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    PRIMARY KEY (cart_id, sku, contributor_id),
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id),
    FOREIGN KEY (household_id, contributor_id) REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS cart_sync_events (
    id BIGSERIAL PRIMARY KEY,
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    actor_id TEXT,
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id)
);

CREATE TABLE IF NOT EXISTS shopping_lists (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    archived_at TEXT,
    UNIQUE (household_id, id),
    FOREIGN KEY (household_id, created_by)
        REFERENCES household_members (household_id, user_id)
);

CREATE INDEX IF NOT EXISTS shopping_lists_household_idx
    ON shopping_lists (household_id, created_at);

CREATE TABLE IF NOT EXISTS shopping_list_items (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL,
    household_id TEXT NOT NULL,
    sku TEXT,
    custom_name TEXT,
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999),
    note TEXT,
    added_by TEXT NOT NULL,
    checked SMALLINT NOT NULL DEFAULT 0 CHECK (checked IN (0, 1)),
    checked_by TEXT,
    checked_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (sku IS NOT NULL OR custom_name IS NOT NULL),
    FOREIGN KEY (household_id, list_id) REFERENCES shopping_lists (household_id, id),
    FOREIGN KEY (household_id, added_by) REFERENCES household_members (household_id, user_id)
);

CREATE INDEX IF NOT EXISTS shopping_list_items_list_idx
    ON shopping_list_items (list_id, checked, created_at);

-- Live-update worker: "which lists changed?" and per-household event lookups.
CREATE INDEX IF NOT EXISTS shopping_lists_updated_idx ON shopping_lists (updated_at);
CREATE INDEX IF NOT EXISTS cart_sync_events_household_idx ON cart_sync_events (household_id, id);

-- Baseline from backend/services/familycart_service/checkout_schema.sql

CREATE TABLE IF NOT EXISTS store_inventory (
    store_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (store_id, sku)
);

CREATE TABLE IF NOT EXISTS checkout_cart_revisions (
    kind TEXT NOT NULL CHECK (kind IN ('personal', 'household')),
    cart_id TEXT NOT NULL,
    revision BIGINT NOT NULL,
    PRIMARY KEY (kind, cart_id)
);

CREATE TABLE IF NOT EXISTS checkout_quotes (
    id TEXT PRIMARY KEY,
    purchaser_id TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    snapshot TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    order_number TEXT NOT NULL UNIQUE,
    purchaser_id TEXT NOT NULL,
    household_id TEXT,
    placed_at TEXT NOT NULL,
    currency TEXT NOT NULL CHECK (currency = 'AED'),
    total_fils BIGINT NOT NULL CHECK (total_fils >= 0),
    status TEXT NOT NULL CHECK (status = 'PLACED'),
    mock_payment_outcome TEXT NOT NULL CHECK (mock_payment_outcome = 'approve'),
    quote_id TEXT NOT NULL UNIQUE,
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    UNIQUE (purchaser_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS orders_purchaser_history_idx ON orders (purchaser_id, placed_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS order_delivery_groups (
    order_id TEXT NOT NULL REFERENCES orders(id),
    group_number INTEGER NOT NULL,
    store_id TEXT NOT NULL,
    store_name TEXT NOT NULL,
    address_kind TEXT NOT NULL,
    address_id TEXT NOT NULL,
    address_snapshot TEXT NOT NULL,
    subtotal_fils BIGINT NOT NULL CHECK (subtotal_fils >= 0),
    PRIMARY KEY (order_id, group_number)
);

CREATE TABLE IF NOT EXISTS order_lines (
    order_id TEXT NOT NULL,
    group_number INTEGER NOT NULL,
    line_number INTEGER NOT NULL,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    unit_price_fils BIGINT NOT NULL CHECK (unit_price_fils > 0),
    quantity BIGINT NOT NULL CHECK (quantity > 0),
    contributor_id TEXT,
    contributor_name TEXT,
    PRIMARY KEY (order_id, group_number, line_number),
    FOREIGN KEY (order_id, group_number) REFERENCES order_delivery_groups(order_id, group_number)
);

-- Baseline from backend/services/familycart_service/seed.sql

INSERT INTO households (id)
VALUES ('demo-household')
ON CONFLICT (id) DO NOTHING;

INSERT INTO household_members (household_id, user_id, role)
VALUES
    ('demo-household', 'user-primary', 'primary'),
    ('demo-household', 'user-child', 'member'),
    ('demo-household', 'user-adult', 'member')
ON CONFLICT (household_id, user_id) DO NOTHING;

INSERT INTO household_profiles (household_id, name, created_by, created_at)
VALUES ('demo-household', 'Demo Family', 'user-primary', '2026-01-01T00:00:00+00:00')
ON CONFLICT (household_id) DO NOTHING;

INSERT INTO household_member_profiles (household_id, user_id, display_name, is_adult)
VALUES
    ('demo-household', 'user-primary', 'Primary Customer', 1),
    ('demo-household', 'user-child', 'Demo Child', 0),
    ('demo-household', 'user-adult', 'Demo Adult', 1)
ON CONFLICT (household_id, user_id) DO NOTHING;

INSERT INTO carts (id, household_id, creator_id, name, adults_only)
VALUES
    ('regular-cart', 'demo-household', 'user-primary', 'Regular Cart', 0),
    ('adults-only-cart', 'demo-household', 'user-primary', 'Adults-Only Cart', 1)
ON CONFLICT (id) DO NOTHING;

REVOKE ALL ON ALL TABLES IN SCHEMA familycart_shopping FROM PUBLIC, anon, authenticated, service_role;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA familycart_shopping FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA familycart_shopping TO familycart_shopping;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA familycart_shopping TO familycart_shopping;
