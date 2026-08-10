DROP TABLE feedback;
DROP TABLE eval_runs;
DROP TABLE proposals;

CREATE TABLE feedback (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    message_id INTEGER NOT NULL REFERENCES messages(id),
    rating TEXT NOT NULL CHECK(rating IN ('good', 'bad')),
    redacted_reason TEXT,
    context_hash TEXT NOT NULL CHECK(length(context_hash) = 64),
    status TEXT NOT NULL CHECK(status IN ('open', 'forgotten')),
    created_at TEXT NOT NULL,
    forgotten_at TEXT,
    UNIQUE(message_id, owner_id)
);

CREATE INDEX feedback_owner_status_idx ON feedback(owner_id, status, id);

CREATE TABLE proposals (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    feedback_id INTEGER NOT NULL REFERENCES feedback(id),
    target_type TEXT NOT NULL CHECK(target_type IN ('prompt', 'skill', 'memory')),
    target_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'draft', 'evaluating', 'rejected', 'approved', 'applied', 'rolled_back', 'failed'
    )),
    current_version_id INTEGER REFERENCES proposal_versions(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX proposals_owner_status_idx ON proposals(owner_id, status, id);
CREATE INDEX proposals_feedback_idx ON proposals(feedback_id);

CREATE TABLE proposal_versions (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
    base_hash TEXT NOT NULL CHECK(length(base_hash) = 64),
    candidate_hash TEXT NOT NULL UNIQUE CHECK(length(candidate_hash) = 64),
    manifest_json TEXT NOT NULL DEFAULT '{}',
    candidate_ref TEXT NOT NULL,
    rationale TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(proposal_id, ordinal)
);

CREATE INDEX proposal_versions_proposal_idx ON proposal_versions(proposal_id, ordinal);

CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY,
    proposal_version_id INTEGER NOT NULL REFERENCES proposal_versions(id),
    suite_manifest_hash TEXT NOT NULL CHECK(length(suite_manifest_hash) = 64),
    status TEXT NOT NULL CHECK(status IN ('running', 'passed', 'failed', 'error')),
    receipt_hash TEXT CHECK(length(receipt_hash) = 64),
    total_cases INTEGER NOT NULL DEFAULT 0,
    passed_cases INTEGER NOT NULL DEFAULT 0,
    safety_failures INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX eval_runs_proposal_version_idx ON eval_runs(proposal_version_id, id);

CREATE TABLE eval_case_results (
    id INTEGER PRIMARY KEY,
    eval_run_id INTEGER NOT NULL REFERENCES eval_runs(id),
    case_id TEXT NOT NULL,
    suite_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('passed', 'failed', 'error')),
    latency_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    result_hash TEXT NOT NULL CHECK(length(result_hash) = 64),
    UNIQUE(eval_run_id, case_id)
);

CREATE TABLE active_revision (
    owner_id INTEGER NOT NULL REFERENCES users(id),
    target_type TEXT NOT NULL CHECK(target_type IN ('prompt', 'skill', 'memory')),
    target_name TEXT NOT NULL,
    proposal_version_id INTEGER NOT NULL REFERENCES proposal_versions(id),
    previous_version_id INTEGER REFERENCES proposal_versions(id),
    activated_at TEXT NOT NULL,
    PRIMARY KEY(owner_id, target_type, target_name)
);
