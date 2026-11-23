INSERT INTO households (id)
VALUES ('demo-household')
ON CONFLICT (id) DO NOTHING;

INSERT INTO household_members (household_id, user_id, role)
VALUES
    ('demo-household', 'user-primary', 'primary'),
    ('demo-household', 'user-child', 'member'),
    ('demo-household', 'user-adult', 'member')
ON CONFLICT (household_id, user_id) DO NOTHING;

INSERT INTO household_profiles (household_id, name, created_by, created_at)
VALUES ('demo-household', 'Demo Family', 'user-primary', '2026-01-01T00:00:00+00:00')
ON CONFLICT (household_id) DO NOTHING;

INSERT INTO household_member_profiles (household_id, user_id, display_name, is_adult)
VALUES
    ('demo-household', 'user-primary', 'Primary Customer', 1),
    ('demo-household', 'user-child', 'Demo Child', 0),
    ('demo-household', 'user-adult', 'Demo Adult', 1)
ON CONFLICT (household_id, user_id) DO NOTHING;

INSERT INTO carts (id, household_id, creator_id, name, adults_only)
VALUES
    ('regular-cart', 'demo-household', 'user-primary', 'Regular Cart', 0),
    ('adults-only-cart', 'demo-household', 'user-primary', 'Adults-Only Cart', 1)
ON CONFLICT (id) DO NOTHING;
