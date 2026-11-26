-- Fewer tables: 29 -> 21 in familycart_shopping (32 -> 24 with the identity schema).
-- Run as the project administrator, in one transaction, after 20261126090000_cart_operations.sql.
-- Safe to run more than once.
--
-- Moves into existing tables:
--   household_profiles         -> households (name, admin_id, created_at)
--   household_member_profiles  -> household_members (display_name, is_adult, age_status)
--   deleted_carts              -> carts (deleted, next_cart_id)
--   cart_creator_revocations   -> carts (creator_revoked)
--   checkout_cart_revisions    -> carts (revision, live_revision)
-- Removes, unused by the app: user_addresses, personal_carts, personal_cart_items.
-- Also removes household_members.role: households.admin_id says who the admin is.
--
-- The previous application version does NOT work after this. Update the app before running it.

SET LOCAL search_path = familycart_shopping;

ALTER TABLE households
    ADD COLUMN IF NOT EXISTS name TEXT,
    ADD COLUMN IF NOT EXISTS admin_id TEXT,
    ADD COLUMN IF NOT EXISTS created_at TEXT;

ALTER TABLE household_members
    ADD COLUMN IF NOT EXISTS display_name TEXT,
    ADD COLUMN IF NOT EXISTS is_adult SMALLINT NOT NULL DEFAULT 0 CHECK (is_adult IN (0, 1)),
    ADD COLUMN IF NOT EXISTS age_status TEXT NOT NULL DEFAULT 'APPROVED'
        CHECK (age_status IN ('NONE', 'PENDING', 'APPROVED', 'REJECTED'));

ALTER TABLE carts
    ADD COLUMN IF NOT EXISTS deleted SMALLINT NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    ADD COLUMN IF NOT EXISTS next_cart_id TEXT,
    ADD COLUMN IF NOT EXISTS creator_revoked SMALLINT NOT NULL DEFAULT 0 CHECK (creator_revoked IN (0, 1)),
    ADD COLUMN IF NOT EXISTS revision BIGINT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS live_revision BIGINT NOT NULL DEFAULT 0;

-- Copy everything across. Each step only runs while its old table is still there.
DO $$
BEGIN
    IF to_regclass('household_profiles') IS NOT NULL THEN
        UPDATE households SET name = p.name, admin_id = p.created_by, created_at = p.created_at
        FROM household_profiles AS p WHERE p.household_id = households.id;
    END IF;

    IF to_regclass('household_member_profiles') IS NOT NULL THEN
        UPDATE household_members SET display_name = p.display_name, is_adult = p.is_adult, age_status = p.age_status
        FROM household_member_profiles AS p
        WHERE p.household_id = household_members.household_id AND p.user_id = household_members.user_id;
    END IF;

    IF to_regclass('deleted_carts') IS NOT NULL THEN
        UPDATE carts SET deleted = 1, next_cart_id = d.next_cart_id
        FROM deleted_carts AS d WHERE d.cart_id = carts.id;
    END IF;

    IF to_regclass('cart_creator_revocations') IS NOT NULL THEN
        UPDATE carts SET creator_revoked = 1 WHERE id IN (SELECT cart_id FROM cart_creator_revocations);
    END IF;

    IF to_regclass('checkout_cart_revisions') IS NOT NULL THEN
        UPDATE carts SET revision = r.revision, live_revision = r.live_revision
        FROM checkout_cart_revisions AS r WHERE r.kind = 'household' AND r.cart_id = carts.id;
    END IF;

    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_schema = 'familycart_shopping' AND table_name = 'household_members'
                 AND column_name = 'role') THEN
        UPDATE households SET admin_id = m.user_id
        FROM household_members AS m
        WHERE households.admin_id IS NULL AND m.household_id = households.id AND m.role = 'primary';
    END IF;
END
$$;

-- Anything without the old details still gets usable values.
UPDATE households SET name = 'My Household' WHERE name IS NULL;
UPDATE households SET created_at = '2026-01-01T00:00:00+00:00' WHERE created_at IS NULL;
UPDATE household_members SET display_name = user_id WHERE display_name IS NULL;

-- Stops the migration (and undoes all of it) if a household still has no admin.
ALTER TABLE households
    ALTER COLUMN name SET NOT NULL,
    ALTER COLUMN admin_id SET NOT NULL,
    ALTER COLUMN created_at SET NOT NULL;
ALTER TABLE household_members ALTER COLUMN display_name SET NOT NULL;

DROP TABLE IF EXISTS household_member_profiles;
DROP TABLE IF EXISTS household_profiles;
DROP TABLE IF EXISTS deleted_carts;
DROP TABLE IF EXISTS cart_creator_revocations;
DROP TABLE IF EXISTS checkout_cart_revisions;
DROP TABLE IF EXISTS user_addresses;
DROP TABLE IF EXISTS personal_cart_items;
DROP TABLE IF EXISTS personal_carts;
ALTER TABLE household_members DROP COLUMN IF EXISTS role;
