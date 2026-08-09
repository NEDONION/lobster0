"""Deterministic hardened Docker sandbox backend。"""

import os
import re
from pathlib import Path
from typing import Literal

from miniclaw.sandbox.base import (
    ExecutionPlan,
    ExecutionReceipt,
    SandboxAvailability,
    SandboxPlanError,
    SandboxUnavailableError,
)
from miniclaw.sandbox.host import EnvironmentResolver, HostSandbox

_PINNED_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}\Z")


class DockerSandbox:
    """用固定 Docker flags 执行 plan，缺失时绝不回退 Host。"""

    def __init__(
        self,
        *,
        image: str,
        docker_executable: str = "/usr/bin/docker",
        environment_resolver: EnvironmentResolver | None = None,
    ) -> None:
        """绑定 pinned image、可信 Docker 路径和受管环境 resolver。"""
        if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
            raise ValueError("docker image must be pinned by sha256 digest")
        executable = Path(docker_executable)
        if not executable.is_absolute():
            raise ValueError("docker_executable must be absolute")
        self._image = image
        self._docker_executable = executable
        self._environment_resolver = environment_resolver or _default_environment

    def availability(self) -> SandboxAvailability:
        """只探测 exact executable，不搜索 PATH 或执行 Docker daemon。"""
        available = self._docker_executable.is_file() and os.access(
            self._docker_executable, os.X_OK
        )
        return SandboxAvailability(
            available,
            "docker executable is ready" if available else "docker executable is unavailable",
        )

    def build_argv(self, plan: ExecutionPlan) -> tuple[str, ...]:
        """从 canonical plan 构造不可注入的 hardened Docker exact argv。"""
        if plan.backend != "docker":
            raise SandboxPlanError("sandbox_backend_mismatch")
        if plan.network_mode != "none":
            raise ValueError("sandbox_network_unsupported")
        if any("," in str(root) for root in (*plan.read_roots, *plan.write_roots)):
            raise SandboxPlanError("execution_plan_invalid", "mount path contains comma")
        mounts: list[str] = []
        read_index = 0
        for root in plan.read_roots:
            read_index += 1
            destination = "/workspace" if root == plan.cwd else f"/mnt/readonly-{read_index}"
            mounts.extend(
                ("--mount", f"type=bind,src={root},dst={destination},ro")
            )
        write_index = 0
        for root in plan.write_roots:
            if root == plan.cwd:
                destination = "/workspace"
            else:
                write_index += 1
                destination = f"/mnt/write-{write_index}"
            mounts.extend(("--mount", f"type=bind,src={root},dst={destination},rw"))
        environment: list[str] = []
        for name in plan.environment_names:
            environment.extend(("--env", name))
        return (
            str(self._docker_executable),
            "run",
            "--rm",
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            str(plan.pids_limit),
            "--memory",
            f"{plan.memory_mib}m",
            "--cpus",
            "1.0",
            "--user",
            "65532:65532",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--workdir",
            "/workspace",
            *environment,
            *mounts,
            "--",
            self._image,
            *plan.argv,
        )

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """执行 hardened Docker argv，并把 receipt 重新绑定原 Docker plan。"""
        if not self.availability().available:
            raise SandboxUnavailableError()
        argv = self.build_argv(plan)
        wrapper = ExecutionPlan(
            argv=argv,
            cwd=plan.cwd,
            environment_names=plan.environment_names,
            read_roots=plan.read_roots,
            write_roots=plan.write_roots,
            timeout_seconds=plan.timeout_seconds,
            memory_mib=plan.memory_mib,
            cpu_seconds=plan.cpu_seconds,
            pids_limit=plan.pids_limit,
            network_mode="none",
            backend="host",
        )
        receipt = await HostSandbox(self._environment_resolver).execute(wrapper)
        return _rebind_receipt(receipt, plan, "docker")


def _default_environment(name: str) -> str | None:
    """只解析 Docker client 启动所需的非 Secret 最小环境。"""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}.get(name)


def _rebind_receipt(
    receipt: ExecutionReceipt,
    plan: ExecutionPlan,
    backend: Literal["docker", "seatbelt"],
) -> ExecutionReceipt:
    """把 wrapper 结果绑定回原 plan hash，保留有界输出。"""
    if backend not in {"docker", "seatbelt"}:
        raise ValueError("invalid sandbox receipt backend")
    return ExecutionReceipt(
        plan_hash=plan.sha256,
        backend=backend,
        exit_code=receipt.exit_code,
        signal=receipt.signal,
        timed_out=receipt.timed_out,
        stdout=receipt.stdout,
        stderr=receipt.stderr,
        stdout_truncated=receipt.stdout_truncated,
        stderr_truncated=receipt.stderr_truncated,
        duration_ms=receipt.duration_ms,
        changed_paths=receipt.changed_paths,
    )
