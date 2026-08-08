"""MiniClaw 离线本地诊断的行为测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from miniclaw.bootstrap import initialize_state
from miniclaw.doctor import CheckStatus, run_local_checks
from miniclaw.paths import build_state_paths
from miniclaw.policy.engine import PolicyAction, PolicyDecision
from miniclaw.providers.base import ToolCall
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository
from miniclaw.tools.base import ToolContext


class DoctorTest(unittest.TestCase):
    """验证 doctor 检查真实状态、保持只读且不泄露配置内容。"""

    def setUp(self) -> None:
        """为每个诊断场景创建独立状态路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.node = self.paths.home / "test-node"
        self.node.parent.mkdir(parents=True, exist_ok=True)
        self.node.write_text("#!/bin/sh\nprintf 'v22.19.0\\n'\n", encoding="utf-8")
        self.node.chmod(0o700)
        self.tui_entry = self.paths.home / "main.js"
        self.tui_entry.write_text("// test entry\n", encoding="utf-8")
        self.tui_environ = {
            "MINICLAW_NODE": str(self.node),
            "MINICLAW_TUI_ENTRY": str(self.tui_entry),
        }

    def test_initialized_state_passes_all_local_checks(self) -> None:
        """完整初始化后三平台二十项离线检查都应实际通过。"""
        initialize_state(self.paths)

        results = run_local_checks(self.paths, self.tui_environ)

        self.assertEqual(
            {result.name for result in results},
            {
                "state_home",
                "config",
                "workspace",
                "database",
                "permissions",
                "tools",
                "approvals",
                "node",
                "pi_tui",
                "feishu_config",
                "feishu_sdk",
                "feishu_runtime",
                "telegram_config",
                "telegram_sdk",
                "telegram_runtime",
                "discord_config",
                "discord_sdk",
                "discord_runtime",
                "channel_database",
                "channel_workers",
            },
        )
        self.assertTrue(all(result.status is CheckStatus.PASS for result in results))

    def test_old_node_reports_actionable_pi_tui_failure(self) -> None:
        """默认 pi-tui 遇到旧 Node 时必须给出最低版本，而不是启动后崩溃。"""
        initialize_state(self.paths)
        self.node.write_text("#!/bin/sh\nprintf 'v20.19.0\\n'\n", encoding="utf-8")

        results = run_local_checks(self.paths, self.tui_environ)

        node = next(result for result in results if result.name == "node")
        build = next(result for result in results if result.name == "pi_tui")
        self.assertIs(node.status, CheckStatus.FAIL)
        self.assertIn("22.19.0", node.message)
        self.assertIs(build.status, CheckStatus.FAIL)

    def test_corrupt_config_fails_without_exposing_file_contents(self) -> None:
        """损坏配置必须失败，诊断消息不能回显其中的密钥样例。"""
        initialize_state(self.paths)
        self.paths.config.write_text(
            '[provider\napi_key = "super-secret-value"\n',
            encoding="utf-8",
        )

        results = run_local_checks(self.paths, {})
        config_result = next(result for result in results if result.name == "config")

        self.assertIs(config_result.status, CheckStatus.FAIL)
        self.assertNotIn("super-secret-value", config_result.message)

    def test_missing_state_is_reported_without_creating_it(self) -> None:
        """doctor 是只读检查，不能为了诊断而创建不存在的状态目录。"""
        missing_paths = build_state_paths(self.paths.home / "missing")

        results = run_local_checks(missing_paths, {})

        self.assertTrue(any(result.status is CheckStatus.FAIL for result in results))
        self.assertFalse(missing_paths.home.exists())

    def test_permissive_config_mode_fails_permission_check(self) -> None:
        """配置对 group 或 other 可读时必须报告权限失败。"""
        initialize_state(self.paths)
        self.paths.config.chmod(0o644)

        results = run_local_checks(self.paths, {})
        permission_result = next(result for result in results if result.name == "permissions")

        self.assertIs(permission_result.status, CheckStatus.FAIL)

    def test_reports_pending_approval_without_executing_it_or_writing_database(self) -> None:
        """doctor 只报告当前 pending 数，不消费动作也不更新任何 SQLite 行。"""
        initialized = initialize_state(self.paths)
        database = Database(self.paths.database)
        session = SessionRepository(database).get_or_create_cli(
            initialized.owner.id,
            "doctor-approval",
        )
        turns = TurnRepository(database)
        turn = turns.create_with_user_message(
            session.id,
            "doctor-event",
            "test-model",
            "write",
        )
        turns.mark_running(turn.id)
        side_effect = self.paths.workspace / "doctor-must-not-write.txt"
        arguments = {"path": str(side_effect), "content": "no", "overwrite": False}
        ApprovalRepository(
            database,
            clock=lambda: datetime(2026, 8, 8, tzinfo=UTC),
        ).create_waiting(
            ToolContext(
                initialized.owner.id,
                session.id,
                turn.id,
                self.paths.home,
                self.paths.workspace,
                (),
            ),
            ToolCall("doctor-write", "write_file", arguments),
            arguments,
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=10**9,
            summary="write_file doctor-must-not-write.txt",
        )
        before = self.paths.database.read_bytes()

        results = run_local_checks(self.paths, {})

        item = next(result for result in results if result.name == "approvals")
        self.assertIs(item.status, CheckStatus.WARN)
        self.assertIn("1 pending", item.message)
        self.assertFalse(side_effect.exists())
        self.assertEqual(self.paths.database.read_bytes(), before)

    def test_enabled_feishu_checks_are_offline_and_secret_redacted(self) -> None:
        """飞书 Doctor 只查配置、SDK、表和变量存在性，不连接平台或打印值。"""
        initialize_state(self.paths)
        with self.paths.config.open("a", encoding="utf-8") as config_file:
            config_file.write(
                "\n[channels.feishu]\n"
                "enabled = true\n"
                'owner_open_id = "ou_owner"\n'
                'allowed_open_ids = ["ou_owner"]\n'
            )
        secret = "doctor-secret-private"
        environment = {
            "MINICLAW_FEISHU_APP_ID": "cli_test",
            "MINICLAW_FEISHU_APP_SECRET": secret,
        }

        with mock.patch("miniclaw.doctor.importlib.util.find_spec", return_value=object()):
            results = run_local_checks(self.paths, environment)

        by_name = {result.name: result for result in results}
        self.assertIs(by_name["feishu_config"].status, CheckStatus.PASS)
        self.assertIs(by_name["feishu_sdk"].status, CheckStatus.PASS)
        self.assertIs(by_name["channel_database"].status, CheckStatus.PASS)
        self.assertIn(
            by_name["feishu_runtime"].status,
            {CheckStatus.PASS, CheckStatus.WARN},
        )
        self.assertNotIn(secret, repr(results))

    def test_three_enabled_channels_are_checked_offline_and_worker_budget_warns(self) -> None:
        """Telegram/Discord 只查本地 SDK/Token；总 worker 超 8 给 WARN，不做认证网络。"""
        initialize_state(self.paths)
        with self.paths.config.open("a", encoding="utf-8") as config_file:
            config_file.write(
                "\n[channels.feishu]\n"
                "enabled = true\n"
                'owner_open_id = "ou_owner"\n'
                'allowed_open_ids = ["ou_owner"]\n'
                "worker_count = 3\n"
                "\n[channels.telegram]\n"
                "enabled = true\n"
                "owner_user_id = 300\n"
                "allowed_user_ids = [300]\n"
                "worker_count = 3\n"
                "\n[channels.discord]\n"
                "enabled = true\n"
                "owner_user_id = 300\n"
                "allowed_user_ids = [300]\n"
                "worker_count = 3\n"
            )
        environment = {
            **self.tui_environ,
            "MINICLAW_FEISHU_APP_ID": "cli_private",
            "MINICLAW_FEISHU_APP_SECRET": "feishu-private",
            "MINICLAW_TELEGRAM_BOT_TOKEN": "telegram-private",
            "MINICLAW_DISCORD_BOT_TOKEN": "discord-private",
        }
        spec_calls: list[str] = []

        def find_spec(name: str):
            spec_calls.append(name)
            return object()

        results = run_local_checks(
            self.paths,
            environment,
            find_spec=find_spec,
        )

        by_name = {result.name: result for result in results}
        for channel in ("feishu", "telegram", "discord"):
            self.assertIs(by_name[f"{channel}_config"].status, CheckStatus.PASS)
            self.assertIs(by_name[f"{channel}_sdk"].status, CheckStatus.PASS)
            self.assertIs(by_name[f"{channel}_runtime"].status, CheckStatus.PASS)
            self.assertIn("locally ready", by_name[f"{channel}_runtime"].message)
        self.assertEqual(spec_calls, ["lark_channel", "telegram", "discord"])
        self.assertIs(by_name["channel_workers"].status, CheckStatus.WARN)
        self.assertIn("9", by_name["channel_workers"].message)
        for name, value in environment.items():
            if name.startswith("MINICLAW_") and name not in {
                "MINICLAW_NODE",
                "MINICLAW_TUI_ENTRY",
            }:
                self.assertNotIn(value, repr(results))


if __name__ == "__main__":
    unittest.main()
