CREATE TABLE evolution_approvals (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    proposal_version_id INTEGER NOT NULL REFERENCES proposal_versions(id),
    eval_run_id INTEGER REFERENCES eval_runs(id),
    action TEXT NOT NULL CHECK(action IN ('evolution.apply', 'evolution.rollback')),
    binding_hash TEXT NOT NULL CHECK(length(binding_hash) = 64),
    summary TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'pending', 'approved', 'denied', 'expired', 'consumed'
    )),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT,
    UNIQUE(owner_id, binding_hash),
    CHECK(action <> 'evolution.apply' OR eval_run_id IS NOT NULL)
);

CREATE INDEX evolution_approvals_status_idx
ON evolution_approvals(owner_id, status, expires_at, id);
