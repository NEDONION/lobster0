"""memory_remember Tool 的参数最小化和显式意图纵切测试。"""

import json
import tempfile
import unittest
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.markdown_store import MemoryMarkdownStore
from miniclaw.memory.models import DisclosureContext
from miniclaw.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryUnitRepository,
)
from miniclaw.memory.service import MemoryService
from miniclaw.memory.store import MemoryStore
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import ToolCall
from miniclaw.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ToolRunRepository
from miniclaw.tools.base import ToolContext
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.memory_v2 import MemoryRememberTool
from miniclaw.tools.registry import ToolRegistry


class MemoryRememberToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 Tool 不接收 Owner/status/scope，并在明确请求内直接提交。"""

    def setUp(self) -> None:
        """创建含明确 User Message 的 running Turn 和真实 Executor。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "memory-tool-v2",
        )
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "remember-tool-source",
            "test-model",
            "请记住我偏好中文回复",
        )
        TurnRepository(self.database).mark_running(turn.id)
        service = MemoryService(
            MemoryMarkdownStore(
                self.paths,
                MemoryManifestRepository(self.database),
            ),
            MemoryUnitRepository(self.database),
            MemoryReviewRepository(self.database),
            MemoryStore(self.paths),
        )
        tool = MemoryRememberTool(service, MessageRepository(self.database))
        self.executor = ToolExecutor(
            ToolRegistry((tool,)),
            PolicyEngine(),
            ToolRunRepository(self.database),
        )
        self.context = ToolContext(
            user_id=self.owner.id,
            session_id=session.id,
            turn_id=turn.id,
            state_home=self.paths.home,
            workspace=self.paths.workspace,
            read_only_roots=(),
            disclosure=DisclosureContext(
                self.owner.id,
                self.owner.id,
                "cli",
                "local",
                True,
            ),
        )

    async def test_explicit_remember_is_low_risk_and_commits_in_one_turn(self) -> None:
        """已绑定明确意图时 Tool 无 Approval 直接返回 active Unit。"""
        outcome = await self.executor.execute(
            self.context,
            ToolCall(
                "remember-v2",
                "memory_remember",
                {"fact": "用户偏好使用中文回复"},
            ),
        )

        payload = json.loads(outcome.model_text)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["status"], "active")
        self.assertIsNone(outcome.approval_id)

    def test_schema_and_validation_exclude_owner_status_scope_and_source(self) -> None:
        """模型只能提供 fact，所有身份、来源和状态均来自 Core。"""
        tool = self.executor._registry.get("memory_remember")
        assert tool is not None
        properties = tool.definition.parameters["properties"]
        self.assertEqual(set(properties), {"fact"})
        with self.assertRaises(ValueError):
            tool.validate({"fact": "x", "owner_id": 1})


if __name__ == "__main__":
    unittest.main()
