INSERT INTO households (id)
VALUES ('demo-household')
ON CONFLICT (id) DO NOTHING;

INSERT INTO household_members (household_id, user_id, role)
VALUES
    ('demo-household', 'user-primary', 'primary'),
    ('demo-household', 'user-child', 'member'),
    ('demo-household', 'user-adult', 'member')
ON CONFLICT (household_id, user_id) DO NOTHING;

INSERT INTO carts (id, household_id, creator_id, name, adults_only)
VALUES
    ('regular-cart', 'demo-household', 'user-primary', 'Regular Cart', 0),
    ('adults-only-cart', 'demo-household', 'user-primary', 'Adults-Only Cart', 1)
ON CONFLICT (id) DO NOTHING;
