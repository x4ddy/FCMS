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
