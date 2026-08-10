"""保持现有 exact-argv 边界的 Host sandbox adapter。"""

import asyncio
import os
import signal
import time
from collections.abc import Callable

from lobster0.sandbox.base import ExecutionPlan, ExecutionReceipt, SandboxPlanError

_STREAM_LIMIT = 1024 * 1024
EnvironmentResolver = Callable[[str], str | None]


class HostSandbox:
    """使用应用 Policy 隔离执行命令；它不是恶意代码安全边界。"""

    def __init__(self, environment_resolver: EnvironmentResolver) -> None:
        """绑定只按 plan 名称返回受管值的环境 resolver。"""
        self._environment_resolver = environment_resolver

    async def execute(self, plan: ExecutionPlan) -> ExecutionReceipt:
        """不经 Shell 执行 exact argv，并完整清理超时或取消的进程组。"""
        if plan.backend != "host":
            raise SandboxPlanError("sandbox_backend_mismatch")
        environment: dict[str, str] = {}
        for name in plan.environment_names:
            value = self._environment_resolver(name)
            if value is None:
                raise SandboxPlanError("sandbox_environment_missing")
            environment[name] = value
        started = time.monotonic()
        try:
            process = await asyncio.create_subprocess_exec(
                *plan.argv,
                cwd=plan.cwd,
                env=environment,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise SandboxPlanError("command_failed") from error
        assert process.stdout is not None and process.stderr is not None
        wait_task = asyncio.create_task(process.wait())
        stdout_task = asyncio.create_task(_read_bounded(process.stdout))
        stderr_task = asyncio.create_task(_read_bounded(process.stderr))
        tasks = (wait_task, stdout_task, stderr_task)
        timed_out = False
        try:
            _, pending = await asyncio.wait(
                tasks,
                timeout=plan.timeout_seconds,
                return_when=asyncio.ALL_COMPLETED,
            )
            if pending:
                timed_out = True
                await _terminate_process_group(process)
                await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await _terminate_process_group(process)
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        stdout, stdout_truncated = stdout_task.result()
        stderr, stderr_truncated = stderr_task.result()
        return ExecutionReceipt(
            plan_hash=plan.sha256,
            backend="host",
            exit_code=None if timed_out else process.returncode,
            signal=None,
            timed_out=timed_out,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
            changed_paths=(),
        )


async def _read_bounded(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
    """并发排空进程流，但只保留最多 1 MiB。"""
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
    """先 TERM 后 KILL 整个独立进程组。"""
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
    """用 signal 0 探测进程组是否仍有成员。"""
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
