-- 放宽 artifacts.source 的 CHECK 约束，接纳用户上传的附件。
-- SQLite 不能就地修改 CHECK，只能重建表并搬运数据；除 source 的取值集合外，
-- 列定义、约束与索引与 0006 完全一致。
CREATE TABLE artifacts_new (
    artifact_id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    media_type TEXT NOT NULL CHECK(media_type IN (
        'image/png', 'image/jpeg', 'application/pdf', 'application/zip',
        'application/json', 'text/plain', 'text/csv'
    )),
    byte_size INTEGER NOT NULL CHECK(byte_size >= 0),
    source TEXT NOT NULL CHECK(source IN (
        'browser_screenshot', 'browser_download', 'user_upload'
    )),
    relative_path TEXT NOT NULL,
    width INTEGER CHECK(width > 0),
    height INTEGER CHECK(height > 0),
    status TEXT NOT NULL CHECK(status IN ('active', 'deleted')),
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    deleted_at TEXT,
    UNIQUE(owner_id, content_hash)
);

INSERT INTO artifacts_new (
    artifact_id, owner_id, content_hash, media_type, byte_size, source,
    relative_path, width, height, status, created_at, expires_at, deleted_at
)
SELECT
    artifact_id, owner_id, content_hash, media_type, byte_size, source,
    relative_path, width, height, status, created_at, expires_at, deleted_at
FROM artifacts;

DROP TABLE artifacts;

ALTER TABLE artifacts_new RENAME TO artifacts;

CREATE INDEX artifacts_expiry_idx
ON artifacts(owner_id, status, expires_at, artifact_id);
