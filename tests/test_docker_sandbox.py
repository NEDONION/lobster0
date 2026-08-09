"""Docker sandbox deterministic hardening contract。"""

import tempfile
import unittest
from pathlib import Path

from miniclaw.sandbox.base import ExecutionPlan, SandboxUnavailableError
from miniclaw.sandbox.docker import DockerSandbox


class DockerSandboxTest(unittest.IsolatedAsyncioTestCase):
    """验证 Docker argv 不可被模型注入且缺失时失败关闭。"""

    def setUp(self) -> None:
        """创建仅声明 workspace write mount 的 Docker plan。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name).resolve()
        self.plan = ExecutionPlan(
            argv=("python", "job.py", "a;echo injected"),
            cwd=self.workspace,
            environment_names=("LANG",),
            read_roots=(),
            write_roots=(self.workspace,),
            timeout_seconds=30,
            memory_mib=256,
            cpu_seconds=15,
            pids_limit=128,
            network_mode="none",
            backend="docker",
        )
        self.image = "example/miniclaw@sha256:" + "a" * 64

    def test_argv_has_required_hardening_and_exact_command_boundary(self) -> None:
        """固定 flags、non-root、mount 与 `--` 必须先于 exact command。"""
        argv = DockerSandbox(
            image=self.image,
            docker_executable="/usr/bin/docker",
        ).build_argv(self.plan)

        for subsequence in (
            ("--network", "none"),
            ("--read-only", "--cap-drop", "ALL"),
            ("--security-opt", "no-new-privileges"),
            ("--pids-limit", "128"),
            ("--memory", "256m"),
            ("--user", "65532:65532"),
        ):
            self.assertTrue(_contains_subsequence(argv, subsequence), subsequence)
        boundary = argv.index("--")
        self.assertEqual(argv[boundary + 1], self.image)
        self.assertEqual(argv[-len(self.plan.argv) :], self.plan.argv)
        self.assertIn(
            f"type=bind,src={self.workspace},dst=/workspace,rw",
            argv,
        )

    def test_image_and_plan_constraints_fail_closed(self) -> None:
        """可变 image、allowlisted network 与非 Docker plan 都不能执行。"""
        for image in ("python:latest", "", "name@sha256:short"):
            with self.subTest(image=image), self.assertRaises(ValueError):
                DockerSandbox(image=image)
        changed = ExecutionPlan(
            argv=self.plan.argv,
            cwd=self.plan.cwd,
            environment_names=self.plan.environment_names,
            read_roots=self.plan.read_roots,
            write_roots=self.plan.write_roots,
            timeout_seconds=self.plan.timeout_seconds,
            memory_mib=self.plan.memory_mib,
            cpu_seconds=self.plan.cpu_seconds,
            pids_limit=self.plan.pids_limit,
            network_mode="allowlisted",
            backend="docker",
        )
        with self.assertRaisesRegex(ValueError, "sandbox_network_unsupported"):
            DockerSandbox(image=self.image).build_argv(changed)

    async def test_missing_docker_never_falls_back_to_host(self) -> None:
        """Docker executable 不存在时返回稳定 unavailable，不执行原 argv。"""
        marker = self.workspace / "must-not-exist"
        dangerous = ExecutionPlan(
            argv=("touch", str(marker)),
            cwd=self.plan.cwd,
            environment_names=self.plan.environment_names,
            read_roots=self.plan.read_roots,
            write_roots=self.plan.write_roots,
            timeout_seconds=self.plan.timeout_seconds,
            memory_mib=self.plan.memory_mib,
            cpu_seconds=self.plan.cpu_seconds,
            pids_limit=self.plan.pids_limit,
            network_mode="none",
            backend="docker",
        )
        backend = DockerSandbox(
            image=self.image,
            docker_executable="/definitely/missing/docker",
        )

        with self.assertRaisesRegex(
            SandboxUnavailableError, "sandbox_backend_unavailable"
        ):
            await backend.execute(dangerous)
        self.assertFalse(marker.exists())


def _contains_subsequence(values: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    """判断 expected 是否按连续顺序存在于 values。"""
    length = len(expected)
    return any(values[index : index + length] == expected for index in range(len(values)))


if __name__ == "__main__":
    unittest.main()
