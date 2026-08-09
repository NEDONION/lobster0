"""共享 AgentRuntime 的最小装配测试。"""

import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.bootstrap import initialize_state
from miniclaw.config import load_config
from miniclaw.memory.models import DisclosureContext
from miniclaw.paths import build_state_paths
from miniclaw.runtime import create_channel_manager, create_runtime, limits_for_channel


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
                "import sys; import miniclaw.runtime; "
                "raise SystemExit(any(name in sys.modules "
                "for name in ('lark_channel', 'telegram', 'discord')))",
            ],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(imported.returncode, 0, imported.stderr)

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

            with mock.patch("miniclaw.runtime.Path.home", return_value=owner_home):
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


if __name__ == "__main__":
    unittest.main()
