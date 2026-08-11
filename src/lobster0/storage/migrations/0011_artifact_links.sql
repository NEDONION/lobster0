-- Artifact 与会话的关联。
-- 不把 session_id 加在 artifacts 表上：Artifact 是 content-addressed 且跨会话
-- 去重的，同一份文件在两个会话里出现是同一行记录，塞不下两个归属。
CREATE TABLE artifact_links (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    message_id INTEGER REFERENCES messages(id),
    origin TEXT NOT NULL CHECK(origin IN ('user_upload', 'agent_output')),
    created_at TEXT NOT NULL,
    UNIQUE(artifact_id, session_id, message_id)
);

-- SQLite 的 UNIQUE 里 NULL 互不相等，所以上面的约束管不住 message_id IS NULL
-- 的重复行。尚未落到具体消息的关联用部分唯一索引单独去重。
CREATE UNIQUE INDEX artifact_links_pending_message_idx
ON artifact_links(artifact_id, session_id)
WHERE message_id IS NULL;

CREATE INDEX artifact_links_session_idx
ON artifact_links(owner_id, session_id, id);
