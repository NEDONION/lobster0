"""Desktop 会话查询服务的 Owner 隔离与脱敏响应测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.providers.base import ModelMessage, ToolCall
from lobster0.bridge.conversations import ConversationConsole, ConversationQueryError
from lobster0.paths import build_state_paths
from lobster0.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from lobster0.storage.database import Database


class ConversationConsoleTest(unittest.TestCase):
    """验证 Desktop 只能读取当前 Owner 的有限可见会话数据。"""

    def setUp(self) -> None:
        """创建包含唯一 Owner 与完整 Schema 的临时数据库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(paths)
        self.owner_id = initialized.owner.id
        self.database = Database(paths.database)
        self.sessions = SessionRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.console = ConversationConsole(self.database)

    def test_list_sessions_is_newest_first_and_contains_only_safe_summary(self) -> None:
        """最近任务应提供可读标题和终态，但不泄露 Turn 内部字段。"""
        old = self.sessions.get_or_create_cli(self.owner_id, "task-old")
        old_turn = self.turns.create_with_user_message(old.id, "old", "model", "旧任务")
        self.turns.mark_running(old_turn.id)
        self.turns.complete_with_assistant_message(
            old_turn.id,
            old.id,
            "旧结果",
            input_tokens=1,
            output_tokens=1,
            provider_request_id="secret-provider-id",
            iterations=1,
            finish_reason="stop",
            runtime_snapshot={"secret": "must-not-leak"},
        )
        new = self.sessions.get_or_create_cli(self.owner_id, "task-new")
        self.turns.create_with_user_message(new.id, "new", "model", "生成本周项目简报")

        result = self.console.list_sessions(self.owner_id, limit=20)

        self.assertEqual(
            [item["session_key"] for item in result["sessions"]],
            ["task-new", "task-old"],
        )
        self.assertEqual(result["sessions"][0]["title"], "生成本周项目简报")
        self.assertEqual(result["sessions"][0]["status"], "queued")
        self.assertNotIn("secret-provider-id", repr(result))
        self.assertNotIn("must-not-leak", repr(result))

    def test_history_can_open_an_automation_session(self) -> None:
        """定时任务的会话在 automation 渠道，此前桌面端根本打不开。

        它恰恰是最需要回放的那类——没有实时事件流，不看历史就无从判断
        Agent 做了什么。
        """
        session = self.sessions.get_or_create(
            self.owner_id, "automation", "local", "task:1:run:1"
        )
        self.turns.create_with_user_message(session.id, "event-1", "model", "汇总")

        history = self.console.history(
            self.owner_id, session_key="task:1:run:1", limit=50
        )

        self.assertEqual(history["session_key"], "task:1:run:1")

    def test_history_still_refuses_an_im_channel_session(self) -> None:
        """放开 automation 不等于放开飞书/Discord 的会话。"""
        session = self.sessions.get_or_create(
            self.owner_id, "feishu", "default", "oc_secret"
        )
        self.turns.create_with_user_message(session.id, "event-1", "model", "私聊内容")

        with self.assertRaises(ConversationQueryError) as raised:
            self.console.history(self.owner_id, session_key="oc_secret", limit=50)

        self.assertEqual(raised.exception.code, "session_not_found")

    def test_history_replays_tool_calls_not_just_the_answer(self) -> None:
        """历史必须能还原执行过程，否则用户无从判断 Agent 到底做了什么。

        此前只下发 user/assistant，工具调用被整个滤掉——重新打开任何一个会话
        都只剩问答两行，定时任务尤其致命：它没有实时事件流可看。
        """
        session = self.sessions.get_or_create_cli(self.owner_id, "task-1")
        turn = self.turns.create_with_user_message(
            session.id, "event-1", "model", "汇总昨天的项目"
        )
        self.turns.mark_running(turn.id)
        self.turns.append_intermediate_messages(
            turn.id,
            session.id,
            (
                ModelMessage(
                    role="assistant",
                    content="先查一下认证状态。",
                    tool_calls=(ToolCall("call-1", "run_command", {"args": ["gh"]}),),
                ),
                ModelMessage(
                    role="tool",
                    content='{"data":{"exit_code":1}}',
                    tool_call_id="call-1",
                ),
            ),
        )

        history = self.console.history(self.owner_id, session_key="task-1", limit=100)

        roles = [message["role"] for message in history["messages"]]
        self.assertIn("tool", roles)
        tool_message = next(m for m in history["messages"] if m["role"] == "tool")
        self.assertEqual(tool_message["tool_name"], "run_command")

    def test_history_carries_the_attachments_of_a_user_message(self) -> None:
        """附件早就写进了 metadata_json，却从没下发——渲染层因此无法在气泡里
        显示"这条消息带了什么"，上传的图片在桌面版完全看不见。

        只下发可安全展示的摘要，**不含图片字节**：历史可能有几十条消息，每条
        都塞 data URI 会让一次 session.load 变成几十兆。缩略图按需走
        artifacts.preview。
        """
        session = self.sessions.get_or_create_cli(self.owner_id, "task-1")
        self.turns.create_with_user_message(
            session.id,
            "event-1",
            "model",
            "这张图里写了什么",
            attachments=(
                {
                    "artifact_id": "art_" + "a" * 64,
                    "filename": "screenshot.png",
                    "media_type": "image/png",
                    "byte_size": 2048,
                },
            ),
        )

        history = self.console.history(self.owner_id, session_key="task-1", limit=100)

        user_message = next(m for m in history["messages"] if m["role"] == "user")
        self.assertEqual(
            user_message["attachments"],
            [
                {
                    "artifact_id": "art_" + "a" * 64,
                    "filename": "screenshot.png",
                    "media_type": "image/png",
                    "size_bytes": 2048,
                }
            ],
        )

    def test_history_omits_the_attachment_field_when_there_are_none(self) -> None:
        """没有附件的消息不出现该字段：旧客户端不该因为多出一个空数组而变化。"""
        session = self.sessions.get_or_create_cli(self.owner_id, "task-1")
        self.turns.create_with_user_message(session.id, "event-1", "model", "你好")

        history = self.console.history(self.owner_id, session_key="task-1", limit=100)

        user_message = next(m for m in history["messages"] if m["role"] == "user")
        self.assertNotIn("attachments", user_message)

    def test_history_keeps_an_assistant_turn_that_only_called_tools(self) -> None:
        """只调工具、没写正文的那一轮此前被 content 判空丢掉。

        它恰恰是"Agent 做了什么"的关键一环。
        """
        session = self.sessions.get_or_create_cli(self.owner_id, "task-1")
        turn = self.turns.create_with_user_message(
            session.id, "event-1", "model", "汇总"
        )
        self.turns.mark_running(turn.id)
        self.turns.append_intermediate_messages(
            turn.id,
            session.id,
            (
                ModelMessage(
                    role="assistant",
                    content="",
                    tool_calls=(ToolCall("call-1", "http_get", {"url": "https://x/"}),),
                ),
            ),
        )

        history = self.console.history(self.owner_id, session_key="task-1", limit=100)

        assistant = [m for m in history["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]["tool_calls"], ["http_get"])

    def test_history_is_owner_scoped_and_exposes_stable_interruption(self) -> None:
        """历史只返回可见消息和稳定错误码，其他 Owner 按不存在处理。"""
        session = self.sessions.get_or_create_cli(self.owner_id, "task-1")
        turn = self.turns.create_with_user_message(session.id, "event-1", "model", "整理报告")
        self.turns.mark_running(turn.id)
        self.turns.interrupt_stale()

        history = self.console.history(
            self.owner_id,
            session_key="task-1",
            limit=100,
        )

        self.assertEqual(history["session_key"], "task-1")
        self.assertEqual(
            history["turns"],
            [{"turn_id": turn.id, "status": "failed", "error_code": "runtime_interrupted"}],
        )
        self.assertEqual(
            history["messages"],
            [{"role": "user", "content": "整理报告", "turn_id": turn.id}],
        )
        self.assertNotIn("error_message", repr(history))

        with self.database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO users (display_name, created_at) VALUES (?, ?)",
                ("other", datetime.now(UTC).isoformat()),
            )
            other_owner_id = int(cursor.lastrowid)
        with self.assertRaises(ConversationQueryError) as captured:
            self.console.history(other_owner_id, session_key="task-1", limit=100)
        self.assertEqual(captured.exception.code, "session_not_found")


if __name__ == "__main__":
    unittest.main()
