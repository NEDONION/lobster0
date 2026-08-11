"""有界、content-addressed 且只保存 metadata 到 SQLite 的 Artifact Store。"""

import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.providers.base import JsonValue
from lobster0.storage.database import Database

_MEDIA_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/json": ".json",
    "text/plain": ".txt",
    "text/csv": ".csv",
}
_SOURCES = frozenset({"browser_screenshot", "browser_download", "user_upload"})
_LINK_ORIGINS = frozenset({"user_upload", "agent_output"})
_MAX_LIST_LIMIT = 500
_MAX_IMAGE_DIMENSION = 16_384
_MAX_IMAGE_PIXELS = 64_000_000


class ArtifactError(RuntimeError):
    """表示 Artifact 输入、存储或读取命中稳定安全边界。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可安全返回给 Tool 的短错误码和公开消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Artifact:
    """保存一个已验证 Artifact 的 metadata 与私有本地路径。"""

    artifact_id: str
    content_hash: str
    media_type: str
    byte_size: int
    source: str
    path: Path
    created_at: datetime
    expires_at: datetime
    width: int | None = None
    height: int | None = None

    def to_tool_payload(self) -> dict[str, JsonValue]:
        """只向模型公开 ID、hash、类型、大小和可选图片尺寸。"""
        payload: dict[str, JsonValue] = {
            "artifact_id": self.artifact_id,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
        }
        if self.width is not None and self.height is not None:
            payload.update({"width": self.width, "height": self.height})
        return payload


@dataclass(frozen=True, slots=True)
class ArtifactLink:
    """一条 Artifact 在某个会话里的出现，只含可安全展示的摘要。"""

    artifact_id: str
    media_type: str
    byte_size: int
    origin: str
    message_id: int | None
    created_at: datetime
    filename: str | None = None
    width: int | None = None
    height: int | None = None


class ArtifactStore:
    """消费私有 staging 文件并原子投影到 content-addressed Store。"""

    def __init__(
        self,
        database: Database,
        *,
        owner_id: int,
        root: Path,
        staging_root: Path,
        max_bytes: int,
        ttl_seconds: int = 86_400,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定 Owner、私有目录、单文件配额和默认 TTL。"""
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("artifact owner_id must be positive")
        if type(max_bytes) is not int or not 1 <= max_bytes <= 100 * 1024 * 1024:
            raise ValueError("artifact max_bytes is invalid")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 30 * 86_400:
            raise ValueError("artifact ttl_seconds is invalid")
        self._database = database
        self._owner_id = owner_id
        self._root = _private_directory(root)
        self._staging_root = _private_directory(staging_root)
        self._max_bytes = max_bytes
        self._ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def put(
        self,
        source_path: Path,
        *,
        declared_media_type: str,
        source: str,
    ) -> Artifact:
        """验证并消费 Worker staging 文件，返回无正文的 Artifact metadata。

        Args:
            source_path: Worker 在专用 staging root 直接创建的普通文件。
            declared_media_type: Worker 根据固定动作或下载后缀声明的 MIME。
            source: ``browser_screenshot`` 或 ``browser_download``。

        Returns:
            已去重并持久化的 Artifact。

        Raises:
            ArtifactError: 路径、类型、大小、magic、图片尺寸或 Store 状态不安全。
            OSError: 私有文件无法原子写入时原样抛出。
        """
        if declared_media_type not in _MEDIA_EXTENSIONS:
            raise ArtifactError("artifact_media_denied", "artifact media type is denied")
        if source not in _SOURCES:
            raise ArtifactError("artifact_source_denied", "artifact source is denied")
        candidate = Path(source_path)
        safe_to_consume = candidate.parent == self._staging_root
        try:
            if not safe_to_consume:
                raise ArtifactError("artifact_source_denied", "artifact source is denied")
            data = self._read_staging(candidate)
            actual_media_type, width, height = _inspect_content(data, declared_media_type)
            if actual_media_type != declared_media_type:
                raise ArtifactError(
                    "artifact_media_mismatch",
                    "artifact media type does not match content",
                )
            content_hash = hashlib.sha256(data).hexdigest()
            artifact_id = f"art_{content_hash}"
            relative_path = Path(content_hash[:2]) / (
                content_hash + _MEDIA_EXTENSIONS[actual_media_type]
            )
            target = self._root / relative_path
            if target.exists() or target.is_symlink():
                if not _matches_private_target(target, content_hash, self._max_bytes):
                    raise ArtifactError("artifact_store_corrupt", "artifact store is corrupt")
            else:
                _write_private_atomic(target, data)
            now = self._now()
            expires_at = now + timedelta(seconds=self._ttl_seconds)
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO artifacts (
                        artifact_id, owner_id, content_hash, media_type, byte_size,
                        source, relative_path, width, height, status, created_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    ON CONFLICT(owner_id, content_hash) DO UPDATE SET
                        media_type = excluded.media_type,
                        byte_size = excluded.byte_size,
                        source = excluded.source,
                        relative_path = excluded.relative_path,
                        width = excluded.width,
                        height = excluded.height,
                        status = 'active',
                        expires_at = excluded.expires_at,
                        deleted_at = NULL
                    """,
                    (
                        artifact_id,
                        self._owner_id,
                        content_hash,
                        actual_media_type,
                        len(data),
                        source,
                        relative_path.as_posix(),
                        width,
                        height,
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
            return self.read_metadata(artifact_id)
        finally:
            if safe_to_consume:
                candidate.unlink(missing_ok=True)

    def stage_from_external_path(self, source: Path, *, max_bytes: int) -> Path:
        """把用户选中的外部文件安全拷进 staging，返回可交给 :meth:`put` 的路径。

        :meth:`put` 只接受 staging 目录里的 0600 文件，而用户从「文稿」选的文件
        通常是 0644——所以这一步不是优化，是功能能否成立的前提。它保留了
        :meth:`_read_staging` 的全部边界（symlink、普通文件、大小、TOCTOU），
        唯独不要求 owner-only：外部文件本就不归 Lobster0 管。

        Args:
            source: 用户在系统 Dialog 中选定的绝对路径。
            max_bytes: 附件自己的上限，必须不超过 Store 的 ``max_bytes``。

        Returns:
            staging 目录内的 0600 文件路径。

        Raises:
            ArtifactError: 源是 symlink/非普通文件/超限/读取期间被替换。
            OSError: staging 文件无法写入时原样抛出。
        """
        if type(max_bytes) is not int or not 1 <= max_bytes <= self._max_bytes:
            raise ArtifactError("artifact_too_large", "attachment limit is invalid")
        data = self._read_external(Path(source), max_bytes)
        self._staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".upload-", dir=self._staging_root
        )
        staged = Path(temporary)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # 失败必须不留半个 staging 文件，否则下一次 put 会把它当成输入。
            staged.unlink(missing_ok=True)
            raise
        return staged

    def _read_external(self, source: Path, max_bytes: int) -> bytes:
        """读取用户选中的外部文件，边界与 staging 相同但不要求 owner-only。"""
        try:
            descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            raise ArtifactError("artifact_source_denied", "artifact source is denied") from None
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactError("artifact_source_denied", "artifact source is denied")
            if before.st_size > max_bytes:
                raise ArtifactError("artifact_too_large", "artifact is too large")
            chunks: list[bytes] = []
            total = 0
            # 按 max_bytes + 1 读而不信任 st_size：文件可能在 open 与 read 之间变大。
            while chunk := os.read(descriptor, min(65_536, max_bytes + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise ArtifactError("artifact_too_large", "artifact is too large")
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or after.st_size != total
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ArtifactError("artifact_source_changed", "artifact source changed")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def link(
        self,
        artifact_id: str,
        *,
        session_id: int,
        origin: str,
        message_id: int | None = None,
        filename: str | None = None,
    ) -> None:
        """把一条 Artifact 关联到某个会话。

        关联是多对多的：Artifact 是 content-addressed 且跨会话去重的，同一份文件
        在两个会话里出现是同一行 artifacts 记录，归属放不进那一行。

        Args:
            artifact_id: 必须是当前 Owner 名下未过期的 Artifact。
            session_id: 会话的内部 ID。
            origin: ``user_upload`` 或 ``agent_output``。
            message_id: 关联到的消息；为空表示尚未落到具体消息上。
            filename: 可选的显示文件名。它是**不可信的展示文本**，不参与任何安全
                判定；入库前会去掉控制字符并截断长度。

        Raises:
            ArtifactError: Artifact 不存在、已过期，或 origin 不在允许集合内。
        """
        if origin not in _LINK_ORIGINS:
            raise ArtifactError("artifact_source_denied", "artifact origin is denied")
        if type(session_id) is not int or session_id <= 0:
            raise ArtifactError("artifact_not_found", "artifact was not found")
        # 借 read_metadata 做一次完整存在性与归属校验，伪造 id 到不了写入这步。
        self.read_metadata(artifact_id)
        with self._database.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO artifact_links "
                "(owner_id, artifact_id, session_id, message_id, origin, filename, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._owner_id,
                    artifact_id,
                    session_id,
                    message_id,
                    origin,
                    display_filename(filename),
                    self._now().isoformat(),
                ),
            )

    def list_for_session(self, session_id: int, *, limit: int) -> list[ArtifactLink]:
        """按会话列出未过期的产物摘要，最新的在前。

        Raises:
            ArtifactError: ``limit`` 越界。列表必须有界，否则一次会把整个会话的
                产物读进内存。
        """
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST_LIMIT:
            raise ArtifactError("artifact_limit_invalid", "artifact limit is invalid")
        if type(session_id) is not int or session_id <= 0:
            return []
        now = self._now().isoformat()
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT l.artifact_id, l.origin, l.message_id, l.filename, l.created_at, "
                "a.media_type, a.byte_size, a.width, a.height "
                "FROM artifact_links AS l JOIN artifacts AS a "
                "ON a.artifact_id = l.artifact_id AND a.owner_id = l.owner_id "
                "WHERE l.owner_id = ? AND l.session_id = ? "
                "AND a.status = 'active' AND a.expires_at > ? "
                "ORDER BY l.id DESC LIMIT ?",
                (self._owner_id, session_id, now, limit),
            ).fetchall()
        return [
            ArtifactLink(
                artifact_id=row["artifact_id"],
                media_type=row["media_type"],
                byte_size=row["byte_size"],
                origin=row["origin"],
                message_id=row["message_id"],
                filename=row["filename"],
                created_at=datetime.fromisoformat(row["created_at"]),
                width=row["width"],
                height=row["height"],
            )
            for row in rows
        ]

    def read_metadata(self, artifact_id: str) -> Artifact:
        """读取 Owner 当前未过期 Artifact，并重新校验本地文件形状。"""
        if not isinstance(artifact_id, str) or not artifact_id.startswith("art_"):
            raise ArtifactError("artifact_not_found", "artifact was not found")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE owner_id = ? AND artifact_id = ? "
                "AND status = 'active'",
                (self._owner_id, artifact_id),
            ).fetchone()
        if row is None or datetime.fromisoformat(row["expires_at"]) <= self._now():
            raise ArtifactError("artifact_not_found", "artifact was not found")
        relative = Path(row["relative_path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ArtifactError("artifact_store_corrupt", "artifact store is corrupt")
        path = self._root / relative
        if not _matches_private_target(path, row["content_hash"], self._max_bytes):
            raise ArtifactError("artifact_store_corrupt", "artifact store is corrupt")
        return Artifact(
            artifact_id=row["artifact_id"],
            content_hash=row["content_hash"],
            media_type=row["media_type"],
            byte_size=row["byte_size"],
            source=row["source"],
            path=path,
            width=row["width"],
            height=row["height"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )

    def delete_expired(self) -> int:
        """删除当前 Owner 已到 TTL 的文件，并把 metadata 标为 deleted。"""
        now = self._now()
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id, relative_path FROM artifacts "
                "WHERE owner_id = ? AND status = 'active' AND expires_at <= ? "
                "ORDER BY artifact_id",
                (self._owner_id, now.isoformat()),
            ).fetchall()
            for row in rows:
                relative = Path(row["relative_path"])
                if not relative.is_absolute() and ".." not in relative.parts:
                    (self._root / relative).unlink(missing_ok=True)
                connection.execute(
                    "UPDATE artifacts SET status = 'deleted', deleted_at = ? "
                    "WHERE owner_id = ? AND artifact_id = ? AND status = 'active'",
                    (now.isoformat(), self._owner_id, row["artifact_id"]),
                )
        return len(rows)

    def _read_staging(self, path: Path) -> bytes:
        """用 O_NOFOLLOW 读取同一 inode 的有界 owner-only 普通文件。"""
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError:
            raise ArtifactError("artifact_source_denied", "artifact source is denied") from None
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_mode & 0o077
                or before.st_size > self._max_bytes
            ):
                code = (
                    "artifact_too_large"
                    if before.st_size > self._max_bytes
                    else "artifact_source_denied"
                )
                raise ArtifactError(code, "artifact source is denied")
            chunks: list[bytes] = []
            total = 0
            while chunk := os.read(descriptor, min(65_536, self._max_bytes + 1 - total)):
                chunks.append(chunk)
                total += len(chunk)
                if total > self._max_bytes:
                    raise ArtifactError("artifact_too_large", "artifact is too large")
            after = os.fstat(descriptor)
            if (
                before.st_dev != after.st_dev
                or before.st_ino != after.st_ino
                or after.st_size != total
                or before.st_mtime_ns != after.st_mtime_ns
            ):
                raise ArtifactError("artifact_source_changed", "artifact source changed")
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _now(self) -> datetime:
        """返回 aware UTC 时钟，拒绝测试或调用方注入 naive 时间。"""
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("artifact clock must return an aware datetime")
        return value.astimezone(UTC)


def display_filename(value: str | None) -> str | None:
    """把调用方给的文件名收敛成可安全展示的短文本。

    文件名不参与任何安全判定，但它会被渲染，也会进入发给模型的附件清单：
    控制字符可以伪造换行制造假清单条目，超长会撑坏界面。
    """
    if value is None:
        return None
    cleaned = "".join(character for character in value if character.isprintable())
    cleaned = cleaned.strip()
    return cleaned[:255] or None


def _private_directory(path: Path) -> Path:
    """校验已初始化、非 symlink 且 owner-only 的固定目录。"""
    candidate = Path(path)
    if (
        candidate.is_symlink()
        or not candidate.is_dir()
        or candidate.stat().st_mode & 0o077
    ):
        raise ValueError("artifact directory must be private")
    return candidate.resolve()


def _inspect_content(data: bytes, declared: str) -> tuple[str, int | None, int | None]:
    """根据 magic/UTF-8 校验内容，并返回实际 MIME 与可选 PNG 尺寸。"""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        if (
            width <= 0
            or height <= 0
            or width > _MAX_IMAGE_DIMENSION
            or height > _MAX_IMAGE_DIMENSION
            or width * height > _MAX_IMAGE_PIXELS
        ):
            raise ArtifactError("artifact_dimensions_denied", "image dimensions are denied")
        return "image/png", width, height
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", None, None
    if data.startswith(b"%PDF-"):
        return "application/pdf", None, None
    if data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return "application/zip", None, None
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ArtifactError("artifact_media_mismatch", "artifact media is invalid") from None
    if "\x00" in text:
        raise ArtifactError("artifact_media_mismatch", "artifact media is invalid")
    if declared == "application/json":
        try:
            json.loads(text)
        except json.JSONDecodeError:
            raise ArtifactError("artifact_media_mismatch", "artifact media is invalid") from None
        return "application/json", None, None
    if declared in {"text/plain", "text/csv"}:
        return declared, None, None
    raise ArtifactError("artifact_media_mismatch", "artifact media type does not match content")


def _write_private_atomic(path: Path, data: bytes) -> None:
    """在 content-addressed 目标同目录写入 0600 临时文件并原子替换。"""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise ArtifactError("artifact_store_corrupt", "artifact store is corrupt")
    descriptor, temporary = tempfile.mkstemp(prefix=".artifact-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _matches_private_target(path: Path, content_hash: str, max_bytes: int) -> bool:
    """确认 Store 目标是 0600 普通文件且内容仍匹配地址 hash。"""
    try:
        state = path.stat()
        if (
            path.is_symlink()
            or not path.is_file()
            or state.st_mode & 0o077
            or state.st_size > max_bytes
        ):
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == content_hash
    except OSError:
        return False
