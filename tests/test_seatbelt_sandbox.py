"""macOS Seatbelt profile 与 availability contract。"""

import sys
import tempfile
import unittest
from pathlib import Path

from miniclaw.sandbox.base import (
    ExecutionPlan,
    SandboxPlanError,
    SandboxUnavailableError,
)
from miniclaw.sandbox.executables import capture_executable_chain
from miniclaw.sandbox.seatbelt import SeatbeltSandbox
from scripts import sandbox_live_smoke


class SeatbeltSandboxTest(unittest.IsolatedAsyncioTestCase):
    """验证 profile 由 canonical paths 生成且 unsupported 平台失败关闭。"""

    def setUp(self) -> None:
        """创建 read/write 分离且默认无网络的 Seatbelt plan。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        root = Path(self.temporary_directory.name).resolve()
        self.workspace = root / "workspace"
        self.read_root = root / "readonly"
        self.workspace.mkdir()
        self.read_root.mkdir()
        self.plan = ExecutionPlan(
            argv=("/bin/echo", "hello"),
            cwd=self.workspace,
            environment_names=("LANG",),
            read_roots=(self.read_root,),
            write_roots=(self.workspace,),
            timeout_seconds=30,
            memory_mib=256,
            cpu_seconds=15,
            pids_limit=64,
            network_mode="none",
            backend="seatbelt",
        )

    def test_profile_is_deny_default_with_literal_roots_and_no_network(self) -> None:
        """profile 必须包含 deny-default、literal executable、subpath roots 与 network deny。"""
        profile = SeatbeltSandbox().build_profile(self.plan)

        self.assertIn("(deny default)", profile)
        self.assertIn('(import "system.sb")', profile)
        self.assertLess(profile.index('(import "system.sb")'), profile.index("(deny network*)"))
        self.assertIn("(deny network*)", profile)
        self.assertIn(f'(literal "{self.plan.argv[0]}")', profile)
        self.assertIn(f'(subpath "{self.read_root}")', profile)
        self.assertIn(f'(subpath "{self.workspace}")', profile)
        self.assertIn(f'(path-ancestors "{self.read_root}")', profile)
        self.assertIn(f'(path-ancestors "{self.workspace}")', profile)
        self.assertNotIn("TOKEN", profile)

    def test_profile_escapes_paths_as_literals(self) -> None:
        """路径中的引号不能闭合 Seatbelt literal 表达式。"""
        quoted = self.workspace.parent / 'write"root'
        quoted.mkdir()
        plan = ExecutionPlan(
            argv=self.plan.argv,
            cwd=quoted,
            environment_names=self.plan.environment_names,
            read_roots=self.plan.read_roots,
            write_roots=(quoted,),
            timeout_seconds=self.plan.timeout_seconds,
            memory_mib=self.plan.memory_mib,
            cpu_seconds=self.plan.cpu_seconds,
            pids_limit=self.plan.pids_limit,
            network_mode="none",
            backend="seatbelt",
        )

        profile = SeatbeltSandbox().build_profile(plan)

        self.assertIn('write\\"root', profile)
        self.assertNotIn(f'(subpath "{quoted}")', profile)

    def test_live_smoke_resolves_python_symlink_before_binding_profile(self) -> None:
        """Live harness 必须和生产 Policy 一样先冻结真实 executable。"""
        resolved = sandbox_live_smoke._seatbelt_probe_executable(sys.executable)
        runtime_root = sandbox_live_smoke._seatbelt_python_runtime_root(resolved)

        self.assertEqual(resolved, str(Path(sys.executable).resolve(strict=True)))
        self.assertEqual(runtime_root, Path(resolved).parent.parent)
        self.assertTrue((runtime_root / "lib").is_dir())

    async def test_unsupported_platform_fails_without_running_command(self) -> None:
        """非 macOS 或 executable 缺失时返回稳定 unavailable。"""
        backend = SeatbeltSandbox(
            platform="linux",
            executable="/usr/bin/sandbox-exec",
        )

        with self.assertRaisesRegex(
            SandboxUnavailableError, "sandbox_backend_unavailable"
        ):
            await backend.execute(self.plan)

    def test_v2_profile_allows_only_bound_literal_chain(self) -> None:
        """v2 profile 必须逐项 literal 放行，不能扩大成 executable subpath。"""
        interpreter = self.workspace / "interpreter"
        interpreter.write_bytes(b"native-interpreter")
        interpreter.chmod(0o700)
        script = self.workspace / "script"
        script.write_text(f"#!{interpreter}\n", encoding="utf-8")
        script.chmod(0o700)
        chain = capture_executable_chain(
            script,
            executable_path=str(self.workspace),
        )
        plan = ExecutionPlan(
            argv=(str(script),),
            cwd=self.workspace,
            environment_names=("LANG",),
            read_roots=(self.read_root,),
            write_roots=(self.workspace,),
            timeout_seconds=30,
            memory_mib=256,
            cpu_seconds=15,
            pids_limit=64,
            network_mode="none",
            backend="seatbelt",
            executables=chain,
            schema_version=2,
        )

        profile = SeatbeltSandbox().build_profile(plan)

        for ref in chain:
            self.assertIn(f'(allow process-exec (literal "{ref.path}"))', profile)
        self.assertNotIn("process-exec (subpath", profile)

    async def test_changed_v2_ref_fails_before_sandbox_exec(self) -> None:
        """执行前 hash 不一致必须稳定拒绝，不能启动 sandbox wrapper。"""
        program = self.workspace / "program"
        program.write_bytes(b"before")
        program.chmod(0o700)
        chain = capture_executable_chain(
            program,
            executable_path=str(self.workspace),
        )
        plan = ExecutionPlan(
            argv=(str(program),),
            cwd=self.workspace,
            environment_names=("LANG",),
            read_roots=(self.read_root,),
            write_roots=(self.workspace,),
            timeout_seconds=30,
            memory_mib=256,
            cpu_seconds=15,
            pids_limit=64,
            network_mode="none",
            backend="seatbelt",
            executables=chain,
            schema_version=2,
        )
        program.write_bytes(b"after")
        backend = SeatbeltSandbox(executable=str(program), platform="darwin")

        with self.assertRaises(SandboxPlanError) as raised:
            await backend.execute(plan)

        self.assertEqual(raised.exception.code, "execution_plan_executable_changed")
        self.assertNotIn(str(self.workspace), str(raised.exception))


if __name__ == "__main__":
    unittest.main()
