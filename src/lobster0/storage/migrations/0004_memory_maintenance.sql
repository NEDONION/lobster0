CREATE TABLE memory_legacy_imports (
    owner_id INTEGER NOT NULL REFERENCES users(id),
    relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    source_message_id INTEGER REFERENCES messages(id),
    unit_ids_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL CHECK(status IN ('imported', 'skipped')),
    rejected_chunks INTEGER NOT NULL DEFAULT 0 CHECK(rejected_chunks >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY(owner_id, relative_path, content_hash)
);

CREATE INDEX memory_legacy_imports_owner_idx
ON memory_legacy_imports(owner_id, relative_path, created_at);
