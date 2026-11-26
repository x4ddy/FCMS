CREATE TABLE IF NOT EXISTS households (
    id TEXT NOT NULL PRIMARY KEY,
    name TEXT NOT NULL,
    -- The admin. Changes when ownership is handed over.
    admin_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS household_members (
    household_id TEXT NOT NULL REFERENCES households(id),
    user_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    display_name TEXT NOT NULL,
    -- 1 once this household's admin approved them as 18 or over.
    is_adult INTEGER NOT NULL DEFAULT 0 CHECK (is_adult IN (0, 1)),
    -- NONE: said under 18. Others: said 18 or over; the admin decides.
    age_status TEXT NOT NULL DEFAULT 'APPROVED' CHECK (age_status IN ('NONE', 'PENDING', 'APPROVED', 'REJECTED')),
    PRIMARY KEY (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS carts (
    id TEXT NOT NULL PRIMARY KEY,
    household_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    name TEXT NOT NULL,
    adults_only INTEGER NOT NULL CHECK (adults_only IN (0, 1)),
    inventory_store_id TEXT,
    -- Deleted, or checked out and replaced by next_cart_id. Kept for old records.
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    next_cart_id TEXT,
    -- The admin took the creator's access away.
    creator_revoked INTEGER NOT NULL DEFAULT 0 CHECK (creator_revoked IN (0, 1)),
    -- What checkout reviews are compared with, and the latest change including waiting items.
    revision BIGINT NOT NULL DEFAULT 0,
    live_revision BIGINT NOT NULL DEFAULT 0,
    UNIQUE (household_id, id),
    FOREIGN KEY (household_id, creator_id)
        REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS household_invitations (
    id TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES households(id),
    token_hash TEXT NOT NULL UNIQUE,
    child_user_id TEXT,
    redemption_key_hash TEXT,
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
    latitude REAL,
    longitude REAL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (household_id, created_by)
        REFERENCES household_members (household_id, user_id)
);

CREATE INDEX IF NOT EXISTS household_addresses_household_idx
    ON household_addresses (household_id, created_at);

CREATE INDEX IF NOT EXISTS carts_household_idx ON carts (household_id);

CREATE TABLE IF NOT EXISTS cart_grants (
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    PRIMARY KEY (cart_id, member_id),
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id),
    FOREIGN KEY (household_id, member_id) REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS cart_item_contributions (
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    contributor_id TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity > 0),
    PRIMARY KEY (cart_id, sku, contributor_id),
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id),
    FOREIGN KEY (household_id, contributor_id) REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS cart_sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    actor_id TEXT,
    -- 'order' when checkout emptied the cart, so members are not told items were removed.
    reason TEXT,
    request_id TEXT,
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
    quantity INTEGER NOT NULL CHECK (typeof(quantity) = 'integer' AND quantity BETWEEN 1 AND 999),
    note TEXT,
    added_by TEXT NOT NULL,
    checked INTEGER NOT NULL DEFAULT 0 CHECK (checked IN (0, 1)),
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

-- Private lists that belong to one account, separate from household lists.
CREATE TABLE IF NOT EXISTS personal_lists (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS personal_lists_user_idx ON personal_lists (user_id, updated_at);

CREATE TABLE IF NOT EXISTS personal_list_items (
    id TEXT PRIMARY KEY,
    list_id TEXT NOT NULL REFERENCES personal_lists(id) ON DELETE CASCADE,
    sku TEXT,
    custom_name TEXT,
    quantity INTEGER NOT NULL CHECK (quantity BETWEEN 1 AND 999),
    note TEXT,
    checked INTEGER NOT NULL DEFAULT 0 CHECK (checked IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (sku IS NOT NULL OR custom_name IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS personal_list_items_list_idx ON personal_list_items (list_id, created_at);
