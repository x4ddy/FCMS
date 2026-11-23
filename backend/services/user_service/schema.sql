CREATE TABLE users (
    user_id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    google_subject TEXT UNIQUE,
    display_name TEXT NOT NULL,
    is_adult INTEGER NOT NULL DEFAULT 0 CHECK (is_adult IN (0, 1)),
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'LOCKED', 'DISABLED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE refresh_sessions (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id),
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    revoked_at TEXT,
    user_agent TEXT,
    ip_address TEXT
);

CREATE TABLE refresh_token_history (
    session_id TEXT NOT NULL REFERENCES refresh_sessions(session_id),
    token_hash TEXT NOT NULL,
    rotated_at TEXT NOT NULL,
    PRIMARY KEY (session_id, token_hash)
);
