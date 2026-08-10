"""ExecutionPlan、Host backend 与持久绑定的安全契约测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxPlanError
from lobster0.sandbox.host import HostSandbox
from lobster0.sandbox.repository import ExecutionPlanRepository
from lobster0.storage.database import Database


class ExecutionPlanContractTest(unittest.TestCase):
    """验证 Plan 是稳定、无 Secret value 且不能表达 Shell 的值对象。"""

    def setUp(self) -> None:
        """创建 canonical absolute workspace。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name).resolve()

    def plan(self, **overrides: object) -> ExecutionPlan:
        """返回一份可按字段覆盖的合法 Host Plan。"""
        values: dict[str, object] = {
            "argv": (sys.executable, "script.py"),
            "cwd": self.workspace,
            "environment_names": ("PATH", "LANG"),
            "read_roots": (),
            "write_roots": (self.workspace,),
            "timeout_seconds": 30,
            "memory_mib": 512,
            "cpu_seconds": 30,
            "pids_limit": 64,
            "network_mode": "none",
            "backend": "host",
        }
        values.update(overrides)
        return ExecutionPlan(**values)  # type: ignore[arg-type]

    def test_plan_hash_is_stable_across_environment_order(self) -> None:
        """环境名称顺序不同必须 canonicalize 为同一个 hash。"""
        first = self.plan(environment_names=("PATH", "LANG"))
        second = self.plan(environment_names=("LANG", "PATH"))

        self.assertEqual(first.sha256, second.sha256)
        self.assertEqual(first.canonical_json, second.canonical_json)
        self.assertNotIn("secret-value", first.canonical_json)

    def test_empty_non_program_argument_is_preserved_in_exact_argv(self) -> None:
        """lark-cli --query 空字符串合法，但 executable 本身仍必须非空。"""
        plan = self.plan(argv=(sys.executable, "--query", ""))

        self.assertEqual(plan.argv, (sys.executable, "--query", ""))
        self.assertIn('"--query",""', plan.canonical_json)
        with self.assertRaises(SandboxPlanError):
            self.plan(argv=("", "--query"))

    def test_invalid_or_ambiguous_plan_is_unrepresentable(self) -> None:
        """控制字符、相对路径、重复/重叠 mount 与越界资源必须拒绝。"""
        cases = (
            {"argv": ("echo\nleak",)},
            {"cwd": Path("relative")},
            {"environment_names": ("PATH", "PATH")},
            {"environment_names": ("BAD=VALUE",)},
            {"read_roots": (self.workspace,), "write_roots": (self.workspace,)},
            {"write_roots": (Path("relative"),)},
            {"timeout_seconds": 0},
            {"memory_mib": 0},
            {"cpu_seconds": 0},
            {"pids_limit": 0},
            {"network_mode": "open"},
            {"backend": "shell"},
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(SandboxPlanError):
                self.plan(**values)

    def test_round_trip_recomputes_and_verifies_hash(self) -> None:
        """数据库恢复必须重新解析 canonical JSON，不能信任任意 hash。"""
        plan = self.plan()

        restored = ExecutionPlan.from_canonical_json(plan.canonical_json)

        self.assertEqual(restored, plan)
        self.assertEqual(restored.sha256, plan.sha256)


class HostSandboxTest(unittest.IsolatedAsyncioTestCase):
    """验证 Host backend 只执行 exact argv 和受管环境名称。"""

    def setUp(self) -> None:
        """创建临时 workspace 与不含父进程 Secret 的 resolver。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.backend = HostSandbox(
            lambda name: {"PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8"}.get(name)
        )

    def plan(self, argv: tuple[str, ...], *, timeout: int = 3) -> ExecutionPlan:
        """构造受限 Host Plan。"""
        return ExecutionPlan(
            argv=argv,
            cwd=self.workspace,
            environment_names=("PATH", "LANG"),
            read_roots=(),
            write_roots=(self.workspace,),
            timeout_seconds=timeout,
            memory_mib=512,
            cpu_seconds=30,
            pids_limit=64,
            network_mode="none",
            backend="host",
        )

    async def test_executes_exact_argv_without_parent_secret(self) -> None:
        """特殊字符保留为单个 argv，父环境 Secret 不得被继承。"""
        helper = self.workspace / "inspect.py"
        helper.write_text(
            "import os, sys\n"
            "print(repr(sys.argv[1:]))\n"
            "print(os.environ.get('LOBSTER0_TEST_SECRET', 'missing'))\n",
            encoding="utf-8",
        )
        plan = self.plan((sys.executable, str(helper), "a; echo injected"))
        with mock.patch.dict(
            os.environ, {"LOBSTER0_TEST_SECRET": "secret-value"}, clear=False
        ):
            receipt = await self.backend.execute(plan)

        self.assertEqual(receipt.exit_code, 0)
        self.assertIn("['a; echo injected']", receipt.stdout)
        self.assertIn("missing", receipt.stdout)
        self.assertNotIn("secret-value", receipt.canonical_json)
        self.assertEqual(receipt.plan_hash, plan.sha256)

    async def test_timeout_returns_bounded_receipt(self) -> None:
        """超时终止进程组并产生绑定原 plan hash 的 receipt。"""
        helper = self.workspace / "sleep.py"
        helper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

        receipt = await self.backend.execute(
            self.plan((sys.executable, str(helper)), timeout=1)
        )

        self.assertTrue(receipt.timed_out)
        self.assertIsNone(receipt.exit_code)
        self.assertLessEqual(len(receipt.stdout.encode()), 1024 * 1024)


class ExecutionPlanRepositoryTest(unittest.TestCase):
    """验证 plan 与 receipt 的不可变 SQLite 生命周期。"""

    def setUp(self) -> None:
        """初始化 v5 数据库并创建可引用 ToolRun。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialize_state(paths)
        self.database = Database(paths.database)
        self.repository = ExecutionPlanRepository(self.database)
        with self.database.connect() as connection:
            session_id = connection.execute(
                "INSERT INTO sessions (user_id, channel, account_id, external_conversation_id, "
                "status, created_at, updated_at) VALUES "
                "(1, 'cli', 'local', 'sandbox-test', 'active', CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ).lastrowid
            turn_id = connection.execute(
                "INSERT INTO turns (session_id, inbound_event_id, status, model, started_at) "
                "VALUES (?, 'sandbox-event', 'running', 'test', CURRENT_TIMESTAMP)",
                (session_id,),
            ).lastrowid
            self.run_id = int(
                connection.execute(
                    "INSERT INTO tool_runs (turn_id, tool_call_id, tool_name, arguments_json, "
                    "arguments_hash, policy_action, status, created_at) "
                    "VALUES (?, 'call', 'run_command', '{}', 'hash', 'allow', 'running', "
                    "CURRENT_TIMESTAMP)",
                    (turn_id,),
                ).lastrowid
            )
        self.workspace = paths.workspace

    def plan(self, *argv: str) -> ExecutionPlan:
        """返回绑定临时 workspace 的 plan。"""
        return ExecutionPlan(
            argv=argv or (sys.executable,),
            cwd=self.workspace,
            environment_names=("PATH",),
            read_roots=(),
            write_roots=(self.workspace,),
            timeout_seconds=30,
            memory_mib=512,
            cpu_seconds=30,
            pids_limit=64,
            network_mode="none",
            backend="host",
        )

    def test_plan_is_idempotent_but_conflicting_rewrite_fails(self) -> None:
        """同一 plan 可重试写入，任何不同内容不得覆盖。"""
        first = self.plan(sys.executable, "a")
        self.repository.create(self.run_id, first)
        self.repository.create(self.run_id, first)

        self.assertEqual(self.repository.get(self.run_id), first)
        with self.assertRaisesRegex(SandboxPlanError, "execution_plan_mismatch"):
            self.repository.create(self.run_id, self.plan(sys.executable, "b"))

    def test_receipt_is_written_once_and_must_match_plan(self) -> None:
        """终结 receipt 必须绑定 plan hash，且不能被第二份结果替换。"""
        plan = self.plan(sys.executable)
        self.repository.create(self.run_id, plan)
        receipt = ExecutionReceipt(
            plan_hash=plan.sha256,
            backend="host",
            exit_code=0,
            signal=None,
            timed_out=False,
            stdout="ok",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            changed_paths=(),
        )
        self.repository.complete(self.run_id, receipt)
        self.repository.complete(self.run_id, receipt)

        self.assertEqual(self.repository.receipt(self.run_id), receipt)
        changed = ExecutionReceipt(
            plan_hash=plan.sha256,
            backend="host",
            exit_code=1,
            signal=None,
            timed_out=False,
            stdout="",
            stderr="changed",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=2,
            changed_paths=(),
        )
        with self.assertRaisesRegex(SandboxPlanError, "execution_receipt_conflict"):
            self.repository.complete(self.run_id, changed)


if __name__ == "__main__":
    unittest.main()
