CREATE TABLE IF NOT EXISTS households (
    id TEXT NOT NULL PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS household_members (
    household_id TEXT NOT NULL REFERENCES households(id),
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('primary', 'member')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    PRIMARY KEY (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS carts (
    id TEXT NOT NULL PRIMARY KEY,
    household_id TEXT NOT NULL,
    creator_id TEXT NOT NULL,
    name TEXT NOT NULL,
    adults_only INTEGER NOT NULL CHECK (adults_only IN (0, 1)),
    FOREIGN KEY (household_id, creator_id)
        REFERENCES household_members (household_id, user_id)
);

CREATE INDEX IF NOT EXISTS carts_household_idx ON carts (household_id);

CREATE UNIQUE INDEX IF NOT EXISTS carts_household_id_idx ON carts (household_id, id);

CREATE TABLE IF NOT EXISTS cart_grants (
    household_id TEXT NOT NULL,
    cart_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    PRIMARY KEY (cart_id, member_id),
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id),
    FOREIGN KEY (household_id, member_id) REFERENCES household_members (household_id, user_id)
);

CREATE TABLE IF NOT EXISTS cart_creator_revocations (
    cart_id TEXT NOT NULL PRIMARY KEY REFERENCES carts (id)
);

CREATE TABLE IF NOT EXISTS deleted_carts (
    cart_id TEXT NOT NULL PRIMARY KEY REFERENCES carts (id)
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
    FOREIGN KEY (household_id, cart_id) REFERENCES carts (household_id, id)
);
