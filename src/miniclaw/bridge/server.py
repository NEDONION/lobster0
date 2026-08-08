"""把 protocol v1 请求编排到唯一 Python Agent Runtime。"""

import asyncio
from collections.abc import Coroutine
from typing import Protocol

from miniclaw import __version__
from miniclaw.agent.events import RunEvent
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.providers.base import JsonValue
from miniclaw.runtime import AgentRuntime

from .protocol import BridgeFrame, BridgeRequest, ProtocolError, decode_request, encode_frame


class AsyncLineReader(Protocol):
    """描述 Bridge 所需的最小异步行读取能力。"""

    async def readline(self) -> bytes:
        """返回下一条完整字节行；EOF 返回空字节。"""
        ...


class AsyncFrameWriter(Protocol):
    """描述 Bridge 所需的原子异步帧写入能力。"""

    async def write(self, data: bytes) -> None:
        """写入一条已经编码的完整 NDJSON 帧。"""
        ...


class BridgeServer:
    """串行接收 UI 请求，并允许一个前台 Turn 在后台发布事件。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        reader: AsyncLineReader,
        writer: AsyncFrameWriter,
    ) -> None:
        """绑定 Runtime 与专用协议输入输出。"""
        self._runtime = runtime
        self._reader = reader
        self._writer = writer
        self._write_lock = asyncio.Lock()
        self._active_task: asyncio.Task[None] | None = None
        self._pending_approval_id: int | None = None
        self._session_key = "default"

    async def run(self) -> int:
        """处理请求直到 stdin EOF 或 `bridge.shutdown`。

        Returns:
            正常 EOF 或显式关闭均返回退出码 0。
        """
        try:
            while True:
                raw = await self._reader.readline()
                if not raw:
                    break
                try:
                    request = decode_request(raw)
                except ProtocolError as error:
                    await self._error(None, error.code, str(error), retryable=False)
                    continue
                if not await self._handle(request):
                    return 0
        finally:
            await self._cancel_active()
        return 0

    async def _handle(self, request: BridgeRequest) -> bool:
        """执行一条已验证请求；返回 False 表示关闭 Bridge。"""
        await self._reap_active()
        if request.type == "client.hello":
            await self._ok(
                request.request_id,
                {
                    "protocol": 1,
                    "core_version": __version__,
                    "model": self._runtime.model,
                    "workspace": getattr(
                        self._runtime.workspace,
                        "name",
                        str(self._runtime.workspace),
                    ),
                    "language": self._runtime.ui_language,
                    "context_budget_tokens": self._runtime.context_budget_tokens,
                    "permission_mode": self._runtime.permission_state.mode.value,
                    "tools": [definition.name for definition in self._runtime.tool_definitions],
                    "capabilities": [
                        "streaming",
                        "reasoning",
                        "tools",
                        "approvals",
                        "telemetry",
                    ],
                },
            )
            return True
        if request.type == "turn.start":
            if self._active_task is not None:
                await self._error(
                    request.request_id,
                    "turn_busy",
                    "已有任务正在运行",
                    retryable=True,
                )
                return True
            if self._pending_approval_id is not None:
                await self._error(
                    request.request_id,
                    "approval_pending",
                    "请先处理当前审批",
                    retryable=True,
                )
                return True
            await self._ok(request.request_id, {})
            text = request.payload["text"]
            session_key = request.payload["session_key"]
            assert isinstance(text, str) and isinstance(session_key, str)
            self._session_key = session_key
            self._start_active(
                self._runtime.service.handle(
                    self._runtime.owner_id,
                    text,
                    session_key,
                    on_event=self._on_event,
                )
            )
            return True
        if request.type == "turn.cancel":
            await self._cancel_active()
            await self._ok(request.request_id, {})
            return True
        if request.type == "approval.resolve":
            await self._resolve_approval(request)
            return True
        if request.type == "permissions.set":
            await self._set_permission_mode(request)
            return True
        if request.type == "session.new":
            if self._active_task is not None or self._pending_approval_id is not None:
                await self._error(
                    request.request_id,
                    "session_busy",
                    "运行或审批期间不能切换 Session",
                    retryable=True,
                )
                return True
            session_key = request.payload["session_key"]
            assert isinstance(session_key, str)
            self._session_key = session_key
            await self._ok(request.request_id, {"session_key": session_key})
            return True
        await self._cancel_active()
        await self._ok(request.request_id, {})
        return False

    async def _set_permission_mode(self, request: BridgeRequest) -> None:
        """仅在无运行 Turn/待审批时切换共享权限状态。"""
        if self._active_task is not None or self._pending_approval_id is not None:
            await self._error(
                request.request_id,
                "permissions_busy",
                "运行或审批期间不能切换权限模式",
                retryable=True,
            )
            return
        mode = request.payload["mode"]
        assert isinstance(mode, str)
        try:
            selected = self._runtime.permission_state.set_mode(
                mode,
                user_id=self._runtime.owner_id,
                source="cli",
            )
        except Exception:  # noqa: BLE001 - 审计/状态异常只能暴露稳定 Bridge 错误
            await self._error(
                request.request_id,
                "permissions_change_failed",
                "权限模式切换失败",
                retryable=False,
            )
            return
        await self._ok(request.request_id, {"permission_mode": selected.value})

    async def _resolve_approval(self, request: BridgeRequest) -> None:
        """校验 pending id 后启动同一 Core 的审批续跑。"""
        await self._wait_active()
        approval_id = request.payload["approval_id"]
        decision_value = request.payload["decision"]
        assert isinstance(approval_id, int) and isinstance(decision_value, str)
        if approval_id != self._pending_approval_id:
            await self._error(
                request.request_id,
                "approval_not_pending",
                "审批不存在或已经处理",
                retryable=False,
            )
            return
        decision = ApprovalDecision(decision_value)
        self._pending_approval_id = None
        await self._ok(request.request_id, {})
        self._start_active(
            self._runtime.service.continue_approval(
                self._runtime.owner_id,
                approval_id,
                decision=decision,
                on_event=self._on_event,
            )
        )

    def _start_active(self, operation: Coroutine[object, object, object]) -> None:
        """把一个 Core operation 包装为会隔离异常的前台任务。"""
        self._active_task = asyncio.create_task(self._run_operation(operation))

    async def _run_operation(self, operation: Coroutine[object, object, object]) -> None:
        """运行 Core operation，并把未知失败收窄为安全 Bridge 事件。"""
        try:
            await operation
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._send(
                BridgeFrame(
                    type="event.bridge_error",
                    payload={
                        "code": "core_operation_failed",
                        "message": "MiniClaw Core 运行失败",
                        "retryable": False,
                    },
                )
            )

    async def _on_event(self, event: RunEvent) -> None:
        """把进程内 RunEvent 转换为一条同序协议事件。"""
        payload: dict[str, JsonValue] = {"turn_id": event.turn_id, **event.data}
        if event.kind == "approval_required":
            approval_id = event.data.get("approval_id")
            if isinstance(approval_id, int) and not isinstance(approval_id, bool):
                self._pending_approval_id = approval_id
        await self._send(BridgeFrame(type=f"event.{event.kind}", payload=payload))

    async def _reap_active(self) -> None:
        """清理已经完成的前台 Task 引用。"""
        if self._active_task is not None and self._active_task.done():
            await self._wait_active()

    async def _wait_active(self) -> None:
        """等待当前 Task 并吞掉已经转成安全事件的终态。"""
        task = self._active_task
        if task is None:
            return
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            if self._active_task is task:
                self._active_task = None

    async def _cancel_active(self) -> None:
        """取消并回收当前 Core operation；空闲时幂等。"""
        task = self._active_task
        if task is not None and not task.done():
            task.cancel()
        await self._wait_active()

    async def _ok(self, request_id: str, payload: dict[str, JsonValue]) -> None:
        """写出一条与请求绑定的成功响应。"""
        await self._send(
            BridgeFrame(type="response.ok", payload=payload, request_id=request_id)
        )

    async def _error(
        self,
        request_id: str | None,
        code: str,
        message: str,
        *,
        retryable: bool,
    ) -> None:
        """写出一条不含底层异常正文的稳定错误响应。"""
        await self._send(
            BridgeFrame(
                type="response.error",
                request_id=request_id,
                payload={"code": code, "message": message, "retryable": retryable},
            )
        )

    async def _send(self, frame: BridgeFrame) -> None:
        """在单锁内原子编码并写出一条协议帧。"""
        encoded = encode_frame(frame)
        async with self._write_lock:
            await self._writer.write(encoded)
