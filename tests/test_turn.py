"""TurnService 对 Context、Runner 和 SQLite 的编排测试。"""

import asyncio
import tempfile
import unittest
from pathlib import Path

from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.providers.base import ModelResponse, ProviderAuthenticationError
from miniclaw.storage.conversations import (
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from miniclaw.storage.database import Database
from tests.fakes.fake_provider import FakeProvider


def final_response(content: str = "world") -> ModelResponse:
    """创建 Turn 成功路径使用的固定最终响应。"""
    return ModelResponse(
        content=content,
        tool_calls=(),
        reasoning_content="internal",
        finish_reason="stop",
        input_tokens=9,
        output_tokens=3,
        provider_request_id="req_turn",
    )


class TurnServiceTest(unittest.IsolatedAsyncioTestCase):
    """验证一次用户输入最终形成可回放的终态 Turn。"""

    def setUp(self) -> None:
        """创建完整状态、Repository 和 ContextBuilder。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.owner = initialized.owner
        self.database = Database(self.paths.database)
        self.sessions = SessionRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.context = ContextBuilder(self.paths)

    def service(self, provider: FakeProvider) -> TurnService:
        """用真实 Repository/Context/Runner 和指定模型 Fake 构造服务。"""
        return TurnService(
            model="deepseek-v4-pro",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=self.context,
            runner=AgentRunner(provider),
        )

    async def test_success_persists_user_assistant_usage_and_completed_turn(self) -> None:
        """成功 Turn 应保存两条消息、Token、最终文本和 completed 状态。"""
        provider = FakeProvider((final_response(),))

        result = await self.service(provider).handle(self.owner.id, "hello", "default")

        saved = self.turns.get(result.turn_id)
        history = self.messages.list_recent(result.session_id)
        self.assertEqual(result.content, "world")
        self.assertEqual(saved.status, "completed")
        self.assertEqual((saved.input_tokens, saved.output_tokens), (9, 3))
        self.assertEqual(
            [(message.role, message.content) for message in history],
            [("user", "hello"), ("assistant", "world")],
        )
        self.assertEqual(provider.requests[0].messages[-1].content, "hello")

    async def test_second_turn_receives_previous_history_in_chronological_order(self) -> None:
        """复用同一 CLI Session 时，新请求应包含上一轮和当前输入。"""
        provider = FakeProvider((final_response("first answer"), final_response("second answer")))
        service = self.service(provider)

        await service.handle(self.owner.id, "first question", "default")
        await service.handle(self.owner.id, "second question", "default")

        messages = provider.requests[1].messages
        self.assertEqual(
            [(message.role, message.content) for message in messages[-3:]],
            [
                ("user", "first question"),
                ("assistant", "first answer"),
                ("user", "second question"),
            ],
        )

    async def test_provider_failure_marks_turn_failed_with_stable_code(self) -> None:
        """认证失败应原样抛给 CLI，同时数据库保存安全错误分类。"""
        provider = FakeProvider((ProviderAuthenticationError("authentication failed"),))

        with self.assertRaises(ProviderAuthenticationError):
            await self.service(provider).handle(self.owner.id, "hello", "default")

        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        saved = self.turns.list_recent(session.id, limit=1)[0]
        self.assertEqual(saved.status, "failed")
        self.assertEqual(saved.error_code, "provider_authentication")
        self.assertEqual(saved.error_message, "authentication failed")

    async def test_cancellation_marks_turn_cancelled_and_propagates(self) -> None:
        """取消必须持久化 cancelled，并继续抛出以便 CLI 返回 130。"""
        provider = FakeProvider((asyncio.CancelledError(),))

        with self.assertRaises(asyncio.CancelledError):
            await self.service(provider).handle(self.owner.id, "hello", "default")

        session = self.sessions.get_or_create_cli(self.owner.id, "default")
        saved = self.turns.list_recent(session.id, limit=1)[0]
        self.assertEqual(saved.status, "cancelled")


if __name__ == "__main__":
    unittest.main()
