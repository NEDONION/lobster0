"""MiniClaw 命令行入口的行为测试。"""

import contextlib
import io
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.automation.models import (  # noqa: E402
    DeliveryTarget,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
)
from miniclaw.automation.repository import ScheduledTaskRepository  # noqa: E402
from miniclaw.cli import main  # noqa: E402
from miniclaw.paths import build_state_paths  # noqa: E402
from miniclaw.storage.database import Database  # noqa: E402
from miniclaw.storage.repositories import OwnerRepository  # noqa: E402


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
        """裸 miniclaw 必须进入 TUI，不能打印帮助或跳到另一套聊天循环。"""
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch("miniclaw.cli._is_tui_terminal", return_value=True),
            mock.patch("miniclaw.cli.run_default_tui", return_value=0) as run_tui,
        ):
            exit_code, output, error = run_cli(["--home", directory])

        self.assertEqual((exit_code, output, error), (0, "", ""))
        self.assertEqual(run_tui.call_args.args[0].home, Path(directory).resolve())

    def test_bare_command_rejects_non_interactive_terminal(self) -> None:
        """全屏 TUI 不得在 pipe、CI 或 TERM=dumb 中挂起。"""
        with mock.patch("miniclaw.cli._is_tui_terminal", return_value=False):
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

        self.assertIn("miniclaw 0.7.0", output.getvalue())

    def test_help_lists_tui_gateway_and_maintenance_commands(self) -> None:
        """帮助包含唯一 TUI、gateway 和维护命令，不恢复 chat/approval 分叉。"""
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                main(["--help"])

        help_text = output.getvalue()
        self.assertIn("init", help_text)
        self.assertIn("doctor", help_text)
        self.assertIn("eval", help_text)
        self.assertIn("gateway", help_text)
        self.assertIn("task", help_text)
        self.assertIn("all enabled IM channels", help_text)
        self.assertNotIn("chat", help_text)
        self.assertNotIn("approvals", help_text)

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
                "miniclaw.runtime.create_runtime",
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

            self.assertTrue((Path(directory) / "miniclaw.db").is_file())

        self.assertEqual((first_code, second_code), (0, 0))
        self.assertEqual((first_error, second_error), ("", ""))
        self.assertIn("Initialized MiniClaw", first_output)
        self.assertIn("already initialized", second_output)

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
        """doctor 应输出含 Automation、Memory、三平台与数据库的二十七项 PASS。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            node = Path(directory) / "test-node"
            node.write_text("#!/bin/sh\nprintf 'v22.19.0\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            entry = Path(directory) / "main.js"
            entry.write_text("// test entry\n", encoding="utf-8")

            with mock.patch.dict(
                "os.environ",
                {
                    "MINICLAW_NODE": str(node),
                    "MINICLAW_TUI_ENTRY": str(entry),
                },
                clear=False,
            ):
                exit_code, output, error = run_cli(["doctor", "--home", directory])

        self.assertEqual(exit_code, 0)
        self.assertEqual(error, "")
        self.assertEqual(output.count("[PASS]"), 27)

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
                "MINICLAW_FEISHU_APP_ID=cli_test\n"
                f"MINICLAW_FEISHU_APP_SECRET={secret}\n",
                encoding="utf-8",
            )
            dotenv.chmod(0o600)
            node = root / "test-node"
            node.write_text("#!/bin/sh\nprintf 'v22.19.0\\n'\n", encoding="utf-8")
            node.chmod(0o700)
            entry = root / "main.js"
            entry.write_text("// test entry\n", encoding="utf-8")

            with (
                mock.patch("miniclaw.cli.Path.cwd", return_value=root),
                mock.patch.dict(
                    "miniclaw.cli.os.environ",
                    {
                        "MINICLAW_NODE": str(node),
                        "MINICLAW_TUI_ENTRY": str(entry),
                    },
                    clear=True,
                ),
                mock.patch("miniclaw.doctor.importlib.util.find_spec", return_value=object()),
            ):
                exit_code, output, error = run_cli(
                    ["doctor", "--home", str(state)]
                )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("missing environment", output)
        self.assertNotIn(secret, output + error)

    def test_gateway_command_uses_selected_home_and_stable_exit_codes(self) -> None:
        """gateway 不要求 TTY，并把配置错误映射为 2。"""
        async def successful(paths):
            self.assertEqual(paths.home, Path(directory).resolve())

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch("miniclaw.cli.prepare_gateway_sdk_runtime"),
                mock.patch("miniclaw.cli.run_gateway", side_effect=successful),
            ):
                exit_code, output, error = run_cli(["gateway", "--home", directory])
        self.assertEqual((exit_code, output, error), (0, "", ""))

    def test_gateway_preloads_channel_sdk_before_starting_asyncio(self) -> None:
        """飞书 SDK 必须在主循环启动前加载，避免捕获正在运行的 loop。"""
        events: list[str] = []

        def prepare() -> None:
            events.append("prepare")

        async def successful(paths) -> None:
            del paths
            events.append("run")

        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch(
                    "miniclaw.cli.prepare_gateway_sdk_runtime",
                    create=True,
                    side_effect=prepare,
                ),
                mock.patch("miniclaw.cli.run_gateway", side_effect=successful),
            ):
                exit_code, output, error = run_cli(
                    ["gateway", "--home", directory]
                )

        self.assertEqual((exit_code, output, error), (0, "", ""))
        self.assertEqual(events, ["prepare", "run"])


if __name__ == "__main__":
    unittest.main()
