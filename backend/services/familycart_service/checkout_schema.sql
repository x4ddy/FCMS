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
