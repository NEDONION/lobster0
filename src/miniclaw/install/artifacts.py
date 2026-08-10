"""提供 allowlisted HTTPS 下载与 bounded tar.gz 解包。"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import http.client
import os
import shutil
import stat
import tarfile
import tempfile
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from miniclaw.install.models import Artifact, InstallError

_CHUNK_BYTES = 64 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_MAX_REDIRECTS = 5
_MAX_ENTRIES = 20_000
_MAX_BYTES = 1_073_741_824
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RELEASES_API_URL = "https://api.github.com/repos/NEDONION/mini-claw/releases?per_page=20"
_GITHUB_RELEASE_PREFIX = "/NEDONION/mini-claw/releases/"


class _Response(Protocol):
    """描述下载器需要的最小 urllib response 契约。"""

    status: int
    headers: object

    def read(self, size: int = -1) -> bytes:
        """读取最多 size 字节。"""

    def close(self) -> None:
        """关闭响应资源。"""


class _Opener(Protocol):
    """描述可替换为离线 fake 的 urllib opener。"""

    def open(self, request: Request, timeout: float | None = None) -> _Response:
        """打开一个禁用自动 redirect 的请求。"""


class _NoRedirectHandler(HTTPRedirectHandler):
    """禁止 urllib 自动跟随，以便每跳重新执行 allowlist 校验。"""

    def redirect_request(
        self,
        request: Request,
        file_pointer: BinaryIO,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        """让 redirect 以 HTTPError response 返回给显式循环。"""
        del request, file_pointer, code, message, headers, new_url
        return None


@dataclass(frozen=True, slots=True)
class ExtractionLimits:
    """限制 archive member 数与实际输出字节。

    Args:
        max_entries: 允许的最大 header/member 数。
        max_bytes: 允许的 declared 和实际 regular file 总字节。

    Raises:
        InstallError: 预算不是 bounded exact positive int。
    """

    max_entries: int = _MAX_ENTRIES
    max_bytes: int = _MAX_BYTES

    def __post_init__(self) -> None:
        """拒绝 bool、零值和大于 Release 上限的预算。"""
        if (
            type(self.max_entries) is not int
            or not 1 <= self.max_entries <= _MAX_ENTRIES
            or type(self.max_bytes) is not int
            or not 1 <= self.max_bytes <= _MAX_BYTES
        ):
            raise InstallError("manifest_invalid", "manifest")


def download_artifact(
    artifact: Artifact,
    destination: Path,
    opener: _Opener | None = None,
) -> Path:
    """流式下载并在 exact size/hash 校验后原子落到 destination。

    Args:
        artifact: 已通过 strict manifest 校验的 Artifact。
        destination: owner-only staging 下尚不存在的绝对目标路径。
        opener: 测试可注入的无自动 redirect opener；省略时使用 urllib。

    Returns:
        已 fsync、mode 0600 且校验通过的目标路径。

    Raises:
        InstallError: URL/HTTP/IO 中断或 size/hash/目标状态不可信。
    """
    if type(artifact) is not Artifact or not _safe_absolute_path(destination):
        raise InstallError("artifact_download_failed", "artifacts")
    if not destination.parent.is_dir() or _lexists(destination):
        raise InstallError("artifact_download_failed", "artifacts")
    part = destination.with_name(f"{destination.name}.part")
    if _lexists(part):
        raise InstallError("artifact_download_failed", "artifacts")
    response: _Response | None = None
    created_part = False
    replaced = False
    succeeded = False
    try:
        response = _open_verified_response(artifact.url, opener)
        if _response_status(response) != 200:
            raise InstallError("artifact_download_failed", "artifacts")
        _require_identity_encoding(response, "artifact_download_failed")
        declared_size = _content_length(response, "artifact_download_failed")
        if declared_size is not None and declared_size != artifact.size:
            raise InstallError("artifact_hash_mismatch", "artifacts.size")

        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(part, flags, 0o600)
        created_part = True
        digest = hashlib.sha256()
        written = 0
        with os.fdopen(descriptor, "wb") as output:
            while True:
                chunk = response.read(min(_CHUNK_BYTES, artifact.size - written + 1))
                if not isinstance(chunk, bytes):
                    raise InstallError("artifact_download_failed", "artifacts")
                if not chunk:
                    break
                if written + len(chunk) > artifact.size:
                    raise InstallError("artifact_hash_mismatch", "artifacts.size")
                output.write(chunk)
                digest.update(chunk)
                written += len(chunk)
            if written != artifact.size:
                raise InstallError("artifact_hash_mismatch", "artifacts.size")
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o600)
        if not hmac.compare_digest(digest.hexdigest(), artifact.sha256):
            raise InstallError("artifact_hash_mismatch", "artifacts.sha256")
        if _lexists(destination):
            raise InstallError("artifact_download_failed", "artifacts")
        os.replace(part, destination)
        created_part = False
        replaced = True
        _fsync_directory(destination.parent)
        succeeded = True
        return destination
    except InstallError:
        raise
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
        ValueError,
    ):
        raise InstallError("artifact_download_failed", "artifacts") from None
    finally:
        if response is not None:
            _close_response(response)
        if replaced and not succeeded:
            cleanup = _quarantine_failed_download(destination)
            if cleanup is not None:
                _fsync_directory_best_effort(destination.parent)
                _remove_owned_path(cleanup)
                _fsync_directory_best_effort(destination.parent)
        if created_part:
            _remove_owned_path(part)


def extract_tar_gz(
    archive: Path,
    destination: Path,
    limits: ExtractionLimits,
    *,
    executable_paths: Collection[str] = frozenset(),
) -> tuple[Path, ...]:
    """先校验全部 headers，再手工解包 regular 文件和目录。

    Args:
        archive: 已下载校验的 `.tar.gz` 普通文件。
        destination: 不存在或为空的 owner-only staging 目录。
        limits: member 与输出字节的双预算。
        executable_paths: manifest/调用方明确声明为可执行的 POSIX member paths。

    Returns:
        按 archive 顺序返回已声明 member 的最终绝对路径。

    Raises:
        InstallError: archive、path、type、碰撞、mode 声明或预算不可信。
    """
    if (
        not _safe_absolute_path(archive)
        or not _safe_absolute_path(destination)
        or type(limits) is not ExtractionLimits
        or not _regular_file_without_symlink(archive)
        or not destination.parent.is_dir()
    ):
        raise InstallError("manifest_invalid", "manifest")
    destination_existed = _lexists(destination)
    if destination_existed:
        if destination.is_symlink() or not destination.is_dir() or any(destination.iterdir()):
            raise InstallError("manifest_invalid", "manifest")
    executable_names = _validate_executable_paths(executable_paths)
    work = Path(tempfile.mkdtemp(prefix=f".{destination.name}.extract-", dir=destination.parent))
    previous = work.with_name(f"{work.name}.previous")
    original_moved = False
    replaced = False
    succeeded = False
    try:
        with tempfile.TemporaryFile(mode="w+b") as raw_archive:
            _decompress_bounded_tar(archive, raw_archive, limits)
            _validate_raw_tar(raw_archive, limits)
            members = _validated_members(raw_archive, work, limits)
            regular_names = {member.name for member in members if member.isreg()}
            if not executable_names.issubset(regular_names):
                raise InstallError("manifest_invalid", "manifest")
            _extract_validated_members(
                raw_archive,
                work,
                members,
                limits,
                executable_names,
            )
        _fsync_tree(work)
        if destination_existed:
            os.replace(destination, previous)
            original_moved = True
        elif _lexists(destination):
            raise InstallError("manifest_invalid", "manifest")
        os.replace(work, destination)
        replaced = True
        _fsync_directory(destination.parent)
        succeeded = True
        if original_moved:
            try:
                previous.rmdir()
            except OSError:
                pass
            else:
                original_moved = False
                try:
                    _fsync_directory(destination.parent)
                except OSError:
                    pass
        return tuple(destination.joinpath(*PurePosixPath(member.name).parts) for member in members)
    except InstallError:
        raise
    except (OSError, EOFError, tarfile.TarError, ValueError):
        raise InstallError("manifest_invalid", "manifest") from None
    finally:
        if not succeeded:
            if replaced:
                try:
                    os.replace(destination, work)
                except OSError:
                    pass
            if destination_existed:
                _restore_empty_destination(destination, previous, original_moved)
            _fsync_directory_best_effort(destination.parent)
            _remove_owned_path(work)
            _fsync_directory_best_effort(destination.parent)


def _safe_member_path(root: Path, name: str) -> Path:
    """把 POSIX archive name 限制在 root 内。

    Args:
        root: 已存在的私有 extraction root。
        name: 未信任的 tar member name。

    Returns:
        root 内对应的目标 Path。

    Raises:
        InstallError: 名称绝对、空、含 dot segment/control/NUL 或逃出 root。
    """
    if type(name) is not str or not name or "\x00" in name or any(
        ord(character) < 32 for character in name
    ):
        raise InstallError("manifest_invalid", "unsafe archive path")
    raw_parts = name.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise InstallError("manifest_invalid", "unsafe archive path")
    pure = PurePosixPath(name)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise InstallError("manifest_invalid", "unsafe archive path")
    target = root.joinpath(*pure.parts)
    if not target.resolve(strict=False).is_relative_to(root.resolve()):
        raise InstallError("manifest_invalid", "archive path escapes destination")
    return target


def _open_verified_response(
    url: str,
    opener: _Opener | None,
    *,
    code: str = "artifact_download_failed",
    allow_api_query: bool = False,
) -> _Response:
    """显式跟随并重新验证每一跳 redirect，返回最终 response。"""
    current = url
    previous: str | None = None
    selected: _Opener = opener if opener is not None else _stdlib_opener()
    for redirect_count in range(_MAX_REDIRECTS + 1):
        _validate_download_url(current, previous, allow_api_query=allow_api_query, code=code)
        request = Request(
            current,
            headers={
                "Accept": "application/octet-stream, application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "miniclaw-installer/0.7",
            },
            method="GET",
        )
        try:
            response = selected.open(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS)
        except HTTPError as error:
            response = error  # HTTPError also implements the response interface.
        except (URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as error:
            raise InstallError(code, "artifacts") from error
        status = _response_status(response)
        if status not in _REDIRECT_STATUSES:
            return response
        try:
            location = _header(response, "Location", code)
        finally:
            _close_response(response)
        if location is None or redirect_count >= _MAX_REDIRECTS:
            raise InstallError(code, "artifacts")
        previous, current = current, urljoin(current, location)
    raise InstallError(code, "artifacts")


def _read_bounded_url(
    url: str,
    max_bytes: int,
    opener: _Opener | None,
    *,
    code: str,
    allow_api_query: bool = False,
) -> bytes:
    """读取一个 allowlisted URL，并同时执行 header 和实际字节预算。"""
    response: _Response | None = None
    try:
        response = _open_verified_response(
            url,
            opener,
            code=code,
            allow_api_query=allow_api_query,
        )
        if _response_status(response) != 200:
            raise InstallError(code, "manifest")
        _require_identity_encoding(response, code)
        declared_size = _content_length(response, code)
        if declared_size is not None and declared_size > max_bytes:
            raise InstallError(code, "manifest")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(min(_CHUNK_BYTES, max_bytes - total + 1))
            if not isinstance(chunk, bytes):
                raise InstallError(code, "manifest")
            if not chunk:
                break
            if total + len(chunk) > max_bytes:
                raise InstallError(code, "manifest")
            chunks.append(chunk)
            total += len(chunk)
        if declared_size is not None and total != declared_size:
            raise InstallError(code, "manifest")
        return b"".join(chunks)
    except InstallError:
        raise
    except (
        HTTPError,
        URLError,
        TimeoutError,
        OSError,
        http.client.HTTPException,
        ValueError,
    ) as error:
        raise InstallError(code, "manifest") from error
    finally:
        if response is not None:
            _close_response(response)


def _stdlib_opener() -> OpenerDirector:
    """构造禁用自动 redirect 的 stdlib opener。"""
    return build_opener(_NoRedirectHandler())


def _validate_download_url(
    url: str,
    previous: str | None,
    *,
    allow_api_query: bool,
    code: str,
) -> None:
    """验证 initial URL 与 redirect target 的 scheme、parts、host 和 path。"""
    if (
        type(url) is not str
        or not url.isascii()
        or len(url) > 8192
        or not url.startswith("https://")
        or any(character.isspace() for character in url)
    ):
        raise InstallError(code, "artifacts.url")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise InstallError(code, "artifacts.url") from error
    if (
        parts.scheme != "https"
        or parts.username is not None
        or parts.password is not None
        or port is not None
        or parts.fragment
        or not parts.path.startswith("/")
        or "%" in parts.path
        or "\\" in parts.path
    ):
        raise InstallError(code, "artifacts.url")
    host = parts.hostname
    query_allowed = allow_api_query and url == _RELEASES_API_URL
    if parts.query and not query_allowed:
        raise InstallError(code, "artifacts.url")
    allowed = (
        (host == "github.com" and parts.path.startswith(_GITHUB_RELEASE_PREFIX))
        or (host == "github.com" and parts.path.startswith("/astral-sh/uv/releases/"))
        or (host == "api.github.com" and query_allowed)
        or (host == "files.pythonhosted.org" and parts.path.startswith("/packages/"))
        or (host == "nodejs.org" and parts.path.startswith("/dist/"))
        or (host == "astral.sh" and parts.path.startswith("/uv/"))
        or (host == "release-assets.githubusercontent.com" and bool(parts.path.strip("/")))
    )
    if not allowed:
        raise InstallError(code, "artifacts.url")
    if previous is None:
        if host == "release-assets.githubusercontent.com":
            raise InstallError(code, "artifacts.url")
        return
    previous_parts = urlsplit(previous)
    if host == previous_parts.hostname:
        return
    if not (
        previous_parts.hostname == "github.com"
        and previous_parts.path.startswith(_GITHUB_RELEASE_PREFIX)
        and host == "release-assets.githubusercontent.com"
    ):
        raise InstallError(code, "artifacts.url")


def _response_status(response: _Response) -> int:
    """兼容 urllib 与测试 fake 读取 exact HTTP status。"""
    status = getattr(response, "status", None)
    if type(status) is int:
        return status
    getter = getattr(response, "getcode", None)
    value = getter() if callable(getter) else None
    return value if type(value) is int else 0


def _close_response(response: _Response) -> None:
    """关闭 response 且不让 close failure 跳过安全清理。"""
    try:
        response.close()
    except (OSError, http.client.HTTPException):
        pass


def _header(response: _Response, name: str, code: str) -> str | None:
    """大小写不敏感地读取唯一 header，并拒绝重复安全字段。"""
    headers = getattr(response, "headers", None)
    if headers is not None:
        items = getattr(headers, "items", None)
        if callable(items):
            values = []
            for key, value in items():
                if str(key).casefold() == name.casefold():
                    values.append(str(value))
            if len(values) > 1:
                raise InstallError(code, "artifacts")
            if values:
                return values[0]
    getter = getattr(response, "getheader", None)
    if callable(getter):
        value = getter(name)
        return str(value) if value is not None else None
    return None


def _content_length(response: _Response, code: str) -> int | None:
    """解析非负十进制 Content-Length，不接受符号或空白。"""
    value = _header(response, "Content-Length", code)
    if value is None:
        return None
    if not value.isascii() or not value.isdecimal():
        raise InstallError(code, "artifacts.size")
    return int(value)


def _require_identity_encoding(response: _Response, code: str) -> None:
    """拒绝会破坏 size/hash 语义的压缩 HTTP content encoding。"""
    value = _header(response, "Content-Encoding", code)
    if value is not None and value.casefold() != "identity":
        raise InstallError(code, "artifacts")


def _safe_absolute_path(path: object) -> bool:
    """判断输入是否为无控制字符的 bounded absolute Path。"""
    return (
        isinstance(path, Path)
        and path.is_absolute()
        and len(str(path)) <= 4096
        and all(character.isprintable() for character in str(path))
    )


def _lexists(path: Path) -> bool:
    """在不跟随 symlink 的情况下判断目录项是否存在。"""
    return os.path.lexists(path)


def _remove_owned_path(path: Path) -> None:
    """尽力删除本次调用创建的 file/tree，且不让 cleanup error 掩盖稳定错误。"""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        elif path.is_dir():
            shutil.rmtree(path)
    except OSError:
        pass


def _quarantine_failed_download(destination: Path) -> Path | None:
    """先把失败 final 原子移到唯一 private 路径，再交给 best-effort cleanup。"""
    descriptor: int | None = None
    cleanup: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=f".{destination.name}.cleanup-",
            dir=destination.parent,
        )
        os.close(descriptor)
        descriptor = None
        cleanup = Path(name)
        os.replace(destination, cleanup)
        return cleanup
    except OSError:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if cleanup is not None:
            _remove_owned_path(cleanup)
        return None


def _restore_empty_destination(destination: Path, previous: Path, original_moved: bool) -> None:
    """失败时尽力恢复调用前的 empty destination，并固定为 0700。"""
    try:
        if original_moved and _lexists(previous) and not _lexists(destination):
            os.replace(previous, destination)
        if not _lexists(destination):
            destination.mkdir(mode=0o700)
        if destination.is_dir() and not destination.is_symlink():
            destination.chmod(0o700)
    except OSError:
        pass


def _regular_file_without_symlink(path: Path) -> bool:
    """判断 path 自身是否为普通文件而不是 symlink。"""
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _validate_executable_paths(values: Collection[str]) -> frozenset[str]:
    """验证 manifest 声明的 executable paths 唯一且为安全 POSIX names。"""
    if isinstance(values, (str, bytes)) or not isinstance(values, Collection):
        raise InstallError("manifest_invalid", "manifest")
    names = tuple(values)
    if any(type(name) is not str for name in names) or len(names) != len(set(names)):
        raise InstallError("manifest_invalid", "manifest")
    scratch = Path("/")
    for name in names:
        _safe_member_path(scratch, name)
    return frozenset(names)


def _decompress_bounded_tar(
    archive: Path,
    output: BinaryIO,
    limits: ExtractionLimits,
) -> None:
    """把 gzip 流解到临时文件，并在 metadata 解析前限制 raw tar 字节。"""
    raw_limit = limits.max_bytes + limits.max_entries * 1024 + 10_240
    total = 0
    with archive.open("rb") as compressed, gzip.GzipFile(fileobj=compressed) as source:
        while True:
            chunk = source.read(min(_CHUNK_BYTES, raw_limit - total + 1))
            if not chunk:
                break
            if total + len(chunk) > raw_limit:
                raise InstallError("manifest_invalid", "manifest")
            output.write(chunk)
            total += len(chunk)
    output.flush()
    output.seek(0)


def _validate_raw_tar(archive: BinaryIO, limits: ExtractionLimits) -> None:
    """逐 block 拒绝 PAX/GNU/sparse pseudo headers 并验证 raw manifest 预算。"""
    archive.seek(0)
    entries = 0
    declared_bytes = 0
    zero_blocks = 0
    while True:
        block = archive.read(tarfile.BLOCKSIZE)
        if not block:
            break
        if len(block) != tarfile.BLOCKSIZE:
            raise InstallError("manifest_invalid", "manifest")
        if block == tarfile.NUL * tarfile.BLOCKSIZE:
            zero_blocks += 1
            continue
        if zero_blocks:
            raise InstallError("manifest_invalid", "manifest")
        try:
            member = tarfile.TarInfo.frombuf(block, "utf-8", "surrogateescape")
        except tarfile.HeaderError as error:
            raise InstallError("manifest_invalid", "manifest") from error
        if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE}:
            raise InstallError("manifest_invalid", "manifest")
        entries += 1
        if entries > limits.max_entries:
            raise InstallError("manifest_invalid", "manifest")
        if member.size < 0 or (member.type == tarfile.DIRTYPE and member.size):
            raise InstallError("manifest_invalid", "manifest")
        declared_bytes += member.size
        if declared_bytes > limits.max_bytes:
            raise InstallError("manifest_invalid", "manifest")
        padded_size = (member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE
        remaining = padded_size * tarfile.BLOCKSIZE
        while remaining:
            chunk = archive.read(min(_CHUNK_BYTES, remaining))
            if not chunk:
                raise InstallError("manifest_invalid", "manifest")
            remaining -= len(chunk)
    if entries == 0 or zero_blocks < 2:
        raise InstallError("manifest_invalid", "manifest")
    archive.seek(0)


def _validated_members(
    archive: BinaryIO,
    root: Path,
    limits: ExtractionLimits,
) -> tuple[tarfile.TarInfo, ...]:
    """首遍校验全部 header、type、path tree 和 declared budgets。"""
    members: list[tarfile.TarInfo] = []
    normalized: set[str] = set()
    normalized_prefixes: dict[tuple[str, ...], tuple[str, ...]] = {}
    regular: set[tuple[str, ...]] = set()
    paths: set[tuple[str, ...]] = set()
    declared_bytes = 0
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:") as source:
        for member in source:
            if len(members) >= limits.max_entries:
                raise InstallError("manifest_invalid", "manifest")
            _safe_member_path(root, member.name)
            parts = PurePosixPath(member.name).parts
            folded = unicodedata.normalize("NFC", member.name).casefold()
            if folded in normalized or parts in paths:
                raise InstallError("manifest_invalid", "manifest")
            for index in range(1, len(parts) + 1):
                original_prefix = parts[:index]
                folded_prefix = tuple(
                    unicodedata.normalize("NFC", part).casefold()
                    for part in original_prefix
                )
                previous_prefix = normalized_prefixes.get(folded_prefix)
                if previous_prefix is not None and previous_prefix != original_prefix:
                    raise InstallError("manifest_invalid", "manifest")
                normalized_prefixes[folded_prefix] = original_prefix
            if any(parts[:index] in regular for index in range(1, len(parts))):
                raise InstallError("manifest_invalid", "manifest")
            if member.isreg() and any(
                existing[: len(parts)] == parts for existing in paths if len(existing) > len(parts)
            ):
                raise InstallError("manifest_invalid", "manifest")
            if not member.isdir() and not member.isreg():
                raise InstallError("manifest_invalid", "manifest")
            if type(member.size) is not int or member.size < 0 or (member.isdir() and member.size):
                raise InstallError("manifest_invalid", "manifest")
            declared_bytes += member.size
            if declared_bytes > limits.max_bytes:
                raise InstallError("manifest_invalid", "manifest")
            normalized.add(folded)
            paths.add(parts)
            if member.isreg():
                regular.add(parts)
            members.append(member)
    if not members:
        raise InstallError("manifest_invalid", "manifest")
    return tuple(members)


def _extract_validated_members(
    archive: BinaryIO,
    root: Path,
    expected: tuple[tarfile.TarInfo, ...],
    limits: ExtractionLimits,
    executable_names: frozenset[str],
) -> None:
    """第二遍按 actual bytes 预算手工写出已验证的 regular/dir members。"""
    actual_total = 0
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:") as source:
        members = tuple(source)
        if tuple((item.name, item.type, item.size) for item in members) != tuple(
            (item.name, item.type, item.size) for item in expected
        ):
            raise InstallError("manifest_invalid", "manifest")
        for member in members:
            target = _safe_member_path(root, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.chmod(0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _chmod_parents(root, target.parent)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(target, flags, 0o600)
            extracted = source.extractfile(member)
            if extracted is None:
                os.close(descriptor)
                raise InstallError("manifest_invalid", "manifest")
            member_written = 0
            try:
                with os.fdopen(descriptor, "wb") as output, extracted:
                    while True:
                        chunk = extracted.read(
                            min(_CHUNK_BYTES, member.size - member_written + 1)
                        )
                        if not isinstance(chunk, bytes):
                            raise InstallError("manifest_invalid", "manifest")
                        if not chunk:
                            break
                        if (
                            member_written + len(chunk) > member.size
                            or actual_total + len(chunk) > limits.max_bytes
                        ):
                            raise InstallError("manifest_invalid", "manifest")
                        output.write(chunk)
                        member_written += len(chunk)
                        actual_total += len(chunk)
                    if member_written != member.size:
                        raise InstallError("artifact_hash_mismatch", "artifacts.size")
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), 0o700 if member.name in executable_names else 0o600)
            except BaseException:
                target.unlink(missing_ok=True)
                raise


def _chmod_parents(root: Path, directory: Path) -> None:
    """把 extraction root 下自动创建的每级父目录限制为 0700。"""
    current = directory
    while current != root:
        current.chmod(0o700)
        current = current.parent


def _fsync_tree(root: Path) -> None:
    """从深到浅 fsync extraction tree 的目录元数据。"""
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _fsync_directory(directory)
    _fsync_directory(root)


def _fsync_directory(directory: Path) -> None:
    """尽力 fsync 一个已验证目录。"""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory_best_effort(directory: Path) -> None:
    """尽力持久化 cleanup namespace，且不掩盖原始稳定错误。"""
    try:
        _fsync_directory(directory)
    except OSError:
        pass
