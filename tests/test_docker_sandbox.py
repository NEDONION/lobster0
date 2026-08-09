"""Docker sandbox deterministic hardening contract。"""

import stat
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

from miniclaw.sandbox import docker as docker_module
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
            f"type=bind,src={self.workspace},dst=/workspace",
            argv,
        )
        self.assertNotIn(
            f"type=bind,src={self.workspace},dst=/workspace,rw",
            argv,
        )

        read_only = ExecutionPlan(
            argv=self.plan.argv,
            cwd=self.plan.cwd,
            environment_names=self.plan.environment_names,
            read_roots=(self.workspace,),
            write_roots=(),
            timeout_seconds=self.plan.timeout_seconds,
            memory_mib=self.plan.memory_mib,
            cpu_seconds=self.plan.cpu_seconds,
            pids_limit=self.plan.pids_limit,
            network_mode="none",
            backend="docker",
        )
        read_argv = DockerSandbox(
            image=self.image,
            docker_executable="/usr/bin/docker",
        ).build_argv(read_only)
        self.assertIn(
            f"type=bind,src={self.workspace},dst=/workspace,readonly",
            read_argv,
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

    def test_linux_rootless_transport_is_engine_specific_and_core_derived(self) -> None:
        """Docker/Podman 只能使用当前非 root UID 的固定 rootless socket。"""
        executable_root = self.workspace / "bin"
        executable_root.mkdir()
        executables = {}
        for name in ("docker", "podman"):
            executable = executable_root / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
            executables[name] = executable

        facts = {
            Path("/run/user/1001"): _fact(stat.S_IFDIR | 0o700, 1001),
            Path("/run/user/1001/docker.sock"): _fact(stat.S_IFSOCK | 0o600, 1001),
            Path("/run/user/1001/podman"): _fact(stat.S_IFDIR | 0o700, 1001),
            Path("/run/user/1001/podman/podman.sock"): _fact(
                stat.S_IFSOCK | 0o600, 1001
            ),
        }

        docker = docker_module.discover_rootless_client_transport(
            "docker-rootless",
            str(executable_root),
            self.workspace / "owner",
            platform_name="linux",
            effective_uid=1001,
            which=lambda name, *, path: str(executables[name]),
            lstat=_fake_lstat(facts),
        )
        podman = docker_module.discover_rootless_client_transport(
            "podman-rootless",
            str(executable_root),
            self.workspace / "owner",
            platform_name="linux",
            effective_uid=1001,
            which=lambda name, *, path: str(executables[name]),
            lstat=_fake_lstat(facts),
        )

        self.assertEqual(docker.executable, executables["docker"])
        self.assertEqual(
            dict(docker.environment),
            {
                "HOME": str(self.workspace / "owner"),
                "XDG_RUNTIME_DIR": "/run/user/1001",
                "DOCKER_HOST": "unix:///run/user/1001/docker.sock",
            },
        )
        self.assertEqual(podman.executable, executables["podman"])
        self.assertEqual(
            dict(podman.environment),
            {
                "HOME": str(self.workspace / "owner"),
                "XDG_RUNTIME_DIR": "/run/user/1001",
                "CONTAINER_HOST": "unix:///run/user/1001/podman/podman.sock",
            },
        )

    def test_rootless_transport_rejects_root_mismatch_and_unsafe_filesystem(self) -> None:
        """UID 0、错误 executable 和不安全 runtime/socket 必须失败关闭。"""
        executable_root = self.workspace / "bin"
        executable_root.mkdir()
        docker = executable_root / "docker"
        podman = executable_root / "podman"
        for executable in (docker, podman):
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
        runtime = Path("/run/user/1001")
        socket = runtime / "docker.sock"
        safe = {
            runtime: _fact(stat.S_IFDIR | 0o700, 1001),
            socket: _fact(stat.S_IFSOCK | 0o600, 1001),
        }

        invalid_cases = (
            (0, "docker-rootless", safe, lambda name, *, path: str(docker)),
            (
                1001,
                "docker-rootless",
                safe,
                lambda name, *, path: str(podman),
            ),
            (
                1001,
                "docker-rootless",
                {**safe, runtime: _fact(stat.S_IFLNK | 0o777, 1001)},
                lambda name, *, path: str(docker),
            ),
            (
                1001,
                "docker-rootless",
                {**safe, runtime: _fact(stat.S_IFDIR | 0o700, 2002)},
                lambda name, *, path: str(docker),
            ),
            (
                1001,
                "docker-rootless",
                {**safe, socket: _fact(stat.S_IFREG | 0o600, 1001)},
                lambda name, *, path: str(docker),
            ),
            (
                1001,
                "docker-rootless",
                {**safe, socket: _fact(stat.S_IFLNK | 0o777, 1001)},
                lambda name, *, path: str(docker),
            ),
            (
                1001,
                "docker-rootless",
                {**safe, socket: _fact(stat.S_IFSOCK | 0o600, 2002)},
                lambda name, *, path: str(docker),
            ),
        )
        for uid, engine, facts, which in invalid_cases:
            with self.subTest(uid=uid, engine=engine, facts=facts), self.assertRaises(
                SandboxUnavailableError
            ):
                docker_module.discover_rootless_client_transport(
                    engine,
                    str(executable_root),
                    self.workspace / "owner",
                    platform_name="linux",
                    effective_uid=uid,
                    which=which,
                    lstat=_fake_lstat(facts),
                )

        var_run_only = {
            runtime: _fact(stat.S_IFDIR | 0o700, 1001),
            Path("/var/run/docker.sock"): _fact(stat.S_IFSOCK | 0o600, 1001),
        }
        with self.assertRaises(SandboxUnavailableError):
            docker_module.discover_rootless_client_transport(
                "docker-rootless",
                str(executable_root),
                self.workspace / "owner",
                platform_name="linux",
                effective_uid=1001,
                which=lambda name, *, path: str(docker),
                lstat=_fake_lstat(var_run_only),
            )

    def test_podman_rejects_unsafe_engine_runtime_directory(self) -> None:
        """Podman socket 的中间 runtime 目录也必须真实且由有效 UID 拥有。"""
        executable_root = self.workspace / "bin"
        executable_root.mkdir()
        podman = executable_root / "podman"
        podman.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        podman.chmod(0o700)
        runtime = Path("/run/user/1001")
        podman_runtime = runtime / "podman"
        socket = podman_runtime / "podman.sock"
        for unsafe in (
            _fact(stat.S_IFLNK | 0o777, 1001),
            _fact(stat.S_IFDIR | 0o700, 2002),
        ):
            facts = {
                runtime: _fact(stat.S_IFDIR | 0o700, 1001),
                podman_runtime: unsafe,
                socket: _fact(stat.S_IFSOCK | 0o600, 1001),
            }
            with self.subTest(unsafe=unsafe), self.assertRaises(
                SandboxUnavailableError
            ):
                docker_module.discover_rootless_client_transport(
                    "podman-rootless",
                    str(executable_root),
                    self.workspace / "owner",
                    platform_name="linux",
                    effective_uid=1001,
                    which=lambda name, *, path: str(podman),
                    lstat=_fake_lstat(facts),
                )


def _contains_subsequence(values: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    """判断 expected 是否按连续顺序存在于 values。"""
    length = len(expected)
    return any(values[index : index + length] == expected for index in range(len(values)))


def _fact(mode: int, uid: int) -> SimpleNamespace:
    """创建只包含 rootless 校验所需字段的文件事实。"""
    return SimpleNamespace(st_mode=mode, st_uid=uid)


def _fake_lstat(
    facts: dict[Path, SimpleNamespace],
) -> Callable[[Path], SimpleNamespace]:
    """返回只允许读取显式路径事实的 lstat fake。"""
    def lstat(path: Path) -> SimpleNamespace:
        try:
            return facts[path]
        except KeyError:
            raise FileNotFoundError(path) from None

    return lstat


if __name__ == "__main__":
    unittest.main()
