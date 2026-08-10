"""管理一个使用版本化 NDJSON 的 Browser Worker 子进程。"""

import asyncio
import json
import os
import re
import secrets
import signal
from contextlib import suppress
from typing import cast

from lobster0.providers.base import JsonValue

from .models import BrowserAction, BrowserProtocolError

PROTOCOL = "lobster0.browser.v1"
MAX_FRAME_BYTES = 1024 * 1024
_MAX_DIAGNOSTIC_BYTES = 4096
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ACTIONS = frozenset(
    {"open", "snapshot", "click", "type", "press", "scroll", "screenshot", "close"}
)


class BrowserClient:
    """启动、调用并有界清理单个 Browser Worker。"""

    def __init__(self, command: tuple[str, ...], *, timeout_seconds: float = 30) -> None:
        """绑定精确 Worker argv 和单次握手/请求超时。"""
        if not command or timeout_seconds <= 0:
            raise ValueError("Browser Worker command and timeout must be positive")
        self._command = command
        self._timeout_seconds = timeout_seconds
        self._process: asyncio.subprocess.Process | None = None
        self._request_lock = asyncio.Lock()
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail = bytearray()

    @property
    def running(self) -> bool:
        """返回 Worker 是否仍持有活动子进程。"""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """启动 Worker 并验证唯一支持的 ready 握手。"""
        if self.running:
            return
        process = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            limit=MAX_FRAME_BYTES + 1,
        )
        self._process = process
        assert process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        try:
            ready = await asyncio.wait_for(
                self._read_frame(process), timeout=self._timeout_seconds
            )
            if ready != {"protocol": PROTOCOL, "type": "ready"}:
                code = (
                    "unsupported_version"
                    if ready.get("protocol") != PROTOCOL
                    else "invalid_handshake"
                )
                raise BrowserProtocolError(code, "Browser Worker 握手不受支持")
        except TimeoutError:
            await self.close()
            raise BrowserProtocolError(
                "browser_timeout", "Browser Worker 握手超时"
            ) from None
        except BrowserProtocolError:
            await self.close()
            raise

    async def request(self, action: BrowserAction) -> dict[str, JsonValue]:
        """发送一个受限动作并返回同 ID 的标准 JSON result。"""
        self._validate_action(action)
        await self.start()
        async with self._request_lock:
            process = self._process
            if process is None or process.stdin is None:
                raise BrowserProtocolError("worker_closed", "Browser Worker 已关闭")
            request_id = secrets.token_hex(16)
            encoded = _encode(
                {
                    "protocol": PROTOCOL,
                    "id": request_id,
                    "session_id": action.session_id,
                    "action": action.kind,
                    "params": action.params,
                }
            )
            try:
                process.stdin.write(encoded)
                await asyncio.wait_for(process.stdin.drain(), timeout=self._timeout_seconds)
                response = await asyncio.wait_for(
                    self._read_frame(process), timeout=self._timeout_seconds
                )
                return _response_result(response, request_id)
            except asyncio.CancelledError:
                await self.close()
                raise
            except TimeoutError:
                await self.close()
                raise BrowserProtocolError(
                    "browser_timeout", "Browser Worker 请求超时"
                ) from None
            except BrowserProtocolError:
                await self.close()
                raise

    async def close(self) -> None:
        """关闭 stdin 并终止整个 Worker 进程组。"""
        process, self._process = self._process, None
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
                with suppress(BrokenPipeError, ConnectionResetError):
                    await process.stdin.wait_closed()
            if process.returncode is None:
                await _terminate_process_group(process)
        task, self._stderr_task = self._stderr_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _read_frame(
        self, process: asyncio.subprocess.Process
    ) -> dict[str, JsonValue]:
        """从 Worker stdout 读取并解码一个有界 JSON object。"""
        if process.stdout is None:
            raise BrowserProtocolError("worker_closed", "Browser Worker 没有 stdout")
        try:
            raw = await process.stdout.readline()
        except ValueError:
            raise BrowserProtocolError("frame_too_large", "Browser Worker 响应过大") from None
        if not raw:
            raise BrowserProtocolError("worker_closed", "Browser Worker 意外退出")
        if len(raw) > MAX_FRAME_BYTES:
            raise BrowserProtocolError("frame_too_large", "Browser Worker 响应过大")
        try:
            value = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise BrowserProtocolError("invalid_frame", "Browser Worker 响应无效") from None
        if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
            raise BrowserProtocolError("invalid_frame", "Browser Worker 响应无效")
        return cast(dict[str, JsonValue], value)

    async def _drain_stderr(self, reader: asyncio.StreamReader) -> None:
        """消费 Worker stderr，并只保留最后 4 KiB 诊断。"""
        while chunk := await reader.read(1024):
            self._stderr_tail.extend(chunk)
            del self._stderr_tail[:-_MAX_DIAGNOSTIC_BYTES]

    @staticmethod
    def _validate_action(action: BrowserAction) -> None:
        """拒绝未知动作、ID 和非标准 JSON 参数。"""
        if _IDENTIFIER.fullmatch(action.session_id) is None:
            raise BrowserProtocolError("invalid_session", "Browser Session ID 不合法")
        if action.kind not in _ACTIONS:
            raise BrowserProtocolError("unknown_action", "Browser 动作不受支持")
        try:
            json.dumps(action.params, allow_nan=False)
        except (TypeError, ValueError):
            raise BrowserProtocolError("invalid_params", "Browser 参数不是标准 JSON") from None


def _encode(value: dict[str, JsonValue]) -> bytes:
    """把一个请求编码成单行、紧凑且有界的 UTF-8 JSON。"""
    encoded = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    if len(encoded) > MAX_FRAME_BYTES:
        raise BrowserProtocolError("frame_too_large", "Browser Worker 请求过大")
    return encoded


def _response_result(
    response: dict[str, JsonValue], request_id: str
) -> dict[str, JsonValue]:
    """验证关联响应，并返回 result 或抛出稳定 Worker 错误。"""
    if response.get("protocol") != PROTOCOL:
        raise BrowserProtocolError("unsupported_version", "Browser Worker 协议不受支持")
    if response.get("id") != request_id or type(response.get("ok")) is not bool:
        raise BrowserProtocolError("invalid_response", "Browser Worker 响应无法关联")
    if response["ok"] is True:
        result = response.get("result")
        if set(response) != {"protocol", "id", "ok", "result"} or not isinstance(result, dict):
            raise BrowserProtocolError("invalid_response", "Browser Worker result 无效")
        return cast(dict[str, JsonValue], result)
    error = response.get("error")
    if set(response) != {"protocol", "id", "ok", "error"} or not isinstance(error, dict):
        raise BrowserProtocolError("invalid_response", "Browser Worker error 无效")
    code, message = error.get("code"), error.get("message")
    if (
        not isinstance(code, str)
        or _ERROR_CODE.fullmatch(code) is None
        or not isinstance(message, str)
        or not 1 <= len(message) <= 256
    ):
        raise BrowserProtocolError("invalid_response", "Browser Worker error 无效")
    raise BrowserProtocolError(code, message)


def _reject_json_constant(value: str) -> JsonValue:
    """拒绝 JSON 标准之外的 NaN 与 Infinity。"""
    raise ValueError(value)


async def _terminate_process_group(process: asyncio.subprocess.Process) -> None:
    """先 TERM 后 KILL 有界终止 Worker 进程组。"""
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
        return
    except TimeoutError:
        pass
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    await process.wait()
