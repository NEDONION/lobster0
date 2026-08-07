"""以 owner-only Markdown 保存长期和每日记忆。"""

import fcntl
import hashlib
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from miniclaw.paths import StatePaths

_MAX_MEMORY_BYTES = 64 * 1024
_SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S{8,}"),
    re.compile(
        r"(?i)(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|"
        r"secret|验证码|密码)\s*[:=]\s*\S{4,}"
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
)


class MemoryError(RuntimeError):
    """表示 Memory 输入、路径或文本违反稳定安全契约。"""

    def __init__(self, code: str, message: str) -> None:
        """保存机器错误码和不包含记忆正文的安全消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class MemoryDocument:
    """描述一个进入上下文的有界 Markdown 文档。"""

    scope: str
    content: str
    content_hash: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """汇总当前 Turn 可见的长期、今日与昨日记忆。"""

    documents: tuple[MemoryDocument, ...]
    text: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class MemoryWrite:
    """描述一次 daily memory 追加或去重结果。"""

    status: str
    day: str
    content_hash: str


class MemoryStore:
    """在固定状态路径内安全读取和追加 Markdown Memory。"""

    def __init__(
        self,
        paths: StatePaths,
        *,
        today: Callable[[], date] | None = None,
    ) -> None:
        """绑定状态路径和可测试日期来源。

        Args:
            paths: 已解析的 MiniClaw 固定状态路径。
            today: 返回当前 UTC 日期的可选函数。
        """
        self._paths = paths
        self._today = today or (lambda: datetime.now(UTC).date())

    def snapshot(self) -> MemorySnapshot:
        """读取长期、昨日和今日记忆，生成稳定文本与组合哈希。

        Returns:
            只包含存在文档的有界 MemorySnapshot。

        Raises:
            MemoryError: 固定路径不安全、文件不可读或不是 UTF-8。
        """
        today = self._today()
        documents = [self._read_document(self._paths.memory_file, "long_term", required=True)]
        for day in (today - timedelta(days=1), today):
            document = self._read_document(
                self._daily_path(day),
                day.isoformat(),
                required=False,
            )
            if document is not None:
                documents.append(document)
        selected = tuple(document for document in documents if document is not None)
        text = "\n\n".join(
            f"## MEMORY {document.scope}\n{document.content.strip()}"
            for document in selected
            if document.content.strip()
        )
        digest = hashlib.sha256()
        for document in selected:
            digest.update(document.scope.encode("utf-8"))
            digest.update(b"\0")
            digest.update(document.content_hash.encode("ascii"))
            digest.update(b"\0")
        return MemorySnapshot(selected, text, digest.hexdigest())

    def read(self, scope: str) -> str:
        """按封闭 scope 读取长期、今日或最近 daily 记忆。

        Args:
            scope: `long_term`、`today` 或 `recent`。

        Returns:
            有界 UTF-8 文本；不存在的 daily 文档返回空字符串。

        Raises:
            MemoryError: scope 非法或目标文件不安全、损坏。
        """
        today = self._today()
        if scope == "long_term":
            document = self._read_document(
                self._paths.memory_file,
                "long_term",
                required=True,
            )
            assert document is not None
            return document.content
        if scope == "today":
            document = self._read_document(
                self._daily_path(today),
                today.isoformat(),
                required=False,
            )
            return "" if document is None else document.content
        if scope == "recent":
            recent: list[str] = []
            for day in (today - timedelta(days=1), today):
                document = self._read_document(
                    self._daily_path(day),
                    day.isoformat(),
                    required=False,
                )
                if document is not None and document.content.strip():
                    recent.append(document.content)
            return "\n\n".join(recent)
        raise MemoryError("invalid_memory_scope", "memory scope is not supported")

    def append_daily(self, content: str, *, source: str, session_id: int) -> MemoryWrite:
        """规范化并追加一条已批准的 daily memory。

        Args:
            content: 不含凭据的事实文本。
            source: 简短、非敏感的来源说明。
            session_id: 当前不可由模型伪造的内部 Session ID。

        Returns:
            `recorded` 或 `duplicate` 状态及内容哈希。

        Raises:
            MemoryError: 输入敏感/无效、路径不安全或文件超过上限。
        """
        fact = _normalize_line(content, "memory content", maximum=2_000)
        origin = _normalize_line(source, "memory source", maximum=200)
        if _contains_sensitive(fact) or _contains_sensitive(origin):
            raise MemoryError(
                "sensitive_memory",
                "memory content looks sensitive and was not stored",
            )
        if type(session_id) is not int or session_id <= 0:
            raise MemoryError("invalid_memory_source", "memory session source is invalid")

        day = self._today()
        content_hash = hashlib.sha256(fact.encode("utf-8")).hexdigest()
        marker = f"content_sha256: {content_hash}"
        entry = (
            f"- fact: {fact}\n"
            f"  source_session: {session_id}\n"
            f"  source: {origin}\n"
            "  confidence: confirmed\n"
            f"  {marker}\n"
        )
        path = self._daily_path(day)
        self._assert_memory_parent(path)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise MemoryError("unsafe_memory_path", "daily memory path is not safe") from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise MemoryError(
                    "unsafe_memory_path",
                    "daily memory must be an owner-only regular file",
                )
            os.lseek(descriptor, 0, os.SEEK_SET)
            existing_bytes = os.read(descriptor, _MAX_MEMORY_BYTES + 1)
            if len(existing_bytes) > _MAX_MEMORY_BYTES:
                raise MemoryError("memory_full", "daily memory reached its size limit")
            existing = _decode_complete(existing_bytes)
            if marker in existing:
                return MemoryWrite("duplicate", day.isoformat(), content_hash)
            prefix = "" if existing else f"# Daily Memory: {day.isoformat()}\n\n"
            payload = (prefix + entry).encode("utf-8")
            if len(existing_bytes) + len(payload) > _MAX_MEMORY_BYTES:
                raise MemoryError("memory_full", "daily memory reached its size limit")
            os.write(descriptor, payload)
            os.fsync(descriptor)
        except OSError as error:
            raise MemoryError("memory_write_failed", "daily memory could not be stored") from error
        finally:
            os.close(descriptor)
        return MemoryWrite("recorded", day.isoformat(), content_hash)

    def _daily_path(self, day: date) -> Path:
        """从内部日期生成不可由模型控制的 daily 文件路径。"""
        return self._paths.memory_dir / f"{day.isoformat()}.md"

    def _read_document(
        self,
        path: Path,
        scope: str,
        *,
        required: bool,
    ) -> MemoryDocument | None:
        """读取一个固定 Memory 文件，并限制进入上下文的字节数。"""
        self._assert_memory_parent(path)
        if path.is_symlink():
            raise MemoryError("unsafe_memory_path", "memory path must not be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            if required:
                raise MemoryError("memory_not_found", "required memory file is missing") from None
            return None
        except OSError as error:
            raise MemoryError("unsafe_memory_path", "memory path is not safely readable") from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise MemoryError("unsafe_memory_path", "memory path is not a regular file")
            payload = os.read(descriptor, _MAX_MEMORY_BYTES + 4)
        except OSError as error:
            raise MemoryError("memory_read_failed", "memory file could not be read") from error
        finally:
            os.close(descriptor)
        truncated = metadata.st_size > _MAX_MEMORY_BYTES
        content = _decode_prefix(payload[:_MAX_MEMORY_BYTES], truncated=truncated)
        return MemoryDocument(
            scope=scope,
            content=content,
            content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            truncated=truncated,
        )

    def _assert_memory_parent(self, path: Path) -> None:
        """确认固定文件父目录未被替换成符号链接或根外路径。"""
        expected = (
            self._paths.home if path == self._paths.memory_file else self._paths.memory_dir
        )
        try:
            if expected.is_symlink() or expected.resolve(strict=True) != path.parent.resolve(
                strict=True
            ):
                raise MemoryError(
                    "unsafe_memory_path",
                    "memory parent directory is not safe",
                )
        except OSError as error:
            raise MemoryError(
                "unsafe_memory_path",
                "memory parent directory is not safe",
            ) from error


def _normalize_line(value: str, name: str, *, maximum: int) -> str:
    """把单条 Markdown 字段收窄为空白稳定的有限文本。"""
    if not isinstance(value, str):
        raise MemoryError("invalid_memory", f"{name} must be text")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise MemoryError("invalid_memory", f"{name} has an invalid length")
    return normalized


def _contains_sensitive(value: str) -> bool:
    """识别不允许持久化到 Memory 的常见凭据形态。"""
    return any(pattern.search(value) for pattern in _SENSITIVE_PATTERNS)


def _decode_complete(payload: bytes) -> str:
    """严格解码完整 daily 文件，不替换损坏字节。"""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MemoryError("invalid_memory_text", "memory file is not valid UTF-8") from error


def _decode_prefix(payload: bytes, *, truncated: bool) -> str:
    """严格解码有界前缀，仅允许截断发生在最后一个 UTF-8 字符内部。"""
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as error:
        if truncated and error.reason == "unexpected end of data":
            try:
                return payload[: error.start].decode("utf-8")
            except UnicodeDecodeError:
                pass
        raise MemoryError("invalid_memory_text", "memory file is not valid UTF-8") from error
