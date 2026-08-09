"""Sandbox live smoke 参数与稳定报告契约。"""

import argparse
import io
import stat
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from miniclaw.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxPlanError
from miniclaw.sandbox.docker import RootlessClientTransport
from scripts import sandbox_live_smoke


class _SuccessfulDockerBackend:
    """模拟外部 daemon 成功执行，同时保留收到的真实 ExecutionPlan。"""

    def __init__(self) -> None:
        """初始化尚未执行的 plan 观察值。"""
        self.plan: ExecutionPlan | None = None

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """写入 containment probe 结果并返回绑定真实 plan hash 的 receipt。"""
        self.plan = plan
        (plan.cwd / "result.txt").write_text("workspace-write-ok", encoding="utf-8")
        return ExecutionReceipt(
            plan_hash=plan.sha256,
            backend="docker",
            exit_code=0,
            signal=None,
            timed_out=False,
            stdout="outside-secret-denied\nnetwork-denied\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            changed_paths=(),
        )


class _PermissionAwareDockerBackend(_SuccessfulDockerBackend):
    """按映射后非 owner UID 的目录权限模拟 rootless workspace write。"""

    def __init__(self) -> None:
        """初始化 workspace 与私有父目录的 mode 观察值。"""
        super().__init__()
        self.parent_mode: int | None = None
        self.workspace_mode: int | None = None

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """仅在 other write/execute 允许固定非 root UID 创建文件时成功。"""
        self.plan = plan
        self.parent_mode = stat.S_IMODE(plan.cwd.parent.stat().st_mode)
        self.workspace_mode = stat.S_IMODE(plan.cwd.stat().st_mode)
        can_create = self.workspace_mode & 0o003 == 0o003
        if can_create:
            (plan.cwd / "result.txt").write_text(
                "workspace-write-ok", encoding="utf-8"
            )
        return ExecutionReceipt(
            plan_hash=plan.sha256,
            backend="docker",
            exit_code=0 if can_create else 1,
            signal=None,
            timed_out=False,
            stdout="outside-secret-denied\nnetwork-denied\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            changed_paths=(),
        )


class _SuccessfulSeatbeltBackend:
    """模拟 Seatbelt 成功，同时保留 v2 executable chain。"""

    def __init__(self) -> None:
        """初始化尚未执行的 plan 观察值。"""
        self.plan: ExecutionPlan | None = None

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """写入探针结果并返回绑定原 Seatbelt plan 的 receipt。"""
        self.plan = plan
        (plan.cwd / "result.txt").write_text("workspace-write-ok", encoding="utf-8")
        return ExecutionReceipt(
            plan_hash=plan.sha256,
            backend="seatbelt",
            exit_code=0,
            signal=None,
            timed_out=False,
            stdout="outside-secret-denied\nnetwork-denied\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            duration_ms=1,
            changed_paths=(),
        )


class SandboxLiveSmokeTest(unittest.IsolatedAsyncioTestCase):
    """验证 live smoke 显式选择 rootless engine 且不泄露本机事实。"""

    async def test_docker_backend_requires_explicit_rootless_engine(self) -> None:
        """Docker live 探针没有 --engine 时必须在发现或执行前拒绝。"""
        arguments = argparse.Namespace(
            backend="docker",
            engine=None,
            confirm_live=True,
            image="example/miniclaw@sha256:" + "a" * 64,
            executable=None,
            probe="python",
        )
        error = io.StringIO()

        with redirect_stderr(error):
            result = await sandbox_live_smoke._run(arguments)

        self.assertEqual(result, 2)
        self.assertIn("--engine is required", error.getvalue())

    async def test_successful_rootless_run_keeps_docker_plan_backend(self) -> None:
        """Rootless engine 只用于发现/报告，immutable plan backend 必须仍为 docker。"""
        arguments = argparse.Namespace(
            backend="docker",
            engine="podman-rootless",
            confirm_live=True,
            image="example/miniclaw@sha256:" + "a" * 64,
            executable=None,
            probe="python",
        )
        backend = _SuccessfulDockerBackend()
        output = io.StringIO()

        with (
            mock.patch(
                "scripts.sandbox_live_smoke._rootless_backend",
                return_value=(backend, "podman-rootless"),
            ),
            redirect_stdout(output),
        ):
            try:
                result = await sandbox_live_smoke._run(arguments)
            except SandboxPlanError as error:
                self.fail(f"rootless smoke constructed an invalid plan: {error.code}")

        self.assertEqual(result, 0)
        self.assertIsNotNone(backend.plan)
        assert backend.plan is not None
        self.assertEqual(backend.plan.backend, "docker")
        self.assertEqual(
            output.getvalue(),
            "engine=podman-rootless containment=PASS\n",
        )

    async def test_rootless_run_prepares_nonroot_workspace_under_private_parent(self) -> None:
        """私有临时根下只给映射后非 owner UID 最小 create/traverse 权限。"""
        arguments = argparse.Namespace(
            backend="docker",
            engine="docker-rootless",
            confirm_live=True,
            image="example/miniclaw@sha256:" + "a" * 64,
            executable=None,
            probe="python",
        )
        backend = _PermissionAwareDockerBackend()
        output = io.StringIO()

        with (
            mock.patch(
                "scripts.sandbox_live_smoke._rootless_backend",
                return_value=(backend, "docker-rootless"),
            ),
            redirect_stdout(output),
        ):
            result = await sandbox_live_smoke._run(arguments)

        self.assertEqual(result, 0)
        self.assertEqual(backend.parent_mode, 0o700)
        self.assertEqual(backend.workspace_mode, 0o703)
        self.assertEqual(
            output.getvalue(),
            "engine=docker-rootless containment=PASS\n",
        )

    def test_rootless_backend_uses_production_discovery_and_stable_identity(self) -> None:
        """Smoke handoff 必须使用生产 transport，并仅返回稳定 engine identity。"""
        private_home = Path("/private/owner")
        private_socket = "/run/user/1001/podman/podman.sock"
        transport = RootlessClientTransport(
            engine="podman-rootless",
            executable=Path("/usr/bin/podman"),
            environment=(
                ("HOME", str(private_home)),
                ("XDG_RUNTIME_DIR", "/run/user/1001"),
                ("CONTAINER_HOST", f"unix://{private_socket}"),
            ),
        )
        arguments = argparse.Namespace(
            backend="docker",
            engine="podman-rootless",
            confirm_live=True,
            image="example/miniclaw@sha256:" + "a" * 64,
            executable=None,
            probe="python",
        )

        with mock.patch(
            "scripts.sandbox_live_smoke.discover_rootless_client_transport",
            return_value=transport,
        ) as discover:
            backend, identity = sandbox_live_smoke._rootless_backend(arguments)

        self.assertEqual(identity, "podman-rootless")
        self.assertEqual(backend.container_engine, "podman-rootless")
        discover.assert_called_once()
        self.assertNotIn(str(private_home), identity)
        self.assertNotIn(private_socket, identity)

    def test_status_output_contains_only_stable_engine_and_containment(self) -> None:
        """Live 结果文本不能包含 UID、路径、exit 或 timeout 等不稳定事实。"""
        self.assertEqual(
            sandbox_live_smoke._stable_status("docker-rootless", True),
            "engine=docker-rootless containment=PASS",
        )
        self.assertEqual(
            sandbox_live_smoke._stable_status("seatbelt", False, "node-chain"),
            "engine=seatbelt probe=node-chain containment=FAIL",
        )

    async def test_node_chain_probe_binds_fixture_env_and_exact_node(self) -> None:
        """node-chain live 路径必须构造真实 env shebang v2 plan。"""
        arguments = argparse.Namespace(
            backend="seatbelt",
            engine=None,
            confirm_live=True,
            image=None,
            executable=None,
            probe="node-chain",
        )
        backend = _SuccessfulSeatbeltBackend()
        output = io.StringIO()

        def node_probe(workspace: Path) -> tuple[str, str]:
            node = workspace / "node"
            node.write_bytes(b"native-node")
            node.chmod(0o700)
            probe = workspace / "probe.js"
            probe.write_text("#!/usr/bin/env node\n", encoding="utf-8")
            probe.chmod(0o700)
            return str(probe), str(workspace)

        with (
            mock.patch("scripts.sandbox_live_smoke.SeatbeltSandbox", return_value=backend),
            mock.patch(
                "scripts.sandbox_live_smoke._seatbelt_node_probe",
                side_effect=node_probe,
            ),
            redirect_stdout(output),
        ):
            result = await sandbox_live_smoke._run(arguments)

        self.assertEqual(result, 0)
        assert backend.plan is not None
        self.assertEqual(backend.plan.schema_version, 2)
        self.assertEqual(
            tuple(ref.path.name for ref in backend.plan.executables),
            ("probe.js", "env", "node"),
        )
        self.assertEqual(
            output.getvalue(),
            "engine=seatbelt probe=node-chain containment=PASS\n",
        )

    async def test_unavailable_node_chain_does_not_fall_back_to_python(self) -> None:
        """找不到 deterministic Node 时必须明确失败，不能借 Python probe 过 Gate。"""
        arguments = argparse.Namespace(
            backend="seatbelt",
            engine=None,
            confirm_live=True,
            image=None,
            executable=None,
            probe="node-chain",
        )
        error = io.StringIO()

        with (
            mock.patch(
                "scripts.sandbox_live_smoke._seatbelt_node_probe",
                side_effect=ValueError("private path"),
            ),
            redirect_stderr(error),
        ):
            result = await sandbox_live_smoke._run(arguments)

        self.assertEqual(result, 3)
        self.assertEqual(error.getvalue(), "seatbelt probe is unavailable\n")
        self.assertNotIn("private path", error.getvalue())


if __name__ == "__main__":
    unittest.main()
