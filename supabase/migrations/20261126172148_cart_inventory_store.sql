ALTER TABLE familycart_shopping.carts
    ADD COLUMN IF NOT EXISTS inventory_store_id TEXT;
