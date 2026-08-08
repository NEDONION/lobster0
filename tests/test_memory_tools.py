"""Memory Tool 经 Registry、Policy 与 Approval 执行的闭环测试。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.store import MemoryStore
from miniclaw.paths import build_state_paths
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository, ToolRunRepository
from miniclaw.tools.base import ToolContext, ToolValidationError
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.memory import ProposeMemoryTool, ReadMemoryTool
from miniclaw.tools.registry import ToolRegistry


class MemoryToolTest(unittest.IsolatedAsyncioTestCase):
    """验证只读自动放行、写入参数绑定审批和敏感内容前置拒绝。"""

    def setUp(self) -> None:
        """创建带真实 Owner、Session、Turn 和固定日期 Store 的状态。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(owner.id, "memory-tools")
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "memory-event",
            "test-model",
            "remember this",
        )
        TurnRepository(self.database).mark_running(turn.id)
        self.context = ToolContext(
            user_id=owner.id,
            session_id=session.id,
            turn_id=turn.id,
            state_home=self.paths.home,
            workspace=self.paths.workspace,
            read_only_roots=(),
        )
        self.store = MemoryStore(self.paths, today=lambda: date(2026, 8, 8))
        self.approvals = ApprovalRepository(self.database)

    def executor(self) -> ToolExecutor:
        """构造同时注册两个 Memory Tool 的真实安全执行入口。"""
        return ToolExecutor(
            ToolRegistry((ReadMemoryTool(self.store), ProposeMemoryTool(self.store))),
            PolicyEngine(),
            ToolRunRepository(self.database),
            approvals=self.approvals,
        )

    async def test_read_memory_is_low_risk_and_returns_bounded_scope(self) -> None:
        """read_memory 应自动执行并持久化 succeeded ToolRun。"""
        self.paths.memory_file.write_text("- prefers concise answers\n", encoding="utf-8")

        outcome = await self.executor().execute(
            self.context,
            ToolCall("read_1", "read_memory", {"scope": "long_term"}),
        )

        payload = json.loads(outcome.model_text)
        self.assertTrue(payload["ok"])
        self.assertIn("prefers concise answers", payload["data"]["content"])
        with self.database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT policy_action, status FROM tool_runs WHERE tool_call_id = 'read_1'"
            ).fetchone()
        self.assertEqual(tuple(row), ("allow", "succeeded"))

    async def test_propose_memory_writes_only_after_bound_approval(self) -> None:
        """候选事实未批准不能落盘，批准消费后只执行原始绑定参数。"""
        executor = self.executor()
        call = ToolCall(
            "remember_1",
            "propose_memory",
            {"content": "I prefer Python 3.12", "source": "explicit user request"},
        )

        waiting = await executor.execute(self.context, call)

        daily = self.paths.memory_dir / "2026-08-08.md"
        self.assertIsNotNone(waiting.approval_id)
        self.assertFalse(daily.exists())
        assert waiting.approval_id is not None
        self.approvals.approve(self.context.user_id, waiting.approval_id)
        run = self.approvals.consume(self.context.user_id, waiting.approval_id)
        completed = await executor.execute_approved(
            self.context,
            run,
            approval_id=waiting.approval_id,
            decision=ApprovalDecision.ONCE,
        )

        payload = json.loads(completed.model_text)
        self.assertEqual(payload["data"]["status"], "recorded")
        self.assertIn("I prefer Python 3.12", daily.read_text(encoding="utf-8"))

    def test_sensitive_candidate_is_rejected_before_approval_summary(self) -> None:
        """敏感内容必须在 Executor 创建 Approval 之前被安全拒绝。"""
        tool = ProposeMemoryTool(self.store)
        with self.assertRaises(ToolValidationError) as caught:
            tool.validate(
                {
                    "content": "api_key = super-secret-value-123456",
                    "source": "user",
                }
            )

        self.assertNotIn("super-secret-value", str(caught.exception))
        with self.database.connect_read_only() as connection:
            count = connection.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
