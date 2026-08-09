"""Deterministic hardened Docker sandbox backend。"""

import os
import re
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from miniclaw.sandbox.base import (
    ExecutionPlan,
    ExecutionReceipt,
    SandboxAvailability,
    SandboxPlanError,
    SandboxUnavailableError,
)
from miniclaw.sandbox.host import EnvironmentResolver, HostSandbox

_PINNED_IMAGE = re.compile(r"[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}\Z")
_CLIENT_ONLY_ENVIRONMENT_NAMES = frozenset(
    {"HOME", "XDG_RUNTIME_DIR", "DOCKER_HOST", "CONTAINER_HOST"}
)
ContainerEngine = Literal["docker-rootless", "podman-rootless"]


@dataclass(frozen=True, slots=True)
class RootlessClientTransport:
    """保存 Core 验证后的 rootless 引擎 executable 与 host-only 环境。"""

    engine: ContainerEngine
    executable: Path
    environment: tuple[tuple[str, str], ...]


def discover_rootless_client_transport(
    container_engine: str,
    executable_path: str,
    owner_home: Path | None,
    *,
    platform_name: str | None = None,
    effective_uid: int | None = None,
    which: Callable[..., str | None] | None = None,
    lstat: Callable[[Path], os.stat_result] | None = None,
) -> RootlessClientTransport:
    """发现并验证固定 Linux rootless client transport。

    Args:
        container_engine: 严格配置的 Docker 或 Podman rootless 模式。
        executable_path: 只含可信 executable roots 的 PATH。
        owner_home: 已解析的 Owner Home；为 ``None`` 时不注入 HOME。
        platform_name: 测试可注入的平台名；默认使用当前平台。
        effective_uid: 测试可注入的有效 UID；默认读取当前进程。
        which: 测试可注入的 executable 发现函数。
        lstat: 测试可注入且不跟随 symlink 的文件事实读取函数。

    Returns:
        只供 host client 进程使用的已验证 transport。

    Raises:
        SandboxUnavailableError: 平台、UID、executable、runtime 或 socket 不安全。
        ValueError: container_engine 或 Owner Home 输入无效。
    """
    if container_engine not in {"docker-rootless", "podman-rootless"}:
        raise ValueError("container_engine is invalid")
    validated_engine = cast(ContainerEngine, container_engine)
    if not isinstance(executable_path, str) or not executable_path:
        raise ValueError("executable_path must be a non-empty string")
    if owner_home is not None and not owner_home.is_absolute():
        raise ValueError("owner_home must be absolute")
    current_platform = sys.platform if platform_name is None else platform_name
    current_uid = os.geteuid() if effective_uid is None else effective_uid
    if current_platform != "linux" or type(current_uid) is not int or current_uid <= 0:
        raise SandboxUnavailableError("rootless container client is unavailable")

    executable_name = "docker" if container_engine == "docker-rootless" else "podman"
    discovered = (shutil.which if which is None else which)(
        executable_name, path=executable_path
    )
    if discovered is None:
        raise SandboxUnavailableError("rootless container executable is unavailable")
    executable = Path(discovered).resolve()
    if (
        not executable.is_absolute()
        or executable.name != executable_name
        or not executable.is_file()
        or not os.access(executable, os.X_OK)
    ):
        raise SandboxUnavailableError("rootless container executable is unsafe")

    runtime = Path("/run/user") / str(current_uid)
    socket = (
        runtime / "docker.sock"
        if container_engine == "docker-rootless"
        else runtime / "podman/podman.sock"
    )
    engine_runtime = runtime if container_engine == "docker-rootless" else runtime / "podman"
    inspect = os.lstat if lstat is None else lstat
    try:
        runtime_facts = tuple(inspect(path) for path in dict.fromkeys((runtime, engine_runtime)))
        socket_fact = inspect(socket)
    except OSError:
        raise SandboxUnavailableError("rootless container transport is unavailable") from None
    if (
        any(
            not stat.S_ISDIR(fact.st_mode)
            or fact.st_uid != current_uid
            or stat.S_IMODE(fact.st_mode) != 0o700
            for fact in runtime_facts
        )
        or not stat.S_ISSOCK(socket_fact.st_mode)
        or socket_fact.st_uid != current_uid
    ):
        raise SandboxUnavailableError("rootless container transport is unsafe")

    environment = [] if owner_home is None else [("HOME", str(owner_home))]
    environment.append(("XDG_RUNTIME_DIR", str(runtime)))
    if container_engine == "docker-rootless":
        environment.append(("DOCKER_HOST", f"unix://{socket}"))
    else:
        environment.append(("CONTAINER_HOST", f"unix://{socket}"))
    return RootlessClientTransport(
        engine=validated_engine,
        executable=executable,
        environment=tuple(environment),
    )


class DockerSandbox:
    """用固定 Docker flags 执行 plan，缺失时绝不回退 Host。"""

    def __init__(
        self,
        *,
        image: str,
        container_engine: str = "docker-rootless",
        docker_executable: str = "/usr/bin/docker",
        client_transport: RootlessClientTransport | None = None,
        environment_resolver: EnvironmentResolver | None = None,
    ) -> None:
        """绑定 pinned image、rootless engine、可信路径和受管环境 resolver。"""
        if not isinstance(image, str) or _PINNED_IMAGE.fullmatch(image) is None:
            raise ValueError("docker image must be pinned by sha256 digest")
        if container_engine not in {"docker-rootless", "podman-rootless"}:
            raise ValueError("container_engine is invalid")
        validated_engine = cast(ContainerEngine, container_engine)
        executable = Path(docker_executable)
        if not executable.is_absolute():
            raise ValueError("docker_executable must be absolute")
        if client_transport is not None and (
            client_transport.engine != container_engine
            or client_transport.executable != executable
        ):
            raise ValueError("rootless client transport does not match executable")
        self._image = image
        self._container_engine = validated_engine
        self._docker_executable = executable
        self._client_transport = client_transport
        self._environment_resolver = environment_resolver or _default_environment

    @property
    def container_engine(self) -> ContainerEngine:
        """返回已绑定的稳定 rootless engine 名称。"""
        return self._container_engine

    def availability(self) -> SandboxAvailability:
        """报告已验证 transport 与 exact executable 是否仍可执行。"""
        available = (
            self._client_transport is not None
            and self._docker_executable.is_file()
            and os.access(self._docker_executable, os.X_OK)
        )
        return SandboxAvailability(
            available,
            "container executable is ready"
            if available
            else "container executable is unavailable",
        )

    def build_argv(self, plan: ExecutionPlan) -> tuple[str, ...]:
        """从 canonical plan 构造不可注入的 hardened Docker exact argv。"""
        if plan.backend != "docker":
            raise SandboxPlanError("sandbox_backend_mismatch")
        if _CLIENT_ONLY_ENVIRONMENT_NAMES.intersection(plan.environment_names):
            raise SandboxPlanError("sandbox_environment_forbidden")
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
                ("--mount", f"type=bind,src={root},dst={destination},readonly")
            )
        write_index = 0
        for root in plan.write_roots:
            if root == plan.cwd:
                destination = "/workspace"
            else:
                write_index += 1
                destination = f"/mnt/write-{write_index}"
            mounts.extend(("--mount", f"type=bind,src={root},dst={destination}"))
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
        assert self._client_transport is not None
        argv = self.build_argv(plan)
        client_environment = dict(self._client_transport.environment)
        wrapper_environment_names = tuple(
            dict.fromkeys((*plan.environment_names, *client_environment))
        )
        wrapper = ExecutionPlan(
            argv=argv,
            cwd=plan.cwd,
            environment_names=wrapper_environment_names,
            read_roots=plan.read_roots,
            write_roots=plan.write_roots,
            timeout_seconds=plan.timeout_seconds,
            memory_mib=plan.memory_mib,
            cpu_seconds=plan.cpu_seconds,
            pids_limit=plan.pids_limit,
            network_mode="none",
            backend="host",
        )
        receipt = await HostSandbox(
            lambda name: (
                client_environment[name]
                if name in client_environment
                else self._environment_resolver(name)
            )
        ).execute(wrapper)
        return _rebind_receipt(
            receipt,
            plan,
            "docker",
            redacted_values=tuple(client_environment.values()),
        )


def _default_environment(name: str) -> str | None:
    """只解析 Docker client 启动所需的非 Secret 最小环境。"""
    return {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C.UTF-8"}.get(name)


def _rebind_receipt(
    receipt: ExecutionReceipt,
    plan: ExecutionPlan,
    backend: Literal["docker", "seatbelt"],
    *,
    redacted_values: tuple[str, ...] = (),
) -> ExecutionReceipt:
    """把 wrapper 结果绑定回原 plan hash，并移除 Core-only client 值。"""
    if backend not in {"docker", "seatbelt"}:
        raise ValueError("invalid sandbox receipt backend")
    return ExecutionReceipt(
        plan_hash=plan.sha256,
        backend=backend,
        exit_code=receipt.exit_code,
        signal=receipt.signal,
        timed_out=receipt.timed_out,
        stdout=_redact_values(receipt.stdout, redacted_values),
        stderr=_redact_values(receipt.stderr, redacted_values),
        stdout_truncated=receipt.stdout_truncated,
        stderr_truncated=receipt.stderr_truncated,
        duration_ms=receipt.duration_ms,
        changed_paths=receipt.changed_paths,
    )


def _redact_values(value: str, redacted_values: tuple[str, ...]) -> str:
    """从 client 输出移除不应持久化的 Core-derived host 值。"""
    result = value
    for redacted in sorted(set(redacted_values), key=len, reverse=True):
        if redacted:
            result = result.replace(redacted, "[rootless-client]")
    return result
