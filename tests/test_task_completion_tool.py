"""Automation-only complete_task terminal Tool 测试。"""

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.tools.base import ToolContext, ToolValidationError
from miniclaw.tools.task_completion import CompleteTaskTool


class CompleteTaskToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 completion 只能从绑定 TaskRun 的 automation context 产生。"""

    def setUp(self) -> None:
        """创建一个真实 ToolContext 并保留 interactive 默认值。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(paths)
        database = Database(paths.database)
        session = SessionRepository(database).get_or_create_cli(
            initialized.owner.id,
            "complete-task-test",
        )
        turns = TurnRepository(database)
        turn = turns.create_with_user_message(
            session.id,
            "complete-task-event",
            "test-model",
            "finish",
        )
        turns.mark_running(turn.id)
        self.context = ToolContext(
            user_id=initialized.owner.id,
            session_id=session.id,
            turn_id=turn.id,
            state_home=paths.home,
            workspace=paths.workspace,
            read_only_roots=(),
        )
        self.tool = CompleteTaskTool()

    async def test_valid_notify_and_silent_results_are_canonical(self) -> None:
        """通知与静默结果都返回固定 notify/text 结构。"""
        context = replace(self.context, source="automation", task_run_id=42)

        notified = await self.tool.execute(
            context,
            self.tool.validate({"notify": True, "text": "完成"}),
        )
        silent = await self.tool.execute(
            context,
            self.tool.validate({"notify": False, "text": ""}),
        )

        self.assertEqual(notified.data, {"notify": True, "text": "完成"})
        self.assertEqual(silent.data, {"notify": False, "text": ""})

    async def test_interactive_or_unbound_context_fails_closed(self) -> None:
        """普通聊天与缺少 task_run_id 的伪 automation context 均不能终止任务。"""
        arguments = self.tool.validate({"notify": True, "text": "完成"})

        interactive = await self.tool.execute(self.context, arguments)
        unbound = await self.tool.execute(
            replace(self.context, source="automation"),
            arguments,
        )

        self.assertEqual(interactive.error_code, "automation_context_required")
        self.assertEqual(unbound.error_code, "automation_context_required")

    def test_unknown_types_silent_text_and_large_text_are_rejected(self) -> None:
        """Schema 绕过、bool 伪造和超大结果必须在执行前拒绝。"""
        invalid = (
            {"notify": True, "text": "ok", "extra": 1},
            {"notify": 1, "text": "ok"},
            {"notify": False, "text": "not silent"},
            {"notify": True, "text": "好" * 100_000},
        )
        for arguments in invalid:
            with self.subTest(arguments=set(arguments)):
                with self.assertRaises(ToolValidationError):
                    self.tool.validate(arguments)


if __name__ == "__main__":
    unittest.main()
