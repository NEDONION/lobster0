"""生成并管理 owner-only systemd user 与 LaunchAgent 服务。"""

from __future__ import annotations

import hashlib
import os
import plistlib
import pwd
import re
import shlex
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Never

from miniclaw.install.layout import InstallLayout
from miniclaw.install.models import InstallError
from miniclaw.install.receipt import (
    _fsync_directory,
    _quarantine_expected,
    _QuarantinedPath,
    managed_file_sha256,
    verify_managed_file,
)
from miniclaw.install.runtime import CommandResult, CommandRunner, _SubprocessRunner

_SYSTEMD_LABEL = "miniclaw-gateway.service"
_LAUNCHD_LABEL = "io.miniclaw.gateway"
_PATH = "/usr/local/bin:/usr/bin:/bin"
_TIMEOUT_SECONDS = 30.0
_HASH = re.compile(r"^[0-9a-f]{64}$")
_MAX_SERVICE_BYTES = 1024 * 1024


class ServicePlatform(StrEnum):
    """列出 Tier 1 支持的两个用户级 service manager。"""

    SYSTEMD_USER = "systemd-user"
    LAUNCHD = "launchd"


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """保存一个受管 service 文件和 exact manager commands。

    Args:
        platform: 用户级 manager 类型。
        label: 固定 service unit 或 LaunchAgent label。
        path: 用户 Home 下固定 service 文件路径。
        content: 完整 unit/plist bytes，不含 Secret 值。
        install_argvs: lint 后注册服务的 exact argv。
        status_argv: 查询服务状态的 exact argv。
        restart_argv: 重启服务的 exact argv。
        uninstall_argvs: 停止、注销和刷新 manager 的 exact argv。

    Raises:
        InstallError: 任一字段不是 closed-world user-service contract。
    """

    platform: ServicePlatform
    label: str
    path: Path
    content: bytes
    install_argvs: tuple[tuple[str, ...], ...]
    status_argv: tuple[str, ...]
    restart_argv: tuple[str, ...]
    uninstall_argvs: tuple[tuple[str, ...], ...]

    def __post_init__(self) -> None:
        """重新解析内容并校验 path、UID domain 与全部 exact argv。"""
        _validate_service_spec(self)


@dataclass(frozen=True, slots=True)
class _OwnedFile:
    """绑定 service 文件的 no-follow inode、hash 与原始 bytes。"""

    identity: tuple[int, int]
    sha256: str
    content: bytes


def render_service_spec(layout: InstallLayout, platform: ServicePlatform) -> ServiceSpec:
    """从受管 layout 生成用户级 service spec。

    Args:
        layout: Task 7 已校验的 stable launcher 与 state layout。
        platform: Task 5 已检测的 user service manager。

    Returns:
        可直接交给 lifecycle 函数的 strict ServiceSpec。

    Raises:
        InstallError: layout/platform 不可信、当前是 root 或路径不可安全表达。
    """
    if type(layout) is not InstallLayout or type(platform) is not ServicePlatform:
        _service_failed()
    layout.__post_init__()
    uid = os.geteuid()
    if type(uid) is not int or uid <= 0:
        _service_failed()
    home = _layout_home(layout, uid)
    _require_safe_path(home)
    arguments = (
        str(layout.launcher),
        "gateway",
        "--home",
        str(layout.state_home),
    )
    if platform is ServicePlatform.SYSTEMD_USER:
        label = _SYSTEMD_LABEL
        path = home / ".config" / "systemd" / "user" / label
        content = _render_systemd(arguments, layout.secrets_file)
        install = (
            ("/usr/bin/systemctl", "--user", "daemon-reload"),
            ("/usr/bin/systemctl", "--user", "enable", "--now", label),
        )
        status_argv = ("/usr/bin/systemctl", "--user", "is-active", label)
        restart_argv = ("/usr/bin/systemctl", "--user", "restart", label)
        uninstall = (
            ("/usr/bin/systemctl", "--user", "disable", "--now", label),
            ("/usr/bin/systemctl", "--user", "daemon-reload"),
        )
    else:
        label = _LAUNCHD_LABEL
        path = home / "Library" / "LaunchAgents" / f"{label}.plist"
        content = _render_launchd(arguments, layout)
        domain = f"gui/{uid}"
        target = f"{domain}/{label}"
        install = (("/bin/launchctl", "bootstrap", domain, str(path)),)
        status_argv = ("/bin/launchctl", "print", target)
        restart_argv = ("/bin/launchctl", "kickstart", "-k", target)
        uninstall = (("/bin/launchctl", "bootout", domain, str(path)),)
    return ServiceSpec(
        platform=platform,
        label=label,
        path=path,
        content=content,
        install_argvs=install,
        status_argv=status_argv,
        restart_argv=restart_argv,
        uninstall_argvs=uninstall,
    )


def service_install(
    spec: ServiceSpec,
    runner: CommandRunner | None = None,
    *,
    expected_sha256: str | None = None,
) -> str:
    """验证、原子发布并注册一个受管用户服务。

    Args:
        spec: render_service_spec 生成的 strict spec。
        runner: 可注入的 bounded exact runner；省略时使用 Task 8 runner。
        expected_sha256: 已有文件必须匹配的 prior receipt hash。

    Returns:
        新 service 文件的 lowercase SHA-256，供 install receipt 持有。

    Raises:
        InstallError: ownership、lint、publish、manager 或 health 检查失败。
    """
    _validate_service_spec(spec)
    selected = _runner(runner)
    owner = _preflight_owned(spec.path, expected_sha256)
    wanted = hashlib.sha256(spec.content).hexdigest()
    if owner is not None and owner.sha256 == wanted:
        if service_status(spec, selected):
            return wanted
        _run_install_and_health(spec, selected)
        return wanted

    home, uid = _spec_home_uid(spec)
    _ensure_private_parent(spec.path.parent, home, uid)
    if spec.platform is ServicePlatform.LAUNCHD:
        _ensure_private_parent(_launchd_log_parent(spec), home, uid)
    temporary, temporary_identity = _write_temporary(spec)
    quarantined: _QuarantinedPath | None = None
    published_identity: tuple[int, int] | None = None
    try:
        _validate_temporary(spec, temporary, selected)
        if owner is not None:
            quarantined = _quarantine_expected(
                spec.path,
                owner.identity,
                require_symlink=False,
            )
            if quarantined is None:
                _ownership_failed()
        try:
            os.link(temporary, spec.path, follow_symlinks=False)
        except FileExistsError:
            _ownership_failed()
        metadata = spec.path.lstat()
        published_identity = (metadata.st_dev, metadata.st_ino)
        if published_identity != temporary_identity or metadata.st_nlink != 2:
            _ownership_failed()
        _unlink_exact(temporary, temporary_identity)
        temporary_identity = None
        _fsync_directory(spec.path.parent)
        if managed_file_sha256(spec.path, expected_mode=0o600, require_symlink=False) != wanted:
            _ownership_failed()
        _run_install_and_health(spec, selected)
        if quarantined is not None and not quarantined.discard():
            _ownership_failed()
        return wanted
    except BaseException as error:
        if temporary_identity is not None:
            _unlink_exact(temporary, temporary_identity)
        _rollback_install(spec, selected, published_identity, quarantined)
        if isinstance(error, InstallError):
            raise
        raise InstallError("service_install_failed", "service") from None


def service_status(spec: ServiceSpec, runner: CommandRunner | None = None) -> bool:
    """查询用户服务是否处于 manager active 状态。

    Args:
        spec: strict user service spec。
        runner: 可注入 bounded runner。

    Returns:
        manager 返回零时为 True，合法非零状态为 False。

    Raises:
        InstallError: spec、runner 或 runner result 不可信。
    """
    _validate_service_spec(spec)
    result = _run(spec, _runner(runner), spec.status_argv)
    return result.returncode == 0


def service_logs(spec: ServiceSpec, runner: CommandRunner | None = None) -> CommandResult:
    """读取一次有界 user-service 日志。

    Args:
        spec: strict user service spec。
        runner: 可注入 bounded runner。

    Returns:
        Task 8 限制为 64 KiB 的 CommandResult。

    Raises:
        InstallError: manager/log 命令失败或 runner 不可信。
    """
    _validate_service_spec(spec)
    if spec.platform is ServicePlatform.SYSTEMD_USER:
        argv = ("/usr/bin/journalctl", "--user-unit", spec.label)
    else:
        stdout, stderr = _launchd_logs(spec)
        argv = ("/usr/bin/tail", "-n", "200", str(stdout), str(stderr))
    result = _run(spec, _runner(runner), argv)
    if result.returncode != 0:
        _service_failed()
    return result


def service_restart(spec: ServiceSpec, runner: CommandRunner | None = None) -> None:
    """以 exact manager argv 重启用户服务。

    Args:
        spec: strict user service spec。
        runner: 可注入 bounded runner。

    Raises:
        InstallError: manager 命令失败或 runner 不可信。
    """
    _validate_service_spec(spec)
    if _run(spec, _runner(runner), spec.restart_argv).returncode != 0:
        _service_failed()


def service_uninstall(
    spec: ServiceSpec,
    runner: CommandRunner | None = None,
    *,
    expected_sha256: str,
) -> None:
    """仅按 receipt hash 注销并删除受管 service 文件。

    Args:
        spec: strict user service spec。
        runner: 可注入 bounded runner。
        expected_sha256: install receipt 记录的 service file hash。

    Raises:
        InstallError: hash/identity 漂移、manager 命令或原子删除失败。
    """
    _validate_service_spec(spec)
    selected = _runner(runner)
    owner = _preflight_owned(spec.path, expected_sha256)
    if owner is None:
        _ownership_failed()
    first = spec.uninstall_argvs[0]
    if _run(spec, selected, first).returncode != 0:
        _service_failed()
    quarantined: _QuarantinedPath | None = None
    try:
        quarantined = _quarantine_expected(spec.path, owner.identity, require_symlink=False)
        if quarantined is None:
            _ownership_failed()
        for argv in spec.uninstall_argvs[1:]:
            if _run(spec, selected, argv).returncode != 0:
                _service_failed()
        if not quarantined.discard():
            _ownership_failed()
    except BaseException as error:
        if quarantined is not None:
            quarantined.restore()
        _best_effort_register(spec, selected)
        if isinstance(error, InstallError):
            raise
        raise InstallError("service_install_failed", "service") from None


def _render_systemd(arguments: tuple[str, ...], secrets_file: Path) -> bytes:
    """生成无 shell、无 root 字段且完成 specifier escaping 的 unit。"""
    for value in (*arguments, str(secrets_file)):
        _require_safe_text(value)
    executable = " ".join(_systemd_word(value) for value in arguments)
    environment = _systemd_word(f"MINICLAW_ENV_FILE={secrets_file}")
    return (
        "[Unit]\n"
        "Description=MiniClaw Gateway\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={executable}\n"
        f"Environment=PATH={_PATH}\n"
        f"Environment={environment}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "TimeoutStopSec=30\n"
        "UMask=0077\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    ).encode()


def _render_launchd(arguments: tuple[str, ...], layout: InstallLayout) -> bytes:
    """使用 plistlib 生成 exact ProgramArguments 与 owner log paths。"""
    for value in (*arguments, str(layout.secrets_file), str(layout.state_home)):
        _require_safe_text(value)
    document = {
        "EnvironmentVariables": {
            "MINICLAW_ENV_FILE": str(layout.secrets_file),
            "PATH": _PATH,
        },
        "KeepAlive": {"SuccessfulExit": False},
        "Label": _LAUNCHD_LABEL,
        "ProcessType": "Background",
        "ProgramArguments": list(arguments),
        "RunAtLoad": True,
        "StandardErrorPath": str(layout.state_home / "logs" / "gateway.stderr.log"),
        "StandardOutPath": str(layout.state_home / "logs" / "gateway.stdout.log"),
        "Umask": 0o077,
    }
    return plistlib.dumps(document, fmt=plistlib.FMT_XML, sort_keys=True)


def _validate_service_spec(spec: object) -> None:
    """重新解析 direct-constructor spec 并闭合所有派生关系。"""
    if type(spec) is not ServiceSpec:
        _service_failed()
    assert isinstance(spec, ServiceSpec)
    if (
        type(spec.platform) is not ServicePlatform
        or type(spec.label) is not str
        or not isinstance(spec.path, Path)
        or type(spec.content) is not bytes
        or not 1 <= len(spec.content) <= _MAX_SERVICE_BYTES
        or type(spec.install_argvs) is not tuple
        or type(spec.status_argv) is not tuple
        or type(spec.restart_argv) is not tuple
        or type(spec.uninstall_argvs) is not tuple
    ):
        _service_failed()
    _require_safe_path(spec.path)
    home, uid = _spec_home_uid(spec)
    if spec.platform is ServicePlatform.SYSTEMD_USER:
        if spec.label != _SYSTEMD_LABEL or spec.path != (
            home / ".config/systemd/user" / _SYSTEMD_LABEL
        ):
            _service_failed()
        arguments, secrets = _parse_systemd(spec.content)
        expected_install = (
            ("/usr/bin/systemctl", "--user", "daemon-reload"),
            ("/usr/bin/systemctl", "--user", "enable", "--now", spec.label),
        )
        expected_status = ("/usr/bin/systemctl", "--user", "is-active", spec.label)
        expected_restart = ("/usr/bin/systemctl", "--user", "restart", spec.label)
        expected_uninstall = (
            ("/usr/bin/systemctl", "--user", "disable", "--now", spec.label),
            ("/usr/bin/systemctl", "--user", "daemon-reload"),
        )
    else:
        if spec.label != _LAUNCHD_LABEL or spec.path != (
            home / "Library/LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"
        ):
            _service_failed()
        arguments, secrets = _parse_launchd(spec.content)
        domain = f"gui/{uid}"
        target = f"{domain}/{spec.label}"
        expected_install = (("/bin/launchctl", "bootstrap", domain, str(spec.path)),)
        expected_status = ("/bin/launchctl", "print", target)
        expected_restart = ("/bin/launchctl", "kickstart", "-k", target)
        expected_uninstall = (("/bin/launchctl", "bootout", domain, str(spec.path)),)
    if (
        spec.install_argvs != expected_install
        or spec.status_argv != expected_status
        or spec.restart_argv != expected_restart
        or spec.uninstall_argvs != expected_uninstall
        or len(arguments) != 4
        or arguments[1:] != ("gateway", "--home", arguments[3])
        or not Path(arguments[0]).is_absolute()
        or not Path(arguments[3]).is_absolute()
        or secrets != Path(arguments[3]) / "secrets.env"
    ):
        _service_failed()
    for argv in (*spec.install_argvs, spec.status_argv, spec.restart_argv, *spec.uninstall_argvs):
        _require_exact_argv(argv)


def _parse_systemd(content: bytes) -> tuple[tuple[str, ...], Path]:
    """解析 renderer 的 exact unit，并还原四个 ProgramArguments。"""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        _service_failed()
    lines = text.splitlines()
    if (
        len(lines) != 17
        or lines[:7]
        != [
            "[Unit]",
            "Description=MiniClaw Gateway",
            "After=network-online.target",
            "Wants=network-online.target",
            "",
            "[Service]",
            "Type=simple",
        ]
        or lines[8:]
        != [
            f"Environment=PATH={_PATH}",
            lines[9],
            "Restart=on-failure",
            "RestartSec=5",
            "TimeoutStopSec=30",
            "UMask=0077",
            "",
            "[Install]",
            "WantedBy=default.target",
        ]
        or not lines[7].startswith("ExecStart=")
        or not lines[9].startswith("Environment=")
    ):
        _service_failed()
    arguments = _systemd_split(lines[7].removeprefix("ExecStart="))
    environment = _systemd_split(lines[9].removeprefix("Environment="))
    if len(environment) != 1 or not environment[0].startswith("MINICLAW_ENV_FILE="):
        _service_failed()
    return arguments, Path(environment[0].split("=", 1)[1])


def _parse_launchd(content: bytes) -> tuple[tuple[str, ...], Path]:
    """解析 exact-key plist 并返回 ProgramArguments 与 Secret path。"""
    try:
        document = plistlib.loads(content)
    except (plistlib.InvalidFileException, ValueError, TypeError, OverflowError):
        _service_failed()
    keys = {
        "EnvironmentVariables",
        "KeepAlive",
        "Label",
        "ProcessType",
        "ProgramArguments",
        "RunAtLoad",
        "StandardErrorPath",
        "StandardOutPath",
        "Umask",
    }
    if type(document) is not dict or set(document) != keys:
        _service_failed()
    environment = document["EnvironmentVariables"]
    arguments = document["ProgramArguments"]
    if (
        type(environment) is not dict
        or set(environment) != {"MINICLAW_ENV_FILE", "PATH"}
        or environment["PATH"] != _PATH
        or type(environment["MINICLAW_ENV_FILE"]) is not str
        or type(arguments) is not list
        or len(arguments) != 4
        or any(type(value) is not str for value in arguments)
        or document["Label"] != _LAUNCHD_LABEL
        or document["KeepAlive"] != {"SuccessfulExit": False}
        or document["ProcessType"] != "Background"
        or document["RunAtLoad"] is not True
        or document["Umask"] != 0o077
        or document["StandardOutPath"] != f"{arguments[3]}/logs/gateway.stdout.log"
        or document["StandardErrorPath"] != f"{arguments[3]}/logs/gateway.stderr.log"
    ):
        _service_failed()
    return tuple(arguments), Path(environment["MINICLAW_ENV_FILE"])


def _systemd_word(value: str) -> str:
    """转义 systemd specifier，并仅在需要时双引号包裹一个 argv。"""
    escaped = value.replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    needs_quotes = any(character.isspace() or character in '\\"' for character in value)
    return f'"{escaped}"' if needs_quotes else escaped


def _systemd_split(value: str) -> tuple[str, ...]:
    """解析 renderer 支持的 systemd quoting 子集并拒绝未转义 specifier。"""
    try:
        words = shlex.split(value, posix=True)
    except ValueError:
        _service_failed()
    result: list[str] = []
    for word in words:
        if re.search(r"(?<!%)%(?!%)", word) is not None:
            _service_failed()
        result.append(word.replace("%%", "%"))
    return tuple(result)


def _layout_home(layout: InstallLayout, uid: int) -> Path:
    """从 user command link 或当前 target account 派生 service Home。"""
    if layout.command_link == Path("/usr/local/bin/miniclaw"):
        try:
            home = Path(pwd.getpwuid(uid).pw_dir)
        except KeyError:
            _service_failed()
    else:
        home = layout.command_link.parents[2]
    if layout.command_link != Path("/usr/local/bin/miniclaw") and layout.command_link != (
        home / ".local/bin/miniclaw"
    ):
        _service_failed()
    return home


def _spec_home_uid(spec: ServiceSpec) -> tuple[Path, int]:
    """从固定 service path 和当前 non-root target user 恢复 Home/UID。"""
    uid = os.geteuid()
    if type(uid) is not int or uid <= 0:
        _service_failed()
    if spec.platform is ServicePlatform.SYSTEMD_USER:
        if len(spec.path.parents) < 4:
            _service_failed()
        home = spec.path.parents[3]
    else:
        if len(spec.path.parents) < 3:
            _service_failed()
        home = spec.path.parents[2]
    _require_safe_path(home)
    return home, uid


def _manager_environment(spec: ServiceSpec) -> dict[str, str]:
    """返回不继承调用者变量且绑定 target user session 的 manager env。"""
    home, uid = _spec_home_uid(spec)
    environment = {
        "HOME": str(home),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": _PATH,
    }
    if spec.platform is ServicePlatform.SYSTEMD_USER:
        environment.update(
            {
                "DBUS_SESSION_BUS_ADDRESS": f"unix:path=/run/user/{uid}/bus",
                "XDG_CONFIG_HOME": str(home / ".config"),
                "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            }
        )
    return environment


def _preflight_owned(path: Path, expected_sha256: str | None) -> _OwnedFile | None:
    """在任何 service 写入/manager 调用前绑定 prior receipt 文件。"""
    exists = _lexists(path)
    if not exists:
        if expected_sha256 is not None:
            _ownership_failed()
        return None
    if type(expected_sha256) is not str or _HASH.fullmatch(expected_sha256) is None:
        _ownership_failed()
    verify_managed_file(
        path,
        expected_sha256,
        expected_mode=0o600,
        require_symlink=False,
    )
    content, identity = _read_owned(path)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        _ownership_failed()
    return _OwnedFile(identity, actual, content)


def _read_owned(path: Path) -> tuple[bytes, tuple[int, int]]:
    """no-follow 有界读取 owner-only 0600 service file 并绑定 inode。"""
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            _ownership_failed()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            _ownership_failed()
        payload = bytearray()
        while chunk := os.read(descriptor, min(65_536, _MAX_SERVICE_BYTES + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > _MAX_SERVICE_BYTES:
                _ownership_failed()
        after = path.lstat()
        if (
            (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or after.st_size != before.st_size
            or after.st_mtime_ns != before.st_mtime_ns
        ):
            _ownership_failed()
        return bytes(payload), (before.st_dev, before.st_ino)
    except InstallError:
        raise
    except OSError:
        _ownership_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_temporary(spec: ServiceSpec) -> tuple[Path, tuple[int, int]]:
    """在 service parent 内以 O_EXCL 0600 写入、fsync validator temp。"""
    temporary = spec.path.with_name(f".{spec.path.name}.{os.getpid()}.tmp")
    descriptor = -1
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if metadata.st_uid != os.geteuid() or not stat.S_ISREG(metadata.st_mode):
            _ownership_failed()
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, spec.content)
        os.fsync(descriptor)
        return temporary, identity
    except InstallError:
        if identity is not None:
            _unlink_exact(temporary, identity)
        raise
    except OSError:
        if identity is not None:
            _unlink_exact(temporary, identity)
        _service_failed()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_temporary(spec: ServiceSpec, temporary: Path, runner: CommandRunner) -> None:
    """先运行 fixed lint，再重验 temp identity/content/hash。"""
    if spec.platform is ServicePlatform.SYSTEMD_USER:
        argv = (
            (
                "/usr/bin/systemd-analyze",
                "--user",
                "verify",
                str(temporary),
            )
            if _systemd_analyze_available()
            else None
        )
    else:
        argv = ("/usr/bin/plutil", "-lint", str(temporary))
    if argv is not None and _run(spec, runner, argv).returncode != 0:
        _service_failed()
    if managed_file_sha256(temporary, expected_mode=0o600, require_symlink=False) != (
        hashlib.sha256(spec.content).hexdigest()
    ):
        _ownership_failed()


def _run_install_and_health(spec: ServiceSpec, runner: CommandRunner) -> None:
    """按 spec 固定顺序注册服务并立即查询 health。"""
    for argv in spec.install_argvs:
        if _run(spec, runner, argv).returncode != 0:
            _service_failed()
    if _run(spec, runner, spec.status_argv).returncode != 0:
        _service_failed()


def _run(spec: ServiceSpec, runner: CommandRunner, argv: tuple[str, ...]) -> CommandResult:
    """用 closed env/bounded runner 执行 exact argv，并隐藏所有原始错误。"""
    _require_exact_argv(argv)
    try:
        result = runner.run(
            argv,
            env=_manager_environment(spec),
            timeout=_TIMEOUT_SECONDS,
        )
    except BaseException:
        _service_failed()
    if type(result) is not CommandResult:
        _service_failed()
    return result


def _runner(runner: CommandRunner | None) -> CommandRunner:
    """返回现有 Task 8 bounded runner 或验证注入 runner。"""
    selected = _SubprocessRunner() if runner is None else runner
    if not callable(getattr(selected, "run", None)):
        _service_failed()
    return selected


def _rollback_install(
    spec: ServiceSpec,
    runner: CommandRunner,
    published_identity: tuple[int, int] | None,
    prior: _QuarantinedPath | None,
) -> None:
    """注册失败时仅移除新 inode，并恢复 prior file/manager。"""
    if published_identity is None and prior is None:
        return
    if published_identity is not None:
        current = _quarantine_expected(spec.path, published_identity, require_symlink=False)
        if current is not None:
            current.discard()
    restored = prior is not None and prior.restore()
    if restored:
        _best_effort_register(spec, runner)
    else:
        _best_effort_unregister(spec, runner)


def _best_effort_register(spec: ServiceSpec, runner: CommandRunner) -> None:
    """回滚文件后尽力恢复旧 manager registration，不传播 runner 输出。"""
    try:
        for argv in spec.install_argvs:
            _run(spec, runner, argv)
    except InstallError:
        pass


def _best_effort_unregister(spec: ServiceSpec, runner: CommandRunner) -> None:
    """删除首次安装文件后尽力清理 manager registration。"""
    try:
        for argv in spec.uninstall_argvs:
            _run(spec, runner, argv)
    except InstallError:
        pass


def _launchd_logs(spec: ServiceSpec) -> tuple[Path, Path]:
    """从已 strict 解析的 plist 返回 stdout/stderr 路径。"""
    document = plistlib.loads(spec.content)
    return Path(document["StandardOutPath"]), Path(document["StandardErrorPath"])


def _launchd_log_parent(spec: ServiceSpec) -> Path:
    """返回 exact 两个 LaunchAgent 日志共享的 owner-only parent。"""
    stdout, stderr = _launchd_logs(spec)
    if stdout.parent != stderr.parent:
        _service_failed()
    return stdout.parent


def _ensure_private_parent(path: Path, home: Path, uid: int) -> None:
    """创建/验证 Home 内 0700 parent，拒绝 symlink 与宽权限 ancestor。"""
    if path != home and not path.is_relative_to(home):
        _ownership_failed()
    _validate_home_chain(home, uid)
    current = home
    for part in path.relative_to(home).parts:
        current /= part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            try:
                current.mkdir(mode=0o700)
                current.chmod(0o700)
                _fsync_directory(current.parent)
                metadata = current.lstat()
            except OSError:
                _ownership_failed()
        except OSError:
            _ownership_failed()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            _ownership_failed()
    metadata = path.lstat()
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        _ownership_failed()


def _validate_home_chain(home: Path, uid: int) -> None:
    """从 filesystem root lstat Home，拒绝 symlink/可替换 ancestor。"""
    current = Path("/")
    candidates = [current]
    for part in home.parts[1:]:
        current /= part
        candidates.append(current)
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except OSError:
            _ownership_failed()
        mode = stat.S_IMODE(metadata.st_mode)
        sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, uid}
            or mode & 0o022
            and not sticky_root
        ):
            _ownership_failed()
    metadata = home.lstat()
    if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) & 0o022:
        _ownership_failed()


def _systemd_analyze_available() -> bool:
    """只接受固定 root-owned executable `/usr/bin/systemd-analyze`。"""
    try:
        metadata = Path("/usr/bin/systemd-analyze").lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and not stat.S_IMODE(metadata.st_mode) & 0o022
        and bool(stat.S_IMODE(metadata.st_mode) & 0o111)
    )


def _require_exact_argv(argv: object) -> None:
    """拒绝 shell 字符串、相对程序、控制字符与无界 argv。"""
    if (
        type(argv) is not tuple
        or not 1 <= len(argv) <= 16
        or not isinstance(argv[0], str)
        or not Path(argv[0]).is_absolute()
        or any(
            type(value) is not str or not 1 <= len(value) <= 4096 or not value.isprintable()
            for value in argv
        )
    ):
        _service_failed()


def _require_safe_path(path: Path) -> None:
    """拒绝相对、逃逸、控制字符和无界 service path。"""
    if (
        not isinstance(path, Path)
        or not path.is_absolute()
        or ".." in path.parts
        or len(str(path)) > 4096
        or not str(path).isprintable()
    ):
        _service_failed()


def _require_safe_text(value: str) -> None:
    """拒绝空值、NUL/newline/control 和无界 service 文本字段。"""
    if type(value) is not str or not 1 <= len(value) <= 4096 or not value.isprintable():
        _service_failed()


def _write_all(descriptor: int, payload: bytes) -> None:
    """完整写入 service bytes，并拒绝零进度。"""
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _unlink_exact(path: Path, identity: tuple[int, int]) -> None:
    """仅隔离并删除仍匹配调用方 inode 的目录项。"""
    quarantined = _quarantine_expected(path, identity, require_symlink=False)
    if quarantined is not None and not quarantined.discard():
        _ownership_failed()


def _lexists(path: Path) -> bool:
    """不跟随最终目录项判断 service path 是否存在。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _ownership_failed()
    return True


def _service_failed() -> Never:
    """抛出不含 path、输出或 Secret 的稳定 service error。"""
    raise InstallError("service_install_failed", "service")


def _ownership_failed() -> Never:
    """抛出不含 pathname 的稳定 receipt ownership error。"""
    raise InstallError("uninstall_ownership_mismatch", "manifest")
