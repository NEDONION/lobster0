"""为没有 install receipt 的包安装驱动用户级 Gateway 服务。

wheel、``uv tool``、``pipx`` 与 ``pip`` 安装都没有受管 ``InstallLayout``，
因此无法走 ``orchestrator.run_install_action``；但它们同样需要一个开机自启、
崩溃自愈、注销后仍存活的 Gateway。本模块只负责三件受管路径由 install receipt
承担的事——选平台、解析 ``ExecStart`` 可执行文件、维护一个极小的 owner-only
service receipt——unit/plist 的渲染、发布、lint 与回滚全部复用
``lobster0.install.service``，不存在第二套实现。
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
import sysconfig
from pathlib import Path
from typing import TextIO

from lobster0.install.models import InstallError
from lobster0.install.service import (
    ServicePlatform,
    ServiceSpec,
    render_package_service_spec,
    service_install,
    service_logs,
    service_restart,
    service_status,
    service_uninstall,
)

_ACTIONS = ("install", "status", "logs", "restart", "uninstall")
_EXECUTABLE_NAME = "lobster0"
_RECEIPT_NAME = "service.json"
_MAX_RECEIPT_BYTES = 4096
_HASH = re.compile(r"^[0-9a-f]{64}$")
_EXECUTABLE_HINT = (
    "The lobster0 executable for this installation could not be located. "
    "Reinstall with `uv tool install` or `pipx install` so the console script "
    "sits beside the interpreter, then re-run `lobster0 service install`."
)
_PLATFORM_HINT = (
    "A user-level Lobster0 service is only supported on Linux (systemd user "
    "units) and macOS (LaunchAgents)."
)


class PackageServiceError(RuntimeError):
    """表示包安装模式下的请求错误，只暴露稳定错误码与可执行指引。"""

    def __init__(self, code: str, hint: str) -> None:
        """保存稳定错误码与面向用户的下一步指引。"""
        self.code = code
        self.hint = hint
        super().__init__(code)


def package_service_platform(platform_name: str | None = None) -> ServicePlatform:
    """把平台名映射到受支持的用户级 service manager。

    Args:
        platform_name: 平台标识；省略时使用 ``sys.platform``。

    Returns:
        Linux 为 ``SYSTEMD_USER``，macOS 为 ``LAUNCHD``。

    Raises:
        PackageServiceError: 平台没有受支持的用户级 service manager。
    """
    name = sys.platform if platform_name is None else str(platform_name)
    if name == "darwin":
        return ServicePlatform.LAUNCHD
    if name.startswith("linux"):
        return ServicePlatform.SYSTEMD_USER
    raise PackageServiceError("service_platform_unsupported", _PLATFORM_HINT)


def resolve_package_launcher(
    *,
    executable: Path | None = None,
    scripts_paths: tuple[Path, ...] | None = None,
) -> Path:
    """解析 ``ExecStart`` 必须指向的 ``lobster0`` 可执行文件。

    按可信度取第一个存在、是普通文件且当前用户可执行的候选：解释器同目录的
    console script（venv/``uv tool``/``pipx``）优先，因为它保证服务指向的正是
    **当前正在运行的这个安装**；其次是当前 install scheme 与 user scheme 的
    scripts 目录（``pip install --user``）。

    刻意不回退到 ``shutil.which``：PATH 上的同名命令可能属于另一个安装，让服务
    指向另一个版本比干净失败更糟。

    Args:
        executable: 当前解释器路径；省略时使用 ``sys.executable``。
        scripts_paths: 候选 scripts 目录；省略时由 ``sysconfig`` 推导。

    Returns:
        绝对、存在且可执行的 launcher 路径。

    Raises:
        PackageServiceError: 全部候选都不可用。
    """
    ordered: list[Path] = []
    interpreter = Path(sys.executable if executable is None else executable)
    if interpreter.is_absolute():
        ordered.append(interpreter.parent / _EXECUTABLE_NAME)
    for directory in _scripts_directories() if scripts_paths is None else scripts_paths:
        candidate = Path(directory) / _EXECUTABLE_NAME
        if candidate.is_absolute():
            ordered.append(candidate)
    seen: set[Path] = set()
    for candidate in ordered:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _usable_executable(candidate):
            return candidate
    raise PackageServiceError("service_executable_unresolved", _EXECUTABLE_HINT)


def run_package_service_action(
    command: str,
    *,
    state_home: Path,
    runner: object | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    platform_name: str | None = None,
    launcher: Path | None = None,
    executable: Path | None = None,
    scripts_paths: tuple[Path, ...] | None = None,
    user_home: Path | None = None,
) -> int:
    """在包安装模式下执行一个 service 动作并返回稳定退出码。

    Args:
        command: ``install``/``status``/``logs``/``restart``/``uninstall``。
        state_home: 已解析的绝对状态根。
        runner: 可注入的 bounded exact runner。
        stdout: 可替换标准输出。
        stderr: 可替换标准错误。
        platform_name: 平台标识；省略时使用 ``sys.platform``。
        launcher: 已解析的 launcher；省略时按 ``resolve_package_launcher`` 推导。
        executable: 当前解释器路径，仅在需要推导 launcher 时使用。
        scripts_paths: 候选 scripts 目录，仅在需要推导 launcher 时使用。
        user_home: 目标用户 Home；省略时从 passwd 解析。

    Returns:
        成功为 0、请求/解析错误为 2、lifecycle 错误为 5。
    """
    out = sys.stdout if stdout is None else stdout
    error_stream = sys.stderr if stderr is None else stderr
    if command not in _ACTIONS:
        print("error: request_invalid", file=error_stream)
        return 2
    try:
        platform = package_service_platform(platform_name)
        selected = (
            resolve_package_launcher(executable=executable, scripts_paths=scripts_paths)
            if launcher is None
            else Path(launcher)
        )
        spec = render_package_service_spec(
            launcher=selected,
            state_home=state_home,
            platform=platform,
            user_home=user_home,
        )
    except PackageServiceError as failure:
        print(f"error: {failure.code}", file=error_stream)
        print(failure.hint, file=error_stream)
        return 2
    except InstallError as failure:
        print(f"error: {failure.code}", file=error_stream)
        return 5
    try:
        return _dispatch(command, spec, state_home=state_home, runner=runner, stdout=out)
    except PackageServiceError as failure:
        print(f"error: {failure.code}", file=error_stream)
        print(failure.hint, file=error_stream)
        return 5
    except InstallError as failure:
        print(f"error: {failure.code}", file=error_stream)
        return 5
    except OSError:
        print("error: service_state_failed", file=error_stream)
        return 5


def _dispatch(
    command: str,
    spec: ServiceSpec,
    *,
    state_home: Path,
    runner: object | None,
    stdout: TextIO,
) -> int:
    """驱动五个固定动作，并在 install/uninstall 两端维护 service receipt。"""
    if command == "status":
        running = service_status(spec, runner)
        installed = _recorded_digest(spec, state_home) is not None and _lexists(spec.path)
        print(
            f"service installed={'true' if installed else 'false'} "
            f"running={'true' if running else 'false'}",
            file=stdout,
        )
        return 0
    if command == "logs":
        result = service_logs(spec, runner)
        print(result.stdout.decode("utf-8", errors="replace"), end="", file=stdout)
        return 0
    if command == "restart":
        service_restart(spec, runner)
        print("service restarted", file=stdout)
        return 0
    recorded = _recorded_digest(spec, state_home)
    present = _lexists(spec.path)
    if present and recorded is None:
        # 同名 service 文件存在但不是我们写的（或已被改动）：绝不覆盖或删除。
        raise PackageServiceError(
            "service_file_unowned",
            "A service file with the managed name already exists and does not match "
            "the Lobster0 service receipt; inspect and remove it manually.",
        )
    if command == "install":
        digest = service_install(spec, runner, expected_sha256=recorded if present else None)
        _write_receipt(spec, state_home, digest)
        print("service installed", file=stdout)
        return 0
    if not present:
        _remove_receipt(state_home)
        print("service uninstalled", file=stdout)
        return 0
    assert recorded is not None
    service_uninstall(spec, runner, expected_sha256=recorded)
    _remove_receipt(state_home)
    print("service uninstalled", file=stdout)
    return 0


def _scripts_directories() -> tuple[Path, ...]:
    """返回当前 install scheme 与 user scheme 的 scripts 目录。"""
    directories: list[Path] = []
    for scheme in (None, "posix_user"):
        try:
            raw = (
                sysconfig.get_path("scripts")
                if scheme is None
                else sysconfig.get_path("scripts", scheme=scheme)
            )
        except (KeyError, TypeError, ValueError):
            continue
        if type(raw) is str and raw:
            directories.append(Path(raw))
    return tuple(directories)


def _usable_executable(path: Path) -> bool:
    """确认候选是当前用户可执行的普通文件。"""
    try:
        metadata = path.stat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and os.access(path, os.X_OK)


def _receipt_path(state_home: Path) -> Path:
    """返回包安装模式的 owner-only service receipt 路径。"""
    return Path(state_home) / "run" / _RECEIPT_NAME


def _recorded_digest(spec: ServiceSpec, state_home: Path) -> str | None:
    """读取仍与当前 spec 的 label/path 匹配的已登记 service 文件摘要。"""
    path = _receipt_path(state_home)
    try:
        with open(path, "rb") as handle:
            payload = handle.read(_MAX_RECEIPT_BYTES + 1)
    except OSError:
        return None
    if len(payload) > _MAX_RECEIPT_BYTES:
        return None
    try:
        document = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if type(document) is not dict:
        return None
    digest = document.get("sha256")
    if (
        document.get("label") != spec.label
        or document.get("path") != str(spec.path)
        or type(digest) is not str
        or _HASH.fullmatch(digest) is None
    ):
        return None
    return digest


def _write_receipt(spec: ServiceSpec, state_home: Path, digest: str) -> None:
    """原子写入 0600 的 service receipt，只记录 label、path 与摘要。"""
    parent = _receipt_path(state_home).parent
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = _receipt_path(state_home)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(
        {"label": spec.label, "path": str(spec.path), "sha256": digest},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _remove_receipt(state_home: Path) -> None:
    """删除 service receipt；不存在时视为已完成。"""
    try:
        _receipt_path(state_home).unlink()
    except FileNotFoundError:
        return


def _lexists(path: Path) -> bool:
    """no-follow 判断目录项是否存在。"""
    try:
        path.lstat()
    except OSError:
        return False
    return True
