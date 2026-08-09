"""定义受管 Runtime layout、stable launcher 与 install lock。"""

from __future__ import annotations

import hashlib
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Never, cast

from miniclaw.install.models import InstallError, InstallPlan, InstallRequest
from miniclaw.install.receipt import managed_file_sha256, verify_managed_file

_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_LOCK_KEYS = {"pid", "uid", "start"}
_MAX_LOCK_BYTES = 4096
_SYSTEM_PREFIX = Path("/usr/local/lib/miniclaw")
_SYSTEM_COMMAND = Path("/usr/local/bin/miniclaw")
ProcessState = Literal["alive", "dead", "unknown"]


@dataclass(frozen=True, slots=True)
class InstallLayout:
    """描述受管程序、共享状态与一个 staging Runtime 的路径。

    Args:
        program_prefix: immutable Runtime 和 stable launcher 的程序根。
        state_home: 配置、Secret、DB、Memory 与 Workspace 的用户状态根。
        bin_dir: stable launcher 目录。
        runtimes_dir: 版本化 immutable Runtime 目录。
        current: 指向当前 Runtime 的相对 symlink。
        staging: 当前版本构建 staging 目录。
        runtime: 当前目标版本最终 Runtime 目录。
        launcher: 不随版本变化的 POSIX launcher。
        command_link: PATH 上指向 stable launcher 的相对 symlink。
        receipt: owner-only install receipt。
        lock: O_EXCL install lock。
        secrets_file: launcher 传递给 Runtime 的 owner-only dotenv 路径。

    Raises:
        InstallError: 路径不是 canonical lexical absolute，过宽或派生关系不一致。
    """

    program_prefix: Path
    state_home: Path
    bin_dir: Path
    runtimes_dir: Path
    current: Path
    staging: Path
    runtime: Path
    launcher: Path
    command_link: Path
    receipt: Path
    lock: Path
    secrets_file: Path

    def __post_init__(self) -> None:
        """验证根路径与全部固定派生关系。"""
        _validate_lexical_root(self.program_prefix, allow_home=False)
        _validate_lexical_root(self.state_home, allow_home=False)
        if (
            self.bin_dir != self.program_prefix / "bin"
            or self.runtimes_dir != self.program_prefix / "runtimes"
            or self.current != self.program_prefix / "current"
            or self.launcher != self.bin_dir / "miniclaw"
            or self.receipt != self.program_prefix / "install-receipt.json"
            or self.lock != self.program_prefix / ".install.lock"
            or self.secrets_file != self.state_home / "secrets.env"
            or self.runtime.parent != self.runtimes_dir
            or self.staging.parent != self.runtimes_dir
            or self.staging.name != f".{self.runtime.name}.staging"
            or _SEMVER.fullmatch(self.runtime.name) is None
            or not _safe_absolute_path(self.command_link)
        ):
            _request_invalid()

    @classmethod
    def user(cls, home: Path, *, version: str) -> InstallLayout:
        """构造默认用户安装 layout。

        Args:
            home: 目标用户的 lexical absolute Home。
            version: 目标 Runtime 规范 SemVer。

        Returns:
            `~/.miniclaw` 程序/状态根和 `~/.local/bin/miniclaw` 命令路径。

        Raises:
            InstallError: Home、版本、现有路径 owner/type/mode 不安全。
        """
        if not _safe_absolute_path(home) or home == Path("/"):
            _request_invalid()
        uid = os.geteuid()
        _validate_existing_chain(home, home, uid)
        prefix = home / ".miniclaw"
        _validate_install_root(prefix, home, uid)
        return cls._build(prefix, prefix, home / ".local" / "bin" / "miniclaw", version)

    @classmethod
    def for_request(
        cls,
        request: InstallRequest,
        user: pwd.struct_passwd,
    ) -> InstallLayout:
        """从 canonical request 和已验证目标用户构造 layout。

        Args:
            request: Task 4 strict installer 请求。
            user: Task 5 解析并绑定的真实非 root passwd 记录。

        Returns:
            用户或 system-prefix layout。

        Raises:
            InstallError: 请求、用户、版本、路径、owner 或权限不安全。
        """
        if type(request) is not InstallRequest or type(user) is not pwd.struct_passwd:
            _request_invalid()
        home = Path(user.pw_dir)
        if (
            type(user.pw_uid) is not int
            or user.pw_uid <= 0
            or not _safe_absolute_path(home)
            or home == Path("/")
            or request.version is None
        ):
            raise InstallError("privilege_denied", "platform")
        try:
            home_owner = home.lstat().st_uid
        except OSError as error:
            raise InstallError("privilege_denied", "platform") from error
        if home_owner != user.pw_uid:
            raise InstallError("privilege_denied", "platform")
        _validate_existing_chain(home, home, user.pw_uid)
        state_home = request.state_home
        _validate_install_root(state_home, home, user.pw_uid)
        if request.system_prefix:
            _validate_system_prefix(_SYSTEM_PREFIX)
            return cls._build(
                _SYSTEM_PREFIX,
                state_home,
                _SYSTEM_COMMAND,
                request.version,
            )
        prefix = request.prefix if request.prefix is not None else home / ".miniclaw"
        _validate_install_root(prefix, home, user.pw_uid)
        return cls._build(
            prefix,
            state_home,
            home / ".local" / "bin" / "miniclaw",
            request.version,
        )

    @classmethod
    def for_plan(cls, plan: InstallPlan) -> InstallLayout:
        """从已确认 InstallPlan 构造目标 Release layout。

        Args:
            plan: Task 4/5 已校验、已绑定 manifest 的计划。

        Returns:
            使用 manifest version 的 immutable layout。

        Raises:
            InstallError: plan 类型或任一路径本地事实不安全。
        """
        if type(plan) is not InstallPlan:
            raise InstallError("plan_invalid", "model")
        system = plan.request.system_prefix
        if system:
            if plan.program_prefix != _SYSTEM_PREFIX:
                raise InstallError("plan_invalid", "program_prefix")
            _validate_system_prefix(plan.program_prefix)
            state_uid = _nearest_existing_uid(plan.state_home)
            if state_uid <= 0:
                raise InstallError("privilege_denied", "state_home")
            _validate_install_root(plan.state_home, _nearest_existing(plan.state_home), state_uid)
            command = _SYSTEM_COMMAND
        else:
            uid = os.geteuid()
            home = _common_user_anchor(plan.program_prefix, plan.state_home, uid)
            _validate_install_root(plan.program_prefix, home, uid)
            _validate_install_root(plan.state_home, home, uid)
            command = home / ".local" / "bin" / "miniclaw"
        return cls._build(
            plan.program_prefix,
            plan.state_home,
            command,
            plan.manifest.version,
        )

    @classmethod
    def _build(
        cls,
        program_prefix: Path,
        state_home: Path,
        command_link: Path,
        version: str,
    ) -> InstallLayout:
        """从已校验根路径派生全部固定 layout 字段。"""
        if type(version) is not str or _SEMVER.fullmatch(version) is None:
            _request_invalid()
        bin_dir = program_prefix / "bin"
        runtimes = program_prefix / "runtimes"
        return cls(
            program_prefix=program_prefix,
            state_home=state_home,
            bin_dir=bin_dir,
            runtimes_dir=runtimes,
            current=program_prefix / "current",
            staging=runtimes / f".{version}.staging",
            runtime=runtimes / version,
            launcher=bin_dir / "miniclaw",
            command_link=command_link,
            receipt=program_prefix / "install-receipt.json",
            lock=program_prefix / ".install.lock",
            secrets_file=state_home / "secrets.env",
        )


class InstallLock:
    """持有一个绑定创建 inode 与进程 identity 的 install lock。"""

    __slots__ = ("_closed", "_identity", "_path", "_payload")

    def __init__(self, path: Path, payload: bytes, identity: tuple[int, int]) -> None:
        """保存仅供 `acquire` 构造的 lock ownership 证据。"""
        self._path = path
        self._payload = payload
        self._identity = identity
        self._closed = False

    @classmethod
    def acquire(cls, layout: InstallLayout) -> InstallLock:
        """以 O_CREAT|O_EXCL 获取 install lock。

        Args:
            layout: 已通过 lexical/no-follow 校验的安装 layout。

        Returns:
            支持 context manager 且 close ownership-bound 的 lock。

        Raises:
            InstallError: layout、目录、已有 lock 或持久化不安全。
        """
        if type(layout) is not InstallLayout:
            raise InstallError("install_locked", "manifest")
        system = layout.program_prefix == _SYSTEM_PREFIX
        _ensure_directory(layout.program_prefix, 0o755 if system else 0o700, os.geteuid())
        for attempt in range(2):
            try:
                return cls._create(layout.lock)
            except FileExistsError:
                if attempt or not _remove_stale_lock(layout.lock):
                    raise InstallError("install_locked", "manifest") from None
            except InstallError:
                raise
            except OSError as error:
                raise InstallError("install_locked", "manifest") from error
        raise InstallError("install_locked", "manifest")

    @classmethod
    def _create(cls, path: Path) -> InstallLock:
        """创建、fsync 并返回一个新的 exact JSON lock。"""
        uid = os.geteuid()
        state, process_start = _probe_process(os.getpid())
        start = process_start if state == "alive" and process_start is not None else _utc_now()
        document = {"pid": os.getpid(), "uid": uid, "start": start}
        payload = (
            json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
        ).encode("utf-8")
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        identity: tuple[int, int] | None = None
        try:
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            if identity is not None:
                _unlink_same_inode(path, identity)
            raise
        os.close(descriptor)
        try:
            _fsync_directory(path.parent)
        except BaseException:
            if identity is not None:
                _unlink_same_inode(path, identity)
            raise
        return cls(path, payload, cast(tuple[int, int], identity))

    def close(self) -> None:
        """仅删除仍匹配创建 inode 和 exact payload 的 lock。"""
        if self._closed:
            return
        self._closed = True
        try:
            metadata = self._path.lstat()
            if (metadata.st_dev, metadata.st_ino) != self._identity:
                return
            if _read_lock_bytes(self._path, os.geteuid()) != self._payload:
                return
            self._path.unlink()
            _fsync_directory(self._path.parent)
        except (InstallError, OSError):
            return

    def __enter__(self) -> InstallLock:
        """返回当前已持有 lock。"""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object | None,
    ) -> None:
        """离开 context 时释放 ownership-bound lock。"""
        del exception_type, exception, traceback
        self.close()


def render_launcher(layout: InstallLayout) -> bytes:
    """生成只跟随 current 且不依赖 shell rc 的 POSIX launcher。

    Args:
        layout: 已校验的安装 layout。

    Returns:
        shell-quoted、保留 argv 边界的 UTF-8 executable script。

    Raises:
        InstallError: layout 类型不可信。
    """
    if type(layout) is not InstallLayout:
        _request_invalid()
    prefix = shlex.quote(str(layout.program_prefix))
    home = shlex.quote(str(layout.state_home))
    return (
        "#!/bin/sh\nset -eu\n"
        f"MINICLAW_PREFIX={prefix}\nMINICLAW_HOME={home}\n"
        'MINICLAW_NODE="$MINICLAW_PREFIX/current/node/bin/node"\n'
        'MINICLAW_TUI_ENTRY="$MINICLAW_PREFIX/current/tui/dist/main.js"\n'
        'MINICLAW_ENV_FILE="$MINICLAW_HOME/secrets.env"\n'
        "export MINICLAW_HOME MINICLAW_NODE MINICLAW_TUI_ENTRY MINICLAW_ENV_FILE\n"
        'exec "$MINICLAW_PREFIX/current/venv/bin/python" -m miniclaw "$@"\n'
    ).encode()


def install_launcher(
    layout: InstallLayout,
    *,
    launcher_sha256: str | None = None,
    command_link_sha256: str | None = None,
) -> tuple[str, str]:
    """写入 stable launcher 和 PATH 上的相对 command symlink。

    Args:
        layout: 已校验安装 layout。
        launcher_sha256: 已有 launcher 的 prior receipt hash。
        command_link_sha256: 已有 command link 的 prior receipt hash。

    Returns:
        新 launcher 和 command link 的 ownership hash。

    Raises:
        InstallError: 已有文件无 receipt、类型错误、hash 漂移或写入失败。
    """
    if type(layout) is not InstallLayout:
        _ownership_invalid()
    _preflight_managed(layout.launcher, launcher_sha256, require_symlink=False)
    _preflight_managed(layout.command_link, command_link_sha256, require_symlink=True)
    created_launcher = False
    try:
        _ensure_directory(layout.bin_dir, 0o700, os.geteuid())
        _ensure_directory(layout.command_link.parent, 0o700, os.geteuid())
        payload = render_launcher(layout)
        if not _lexists(layout.launcher):
            _create_regular(layout.launcher, payload, 0o700)
            created_launcher = True
        else:
            _preflight_managed(layout.launcher, launcher_sha256, require_symlink=False)
            if managed_file_sha256(layout.launcher) != hashlib.sha256(payload).hexdigest():
                _replace_regular(layout.launcher, payload, 0o700)
        relative_target = os.path.relpath(layout.launcher, start=layout.command_link.parent)
        if not _lexists(layout.command_link):
            os.symlink(relative_target, layout.command_link)
            _fsync_directory(layout.command_link.parent)
        else:
            _preflight_managed(
                layout.command_link,
                command_link_sha256,
                require_symlink=True,
            )
            if os.readlink(layout.command_link) != relative_target:
                _replace_symlink(layout.command_link, relative_target)
        return (
            managed_file_sha256(layout.launcher),
            managed_file_sha256(layout.command_link),
        )
    except InstallError:
        if created_launcher:
            try:
                layout.launcher.unlink()
            except OSError:
                pass
        raise
    except OSError as error:
        if created_launcher:
            try:
                layout.launcher.unlink()
            except OSError:
                pass
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def _preflight_managed(path: Path, expected: str | None, *, require_symlink: bool) -> None:
    """在任何写入前验证 existing path 由 prior receipt 持有且类型正确。"""
    exists = _lexists(path)
    if not exists:
        if expected is not None:
            _ownership_invalid()
        return
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error
    if require_symlink != stat.S_ISLNK(metadata.st_mode):
        _ownership_invalid()
    if not require_symlink and not stat.S_ISREG(metadata.st_mode):
        _ownership_invalid()
    if expected is None:
        _ownership_invalid()
    verify_managed_file(path, expected)


def _remove_stale_lock(path: Path) -> bool:
    """仅删除同 UID 且进程 confirmed dead 或 PID start 已变化的 lock。"""
    uid = os.geteuid()
    try:
        before = path.lstat()
        payload = _read_lock_bytes(path, uid)
        document = json.loads(payload.decode("utf-8"), object_pairs_hook=_strict_object)
        if type(document) is not dict or set(document) != _LOCK_KEYS:
            return False
        pid = document["pid"]
        lock_uid = document["uid"]
        start = document["start"]
        if (
            type(pid) is not int
            or pid <= 0
            or type(lock_uid) is not int
            or lock_uid != uid
            or type(start) is not str
            or not _valid_utc(start)
        ):
            return False
        state, current_start = _probe_process(pid)
        stale = state == "dead" or (
            state == "alive" and current_start is not None and current_start != start
        )
        if not stale:
            return False
        after = path.lstat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            return False
        if _read_lock_bytes(path, uid) != payload:
            return False
        path.unlink()
        _fsync_directory(path.parent)
        return True
    except (InstallError, OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return False


def _read_lock_bytes(path: Path, expected_uid: int) -> bytes:
    """no-follow 读取 expected owner 的 exact 0600 bounded lock。"""
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise InstallError("install_locked", "manifest")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != expected_uid
                or stat.S_IMODE(opened.st_mode) != 0o600
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                or opened.st_size > _MAX_LOCK_BYTES
            ):
                raise InstallError("install_locked", "manifest")
            chunks: list[bytes] = []
            remaining = _MAX_LOCK_BYTES + 1
            while remaining and (chunk := os.read(descriptor, min(remaining, 4096))):
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > _MAX_LOCK_BYTES:
                raise InstallError("install_locked", "manifest")
            return payload
        finally:
            os.close(descriptor)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("install_locked", "manifest") from error


def _probe_process(pid: int) -> tuple[ProcessState, str | None]:
    """返回 PID liveness 和可用时的 UTC process start identity。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead", None
    except PermissionError:
        return "unknown", None
    except OSError:
        return "unknown", None
    start = _linux_process_start(pid)
    if start is None:
        start = _ps_process_start(pid)
    return "alive", start


def _linux_process_start(pid: int) -> str | None:
    """从 Linux procfs 读取 PID start ticks 并转换为 UTC。"""
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        tail = stat_text[stat_text.rindex(")") + 2 :].split()
        start_ticks = int(tail[19])
        boot_line = next(
            line for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
            if line.startswith("btime ")
        )
        boot = int(boot_line.split()[1])
        ticks = os.sysconf("SC_CLK_TCK")
        if type(ticks) is not int or ticks <= 0:
            return None
        return _format_utc(datetime.fromtimestamp(boot + start_ticks / ticks, UTC))
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _ps_process_start(pid: int) -> str | None:
    """在无 procfs 平台用固定 `/bin/ps` exact argv 读取 process start。"""
    try:
        result = subprocess.run(
            ("/bin/ps", "-o", "lstart=", "-p", str(pid)),
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"},
        )
        if result.returncode != 0:
            return None
        parsed = datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y")
        local_timezone = datetime.now().astimezone().tzinfo
        if local_timezone is None:
            return None
        return _format_utc(parsed.replace(tzinfo=local_timezone).astimezone(UTC))
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _format_utc(value: datetime) -> str:
    """把 timezone-aware datetime 规范成秒级 UTC RFC3339。"""
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc_now() -> str:
    """返回当前秒级 UTC RFC3339 timestamp。"""
    return _format_utc(datetime.now(UTC))


def _valid_utc(value: str) -> bool:
    """判断 timestamp 是否为真实 UTC RFC3339。"""
    if _UTC.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0)


def _validate_install_root(path: Path, home: Path, expected_uid: int) -> None:
    """拒绝 broad/Home 根并校验 home 锚点内的 no-follow owner chain。"""
    _validate_lexical_root(path, allow_home=False)
    if path == home:
        _request_invalid()
    anchor = home if path.is_relative_to(home) else _nearest_existing(path)
    _validate_existing_chain(path, anchor, expected_uid)


def _validate_system_prefix(path: Path) -> None:
    """校验固定 system prefix 的现有 root-owned no-follow chain。"""
    if path != _SYSTEM_PREFIX:
        _request_invalid()
    _validate_existing_chain(path, Path("/"), 0, allow_root=True)


def _validate_existing_chain(
    path: Path,
    anchor: Path,
    expected_uid: int,
    *,
    allow_root: bool = False,
) -> None:
    """用 lstat 校验 anchor 到 path 的现有 directory components。"""
    if not _safe_absolute_path(path) or not _safe_absolute_path(anchor):
        _request_invalid()
    if path != anchor and not path.is_relative_to(anchor):
        _request_invalid()
    relative = path.relative_to(anchor)
    candidates = [anchor]
    current = anchor
    for part in relative.parts:
        current /= part
        candidates.append(current)
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _request_invalid()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or metadata.st_uid != expected_uid
            and not (allow_root and metadata.st_uid == 0)
        ):
            _request_invalid()


def _ensure_directory(path: Path, mode: int, expected_uid: int) -> None:
    """创建或校验 owner、no-follow 且不可被 group/world 写的目录链。"""
    if _lexists(path):
        try:
            metadata = path.lstat()
        except OSError as error:
            raise InstallError("uninstall_ownership_mismatch", "manifest") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            _ownership_invalid()
        return
    parent = path.parent
    if parent != path:
        _ensure_directory(parent, mode, expected_uid)
    try:
        path.mkdir(mode=mode)
        path.chmod(mode)
    except OSError as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def _create_regular(path: Path, payload: bytes, mode: int) -> None:
    """用 O_EXCL 创建、fsync 一个 regular managed file。"""
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        mode,
    )
    try:
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _replace_regular(path: Path, payload: bytes, mode: int) -> None:
    """通过同目录 O_EXCL temp 原子替换已验证 managed regular file。"""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        try:
            os.fchmod(descriptor, mode)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        identity = None
        _fsync_directory(path.parent)
    except BaseException:
        if identity is not None:
            _unlink_same_inode(temporary, identity)
        raise


def _replace_symlink(path: Path, target: str) -> None:
    """通过同目录临时 symlink 原子替换已验证 command link。"""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    os.symlink(target, temporary)
    try:
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            if temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
        raise


def _write_all(descriptor: int, payload: bytes) -> None:
    """完整写入 payload，拒绝零进度。"""
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    """fsync no-follow directory 以持久化目录项。"""
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_same_inode(path: Path, identity: tuple[int, int]) -> None:
    """仅清理仍匹配调用方创建 inode 的临时目录项。"""
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity:
            path.unlink()
    except OSError:
        pass


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造拒绝 duplicate key 的 lock JSON object。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _nearest_existing(path: Path) -> Path:
    """返回 lexical path 最近的 existing no-follow ancestor。"""
    current = path
    while True:
        try:
            current.lstat()
            return current
        except FileNotFoundError:
            if current == current.parent:
                _request_invalid()
            current = current.parent
        except OSError:
            _request_invalid()


def _nearest_existing_uid(path: Path) -> int:
    """返回最近 existing ancestor 的 UID。"""
    try:
        return _nearest_existing(path).lstat().st_uid
    except OSError:
        _request_invalid()


def _common_user_anchor(program_prefix: Path, state_home: Path, uid: int) -> Path:
    """为 user plan 选择同时包含 program/state 且由当前 owner 持有的最近锚点。"""
    program_ancestor = _nearest_existing(program_prefix)
    state_ancestor = _nearest_existing(state_home)
    candidates = (program_ancestor, *program_ancestor.parents)
    anchor = next(
        (candidate for candidate in candidates if state_ancestor.is_relative_to(candidate)),
        None,
    )
    if anchor is None:
        _request_invalid()
    metadata = anchor.lstat()
    if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) & 0o022:
        _request_invalid()
    return anchor


def _validate_lexical_root(path: object, *, allow_home: bool) -> None:
    """拒绝非 Path、相对、`..`、控制字符、`/` 和可选 Home root。"""
    if not _safe_absolute_path(path) or path == Path("/"):
        _request_invalid()
    if not allow_home and path == Path.home():
        _request_invalid()


def _safe_absolute_path(path: object) -> bool:
    """判断路径是否为 bounded canonical lexical absolute Path。"""
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and ".." not in path.parts
        and len(str(path)) <= 4096
        and all(character.isprintable() for character in str(path))
    )


def _lexists(path: Path) -> bool:
    """不跟随 symlink 判断目录项是否存在。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _ownership_invalid()
    return True


def _request_invalid() -> Never:
    """抛出不包含本地路径的稳定 request error。"""
    raise InstallError("request_invalid", "prefix")


def _ownership_invalid() -> Never:
    """抛出不包含本地路径的稳定 ownership mismatch。"""
    raise InstallError("uninstall_ownership_mismatch", "manifest")
