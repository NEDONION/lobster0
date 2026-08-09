"""管理 owner-only install receipt 与 no-follow 文件 ownership hash。"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal, Never

from miniclaw.install.models import InstallError, PlatformKey

_MAX_RECEIPT_BYTES = 1_048_576
_MAX_MANAGED_FILES = 512
_MAX_MANAGED_PATH_BYTES = 1024
_MAX_PATH_COMPONENT_BYTES = 255
_HASH = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_SEMVER = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")
_SERVICE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_RECEIPT_KEYS = {
    "schema_version",
    "version",
    "git_commit",
    "platform",
    "installed_at",
    "managed_files",
    "current_runtime",
    "previous_runtime",
    "service_label",
    "service_file",
    "service_file_sha256",
}
_SENSITIVE_NAMES = {
    ".env",
    "config.toml",
    "secrets.env",
    "miniclaw.db",
    "memory",
    "memory.md",
    "skills",
    "workspace",
    "logs",
}


@dataclass(frozen=True, slots=True)
class InstallReceipt:
    """记录受管程序文件，不记录任何用户数据或 Secret。

    Args:
        schema_version: 当前 receipt schema，固定为 1。
        version: 已安装 MiniClaw 规范 SemVer。
        git_commit: Release 绑定的 lowercase 40-hex commit。
        platform: 已安装的具体 Release 平台。
        installed_at: UTC RFC3339 安装时间。
        managed_files: program prefix 下的相对路径及 lowercase SHA-256。
        current_runtime: 当前 Runtime 的相对路径。
        previous_runtime: 可回滚 Runtime 的可选相对路径。
        service_label: 可选服务 label。
        service_file: 可选服务文件逻辑相对路径。
        service_file_sha256: 可选服务文件 ownership hash。

    Raises:
        InstallError: 任一字段、类型、路径或字段关系不安全。
    """

    schema_version: Literal[1]
    version: str
    git_commit: str
    platform: PlatformKey
    installed_at: str
    managed_files: tuple[tuple[str, str], ...]
    current_runtime: str
    previous_runtime: str | None
    service_label: str | None
    service_file: str | None
    service_file_sha256: str | None

    def __post_init__(self) -> None:
        """闭合 receipt schema、类型、相对路径和 service 字段关系。"""
        if type(self.schema_version) is not int or self.schema_version != 1:
            _invalid()
        if type(self.version) is not str or _SEMVER.fullmatch(self.version) is None:
            _invalid()
        if type(self.git_commit) is not str or _COMMIT.fullmatch(self.git_commit) is None:
            _invalid()
        if (
            type(self.platform) is not PlatformKey
            or self.platform.os not in {"linux", "macos"}
            or self.platform.arch not in {"x86_64", "arm64"}
        ):
            _invalid()
        _validate_utc(self.installed_at)
        if (
            type(self.managed_files) is not tuple
            or not 1 <= len(self.managed_files) <= _MAX_MANAGED_FILES
        ):
            _invalid()
        path_keys: list[str] = []
        for item in self.managed_files:
            if type(item) is not tuple or len(item) != 2:
                _invalid()
            path, digest = item
            _validate_relative_path(path)
            _validate_hash(digest)
            path_keys.append(path.casefold())
        if len(path_keys) != len(set(path_keys)):
            _invalid()
        _validate_runtime_path(self.current_runtime, self.version)
        if self.previous_runtime is not None:
            _validate_runtime_path(self.previous_runtime)
            if self.previous_runtime == self.current_runtime:
                _invalid()
        service_values = (self.service_label, self.service_file, self.service_file_sha256)
        if not all(value is None for value in service_values):
            if any(value is None for value in service_values):
                _invalid()
            if (
                type(self.service_label) is not str
                or _SERVICE_LABEL.fullmatch(self.service_label) is None
                or type(self.service_file) is not str
                or type(self.service_file_sha256) is not str
            ):
                _invalid()
            _validate_relative_path(self.service_file)
            _validate_hash(self.service_file_sha256)
        if len(_serialize_receipt(self)) > _MAX_RECEIPT_BYTES:
            _invalid()

    @property
    def launcher_sha256(self) -> str:
        """返回 `bin/miniclaw` 的 ownership hash。

        Returns:
            stable launcher 的 lowercase SHA-256。

        Raises:
            InstallError: receipt 没有唯一 launcher 记录。
        """
        matches = tuple(digest for path, digest in self.managed_files if path == "bin/miniclaw")
        if len(matches) != 1:
            _invalid()
        return matches[0]

    def to_bytes(self) -> bytes:
        """返回 deterministic compact strict JSON。

        Returns:
            带单个结尾换行的 UTF-8 receipt 字节。
        """
        return _serialize_receipt(self)

    @classmethod
    def load(
        cls,
        path: Path,
        *,
        expected_uid: int | None = None,
        expected_platform: PlatformKey | None = None,
    ) -> InstallReceipt:
        """从 owner-only regular file 读取 strict receipt。

        Args:
            path: receipt 绝对路径。
            expected_uid: 期望文件 owner；省略时使用当前 euid。
            expected_platform: 可选的当前 Release 平台绑定。

        Returns:
            完成 exact-key、类型和关系校验的 receipt。

        Raises:
            InstallError: 路径、owner、权限、JSON、schema 或平台不匹配。
        """
        uid = os.geteuid() if expected_uid is None else expected_uid
        if type(uid) is not int or uid < 0:
            _invalid()
        payload = _read_private_regular(path, uid)
        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_strict_object,
                parse_constant=lambda _value: _raise_json_value(),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            _invalid()
        if type(document) is not dict or set(document) != _RECEIPT_KEYS:
            _invalid()
        platform_document = document["platform"]
        if type(platform_document) is not dict or set(platform_document) != {"os", "arch"}:
            _invalid()
        managed_document = document["managed_files"]
        if type(managed_document) is not list:
            _invalid()
        managed: list[tuple[str, str]] = []
        for item in managed_document:
            if type(item) is not list or len(item) != 2:
                _invalid()
            path_value, digest = item
            if type(path_value) is not str or type(digest) is not str:
                _invalid()
            managed.append((path_value, digest))
        try:
            receipt = cls(
                schema_version=document["schema_version"],
                version=document["version"],
                git_commit=document["git_commit"],
                platform=PlatformKey(platform_document["os"], platform_document["arch"]),
                installed_at=document["installed_at"],
                managed_files=tuple(managed),
                current_runtime=document["current_runtime"],
                previous_runtime=document["previous_runtime"],
                service_label=document["service_label"],
                service_file=document["service_file"],
                service_file_sha256=document["service_file_sha256"],
            )
        except InstallError:
            _invalid()
        if expected_platform is not None and (
            type(expected_platform) is not PlatformKey or receipt.platform != expected_platform
        ):
            _invalid()
        return receipt

    def write(self, path: Path, *, owner_uid: int | None = None) -> None:
        """以 O_EXCL temp、fsync、replace 和 parent fsync 原子写 receipt。

        Args:
            path: receipt 绝对目标路径。
            owner_uid: 文件 owner；省略时使用当前 euid。

        Raises:
            InstallError: 路径/owner 不安全、已有 receipt 无效或原子持久化失败。
        """
        uid = os.geteuid() if owner_uid is None else owner_uid
        if type(uid) is not int or uid < 0 or not _safe_absolute_path(path):
            _invalid()
        _validate_private_parent(path.parent, uid)
        original: bytes | None = None
        if _lexists(path):
            original = _read_private_regular(path, uid)
            type(self).load(path, expected_uid=uid)
        payload = self.to_bytes()
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary_identity: tuple[int, int] | None = None
        replaced = False
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
                0o600,
            )
            metadata = os.fstat(descriptor)
            temporary_identity = (metadata.st_dev, metadata.st_ino)
            try:
                os.fchmod(descriptor, 0o600)
                if metadata.st_uid != uid:
                    os.fchown(descriptor, uid, -1)
                _write_all(descriptor, payload)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            replaced = True
            temporary_identity = None
            _fsync_directory(path.parent)
        except BaseException as error:
            if temporary_identity is not None:
                _unlink_same_inode(temporary, temporary_identity)
            if replaced:
                _restore_receipt(path, original, uid)
            raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def managed_file_sha256(
    path: Path,
    *,
    expected_uid: int | None = None,
    expected_mode: int | None = None,
    require_symlink: bool | None = None,
) -> str:
    """不跟随 symlink 计算受管 regular file 或 link identity 的 SHA-256。

    Args:
        path: 受管文件的绝对路径。
        expected_uid: 期望 owner；省略时使用当前 euid。
        expected_mode: regular file 的期望 permission bits。
        require_symlink: 显式要求 symlink 或 regular file；省略时两者都允许。

    Returns:
        regular file bytes，或 `symlink\\0 + raw target` 的 lowercase SHA-256。

    Raises:
        InstallError: 路径敏感、带控制字符、缺失、特殊文件或发生竞态。
    """
    uid = os.geteuid() if expected_uid is None else expected_uid
    if (
        not _safe_absolute_path(path)
        or _path_is_sensitive(path)
        or type(uid) is not int
        or uid < 0
        or expected_mode is not None
        and (type(expected_mode) is not int or not 0 <= expected_mode <= 0o777)
        or require_symlink is not None
        and type(require_symlink) is not bool
    ):
        _invalid()
    try:
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            if (
                require_symlink is False
                or expected_mode is not None
                or metadata.st_uid != uid
                or metadata.st_nlink != 1
            ):
                _invalid()
            target = os.readlink(path)
            if not target or any(
                ord(character) < 32 or ord(character) == 127 for character in target
            ):
                _invalid()
            after = path.lstat()
            if _metadata_identity(after) != _metadata_identity(metadata):
                _invalid()
            payload = b"symlink\0" + os.fsencode(target)
            return hashlib.sha256(payload).hexdigest()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or require_symlink is True
            or metadata.st_uid != uid
            or metadata.st_nlink != 1
            or expected_mode is not None
            and stat.S_IMODE(metadata.st_mode) != expected_mode
        ):
            _invalid()
        descriptor = os.open(path, os.O_RDONLY | _no_follow_flag())
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or _metadata_snapshot(opened) != _metadata_snapshot(metadata)
            ):
                _invalid()
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            opened_after = os.fstat(descriptor)
            after = path.lstat()
            if (
                _metadata_snapshot(opened_after) != _metadata_snapshot(opened)
                or _metadata_snapshot(after) != _metadata_snapshot(metadata)
            ):
                _invalid()
            return digest.hexdigest()
        finally:
            os.close(descriptor)
    except InstallError:
        raise
    except (OSError, UnicodeError, ValueError) as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def verify_managed_file(
    path: Path,
    expected_sha256: str,
    *,
    expected_uid: int | None = None,
    expected_mode: int | None = None,
    require_symlink: bool | None = None,
) -> None:
    """验证文件 no-follow ownership hash 与 receipt 完全一致。

    Args:
        path: 待重写或删除的受管文件。
        expected_sha256: receipt 记录的 lowercase SHA-256。
        expected_uid: 期望 owner；省略时使用当前 euid。
        expected_mode: regular file 的期望 permission bits。
        require_symlink: 显式要求 symlink 或 regular file。

    Raises:
        InstallError: hash 无效、文件不安全或 identity 不匹配。
    """
    _validate_hash(expected_sha256)
    actual = managed_file_sha256(
        path,
        expected_uid=expected_uid,
        expected_mode=expected_mode,
        require_symlink=require_symlink,
    )
    if not hmac.compare_digest(actual, expected_sha256):
        _invalid()


def _read_private_regular(path: Path, expected_uid: int) -> bytes:
    """no-follow 读取指定 owner 的 0600 bounded regular file。"""
    if not _safe_absolute_path(path):
        _invalid()
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
        ):
            _invalid()
        descriptor = os.open(path, os.O_RDONLY | _no_follow_flag())
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_uid != expected_uid
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                _invalid()
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, min(65_536, _MAX_RECEIPT_BYTES + 1 - size)):
                chunks.append(chunk)
                size += len(chunk)
                if size > _MAX_RECEIPT_BYTES:
                    _invalid()
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    except InstallError:
        raise
    except OSError as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error


def _validate_private_parent(path: Path, expected_uid: int) -> None:
    """校验 receipt parent 为 no-follow owner-only directory。"""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise InstallError("uninstall_ownership_mismatch", "manifest") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != expected_uid
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        _invalid()


def _restore_receipt(path: Path, original: bytes | None, owner_uid: int) -> None:
    """在 replace 后异常时尽力恢复原 receipt 或移除新文件。"""
    if original is None:
        try:
            path.unlink()
            _fsync_directory(path.parent)
        except OSError:
            pass
        return
    recovery = path.with_name(f".{path.name}.{os.getpid()}.restore.tmp")
    identity: tuple[int, int] | None = None
    try:
        descriptor = os.open(
            recovery,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | _no_follow_flag(),
            0o600,
        )
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        try:
            os.fchmod(descriptor, 0o600)
            if metadata.st_uid != owner_uid:
                os.fchown(descriptor, owner_uid, -1)
            _write_all(descriptor, original)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(recovery, path)
        identity = None
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass
    except OSError:
        pass
    finally:
        if identity is not None:
            _unlink_same_inode(recovery, identity)


def _fsync_directory(path: Path) -> None:
    """fsync 一个 no-follow directory 以持久化目录项。"""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _no_follow_flag())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_all(descriptor: int, payload: bytes) -> None:
    """把完整 payload 写入 descriptor，拒绝零进度。"""
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _unlink_same_inode(path: Path, identity: tuple[int, int]) -> None:
    """仅在 path 仍指向调用方创建的 inode 时清理 temp。"""
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity:
            path.unlink()
    except OSError:
        pass


def _validate_relative_path(value: object) -> None:
    """验证无控制字符、无逃逸且不指向用户数据的 POSIX 相对路径。"""
    if (
        type(value) is not str
        or not value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or len(value.encode("utf-8")) > _MAX_MANAGED_PATH_BYTES
    ):
        _invalid()
    if any(not character.isprintable() for character in value):
        _invalid()
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or str(candidate) != value
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        _invalid()
    if any(len(part.encode("utf-8")) > _MAX_PATH_COMPONENT_BYTES for part in candidate.parts):
        _invalid()
    if _parts_are_sensitive(candidate.parts):
        _invalid()


def _validate_runtime_path(value: object, version: str | None = None) -> None:
    """验证 receipt runtime 路径固定为 `runtimes/<semver>`。"""
    _validate_relative_path(value)
    candidate = PurePosixPath(value)
    if (
        len(candidate.parts) != 2
        or candidate.parts[0] != "runtimes"
        or _SEMVER.fullmatch(candidate.parts[1]) is None
        or version is not None
        and candidate.parts[1] != version
    ):
        _invalid()


def _validate_hash(value: object) -> None:
    """验证 lowercase SHA-256 文本。"""
    if type(value) is not str or _HASH.fullmatch(value) is None:
        _invalid()


def _validate_utc(value: object) -> None:
    """验证 UTC RFC3339 timestamp 及真实日历值。"""
    if type(value) is not str or _UTC.fullmatch(value) is None:
        _invalid()
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _invalid()
    if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
        _invalid()


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """构造拒绝 duplicate key 的 JSON object。"""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _raise_json_value() -> Never:
    """拒绝 JSON NaN/Infinity。"""
    raise ValueError("invalid JSON value")


def _safe_absolute_path(path: object) -> bool:
    """判断值是否为无控制字符和 `..` 的 lexical absolute Path。"""
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and ".." not in path.parts
        and len(str(path)) <= 4096
        and all(character.isprintable() for character in str(path))
    )


def _path_is_sensitive(path: Path) -> bool:
    """判断绝对路径是否明确落入配置、Secret 或用户数据命名空间。"""
    return _parts_are_sensitive(path.parts)


def _parts_are_sensitive(parts: tuple[str, ...]) -> bool:
    """判断路径组件是否命中禁止写入 receipt 的用户数据名。"""
    lowered = tuple(part.casefold() for part in parts)
    return any(part in _SENSITIVE_NAMES or part.endswith(".db") for part in lowered)


def _serialize_receipt(receipt: InstallReceipt) -> bytes:
    """把已校验 receipt 序列化为 deterministic compact UTF-8 JSON。"""
    document = {
        "schema_version": receipt.schema_version,
        "version": receipt.version,
        "git_commit": receipt.git_commit,
        "platform": {"os": receipt.platform.os, "arch": receipt.platform.arch},
        "installed_at": receipt.installed_at,
        "managed_files": [list(item) for item in receipt.managed_files],
        "current_runtime": receipt.current_runtime,
        "previous_runtime": receipt.previous_runtime,
        "service_label": receipt.service_label,
        "service_file": receipt.service_file,
        "service_file_sha256": receipt.service_file_sha256,
    }
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode()


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    """返回 symlink identity/owner/link-count 快照。"""
    return metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_nlink


def _metadata_snapshot(metadata: os.stat_result) -> tuple[int, ...]:
    """返回 regular file hash 前后必须稳定的完整 metadata 快照。"""
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _lexists(path: Path) -> bool:
    """不跟随 symlink 判断目录项是否存在。"""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _invalid()
    return True


def _no_follow_flag() -> int:
    """返回当前平台可用的 O_NOFOLLOW flag。"""
    return getattr(os, "O_NOFOLLOW", 0)


def _invalid() -> Never:
    """抛出统一且不包含路径或 Secret 的 ownership mismatch。"""
    raise InstallError("uninstall_ownership_mismatch", "manifest")
