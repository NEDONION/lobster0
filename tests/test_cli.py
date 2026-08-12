"""Lobster0 命令行入口的行为测试。"""

import argparse
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from lobster0 import __version__  # noqa: E402
from lobster0.automation.models import (  # noqa: E402
    DeliveryTarget,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
)
from lobster0.automation.repository import ScheduledTaskRepository  # noqa: E402
from lobster0.channels.restart import RESTART_EXIT_CODE  # noqa: E402
from lobster0.cli import build_parser, main  # noqa: E402
from lobster0.config import load_config  # noqa: E402
from lobster0.paths import build_state_paths  # noqa: E402
from lobster0.setup import SetupAnswers, write_fresh_setup  # noqa: E402
from lobster0.storage.database import Database  # noqa: E402
from lobster0.storage.repositories import OwnerRepository  # noqa: E402
from lobster0.web_launcher import WebLaunchError  # noqa: E402


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    """调用真实 CLI main，并返回退出码、标准输出和标准错误。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


class CliTest(unittest.TestCase):
    """验证帮助、版本和已有本地状态命令的稳定行为。"""

    def test_bare_command_enters_the_only_tui_with_selected_home(self) -> None:
        """裸 lobster0 必须进入 TUI，不能打印帮助或跳到另一套聊天循环。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("lobster0.cli._is_tui_terminal", return_value=True),
            mock.patch("lobster0.cli.run_default_tui", return_value=0) as run_tui,
        ):
            exit_code, output, error = run_cli(["--home", directory])

        self.assertEqual((exit_code, output, error), (0, "", ""))
        self.assertEqual(run_tui.call_args.args[0].home, Path(directory).resolve())

    def test_bare_command_rejects_non_interactive_terminal(self) -> None:
        """全屏 TUI 不得在 pipe、CI 或 TERM=dumb 中挂起。"""
        with mock.patch("lobster0.cli._is_tui_terminal", return_value=False):
            exit_code, output, error = run_cli([])

        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("interactive terminal", error)

    def test_version_option_prints_version(self) -> None:
        """版本参数应以成功状态退出，并输出当前可安装包版本。"""
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                main(["--version"])

        self.assertIn("lobster0 0.7.0", output.getvalue())

    def test_help_lists_tui_gateway_and_maintenance_commands(self) -> None:
        """帮助包含唯一 TUI、gateway 和维护命令，不恢复 chat/approval 分叉。"""
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                main(["--help"])

        help_text = output.getvalue()
        self.assertIn("init", help_text)
        self.assertIn("setup", help_text)
        self.assertIn("doctor", help_text)
        self.assertIn("eval", help_text)
        self.assertIn("gateway", help_text)
        self.assertIn("task", help_text)
        self.assertIn("service", help_text)
        self.assertIn("all enabled IM channels", help_text)
        self.assertNotIn("chat", help_text)
        self.assertNotIn("approvals", help_text)

    def test_setup_and_init_expose_no_secret_valued_options(self) -> None:
        """parser 全树不得提供 API Key、Token 或 App Secret argv。"""
        parser = build_parser()
        pending = [parser]
        all_options: set[str] = set()
        commands: dict[str, argparse.ArgumentParser] = {}
        while pending:
            selected = pending.pop()
            for action in selected._actions:
                all_options.update(action.option_strings)
                if isinstance(action, argparse._SubParsersAction):
                    commands.update(action.choices)
                    pending.extend(action.choices.values())

        self.assertTrue({"--api-key", "--token", "--app-secret"}.isdisjoint(all_options))
        for command in ("setup", "init"):
            with self.subTest(command=command):
                options = {
                    option
                    for action in commands[command]._actions
                    for option in action.option_strings
                }
                self.assertEqual(options, {"-h", "--help", "--home", "--sandbox-image"})

    def test_task_commands_are_repository_only_and_redact_private_fields(self) -> None:
        """list/show/run/runs 不加载 Provider，且不输出 Prompt 或 conversation ID。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            paths = build_state_paths(Path(directory).resolve())
            database = Database(paths.database)
            owner = OwnerRepository(database).get_or_create()
            sentinel = "SECRET_SENTINEL"
            task = ScheduledTaskRepository(database).create(
                owner_id=owner.id,
                name="daily report",
                schedule=ScheduleSpec(
                    ScheduleKind.INTERVAL,
                    "3600",
                    "UTC",
                    datetime.now(UTC) + timedelta(hours=1),
                ),
                prompt=f"summarize {sentinel}",
                skill_names=(),
                delivery=DeliveryTarget(
                    "explicit",
                    "feishu",
                    "default",
                    "oc_external_id",
                ),
                policy_profile="automation-default",
                budget=TaskBudget(),
            )
            with mock.patch(
                "lobster0.runtime.create_runtime",
                side_effect=AssertionError("Provider runtime must not load"),
            ) as runtime_factory:
                listed = run_cli(["task", "--home", directory, "list"])
                shown = run_cli(
                    ["task", "--home", directory, "show", str(task.id)]
                )
                started = run_cli(
                    ["task", "--home", directory, "run", str(task.id)]
                )
                runs = run_cli(
                    ["task", "--home", directory, "runs", str(task.id)]
                )

        self.assertTrue(all(result[0] == 0 for result in (listed, shown, started, runs)))
        output = "".join(result[1] for result in (listed, shown, started, runs))
        self.assertIn("daily report", output)
        self.assertIn("status=queued", output)
        self.assertNotIn(sentinel, output)
        self.assertNotIn("oc_external_id", output)
        runtime_factory.assert_not_called()

    def test_task_lifecycle_halt_and_errors_have_stable_exit_codes(self) -> None:
        """pause/resume/cancel 与 E-stop 本地持久化，非法 transition 返回 4。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            paths = build_state_paths(Path(directory).resolve())
            database = Database(paths.database)
            owner = OwnerRepository(database).get_or_create()
            task = ScheduledTaskRepository(database).create(
                owner_id=owner.id,
                name="lifecycle",
                schedule=ScheduleSpec(
                    ScheduleKind.INTERVAL,
                    "3600",
                    "UTC",
                    datetime.now(UTC) + timedelta(hours=1),
                ),
                prompt="safe",
                skill_names=(),
                delivery=DeliveryTarget("none", "none"),
                policy_profile="automation-default",
                budget=TaskBudget(),
            )

            paused = run_cli(["task", "--home", directory, "pause", str(task.id)])
            resumed = run_cli(["task", "--home", directory, "resume", str(task.id)])
            halted = run_cli(
                ["task", "--home", directory, "halt", "--reason", "incident"]
            )
            blocked = run_cli(["task", "--home", directory, "run", str(task.id)])
            active = run_cli(["task", "--home", directory, "unhalt"])
            cancelled = run_cli(["task", "--home", directory, "cancel", str(task.id)])
            invalid = run_cli(["task", "--home", directory, "resume", str(task.id)])
            missing = run_cli(["task", "--home", directory, "show", "999999"])

        self.assertEqual((paused[0], resumed[0], halted[0], active[0], cancelled[0]), (0,) * 5)
        self.assertIn("automation halted", halted[1])
        self.assertIn("automation active", active[1])
        self.assertEqual((blocked[0], invalid[0], missing[0]), (4, 4, 4))
        self.assertIn("automation_halted", blocked[2])

    def test_legacy_chat_tui_and_approval_aliases_are_not_commands(self) -> None:
        """历史 REPL、TUI 别名和审批 CLI 都不能形成第二个人类交互入口。"""
        for command in ("chat", "tui", "approvals"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(SystemExit, "2"):
                    main([command])

    def test_init_creates_state_and_is_repeatable(self) -> None:
        """CLI 重复初始化应成功，并清楚区分首次创建和已有状态。"""
        with tempfile.TemporaryDirectory() as directory:
            first_code, first_output, first_error = run_cli(["init", "--home", directory])
            second_code, second_output, second_error = run_cli(["init", "--home", directory])

            self.assertTrue((Path(directory) / "lobster0.db").is_file())

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual((first_error, second_error), ("", ""))
        self.assertIn("Initialized Lobster0", first_output)
        self.assertIn("already initialized", second_output)

    def test_setup_dispatches_only_home_and_digest_without_secret_output(self) -> None:
        """CLI setup 只转交 state home 与非 Secret image digest。"""
        pinned = "ghcr.io/nedonion/lobster0-sandbox@sha256:" + "a" * 64
        fake_result = mock.Mock()
        fake_result.owner.id = 7
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "lobster0.cli.run_interactive_setup", return_value=fake_result
            ) as interactive,
        ):
            exit_code, output, error = run_cli(
                ["setup", "--home", directory, "--sandbox-image", pinned]
            )

        self.assertEqual((exit_code, error), (0, ""))
        self.assertIn("Configured Lobster0", output)
        self.assertNotIn("key", output.casefold())
        interactive.assert_called_once()
        call = interactive.call_args
        self.assertEqual(call.args[0].home, Path(directory).resolve())
        self.assertEqual(call.kwargs, {"sandbox_image": pinned})

    def test_setup_rejects_lexical_symlink_home_before_resolution(self) -> None:
        """CLI setup 不得先把 existing symlink home 解引用成可写 target。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "target"
            target.mkdir(mode=0o700)
            link = root / "state"
            link.symlink_to(target, target_is_directory=True)

            def setup_without_tty(paths, *, sandbox_image):
                return write_fresh_setup(
                    paths,
                    SetupAnswers.defaults(),
                    {"LOBSTER0_MODEL_API_KEY": "test-only-key"},
                    sandbox_image,
                )

            with mock.patch(
                "lobster0.cli.run_interactive_setup",
                side_effect=setup_without_tty,
            ):
                exit_code, output, error = run_cli(["setup", "--home", str(link)])

            self.assertEqual(exit_code, 2)
            self.assertEqual(output, "")
            self.assertIn("symbolic link", error)
            self.assertEqual(tuple(target.iterdir()), ())

    def test_init_accepts_digest_pinned_sandbox_image_and_stays_idempotent(self) -> None:
        """init 的唯一新增值参数应写入 pin，重复执行仍不覆盖配置。"""
        pinned = "ghcr.io/nedonion/lobster0-sandbox@sha256:" + "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            first = run_cli(["init", "--home", directory, "--sandbox-image", pinned])
            second = run_cli(["init", "--home", directory, "--sandbox-image", pinned])
            config = load_config(build_state_paths(Path(directory).resolve()), {})

        self.assertEqual((first[0], second[0]), (0, 0))
        self.assertEqual(config.sandbox.image, pinned)
        self.assertIn("already initialized", second[1])

    def test_init_rejects_relative_home_with_exit_code_two(self) -> None:
        """CLI 必须把相对状态目录分类为可操作的配置错误。"""
        exit_code, output, error = run_cli(["init", "--home", "relative"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("absolute path", error)

    def test_init_returns_two_for_existing_corrupt_config(self) -> None:
        """重复 init 遇到损坏配置时应安全失败且不打印 traceback。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            Path(directory, "config.toml").write_text("[agent\n", encoding="utf-8")

            exit_code, output, error = run_cli(["init", "--home", directory])

        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("invalid TOML", error)

    def test_doctor_reports_healthy_initialized_state(self) -> None:
        """doctor 应输出含 Browser、Automation、Memory、三平台与数据库的二十八项 PASS。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            node = Path(directory) / "test-node"
            node.write_text("#!/bin/sh\nprintf 'v22.22.3\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            entry = Path(directory) / "main.js"
            entry.write_text("// test entry\n", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {
                    "LOBSTER0_NODE": str(node),
                    "LOBSTER0_TUI_ENTRY": str(entry),
                },
                clear=False,
            ):
                exit_code, output, error = run_cli(["doctor", "--home", directory])

        self.assertEqual(exit_code, 0)
        self.assertEqual(error, "")
        self.assertEqual(output.count("[PASS]"), 28)

    def test_doctor_returns_two_for_corrupt_config(self) -> None:
        """损坏配置应显示失败项并使用配置错误退出码 2。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            Path(directory, "config.toml").write_text("[agent\n", encoding="utf-8")

            exit_code, output, error = run_cli(["doctor", "--home", directory])

        self.assertEqual(exit_code, 2)
        self.assertIn("[FAIL] config", output)
        self.assertEqual(error, "")

    def test_doctor_loads_private_dotenv_for_feishu_runtime_check(self) -> None:
        """doctor 应与 gateway 一样读取当前目录的私密 .env，但不回显值。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            run_cli(["init", "--home", str(state)])
            with (state / "config.toml").open("a", encoding="utf-8") as config_file:
                config_file.write(
                    "\n[channels.feishu]\n"
                    "enabled = true\n"
                    'owner_open_id = "ou_owner"\n'
                    'allowed_open_ids = ["ou_owner"]\n'
                )
            secret = "doctor-dotenv-secret"
            dotenv = root / ".env"
            dotenv.write_text(
                "LOBSTER0_FEISHU_APP_ID=cli_test\n"
                f"LOBSTER0_FEISHU_APP_SECRET={secret}\n",
                encoding="utf-8",
            )
            dotenv.chmod(0o600)
            node = root / "test-node"
            node.write_text("#!/bin/sh\nprintf 'v22.22.3\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            entry = root / "main.js"
            entry.write_text("// test entry\n", encoding="utf-8")

            with (
                mock.patch("lobster0.env.Path.cwd", return_value=root),
                mock.patch.dict(
                    "lobster0.cli.os.environ",
                    {
                        "LOBSTER0_NODE": str(node),
                        "LOBSTER0_TUI_ENTRY": str(entry),
                    },
                    clear=True,
                ),
                mock.patch("lobster0.doctor.importlib.util.find_spec", return_value=object()),
            ):
                exit_code, output, error = run_cli(
                    ["doctor", "--home", str(state)]
                )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("missing environment", output)
        self.assertNotIn(secret, output + error)

    def test_doctor_loads_installed_secret_file_without_echoing_it(self) -> None:
        """doctor 应只加载显式安装态 Secret 文件，且不向诊断输出泄露凭据。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            run_cli(["init", "--home", str(state)])
            with (state / "config.toml").open("a", encoding="utf-8") as config_file:
                config_file.write(
                    "\n[channels.feishu]\n"
                    "enabled = true\n"
                    'owner_open_id = "ou_owner"\n'
                    'allowed_open_ids = ["ou_owner"]\n'
                )
            secret = "doctor-installed-secret"
            dotenv = state / "secrets.env"
            dotenv.write_text(
                "LOBSTER0_FEISHU_APP_ID=cli_test\n"
                f"LOBSTER0_FEISHU_APP_SECRET={secret}\n",
                encoding="utf-8",
            )
            dotenv.chmod(0o600)
            node = root / "test-node"
            node.write_text("#!/bin/sh\nprintf 'v22.22.3\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            entry = root / "main.js"
            entry.write_text("// test entry\n", encoding="utf-8")

            with (
                mock.patch.dict(
                    "lobster0.cli.os.environ",
                    {
                        "LOBSTER0_ENV_FILE": str(dotenv),
                        "LOBSTER0_NODE": str(node),
                        "LOBSTER0_TUI_ENTRY": str(entry),
                    },
                    clear=True,
                ),
                mock.patch("lobster0.doctor.importlib.util.find_spec", return_value=object()),
            ):
                exit_code, output, error = run_cli(["doctor", "--home", str(state)])

        self.assertEqual(exit_code, 0)
        self.assertNotIn("missing environment", output)
        self.assertNotIn(secret, output + error)

    def test_gateway_command_uses_selected_home_and_stable_exit_codes(self) -> None:
        """gateway 不要求 TTY，并把配置错误映射为 2。"""
        async def successful(paths):
            self.assertEqual(paths.home, Path(directory).resolve())
            return 0

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch("lobster0.cli.prepare_gateway_sdk_runtime"),
                mock.patch("lobster0.cli.run_gateway", side_effect=successful),
            ):
                exit_code, output, error = run_cli(["gateway", "--home", directory])
        self.assertEqual((exit_code, output, error), (0, "", ""))

    def test_gateway_command_propagates_the_restart_exit_code(self) -> None:
        """Owner 的 /restart 要求进程非零退出，CLI 不能把它压成 0。"""
        async def restarting(paths):
            del paths
            return RESTART_EXIT_CODE

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch("lobster0.cli.prepare_gateway_sdk_runtime"),
                mock.patch("lobster0.cli.run_gateway", side_effect=restarting),
            ):
                exit_code, output, error = run_cli(["gateway", "--home", directory])
        self.assertEqual((exit_code, output, error), (RESTART_EXIT_CODE, "", ""))

    def test_service_update_and_uninstall_dispatch_outside_agent_runtime(self) -> None:
        """公共 lifecycle 命令只把 typed action 交给安装模块，不加载 Agent runtime。"""
        with tempfile.TemporaryDirectory() as directory:
            for argv, action in (
                (["service", "--home", directory, "status"], "service.status"),
                (["service", "--home", directory, "logs"], "service.logs"),
                (["update", "--home", directory], "update"),
                (["uninstall", "--home", directory], "uninstall"),
            ):
                with (
                    self.subTest(argv=argv),
                    mock.patch("lobster0.cli.run_install_action", return_value=0) as run,
                    mock.patch(
                        "lobster0.cli.resolve_install_facts",
                        return_value=mock.Mock(managed=True),
                    ),
                    mock.patch(
                        "lobster0.runtime.create_runtime",
                        side_effect=AssertionError("Agent runtime must not load"),
                    ),
                ):
                    self.assertEqual(main(argv), 0)
                    self.assertEqual(run.call_args.args[0], action)

    def test_service_falls_back_to_source_checkout_launchagent(self) -> None:
        """没有 receipt 的源码 checkout 必须保持 Phase 6 LaunchAgent 行为。"""
        service = mock.Mock()
        service.restart = mock.Mock(return_value=None)
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("lobster0.cli.run_install_action") as managed,
            mock.patch("lobster0.cli._launchd_service", return_value=service),
        ):
            exit_code, output, error = run_cli(["service", "--home", directory, "restart"])

        self.assertEqual((exit_code, error), (0, ""))
        self.assertIn("service restarted", output)
        managed.assert_not_called()

    def test_service_logs_is_unavailable_in_source_checkout(self) -> None:
        """logs 只对受管安装有意义，源码模式必须干净失败。"""
        with tempfile.TemporaryDirectory() as directory:
            exit_code, output, error = run_cli(["service", "--home", directory, "logs"])

        self.assertEqual(exit_code, 2)
        self.assertEqual(output, "")
        self.assertIn("service_logs_unavailable", error)

    def test_lifecycle_help_exposes_no_secret_bearing_flags(self) -> None:
        """service/update/uninstall 帮助不得出现任何 Secret 值参数。"""
        parser = build_parser()
        commands = next(
            action.choices
            for action in parser._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        for name in ("service", "update", "uninstall", "install-smoke"):
            with self.subTest(command=name):
                options = {
                    option
                    for action in commands[name]._actions
                    for option in action.option_strings
                }
                self.assertTrue(
                    {
                        "--api-key",
                        "--token",
                        "--app-secret",
                        "--secret",
                        "--secrets-file",
                        "--password",
                    }.isdisjoint(options)
                )
        uninstall_options = {
            option
            for action in commands["uninstall"]._actions
            for option in action.option_strings
        }
        self.assertIn("--purge-data", uninstall_options)
        self.assertIn("--yes-i-understand-data-loss", uninstall_options)

    def test_uninstall_maps_the_exact_long_confirmation_flag(self) -> None:
        """`--yes-i-understand-data-loss` 必须是唯一的非交互破坏性确认。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("lobster0.cli.run_install_action", return_value=0) as run,
            mock.patch(
                "lobster0.cli.resolve_install_facts",
                return_value=mock.Mock(managed=True),
            ),
        ):
            exit_code, _output, _error = run_cli(
                [
                    "uninstall",
                    "--home",
                    directory,
                    "--purge-data",
                    "--yes-i-understand-data-loss",
                ]
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(run.call_args.kwargs["purge_data"])
        self.assertTrue(run.call_args.kwargs["confirm_data_loss"])

    def test_install_smoke_emits_one_json_document_without_network(self) -> None:
        """install-smoke 必须输出 status/version 且不触碰 Provider、Channel 或网络。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            node = Path(directory) / "test-node"
            node.write_text("#!/bin/sh\nprintf 'v22.22.3\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            entry = Path(directory) / "main.js"
            entry.write_text("// test entry\n", encoding="utf-8")
            with (
                mock.patch.dict(
                    "os.environ",
                    {
                        "LOBSTER0_NODE": str(node),
                        "LOBSTER0_TUI_ENTRY": str(entry),
                    },
                    clear=False,
                ),
                mock.patch(
                    "lobster0.runtime.create_runtime",
                    side_effect=AssertionError("Provider runtime must not load"),
                ),
                mock.patch(
                    "socket.socket",
                    side_effect=AssertionError("install-smoke must not open sockets"),
                ),
                mock.patch(
                    "lobster0.cli.importlib.util.find_spec",
                    return_value=object(),
                ),
            ):
                exit_code, output, error = run_cli(
                    ["install-smoke", "--home", directory, "--json"]
                )

        self.assertEqual((exit_code, error), (0, ""))
        document = json.loads(output)
        self.assertEqual(document["status"], "ok")
        self.assertEqual(document["version"], __version__)

    def test_gateway_preloads_channel_sdk_before_starting_asyncio(self) -> None:
        """飞书 SDK 必须在主循环启动前加载，避免捕获正在运行的 loop。"""
        events: list[str] = []

        def prepare() -> None:
            events.append("prepare")

        async def successful(paths) -> int:
            del paths
            events.append("run")
            return 0

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch(
                    "lobster0.cli.prepare_gateway_sdk_runtime",
                    create=True,
                    side_effect=prepare,
                ),
                mock.patch("lobster0.cli.run_gateway", side_effect=successful),
            ):
                exit_code, output, error = run_cli(
                    ["gateway", "--home", directory]
                )

        self.assertEqual((exit_code, output, error), (0, "", ""))
        self.assertEqual(events, ["prepare", "run"])

    def test_web_command_defaults_to_loopback_and_needs_no_flags(self) -> None:
        """裸 lobster0 web 必须绑回环，且不向 launcher 传任何显式 host。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("lobster0.cli.run_web_console", return_value=0) as run_web,
        ):
            exit_code, output, error = run_cli(["web", "--home", directory])

        self.assertEqual((exit_code, output, error), (0, "", ""))
        self.assertEqual(run_web.call_args.kwargs["host"], None)
        self.assertEqual(run_web.call_args.kwargs["port"], None)

    def test_web_command_forwards_an_explicit_bind(self) -> None:
        """--host/--port 原样交给 launcher 判定，CLI 不自己放宽绑定。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("lobster0.cli.run_web_console", return_value=0) as run_web,
        ):
            run_cli(["web", "--home", directory, "--host", "0.0.0.0", "--port", "8080"])

        self.assertEqual(run_web.call_args.kwargs["host"], "0.0.0.0")
        self.assertEqual(run_web.call_args.kwargs["port"], 8080)

    def test_web_command_has_no_token_flag(self) -> None:
        """token 只能走环境变量；命令行参数会被同机其他用户从 ps 读到。"""
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(SystemExit):
                run_cli(["web", "--home", directory, "--token", "t" * 32])

    def test_web_command_maps_a_refused_bind_to_a_configuration_exit_code(self) -> None:
        """缺 token 的非回环绑定必须以配置错误码 2 退出，并打印原因。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch(
                "lobster0.cli.run_web_console",
                side_effect=WebLaunchError("需要 LOBSTER0_WEB_TOKEN"),
            ),
        ):
            exit_code, _, error = run_cli(["web", "--home", directory, "--host", "0.0.0.0"])

        self.assertEqual(exit_code, 2)
        self.assertIn("LOBSTER0_WEB_TOKEN", error)

    def test_web_command_does_not_require_a_terminal(self) -> None:
        """Web 控制台由浏览器交互，不该继承 TUI 的 TTY 前置条件。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("lobster0.cli._is_tui_terminal", return_value=False),
            mock.patch("lobster0.cli.run_web_console", return_value=0),
        ):
            exit_code, _, _ = run_cli(["web", "--home", directory])

        self.assertEqual(exit_code, 0)


class _TerminalStdin(io.StringIO):
    """isatty 恒为真的 stdin 替身；本身不提供任何 Secret 输入。"""

    def isatty(self) -> bool:
        """声明当前进程连着交互终端。"""
        return True


class _RecordingTty:
    """记录全部终端回显的最小双工 TTY fake。"""

    def __init__(self) -> None:
        """准备一个空的回显缓冲。"""
        self.output = io.StringIO()

    def __enter__(self) -> "_RecordingTty":
        """把 fake 作为 context manager 返回。"""
        return self

    def __exit__(self, *arguments: object) -> None:
        """保持缓冲可读，不吞掉异常。"""

    def write(self, value: str) -> int:
        """记录不含 Secret 的提示文本。"""
        return self.output.write(value)

    def flush(self) -> None:
        """匹配真实 TTY 的 flush 接口。"""


class SecretCommandTest(unittest.TestCase):
    """`lobster0 secret` 必须能在不销毁状态的前提下更新存量实例的密钥。"""

    def setUp(self) -> None:
        """准备一个已经配置好、带真实状态的存量实例。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.home = Path(self.temporary_directory.name) / "state"
        self.paths = build_state_paths(self.home)
        write_fresh_setup(
            self.paths,
            SetupAnswers(True, "ou_owner", False, None, False, None),
            {
                "LOBSTER0_MODEL_API_KEY": "old-model-key",
                "LOBSTER0_FEISHU_APP_ID": "cli_app",
                "LOBSTER0_FEISHU_APP_SECRET": "old-app-secret",
            },
            sandbox_image="ghcr.io/nedonion/lobster0-sandbox@sha256:" + "a" * 64,
        )

    def _run_secret_set(self, name: str, values: list[str]) -> tuple[int, str, str]:
        """在受控终端上执行一次 `secret set`。"""
        with (
            mock.patch("sys.stdin", new=_TerminalStdin()),
            mock.patch("lobster0.setup._open_tty", return_value=_RecordingTty()),
            mock.patch("lobster0.setup.getpass.getpass", side_effect=values),
        ):
            return run_cli(["secret", "--home", str(self.home), "set", name])

    def test_updates_a_configured_install_without_destroying_state(self) -> None:
        """存量实例改一个密钥，不需要 rm -rf，其余状态与其他密钥全部保留。"""
        config_before = self.paths.config.read_bytes()
        database_before = self.paths.database.read_bytes()

        exit_code, output, error = self._run_secret_set(
            "LOBSTER0_FEISHU_APP_SECRET", ["rotated-app-secret", "rotated-app-secret"]
        )

        self.assertEqual((exit_code, error), (0, ""))
        self.assertIn("LOBSTER0_FEISHU_APP_SECRET", output)
        self.assertNotIn("rotated-app-secret", output)
        self.assertEqual(
            self.paths.secrets_file.read_text(encoding="utf-8"),
            "LOBSTER0_MODEL_API_KEY=old-model-key\n"
            "LOBSTER0_FEISHU_APP_ID=cli_app\n"
            "LOBSTER0_FEISHU_APP_SECRET=rotated-app-secret\n",
        )
        self.assertEqual(stat.S_IMODE(self.paths.secrets_file.stat().st_mode), 0o600)
        self.assertEqual(self.paths.config.read_bytes(), config_before)
        self.assertEqual(self.paths.database.read_bytes(), database_before)

    def test_rejects_an_unknown_name_with_a_configuration_exit_code(self) -> None:
        """打错的变量名必须以退出码 2 拒绝，并提示合法名单。"""
        original = self.paths.secrets_file.read_text(encoding="utf-8")

        exit_code, output, error = self._run_secret_set(
            "LOBSTER0_FEISHU_APP_SECRETT", ["never-read", "never-read"]
        )

        self.assertEqual(exit_code, 2)
        self.assertIn("LOBSTER0_FEISHU_APP_SECRET", error)
        self.assertEqual(output, "")
        self.assertEqual(self.paths.secrets_file.read_text(encoding="utf-8"), original)

    def test_fails_closed_when_stdin_is_not_a_terminal(self) -> None:
        """stdin 是管道时以退出码 2 失败，绝不从管道读取 Secret。"""
        original = self.paths.secrets_file.read_text(encoding="utf-8")

        with (
            mock.patch("sys.stdin", new=io.StringIO("piped-secret\npiped-secret\n")),
            mock.patch("lobster0.setup.getpass.getpass") as hidden,
        ):
            exit_code, output, error = run_cli(
                ["secret", "--home", str(self.home), "set", "LOBSTER0_MODEL_API_KEY"]
            )

        self.assertEqual(exit_code, 2)
        self.assertIn("interactive terminal", error)
        self.assertEqual((output, hidden.call_count), ("", 0))
        self.assertEqual(self.paths.secrets_file.read_text(encoding="utf-8"), original)

    def test_list_reports_names_and_state_without_any_value(self) -> None:
        """列表只回答哪些变量已设置，不得输出任何值。"""
        exit_code, output, error = run_cli(["secret", "--home", str(self.home), "list"])

        self.assertEqual((exit_code, error), (0, ""))
        self.assertIn("[SET] LOBSTER0_MODEL_API_KEY", output)
        self.assertIn("[SET] LOBSTER0_FEISHU_APP_SECRET", output)
        self.assertIn("[UNSET] LOBSTER0_TELEGRAM_BOT_TOKEN", output)
        self.assertNotIn("old-model-key", output)
        self.assertNotIn("old-app-secret", output)
        self.assertNotIn("cli_app", output)

    def test_secret_command_exposes_no_value_bearing_option(self) -> None:
        """`secret` 族只接受变量名，任何承载值的 flag 都不得存在。"""
        parser = build_parser()
        commands = next(
            action.choices
            for action in parser._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        self.assertIn("secret", commands)
        secret_actions = next(
            action.choices
            for action in commands["secret"]._actions
            if hasattr(action, "choices") and isinstance(action.choices, dict)
        )
        options = {
            option
            for parsers in (commands["secret"], *secret_actions.values())
            for action in parsers._actions
            for option in action.option_strings
        }
        self.assertEqual(options, {"-h", "--help", "--home"})

    def test_doctor_reflects_a_secret_updated_through_the_command(self) -> None:
        """更新后的密钥必须被 doctor 读到：由 FAIL 变 PASS，且不回显值。"""
        self.paths.secrets_file.write_text(
            "LOBSTER0_MODEL_API_KEY=old-model-key\nLOBSTER0_FEISHU_APP_ID=cli_app\n",
            encoding="utf-8",
        )
        self.paths.secrets_file.chmod(0o600)
        secret = "doctor-visible-rotated-secret"
        environment = {"PATH": os.environ.get("PATH", "")}

        with (
            mock.patch.dict("lobster0.cli.os.environ", environment, clear=True),
            mock.patch("lobster0.doctor.importlib.util.find_spec", return_value=object()),
        ):
            _, before, _ = run_cli(["doctor", "--home", str(self.home)])
            self._run_secret_set("LOBSTER0_FEISHU_APP_SECRET", [secret, secret])
            _, after, _ = run_cli(["doctor", "--home", str(self.home)])

        self.assertIn("[FAIL] feishu_runtime", before)
        self.assertIn("LOBSTER0_FEISHU_APP_SECRET", before)
        self.assertIn("[PASS] feishu_runtime", after)
        self.assertNotIn(secret, before + after)


if __name__ == "__main__":
    unittest.main()
