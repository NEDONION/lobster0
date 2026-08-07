"""MiniClaw 命令行入口的行为测试。"""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.cli import main  # noqa: E402


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
            mock.patch("miniclaw.cli.run_tui", return_value=0) as run_tui,
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

        self.assertIn("miniclaw 0.1.0", output.getvalue())

    def test_help_lists_only_tui_maintenance_commands(self) -> None:
        """帮助只保留 init/doctor/eval，不再公开 chat 或 approvals 分叉入口。"""
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                main(["--help"])

        help_text = output.getvalue()
        self.assertIn("init", help_text)
        self.assertIn("doctor", help_text)
        self.assertIn("eval", help_text)
        self.assertNotIn("chat", help_text)
        self.assertNotIn("approvals", help_text)

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
        """doctor 应输出七项 PASS 并以 0 退出。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])

            exit_code, output, error = run_cli(["doctor", "--home", directory])

        self.assertEqual(exit_code, 0)
        self.assertEqual(error, "")
        self.assertEqual(output.count("[PASS]"), 7)

    def test_doctor_returns_two_for_corrupt_config(self) -> None:
        """损坏配置应显示失败项并使用配置错误退出码 2。"""
        with tempfile.TemporaryDirectory() as directory:
            run_cli(["init", "--home", directory])
            Path(directory, "config.toml").write_text("[agent\n", encoding="utf-8")

            exit_code, output, error = run_cli(["doctor", "--home", directory])

        self.assertEqual(exit_code, 2)
        self.assertIn("[FAIL] config", output)
        self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
