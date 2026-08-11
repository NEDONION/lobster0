ALTER TABLE processed_events
ADD COLUMN replied_to_message_id TEXT NOT NULL DEFAULT '';
