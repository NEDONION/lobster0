"""Sandbox live smoke 参数与稳定报告契约。"""

import argparse
import io
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from miniclaw.sandbox.docker import RootlessClientTransport
from scripts import sandbox_live_smoke


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
        )
        error = io.StringIO()

        with redirect_stderr(error):
            result = await sandbox_live_smoke._run(arguments)

        self.assertEqual(result, 2)
        self.assertIn("--engine is required", error.getvalue())

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
            sandbox_live_smoke._stable_status("seatbelt", False),
            "engine=seatbelt containment=FAIL",
        )


if __name__ == "__main__":
    unittest.main()
