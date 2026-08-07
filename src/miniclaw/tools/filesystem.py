"""Workspace 内受限的文本文件读取 Tool。"""

import asyncio
import codecs
import stat
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

_MAX_PREFIX_BYTES = 512 * 1024


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
            prefix, boundary = await asyncio.to_thread(_read_prefix, resolved)
        except FileNotFoundError:
            return ToolResult.failure("not_found", "file was not found")
        except IsADirectoryError:
            return ToolResult.failure("not_a_file", "path is not a regular file")
        except OSError:
            return ToolResult.failure("file_read_failed", "file could not be read")

        try:
            content = _decode_utf8_prefix(prefix, boundary)
        except UnicodeDecodeError:
            return ToolResult.failure("binary_file", "file is not valid UTF-8 text")
        lines = content.splitlines(keepends=True)
        start = offset - 1
        window = lines[start : start + limit]
        truncated = bool(boundary) or start + len(window) < len(lines)
        root = next(
            root
            for root in (context.workspace, *context.read_only_roots)
            if resolved.is_relative_to(root.resolve(strict=False))
        )
        data: dict[str, JsonValue] = {
            "path": guard.display(context, resolved, root=root),
            "content": "".join(window),
            "offset": offset,
            "lines": len(window),
            "truncated": truncated,
        }
        if truncated and window:
            data["next_offset"] = offset + len(window)
        return ToolResult.success(data)


def _read_prefix(path: Path) -> tuple[bytes, bytes]:
    """读取普通文件的 512 KiB 前缀和最多三个边界字节。"""
    if not stat.S_ISREG(path.stat().st_mode):
        raise IsADirectoryError
    with path.open("rb") as file:
        return file.read(_MAX_PREFIX_BYTES), file.read(3)


def _decode_utf8_prefix(prefix: bytes, boundary: bytes) -> str:
    """严格解码前缀，并仅用边界字节验证被截断的 UTF-8 字符。"""
    if b"\0" in prefix:
        nul_index = prefix.index(b"\0")
        raise UnicodeDecodeError("utf-8", prefix, nul_index, nul_index + 1, "NUL byte")
    decoder = codecs.getincrementaldecoder("utf-8")()
    content = decoder.decode(prefix, final=not boundary)
    if not boundary:
        return content
    for byte in boundary:
        decoder.decode(bytes((byte,)), final=False)
        if not decoder.getstate()[0]:
            break
    else:
        decoder.decode(b"", final=True)
    return content
