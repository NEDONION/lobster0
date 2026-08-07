"""RunCommandTool 的进程、环境、输出和超时边界测试。"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.tools.base import ToolContext, ToolValidationError
from miniclaw.tools.command import RunCommandTool


class RunCommandToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 subprocess_exec 不经过 Shell 且资源严格受限。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.context = ToolContext(
            user_id=1,
            session_id=1,
            turn_id=1,
            state_home=self.workspace / "state",
            workspace=self.workspace,
            read_only_roots=(),
        )

    async def test_runs_exact_argv_in_workspace_without_secret_environment(self) -> None:
        """子进程固定 cwd、stdin EOF，并且不继承父进程秘密。"""
        helper = self.workspace / "inspect.py"
        helper.write_text(
            "import os, sys\n"
            "print('cwd=' + os.getcwd())\n"
            "print('secret=' + os.environ.get('MINICLAW_TEST_SECRET', 'missing'))\n"
            "print('stdin=' + str(len(sys.stdin.read())))\n"
            "print('separate-error', file=sys.stderr)\n",
            encoding="utf-8",
        )
        tool = RunCommandTool()
        arguments = tool.validate(
            {"program": sys.executable, "args": [str(helper)]}
        )

        with mock.patch.dict(
            os.environ,
            {"MINICLAW_TEST_SECRET": "super-secret"},
            clear=False,
        ):
            result = await tool.execute(self.context, arguments)

        self.assertTrue(result.ok)
        assert isinstance(result.data, dict)
        self.assertEqual(result.data["cwd"], str(self.workspace))
        self.assertIn("secret=missing", result.data["stdout"])
        self.assertIn("stdin=0", result.data["stdout"])
        self.assertNotIn("super-secret", str(result.data))
        self.assertEqual(result.data["stderr"].strip(), "separate-error")
        self.assertEqual(result.data["exit_code"], 0)

    async def test_stdout_and_stderr_are_independently_bounded(self) -> None:
        """两个流都只能保留 1 MiB，超出部分继续排空但不进入内存结果。"""
        helper = self.workspace / "large.py"
        helper.write_text(
            "import sys\n"
            "sys.stdout.write('o' * (1024 * 1024 + 100))\n"
            "sys.stderr.write('e' * (1024 * 1024 + 200))\n",
            encoding="utf-8",
        )
        tool = RunCommandTool()

        result = await tool.execute(
            self.context,
            tool.validate({"program": sys.executable, "args": [str(helper)]}),
        )

        self.assertTrue(result.ok)
        assert isinstance(result.data, dict)
        self.assertEqual(len(result.data["stdout"].encode()), 1024 * 1024)
        self.assertEqual(len(result.data["stderr"].encode()), 1024 * 1024)
        self.assertIs(result.data["stdout_truncated"], True)
        self.assertIs(result.data["stderr_truncated"], True)

    async def test_timeout_terminates_process_group_with_stable_error(self) -> None:
        """超过预算必须终止新进程组并返回 tool_timeout。"""
        helper = self.workspace / "sleep.py"
        helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
        tool = RunCommandTool(timeout_seconds=1, max_timeout_seconds=2)
        started = time.monotonic()

        result = await tool.execute(
            self.context,
            tool.validate({"program": sys.executable, "args": [str(helper)]}),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "tool_timeout")
        self.assertLess(time.monotonic() - started, 4)

    async def test_background_child_cannot_outlive_the_command_timeout(self) -> None:
        """父进程退出后仍占用管道的后台子进程也必须被整个进程组超时终止。"""
        helper = self.workspace / "background.py"
        helper.write_text(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(3)'])\n",
            encoding="utf-8",
        )
        tool = RunCommandTool(timeout_seconds=1, max_timeout_seconds=2)
        started = time.monotonic()

        result = await tool.execute(
            self.context,
            tool.validate({"program": sys.executable, "args": [str(helper)]}),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "tool_timeout")
        self.assertLess(time.monotonic() - started, 2.5)

    def test_schema_and_validation_exclude_shell_cwd_env_and_large_timeout(self) -> None:
        """模型只能提供结构化 argv 和不超过配置上限的 timeout。"""
        tool = RunCommandTool(timeout_seconds=30, max_timeout_seconds=120)
        properties = tool.definition.parameters["properties"]

        self.assertEqual(set(properties), {"program", "args", "timeout_seconds"})
        for arguments in (
            {"command": "echo hi"},
            {"program": sys.executable, "args": [], "cwd": "/tmp"},
            {"program": sys.executable, "args": [], "env": {"X": "Y"}},
            {"program": sys.executable, "args": [], "timeout_seconds": 121},
            {"program": sys.executable, "args": [1]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(
                ToolValidationError
            ):
                tool.validate(arguments)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
