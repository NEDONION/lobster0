"""Checkpoint 的两步、冲突感知且原子替换 rollback。"""

import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from miniclaw.checkpoints.store import CheckpointError, CheckpointManifest, CheckpointStore
from miniclaw.providers.base import JsonValue

RollbackAction = Literal["restore", "delete", "conflict"]


class RollbackConflictError(CheckpointError):
    """表示 preview 后状态变化或目标已无法安全恢复。"""

    def __init__(self) -> None:
        """使用稳定 rollback_conflict 错误码。"""
        super().__init__("rollback_conflict")


@dataclass(frozen=True, slots=True)
class RollbackOperation:
    """记录一个相对路径、动作和 preview 时观察到的状态。"""

    path: str
    action: RollbackAction
    observed_kind: str
    observed_sha256: str | None

    def as_json(self) -> dict[str, JsonValue]:
        """返回用于 preview hash 的稳定 JSON object。"""
        return {
            "action": self.action,
            "observed_kind": self.observed_kind,
            "observed_sha256": self.observed_sha256,
            "path": self.path,
        }


@dataclass(frozen=True, slots=True)
class RollbackPreview:
    """绑定 checkpoint 与当前文件状态的精确操作预览。"""

    checkpoint_id: int
    manifest_hash: str
    operations: tuple[RollbackOperation, ...]
    sha256: str

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """返回需要执行动作的稳定相对路径。"""
        return tuple(operation.path for operation in self.operations)


@dataclass(frozen=True, slots=True)
class RollbackReceipt:
    """记录成功恢复的 preview hash 与相对路径。"""

    checkpoint_id: int
    preview_hash: str
    changed_paths: tuple[str, ...]
    completed_at: datetime


class RollbackService:
    """先 preview，再 compare-and-apply 恢复同一批 before images。"""

    def __init__(self, store: CheckpointStore) -> None:
        """绑定已经配置安全边界的 CheckpointStore。"""
        self._store = store

    def preview(self, checkpoint_id: int) -> RollbackPreview:
        """读取 checkpoint 并 hash 当前精确操作集，不产生文件副作用。"""
        manifest = self._store.get(checkpoint_id)
        if manifest.status != "captured":
            raise CheckpointError("checkpoint_not_restorable")
        operations: list[RollbackOperation] = []
        for entry in manifest.entries:
            target = self._safe_target(entry.path)
            kind, digest = _current_state(target)
            if entry.existed:
                if kind == "file" and digest == entry.sha256:
                    continue
                action: RollbackAction = "restore" if kind in {"file", "missing"} else "conflict"
            else:
                if kind == "missing":
                    continue
                action = "delete" if kind == "file" else "conflict"
            operations.append(RollbackOperation(entry.path, action, kind, digest))
        ordered = tuple(sorted(operations, key=lambda operation: operation.path))
        preview_json = _preview_json(manifest, ordered)
        return RollbackPreview(
            checkpoint_id,
            manifest.manifest_hash,
            ordered,
            hashlib.sha256(preview_json.encode("utf-8")).hexdigest(),
        )

    def apply(self, checkpoint_id: int, expected_preview_hash: str) -> RollbackReceipt:
        """重新验证 preview，先 stage 所有 restore，再执行 atomic replace/delete。"""
        preview = self.preview(checkpoint_id)
        if preview.sha256 != expected_preview_hash or any(
            operation.action == "conflict" for operation in preview.operations
        ):
            raise RollbackConflictError()
        manifest = self._store.get(checkpoint_id)
        entries = {entry.path: entry for entry in manifest.entries}
        staged: dict[str, Path] = {}
        try:
            for operation in preview.operations:
                if operation.action != "restore":
                    continue
                entry = entries[operation.path]
                assert entry.sha256 is not None and entry.mode is not None
                content = self._store.read_blob(entry.sha256, entry.size)
                target = self._safe_target(operation.path)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=".miniclaw-rollback-", dir=target.parent
                )
                temporary = Path(temporary_name)
                os.fchmod(descriptor, entry.mode)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                staged[operation.path] = temporary
            if self.preview(checkpoint_id).sha256 != expected_preview_hash:
                raise RollbackConflictError()
            for operation in preview.operations:
                target = self._safe_target(operation.path)
                if operation.action == "restore":
                    os.replace(staged.pop(operation.path), target)
                    _fsync_directory(target.parent)
                elif operation.action == "delete":
                    target.unlink()
                    _fsync_directory(target.parent)
        finally:
            for temporary in staged.values():
                temporary.unlink(missing_ok=True)
        completed_at = datetime.now(UTC)
        with self._store.database.connect() as connection:
            updated = connection.execute(
                "UPDATE checkpoints SET status = 'restored', restored_at = ? "
                "WHERE id = ? AND status = 'captured'",
                (completed_at.isoformat(), checkpoint_id),
            )
            if updated.rowcount != 1:
                raise RollbackConflictError()
            connection.execute(
                "INSERT INTO audit_events (event_type, user_id, summary, metadata_json, "
                "created_at) VALUES ('checkpoint.restored', ?, 'Restored checkpoint', ?, ?)",
                (
                    manifest.owner_id,
                    json.dumps(
                        {
                            "checkpoint_id": checkpoint_id,
                            "changed_count": len(preview.operations),
                            "preview_hash": preview.sha256[:12],
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    ),
                    completed_at.isoformat(),
                ),
            )
        return RollbackReceipt(
            checkpoint_id,
            preview.sha256,
            preview.changed_paths,
            completed_at,
        )

    def _safe_target(self, relative: str) -> Path:
        """把 manifest 相对路径映射回 Workspace，并拒绝父 symlink。"""
        target = self._store.workspace / relative
        try:
            target.relative_to(self._store.workspace)
        except ValueError:
            raise CheckpointError("checkpoint_manifest_invalid") from None
        current = self._store.workspace
        for part in Path(relative).parts[:-1]:
            current /= part
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise RollbackConflictError()
            except FileNotFoundError:
                raise RollbackConflictError() from None
        return target


def _current_state(path: Path) -> tuple[str, str | None]:
    """不跟随 symlink 地返回 missing/file/symlink/other 与内容 hash。"""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "missing", None
    if stat.S_ISLNK(metadata.st_mode):
        return "symlink", None
    if not stat.S_ISREG(metadata.st_mode):
        return "other", None
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return "other", None
    try:
        before = os.fstat(descriptor)
        while chunk := os.read(descriptor, 64 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        return "changed", None
    return "file", digest.hexdigest()


def _preview_json(
    manifest: CheckpointManifest,
    operations: tuple[RollbackOperation, ...],
) -> str:
    """编码绑定 manifest 与 observed current state 的 canonical preview。"""
    value: dict[str, JsonValue] = {
        "checkpoint_id": manifest.id,
        "manifest_hash": manifest.manifest_hash,
        "operations": [operation.as_json() for operation in operations],
        "schema_version": 1,
    }
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _fsync_directory(path: Path) -> None:
    """尽力 fsync parent directory，使 atomic rename durable。"""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
