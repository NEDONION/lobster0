"""ExecutionPlan、Host backend 与持久绑定的安全契约测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.bootstrap import initialize_state
from miniclaw.paths import build_state_paths
from miniclaw.sandbox.base import (
    ExecutableRef,
    ExecutionPlan,
    ExecutionReceipt,
    SandboxPlanError,
)
from miniclaw.sandbox.host import HostSandbox
from miniclaw.sandbox.repository import ExecutionPlanRepository
from miniclaw.storage.database import Database


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

    def test_v1_canonical_json_remains_byte_compatible(self) -> None:
        """增加 v2 后，历史 v1 JSON 的字段与字节顺序不能变化。"""
        plan = self.plan(
            argv=("/python", "script.py"),
            cwd=Path("/workspace"),
            write_roots=(Path("/workspace"),),
        )

        self.assertEqual(
            plan.canonical_json,
            '{"argv":["/python","script.py"],"backend":"host","cpu_seconds":30,'
            '"cwd":"/workspace","environment_names":["LANG","PATH"],"memory_mib":512,'
            '"network_mode":"none","pids_limit":64,"read_roots":[],"schema_version":1,'
            '"timeout_seconds":30,"write_roots":["/workspace"]}',
        )
        self.assertEqual(
            ExecutionPlan.from_canonical_json(plan.canonical_json).canonical_json,
            plan.canonical_json,
        )

    def test_v2_binds_exact_executable_paths_and_hashes(self) -> None:
        """v2 必须按执行顺序持久化 exact executable path 与摘要。"""
        first = ExecutableRef(Path("/bin/echo"), "a" * 64)
        second = ExecutableRef(Path("/usr/bin/env"), "b" * 64)
        plan = self.plan(
            backend="seatbelt",
            schema_version=2,
            executables=(first, second),
        )

        restored = ExecutionPlan.from_canonical_json(plan.canonical_json)

        self.assertEqual(restored.executables, (first, second))
        self.assertIn(
            '"executables":[{"path":"/bin/echo","sha256":"' + "a" * 64,
            plan.canonical_json,
        )
        self.assertEqual(restored.sha256, plan.sha256)

    def test_v2_rejects_ambiguous_or_unbound_executable_refs(self) -> None:
        """无绑定、重复、过长或不由 Seatbelt 消费的 chain 必须失败关闭。"""
        valid = ExecutableRef(Path("/bin/echo"), "a" * 64)
        with self.assertRaises(SandboxPlanError):
            self.plan(backend="seatbelt", schema_version=2, executables=())
        with self.assertRaises(SandboxPlanError):
            self.plan(executables=(valid,))
        with self.assertRaises(SandboxPlanError):
            self.plan(backend="host", schema_version=2, executables=(valid,))
        with self.assertRaises(SandboxPlanError):
            self.plan(
                backend="seatbelt",
                schema_version=2,
                executables=(valid, valid),
            )
        with self.assertRaises(SandboxPlanError):
            self.plan(
                backend="seatbelt",
                schema_version=2,
                executables=tuple(
                    ExecutableRef(Path(f"/bin/tool-{index}"), f"{index + 1:064x}")
                    for index in range(5)
                ),
            )

    def test_executable_ref_rejects_relative_control_and_bad_hash(self) -> None:
        """ExecutableRef 的 path/hash 输入不能制造歧义或非标准摘要。"""
        invalid = (
            (Path("relative"), "a" * 64),
            (Path("/bin/bad\nname"), "a" * 64),
            (Path("/bin/echo"), "A" * 64),
            (Path("/bin/echo"), "not-sha256"),
        )
        for path, digest in invalid:
            with self.subTest(path=path, digest=digest), self.assertRaises(
                SandboxPlanError
            ):
                ExecutableRef(path, digest)


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
            "print(os.environ.get('MINICLAW_TEST_SECRET', 'missing'))\n",
            encoding="utf-8",
        )
        plan = self.plan((sys.executable, str(helper), "a; echo injected"))
        with mock.patch.dict(
            os.environ, {"MINICLAW_TEST_SECRET": "secret-value"}, clear=False
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
