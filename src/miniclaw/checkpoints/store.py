"""有界、content-addressed 且不跟随 symlink 的 Checkpoint store。"""

import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from miniclaw.providers.base import JsonValue
from miniclaw.storage.database import Database

_SECRET_NAMES = frozenset(
    {
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials",
        "credentials.json",
        "secrets.json",
        "secrets.yaml",
        "token.json",
    }
)
_SECRET_DIRECTORIES = frozenset({".git", ".ssh", ".aws", ".gnupg", ".kube"})


class CheckpointError(ValueError):
    """表示 capture 违反路径、Secret、类型或资源配额边界。"""

    def __init__(self, code: str, message: str | None = None) -> None:
        """保存稳定错误码，不要求调用方解析底层异常。"""
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CheckpointEntry:
    """保存一个 Workspace 相对 regular file 的 before image 或 tombstone。"""

    path: str
    existed: bool
    sha256: str | None
    size: int
    mode: int | None

    def __post_init__(self) -> None:
        """拒绝绝对/逃逸路径和不一致的 tombstone 元数据。"""
        supplied = Path(self.path)
        if (
            not self.path
            or supplied.is_absolute()
            or ".." in supplied.parts
            or any(ord(character) < 32 for character in self.path)
        ):
            raise CheckpointError("checkpoint_manifest_invalid")
        if self.existed:
            if (
                self.sha256 is None
                or len(self.sha256) != 64
                or any(character not in "0123456789abcdef" for character in self.sha256)
                or type(self.size) is not int
                or self.size < 0
                or type(self.mode) is not int
                or not 0 <= self.mode <= 0o777
            ):
                raise CheckpointError("checkpoint_manifest_invalid")
        elif self.sha256 is not None or self.size != 0 or self.mode is not None:
            raise CheckpointError("checkpoint_manifest_invalid")

    def as_json(self) -> dict[str, JsonValue]:
        """返回不含绝对路径或内容的稳定 JSON object。"""
        return {
            "existed": self.existed,
            "mode": self.mode,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
        }

    @classmethod
    def from_json(cls, value: object) -> Self:
        """严格恢复 manifest entry。"""
        if not isinstance(value, dict) or set(value) != {
            "existed",
            "mode",
            "path",
            "sha256",
            "size",
        }:
            raise CheckpointError("checkpoint_manifest_invalid")
        try:
            return cls(
                path=_string(value["path"]),
                existed=_boolean(value["existed"]),
                sha256=_optional_string(value["sha256"]),
                size=_integer(value["size"]),
                mode=_optional_integer(value["mode"]),
            )
        except (TypeError, CheckpointError):
            raise CheckpointError("checkpoint_manifest_invalid") from None


@dataclass(frozen=True, slots=True)
class CheckpointManifest:
    """表示已经持久化、可恢复的一组 before images。"""

    id: int
    owner_id: int
    reason: str
    entries: tuple[CheckpointEntry, ...]
    manifest_hash: str
    total_bytes: int
    status: str
    created_at: datetime

    @property
    def canonical_json(self) -> str:
        """返回用于 hash/SQLite 的 versioned canonical manifest JSON。"""
        return _manifest_json(self.reason, self.entries)


class CheckpointStore:
    """捕获 Workspace regular files 到 bounded CAS，并保存 manifest 事实。"""

    def __init__(
        self,
        database: Database,
        *,
        owner_id: int,
        workspace: Path,
        state_home: Path,
        max_entries: int,
        max_total_bytes: int,
        max_file_bytes: int,
        max_count: int,
    ) -> None:
        """绑定路径和严格正整数配额。"""
        limits = (max_entries, max_total_bytes, max_file_bytes, max_count)
        if any(type(value) is not int or value <= 0 for value in limits):
            raise ValueError("checkpoint limits must be positive integers")
        if max_file_bytes > max_total_bytes:
            raise ValueError("max_file_bytes must not exceed max_total_bytes")
        if not workspace.is_absolute() or not state_home.is_absolute():
            raise ValueError("checkpoint paths must be absolute")
        self._database = database
        self._owner_id = owner_id
        self._workspace = workspace.resolve()
        self._state_home = state_home.resolve()
        self._root = self._state_home / "checkpoints"
        self._blobs = self._root / "blobs"
        self._max_entries = max_entries
        self._max_total_bytes = max_total_bytes
        self._max_file_bytes = max_file_bytes
        self._max_count = max_count

    @property
    def database(self) -> Database:
        """返回供 Rollback 原子状态更新使用的数据库。"""
        return self._database

    @property
    def workspace(self) -> Path:
        """返回 canonical Workspace root。"""
        return self._workspace

    def capture(
        self,
        paths: tuple[Path, ...],
        *,
        reason: str,
        now: datetime,
        turn_id: int | None = None,
        task_run_id: int | None = None,
        tool_run_id: int | None = None,
    ) -> CheckpointManifest:
        """在配额内捕获 before image，blob durable 后再提交 manifest。"""
        if not paths:
            raise CheckpointError("checkpoint_path_denied", "paths must not be empty")
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 64
            or any(ord(character) < 32 for character in reason)
        ):
            raise CheckpointError("checkpoint_reason_invalid")
        if now.tzinfo is None:
            raise ValueError("checkpoint timestamp must be timezone-aware")
        candidates = self._expand(paths)
        if len(candidates) > self._max_entries:
            raise CheckpointError("checkpoint_budget_exceeded")
        entries: list[CheckpointEntry] = []
        total_bytes = 0
        for relative, path in candidates:
            if _is_secret(relative):
                raise CheckpointError("checkpoint_secret_path_denied")
            if path.is_symlink():
                raise CheckpointError("checkpoint_symlink_denied")
            if not path.exists():
                entries.append(CheckpointEntry(relative, False, None, 0, None))
                continue
            content, mode = self._read_regular(path)
            total_bytes += len(content)
            if total_bytes > self._max_total_bytes:
                raise CheckpointError("checkpoint_budget_exceeded")
            digest = hashlib.sha256(content).hexdigest()
            self._persist_blob(digest, content)
            entries.append(CheckpointEntry(relative, True, digest, len(content), mode))
        ordered = tuple(sorted(entries, key=lambda entry: entry.path))
        manifest_json = _manifest_json(reason.strip(), ordered)
        manifest_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        created_at = now.astimezone(UTC)
        with self._database.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO checkpoints (owner_id, turn_id, task_run_id, tool_run_id, "
                "manifest_json, manifest_hash, status, total_bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'captured', ?, ?)",
                (
                    self._owner_id,
                    turn_id,
                    task_run_id,
                    tool_run_id,
                    manifest_json,
                    manifest_hash,
                    total_bytes,
                    created_at.isoformat(),
                ),
            )
            checkpoint_id = int(cursor.lastrowid)
            self._expire_old(connection)
        return CheckpointManifest(
            checkpoint_id,
            self._owner_id,
            reason.strip(),
            ordered,
            manifest_hash,
            total_bytes,
            "captured",
            created_at,
        )

    def get(self, checkpoint_id: int) -> CheckpointManifest:
        """读取 owner checkpoint，并重新校验 manifest hash/entries。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE id = ? AND owner_id = ?",
                (checkpoint_id, self._owner_id),
            ).fetchone()
        if row is None:
            raise CheckpointError("checkpoint_not_found")
        reason, entries = _decode_manifest(row["manifest_json"])
        expected = hashlib.sha256(row["manifest_json"].encode("utf-8")).hexdigest()
        if expected != row["manifest_hash"]:
            raise CheckpointError("checkpoint_manifest_invalid")
        return CheckpointManifest(
            id=row["id"],
            owner_id=row["owner_id"],
            reason=reason,
            entries=entries,
            manifest_hash=row["manifest_hash"],
            total_bytes=row["total_bytes"],
            status=row["status"],
            created_at=_time(row["created_at"]),
        )

    def blob_path(self, digest: str) -> Path:
        """返回验证过的 CAS blob 路径。"""
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise CheckpointError("checkpoint_manifest_invalid")
        return self._blobs / digest[:2] / digest

    def read_blob(self, digest: str, expected_size: int) -> bytes:
        """有界读取并验证 CAS blob 的类型、size 与内容 hash。"""
        if type(expected_size) is not int or not 0 <= expected_size <= self._max_file_bytes:
            raise CheckpointError("checkpoint_blob_invalid")
        path = self.blob_path(digest)
        try:
            metadata = path.lstat()
        except OSError:
            raise CheckpointError("checkpoint_blob_invalid") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise CheckpointError("checkpoint_blob_invalid")
        content, _ = self._read_regular(path)
        if len(content) != expected_size or hashlib.sha256(content).hexdigest() != digest:
            raise CheckpointError("checkpoint_blob_invalid")
        return content

    def _expand(self, paths: tuple[Path, ...]) -> tuple[tuple[str, Path], ...]:
        """展开目录为稳定 regular-file 候选，并在遍历中拒绝 symlink。"""
        expanded: dict[str, Path] = {}
        for supplied in paths:
            path = supplied if supplied.is_absolute() else self._workspace / supplied
            lexical = Path(os.path.abspath(path))
            try:
                relative = lexical.relative_to(self._workspace)
            except ValueError:
                raise CheckpointError("checkpoint_path_denied") from None
            self._reject_parent_symlink(relative)
            relative_text = relative.as_posix()
            if _is_secret(relative_text):
                raise CheckpointError("checkpoint_secret_path_denied")
            if lexical.is_symlink():
                raise CheckpointError("checkpoint_symlink_denied")
            if lexical.is_dir():
                for root, directory_names, file_names in os.walk(lexical, followlinks=False):
                    root_path = Path(root)
                    for name in tuple(directory_names):
                        child = root_path / name
                        child_relative = child.relative_to(self._workspace).as_posix()
                        if child.is_symlink():
                            raise CheckpointError("checkpoint_symlink_denied")
                        if _is_secret(child_relative):
                            raise CheckpointError("checkpoint_secret_path_denied")
                    for name in file_names:
                        child = root_path / name
                        child_relative = child.relative_to(self._workspace).as_posix()
                        if child.is_symlink():
                            raise CheckpointError("checkpoint_symlink_denied")
                        if _is_secret(child_relative):
                            raise CheckpointError("checkpoint_secret_path_denied")
                        expanded[child_relative] = child
                        if len(expanded) > self._max_entries:
                            raise CheckpointError("checkpoint_budget_exceeded")
            else:
                expanded[relative_text] = lexical
        return tuple(sorted(expanded.items()))

    def _reject_parent_symlink(self, relative: Path) -> None:
        """逐段 lstat Workspace 内父路径，防止 capture 跟随目录 symlink。"""
        current = self._workspace
        for part in relative.parts[:-1]:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise CheckpointError("checkpoint_symlink_denied")
            except FileNotFoundError:
                break

    def _read_regular(self, path: Path) -> tuple[bytes, int]:
        """用 no-follow open/fstat identity check 读取有界 regular file。"""
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            raise CheckpointError("checkpoint_file_changed") from None
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise CheckpointError("checkpoint_file_type_denied")
            if before.st_size > self._max_file_bytes:
                raise CheckpointError("checkpoint_budget_exceeded")
            chunks: list[bytes] = []
            size = 0
            while chunk := os.read(descriptor, min(64 * 1024, self._max_file_bytes + 1)):
                size += len(chunk)
                if size > self._max_file_bytes:
                    raise CheckpointError("checkpoint_budget_exceeded")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise CheckpointError("checkpoint_file_changed")
            return b"".join(chunks), stat.S_IMODE(before.st_mode)
        finally:
            os.close(descriptor)

    def _persist_blob(self, digest: str, content: bytes) -> None:
        """同 filesystem 临时写、fsync、atomic replace 保存 owner-only blob。"""
        target = self.blob_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if target.exists():
            self.read_blob(digest, len(content))
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=".blob-", dir=target.parent)
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            target.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _expire_old(self, connection: sqlite3.Connection) -> None:
        """只过期超出 owner max_count 的旧 manifest，不删除共享 blob。"""
        rows = connection.execute(
            "SELECT id FROM checkpoints WHERE owner_id = ? AND status = 'captured' "
            "ORDER BY created_at DESC, id DESC",
            (self._owner_id,),
        ).fetchall()
        old = [row["id"] for row in rows[self._max_count :]]
        if old:
            placeholders = ",".join("?" for _ in old)
            connection.execute(
                f"UPDATE checkpoints SET status = 'expired' WHERE id IN ({placeholders})",
                old,
            )


def _manifest_json(reason: str, entries: tuple[CheckpointEntry, ...]) -> str:
    """编码不含绝对路径和内容的 versioned canonical manifest。"""
    value: dict[str, JsonValue] = {
        "entries": [entry.as_json() for entry in entries],
        "reason": reason,
        "schema_version": 1,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _decode_manifest(value: str) -> tuple[str, tuple[CheckpointEntry, ...]]:
    """严格恢复 canonical manifest，并拒绝未知字段/非标准 JSON。"""
    try:
        decoded = json.loads(value, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError):
        raise CheckpointError("checkpoint_manifest_invalid") from None
    if (
        not isinstance(decoded, dict)
        or set(decoded) != {"entries", "reason", "schema_version"}
        or decoded["schema_version"] != 1
        or not isinstance(decoded["entries"], list)
    ):
        raise CheckpointError("checkpoint_manifest_invalid")
    reason = _string(decoded["reason"])
    entries = tuple(CheckpointEntry.from_json(item) for item in decoded["entries"])
    if tuple(sorted(entries, key=lambda entry: entry.path)) != entries:
        raise CheckpointError("checkpoint_manifest_invalid")
    if _manifest_json(reason, entries) != value:
        raise CheckpointError("checkpoint_manifest_invalid")
    return reason, entries


def _is_secret(relative: str) -> bool:
    """识别凭据、状态 sidecar 与仓库内部元数据路径。"""
    parts = tuple(part.casefold() for part in Path(relative).parts)
    leaf = parts[-1] if parts else ""
    return (
        any(part.startswith(".env") for part in parts)
        or any(part in _SECRET_DIRECTORIES for part in parts)
        or leaf in _SECRET_NAMES
        or leaf.endswith((".pem", ".key", ".p12", ".pfx"))
        or leaf.endswith((".db-wal", ".db-shm", ".db-journal"))
        or leaf.endswith(".sock")
    )


def _string(value: object) -> str:
    """严格提取 JSON string。"""
    if not isinstance(value, str):
        raise TypeError
    return value


def _optional_string(value: object) -> str | None:
    """严格提取 nullable JSON string。"""
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    """严格提取 JSON integer。"""
    if type(value) is not int:
        raise TypeError
    return value


def _optional_integer(value: object) -> int | None:
    """严格提取 nullable JSON integer。"""
    return None if value is None else _integer(value)


def _boolean(value: object) -> bool:
    """严格提取 JSON boolean。"""
    if type(value) is not bool:
        raise TypeError
    return value


def _reject_json_constant(value: str) -> JsonValue:
    """拒绝 NaN/Infinity。"""
    raise ValueError(value)


def _time(value: str) -> datetime:
    """解析 timezone-aware UTC timestamp。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise CheckpointError("checkpoint_manifest_invalid")
    return parsed.astimezone(UTC)
