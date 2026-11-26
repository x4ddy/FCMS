ALTER TABLE familycart_shopping.household_invitations
    ADD COLUMN IF NOT EXISTS child_user_id TEXT,
    ADD COLUMN IF NOT EXISTS redemption_key_hash TEXT;
