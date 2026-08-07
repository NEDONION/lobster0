"""Workspace 内受限的 UTF-8 文本文件读写 Tool。"""

import asyncio
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from miniclaw.policy.workspace import WorkspaceAccessError, WorkspaceGuard
from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_MAX_READ_BYTES = 512 * 1024
_MAX_WRITE_BYTES = 256 * 1024
_MAX_EDIT_BYTES = 1024 * 1024


class _LineTooLargeError(ValueError):
    """表示单行超过一次调用可安全返回的字节上限。"""


@dataclass(frozen=True, slots=True)
class _FileToolError(ValueError):
    """把文件边界失败转换成稳定且脱敏的 Tool 错误。"""

    code: str
    message: str


class ReadFileTool:
    """按行读取配置 Workspace 或只读根内的 UTF-8 文本文件。"""

    definition = ToolDefinition(
        name="read_file",
        description="Read a UTF-8 text file inside the configured workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 1},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验路径及可选一开始行号、窗口行数，并补齐默认值。"""
        if set(arguments) - {"path", "offset", "limit"}:
            raise ToolValidationError("read_file only accepts path, offset, and limit")
        path = arguments.get("path")
        offset = arguments.get("offset", 1)
        limit = arguments.get("limit", 200)
        if not isinstance(path, str) or not path:
            raise ToolValidationError("path must be a non-empty string")
        if type(offset) is not int or offset < 1:
            raise ToolValidationError("offset must be a positive integer")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ToolValidationError("limit must be an integer between 1 and 1000")
        return {"path": path, "offset": offset, "limit": limit}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """读取受 Guard 约束的 UTF-8 文件，并返回请求的行窗口。"""
        path = arguments["path"]
        offset = arguments["offset"]
        limit = arguments["limit"]
        assert isinstance(path, str)
        assert type(offset) is int
        assert type(limit) is int
        guard = WorkspaceGuard()
        try:
            resolved = guard.resolve_read(context, path)
        except WorkspaceAccessError as error:
            return ToolResult.failure(error.code, str(error))
        try:
            content, lines, truncated = await asyncio.to_thread(
                _read_window,
                resolved,
                offset,
                limit,
            )
        except FileNotFoundError:
            return ToolResult.failure("not_found", "file was not found")
        except IsADirectoryError:
            return ToolResult.failure("not_a_file", "path is not a regular file")
        except _LineTooLargeError:
            return ToolResult.failure(
                "line_too_large",
                "line exceeds the 512 KiB read limit",
            )
        except UnicodeDecodeError:
            return ToolResult.failure("binary_file", "file is not valid UTF-8 text")
        except OSError:
            return ToolResult.failure("file_read_failed", "file could not be read")
        root = next(
            root
            for root in (context.workspace, *context.read_only_roots)
            if resolved.is_relative_to(root.resolve(strict=False))
        )
        data: dict[str, JsonValue] = {
            "path": guard.display(context, resolved, root=root),
            "content": content,
            "offset": offset,
            "lines": lines,
            "truncated": truncated,
        }
        if truncated and lines:
            data["next_offset"] = offset + lines
        return ToolResult.success(data)


class WriteFileTool:
    """在 Workspace 内原子创建或显式覆盖一个有限 UTF-8 文件。"""

    definition = ToolDefinition(
        name="write_file",
        description="Write a UTF-8 text file inside the configured workspace.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
        risk=ToolRisk.MEDIUM,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验路径、UTF-8 内容字节上限和显式 overwrite。"""
        if set(arguments) - {"path", "content", "overwrite"}:
            raise ToolValidationError("write_file only accepts path, content, and overwrite")
        path = arguments.get("path")
        content = arguments.get("content")
        overwrite = arguments.get("overwrite", False)
        if not isinstance(path, str) or not path:
            raise ToolValidationError("path must be a non-empty string")
        if not isinstance(content, str) or "\0" in content:
            raise ToolValidationError("content must be UTF-8 text without NUL bytes")
        try:
            content_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolValidationError("content must be valid UTF-8 text") from None
        if len(content_bytes) > _MAX_WRITE_BYTES:
            raise ToolValidationError("content must not exceed 256 KiB")
        if type(overwrite) is not bool:
            raise ToolValidationError("overwrite must be a boolean")
        return {"path": path, "content": content, "overwrite": overwrite}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """通过同目录临时文件原子写入，不隐式创建父目录。"""
        path = arguments["path"]
        content = arguments["content"]
        overwrite = arguments["overwrite"]
        assert isinstance(path, str)
        assert isinstance(content, str)
        assert type(overwrite) is bool
        try:
            data = await asyncio.to_thread(_write_text, context, path, content, overwrite)
        except WorkspaceAccessError as error:
            return ToolResult.failure(error.code, str(error))
        except _FileToolError as error:
            return ToolResult.failure(error.code, error.message)
        except OSError:
            return ToolResult.failure("write_failed", "file could not be written")
        return ToolResult.success(data)


class EditFileTool:
    """在 Workspace 内原子替换唯一出现的一段精确文本。"""

    definition = ToolDefinition(
        name="edit_file",
        description="Replace one exact text occurrence in a UTF-8 workspace file.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_text": {"type": "string"},
                "new_text": {"type": "string"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.MEDIUM,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验精确替换参数，不接受 regex、模糊 patch 或 NUL。"""
        if set(arguments) != {"path", "old_text", "new_text"}:
            raise ToolValidationError("edit_file requires path, old_text, and new_text")
        path = arguments.get("path")
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(path, str) or not path:
            raise ToolValidationError("path must be a non-empty string")
        if not isinstance(old_text, str) or not old_text or "\0" in old_text:
            raise ToolValidationError("old_text must be non-empty UTF-8 text without NUL bytes")
        if not isinstance(new_text, str) or "\0" in new_text:
            raise ToolValidationError("new_text must be UTF-8 text without NUL bytes")
        try:
            old_text.encode("utf-8")
            new_text.encode("utf-8")
        except UnicodeEncodeError:
            raise ToolValidationError("edit text must be valid UTF-8") from None
        return {"path": path, "old_text": old_text, "new_text": new_text}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """读取稳定普通文件并原子写回唯一精确替换。"""
        path = arguments["path"]
        old_text = arguments["old_text"]
        new_text = arguments["new_text"]
        assert isinstance(path, str)
        assert isinstance(old_text, str)
        assert isinstance(new_text, str)
        try:
            data = await asyncio.to_thread(_edit_text, context, path, old_text, new_text)
        except WorkspaceAccessError as error:
            return ToolResult.failure(error.code, str(error))
        except _FileToolError as error:
            return ToolResult.failure(error.code, error.message)
        except OSError:
            return ToolResult.failure("write_failed", "file could not be edited")
        return ToolResult.success(data)


def _read_window(path: Path, offset: int, limit: int) -> tuple[str, int, bool]:
    """流式跳过 offset 前行，并读取不超过 512 KiB 的完整 UTF-8 行。"""
    if not stat.S_ISREG(path.stat().st_mode):
        raise IsADirectoryError
    with path.open("rb") as file:
        for _ in range(offset - 1):
            line = file.readline(_MAX_READ_BYTES + 1)
            if not line:
                return "", 0, False
            if len(line) > _MAX_READ_BYTES:
                raise _LineTooLargeError
            _decode_line(line)

        content: list[str] = []
        content_bytes = 0
        for _ in range(limit):
            line = file.readline(_MAX_READ_BYTES + 1)
            if not line:
                return "".join(content), len(content), False
            if len(line) > _MAX_READ_BYTES:
                if content:
                    return "".join(content), len(content), True
                raise _LineTooLargeError
            if content_bytes + len(line) > _MAX_READ_BYTES:
                return "".join(content), len(content), True
            content.append(_decode_line(line))
            content_bytes += len(line)
        return "".join(content), len(content), bool(file.read(1))


def _decode_line(line: bytes) -> str:
    """严格解码一条完整行，并把 NUL 当作二进制内容拒绝。"""
    if b"\0" in line:
        nul_index = line.index(b"\0")
        raise UnicodeDecodeError("utf-8", line, nul_index, nul_index + 1, "NUL byte")
    return line.decode("utf-8")


def _write_text(
    context: ToolContext,
    raw_path: str,
    content: str,
    overwrite: bool,
) -> dict[str, JsonValue]:
    """完成 write_file 的同步预检、权限选择和原子写入。"""
    guard = WorkspaceGuard()
    target = guard.resolve_write(context, raw_path)
    existed = target.exists()
    if existed:
        target_stat = target.stat()
        if not stat.S_ISREG(target_stat.st_mode):
            raise _FileToolError("not_a_file", "path is not a regular file")
        if not overwrite:
            raise _FileToolError("file_exists", "file already exists")
        mode = stat.S_IMODE(target_stat.st_mode)
    else:
        mode = 0o600
    payload = content.encode("utf-8")
    _atomic_write(
        context,
        raw_path,
        target,
        payload,
        mode,
        replace=existed,
    )
    return {
        "path": guard.display(context, target),
        "bytes": len(payload),
        "overwritten": existed,
    }


def _edit_text(
    context: ToolContext,
    raw_path: str,
    old_text: str,
    new_text: str,
) -> dict[str, JsonValue]:
    """完成 edit_file 的稳定读取、唯一匹配和原子替换。"""
    guard = WorkspaceGuard()
    target = guard.resolve_write(context, raw_path)
    try:
        with target.open("rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise _FileToolError("not_a_file", "path is not a regular file")
            payload = source.read(_MAX_EDIT_BYTES + 1)
            after = os.fstat(source.fileno())
    except FileNotFoundError:
        raise _FileToolError("not_found", "file was not found") from None
    if _stat_identity(before) != _stat_identity(after):
        raise _FileToolError("file_changed", "file changed while it was read")
    if len(payload) > _MAX_EDIT_BYTES:
        raise _FileToolError("file_too_large", "file exceeds the 1 MiB edit limit")
    if b"\0" in payload:
        raise _FileToolError("binary_file", "file is not valid UTF-8 text")
    try:
        content = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _FileToolError("binary_file", "file is not valid UTF-8 text") from None

    first = content.find(old_text)
    if first < 0:
        raise _FileToolError("text_not_found", "old_text was not found")
    if content.find(old_text, first + 1) >= 0:
        raise _FileToolError("text_not_unique", "old_text occurs more than once")
    updated = f"{content[:first]}{new_text}{content[first + len(old_text):]}".encode()
    if len(updated) > _MAX_EDIT_BYTES:
        raise _FileToolError("file_too_large", "edited file exceeds the 1 MiB limit")
    _atomic_write(
        context,
        raw_path,
        target,
        updated,
        stat.S_IMODE(before.st_mode),
        replace=True,
        expected=_stat_identity(before),
    )
    return {"path": guard.display(context, target), "bytes": len(updated)}


def _atomic_write(
    context: ToolContext,
    raw_path: str,
    target: Path,
    payload: bytes,
    mode: int,
    *,
    replace: bool,
    expected: tuple[int, int, int, int] | None = None,
) -> None:
    """同目录落盘后原子发布；新建使用 link 保证不覆盖竞态文件。"""
    descriptor, temporary_name = tempfile.mkstemp(prefix=".miniclaw-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            os.fchmod(output.fileno(), mode)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        verified = WorkspaceGuard().resolve_write(context, raw_path)
        if verified != target:
            raise _FileToolError("file_changed", "target path changed before write")
        if expected is not None:
            try:
                current = target.stat(follow_symlinks=False)
            except FileNotFoundError:
                raise _FileToolError("file_changed", "file changed before write") from None
            if _stat_identity(current) != expected:
                raise _FileToolError("file_changed", "file changed before write")
        if replace:
            os.replace(temporary, target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                raise _FileToolError("file_exists", "file already exists") from None
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    """返回编辑前后用于检测普通文件变化的稳定身份字段。"""
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
