-- Age approval, order notices and private lists.
-- Run as the project administrator, in one transaction, after the FamilyCart baseline.
-- Safe to run more than once. The previous application version keeps working after it.

SET LOCAL search_path = familycart_shopping;

-- NONE: said under 18. Others: said 18 or over; the primary decides.
-- People who were members before age approval existed keep the access they had.
ALTER TABLE household_member_profiles ADD COLUMN IF NOT EXISTS age_status TEXT NOT NULL DEFAULT 'APPROVED'
    CHECK (age_status IN ('NONE', 'PENDING', 'APPROVED', 'REJECTED'));

-- 'order' when checkout emptied the cart, so members are not told items were removed.
ALTER TABLE cart_sync_events ADD COLUMN IF NOT EXISTS reason TEXT;

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
    checked SMALLINT NOT NULL DEFAULT 0 CHECK (checked IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (sku IS NOT NULL OR custom_name IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS personal_list_items_list_idx ON personal_list_items (list_id, created_at);

REVOKE ALL ON personal_lists, personal_list_items FROM PUBLIC, anon, authenticated, service_role;
GRANT SELECT, INSERT, UPDATE, DELETE ON personal_lists, personal_list_items TO familycart_shopping;
