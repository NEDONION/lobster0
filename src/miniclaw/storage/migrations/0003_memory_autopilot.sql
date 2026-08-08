CREATE TABLE memory_flush_runs (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    first_message_id INTEGER NOT NULL REFERENCES messages(id),
    last_message_id INTEGER NOT NULL REFERENCES messages(id),
    extractor_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL CHECK(length(prompt_hash) = 64),
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'retry', 'projection_pending', 'completed', 'dead_letter'
    )),
    lease_owner TEXT,
    lease_expires_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    next_attempt_at TEXT,
    last_error_code TEXT,
    markdown_committed_at TEXT,
    projection_committed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK(first_message_id <= last_message_id),
    CHECK(
        (status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR
        (status != 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL)
    ),
    UNIQUE(owner_id, first_message_id, last_message_id, extractor_version, prompt_hash)
);

CREATE TABLE memory_buffers (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_id INTEGER NOT NULL UNIQUE REFERENCES turns(id),
    first_message_id INTEGER NOT NULL REFERENCES messages(id),
    last_message_id INTEGER NOT NULL REFERENCES messages(id),
    capture_scope TEXT NOT NULL CHECK(capture_scope IN ('private', 'public')),
    status TEXT NOT NULL CHECK(status IN ('pending', 'assigned', 'flushed')),
    flush_run_id INTEGER REFERENCES memory_flush_runs(id),
    created_at TEXT NOT NULL,
    flushed_at TEXT,
    CHECK(first_message_id <= last_message_id),
    CHECK(
        (status = 'pending' AND flush_run_id IS NULL AND flushed_at IS NULL)
        OR
        (status = 'assigned' AND flush_run_id IS NOT NULL AND flushed_at IS NULL)
        OR
        (status = 'flushed' AND flush_run_id IS NOT NULL AND flushed_at IS NOT NULL)
    )
);

CREATE TABLE memory_candidates (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES memory_flush_runs(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    candidate_hash TEXT NOT NULL CHECK(length(candidate_hash) = 64),
    text TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('private', 'public', 'group')),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('low', 'medium', 'high', 'secret')),
    status TEXT NOT NULL CHECK(status IN (
        'observed', 'validated', 'rejected', 'committed'
    )),
    source_ids_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    rejection_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id, ordinal),
    UNIQUE(run_id, candidate_hash)
);

CREATE TABLE memory_units (
    id TEXT PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    candidate_id INTEGER REFERENCES memory_candidates(id),
    memory_key TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL CHECK(length(text_hash) = 64),
    kind TEXT NOT NULL,
    scope TEXT NOT NULL CHECK(scope IN ('private', 'public', 'group')),
    status TEXT NOT NULL CHECK(status IN (
        'observed', 'short_term', 'review_required', 'active', 'rejected',
        'superseded', 'archived', 'expired'
    )),
    confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
    sensitivity TEXT NOT NULL CHECK(sensitivity IN ('low', 'medium', 'high')),
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    supersedes_unit_id TEXT REFERENCES memory_units(id),
    markdown_hash TEXT,
    search_shadow TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(owner_id, text_hash)
);

CREATE TABLE memory_sources (
    unit_id TEXT NOT NULL REFERENCES memory_units(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    channel TEXT NOT NULL CHECK(channel IN ('cli', 'feishu', 'telegram', 'discord')),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    PRIMARY KEY(unit_id, message_id),
    UNIQUE(unit_id, ordinal)
);

CREATE TABLE memory_conflicts (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    memory_key TEXT NOT NULL,
    active_unit_id TEXT NOT NULL REFERENCES memory_units(id),
    incoming_unit_id TEXT REFERENCES memory_units(id),
    candidate_id INTEGER REFERENCES memory_candidates(id),
    status TEXT NOT NULL CHECK(status IN ('pending', 'resolved', 'dismissed')),
    resolution TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    CHECK(incoming_unit_id IS NOT NULL OR candidate_id IS NOT NULL),
    UNIQUE(owner_id, active_unit_id, incoming_unit_id, candidate_id)
);

CREATE TABLE memory_reviews (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    candidate_id INTEGER REFERENCES memory_candidates(id),
    unit_id TEXT REFERENCES memory_units(id),
    review_type TEXT NOT NULL CHECK(review_type IN (
        'sensitivity', 'conflict', 'behavior', 'correction', 'forget', 'weekly'
    )),
    preview_hash TEXT NOT NULL CHECK(length(preview_hash) = 64),
    requested_transition TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'approved', 'rejected', 'expired', 'consumed'
    )),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(owner_id, preview_hash)
);

CREATE TABLE memory_manifests (
    owner_id INTEGER NOT NULL REFERENCES users(id),
    relative_path TEXT NOT NULL,
    content_hash TEXT NOT NULL CHECK(length(content_hash) = 64),
    last_valid_hash TEXT NOT NULL CHECK(length(last_valid_hash) = 64),
    mtime_ns INTEGER NOT NULL CHECK(mtime_ns >= 0),
    parser_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('current', 'drift', 'error')),
    last_scanned_at TEXT NOT NULL,
    PRIMARY KEY(owner_id, relative_path)
);

CREATE TABLE memory_audit (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL,
    unit_id TEXT REFERENCES memory_units(id),
    run_id INTEGER REFERENCES memory_flush_runs(id),
    review_id INTEGER REFERENCES memory_reviews(id),
    reason_code TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX memory_buffers_pending_idx
ON memory_buffers(owner_id, status, id);

CREATE INDEX memory_flush_runs_claim_idx
ON memory_flush_runs(status, next_attempt_at, lease_expires_at, owner_id, id);

CREATE INDEX memory_candidates_run_idx
ON memory_candidates(run_id, status, ordinal);

CREATE INDEX memory_units_recall_idx
ON memory_units(owner_id, scope, status, valid_until, id);

CREATE INDEX memory_units_key_idx
ON memory_units(owner_id, memory_key, status, id);

CREATE INDEX memory_reviews_pending_idx
ON memory_reviews(owner_id, status, id);

CREATE INDEX memory_audit_owner_idx
ON memory_audit(owner_id, id);

CREATE TRIGGER memory_buffers_terminal_guard
BEFORE UPDATE OF status ON memory_buffers
WHEN OLD.status = 'flushed' AND NEW.status != OLD.status
BEGIN
    SELECT RAISE(ABORT, 'memory buffer terminal state is immutable');
END;

CREATE TRIGGER memory_flush_runs_terminal_guard
BEFORE UPDATE OF status ON memory_flush_runs
WHEN OLD.status IN ('completed', 'dead_letter') AND NEW.status != OLD.status
BEGIN
    SELECT RAISE(ABORT, 'memory run terminal state is immutable');
END;

CREATE TRIGGER memory_units_terminal_guard
BEFORE UPDATE OF status ON memory_units
WHEN OLD.status IN ('rejected', 'superseded', 'archived', 'expired')
    AND NEW.status != OLD.status
BEGIN
    SELECT RAISE(ABORT, 'memory unit terminal state is immutable');
END;

CREATE TRIGGER memory_reviews_terminal_guard
BEFORE UPDATE OF status ON memory_reviews
WHEN OLD.status IN ('rejected', 'expired', 'consumed') AND NEW.status != OLD.status
BEGIN
    SELECT RAISE(ABORT, 'memory review terminal state is immutable');
END;
