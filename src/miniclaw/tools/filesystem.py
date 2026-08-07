"""Workspace 内受限的文本文件读取 Tool。"""

import asyncio
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

_MAX_READ_BYTES = 512 * 1024


class _LineTooLargeError(ValueError):
    """表示单行超过一次调用可安全返回的字节上限。"""


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
