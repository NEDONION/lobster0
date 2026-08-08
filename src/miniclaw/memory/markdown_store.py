"""以 owner-only 原子 Markdown 文件保存已接受的 Memory Unit。"""

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.memory.models import SourceRef
from miniclaw.memory.repository import MemoryManifestRepository
from miniclaw.paths import StatePaths

_PARSER_VERSION = "memory-markdown-v1"
_RELATIVE_PATH = "memory.md"
_HEADER = "# MiniClaw Memory\n\n<!-- format: memory-markdown-v1 -->\n"


class MarkdownMemoryError(RuntimeError):
    """表示 Markdown 路径、manifest、编码或原子发布失败。"""


@dataclass(frozen=True, slots=True)
class MarkdownUnitDocument:
    """描述写入 Markdown 真相源的完整、可核验 Unit block。"""

    unit_id: str
    owner_id: int
    key: str
    text: str
    kind: str
    scope: str
    status: str
    confidence: float
    sensitivity: str
    valid_from: datetime
    valid_until: datetime | None
    sources: tuple[SourceRef, ...]

    def __post_init__(self) -> None:
        """拒绝可能破坏 block 边界或 Owner 隔离的字段。"""
        if (
            not self.unit_id
            or len(self.unit_id) > 160
            or any(character.isspace() for character in self.unit_id)
        ):
            raise ValueError("memory unit_id is invalid")
        if type(self.owner_id) is not int or self.owner_id <= 0:
            raise ValueError("memory owner_id is invalid")
        for value, name, maximum in (
            (self.key, "key", 200),
            (self.text, "text", 8_000),
            (self.kind, "kind", 120),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"memory {name} is invalid")
        if "<!-- miniclaw:" in self.text or "```miniclaw-memory" in self.text:
            raise ValueError("memory text contains reserved markers")
        if self.scope not in {"private", "public", "group"}:
            raise ValueError("memory scope is invalid")
        if self.status not in {
            "observed",
            "short_term",
            "review_required",
            "active",
            "rejected",
            "superseded",
            "archived",
            "expired",
        }:
            raise ValueError("memory status is invalid")
        if self.sensitivity not in {"low", "medium", "high"}:
            raise ValueError("memory sensitivity is invalid")
        if (
            not isinstance(self.confidence, (int, float))
            or isinstance(self.confidence, bool)
            or not 0 <= float(self.confidence) <= 1
        ):
            raise ValueError("memory confidence is invalid")
        _time_text(self.valid_from)
        if self.valid_until is not None and self.valid_until <= self.valid_from:
            raise ValueError("memory valid_until must follow valid_from")
        if not self.sources:
            raise ValueError("memory unit requires sources")

    def as_dict(self) -> dict[str, object]:
        """返回可用于测试或兼容构造的原始字段副本。"""
        return {
            "unit_id": self.unit_id,
            "owner_id": self.owner_id,
            "key": self.key,
            "text": self.text,
            "kind": self.kind,
            "scope": self.scope,
            "status": self.status,
            "confidence": self.confidence,
            "sensitivity": self.sensitivity,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "sources": self.sources,
        }


@dataclass(frozen=True, slots=True)
class MarkdownWrite:
    """描述 Unit block 的 recorded/duplicate 原子写入结果。"""

    status: str
    path: Path
    content_hash: str
    block_hash: str


@dataclass(frozen=True, slots=True)
class MarkdownBatchWrite:
    """描述一次原子多 Unit 更新及每个最终 block hash。"""

    status: str
    path: Path
    content_hash: str
    block_hashes: dict[str, str]


@dataclass(frozen=True, slots=True)
class MarkdownSource:
    """描述供 Reconciler 严格解析的安全 Markdown 文件快照。"""

    path: Path
    payload: bytes
    content_hash: str
    mtime_ns: int


class MemoryMarkdownStore:
    """在 ``memory/owners/<owner>/memory.md`` 原子追加稳定 Unit block。"""

    def __init__(
        self,
        paths: StatePaths,
        manifests: MemoryManifestRepository,
    ) -> None:
        """绑定固定状态路径和 SQLite manifest Repository。"""
        self._paths = paths
        self._manifests = manifests

    def path_for_owner(self, owner_id: int) -> Path:
        """从受信 Owner ID 推导不可由模型控制的 Markdown 路径。"""
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("memory owner_id is invalid")
        return self._paths.memory_dir / "owners" / str(owner_id) / _RELATIVE_PATH

    def read_for_reconcile(self, owner_id: int) -> MarkdownSource | None:
        """安全读取 Owner Markdown；文件尚不存在时返回 None。"""
        path = self.path_for_owner(owner_id)
        if not path.exists():
            return None
        payload = _read_existing(path)
        metadata = path.stat(follow_symlinks=False)
        return MarkdownSource(
            path,
            payload,
            hashlib.sha256(payload).hexdigest(),
            metadata.st_mtime_ns,
        )

    def append(self, unit: MarkdownUnitDocument) -> MarkdownWrite:
        """持锁比较 manifest，幂等追加 Unit，并 fsync + replace 完整文件。"""
        path = self.path_for_owner(unit.owner_id)
        owner_directory = self._prepare_owner_directory(unit.owner_id)
        lock_path = owner_directory / ".memory.lock"
        lock_descriptor = _open_lock(lock_path)
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            existing = _read_existing(path)
            existing_hash = hashlib.sha256(existing).hexdigest()
            manifest = self._manifests.find(unit.owner_id, _RELATIVE_PATH)
            if manifest is not None and (
                manifest.status != "current" or manifest.content_hash != existing_hash
            ):
                raise MarkdownMemoryError("memory Markdown changed outside the writer")
            block = _unit_block(unit)
            block_hash = hashlib.sha256(block).hexdigest()
            marker = f"<!-- miniclaw:unit {unit.unit_id} -->".encode()
            if marker in existing:
                if block.rstrip() not in existing:
                    raise MarkdownMemoryError("memory Unit id has conflicting Markdown content")
                return MarkdownWrite("duplicate", path, existing_hash, block_hash)
            current = existing or _HEADER.encode("utf-8")
            payload = current.rstrip() + b"\n\n" + block
            content_hash = hashlib.sha256(payload).hexdigest()
            _atomic_replace(path, payload)
            metadata = path.stat()
            self._manifests.upsert(
                owner_id=unit.owner_id,
                relative_path=_RELATIVE_PATH,
                content_hash=content_hash,
                last_valid_hash=content_hash,
                mtime_ns=metadata.st_mtime_ns,
                parser_version=_PARSER_VERSION,
                status="current",
                now=datetime.now(UTC),
            )
            return MarkdownWrite("recorded", path, content_hash, block_hash)
        except MarkdownMemoryError:
            raise
        except (OSError, UnicodeError) as error:
            raise MarkdownMemoryError("memory Markdown could not be stored") from error
        finally:
            os.close(lock_descriptor)

    def upsert(self, unit: MarkdownUnitDocument) -> MarkdownWrite:
        """原子创建或替换一个完整 Unit block，供来源补充和状态晋升。"""
        path = self.path_for_owner(unit.owner_id)
        owner_directory = self._prepare_owner_directory(unit.owner_id)
        lock_descriptor = _open_lock(owner_directory / ".memory.lock")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            existing = _read_existing(path)
            existing_hash = hashlib.sha256(existing).hexdigest()
            manifest = self._manifests.find(unit.owner_id, _RELATIVE_PATH)
            if manifest is not None and (
                manifest.status != "current" or manifest.content_hash != existing_hash
            ):
                raise MarkdownMemoryError("memory Markdown changed outside the writer")
            block = _unit_block(unit)
            block_hash = hashlib.sha256(block).hexdigest()
            start_marker = f"<!-- miniclaw:unit {unit.unit_id} -->".encode()
            end_marker = f"<!-- miniclaw:end {unit.unit_id} -->".encode()
            start = existing.find(start_marker)
            if start < 0:
                current = existing or _HEADER.encode("utf-8")
                payload = current.rstrip() + b"\n\n" + block
            else:
                end = existing.find(end_marker, start)
                if end < 0 or existing.find(start_marker, start + 1) >= 0:
                    raise MarkdownMemoryError("memory Unit markers are invalid")
                end += len(end_marker)
                if end < len(existing) and existing[end : end + 1] == b"\n":
                    end += 1
                if existing[start:end] == block:
                    return MarkdownWrite("duplicate", path, existing_hash, block_hash)
                payload = existing[:start] + block + existing[end:]
            content_hash = hashlib.sha256(payload).hexdigest()
            _atomic_replace(path, payload)
            metadata = path.stat()
            self._manifests.upsert(
                owner_id=unit.owner_id,
                relative_path=_RELATIVE_PATH,
                content_hash=content_hash,
                last_valid_hash=content_hash,
                mtime_ns=metadata.st_mtime_ns,
                parser_version=_PARSER_VERSION,
                status="current",
                now=datetime.now(UTC),
            )
            return MarkdownWrite("recorded", path, content_hash, block_hash)
        except MarkdownMemoryError:
            raise
        except (OSError, UnicodeError) as error:
            raise MarkdownMemoryError("memory Markdown could not be stored") from error
        finally:
            os.close(lock_descriptor)

    def upsert_many(
        self,
        units: tuple[MarkdownUnitDocument, ...],
    ) -> MarkdownBatchWrite:
        """在一次 fsync/replace 中原子创建或替换同 Owner 的多个完整 Unit。"""
        if not units:
            raise ValueError("memory batch requires at least one Unit")
        owner_id = units[0].owner_id
        if any(unit.owner_id != owner_id for unit in units):
            raise ValueError("memory batch Units must share one owner")
        if len({unit.unit_id for unit in units}) != len(units):
            raise ValueError("memory batch Unit ids must be unique")
        path = self.path_for_owner(owner_id)
        owner_directory = self._prepare_owner_directory(owner_id)
        lock_descriptor = _open_lock(owner_directory / ".memory.lock")
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
            existing = _read_existing(path)
            existing_hash = hashlib.sha256(existing).hexdigest()
            manifest = self._manifests.find(owner_id, _RELATIVE_PATH)
            if manifest is not None and (
                manifest.status != "current" or manifest.content_hash != existing_hash
            ):
                raise MarkdownMemoryError("memory Markdown changed outside the writer")
            payload = existing or _HEADER.encode("utf-8")
            block_hashes: dict[str, str] = {}
            for unit in units:
                block = _unit_block(unit)
                block_hashes[unit.unit_id] = hashlib.sha256(block).hexdigest()
                payload = _replace_or_append(payload, unit.unit_id, block)
            content_hash = hashlib.sha256(payload).hexdigest()
            if payload == existing:
                return MarkdownBatchWrite(
                    "duplicate",
                    path,
                    existing_hash,
                    block_hashes,
                )
            _atomic_replace(path, payload)
            metadata = path.stat()
            self._manifests.upsert(
                owner_id=owner_id,
                relative_path=_RELATIVE_PATH,
                content_hash=content_hash,
                last_valid_hash=content_hash,
                mtime_ns=metadata.st_mtime_ns,
                parser_version=_PARSER_VERSION,
                status="current",
                now=datetime.now(UTC),
            )
            return MarkdownBatchWrite("recorded", path, content_hash, block_hashes)
        except MarkdownMemoryError:
            raise
        except (OSError, UnicodeError) as error:
            raise MarkdownMemoryError("memory Markdown could not be stored") from error
        finally:
            os.close(lock_descriptor)

    def _prepare_owner_directory(self, owner_id: int) -> Path:
        """创建并验证 owners/Owner 两级真实 0700 目录。"""
        owners = self._paths.memory_dir / "owners"
        _ensure_private_directory(owners)
        owner_directory = owners / str(owner_id)
        _ensure_private_directory(owner_directory)
        return owner_directory


def _unit_block(unit: MarkdownUnitDocument) -> bytes:
    """把 Unit 编码为人类可读正文和机器可解析 metadata block。"""
    metadata = {
        "confidence": float(unit.confidence),
        "kind": unit.kind,
        "scope": unit.scope,
        "sensitivity": unit.sensitivity,
        "sources": [
            {
                "channel": source.channel,
                "message_id": source.message_id,
                "session_id": source.session_id,
            }
            for source in unit.sources
        ],
        "status": unit.status,
        "text_sha256": hashlib.sha256(unit.text.strip().encode("utf-8")).hexdigest(),
        "valid_from": _time_text(unit.valid_from),
        "valid_until": (
            None if unit.valid_until is None else _time_text(unit.valid_until)
        ),
    }
    encoded = json.dumps(
        metadata,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"<!-- miniclaw:unit {unit.unit_id} -->\n"
        f"## {unit.key}\n\n"
        f"{unit.text.strip()}\n\n"
        "```miniclaw-memory\n"
        f"{encoded}\n"
        "```\n"
        f"<!-- miniclaw:end {unit.unit_id} -->\n"
    ).encode()


def _replace_or_append(payload: bytes, unit_id: str, block: bytes) -> bytes:
    """在内存中的完整 Markdown 文档替换一个 block，缺失时追加。"""
    start_marker = f"<!-- miniclaw:unit {unit_id} -->".encode()
    end_marker = f"<!-- miniclaw:end {unit_id} -->".encode()
    start = payload.find(start_marker)
    if start < 0:
        return payload.rstrip() + b"\n\n" + block
    end = payload.find(end_marker, start)
    if end < 0 or payload.find(start_marker, start + 1) >= 0:
        raise MarkdownMemoryError("memory Unit markers are invalid")
    end += len(end_marker)
    if end < len(payload) and payload[end : end + 1] == b"\n":
        end += 1
    if payload[start:end] == block:
        return payload
    return payload[:start] + block + payload[end:]


def _ensure_private_directory(path: Path) -> None:
    """创建或验证真实 owner-only 目录，绝不跟随 symlink。"""
    if path.is_symlink():
        raise MarkdownMemoryError("memory owner directory must not be a symlink")
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise MarkdownMemoryError("memory owner directory is not safe") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise MarkdownMemoryError("memory owner directory is not safe")
    if metadata.st_mode & 0o077:
        os.chmod(path, 0o700, follow_symlinks=False)


def _open_lock(path: Path) -> int:
    """以 O_NOFOLLOW 打开 owner-only lock file。"""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise MarkdownMemoryError("memory lock file is not safe")
        return descriptor
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


def _read_existing(path: Path) -> bytes:
    """读取当前完整 Markdown，并拒绝 symlink、宽权限和非法 UTF-8。"""
    if not path.exists():
        return b""
    if path.is_symlink():
        raise MarkdownMemoryError("memory Markdown must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise MarkdownMemoryError("memory Markdown file is not safe")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        payload.decode("utf-8")
        return payload
    except (OSError, UnicodeError) as error:
        raise MarkdownMemoryError("memory Markdown could not be read") from error
    finally:
        if "descriptor" in locals():
            os.close(descriptor)


def _atomic_replace(path: Path, payload: bytes) -> None:
    """写同目录临时文件，fsync 后 replace，并再次 fsync 父目录。"""
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".memory.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = os.open(path.parent, directory_flags)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except OSError as error:
        raise MarkdownMemoryError("memory Markdown atomic replace failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _time_text(value: datetime) -> str:
    """要求带时区时间并编码为 UTC ISO 文本。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat()
