"""Live 验收使用的平台无关受管 Gateway 子进程。"""

import asyncio
import os
import re
import signal
import sys
from collections import deque
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.channels.sdk_logging import redact_sdk_text
from miniclaw.gateway_lease import GatewayProvenance

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
_UNSAFE_PARAMETER = re.compile(
    r"(?i)\b(?:access_key|ticket|token|device_id)\s*=\s*"
    r"(?!\*{3}(?:[&,;\s]|$)|<redacted>(?:[&,;\s]|$))[^\s&,;]+"
)
_UNSAFE_BEARER = re.compile(
    r"(?i)\bauthorization\s*[:=]\s*bearer\s+"
    r"(?!\*{3}(?:[,;\s]|$))[^\s,;]+"
)


class ManagedGatewayError(RuntimeError):
    """表示受管 Gateway 只能公开的稳定失败码。"""

    def __init__(self, code: str) -> None:
        """保存不含子进程输出、路径或平台标识的错误码。

        Args:
            code: 对 Runner 稳定的安全错误码。
        """
        self.code = code
        super().__init__(code)


class ManagedGateway:
    """持续排空输出、按精确 marker 就绪并有界退出的 Gateway。"""

    _DIAGNOSTIC_LINES = 200
    _DIAGNOSTIC_CHARS = 4096

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        *,
        ready_line: str,
        provenance: GatewayProvenance,
        secret_values: tuple[str, ...],
    ) -> None:
        """保存已启动进程及其固定 ready/provenance 契约。

        Args:
            process: 已连接 stdout/stderr pipes 的子进程。
            ready_line: 当前单平台配置应输出的精确就绪行。
            provenance: 父 Runner 在启动瞬间绑定的 PID、UTC 和 commit。
            secret_values: 只在内存用于全输出 exact 扫描的已知 Secret。
        """
        self._process = process
        self._ready_line = ready_line
        self.provenance = provenance
        self._secret_values = secret_values
        self._secret_match_count = 0
        self._ready_event = asyncio.Event()
        self._ready = False
        self._diagnostics: deque[str] = deque(maxlen=self._DIAGNOSTIC_LINES)
        assert process.stdout is not None
        assert process.stderr is not None
        self._drain_tasks = (
            asyncio.create_task(self._drain(process.stdout, "stdout")),
            asyncio.create_task(self._drain(process.stderr, "stderr")),
        )

    @classmethod
    async def start(
        cls,
        *,
        project_root: Path,
        home: Path,
        ready_line: str,
        commit: str,
        ready_timeout: float,
        secret_values: tuple[str, ...] = (),
        command: tuple[str, ...] | None = None,
    ) -> "ManagedGateway":
        """启动当前 Python 的 MiniClaw，并等待精确平台 ready marker。

        Args:
            project_root: 子进程使用的绝对 clean worktree。
            home: 传给 MiniClaw CLI 的绝对状态目录。
            ready_line: 由 typed config 生成的精确就绪行。
            commit: preflight 固定的 40 位 clean commit。
            ready_timeout: 等待 ready 的正秒数上界。
            secret_values: 只用于流式匿名计数、绝不进入 diagnostics 的 Secret。
            command: 测试专用 exact argv；省略时启动当前 MiniClaw module。

        Returns:
            已观察到精确 ready line 且仍在运行的受管进程。

        Raises:
            ManagedGatewayError: 输入非法、启动失败、提前退出或 ready 超时。
        """
        if (
            not project_root.is_absolute()
            or not home.is_absolute()
            or _COMMIT.fullmatch(commit) is None
            or not _safe_ready_line(ready_line)
            or not isinstance(ready_timeout, (int, float))
            or isinstance(ready_timeout, bool)
            or ready_timeout <= 0
            or any(
                not isinstance(value, str)
                or not 4 <= len(value.encode("utf-8")) <= 4096
                for value in secret_values
            )
        ):
            raise ManagedGatewayError("gateway_provenance_invalid")
        executable = command or (
            sys.executable,
            "-m",
            "miniclaw",
            "--home",
            str(home),
            "gateway",
        )
        environment = dict(os.environ)
        environment["MINICLAW_GATEWAY_COMMIT"] = commit
        try:
            process = await asyncio.create_subprocess_exec(
                *executable,
                cwd=project_root,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
        except (OSError, ValueError):
            raise ManagedGatewayError("gateway_start_failed") from None
        gateway = cls(
            process,
            ready_line=ready_line,
            provenance=GatewayProvenance(
                pid=process.pid,
                started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                commit=commit,
            ),
            secret_values=tuple(dict.fromkeys(secret_values)),
        )
        ready_wait = asyncio.create_task(gateway._ready_event.wait())
        exit_wait = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_wait, exit_wait),
                timeout=float(ready_timeout),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_wait in done and process.returncode is None:
                gateway._ready = True
                return gateway
            failure = (
                "gateway_exited_before_ready"
                if exit_wait in done
                else "gateway_ready_timeout"
            )
            await gateway._stop_after_failed_start()
            raise ManagedGatewayError(failure)
        finally:
            for waiter in (ready_wait, exit_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(ready_wait, exit_wait, return_exceptions=True)

    @property
    def ready(self) -> bool:
        """返回当前实例是否见过精确 ready marker。"""
        return self._ready

    @property
    def bounded_diagnostics(self) -> tuple[str, ...]:
        """返回最多 200 行、每行最多 4096 字符的内存诊断快照。"""
        return tuple(self._diagnostics)

    @property
    def secret_match_count(self) -> int:
        """返回所有已排空输出中的匿名 Secret 命中总数。"""
        return self._secret_match_count

    async def stop(self, *, timeout: float = 10.0) -> int:
        """最多发送两次 SIGTERM，并等待子进程和输出管道结束。

        Args:
            timeout: 每次优雅等待的正秒数。

        Returns:
            子进程最终退出码。

        Raises:
            ManagedGatewayError: 两次 SIGTERM 后仍未退出。
        """
        if self._process.returncode is None:
            self._send_sigterm()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
            except TimeoutError:
                self._send_sigterm()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=timeout)
                except TimeoutError:
                    raise ManagedGatewayError("gateway_shutdown_timeout") from None
        await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        if self._process.returncode is None:
            raise ManagedGatewayError("gateway_shutdown_timeout")
        return self._process.returncode

    async def _drain(self, stream: asyncio.StreamReader, source: str) -> None:
        """持续排空一个 pipe，并只保存有界单行诊断。

        Args:
            stream: stdout 或 stderr 的异步读取器。
            source: 固定的 stdout/stderr 标签。
        """
        while line_bytes := await stream.readline():
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if source == "stdout" and line == self._ready_line:
                self._ready_event.set()
            self._secret_match_count += sum(
                line.count(secret) for secret in self._secret_values
            )
            self._secret_match_count += len(_UNSAFE_PARAMETER.findall(line))
            self._secret_match_count += len(_UNSAFE_BEARER.findall(line))
            safe_line = redact_sdk_text(line)
            for secret in self._secret_values:
                safe_line = safe_line.replace(secret, "***")
            rendered = f"{source}:{safe_line}"[: self._DIAGNOSTIC_CHARS]
            self._diagnostics.append(rendered)

    def _send_sigterm(self) -> None:
        """优先终止整个子进程组，不支持时退回单进程 terminate。"""
        if self._process.returncode is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            try:
                self._process.terminate()
            except ProcessLookupError:
                return

    async def _stop_after_failed_start(self) -> None:
        """启动失败时尽力回收子进程，不用诊断覆盖稳定错误码。"""
        try:
            await self.stop(timeout=1.0)
        except ManagedGatewayError:
            return


def _safe_ready_line(value: str) -> bool:
    """判断 ready line 非空、有界且不含换行或控制字符。

    Args:
        value: typed config 派生的预期就绪行。

    Returns:
        仅在可以安全进行整行比较时为真。
    """
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and all(character >= " " and character != "\x7f" for character in value)
    )
