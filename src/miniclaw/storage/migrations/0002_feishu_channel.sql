ALTER TABLE processed_events
ADD COLUMN external_user_id TEXT NOT NULL DEFAULT '';

ALTER TABLE processed_events
ADD COLUMN external_conversation_id TEXT NOT NULL DEFAULT '';

ALTER TABLE processed_events
ADD COLUMN chat_type TEXT NOT NULL DEFAULT 'p2p'
CHECK(chat_type IN ('p2p', 'group'));

ALTER TABLE processed_events
ADD COLUMN message_type TEXT NOT NULL DEFAULT 'text'
CHECK(message_type IN ('text', 'unsupported'));

ALTER TABLE processed_events
ADD COLUMN content TEXT NOT NULL DEFAULT '';

ALTER TABLE processed_events
ADD COLUMN reply_to_message_id TEXT NOT NULL DEFAULT '';

ALTER TABLE processed_events
ADD COLUMN status TEXT NOT NULL DEFAULT 'queued'
CHECK(status IN ('queued', 'running', 'completed', 'failed', 'ignored'));

ALTER TABLE processed_events
ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0);

ALTER TABLE processed_events
ADD COLUMN last_error_code TEXT;

ALTER TABLE processed_events
ADD COLUMN updated_at TEXT NOT NULL DEFAULT '';

UPDATE processed_events SET updated_at = received_at WHERE updated_at = '';

ALTER TABLE deliveries RENAME TO deliveries_v1;

CREATE TABLE deliveries (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id),
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    external_conversation_id TEXT NOT NULL,
    reply_to_message_id TEXT NOT NULL DEFAULT '',
    delivery_kind TEXT NOT NULL CHECK(delivery_kind IN (
        'message', 'card', 'approval', 'typing'
    )),
    part_index INTEGER NOT NULL CHECK(part_index >= 0),
    content_hash TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    platform_message_id TEXT,
    status TEXT NOT NULL CHECK(status IN (
        'queued', 'sending', 'retry_wait', 'sent', 'failed', 'unknown', 'superseded'
    )),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
    last_error_code TEXT,
    last_error_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    next_attempt_at TEXT,
    sent_at TEXT,
    UNIQUE(message_id, channel, part_index, delivery_kind),
    UNIQUE(channel, account_id, idempotency_key)
);

INSERT INTO deliveries (
    id,
    message_id,
    channel,
    account_id,
    external_conversation_id,
    reply_to_message_id,
    delivery_kind,
    part_index,
    content_hash,
    idempotency_key,
    platform_message_id,
    status,
    attempts,
    last_error_code,
    created_at,
    updated_at,
    sent_at
)
SELECT
    id,
    message_id,
    channel,
    account_id,
    external_conversation_id,
    '',
    'message',
    part_index,
    content_hash,
    printf('legacy-%016x', id),
    platform_message_id,
    status,
    attempts,
    last_error_code,
    created_at,
    COALESCE(sent_at, created_at),
    sent_at
FROM deliveries_v1;

DROP TABLE deliveries_v1;

CREATE INDEX processed_events_status_idx
ON processed_events(channel, account_id, status, received_at);

CREATE INDEX deliveries_status_idx
ON deliveries(channel, account_id, status, next_attempt_at, id);
