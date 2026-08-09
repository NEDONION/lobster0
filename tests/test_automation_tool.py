"""用户可见 manage_task Tool 的 action、风险、Owner 与 Guard 测试。"""

import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.automation.guard import AutomationPromptGuard
from miniclaw.automation.repository import ScheduledTaskRepository, TaskRunRepository
from miniclaw.bootstrap import initialize_state
from miniclaw.config import ChannelConfig, FeishuConfig
from miniclaw.memory.models import DisclosureContext
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyEngine
from miniclaw.policy.modes import PermissionMode
from miniclaw.providers.base import ToolCall
from miniclaw.skills.loader import SkillLoader
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository, ToolRunRepository
from miniclaw.tools.automation import ManageTaskTool
from miniclaw.tools.base import ToolContext, ToolResult, ToolRisk, ToolValidationError
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.registry import ToolRegistry


class ManageTaskToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 Task control plane 只有明确 action 且始终 owner-scoped。"""

    def setUp(self) -> None:
        """创建启用 Automation 的真实 SQLite、Context 与飞书 origin。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.owner_id = initialized.owner.id
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            self.owner_id,
            "manage-task-test",
        )
        turns = TurnRepository(self.database)
        turn = turns.create_with_user_message(
            session.id,
            "manage-task-event",
            "test-model",
            "create a task",
        )
        turns.mark_running(turn.id)
        self.context = ToolContext(
            user_id=self.owner_id,
            session_id=session.id,
            turn_id=turn.id,
            state_home=self.paths.home,
            workspace=self.paths.workspace,
            read_only_roots=(),
            disclosure=DisclosureContext(
                self.owner_id,
                self.owner_id,
                "feishu",
                "direct",
                True,
            ),
            account_id="work",
            external_conversation_id="oc_direct",
        )
        self.now = datetime(2026, 8, 9, 8, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.wakes = 0
        self.tool = ManageTaskTool(
            self.tasks,
            self.runs,
            AutomationPromptGuard(SkillLoader(self.paths.skills)),
            ChannelConfig(
                feishu=FeishuConfig(
                    enabled=True,
                    account_id="work",
                    owner_open_id="ou_owner",
                    allowed_open_ids=("ou_owner",),
                )
            ),
            enabled=True,
            misfire_grace_seconds=300,
            clock=lambda: self.now,
            wake=self._wake,
        )

    def _wake(self) -> None:
        """记录 create/update/resume/run_now 的 Scheduler wake。"""
        self.wakes += 1

    async def _call(
        self,
        arguments: dict[str, object],
        context: ToolContext | None = None,
    ) -> ToolResult:
        """按真实 Executor 顺序执行 validate → prepare → execute。"""
        selected = context or self.context
        validated = self.tool.validate(arguments)
        prepared = self.tool.prepare(selected, validated)
        return await self.tool.execute(selected, prepared)

    def _create_arguments(self) -> dict[str, object]:
        """返回固定的飞书 origin interval Task 参数。"""
        return {
            "action": "create",
            "name": "hourly report",
            "schedule": {"kind": "interval", "expression": "3600"},
            "prompt": "Summarize reports/status.json in Chinese.",
            "skills": [],
            "delivery": {"route": "origin"},
        }

    async def test_create_list_show_update_pause_resume_cancel(self) -> None:
        """完整 action 生命周期应保持 version 冲突与 terminal 不可逆。"""
        created = await self._call(self._create_arguments())
        task_id = created.data["task_id"]
        version = created.data["version"]
        listed = await self._call({"action": "list"})
        shown = await self._call({"action": "show", "task_id": task_id})

        self.assertEqual(len(listed.data["tasks"]), 1)
        self.assertNotIn("prompt", shown.data)
        self.assertNotIn("conversation_id", str(shown.data))
        updated = await self._call(
            {
                "action": "update",
                "task_id": task_id,
                "version": version,
                "name": "renamed report",
            }
        )
        paused = await self._call(
            {
                "action": "pause",
                "task_id": task_id,
                "version": updated.data["version"],
            }
        )
        resumed = await self._call(
            {
                "action": "resume",
                "task_id": task_id,
                "version": paused.data["version"],
            }
        )
        cancelled = await self._call(
            {
                "action": "cancel",
                "task_id": task_id,
                "version": resumed.data["version"],
            }
        )

        self.assertEqual(updated.data["name"], "renamed report")
        self.assertEqual(paused.data["status"], "paused")
        self.assertEqual(resumed.data["status"], "active")
        self.assertEqual(cancelled.data["status"], "cancelled")
        terminal = await self._call(
            {
                "action": "resume",
                "task_id": task_id,
                "version": cancelled.data["version"],
            }
        )
        self.assertEqual(terminal.error_code, "task_terminal")

    async def test_secret_prompt_and_untrusted_origin_fail_before_write(self) -> None:
        """Guard 与 origin 解析失败时 scheduled_tasks 必须保持空。"""
        secret = self._create_arguments()
        secret["prompt"] = "Authorization: Bearer SECRET_SENTINEL_123"
        with self.assertRaisesRegex(Exception, "task_prompt_secret"):
            await self._call(secret)
        with self.assertRaisesRegex(Exception, "delivery_origin_untrusted"):
            await self._call(
                self._create_arguments(),
                replace(
                    self.context,
                    disclosure=replace(
                        self.context.disclosure,
                        conversation_kind="group",
                    ),
                ),
            )
        self.assertEqual(self.tasks.list(owner_id=self.owner_id), ())

    async def test_owner_isolation_version_conflict_and_run_now(self) -> None:
        """其他 Owner 看不到 Task；旧 version 失败；manual Run 不推进 schedule。"""
        created = await self._call(self._create_arguments())
        task_id = created.data["task_id"]
        before = self.tasks.get(task_id).schedule.next_run_at
        other = replace(self.context, user_id=self.owner_id + 100)

        with self.assertRaisesRegex(Exception, "task_owner_required"):
            await self._call({"action": "list"}, other)
        manual = await self._call({"action": "run_now", "task_id": task_id})

        self.assertEqual(manual.data["status"], "queued")
        self.assertEqual(self.tasks.get(task_id).schedule.next_run_at, before)
        self.assertGreaterEqual(self.wakes, 2)
        stale = await self._call(
            {
                "action": "pause",
                "task_id": task_id,
                "version": created.data["version"] + 1,
            }
        )
        self.assertEqual(stale.error_code, "task_version_conflict")

    async def test_automation_context_is_denied_and_actions_have_bound_risk(self) -> None:
        """后台 Run 不能递归管理 Task，风险由 action 而非静态 Tool 名决定。"""
        denied = await self.tool.execute(
            replace(self.context, source="automation", task_run_id=7),
            {"action": "list"},
        )

        self.assertEqual(denied.error_code, "recursive_automation_denied")
        self.assertEqual(self.tool.effective_risk({"action": "list"}), ToolRisk.LOW)
        self.assertEqual(self.tool.effective_risk({"action": "create"}), ToolRisk.MEDIUM)
        self.assertEqual(self.tool.effective_risk({"action": "cancel"}), ToolRisk.HIGH)

    async def test_executor_prepares_before_persistence_and_high_cancel_waits(self) -> None:
        """Executor 应先 Guard 再写 ToolRun，Autopilot 的 HIGH cancel 仍需审批。"""
        approvals = ApprovalRepository(self.database, clock=lambda: self.now)
        executor = ToolExecutor(
            ToolRegistry((self.tool,)),
            PolicyEngine(mode=PermissionMode.AUTOPILOT),
            ToolRunRepository(self.database),
            approvals=approvals,
        )
        created = await executor.execute(
            self.context,
            ToolCall("call_create", "manage_task", self._create_arguments()),
        )
        assert created.result is not None and isinstance(created.result.data, dict)
        task_id = created.result.data["task_id"]
        version = created.result.data["version"]
        secret = self._create_arguments()
        secret["prompt"] = "token=SECRET_SENTINEL_123"
        rejected = await executor.execute(
            self.context,
            ToolCall("call_secret", "manage_task", secret),
        )
        pending = await executor.execute(
            self.context,
            ToolCall(
                "call_cancel",
                "manage_task",
                {
                    "action": "cancel",
                    "task_id": task_id,
                    "version": version,
                },
            ),
        )

        self.assertTrue(created.succeeded)
        self.assertEqual(rejected.result, None)
        self.assertIn("task_prompt_secret", rejected.model_text)
        self.assertIsNotNone(pending.approval_id)
        with self.database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT tool_call_id, status FROM tool_runs ORDER BY id"
            ).fetchall()
        self.assertEqual(
            [tuple(row) for row in rows],
            [("call_create", "succeeded"), ("call_cancel", "waiting_approval")],
        )

    def test_unknown_actions_keys_and_shapes_are_rejected(self) -> None:
        """halt/unhalt、未知字段、bool ID 与空 update 都不进入 prepare。"""
        invalid = (
            {"action": "halt"},
            {"action": "list", "extra": True},
            {"action": "show", "task_id": True},
            {"action": "update", "task_id": 1, "version": 1},
        )
        for arguments in invalid:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ToolValidationError):
                    self.tool.validate(arguments)


if __name__ == "__main__":
    unittest.main()
