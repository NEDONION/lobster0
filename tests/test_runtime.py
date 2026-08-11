"""共享 AgentRuntime 的最小装配测试。"""

import subprocess
import sys
import tempfile
import tomllib
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from lobster0.bootstrap import initialize_state
from lobster0.config import load_config
from lobster0.memory.models import DisclosureContext
from lobster0.paths import build_state_paths
from lobster0.runtime import create_channel_manager, create_runtime, limits_for_channel
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database


class AgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    """验证 CLI/TUI 共用同一套 Owner、Service 和 Tool Registry。"""

    async def test_channel_sdks_are_optional(self) -> None:
        """核心 Runtime 不应导入三套 Channel SDK，但应提供精确 optional extras。"""
        project_root = Path(__file__).resolve().parents[1]
        with (project_root / "pyproject.toml").open("rb") as project_file:
            project = tomllib.load(project_file)

        extras = project["project"]["optional-dependencies"]
        self.assertEqual(extras["feishu"], ["lark-channel-sdk>=1.2,<2"])
        self.assertEqual(extras["telegram"], ["python-telegram-bot>=21,<23"])
        self.assertEqual(extras["discord"], ["discord.py>=2.4,<3"])
        self.assertEqual(
            extras["channels"],
            [
                "lark-channel-sdk>=1.2,<2",
                "python-telegram-bot>=21,<23",
                "discord.py>=2.4,<3",
            ],
        )

        imported = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import lobster0.runtime; "
                "raise SystemExit(any(name in sys.modules "
                "for name in ('lark_channel', 'telegram', 'discord')))",
            ],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)

    async def test_artifact_store_exists_even_when_the_browser_is_disabled(self) -> None:
        """附件与浏览器无关，Store 不能被 browser.enabled 挡住。

        browser.enabled 默认是 False，若 Store 仍随浏览器一起构造，默认配置下的
        用户就永远发不了附件。
        """
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            config = load_config(paths)
            self.assertFalse(config.browser.enabled)

            runtime = create_runtime(config, paths, "test-key")
            try:
                self.assertIsNotNone(runtime.artifact_store)
                self.assertEqual(runtime.attachment_max_bytes, config.attachments.max_bytes)
            finally:
                await runtime.aclose()

    async def test_create_runtime_exposes_memory_autopilot_tools_and_closes(self) -> None:
        """Runtime 应装配明确 remember Tool，并拥有统一 Provider 生命周期。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            owner = initialize_state(paths).owner
            config = load_config(paths)

            runtime = create_runtime(config, paths, "test-key")
            try:
                await runtime.astart()
                self.assertEqual(runtime.owner_id, owner.id)
                self.assertEqual(runtime.model, "deepseek-v4-pro")
                self.assertEqual(runtime.workspace, paths.workspace)
                self.assertEqual(runtime.ui_language, "zh-CN")
                self.assertEqual(runtime.context_budget_tokens, 32_000)
                self.assertEqual(
                    [definition.name for definition in runtime.tool_definitions],
                    [
                        "edit_file",
                        "glob",
                        "grep",
                        "http_get",
                        "manage_task",
                        "memory_correct",
                        "memory_flush",
                        "memory_forget",
                        "memory_get",
                        "memory_list",
                        "memory_remember",
                        "memory_review_list",
                        "memory_search",
                        "propose_memory",
                        "read_artifact",
                        "read_file",
                        "read_memory",
                        "run_command",
                        "system_info",
                        "write_file",
                    ],
                )
                self.assertIsNotNone(runtime.service)
                self.assertTrue(runtime.memory_worker.running)
                limits = limits_for_channel(config, "feishu")
                manager = create_channel_manager(
                    paths,
                    runtime,
                    limits,
                    owner_external_user_id="ou_owner",
                )
                self.assertIs(manager.service, runtime.service)
                self.assertEqual(manager.owner_id, runtime.owner_id)
            finally:
                await runtime.aclose()
            self.assertFalse(runtime.memory_worker.running)

    async def test_create_runtime_settles_stale_foreground_turns(self) -> None:
        """Desktop 重启时遗留 running Turn 必须可见为 runtime_interrupted。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            owner = initialize_state(paths).owner
            database = Database(paths.database)
            session = SessionRepository(database).get_or_create_cli(owner.id, "desktop-stale")
            turns = TurnRepository(database)
            stale = turns.create_with_user_message(session.id, "stale", "model", "未完成")
            turns.mark_running(stale.id)

            runtime = create_runtime(load_config(paths), paths, "test-key")
            try:
                recovered = turns.get(stale.id)
                self.assertEqual(recovered.status, "failed")
                self.assertEqual(recovered.error_code, "runtime_interrupted")
                listed = runtime.conversation_console.list_sessions(owner.id, limit=10)
                self.assertEqual(listed["sessions"][0]["session_key"], "desktop-stale")
            finally:
                await runtime.aclose()

    async def test_create_runtime_passes_all_adaptive_budgets_to_runner(self) -> None:
        """Runtime 必须把 soft、hard 与无进展预算原样交给 AgentRunner。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            paths.config.write_text(
                "[agent]\n"
                "max_tool_iterations = 40\n"
                "max_tool_iterations_hard = 48\n"
                "max_no_progress_iterations = 5\n",
                encoding="utf-8",
            )
            config = load_config(paths)

            runtime = create_runtime(config, paths, "test-key")
            try:
                runner = runtime.service._runner
                self.assertEqual(runner._max_iterations, 40)
                self.assertEqual(runner._hard_max_iterations, 48)
                self.assertEqual(runner._max_no_progress_iterations, 5)
            finally:
                await runtime.aclose()

    async def test_runtime_receives_hard_budget_expanded_from_legacy_soft_config(self) -> None:
        """旧配置仅给较大 soft 时 Runtime 应收到相同的计算后 hard。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            paths.config.write_text(
                "[agent]\nmax_tool_iterations = 100\n",
                encoding="utf-8",
            )
            config = load_config(paths, {}, {})

            runtime = create_runtime(config, paths, "test-key")
            try:
                runner = runtime.service._runner
                self.assertEqual(runner._max_iterations, 100)
                self.assertEqual(runner._hard_max_iterations, 100)
            finally:
                await runtime.aclose()

    async def test_channel_limits_map_all_typed_config_without_secrets(self) -> None:
        """三个平台应稳定映射同一公共预算，不复制 Manager factory。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            config = load_config(paths)

        feishu = limits_for_channel(config, "feishu")
        telegram = limits_for_channel(config, "telegram")
        discord = limits_for_channel(config, "discord")

        self.assertEqual(
            (feishu.message_max_chars, feishu.progress_update_interval),
            (30000, 0.5),
        )
        self.assertEqual(
            (telegram.message_max_chars, telegram.progress_update_interval),
            (4096, 0.8),
        )
        self.assertEqual(
            (discord.message_max_chars, discord.progress_update_interval),
            (2000, 1.0),
        )
        with self.assertRaisesRegex(ValueError, "unsupported channel"):
            limits_for_channel(config, "matrix")

    async def test_personal_runtime_uses_one_boundary_for_files_and_user_cli(self) -> None:
        """Runtime 必须把解析后的 Personal Roots 和 executable PATH 注入同一纵切。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = build_state_paths(root / "state")
            initialized = initialize_state(paths)
            owner_home = root / "owner"
            documents = owner_home / "Documents"
            nvm_bin = owner_home / ".config/nvm/versions/node/v20.19.0/bin"
            documents.mkdir(parents=True)
            nvm_bin.mkdir(parents=True)
            lark_cli = nvm_bin / "lark-cli"
            lark_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            lark_cli.chmod(0o700)
            with paths.config.open("a", encoding="utf-8") as config_file:
                config_file.write(
                    "\n[tools.run_command]\n"
                    'allow_commands = [{ program = "lark-cli", args = ["--version"] }]\n'
                )
            config = load_config(paths)

            with mock.patch("lobster0.runtime.Path.home", return_value=owner_home):
                runtime = create_runtime(config, paths, "test-key")
            try:
                context = runtime.service._tool_context(
                    initialized.owner.id,
                    1,
                    1,
                    DisclosureContext(
                        initialized.owner.id,
                        initialized.owner.id,
                        "cli",
                        "local",
                        True,
                    ),
                )
                self.assertEqual(context.owner_home, owner_home)
                self.assertIn(owner_home, context.read_only_roots)
                self.assertIn(documents, context.write_roots)
            finally:
                await runtime.aclose()

    async def test_automation_lifecycle_is_inert_by_default_and_owned_when_enabled(self) -> None:
        """默认不启动；启用后 Runtime 负责 recovery、Runner、Scheduler 与反向停止。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            base = load_config(paths)
            disabled = create_runtime(base, paths, "test-key")
            try:
                await disabled.astart()
                self.assertFalse(disabled.task_runner.running)
                self.assertFalse(disabled.scheduler.running)
            finally:
                await disabled.aclose()

            enabled_config = replace(
                base,
                automation=replace(base.automation, enabled=True),
                sandbox=replace(
                    base.sandbox,
                    image="example/lobster0@sha256:" + "a" * 64,
                ),
            )
            enabled = create_runtime(enabled_config, paths, "test-key")
            try:
                await enabled.astart()
                self.assertTrue(enabled.task_runner.running)
                self.assertTrue(enabled.scheduler.running)
                with enabled.database.connect_read_only() as connection:
                    events = connection.execute(
                        "SELECT event_type FROM audit_events "
                        "WHERE event_type LIKE 'automation.%' ORDER BY id"
                    ).fetchall()
                self.assertEqual([row["event_type"] for row in events], ["automation.started"])
            finally:
                await enabled.aclose()
            self.assertFalse(enabled.task_runner.running)
            self.assertFalse(enabled.scheduler.running)

    async def test_halted_runtime_recovers_but_starts_no_automation_workers(self) -> None:
        """durable E-stop 在 startup 时必须阻止 Scheduler 和 Runner claim。"""
        with tempfile.TemporaryDirectory() as directory:
            paths = build_state_paths(Path(directory).resolve())
            initialize_state(paths)
            base = load_config(paths)
            config = replace(
                base,
                automation=replace(base.automation, enabled=True),
                sandbox=replace(
                    base.sandbox,
                    image="example/lobster0@sha256:" + "b" * 64,
                ),
            )
            runtime = create_runtime(config, paths, "test-key")
            runtime.automation_control.halt("local test halt")
            try:
                await runtime.astart()
                self.assertFalse(runtime.task_runner.running)
                self.assertFalse(runtime.scheduler.running)
                with runtime.database.connect_read_only() as connection:
                    event = connection.execute(
                        "SELECT event_type, metadata_json FROM audit_events "
                        "WHERE event_type = 'automation.halted'"
                    ).fetchone()
                self.assertIsNotNone(event)
                self.assertNotIn("local test halt", event["metadata_json"])
            finally:
                await runtime.aclose()


if __name__ == "__main__":
    unittest.main()
