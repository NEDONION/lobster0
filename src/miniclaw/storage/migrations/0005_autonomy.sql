CREATE TABLE scheduled_tasks (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    schedule_kind TEXT NOT NULL
        CHECK(schedule_kind IN ('once', 'interval', 'cron', 'heartbeat')),
    schedule_expression TEXT NOT NULL,
    timezone TEXT NOT NULL,
    prompt TEXT NOT NULL,
    skill_names_json TEXT NOT NULL DEFAULT '[]',
    delivery_json TEXT NOT NULL,
    policy_profile TEXT NOT NULL,
    budget_json TEXT NOT NULL,
    system_key TEXT,
    status TEXT NOT NULL
        CHECK(status IN ('active', 'paused', 'completed', 'cancelled')),
    next_run_at TEXT,
    last_run_at TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX scheduled_tasks_due_idx
ON scheduled_tasks(status, next_run_at, id);

CREATE UNIQUE INDEX scheduled_tasks_system_key_idx
ON scheduled_tasks(owner_id, system_key)
WHERE system_key IS NOT NULL;

CREATE TABLE task_runs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL REFERENCES scheduled_tasks(id),
    session_id INTEGER REFERENCES sessions(id),
    turn_id INTEGER REFERENCES turns(id),
    approval_id INTEGER REFERENCES approvals(id),
    scheduled_for TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'claimed', 'running', 'waiting_approval',
        'succeeded', 'failed', 'cancelled', 'timed_out', 'interrupted'
    )),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK(attempt >= 0),
    worker_id TEXT,
    lease_expires_at TEXT,
    claimed_at TEXT,
    started_at TEXT,
    completed_at TEXT,
    result_preview TEXT,
    response_json TEXT,
    error_code TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX task_runs_state_idx
ON task_runs(status, lease_expires_at, id);

CREATE TABLE automation_control (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    halted INTEGER NOT NULL CHECK(halted IN (0, 1)),
    reason TEXT,
    revision INTEGER NOT NULL CHECK(revision > 0),
    scheduler_heartbeat_at TEXT,
    updated_at TEXT NOT NULL
);

INSERT INTO automation_control (
    singleton, halted, reason, revision, scheduler_heartbeat_at, updated_at
) VALUES (1, 0, NULL, 1, NULL, CURRENT_TIMESTAMP);

CREATE TABLE checkpoints (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    turn_id INTEGER REFERENCES turns(id),
    task_run_id INTEGER REFERENCES task_runs(id),
    tool_run_id INTEGER REFERENCES tool_runs(id),
    manifest_json TEXT NOT NULL,
    manifest_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('captured', 'restored', 'expired')),
    total_bytes INTEGER NOT NULL CHECK(total_bytes >= 0),
    created_at TEXT NOT NULL,
    restored_at TEXT
);

CREATE INDEX checkpoints_created_idx
ON checkpoints(owner_id, created_at DESC, id DESC);

CREATE TABLE execution_plans (
    tool_run_id INTEGER PRIMARY KEY REFERENCES tool_runs(id),
    schema_version INTEGER NOT NULL CHECK(schema_version > 0),
    plan_json TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    backend TEXT NOT NULL CHECK(backend IN ('host', 'docker', 'seatbelt')),
    receipt_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

ALTER TABLE approvals ADD COLUMN execution_plan_hash TEXT;

ALTER TABLE deliveries ADD COLUMN task_run_id INTEGER REFERENCES task_runs(id);

CREATE UNIQUE INDEX deliveries_task_run_part_idx
ON deliveries(task_run_id, channel, part_index, delivery_kind)
WHERE task_run_id IS NOT NULL;
