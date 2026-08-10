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
import tempfile
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Never, cast

from miniclaw.install.models import InstallError, InstallPlan, InstallRequest
from miniclaw.install.platforms import _production_identity, _resolve_invoking_user
from miniclaw.install.receipt import (
    _quarantine_expected,
    managed_file_sha256,
    verify_managed_file,
)

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
class _LockOwnership:
    """保存仅由 lock 创建路径登记的 immutable ownership 事实。"""

    path: Path
    payload: bytes
    identity: tuple[int, int]
    descriptor: int
    proof_descriptor: int
    pid: int
    uid: int
    start: str
    finalizer: weakref.finalize


@dataclass(frozen=True, slots=True)
class _ManagedToken:
    """绑定 preflight 后受管目录项的 inode、类型与 receipt hash。"""

    identity: tuple[int, int]
    sha256: str
    require_symlink: bool
    expected_mode: int | None


@dataclass(frozen=True, slots=True, init=False)
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
        if self.program_prefix == _SYSTEM_PREFIX:
            if self.command_link != _SYSTEM_COMMAND:
                _request_invalid()
        elif self.command_link.parts[-3:] != (".local", "bin", "miniclaw"):
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
        prefix = home / ".miniclaw"
        return cls._build(
            prefix,
            prefix,
            home / ".local" / "bin" / "miniclaw",
            version,
            owner_uid=uid,
            user_home=home,
        )

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
        state_home = request.state_home
        if request.system_prefix:
            return cls._build(
                _SYSTEM_PREFIX,
                state_home,
                _SYSTEM_COMMAND,
                request.version,
                owner_uid=user.pw_uid,
                user_home=home,
            )
        prefix = request.prefix if request.prefix is not None else home / ".miniclaw"
        return cls._build(
            prefix,
            state_home,
            home / ".local" / "bin" / "miniclaw",
            request.version,
            owner_uid=user.pw_uid,
            user_home=home,
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
        effective_uid, original_user, original_uid = _production_identity()
        account = _resolve_invoking_user(effective_uid, original_user, original_uid)
        uid = account.pw_uid
        home = Path(account.pw_dir)
        system = plan.request.system_prefix
        if system:
            if plan.program_prefix != _SYSTEM_PREFIX:
                raise InstallError("plan_invalid", "program_prefix")
            command = _SYSTEM_COMMAND
        else:
            command = home / ".local" / "bin" / "miniclaw"
        return cls._build(
            plan.program_prefix,
            plan.state_home,
            command,
            plan.manifest.version,
            owner_uid=uid,
            user_home=home,
        )

    @classmethod
    def _build(
        cls,
        program_prefix: Path,
        state_home: Path,
        command_link: Path,
        version: str,
        *,
        owner_uid: int | None = None,
        user_home: Path | None = None,
    ) -> InstallLayout:
        """完整校验 owner/Home/no-follow 事实后派生全部固定字段。"""
        if type(version) is not str or _SEMVER.fullmatch(version) is None:
            _request_invalid()
        uid = os.geteuid() if owner_uid is None else owner_uid
        if type(uid) is not int or uid < 0:
            _request_invalid()
        system = program_prefix == _SYSTEM_PREFIX
        if user_home is None:
            if system:
                try:
                    user_home = Path(pwd.getpwuid(uid).pw_dir)
                except KeyError as error:
                    raise InstallError("privilege_denied", "state_home") from error
            elif command_link.parts[-3:] == (".local", "bin", "miniclaw"):
                user_home = command_link.parents[2]
            else:
                _request_invalid()
        if not _safe_absolute_path(user_home) or user_home == Path("/"):
            _request_invalid()
        _validate_trusted_chain(user_home, uid)
        try:
            home_owner = user_home.lstat().st_uid
        except FileNotFoundError:
            if system:
                raise InstallError("privilege_denied", "state_home") from None
            home_anchor = _nearest_existing(user_home)
            _validate_existing_chain(user_home, home_anchor, uid)
        except OSError as error:
            raise InstallError("privilege_denied", "state_home") from error
        else:
            if home_owner != uid:
                raise InstallError("privilege_denied", "state_home")
            _validate_existing_chain(user_home, user_home, uid)
        _validate_install_root(state_home, user_home, uid)
        if system:
            _validate_system_prefix(program_prefix)
            if command_link != _SYSTEM_COMMAND:
                _request_invalid()
        else:
            _validate_install_root(program_prefix, user_home, uid)
            if command_link != user_home / ".local" / "bin" / "miniclaw":
                _request_invalid()
        bin_dir = program_prefix / "bin"
        runtimes = program_prefix / "runtimes"
        values = {
            "program_prefix": program_prefix,
            "state_home": state_home,
            "bin_dir": bin_dir,
            "runtimes_dir": runtimes,
            "current": program_prefix / "current",
            "staging": runtimes / f".{version}.staging",
            "runtime": runtimes / version,
            "launcher": bin_dir / "miniclaw",
            "command_link": command_link,
            "receipt": program_prefix / "install-receipt.json",
            "lock": program_prefix / ".install.lock",
            "secrets_file": state_home / "secrets.env",
        }
        instance = object.__new__(cls)
        for name, value in values.items():
            object.__setattr__(instance, name, value)
        instance.__post_init__()
        return instance


class InstallLock:
    """持有一个绑定创建 inode 与进程 identity 的 install lock。"""

    __slots__ = ("_closed", "_descriptor", "_identity", "_path", "_payload", "__weakref__")

    def __init__(
        self,
        path: Path,
        payload: bytes,
        identity: tuple[int, int],
        descriptor: int,
    ) -> None:
        """保存仅供 `acquire` 构造的 lock ownership 证据。"""
        self._path = path
        self._payload = payload
        self._identity = identity
        self._descriptor = descriptor
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
        mode = _program_mode(layout)
        _ensure_directory(layout.program_prefix, mode, os.geteuid(), parent_mode=mode)
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
        pid = os.getpid()
        uid = os.geteuid()
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        identity: tuple[int, int] | None = None
        payload = b""
        try:
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            state, process_start = _probe_process(pid)
            if state != "alive" or process_start is None or not _valid_utc(process_start):
                raise InstallError("install_locked", "manifest")
            document = {"pid": pid, "uid": uid, "start": process_start}
            payload = (
                json.dumps(
                    document,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                )
                + "\n"
            ).encode()
            os.fchmod(descriptor, 0o600)
            _write_all(descriptor, payload)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            if identity is not None:
                _unlink_same_inode(path, identity)
            raise
        try:
            _fsync_directory(path.parent)
        except BaseException:
            os.close(descriptor)
            if identity is not None:
                _unlink_same_inode(path, identity)
            raise
        proof_descriptor = -1
        finalizer: weakref.finalize | None = None
        try:
            proof_descriptor = os.dup(descriptor)
            lock = cls(path, payload, cast(tuple[int, int], identity), descriptor)
            finalizer = weakref.finalize(
                lock,
                _finalize_lock,
                path,
                payload,
                cast(tuple[int, int], identity),
                descriptor,
                proof_descriptor,
                pid,
                uid,
                cast(str, process_start),
            )
            _LOCK_OWNERS[lock] = _LockOwnership(
                path,
                payload,
                cast(tuple[int, int], identity),
                descriptor,
                proof_descriptor,
                pid,
                uid,
                cast(str, process_start),
                finalizer,
            )
            return lock
        except BaseException:
            if finalizer is not None:
                finalizer.detach()
            if proof_descriptor >= 0:
                os.close(proof_descriptor)
            os.close(descriptor)
            _unlink_same_inode(path, cast(tuple[int, int], identity))
            raise

    def owns(self, layout: InstallLayout) -> bool:
        """判断当前实例是否仍持有指定 layout 的原始 install lock。

        Args:
            layout: 需要验证 lock 路径的 canonical 安装 layout。

        Returns:
            仅当实例、进程、fd、inode、payload、owner 与路径仍完全绑定时返回 true。
        """
        ownership = _LOCK_OWNERS.get(self)
        try:
            if (
                type(layout) is not InstallLayout
                or self._closed
                or ownership is None
                or self._path != ownership.path
                or self._payload != ownership.payload
                or self._identity != ownership.identity
                or self._descriptor != ownership.descriptor
                or layout.lock != ownership.path
                or os.getpid() != ownership.pid
                or os.geteuid() != ownership.uid
                or _probe_process(ownership.pid) != ("alive", ownership.start)
                or not _same_open_description(
                    ownership.descriptor,
                    ownership.proof_descriptor,
                )
            ):
                return False
            return _lock_path_owned(
                ownership.path,
                ownership.descriptor,
                ownership.identity,
                ownership.payload,
                ownership.uid,
            )
        except (AttributeError, OSError):
            return False

    def close(self) -> None:
        """仅删除仍匹配创建 inode 和 exact payload 的 lock。"""
        if self._closed:
            return
        self._closed = True
        ownership = _LOCK_OWNERS.pop(self, None)
        if ownership is None:
            return
        try:
            instance_matches = (
                self._path == ownership.path
                and self._payload == ownership.payload
                and self._identity == ownership.identity
                and self._descriptor == ownership.descriptor
            )
        except AttributeError:
            instance_matches = False
        if ownership.finalizer.detach() is None:
            return
        _finalize_lock(
            ownership.path,
            ownership.payload,
            ownership.identity,
            ownership.descriptor,
            ownership.proof_descriptor,
            ownership.pid,
            ownership.uid,
            ownership.start,
            instance_matches,
        )

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


_LOCK_OWNERS: weakref.WeakKeyDictionary[InstallLock, _LockOwnership] = (
    weakref.WeakKeyDictionary()
)


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
    mode = _program_mode(layout)
    launcher_token = _preflight_managed(
        layout.launcher,
        launcher_sha256,
        require_symlink=False,
        expected_mode=mode,
    )
    link_token = _preflight_managed(
        layout.command_link,
        command_link_sha256,
        require_symlink=True,
    )
    launcher_identity: tuple[int, int] | None = None
    link_identity: tuple[int, int] | None = None
    try:
        _ensure_directory(layout.program_prefix, mode, os.geteuid(), parent_mode=mode)
        _ensure_directory(layout.bin_dir, mode, os.geteuid(), parent_mode=mode)
        _ensure_safe_directory(layout.command_link.parent, os.geteuid(), create_mode=0o755)
        payload = render_launcher(layout)
        if not _lexists(layout.launcher):
            launcher_identity = _create_regular(layout.launcher, payload, mode)
        else:
            if launcher_token is None:
                _ownership_invalid()
            if managed_file_sha256(
                layout.launcher,
                expected_uid=os.geteuid(),
                expected_mode=mode,
            ) != hashlib.sha256(payload).hexdigest():
                _ownership_invalid()
        relative_target = os.path.relpath(layout.launcher, start=layout.command_link.parent)
        if not _lexists(layout.command_link):
            link_identity = _create_symlink(layout.command_link, relative_target)
        else:
            if link_token is None:
                _ownership_invalid()
            if os.readlink(layout.command_link) != relative_target:
                _ownership_invalid()
        return (
            managed_file_sha256(
                layout.launcher,
                expected_uid=os.geteuid(),
                expected_mode=mode,
            ),
            managed_file_sha256(
                layout.command_link,
                expected_uid=os.geteuid(),
                require_symlink=True,
            ),
        )
    except InstallError:
        if link_identity is not None:
            _unlink_same_inode(layout.command_link, link_identity)
        if launcher_identity is not None:
            _unlink_same_inode(layout.launcher, launcher_identity)
        raise
    except OSError as error:
        if link_identity is not None:
            _unlink_same_inode(layout.command_link, link_identity)
        if launcher_identity is not None:
            _unlink_same_inode(layout.launcher, launcher_identity)
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def _preflight_managed(
    path: Path,
    expected: str | None,
    *,
    require_symlink: bool,
    expected_mode: int | None = None,
) -> _ManagedToken | None:
    """在任何写入前验证 existing path 由 prior receipt 持有且类型正确。"""
    exists = _lexists(path)
    if not exists:
        if expected is not None:
            _ownership_invalid()
        return None
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
    verify_managed_file(
        path,
        expected,
        expected_uid=os.geteuid(),
        expected_mode=expected_mode,
        require_symlink=require_symlink,
    )
    try:
        after = path.lstat()
    except OSError as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error
    if (
        (after.st_dev, after.st_ino) != (metadata.st_dev, metadata.st_ino)
        or after.st_uid != metadata.st_uid
        or after.st_mode != metadata.st_mode
        or after.st_nlink != metadata.st_nlink
    ):
        _ownership_invalid()
    return _ManagedToken(
        (metadata.st_dev, metadata.st_ino),
        expected,
        require_symlink,
        expected_mode,
    )


def _program_mode(layout: InstallLayout) -> int:
    """返回 user 0700 或 system-prefix 0755 的程序目录/launcher mode。"""
    return 0o755 if layout.program_prefix == _SYSTEM_PREFIX else 0o700


def _remove_stale_lock(path: Path) -> bool:
    """仅删除同 UID 且进程 confirmed dead 或 PID start 已变化的 lock。"""
    uid = os.geteuid()
    descriptor = -1
    try:
        descriptor, payload, identity = _open_lock(path, uid)
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
        owned = _lock_path_owned(path, descriptor, identity, payload, uid)
        return owned and _quarantine_owned_lock(path, descriptor, identity, payload, uid)
    except (InstallError, OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
        return False
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_lock_bytes(path: Path, expected_uid: int) -> bytes:
    """no-follow 读取 expected owner 的 exact 0600 bounded lock。"""
    descriptor = -1
    try:
        descriptor, payload, _identity = _open_lock(path, expected_uid)
        return payload
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _open_lock(path: Path, expected_uid: int) -> tuple[int, bytes, tuple[int, int]]:
    """打开并保持一个 no-follow private lock fd/inode token。"""
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise InstallError("install_locked", "manifest")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_uid != expected_uid
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
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
        return descriptor, payload, (opened.st_dev, opened.st_ino)
    except InstallError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise InstallError("install_locked", "manifest") from error


def _lock_path_owned(
    path: Path,
    descriptor: int,
    identity: tuple[int, int],
    payload: bytes,
    expected_uid: int,
) -> bool:
    """用持有 fd、稳定 metadata 与两次 exact read 绑定 lock。"""
    try:
        held_before = os.fstat(descriptor)
        path_before = path.lstat()
        snapshot = _owned_lock_snapshot(held_before, identity, expected_uid)
        if (
            snapshot is None
            or _owned_lock_snapshot(path_before, identity, expected_uid) != snapshot
        ):
            return False
        for _ in range(2):
            if _read_lock_bytes(path, expected_uid) != payload:
                return False
            held_after = os.fstat(descriptor)
            path_after = path.lstat()
            if (
                _owned_lock_snapshot(held_after, identity, expected_uid) != snapshot
                or _owned_lock_snapshot(path_after, identity, expected_uid) != snapshot
            ):
                return False
        return True
    except (InstallError, OSError):
        return False


def _owned_lock_snapshot(
    metadata: os.stat_result,
    identity: tuple[int, int],
    expected_uid: int,
) -> tuple[int, int, int, int, int, int, int, int] | None:
    """返回 exact private regular lock 的稳定 metadata，失配时返回空。"""
    if (
        not stat.S_ISREG(metadata.st_mode)
        or (metadata.st_dev, metadata.st_ino) != identity
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_nlink != 1
    ):
        return None
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _same_open_description(descriptor: int, proof_descriptor: int) -> bool:
    """用 dup 共享 offset 验证 fd 编号仍指向 acquire 的 open description。"""
    descriptor_offset: int | None = None
    proof_offset: int | None = None
    try:
        descriptor_offset = os.lseek(descriptor, 0, os.SEEK_CUR)
        proof_offset = os.lseek(proof_descriptor, 0, os.SEEK_CUR)
        if descriptor_offset != proof_offset:
            return False
        marker = 0 if descriptor_offset else 1
        os.lseek(proof_descriptor, marker, os.SEEK_SET)
        return os.lseek(descriptor, 0, os.SEEK_CUR) == marker
    except OSError:
        return False
    finally:
        try:
            if proof_offset is not None:
                os.lseek(proof_descriptor, proof_offset, os.SEEK_SET)
            if descriptor_offset is not None:
                os.lseek(descriptor, descriptor_offset, os.SEEK_SET)
        except OSError:
            pass


def _finalize_lock(
    path: Path,
    payload: bytes,
    identity: tuple[int, int],
    descriptor: int,
    proof_descriptor: int,
    expected_pid: int,
    expected_uid: int,
    expected_start: str,
    remove_path: bool = True,
) -> None:
    """由创建进程清理 exact lock，并在任意进程关闭仍属于本实例的 descriptor。

    Args:
        path: acquire 时创建的 lock 路径。
        payload: acquire 时写入的 exact JSON bytes。
        identity: acquire 时记录的设备号与 inode。
        descriptor: lock 的原始 open descriptor。
        proof_descriptor: 与原始 descriptor 共享 open description 的私有副本。
        expected_pid: acquire 进程 PID。
        expected_uid: acquire 进程 effective UID。
        expected_start: acquire 进程的 UTC start identity。
        remove_path: 实例可变字段仍匹配时才允许删除路径。

    Returns:
        无返回值；身份失配或 cleanup 失败时 fail closed，且不向外抛错。
    """
    process_owned = False
    try:
        process_owned = (
            os.getpid() == expected_pid
            and os.geteuid() == expected_uid
            and _probe_process(expected_pid) == ("alive", expected_start)
        )
    except OSError:
        pass
    descriptor_owned = _same_open_description(descriptor, proof_descriptor)
    try:
        if process_owned and remove_path and descriptor_owned and _lock_path_owned(
            path,
            descriptor,
            identity,
            payload,
            expected_uid,
        ):
            _quarantine_owned_lock(
                path,
                descriptor,
                identity,
                payload,
                expected_uid,
            )
    except (InstallError, OSError):
        pass
    finally:
        for held in ((descriptor,) if descriptor_owned else ()) + (proof_descriptor,):
            try:
                os.close(held)
            except OSError:
                pass


def _quarantine_owned_lock(
    path: Path,
    descriptor: int,
    identity: tuple[int, int],
    payload: bytes,
    expected_uid: int,
) -> bool:
    """原子隔离 lock 后再次绑定 fd/inode/payload，再删除私有目录项。"""
    quarantined = _quarantine_expected(path, identity, require_symlink=False)
    if quarantined is None:
        return False
    if not _lock_path_owned(
        quarantined.private,
        descriptor,
        identity,
        payload,
        expected_uid,
    ):
        quarantined.restore()
        return False
    return quarantined.discard()


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
    _validate_trusted_chain(path, expected_uid)
    anchor = home if path.is_relative_to(home) else _nearest_existing(path)
    _validate_existing_chain(path, anchor, expected_uid)
    if _lexists(path):
        metadata = path.lstat()
        if metadata.st_uid != expected_uid or stat.S_IMODE(metadata.st_mode) != 0o700:
            _request_invalid()


def _validate_system_prefix(path: Path) -> None:
    """校验固定 system prefix 的现有 root-owned no-follow chain。"""
    if path != _SYSTEM_PREFIX:
        _request_invalid()
    _validate_trusted_chain(path, 0)
    _validate_existing_chain(path, Path("/"), 0, allow_root=True)
    if _lexists(path):
        metadata = path.lstat()
        if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o755:
            _request_invalid()


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


def _validate_trusted_chain(path: Path, expected_uid: int) -> None:
    """从 filesystem root 逐组件 lstat，拒绝 symlink 与可替换 ancestor。"""
    if not _safe_absolute_path(path) or type(expected_uid) is not int or expected_uid < 0:
        _request_invalid()
    current = Path("/")
    candidates = [current]
    for part in path.parts[1:]:
        current /= part
        candidates.append(current)
    for candidate in candidates:
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            break
        except OSError:
            _request_invalid()
        mode = stat.S_IMODE(metadata.st_mode)
        sticky_root = metadata.st_uid == 0 and bool(metadata.st_mode & stat.S_ISVTX)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in {0, expected_uid}
            or mode & 0o022
            and not sticky_root
        ):
            _request_invalid()


def _ensure_directory(
    path: Path,
    mode: int,
    expected_uid: int,
    *,
    parent_mode: int,
) -> None:
    """创建或校验 owner、no-follow 且 mode 精确的最终目录。"""
    if _lexists(path):
        try:
            metadata = path.lstat()
        except OSError as error:
            raise InstallError("uninstall_ownership_mismatch", "manifest") from error
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or stat.S_IMODE(metadata.st_mode) != mode
        ):
            _ownership_invalid()
        return
    _ensure_safe_directory(path.parent, expected_uid, create_mode=parent_mode)
    try:
        path.mkdir(mode=mode)
        path.chmod(mode)
    except OSError as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def _ensure_safe_directory(path: Path, expected_uid: int, *, create_mode: int) -> None:
    """接受 owner 持有的不可组/全局写父目录，并按指定 mode 补齐缺失层。"""
    if not _lexists(path):
        _ensure_safe_directory(path.parent, expected_uid, create_mode=create_mode)
        try:
            path.mkdir(mode=create_mode)
            path.chmod(create_mode)
        except OSError as error:
            raise InstallError("uninstall_ownership_mismatch", "manifest") from error
        return
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


def _create_regular(path: Path, payload: bytes, mode: int) -> tuple[int, int]:
    """先完整持久化私有 temp，再以 hardlink O_EXCL 发布 regular file。"""
    temporary, identity = _prepare_regular(path, payload, mode)
    published = False
    try:
        os.link(temporary, path, follow_symlinks=False)
        published = True
        _unlink_same_inode(temporary, identity)
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity or metadata.st_nlink != 1:
            _ownership_invalid()
        _fsync_directory(path.parent)
        return identity
    except BaseException:
        if published:
            _unlink_same_inode(path, identity)
        _unlink_same_inode(temporary, identity)
        raise


def _create_symlink(path: Path, target: str) -> tuple[int, int]:
    """创建并持久化 relative symlink，失败仅清理同 inode。"""
    identity: tuple[int, int] | None = None
    try:
        os.symlink(target, path)
        metadata = path.lstat()
        if (
            not stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_nlink != 1
            or os.readlink(path) != target
        ):
            _ownership_invalid()
        identity = (metadata.st_dev, metadata.st_ino)
        _fsync_directory(path.parent)
        return identity
    except BaseException:
        if identity is not None:
            _unlink_same_inode(path, identity)
        raise


def _prepare_regular(path: Path, payload: bytes, mode: int) -> tuple[Path, tuple[int, int]]:
    """在目标同目录用 O_EXCL temp 完整写入、chmod 并 fsync regular bytes。"""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    identity: tuple[int, int] | None = None
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        os.fchmod(descriptor, mode)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        return temporary, identity
    except BaseException:
        if identity is not None:
            _unlink_same_inode(temporary, identity)
        raise
    finally:
        os.close(descriptor)


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
    """隔离公开 pathname 后，仅清理仍匹配调用方 token 的目录项。"""
    quarantined = _quarantine_expected(path, identity)
    if quarantined is not None and not quarantined.discard():
        _ownership_invalid()


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
