"""RunCommandTool 的进程、环境、输出和超时边界测试。"""

import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from lobster0.tools.base import ToolContext, ToolValidationError
from lobster0.tools.command import RunCommandTool, _safe_environment


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
            "print('secret=' + os.environ.get('LOBSTER0_TEST_SECRET', 'missing'))\n"
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
            {"LOBSTER0_TEST_SECRET": "super-secret"},
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

    def test_personal_environment_is_minimal_and_explicit(self) -> None:
        """Personal 子进程只收到固定 PATH/Home/locale 和关闭通知器的变量。"""
        owner_home = self.workspace / "owner"
        owner_home.mkdir()

        with mock.patch.dict(
            os.environ,
            {
                "LOBSTER0_MODEL_API_KEY": "secret",
                "HTTP_PROXY": "http://private",
                "PYTHONPATH": "/private/python",
                "COOKIE": "private-cookie",
            },
            clear=False,
        ):
            environment = _safe_environment("/trusted/bin", owner_home)

        self.assertEqual(
            environment,
            {
                "PATH": "/trusted/bin",
                "HOME": str(owner_home),
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            },
        )

    async def test_discovered_lark_wrapper_finds_node_from_the_same_minimal_path(self) -> None:
        """NVM wrapper 的 env node 必须从发现 PATH 启动，不依赖父 Shell。"""
        executable_root = self.workspace / "nvm-bin"
        executable_root.mkdir()
        node = executable_root / "node"
        node.write_text("#!/bin/sh\nprintf 'lark-cli 1.0.83\\n'\n", encoding="utf-8")
        node.chmod(0o700)
        lark_cli = executable_root / "lark-cli"
        lark_cli.write_text("#!/usr/bin/env node\n", encoding="utf-8")
        lark_cli.chmod(0o700)
        tool = RunCommandTool(executable_path=str(executable_root))

        result = await tool.execute(
            self.context,
            tool.validate({"program": "lark-cli", "args": ["--version"]}),
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["stdout"], "lark-cli 1.0.83\n")
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

    def test_automation_plan_uses_configured_read_only_backend(self) -> None:
        """无人值守命令默认只读 mount，并使用配置 backend/resource limits。"""
        context = ToolContext(
            user_id=1,
            session_id=1,
            turn_id=1,
            state_home=self.context.state_home,
            workspace=self.workspace,
            read_only_roots=(),
            source="automation",
            task_run_id=1,
        )
        tool = RunCommandTool(
            automation_backend="docker",
            sandbox_image="example/lobster0@sha256:" + "a" * 64,
            sandbox_memory_mib=1024,
            sandbox_cpu_seconds=120,
            sandbox_pids_limit=256,
        )

        plan = tool.build_execution_plan(
            context,
            tool.validate({"program": sys.executable, "args": ["script.py"]}),
        )

        self.assertEqual(plan.backend, "docker")
        self.assertEqual(plan.argv[0], Path(plan.argv[0]).name)
        self.assertFalse(Path(plan.argv[0]).is_absolute())
        self.assertEqual(plan.read_roots, (self.workspace,))
        self.assertEqual(plan.write_roots, ())
        self.assertEqual((plan.memory_mib, plan.cpu_seconds, plan.pids_limit), (1024, 120, 256))
        self.assertNotIn("HOME", plan.environment_names)

    async def test_rootless_client_environment_is_host_only_and_not_persisted(self) -> None:
        """rootless client transport 只给引擎进程，不进入 container env/Plan/Receipt。"""
        executable_root = self.workspace / "bin"
        executable_root.mkdir()
        capture = self.workspace / "client-capture.txt"
        docker = executable_root / "docker"
        docker.write_text(
            "#!/bin/sh\n"
            f'printf "%s\\n%s\\n%s\\n" "$HOME" "$XDG_RUNTIME_DIR" "$DOCKER_HOST" > "{capture}"\n'
            f'printf "%s\\n" "$@" >> "{capture}"\n'
            "printf 'client=%s runtime=%s host=%s\\n' "
            '"$HOME" "$XDG_RUNTIME_DIR" "$DOCKER_HOST"\n',
            encoding="utf-8",
        )
        docker.chmod(0o700)
        workload = executable_root / "workload"
        workload.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        workload.chmod(0o700)
        owner_home = self.workspace / "owner"
        runtime = Path("/run/user/1001")
        rootless_socket = runtime / "docker.sock"

        context = ToolContext(
            user_id=1,
            session_id=1,
            turn_id=1,
            state_home=self.context.state_home,
            workspace=self.workspace,
            read_only_roots=(),
            source="automation",
            task_run_id=1,
        )
        tool = RunCommandTool(
            executable_path=str(executable_root),
            owner_home=owner_home,
            automation_backend="docker",
            container_engine="docker-rootless",
            sandbox_image="example/lobster0@sha256:" + "a" * 64,
        )
        plan = tool.build_execution_plan(
            context,
            tool.validate({"program": "workload", "args": ["version"]}),
        )
        facts = {
            runtime: SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=1001),
            rootless_socket: SimpleNamespace(
                st_mode=stat.S_IFSOCK | 0o600, st_uid=1001
            ),
        }

        def fake_lstat(path: Path) -> SimpleNamespace:
            try:
                return facts[path]
            except KeyError:
                raise FileNotFoundError(path) from None

        with (
            mock.patch("lobster0.sandbox.docker.sys.platform", "linux"),
            mock.patch("lobster0.sandbox.docker.os.geteuid", return_value=1001),
            mock.patch("lobster0.sandbox.docker.os.lstat", side_effect=fake_lstat),
        ):
            result, receipt = await tool.execute_plan(context, plan)

        self.assertTrue(result.ok)
        captured = capture.read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            captured[:3],
            [
                str(owner_home),
                "/run/user/1001",
                "unix:///run/user/1001/docker.sock",
            ],
        )
        env_names = [
            captured[index + 1]
            for index, value in enumerate(captured)
            if value == "--env"
        ]
        self.assertNotIn("HOME", env_names)
        self.assertNotIn("XDG_RUNTIME_DIR", env_names)
        self.assertNotIn("DOCKER_HOST", env_names)
        self.assertNotIn("CONTAINER_HOST", env_names)
        for persisted in (plan.canonical_json, receipt.canonical_json):
            self.assertNotIn(str(owner_home), persisted)
            self.assertNotIn("/run/user/1001", persisted)
            self.assertNotIn("DOCKER_HOST", persisted)
            self.assertNotIn("CONTAINER_HOST", persisted)
        self.assertNotIn(str(owner_home), repr(result.data))
        self.assertNotIn("/run/user/1001", repr(result.data))


if __name__ == "__main__":
    unittest.main()
