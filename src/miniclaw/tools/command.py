"""不经过 Shell、环境隔离且有界的 exact-argv 命令 Tool。"""

import asyncio
import os
import signal
import time
from pathlib import Path

from miniclaw.policy.command import (
    SAFE_EXECUTABLE_PATH,
    CommandPolicyError,
    normalize_command,
)
from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_STREAM_LIMIT = 1024 * 1024
_MAX_ARGS = 64
_MAX_ARGV_BYTES = 32 * 1024


class RunCommandTool:
    """在固定 Workspace 中执行一个解析后的程序和原样参数数组。"""

    definition = ToolDefinition(
        name="run_command",
        description=(
            "Run a single executable directly with exact arguments in the workspace. "
            "Never use a shell, pipeline, redirection, or inline code. Call this tool to "
            "request approval when needed instead of claiming it is unavailable. On macOS, "
            'launch an application with open -a: program "open", args ["-a", "Application"].'
        ),
        parameters={
            "type": "object",
            "properties": {
                "program": {"type": "string"},
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": _MAX_ARGS,
                },
                "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "required": ["program", "args"],
            "additionalProperties": False,
        },
        risk=ToolRisk.HIGH,
    )

    def __init__(self, *, timeout_seconds: int = 30, max_timeout_seconds: int = 120) -> None:
        if (
            type(timeout_seconds) is not int
            or type(max_timeout_seconds) is not int
            or timeout_seconds <= 0
            or max_timeout_seconds <= 0
            or max_timeout_seconds > 120
            or timeout_seconds > max_timeout_seconds
        ):
            raise ValueError("command timeouts must satisfy 0 < default <= maximum <= 120")
        self._timeout_seconds = timeout_seconds
        self._max_timeout_seconds = max_timeout_seconds

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验 program、字符串 argv 和不可放大的 timeout。"""
        if set(arguments) - {"program", "args", "timeout_seconds"}:
            raise ToolValidationError(
                "run_command only accepts program, args, and timeout_seconds"
            )
        if "program" not in arguments or "args" not in arguments:
            raise ToolValidationError("run_command requires program and args")
        program = arguments["program"]
        args = arguments["args"]
        timeout = arguments.get("timeout_seconds", self._timeout_seconds)
        if not isinstance(program, str) or not program:
            raise ToolValidationError("program must be a non-empty string")
        if (
            not isinstance(args, list)
            or len(args) > _MAX_ARGS
            or any(not isinstance(argument, str) for argument in args)
        ):
            raise ToolValidationError("args must be a list of at most 64 strings")
        argv = [program, *args]
        argv_bytes = sum(
            len(value.encode("utf-8", errors="surrogatepass")) for value in argv
        )
        if argv_bytes > _MAX_ARGV_BYTES:
            raise ToolValidationError("command argv must not exceed 32 KiB")
        if type(timeout) is not int or not 1 <= timeout <= self._max_timeout_seconds:
            raise ToolValidationError(
                f"timeout_seconds must be between 1 and {self._max_timeout_seconds}"
            )
        return {"program": program, "args": args, "timeout_seconds": timeout}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """以 shell=False、stdin EOF、最小环境和独立进程组执行命令。"""
        program = arguments["program"]
        args = arguments["args"]
        timeout = arguments["timeout_seconds"]
        assert isinstance(program, str)
        assert isinstance(args, list) and all(isinstance(argument, str) for argument in args)
        assert type(timeout) is int
        try:
            normalized = normalize_command(program, tuple(args), context.workspace)
        except CommandPolicyError as error:
            return ToolResult.failure(error.code, str(error))

        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                normalized.resolved_program,
                *normalized.args,
                cwd=context.workspace,
                env=_safe_environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return ToolResult.failure("command_failed", "command could not be started")
        assert process.stdout is not None and process.stderr is not None
        wait_task = asyncio.create_task(process.wait())
        stdout_task = asyncio.create_task(_read_bounded(process.stdout))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr))
        tasks = (wait_task, stdout_task, stderr_task)
        try:
            _, pending = await asyncio.wait(
                tasks,
                timeout=timeout,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                await _terminate_process_group(process)
                await asyncio.gather(*tasks)
                return ToolResult.failure("tool_timeout", "command exceeded its timeout")
        except asyncio.CancelledError:
            await _terminate_process_group(process)
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        stdout, stdout_truncated = stdout_task.result()
        stderr, stderr_truncated = stderr_task.result()
        data: dict[str, JsonValue] = {
            "program": Path(normalized.resolved_program).name,
            "args": list(normalized.args),
            "cwd": str(context.workspace),
            "exit_code": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
        }
        return ToolResult.success(data)


async def _read_bounded(
    stream: asyncio.StreamReader,
) -> tuple[bytes, bool]:
    """并发排空一个进程流，但最多保留 1 MiB。"""
    kept = bytearray()
    truncated = False
    while chunk := await stream.read(64 * 1024):
        available = _STREAM_LIMIT - len(kept)
        if available > 0:
            kept.extend(chunk[:available])
        if len(chunk) > available:
            truncated = True
    return bytes(kept), truncated


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """先 TERM 后 KILL 独立进程组，确保 timeout 不留下子进程。"""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        await process.wait()
        return
    for _ in range(20):
        await asyncio.sleep(0.1)
        if not _process_group_exists(process.pid):
            await process.wait()
            return
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    await process.wait()


def _process_group_exists(process_group_id: int) -> bool:
    """用 signal 0 探测独立进程组是否仍有成员。"""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _safe_environment() -> dict[str, str]:
    """构造不含 API Key、代理或用户环境的最小子进程环境。"""
    environment = {
        "PATH": SAFE_EXECUTABLE_PATH,
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    if os.name == "nt":
        for name in ("SYSTEMROOT", "WINDIR", "PATHEXT"):
            if name in os.environ:
                environment[name] = os.environ[name]
    return environment
