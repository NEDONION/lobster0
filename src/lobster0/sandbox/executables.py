"""冻结并复核 Seatbelt 允许执行的 exact executable chain。"""

import hashlib
import os
import shutil
import stat
from pathlib import Path

from lobster0.policy.command import CommandPolicyError, normalize_command
from lobster0.sandbox.base import ExecutableRef, SandboxPlanError

_MAX_CHAIN = 4
_SHEBANG_BYTES = 4096
_READ_BYTES = 1024 * 1024


def capture_executable_chain(
    program: Path,
    *,
    executable_path: str,
) -> tuple[ExecutableRef, ...]:
    """冻结 direct、absolute shebang 或 ``env NAME`` executable chain。

    Args:
        program: Policy 已解析的 executable path。
        executable_path: Core 冻结的最小 PATH，用于解析 ``/usr/bin/env NAME``。

    Returns:
        按系统执行顺序排列、包含内容 SHA-256 的一到四个引用。

    Raises:
        SandboxPlanError: 任一路径、shebang、文件类型、执行位或 chain 不安全。
    """
    if (
        not isinstance(program, Path)
        or not isinstance(executable_path, str)
        or not executable_path
        or _has_control(executable_path)
    ):
        raise SandboxPlanError("execution_plan_exec_chain_invalid")
    pending = [_resolve_path(program)]
    refs: list[ExecutableRef] = []
    seen: set[Path] = set()
    while pending:
        path = pending.pop(0)
        if path in seen or len(refs) >= _MAX_CHAIN:
            raise SandboxPlanError("execution_plan_exec_chain_invalid")
        seen.add(path)
        ref, header = _capture(path, "execution_plan_exec_chain_invalid")
        refs.append(ref)
        pending[0:0] = _resolve_shebang(
            header,
            executable_path=executable_path,
            workspace=program.parent,
        )
    return tuple(refs)


def verify_executable_chain(refs: tuple[ExecutableRef, ...]) -> None:
    """在 backend 执行前复核 executable 的类型、执行位与内容摘要。

    Args:
        refs: 审批时进入 ExecutionPlan v2 的 exact chain。

    Returns:
        全部引用未变化时返回 ``None``。

    Raises:
        SandboxPlanError: chain 形状无效或任一 executable 已变化。
    """
    if (
        not isinstance(refs, tuple)
        or not 1 <= len(refs) <= _MAX_CHAIN
        or any(not isinstance(ref, ExecutableRef) for ref in refs)
        or len({ref.path for ref in refs}) != len(refs)
    ):
        raise SandboxPlanError("execution_plan_executable_changed")
    for expected in refs:
        observed, _ = _capture(expected.path, "execution_plan_executable_changed")
        if observed != expected:
            raise SandboxPlanError("execution_plan_executable_changed")


def _capture(path: Path, error_code: str) -> tuple[ExecutableRef, bytes]:
    """以 no-follow descriptor 同时读取 header、hash 和稳定 file facts。"""
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    if no_follow == 0:
        raise SandboxPlanError(error_code)
    flags = os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or not before.st_mode & (
                stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
            ):
                raise SandboxPlanError(error_code)
            header = os.read(descriptor, _SHEBANG_BYTES)
            digest = hashlib.sha256(header)
            while chunk := os.read(descriptor, _READ_BYTES):
                digest.update(chunk)
            after = os.fstat(descriptor)
            if _file_identity(before) != _file_identity(after):
                raise SandboxPlanError(error_code)
        finally:
            os.close(descriptor)
    except SandboxPlanError:
        raise
    except OSError:
        raise SandboxPlanError(error_code) from None
    return ExecutableRef(path, digest.hexdigest()), header


def _resolve_shebang(
    header: bytes,
    *,
    executable_path: str,
    workspace: Path,
) -> list[Path]:
    """把支持的 shebang 收窄为 exact interpreter path 列表。"""
    if not header.startswith(b"#!"):
        return []
    line = header.split(b"\n", 1)[0][2:].strip()
    try:
        parts = line.decode("utf-8").split()
    except UnicodeDecodeError:
        raise SandboxPlanError("execution_plan_exec_chain_invalid") from None
    if not parts or any(_has_control(part) for part in parts):
        raise SandboxPlanError("execution_plan_exec_chain_invalid")
    if parts[0] == "/usr/bin/env":
        if (
            len(parts) != 2
            or parts[1].startswith("-")
            or Path(parts[1]).name != parts[1]
        ):
            raise SandboxPlanError("execution_plan_exec_chain_invalid")
        found = shutil.which(parts[1], path=executable_path)
        if found is None:
            raise SandboxPlanError("execution_plan_exec_chain_invalid")
        target = _allowed_interpreter(Path(found), workspace, executable_path)
        return [_resolve_path(Path("/usr/bin/env")), target]
    if len(parts) != 1 or not Path(parts[0]).is_absolute():
        raise SandboxPlanError("execution_plan_exec_chain_invalid")
    return [_allowed_interpreter(Path(parts[0]), workspace, executable_path)]


def _allowed_interpreter(path: Path, workspace: Path, executable_path: str) -> Path:
    """复用 Command Policy，拒绝 Shell、提权和其他硬禁止解释器。"""
    try:
        normalized = normalize_command(
            str(path),
            (),
            workspace,
            executable_path=executable_path,
        )
    except CommandPolicyError:
        raise SandboxPlanError("execution_plan_exec_chain_invalid") from None
    return Path(normalized.resolved_program)


def _resolve_path(path: Path) -> Path:
    """解析首个可信路径并隐藏底层路径错误。"""
    if not path.is_absolute() or _has_control(str(path)):
        raise SandboxPlanError("execution_plan_exec_chain_invalid")
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SandboxPlanError("execution_plan_exec_chain_invalid") from None


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """返回一次读取前后必须保持稳定的 inode 元数据。"""
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _has_control(value: str) -> bool:
    """识别会让 path、PATH 或 shebang 产生歧义的控制字符。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
