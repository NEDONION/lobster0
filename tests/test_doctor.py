"""Lobster0 离线本地诊断的行为测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from lobster0.bootstrap import initialize_state
from lobster0.doctor import CheckStatus, run_local_checks
from lobster0.memory.markdown_store import MemoryMarkdownStore
from lobster0.memory.models import DisclosureContext, SourceRef
from lobster0.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryUnitRepository,
)
from lobster0.memory.service import ExplicitMemoryRequest, MemoryService
from lobster0.memory.store import MemoryStore
from lobster0.paths import build_state_paths
from lobster0.policy.engine import PolicyAction, PolicyDecision
from lobster0.providers.base import ToolCall
from lobster0.sandbox.base import SandboxUnavailableError
from lobster0.sandbox.docker import RootlessClientTransport
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.tooling import ApprovalRepository
from lobster0.tools.base import ToolContext


class DoctorTest(unittest.TestCase):
    """验证 doctor 检查真实状态、保持只读且不泄露配置内容。"""

    def setUp(self) -> None:
        """为每个诊断场景创建独立状态路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.paths = build_state_paths(self.root)
        self.node = self.paths.home / "test-node"
        self.node.parent.mkdir(parents=True, exist_ok=True)
        self.node.write_text("#!/bin/sh\nprintf 'v22.22.3\\n'\n", encoding="utf-8")
        self.node.chmod(0o700)
        self.tui_entry = self.paths.home / "main.js"
        self.tui_entry.write_text("// test entry\n", encoding="utf-8")
        self.tui_environ = {
            "LOBSTER0_NODE": str(self.node),
            "LOBSTER0_TUI_ENTRY": str(self.tui_entry),
        }

    def test_initialized_state_passes_all_local_checks(self) -> None:
        """完整初始化后 Personal 权限、Memory 与三平台检查都应通过。"""
        initialize_state(self.paths)

        owner_home = self.root / "owner"
        nvm_bin = owner_home / ".config/nvm/versions/node/v20.19.0/bin"
        nvm_bin.mkdir(parents=True)
        lark_cli = nvm_bin / "lark-cli"
        lark_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        lark_cli.chmod(0o700)

        with mock.patch("lobster0.doctor.Path.home", return_value=owner_home):
            results = run_local_checks(self.paths, self.tui_environ)

        self.assertEqual(
            {result.name for result in results},
            {
                "state_home",
                "config",
                "workspace",
                "database",
                "automation",
                "automation_ledger",
                "automation_leases",
                "sandbox_checkpoint",
                "permissions",
                "tools",
                "personal_permissions",
                "executables",
                "approvals",
                "node",
                "pi_tui",
                "browser",
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
                "memory",
            },
        )
        self.assertTrue(all(result.status is CheckStatus.PASS for result in results))
        by_name = {result.name: result for result in results}
        self.assertIn("profile personal", by_name["personal_permissions"].message)
        self.assertIn("lark-cli available", by_name["executables"].message)
        self.assertNotIn(str(owner_home), by_name["executables"].message)
        self.assertIn("schema v5", by_name["automation_ledger"].message)
        self.assertIn("checkpoint quota=64MiB", by_name["sandbox_checkpoint"].message)
        self.assertIn("disabled", by_name["browser"].message)

    def test_enabled_browser_requires_worker_playwright_and_chromium(self) -> None:
        """启用 Browser 后缺少 Worker、Playwright 或 Chromium 必须失败关闭。"""
        initialize_state(self.paths)
        config = self.paths.config.read_text(encoding="utf-8").replace(
            "[browser]\nenabled = false", "[browser]\nenabled = true"
        )
        self.paths.config.write_text(config, encoding="utf-8")

        with (
            mock.patch(
                "lobster0.doctor._browser_worker_root",
                return_value=self.root / "missing-worker",
            ),
            mock.patch("lobster0.doctor.shutil.which", return_value=None),
        ):
            results = run_local_checks(self.paths, self.tui_environ)

        browser = next(result for result in results if result.name == "browser")
        self.assertIs(browser.status, CheckStatus.FAIL)
        self.assertIn("Worker build", browser.message)

    def test_stale_browser_profile_lock_warns_without_deleting_it(self) -> None:
        """Doctor 只报告 stale Profile lock，不能擅自删除恢复证据。"""
        initialize_state(self.paths)
        config = self.paths.config.read_text(encoding="utf-8").replace(
            "[browser]\nenabled = false", "[browser]\nenabled = true"
        )
        self.paths.config.write_text(config, encoding="utf-8")
        worker = self.root / "browser-worker"
        (worker / "dist").mkdir(parents=True)
        (worker / "dist/server.js").write_text("// ready\n", encoding="utf-8")
        (worker / "node_modules/playwright-core").mkdir(parents=True)
        (worker / "node_modules/playwright-core/package.json").write_text(
            '{"name":"playwright-core"}\n', encoding="utf-8"
        )
        chromium = self.root / "chromium"
        chromium.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        chromium.chmod(0o700)
        lock = self.paths.browser / ".lobster0-browser.lock"
        lock.write_text('{"pid":999999,"token":"stale"}\n', encoding="utf-8")

        def discovered(command: str, *, path: str | None = None) -> str | None:
            del path
            return str(chromium) if command == "chromium" else None

        with (
            mock.patch("lobster0.doctor._browser_worker_root", return_value=worker),
            mock.patch("lobster0.doctor.shutil.which", side_effect=discovered),
        ):
            results = run_local_checks(self.paths, self.tui_environ)

        browser = next(result for result in results if result.name == "browser")
        self.assertIs(browser.status, CheckStatus.WARN)
        self.assertIn("stale", browser.message)
        self.assertTrue(lock.exists())

    def test_required_missing_sandbox_backend_fails_only_when_automation_enabled(
        self,
    ) -> None:
        """启用 Automation 后缺失 Docker 必须 FAIL；默认关闭时不阻塞 Doctor。"""
        initialize_state(self.paths)
        config_text = self.paths.config.read_text(encoding="utf-8")
        pinned_image = "example/lobster0@sha256:" + "a" * 64
        self.paths.config.write_text(
            config_text.replace("[automation]\nenabled = false", "[automation]\nenabled = true")
            .replace('image = "lobster0-sandbox:phase6"', f'image = "{pinned_image}"'),
            encoding="utf-8",
        )

        with mock.patch("lobster0.doctor.shutil.which", return_value=None):
            results = run_local_checks(self.paths, self.tui_environ)

        sandbox = next(result for result in results if result.name == "sandbox_checkpoint")
        self.assertIs(sandbox.status, CheckStatus.FAIL)
        self.assertIn("required docker", sandbox.message)

    def test_rootless_doctor_is_engine_specific_offline_and_redacted(self) -> None:
        """Doctor 复用 rootless 边界且不打印 UID、Home 或 socket path。"""
        initialize_state(self.paths)
        config_text = self.paths.config.read_text(encoding="utf-8")
        pinned_image = "example/lobster0@sha256:" + "a" * 64
        self.paths.config.write_text(
            config_text.replace("[automation]\nenabled = false", "[automation]\nenabled = true")
            .replace('image = "lobster0-sandbox:phase6"', f'image = "{pinned_image}"')
            .replace('container_engine = "docker-rootless"',
                     'container_engine = "podman-rootless"'),
            encoding="utf-8",
        )
        private_home = self.root / "private-owner-home"
        private_socket = Path("/run/user/1001/podman/podman.sock")
        transport = RootlessClientTransport(
            engine="podman-rootless",
            executable=Path("/usr/bin/podman"),
            environment=(
                ("HOME", str(private_home)),
                ("XDG_RUNTIME_DIR", "/run/user/1001"),
                ("CONTAINER_HOST", f"unix://{private_socket}"),
            ),
        )

        with mock.patch(
            "lobster0.doctor.discover_rootless_client_transport",
            return_value=transport,
        ):
            results = run_local_checks(self.paths, self.tui_environ)

        sandbox = next(result for result in results if result.name == "sandbox_checkpoint")
        self.assertIs(sandbox.status, CheckStatus.PASS)
        self.assertIn("podman-rootless", sandbox.message)
        self.assertIn("rootless client ready", sandbox.message)
        self.assertNotIn("1001", sandbox.message)
        self.assertNotIn(str(private_home), sandbox.message)
        self.assertNotIn(str(private_socket), sandbox.message)

        with mock.patch(
            "lobster0.doctor.discover_rootless_client_transport",
            side_effect=SandboxUnavailableError(),
        ):
            failed = run_local_checks(self.paths, self.tui_environ)
        unavailable = next(result for result in failed if result.name == "sandbox_checkpoint")
        self.assertIs(unavailable.status, CheckStatus.FAIL)
        self.assertIn("required podman-rootless", unavailable.message)

    def test_old_node_reports_actionable_pi_tui_failure(self) -> None:
        """默认 pi-tui 遇到旧 Node 时必须给出最低版本，而不是启动后崩溃。"""
        initialize_state(self.paths)
        self.node.write_text("#!/bin/sh\nprintf 'v20.19.0\\n'\n", encoding="utf-8")

        results = run_local_checks(self.paths, self.tui_environ)

        node = next(result for result in results if result.name == "node")
        build = next(result for result in results if result.name == "pi_tui")
        self.assertIs(node.status, CheckStatus.FAIL)
        self.assertIn("22.22.3", node.message)
        self.assertIn("24.15.0", node.message)
        self.assertIs(build.status, CheckStatus.FAIL)

    def test_node_check_uses_exact_supported_lts_ranges(self) -> None:
        """Doctor 必须接受两个 floor，并拒绝未验证的 23/25 major。"""
        initialize_state(self.paths)
        cases = (
            ("22.22.3", CheckStatus.PASS),
            ("24.15.0", CheckStatus.PASS),
            ("23.0.0", CheckStatus.FAIL),
            ("25.0.0", CheckStatus.FAIL),
        )

        for version, expected in cases:
            with self.subTest(version=version):
                self.node.write_text(
                    f"#!/bin/sh\nprintf 'v{version}\\n'\n",
                    encoding="utf-8",
                )
                results = run_local_checks(self.paths, self.tui_environ)
                by_name = {result.name: result for result in results}
                self.assertIs(by_name["node"].status, expected)
                self.assertIs(by_name["pi_tui"].status, expected)

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

    def test_memory_drift_is_reported_without_copying_private_text(self) -> None:
        """Doctor 只显示 drift 计数，不解析或回显手工编辑正文。"""
        initialized = initialize_state(self.paths)
        database = Database(self.paths.database)
        session = SessionRepository(database).get_or_create_cli(
            initialized.owner.id,
            "doctor-memory",
        )
        turn = TurnRepository(database).create_with_user_message(
            session.id,
            "doctor-memory-source",
            "test-model",
            "请记住我的私人偏好",
        )
        with database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        markdown = MemoryMarkdownStore(
            self.paths,
            MemoryManifestRepository(database),
        )
        MemoryService(
            markdown,
            MemoryUnitRepository(database),
            MemoryReviewRepository(database),
            MemoryStore(self.paths),
        ).remember_explicit(
            ExplicitMemoryRequest(
                DisclosureContext(
                    initialized.owner.id,
                    initialized.owner.id,
                    "cli",
                    "local",
                    True,
                ),
                SourceRef(message_id, session.id, "cli"),
                "请记住我的私人偏好",
                "用户偏好不公开的内部代号",
                datetime(2026, 8, 9, tzinfo=UTC),
            )
        )
        path = markdown.path_for_owner(initialized.owner.id)
        path.write_text(
            path.read_text(encoding="utf-8").replace("内部代号", "私人新正文"),
            encoding="utf-8",
        )

        results = run_local_checks(self.paths, {})

        item = next(result for result in results if result.name == "memory")
        self.assertIs(item.status, CheckStatus.WARN)
        self.assertIn("manifest_drift=1", item.message)
        self.assertNotIn("私人新正文", item.message)

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
            "LOBSTER0_FEISHU_APP_ID": "cli_test",
            "LOBSTER0_FEISHU_APP_SECRET": secret,
        }
        owner_home = self.root / "feishu-owner"
        nvm_bin = owner_home / ".config/nvm/versions/node/v20.19.0/bin"
        nvm_bin.mkdir(parents=True)
        lark_cli = nvm_bin / "lark-cli"
        lark_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        lark_cli.chmod(0o700)

        def discovered_which(command: str, *, path: str | None = None) -> str | None:
            if command == "lark-cli" and path is not None and str(nvm_bin) in path:
                return str(lark_cli)
            return None

        with (
            mock.patch("lobster0.doctor.Path.home", return_value=owner_home),
            mock.patch("lobster0.doctor.importlib.util.find_spec", return_value=object()),
            mock.patch("lobster0.doctor.shutil.which", side_effect=discovered_which),
        ):
            results = run_local_checks(self.paths, environment)

        by_name = {result.name: result for result in results}
        self.assertIs(by_name["feishu_config"].status, CheckStatus.PASS)
        self.assertIs(by_name["feishu_sdk"].status, CheckStatus.PASS)
        self.assertIs(by_name["channel_database"].status, CheckStatus.PASS)
        self.assertIs(by_name["feishu_runtime"].status, CheckStatus.PASS)
        self.assertNotIn(secret, repr(results))
        self.assertNotIn(str(owner_home), repr(results))

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
            "LOBSTER0_FEISHU_APP_ID": "cli_private",
            "LOBSTER0_FEISHU_APP_SECRET": "feishu-private",
            "LOBSTER0_TELEGRAM_BOT_TOKEN": "telegram-private",
            "LOBSTER0_DISCORD_BOT_TOKEN": "discord-private",
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
            if name.startswith("LOBSTER0_") and name not in {
                "LOBSTER0_NODE",
                "LOBSTER0_TUI_ENTRY",
            }:
                self.assertNotIn(value, repr(results))


if __name__ == "__main__":
    unittest.main()
