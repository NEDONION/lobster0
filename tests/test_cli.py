"""MiniClaw 命令行入口的行为测试。"""

import contextlib
import io
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.cli import main  # noqa: E402


class CliTest(unittest.TestCase):
    """验证尚未实现业务命令时，CLI 仍提供稳定的帮助和版本入口。"""

    def test_no_arguments_prints_help(self) -> None:
        """无参数启动时应成功打印帮助，避免空白退出或伪装已实现的子命令。"""
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("MiniClaw", output.getvalue())

    def test_version_option_prints_version(self) -> None:
        """版本参数应以成功状态退出，并输出当前可安装包版本。"""
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(SystemExit, "0"):
                main(["--version"])

        self.assertIn("miniclaw 0.1.0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
