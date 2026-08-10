"""Sandbox 共用的 immutable plan、receipt 与 backend 协议。"""

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, Self, cast

from lobster0.providers.base import JsonValue

SandboxBackendName = Literal["host", "docker", "seatbelt"]
NetworkMode = Literal["none", "allowlisted"]
_BACKENDS = frozenset({"host", "docker", "seatbelt"})
_NETWORK_MODES = frozenset({"none", "allowlisted"})
_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SandboxPlanError(ValueError):
    """表示 ExecutionPlan/Receipt 无效、被篡改或与批准内容不一致。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存稳定错误码，并避免调用方依赖底层异常正文。"""
        super().__init__(message or code)
        self.code = code


class SandboxUnavailableError(SandboxPlanError):
    """表示显式选择的 sandbox backend 当前不可用且不会自动降级。"""

    def __init__(self, message: str = "sandbox_backend_unavailable") -> None:
        """固定外部错误码，同时允许内部提供不敏感说明。"""
        super().__init__("sandbox_backend_unavailable", message)


@dataclass(frozen=True, slots=True)
class SandboxAvailability:
    """描述 backend 探测结果，不触发安装或降级。"""

    available: bool
    detail: str


@dataclass(frozen=True, slots=True)
class ExecutableRef:
    """绑定一个 exact executable path 与审批时的内容摘要。"""

    path: Path
    sha256: str

    def __post_init__(self) -> None:
        """拒绝相对/控制字符路径和非标准 SHA-256。"""
        path = _absolute_path(self.path)
        if (
            _has_control(str(path))
            or not isinstance(self.sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
        ):
            raise SandboxPlanError("execution_plan_invalid")
        object.__setattr__(self, "path", path)


@dataclass(frozen=True, slots=True)
class ExecutionPlan:
    """描述一次不经 Shell、可 canonicalize 且可绑定审批的命令执行。"""

    argv: tuple[str, ...]
    cwd: Path
    environment_names: tuple[str, ...]
    read_roots: tuple[Path, ...]
    write_roots: tuple[Path, ...]
    timeout_seconds: int
    memory_mib: int
    cpu_seconds: int
    pids_limit: int
    network_mode: NetworkMode
    backend: SandboxBackendName
    executables: tuple[ExecutableRef, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        """规范顺序和 Path，并拒绝歧义、控制字符与越界资源。"""
        if type(self.schema_version) is not int or self.schema_version not in {1, 2}:
            raise SandboxPlanError("execution_plan_invalid", "unsupported schema version")
        if (
            not self.argv
            or not isinstance(self.argv[0], str)
            or not self.argv[0]
            or _has_control(self.argv[0])
            or any(
                not isinstance(value, str) or _has_control(value)
                for value in self.argv[1:]
            )
        ):
            raise SandboxPlanError("execution_plan_invalid", "argv is invalid")
        cwd = _absolute_path(self.cwd)
        environment_names = tuple(sorted(self.environment_names))
        if (
            len(set(environment_names)) != len(environment_names)
            or any(
                not isinstance(name, str) or _ENVIRONMENT_NAME.fullmatch(name) is None
                for name in environment_names
            )
        ):
            raise SandboxPlanError(
                "execution_plan_invalid", "environment names are invalid"
            )
        read_roots = _canonical_roots(self.read_roots)
        write_roots = _canonical_roots(self.write_roots)
        for read_root in read_roots:
            for write_root in write_roots:
                if _paths_overlap(read_root, write_root):
                    raise SandboxPlanError(
                        "execution_plan_invalid", "read and write roots overlap"
                    )
        _bounded_integer("timeout_seconds", self.timeout_seconds, 1, 120)
        _bounded_integer("memory_mib", self.memory_mib, 16, 32_768)
        _bounded_integer("cpu_seconds", self.cpu_seconds, 1, 3600)
        _bounded_integer("pids_limit", self.pids_limit, 1, 4096)
        if self.network_mode not in _NETWORK_MODES:
            raise SandboxPlanError("execution_plan_invalid", "network mode is invalid")
        if self.backend not in _BACKENDS:
            raise SandboxPlanError("execution_plan_invalid", "backend is invalid")
        if not isinstance(self.executables, tuple) or any(
            not isinstance(ref, ExecutableRef) for ref in self.executables
        ):
            raise SandboxPlanError("execution_plan_invalid")
        if self.schema_version == 1:
            if self.executables:
                raise SandboxPlanError("execution_plan_invalid")
        elif (
            self.backend != "seatbelt"
            or not 1 <= len(self.executables) <= 4
            or len({ref.path for ref in self.executables}) != len(self.executables)
        ):
            raise SandboxPlanError("execution_plan_invalid")
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "environment_names", environment_names)
        object.__setattr__(self, "read_roots", read_roots)
        object.__setattr__(self, "write_roots", write_roots)

    @property
    def canonical_json(self) -> str:
        """返回不含环境变量值的 versioned canonical JSON。"""
        value: dict[str, JsonValue] = {
            "argv": list(self.argv),
            "backend": self.backend,
            "cpu_seconds": self.cpu_seconds,
            "cwd": str(self.cwd),
            "environment_names": list(self.environment_names),
            "memory_mib": self.memory_mib,
            "network_mode": self.network_mode,
            "pids_limit": self.pids_limit,
            "read_roots": [str(path) for path in self.read_roots],
            "schema_version": self.schema_version,
            "timeout_seconds": self.timeout_seconds,
            "write_roots": [str(path) for path in self.write_roots],
        }
        if self.schema_version == 2:
            value["executables"] = [
                {"path": str(ref.path), "sha256": ref.sha256}
                for ref in self.executables
            ]
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @property
    def sha256(self) -> str:
        """返回 canonical JSON 的稳定 SHA-256。"""
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_canonical_json(cls, value: str) -> Self:
        """严格恢复已持久化 plan，未知字段或类型一律失败关闭。"""
        try:
            decoded = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            raise SandboxPlanError("execution_plan_invalid") from None
        common_fields = {
            "argv",
            "backend",
            "cpu_seconds",
            "cwd",
            "environment_names",
            "memory_mib",
            "network_mode",
            "pids_limit",
            "read_roots",
            "schema_version",
            "timeout_seconds",
            "write_roots",
        }
        if not isinstance(decoded, dict):
            raise SandboxPlanError("execution_plan_invalid")
        try:
            schema_version = _integer(decoded.get("schema_version"))
        except TypeError:
            raise SandboxPlanError("execution_plan_invalid") from None
        expected = common_fields if schema_version == 1 else common_fields | {"executables"}
        if not isinstance(decoded, dict) or set(decoded) != expected:
            raise SandboxPlanError("execution_plan_invalid")
        try:
            plan = cls(
                argv=_string_tuple(decoded["argv"]),
                cwd=Path(_string(decoded["cwd"])),
                environment_names=_string_tuple(decoded["environment_names"]),
                read_roots=tuple(Path(value) for value in _string_tuple(decoded["read_roots"])),
                write_roots=tuple(
                    Path(value) for value in _string_tuple(decoded["write_roots"])
                ),
                timeout_seconds=_integer(decoded["timeout_seconds"]),
                memory_mib=_integer(decoded["memory_mib"]),
                cpu_seconds=_integer(decoded["cpu_seconds"]),
                pids_limit=_integer(decoded["pids_limit"]),
                network_mode=cast(NetworkMode, _string(decoded["network_mode"])),
                backend=cast(SandboxBackendName, _string(decoded["backend"])),
                executables=(
                    _executable_refs(decoded["executables"])
                    if schema_version == 2
                    else ()
                ),
                schema_version=schema_version,
            )
        except (TypeError, SandboxPlanError):
            raise SandboxPlanError("execution_plan_invalid") from None
        if plan.canonical_json != value:
            raise SandboxPlanError("execution_plan_invalid", "plan is not canonical")
        return plan


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """记录一次 plan 执行的有界、无 Secret value 结果。"""

    plan_hash: str
    backend: SandboxBackendName
    exit_code: int | None
    signal: int | None
    timed_out: bool
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    duration_ms: int
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        """拒绝损坏 hash、负耗时、非法 backend 与不安全路径。"""
        if not re.fullmatch(r"[0-9a-f]{64}", self.plan_hash):
            raise SandboxPlanError("execution_receipt_invalid")
        if self.backend not in _BACKENDS:
            raise SandboxPlanError("execution_receipt_invalid")
        if self.exit_code is not None and type(self.exit_code) is not int:
            raise SandboxPlanError("execution_receipt_invalid")
        if self.signal is not None and type(self.signal) is not int:
            raise SandboxPlanError("execution_receipt_invalid")
        if type(self.timed_out) is not bool or type(self.duration_ms) is not int:
            raise SandboxPlanError("execution_receipt_invalid")
        if self.duration_ms < 0:
            raise SandboxPlanError("execution_receipt_invalid")
        if any(
            not isinstance(path, str)
            or not path
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or _has_control(path)
            for path in self.changed_paths
        ):
            raise SandboxPlanError("execution_receipt_invalid")
        object.__setattr__(self, "changed_paths", tuple(sorted(set(self.changed_paths))))

    @property
    def canonical_json(self) -> str:
        """返回适合 SQLite 的稳定 receipt JSON。"""
        value: dict[str, JsonValue] = {
            "backend": self.backend,
            "changed_paths": list(self.changed_paths),
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "plan_hash": self.plan_hash,
            "signal": self.signal,
            "stderr": self.stderr,
            "stderr_truncated": self.stderr_truncated,
            "stdout": self.stdout,
            "stdout_truncated": self.stdout_truncated,
            "timed_out": self.timed_out,
        }
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    @classmethod
    def from_canonical_json(cls, value: str) -> Self:
        """严格恢复 receipt，并验证重新编码结果完全一致。"""
        try:
            decoded = json.loads(value, parse_constant=_reject_json_constant)
        except (json.JSONDecodeError, ValueError):
            raise SandboxPlanError("execution_receipt_invalid") from None
        expected = {
            "backend",
            "changed_paths",
            "duration_ms",
            "exit_code",
            "plan_hash",
            "signal",
            "stderr",
            "stderr_truncated",
            "stdout",
            "stdout_truncated",
            "timed_out",
        }
        if not isinstance(decoded, dict) or set(decoded) != expected:
            raise SandboxPlanError("execution_receipt_invalid")
        try:
            receipt = cls(
                plan_hash=_string(decoded["plan_hash"]),
                backend=cast(SandboxBackendName, _string(decoded["backend"])),
                exit_code=_optional_integer(decoded["exit_code"]),
                signal=_optional_integer(decoded["signal"]),
                timed_out=_boolean(decoded["timed_out"]),
                stdout=_string(decoded["stdout"]),
                stderr=_string(decoded["stderr"]),
                stdout_truncated=_boolean(decoded["stdout_truncated"]),
                stderr_truncated=_boolean(decoded["stderr_truncated"]),
                duration_ms=_integer(decoded["duration_ms"]),
                changed_paths=_string_tuple(decoded["changed_paths"]),
            )
        except (TypeError, SandboxPlanError):
            raise SandboxPlanError("execution_receipt_invalid") from None
        if receipt.canonical_json != value:
            raise SandboxPlanError("execution_receipt_invalid")
        return receipt


class SandboxBackend(Protocol):
    """执行已经 immutable/canonicalize 的 plan。"""

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """执行 plan 并返回绑定同一 hash 的有界 receipt。"""
        ...


def _absolute_path(value: Path) -> Path:
    """规范 absolute Path，不访问路径内容。"""
    if not isinstance(value, Path) or not value.is_absolute():
        raise SandboxPlanError("execution_plan_invalid", "path must be absolute")
    return Path(os.path.normpath(value))


def _canonical_roots(values: tuple[Path, ...]) -> tuple[Path, ...]:
    """规范 mount root 并拒绝重复、嵌套与系统根。"""
    if not isinstance(values, tuple):
        raise SandboxPlanError("execution_plan_invalid")
    roots = tuple(sorted((_absolute_path(value) for value in values), key=str))
    if len(set(roots)) != len(roots) or any(root == Path(root.anchor) for root in roots):
        raise SandboxPlanError("execution_plan_invalid", "mount roots are ambiguous")
    for index, root in enumerate(roots):
        if any(_paths_overlap(root, other) for other in roots[index + 1 :]):
            raise SandboxPlanError("execution_plan_invalid", "mount roots overlap")
    return roots


def _paths_overlap(first: Path, second: Path) -> bool:
    """判断两个 canonical roots 是否相同或互为祖先。"""
    return first == second or first in second.parents or second in first.parents


def _bounded_integer(name: str, value: int, minimum: int, maximum: int) -> None:
    """验证 exact integer resource limit。"""
    if type(value) is not int or not minimum <= value <= maximum:
        raise SandboxPlanError("execution_plan_invalid", f"{name} is out of bounds")


def _has_control(value: str) -> bool:
    """识别 NUL、换行和其他会造成 argv/日志歧义的控制字符。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _string(value: object) -> str:
    """严格提取 JSON string。"""
    if not isinstance(value, str):
        raise TypeError("value is not a string")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    """严格提取 JSON string array。"""
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError("value is not a string array")
    return tuple(value)


def _executable_refs(value: object) -> tuple[ExecutableRef, ...]:
    """严格提取 v2 executable reference array。"""
    if not isinstance(value, list):
        raise TypeError("value is not an executable reference array")
    refs: list[ExecutableRef] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise TypeError("value is not an executable reference array")
        refs.append(
            ExecutableRef(
                Path(_string(item["path"])),
                _string(item["sha256"]),
            )
        )
    return tuple(refs)


def _integer(value: object) -> int:
    """严格提取 JSON integer，拒绝 bool。"""
    if type(value) is not int:
        raise TypeError("value is not an integer")
    return value


def _optional_integer(value: object) -> int | None:
    """严格提取 nullable JSON integer。"""
    if value is None:
        return None
    return _integer(value)


def _boolean(value: object) -> bool:
    """严格提取 JSON boolean。"""
    if type(value) is not bool:
        raise TypeError("value is not a boolean")
    return value


def _reject_json_constant(value: str) -> JsonValue:
    """拒绝 JSON 标准之外的 NaN/Infinity。"""
    raise ValueError(f"invalid JSON constant: {value}")
