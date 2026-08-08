"""会话压缩的连续 Turn、持久恢复与 TurnService 接入测试。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.agent.compaction import ContextCompactor
from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.bootstrap import initialize_state
from miniclaw.config import WorkspaceConfig
from miniclaw.paths import build_state_paths
from miniclaw.providers.base import ModelMessage, ModelRequest, ModelResponse, ProviderServerError
from miniclaw.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from tests.fakes.fake_provider import FakeProvider


def response(content: str, request_id: str = "req") -> ModelResponse:
    """创建 compaction 与最终回答共用的无 Tool 模型响应。"""
    return ModelResponse(
        content=content,
        tool_calls=(),
        reasoning_content=None,
        finish_reason="stop",
        input_tokens=10,
        output_tokens=3,
        provider_request_id=request_id,
    )


class ContextCompactorTest(unittest.IsolatedAsyncioTestCase):
    """验证压缩不删除原文、保留尾部并可由新 Repository 恢复。"""

    def setUp(self) -> None:
        """创建真实 SQLite Owner、Session 与 Conversation Repository。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        owner = initialize_state(self.paths).owner
        self.owner_id = owner.id
        self.database = Database(self.paths.database)
        self.sessions = SessionRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.session = self.sessions.get_or_create_cli(owner.id, "compaction")

    def complete_turn(self, index: int, *, size: int = 20) -> int:
        """写入一个包含 User/Assistant 的完整 Turn 并返回 Turn ID。"""
        turn = self.turns.create_with_user_message(
            self.session.id,
            f"event-{index}",
            "test-model",
            f"question-{index}-" + "q" * size,
        )
        self.turns.mark_running(turn.id)
        self.turns.complete_with_assistant_message(
            turn.id,
            self.session.id,
            f"answer-{index}-" + "a" * size,
            input_tokens=1,
            output_tokens=1,
            provider_request_id=f"req-{index}",
            iterations=1,
            finish_reason="stop",
        )
        return turn.id

    async def test_compaction_persists_summary_keeps_two_turns_and_all_raw_messages(self) -> None:
        """首次压缩覆盖前三 Turn，Context 留摘要和后两 Turn，原始十条消息不变。"""
        for index in range(1, 6):
            self.complete_turn(index)
        provider = FakeProvider((response("- Goal: finish Phase 3", "compact-1"),))
        compactor = ContextCompactor(
            self.messages,
            provider,
            model="test-model",
            context_budget_tokens=1_000,
        )

        result = await compactor.compact(self.session.id)

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.first_message_id, 1)
        self.assertEqual(result.last_message_id, 6)
        self.assertEqual(result.model, "test-model")
        self.assertEqual(len(result.content_hash), 64)
        with self.database.connect_read_only() as connection:
            raw_count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE session_id = ? AND role != 'system'",
                (self.session.id,),
            ).fetchone()[0]
        self.assertEqual(raw_count, 10)

        restored = MessageRepository(self.database)
        latest = restored.latest_compaction(self.session.id)
        context = restored.list_context(self.session.id)
        self.assertEqual(latest, result)
        self.assertEqual(
            [(message.role, message.content[:8]) for message in context],
            [
                ("system", "- Goal: "),
                ("user", "question"),
                ("assistant", "answer-4"),
                ("user", "question"),
                ("assistant", "answer-5"),
            ],
        )

    async def test_waiting_approval_breaks_the_compactable_prefix(self) -> None:
        """旧 waiting approval 不能被跨过；只允许压缩它之前的连续 Turn。"""
        self.complete_turn(1)
        waiting = self.turns.create_with_user_message(
            self.session.id,
            "event-waiting",
            "test-model",
            "must stay for approval",
        )
        self.turns.mark_running(waiting.id)
        self.turns.wait_for_approval(
            waiting.id,
            self.session.id,
            99,
            input_tokens=1,
            output_tokens=1,
            provider_request_id="wait",
            iterations=1,
        )
        for index in range(3, 6):
            self.complete_turn(index)
        compactor = ContextCompactor(
            self.messages,
            FakeProvider((response("summary before approval"),)),
            model="test-model",
            context_budget_tokens=1_000,
        )

        result = await compactor.compact(self.session.id)

        assert result is not None
        self.assertEqual((result.first_message_id, result.last_message_id), (1, 2))
        context = self.messages.list_context(self.session.id)
        self.assertTrue(any(message.turn_id == waiting.id for message in context))

    async def test_provider_failure_writes_no_summary_and_keeps_history(self) -> None:
        """摘要 Provider 失败必须安全退化，不能写空 summary 或删原消息。"""
        for index in range(1, 4):
            self.complete_turn(index)
        compactor = ContextCompactor(
            self.messages,
            FakeProvider((ProviderServerError("temporary failure"),)),
            model="test-model",
            context_budget_tokens=1_000,
        )

        result = await compactor.compact(self.session.id)

        self.assertIsNone(result)
        self.assertIsNone(self.messages.latest_compaction(self.session.id))
        self.assertEqual(len(self.messages.list_recent(self.session.id, limit=20)), 6)

    async def test_second_compaction_includes_previous_summary_and_advances_coverage(self) -> None:
        """后续压缩必须继承前摘要，继续覆盖新变旧的 Turn，而不是丢掉第一次结果。"""
        for index in range(1, 6):
            self.complete_turn(index)
        provider = FakeProvider(
            (
                response("first compacted summary", "compact-1"),
                response("second compacted summary", "compact-2"),
            )
        )
        compactor = ContextCompactor(
            self.messages,
            provider,
            model="test-model",
            context_budget_tokens=1_000,
        )
        first = await compactor.compact(self.session.id)
        self.complete_turn(6)
        self.complete_turn(7)

        second = await compactor.compact(self.session.id)

        assert first is not None and second is not None
        self.assertEqual(second.first_message_id, first.first_message_id)
        self.assertGreater(second.last_message_id, first.last_message_id)
        self.assertIn("first compacted summary", provider.requests[1].messages[1].content)
        self.assertEqual(self.messages.latest_compaction(self.session.id), second)

    def test_threshold_uses_eighty_percent_of_configured_budget(self) -> None:
        """本地估算低于 80% 不触发，达到阈值才允许额外摘要调用。"""
        compactor = ContextCompactor(
            self.messages,
            FakeProvider(()),
            model="test-model",
            context_budget_tokens=100,
        )
        short = ModelRequest(
            model="test-model",
            messages=(ModelMessage(role="user", content="x" * 100),),
        )
        long = ModelRequest(
            model="test-model",
            messages=(ModelMessage(role="user", content="x" * 320),),
        )

        self.assertFalse(compactor.should_compact(short))
        self.assertTrue(compactor.should_compact(long))

    async def test_turn_service_compacts_before_final_request_and_saves_snapshot(self) -> None:
        """真实 TurnService 超阈值时先摘要，再把 summary 和 hash 交给最终 Agent 请求。"""
        for index in range(1, 4):
            self.complete_turn(index, size=300)
        provider = FakeProvider(
            (
                response("- Goal: continue compacted session", "req-compact"),
                response("final answer", "req-final"),
            )
        )
        compactor = ContextCompactor(
            self.messages,
            provider,
            model="test-model",
            context_budget_tokens=400,
        )
        service = TurnService(
            model="test-model",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=ContextBuilder(self.paths, context_budget_tokens=400),
            runner=AgentRunner(provider),
            compactor=compactor,
            state_home=self.paths.home,
            workspace=WorkspaceConfig(path=self.paths.workspace),
        )

        result = await service.handle(self.owner_id, "current question", "compaction")

        self.assertEqual(result.content, "final answer")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(provider.requests[0].tools, ())
        self.assertTrue(
            any(
                message.role == "system" and "continue compacted" in message.content
                for message in provider.requests[1].messages
            )
        )
        saved = self.turns.get(result.turn_id)
        compaction = saved.runtime_snapshot["compaction"]
        assert isinstance(compaction, dict)
        self.assertEqual(compaction["model"], "test-model")


if __name__ == "__main__":
    unittest.main()
