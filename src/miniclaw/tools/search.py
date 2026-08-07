"""Workspace 内受限的文件名与文本搜索 Tool。"""

import os
import re
import stat
from bisect import insort
from collections.abc import Iterator
from heapq import nsmallest
from pathlib import Path, PurePath

from miniclaw.policy.workspace import WorkspaceAccessError, WorkspaceGuard
from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_MAX_GREP_FILES = 200
_MAX_GREP_FILE_BYTES = 1024 * 1024
_MAX_GREP_TOTAL_BYTES = 20 * 1024 * 1024
_MAX_GREP_TEXT = 500


class GlobTool:
    """在受 Guard 约束的 root 内匹配普通文件相对路径。"""

    definition = ToolDefinition(
        name="glob",
        description="Find files matching a glob inside the configured workspace.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "root": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验相对 glob、搜索 root 和最多 200 条的结果上限。"""
        if set(arguments) - {"pattern", "root", "limit"}:
            raise ToolValidationError("glob only accepts pattern, root, and limit")
        pattern = arguments.get("pattern")
        root = arguments.get("root", ".")
        limit = arguments.get("limit", 200)
        if (
            not isinstance(pattern, str)
            or not pattern
            or PurePath(pattern).is_absolute()
        ):
            raise ToolValidationError("pattern must be a non-empty relative glob")
        if not isinstance(root, str) or not root:
            raise ToolValidationError("root must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ToolValidationError("limit must be an integer between 1 and 200")
        return {"pattern": pattern, "root": root, "limit": limit}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """返回排序后的安全相对路径，并在超过上限时标记截断。"""
        pattern = arguments["pattern"]
        raw_root = arguments["root"]
        limit = arguments["limit"]
        assert isinstance(pattern, str)
        assert isinstance(raw_root, str)
        assert type(limit) is int
        guard = WorkspaceGuard()
        try:
            root = guard.resolve_read(context, raw_root)
        except WorkspaceAccessError as error:
            return ToolResult.failure(error.code, str(error))

        matches = nsmallest(
            limit + 1,
            (display for display, _, _ in _candidates(context, guard, root, pattern)),
        )
        return ToolResult.success(
            {"matches": matches[:limit], "truncated": len(matches) > limit}
        )


class GrepTool:
    """在受 Guard 约束的 UTF-8 普通文件内执行有限正则搜索。"""

    definition = ToolDefinition(
        name="grep",
        description="Search UTF-8 files with a regex inside the configured workspace.",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string"},
                "root": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验正则、相对文件 glob、搜索 root 和最多 100 条结果。"""
        if set(arguments) - {"pattern", "glob", "root", "limit"}:
            raise ToolValidationError("grep only accepts pattern, glob, root, and limit")
        pattern = arguments.get("pattern")
        file_glob = arguments.get("glob", "**/*")
        root = arguments.get("root", ".")
        limit = arguments.get("limit", 100)
        if not isinstance(pattern, str) or not pattern:
            raise ToolValidationError("pattern must be a non-empty string")
        if (
            not isinstance(file_glob, str)
            or not file_glob
            or PurePath(file_glob).is_absolute()
        ):
            raise ToolValidationError("glob must be a non-empty relative glob")
        if not isinstance(root, str) or not root:
            raise ToolValidationError("root must be a non-empty string")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolValidationError("limit must be an integer between 1 and 100")
        return {"pattern": pattern, "glob": file_glob, "root": root, "limit": limit}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """返回有限的逐行正则匹配，并安全跳过不可读或非文本文件。"""
        pattern = arguments["pattern"]
        file_glob = arguments["glob"]
        raw_root = arguments["root"]
        limit = arguments["limit"]
        assert isinstance(pattern, str)
        assert isinstance(file_glob, str)
        assert isinstance(raw_root, str)
        assert type(limit) is int
        try:
            regex = re.compile(pattern)
        except re.error:
            return ToolResult.failure(
                "invalid_pattern",
                "pattern is not a valid regular expression",
            )

        guard = WorkspaceGuard()
        try:
            root = guard.resolve_read(context, raw_root)
        except WorkspaceAccessError as error:
            return ToolResult.failure(error.code, str(error))

        matches: list[dict[str, JsonValue]] = []
        files_scanned = 0
        bytes_read = 0
        for display, path, is_file in _candidates(context, guard, root, file_glob):
            if not is_file:
                continue
            if files_scanned >= _MAX_GREP_FILES or bytes_read >= _MAX_GREP_TOTAL_BYTES:
                return _grep_result(matches, limit, truncated=True)
            text, consumed, budget_exhausted, was_read = _read_search_text(
                path,
                _MAX_GREP_TOTAL_BYTES - bytes_read,
            )
            if was_read:
                files_scanned += 1
            bytes_read += consumed
            if budget_exhausted:
                return _grep_result(matches, limit, truncated=True)
            if text is None:
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line) is None:
                    continue
                insort(
                    matches,
                    {"path": display, "line": line_number, "text": line[:_MAX_GREP_TEXT]},
                    key=_grep_sort_key,
                )
                if len(matches) > limit + 1:
                    matches.pop()
        return _grep_result(matches, limit, truncated=len(matches) > limit)


def _candidates(
    context: ToolContext,
    guard: WorkspaceGuard,
    root: Path,
    pattern: str,
) -> Iterator[tuple[str, Path, bool]]:
    """稳定遍历匹配的安全文件与目录，并标记普通文件。"""
    for directory, directory_names, file_names in os.walk(
        root,
        followlinks=False,
        onerror=lambda _error: None,
    ):
        current = Path(directory)
        safe_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current / name
            if _unsafe_directory(candidate):
                continue
            safe = _safe_candidate(context, guard, root, candidate)
            if safe is None:
                continue
            display, resolved, mode = safe
            if not stat.S_ISDIR(mode):
                continue
            safe_directories.append(name)
            if _glob_matches(display, pattern):
                yield display, resolved, False
        directory_names[:] = safe_directories
        for name in sorted(file_names):
            candidate = current / name
            safe = _safe_candidate(context, guard, root, candidate)
            if safe is None:
                continue
            display, resolved, mode = safe
            if not stat.S_ISREG(mode) or not _glob_matches(display, pattern):
                continue
            yield display, resolved, True


def _safe_candidate(
    context: ToolContext,
    guard: WorkspaceGuard,
    root: Path,
    candidate: Path,
) -> tuple[str, Path, int] | None:
    """通过 Guard 解析候选并返回 root 相对展示路径、规范路径和文件模式。"""
    try:
        resolved = guard.resolve_read(context, str(candidate))
        mode = resolved.stat().st_mode
        display = guard.display(context, resolved, root=root)
    except (OSError, ValueError, WorkspaceAccessError):
        return None
    return display, resolved, mode


def _unsafe_directory(path: Path) -> bool:
    """把无法安全判断或属于 symlink 的目录从遍历队列移除。"""
    try:
        return path.is_symlink()
    except OSError:
        return True


def _glob_matches(path: str, pattern: str) -> bool:
    """匹配相对路径，并让递归前缀同时覆盖 root 顶层文件。"""
    candidate = PurePath(path)
    while True:
        if candidate.match(pattern):
            return True
        if not pattern.startswith("**/"):
            return False
        pattern = pattern[3:]


def _read_search_text(path: Path, remaining: int) -> tuple[str | None, int, bool, bool]:
    """有界读取 UTF-8 文本，并返回读取量、总量阻断和实际读取标记。"""
    try:
        with path.open("rb") as file:
            before = os.fstat(file.fileno())
            if before.st_size > _MAX_GREP_FILE_BYTES:
                return None, 0, False, False
            if before.st_size > remaining:
                return None, 0, True, False
            content = file.read(min(before.st_size + 1, remaining))
            consumed = len(content)
            try:
                after = os.fstat(file.fileno())
            except OSError:
                return None, consumed, False, True
    except OSError:
        return None, 0, False, False
    if (
        consumed != before.st_size
        or consumed > _MAX_GREP_FILE_BYTES
        or after.st_size > _MAX_GREP_FILE_BYTES
        or _stable_file_metadata(before) != _stable_file_metadata(after)
        or b"\0" in content
    ):
        return None, consumed, False, True
    try:
        return content.decode("utf-8"), consumed, False, True
    except UnicodeDecodeError:
        return None, consumed, False, True


def _stable_file_metadata(value: os.stat_result) -> tuple[int, ...]:
    """返回读取前后必须保持不变且排除 atime 的 fd metadata。"""
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _grep_sort_key(item: dict[str, JsonValue]) -> tuple[str, int]:
    """返回 grep 结果的稳定路径与行号排序键。"""
    return str(item["path"]), int(item["line"])


def _grep_result(
    matches: list[dict[str, JsonValue]],
    limit: int,
    *,
    truncated: bool,
) -> ToolResult:
    """按路径和行号稳定排序 grep 结果并设置截断标记。"""
    matches.sort(key=_grep_sort_key)
    return ToolResult.success({"matches": matches[:limit], "truncated": truncated})
