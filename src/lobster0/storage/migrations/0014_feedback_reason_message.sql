-- Owner 在 /bad <原因> 里说的那句话此前只以脱敏字符串留在 redacted_reason，
-- 没有对应的 messages 行。Memory 的 SourceRef 只接受可核验的真实消息，
-- 于是"改正一条记错的记忆"这类提案永远拿不到合法出处。
--
-- 这一列指向那句话落库后的 user message。可空：/bad 不带原因时没有这条消息，
-- 且此前已有的历史反馈也没有。
ALTER TABLE feedback
ADD COLUMN reason_message_id INTEGER REFERENCES messages(id);
