CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE channel_identities (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_user_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(channel, account_id, external_user_id)
);

CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    title TEXT,
    status TEXT NOT NULL CHECK(status IN ('active', 'archived')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(channel, account_id, external_conversation_id)
);

CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    parent_turn_id INTEGER REFERENCES turns(id),
    inbound_event_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'running', 'waiting_approval', 'completed', 'failed', 'cancelled'
    )),
    model TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    runtime_snapshot_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT,
    error_message TEXT,
    UNIQUE(session_id, inbound_event_id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    turn_id INTEGER REFERENCES turns(id),
    role TEXT NOT NULL CHECK(role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    provider_message_id TEXT,
    tool_call_id TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE tool_runs (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    arguments_json TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    policy_action TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'waiting_approval', 'running', 'succeeded', 'failed', 'denied', 'interrupted'
    )),
    result_preview TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(turn_id, tool_call_id)
);

CREATE TABLE processed_events (
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    external_message_id TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    received_at TEXT NOT NULL,
    PRIMARY KEY(channel, account_id, event_id),
    UNIQUE(channel, account_id, external_message_id)
);

CREATE TABLE deliveries (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    part_index INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    platform_message_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('queued', 'sending', 'sent', 'failed', 'unknown')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(message_id, channel, part_index)
);

CREATE TABLE approvals (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    turn_id INTEGER NOT NULL REFERENCES turns(id),
    tool_run_id INTEGER NOT NULL UNIQUE REFERENCES tool_runs(id),
    tool_name TEXT NOT NULL,
    arguments_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'denied', 'expired', 'consumed')),
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE policy_rules (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    tool_name TEXT NOT NULL,
    rule_json TEXT NOT NULL,
    source_approval_id INTEGER REFERENCES approvals(id),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL UNIQUE REFERENCES messages(id),
    rating TEXT NOT NULL CHECK(rating IN ('good', 'bad')),
    reason TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE proposals (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL CHECK(target_type IN ('prompt', 'skill')),
    target_name TEXT NOT NULL,
    base_version TEXT NOT NULL,
    candidate_version TEXT NOT NULL,
    diff_text TEXT NOT NULL,
    rationale TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'evaluating', 'failed', 'passed', 'approved', 'applied', 'rejected', 'rolled_back'
    )),
    created_at TEXT NOT NULL,
    applied_at TEXT
);

CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER REFERENCES proposals(id),
    suite_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'passed', 'failed', 'error')),
    total_cases INTEGER NOT NULL,
    passed_cases INTEGER NOT NULL,
    safety_failures INTEGER NOT NULL,
    duration_ms INTEGER,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    event_type TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id),
    session_id INTEGER REFERENCES sessions(id),
    turn_id INTEGER REFERENCES turns(id),
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX messages_session_time_idx ON messages(session_id, created_at);
CREATE INDEX turns_session_time_idx ON turns(session_id, id);
CREATE INDEX tool_runs_turn_idx ON tool_runs(turn_id);
CREATE INDEX approvals_status_expiry_idx ON approvals(status, expires_at);
CREATE INDEX deliveries_status_idx ON deliveries(status, id);
CREATE INDEX policy_rules_active_idx ON policy_rules(user_id, tool_name, enabled);
CREATE INDEX audit_events_turn_idx ON audit_events(turn_id, id);
