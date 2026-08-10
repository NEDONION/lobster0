"""macOS Seatbelt profile 与 availability contract。"""

import sys
import tempfile
import unittest
from pathlib import Path

from lobster0.sandbox.base import ExecutionPlan, SandboxUnavailableError
from lobster0.sandbox.seatbelt import SeatbeltSandbox
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


if __name__ == "__main__":
    unittest.main()
