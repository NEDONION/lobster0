CREATE TABLE artifacts (
    artifact_id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    media_type TEXT NOT NULL CHECK(media_type IN (
        'image/png', 'image/jpeg', 'application/pdf', 'application/zip',
        'application/json', 'text/plain', 'text/csv'
    )),
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    source TEXT NOT NULL CHECK(source IN ('browser_screenshot', 'browser_download')),
    relative_path TEXT NOT NULL,
    width INTEGER CHECK(width > 0),
    height INTEGER CHECK(height > 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'deleted')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(owner_id, content_hash)
);

CREATE INDEX artifacts_expiry_idx
ON artifacts(owner_id, status, expires_at, artifact_id);
