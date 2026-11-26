CREATE TABLE IF NOT EXISTS store_inventory (
    store_id TEXT NOT NULL,
    sku TEXT NOT NULL,
    quantity BIGINT NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (store_id, sku)
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

-- One row per accepted cart change, saved with the change itself. A retry finds its result here.
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
