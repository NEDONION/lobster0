"""四档权限模式与可信 Owner 边界测试。"""

import json
import tempfile
import unittest
from pathlib import Path

import lobster0.storage.tooling as tooling
from lobster0.bootstrap import initialize_state
from lobster0.config import load_config
from lobster0.paths import build_state_paths
from lobster0.policy.engine import PolicyAction, PolicyEngine
from lobster0.policy.modes import PermissionMode, PermissionState
from lobster0.runtime import create_runtime
from lobster0.storage.database import Database
from lobster0.tools.base import ToolContext
from lobster0.tools.command import RunCommandTool
from lobster0.tools.filesystem import WriteFileTool
from lobster0.tools.web import HttpGetTool


class PermissionModePolicyTest(unittest.TestCase):
    """验证自动化模式只能扩大可信 Owner 的已校验动作。"""

    def setUp(self) -> None:
        """创建可执行文件、Workspace 和不访问真实网络的 Resolver。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.program = self.workspace / "lark-cli"
        self.program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.program.chmod(0o700)

    def context(self, *, trusted_owner: bool) -> ToolContext:
        """构造明确标记入口信任级别的 ToolContext。"""
        try:
            return ToolContext(
                1,
                1,
                1,
                self.workspace / ".state",
                self.workspace,
                (),
                trusted_owner=trusted_owner,
            )
        except TypeError:
            self.fail("ToolContext must expose trusted_owner")

    def engine(self, mode: str) -> PolicyEngine:
        """构造带确定权限模式和离线公网解析结果的 Policy。"""
        try:
            return PolicyEngine(
                mode=mode,
                network_resolver=lambda _host, _port: ("93.184.216.34",),
            )
        except TypeError:
            self.fail("PolicyEngine must accept mode")

    def test_command_modes_auto_run_only_for_trusted_autopilot_and_yolo(self) -> None:
        """未命中规则的命令只能在可信 Autopilot/YOLO 中自动执行。"""
        arguments = {
            "program": str(self.program),
            "args": ["doc", "list"],
            "timeout_seconds": 30,
        }
        cases = (
            ("safe", True, PolicyAction.REQUIRE_APPROVAL),
            ("smart", True, PolicyAction.REQUIRE_APPROVAL),
            ("autopilot", True, PolicyAction.ALLOW),
            ("yolo", True, PolicyAction.ALLOW),
            ("autopilot", False, PolicyAction.REQUIRE_APPROVAL),
            ("yolo", False, PolicyAction.REQUIRE_APPROVAL),
        )

        for mode, trusted_owner, expected in cases:
            with self.subTest(mode=mode, trusted_owner=trusted_owner):
                decision = self.engine(mode).authorize(
                    RunCommandTool().definition,
                    self.context(trusted_owner=trusted_owner),
                    arguments,
                )
                self.assertEqual(decision.action, expected)

    def test_smart_auto_get_and_autopilot_auto_write_only_for_trusted_owner(self) -> None:
        """Smart 只自动 HTTPS GET；写入需可信 Autopilot/YOLO。"""
        http_arguments = {"url": "https://example.com/docs", "timeout_seconds": 20}
        write_arguments = {"path": "note.txt", "content": "hello"}
        trusted = self.context(trusted_owner=True)
        untrusted = self.context(trusted_owner=False)

        self.assertEqual(
            self.engine("safe").authorize(
                HttpGetTool().definition,
                trusted,
                http_arguments,
            ).action,
            PolicyAction.REQUIRE_APPROVAL,
        )
        self.assertEqual(
            self.engine("smart").authorize(
                HttpGetTool().definition,
                trusted,
                http_arguments,
            ).action,
            PolicyAction.ALLOW,
        )
        self.assertEqual(
            self.engine("smart").authorize(
                HttpGetTool().definition,
                untrusted,
                http_arguments,
            ).action,
            PolicyAction.REQUIRE_APPROVAL,
        )

        for mode, context, expected in (
            ("safe", trusted, PolicyAction.REQUIRE_APPROVAL),
            ("smart", trusted, PolicyAction.REQUIRE_APPROVAL),
            ("autopilot", trusted, PolicyAction.ALLOW),
            ("yolo", trusted, PolicyAction.ALLOW),
            ("autopilot", untrusted, PolicyAction.REQUIRE_APPROVAL),
        ):
            with self.subTest(mode=mode, trusted_owner=context.trusted_owner):
                decision = self.engine(mode).authorize(
                    WriteFileTool().definition,
                    context,
                    write_arguments,
                )
                self.assertEqual(decision.action, expected)
                self.assertEqual(
                    decision.normalized_arguments["path"],
                    str(self.workspace / "note.txt"),
                )


class PermissionModeStateTest(unittest.IsolatedAsyncioTestCase):
    """验证共享模式切换可审计、幂等并进入 Runtime。"""

    async def test_mode_change_is_redacted_audited_and_runtime_uses_config(self) -> None:
        """模式变化只记录安全标量，Runtime 使用配置的初始值。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            database = Database(paths.database)
            repository_type = getattr(tooling, "PermissionModeAuditRepository", None)
            self.assertIsNotNone(repository_type)
            repository = repository_type(database)
            state = PermissionState(PermissionMode.SAFE, audit=repository.record)

            selected = state.set_mode(
                PermissionMode.AUTOPILOT,
                user_id=1,
                source="cli",
            )
            state.set_mode(PermissionMode.AUTOPILOT, user_id=1, source="cli")

            self.assertEqual(selected, PermissionMode.AUTOPILOT)
            with database.connect_read_only() as connection:
                rows = connection.execute(
                    "SELECT event_type, summary, metadata_json FROM audit_events "
                    "WHERE event_type = 'policy.mode_changed'"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["summary"], "Changed permission mode")
            self.assertEqual(
                json.loads(rows[0]["metadata_json"]),
                {
                    "current_mode": "autopilot",
                    "previous_mode": "safe",
                    "source": "cli",
                },
            )

            config = load_config(paths, {}, {})
            runtime = create_runtime(config, paths, "offline-key")
            try:
                runtime_state = getattr(runtime, "permission_state", None)
                self.assertIsNotNone(runtime_state)
                self.assertEqual(runtime_state.mode, PermissionMode.AUTOPILOT)
            finally:
                await runtime.aclose()

    async def test_failed_audit_keeps_previous_mode(self) -> None:
        """审计异常必须发生在状态变化前，避免无记录扩权。"""
        def fail_audit(*_arguments) -> None:
            raise RuntimeError("audit unavailable")

        state = PermissionState(PermissionMode.SAFE, audit=fail_audit)

        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            state.set_mode(PermissionMode.YOLO, user_id=1, source="cli")

        self.assertEqual(state.mode, PermissionMode.SAFE)


if __name__ == "__main__":
    unittest.main()
