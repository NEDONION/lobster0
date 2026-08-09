"""管理 MiniClaw 自有的用户级 macOS LaunchAgent。"""

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_LABEL = "io.miniclaw.gateway"
_MAX_MANAGED_BYTES = 128 * 1024
_Runner = Callable[[tuple[str, ...]], subprocess.CompletedProcess[bytes]]


class ServiceError(RuntimeError):
    """表示 service 输入、ownership 或 launchd lifecycle 失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        """只公开稳定错误码，丢弃可能包含本机路径的底层说明。"""
        del detail
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ServiceSpec:
    """保存一个受管 LaunchAgent 的 exact 文件与 ownership receipt。"""

    label: str
    path: Path
    receipt_path: Path
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        """拒绝非固定 label、相对路径、空内容和损坏摘要。"""
        if (
            self.label != _LABEL
            or not self.path.is_absolute()
            or not self.receipt_path.is_absolute()
            or self.path == self.receipt_path
            or not self.content
            or len(self.content) > _MAX_MANAGED_BYTES
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
            or hashlib.sha256(self.content).hexdigest() != self.sha256
            or any(_has_control(str(path)) for path in (self.path, self.receipt_path))
        ):
            raise ServiceError("service_spec_invalid")


@dataclass(frozen=True, slots=True)
class ServiceStatus:
    """保存不含 PID、路径和 launchctl 原始输出的服务状态。"""

    installed: bool
    loaded: bool
    running: bool


def render_launchd_service(
    *,
    launcher: Path,
    state_home: Path,
    working_directory: Path,
    launch_agents: Path,
    dotenv_path: Path,
) -> ServiceSpec:
    """渲染固定 label/argv 的 MiniClaw LaunchAgent。

    Args:
        launcher: managed runtime 中的 ``miniclaw`` console launcher。
        state_home: Gateway 使用的绝对状态根。
        working_directory: 启动时不变化的项目或安装目录。
        launch_agents: 当前用户的 LaunchAgents 目录。
        dotenv_path: owner-only dotenv 文件；plist 只保存路径，不复制内容。

    Returns:
        包含 deterministic plist bytes、目标路径与摘要的服务规范。

    Raises:
        ServiceError: 任一路径相对、包含控制字符或相互关系不安全。
    """
    paths = (launcher, state_home, working_directory, launch_agents, dotenv_path)
    if any(not isinstance(path, Path) or not path.is_absolute() for path in paths) or any(
        _has_control(str(path)) for path in paths
    ):
        raise ServiceError("service_spec_invalid")
    logs = state_home / "logs"
    payload = {
        "EnvironmentVariables": {"MINICLAW_ENV_FILE": str(dotenv_path)},
        "KeepAlive": {"SuccessfulExit": False},
        "Label": _LABEL,
        "ProgramArguments": [
            str(launcher),
            "gateway",
            "--home",
            str(state_home),
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(logs / "gateway.stderr.log"),
        "StandardOutPath": str(logs / "gateway.stdout.log"),
        "ThrottleInterval": 10,
        "WorkingDirectory": str(working_directory),
    }
    content = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    return ServiceSpec(
        label=_LABEL,
        path=launch_agents / f"{_LABEL}.plist",
        receipt_path=state_home / "run" / "launchd-service.json",
        content=content,
        sha256=hashlib.sha256(content).hexdigest(),
    )


class LaunchdService:
    """只安装、查询、重启和删除 MiniClaw 自己拥有的 LaunchAgent。"""

    def __init__(
        self,
        spec: ServiceSpec,
        *,
        runner: _Runner | None = None,
        uid: int | None = None,
        platform: str | None = None,
    ) -> None:
        """绑定服务规范与可测试的 exact command runner。"""
        self._spec = spec
        self._runner = runner or _run_command
        self._uid = os.getuid() if uid is None else uid
        self._platform = os.uname().sysname.lower() if platform is None else platform

    def install(self) -> None:
        """校验、原子写入并 bootstrap 受管 LaunchAgent。

        Returns:
            安装成功或已健康运行时返回 ``None``。

        Raises:
            ServiceError: 平台、文件 ownership、plist 校验或 launchctl 操作失败。
        """
        self._validate_runtime()
        self._prepare_directories()
        current = self._managed_content()
        if current == self._spec.content:
            status = self.status()
            if status.loaded:
                if not status.running:
                    self.restart()
                return
            self._require_success(self._bootstrap_argv())
            return

        self._lint()
        old_receipt = (
            self._spec.receipt_path.read_bytes()
            if current is not None and self._spec.receipt_path.is_file()
            else None
        )
        old_loaded = False
        if current is not None:
            old_loaded = self.status().loaded
            if old_loaded:
                self._require_success(self._bootout_argv())
        try:
            _write_private(self._spec.path, self._spec.content)
            _write_private(self._spec.receipt_path, self._receipt_bytes())
            self._require_success(self._bootstrap_argv())
        except (OSError, ServiceError):
            self._restore(current, old_receipt, old_loaded)
            raise ServiceError("service_manager_failed") from None

    def status(self) -> ServiceStatus:
        """返回受管 plist 与 launchd job 的封闭状态。

        Returns:
            不含 PID、路径或 manager stdout 的 installed/loaded/running 状态。

        Raises:
            ServiceError: 平台不支持或已有同名文件不属于 MiniClaw。
        """
        self._validate_platform()
        current = self._managed_content()
        if current is None:
            return ServiceStatus(False, False, False)
        result = self._invoke(self._print_argv())
        if result.returncode != 0:
            return ServiceStatus(True, False, False)
        running = re.search(rb"(?m)^\s*state\s*=\s*running\s*$", result.stdout) is not None
        return ServiceStatus(True, True, running)

    def restart(self) -> None:
        """要求 launchd 重启同一固定 label 的已安装 job。

        Returns:
            kickstart 成功时返回 ``None``。

        Raises:
            ServiceError: 服务未安装、文件非受管或 launchctl 失败。
        """
        self._validate_runtime()
        if self._managed_content() is None:
            raise ServiceError("service_not_installed")
        self._require_success(self._kickstart_argv())

    def uninstall(self) -> None:
        """bootout 并删除匹配 ownership receipt 的服务文件。

        Returns:
            成功删除或本就不存在时返回 ``None``。

        Raises:
            ServiceError: 同名文件不受管或 launchctl 拒绝 bootout。
        """
        self._validate_platform()
        current = self._managed_content()
        if current is None:
            self._remove_orphan_receipt()
            return
        status = self.status()
        if status.loaded:
            self._require_success(self._bootout_argv())
        try:
            self._spec.path.unlink()
            self._spec.receipt_path.unlink(missing_ok=True)
        except OSError:
            raise ServiceError("service_write_failed") from None

    def _validate_platform(self) -> None:
        """拒绝 root、非 macOS 和非法 UID。"""
        if self._platform != "darwin" or type(self._uid) is not int or self._uid <= 0:
            raise ServiceError("service_platform_unsupported")

    def _validate_runtime(self) -> None:
        """确认 launcher 是当前用户可执行的非 symlink 普通文件。"""
        self._validate_platform()
        try:
            launcher = Path(
                plistlib.loads(self._spec.content)["ProgramArguments"][0]
            )
            info = launcher.lstat()
        except (OSError, KeyError, TypeError, ValueError, plistlib.InvalidFileException):
            raise ServiceError("service_runtime_invalid") from None
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != self._uid
            or not info.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            or launcher.resolve(strict=True) != launcher
        ):
            raise ServiceError("service_runtime_invalid")

    def _prepare_directories(self) -> None:
        """创建并收紧日志、receipt 与 LaunchAgents 父目录。"""
        for path, private in (
            (self._spec.path.parent, False),
            (self._spec.receipt_path.parent, True),
            (self._spec.receipt_path.parent.parent / "logs", True),
        ):
            try:
                if path.is_symlink():
                    raise ServiceError("service_directory_unsafe")
                path.mkdir(mode=0o700 if private else 0o755, parents=True, exist_ok=True)
                info = path.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid != self._uid:
                    raise ServiceError("service_directory_unsafe")
                if private:
                    path.chmod(0o700)
            except ServiceError:
                raise
            except OSError:
                raise ServiceError("service_directory_unsafe") from None

    def _managed_content(self) -> bytes | None:
        """读取已安装内容，并要求 owner/mode/receipt/hash 全部匹配。"""
        if not self._spec.path.exists() and not self._spec.path.is_symlink():
            return None
        try:
            content = _read_private(self._spec.path, self._uid)
            receipt = json.loads(
                _read_private(self._spec.receipt_path, self._uid).decode("utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ServiceError):
            raise ServiceError("service_file_unmanaged") from None
        expected = {
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "label": self._spec.label,
            "path": str(self._spec.path),
            "schema_version": 1,
        }
        if receipt != expected:
            raise ServiceError("service_file_unmanaged")
        return content

    def _receipt_bytes(self) -> bytes:
        """返回只含 label/path/hash 的 deterministic ownership receipt。"""
        return (
            json.dumps(
                {
                    "content_sha256": self._spec.sha256,
                    "label": self._spec.label,
                    "path": str(self._spec.path),
                    "schema_version": 1,
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    def _lint(self) -> None:
        """在替换目标 plist 前用 owner-only 临时文件执行 plutil lint。"""
        descriptor, name = tempfile.mkstemp(
            prefix=".miniclaw-launchd-",
            suffix=".plist",
            dir=self._spec.path.parent,
        )
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self._spec.content)
                stream.flush()
                os.fsync(stream.fileno())
            self._require_success(("/usr/bin/plutil", "-lint", str(temporary)))
        finally:
            temporary.unlink(missing_ok=True)

    def _restore(
        self,
        content: bytes | None,
        receipt: bytes | None,
        was_loaded: bool,
    ) -> None:
        """回滚 manager 失败前的受管文件与 loaded 状态。"""
        try:
            if content is None or receipt is None:
                self._spec.path.unlink(missing_ok=True)
                self._spec.receipt_path.unlink(missing_ok=True)
                return
            _write_private(self._spec.path, content)
            _write_private(self._spec.receipt_path, receipt)
            if was_loaded:
                self._invoke(self._bootstrap_argv())
        except OSError:
            return

    def _remove_orphan_receipt(self) -> None:
        """在 plist 已不存在时删除 owner-only、同 label/path 的残余 receipt。"""
        if not self._spec.receipt_path.exists():
            return
        try:
            receipt = json.loads(
                _read_private(self._spec.receipt_path, self._uid).decode("utf-8")
            )
            if (
                not isinstance(receipt, dict)
                or receipt.get("label") != self._spec.label
                or receipt.get("path") != str(self._spec.path)
            ):
                raise ServiceError("service_file_unmanaged")
            self._spec.receipt_path.unlink()
        except ServiceError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ServiceError("service_file_unmanaged") from None

    def _invoke(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        """执行 exact argv 并把 runner 异常收窄为稳定错误。"""
        try:
            result = self._runner(argv)
        except (OSError, subprocess.SubprocessError):
            raise ServiceError("service_manager_failed") from None
        if not isinstance(result, subprocess.CompletedProcess):
            raise ServiceError("service_manager_failed")
        return result

    def _require_success(self, argv: tuple[str, ...]) -> None:
        """要求 exact manager 命令零退出，且不公开 stdout/stderr。"""
        if self._invoke(argv).returncode != 0:
            raise ServiceError("service_manager_failed")

    def _print_argv(self) -> tuple[str, ...]:
        """返回查询固定 label 的 exact argv。"""
        return ("/bin/launchctl", "print", f"gui/{self._uid}/{self._spec.label}")

    def _bootstrap_argv(self) -> tuple[str, ...]:
        """返回加载固定 plist 的 exact argv。"""
        return (
            "/bin/launchctl",
            "bootstrap",
            f"gui/{self._uid}",
            str(self._spec.path),
        )

    def _kickstart_argv(self) -> tuple[str, ...]:
        """返回强制重启固定 label 的 exact argv。"""
        return (
            "/bin/launchctl",
            "kickstart",
            "-k",
            f"gui/{self._uid}/{self._spec.label}",
        )

    def _bootout_argv(self) -> tuple[str, ...]:
        """返回卸载固定 plist 的 exact argv。"""
        return (
            "/bin/launchctl",
            "bootout",
            f"gui/{self._uid}",
            str(self._spec.path),
        )


def _run_command(argv: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
    """运行固定系统命令，并有界捕获不会直接公开的输出。"""
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        timeout=15,
    )


def _write_private(path: Path, content: bytes) -> None:
    """以临时文件、0600、fsync 和 atomic replace 写受管内容。"""
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _read_private(path: Path, uid: int) -> bytes:
    """读取 owner-only、no-follow 且有界的受管普通文件。"""
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != uid
        or stat.S_IMODE(info.st_mode) & 0o077
        or not 0 <= info.st_size <= _MAX_MANAGED_BYTES
    ):
        raise ServiceError("service_file_unmanaged")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ServiceError("service_file_unmanaged")
        content = os.read(descriptor, _MAX_MANAGED_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_MANAGED_BYTES:
        raise ServiceError("service_file_unmanaged")
    return content


def _has_control(value: str) -> bool:
    """识别会让 plist/path 产生歧义的控制字符。"""
    return any(ord(character) < 32 or ord(character) == 127 for character in value)
