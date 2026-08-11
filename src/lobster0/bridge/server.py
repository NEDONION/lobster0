"""把 protocol v1 请求编排到唯一 Python Agent Runtime。"""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Protocol
from uuid import uuid4

from lobster0 import __version__
from lobster0.agent.events import RunEvent
from lobster0.automation.models import DeliveryTarget, TaskBudget
from lobster0.automation.parser import ScheduleError, parse_schedule
from lobster0.automation.repository import (
    AutomationDataError,
    AutomationStateError,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.memory.store import MemoryError
from lobster0.policy.approvals import ApprovalDecision
from lobster0.providers.base import JsonValue
from lobster0.runtime import AgentRuntime

from .conversations import ConversationQueryError
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
        runtime_start = getattr(self._runtime, "astart", None)
        if callable(runtime_start):
            await runtime_start()
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
                        "sessions",
                        "history",
                        "automation_read",
                "automation_write",
                    ],
                    "automation_enabled": self._runtime.automation_enabled,
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
        if request.type == "session.list":
            limit = request.payload["limit"]
            assert isinstance(limit, int) and not isinstance(limit, bool)
            result = self._runtime.conversation_console.list_sessions(
                self._runtime.owner_id,
                limit=limit,
            )
            await self._ok(request.request_id, result)
            return True
        if request.type == "session.history":
            limit = request.payload["limit"]
            session_key = request.payload["session_key"]
            assert isinstance(limit, int) and not isinstance(limit, bool)
            assert isinstance(session_key, str)
            try:
                result = self._runtime.conversation_console.history(
                    self._runtime.owner_id,
                    session_key=session_key,
                    limit=limit,
                )
            except ConversationQueryError as error:
                await self._error(
                    request.request_id,
                    error.code,
                    str(error),
                    retryable=False,
                )
                return True
            await self._ok(request.request_id, result)
            return True
        if request.type == "automation.list":
            limit = request.payload["limit"]
            assert isinstance(limit, int) and not isinstance(limit, bool)
            await self._ok(request.request_id, self._list_automations(limit))
            return True
        if request.type.startswith("automation.") and request.type != "automation.list":
            return await self._handle_automation_write(request)
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
        if request.type == "memory.command":
            if self._active_task is not None or self._pending_approval_id is not None:
                await self._error(
                    request.request_id,
                    "memory_busy",
                    "运行或审批期间不能执行 Memory 命令",
                    retryable=True,
                )
                return True
            try:
                result = self._runtime.memory_console.command(**request.payload)
            except MemoryError as error:
                await self._error(
                    request.request_id,
                    error.code,
                    str(error),
                    retryable=False,
                )
                return True
            await self._ok(request.request_id, result)
            return True
        await self._cancel_active()
        await self._ok(request.request_id, {})
        return False

    async def _handle_automation_write(self, request: BridgeRequest) -> bool:
        """处理 Automation 的写操作，全部复用 Core 既有 repository 语义。

        与 ``turn.start`` 一样先做忙碌判定：有回合在跑或有待审批时拒绝，避免与
        正在执行的任务互相干扰。真正的状态变更、乐观锁与审计都在 repository 内完成，
        这里只做路由与错误码映射。
        """
        if self._active_task is not None or self._pending_approval_id is not None:
            await self._error(
                request.request_id,
                "turn_busy",
                "当前有任务正在运行，请先结束后再操作自动化",
                retryable=True,
            )
            return True
        try:
            payload = self._run_automation_write(request)
        except (AutomationStateError, AutomationDataError, ScheduleError, ValueError) as error:
            await self._error(
                request.request_id,
                _automation_error_code(error),
                "自动化操作未完成",
                retryable=False,
            )
            return True
        await self._ok(request.request_id, payload)
        return True

    def _run_automation_write(self, request: BridgeRequest) -> dict[str, JsonValue]:
        """按类型执行一次 Automation 写操作并返回可安全展示的结果。"""
        owner_id = self._runtime.owner_id
        database = self._runtime.database
        tasks = ScheduledTaskRepository(database)

        if request.type == "automation.halt":
            reason = request.payload["reason"]
            assert isinstance(reason, str)
            state = self._runtime.automation_control.halt(reason)
            return {"halted": True, "revision": state.revision}
        if request.type == "automation.unhalt":
            state = self._runtime.automation_control.unhalt()
            return {"halted": False, "revision": state.revision}
        if request.type == "automation.create":
            return {"task": _task_summary(self._create_automation(tasks, request))}

        task_id = request.payload["task_id"]
        assert isinstance(task_id, int) and not isinstance(task_id, bool)
        if request.type == "automation.runs":
            limit = request.payload["limit"]
            assert isinstance(limit, int) and not isinstance(limit, bool)
            task = tasks.get(task_id, owner_id=owner_id)
            runs = TaskRunRepository(database).list(task_id=task.id, limit=limit)
            return {"runs": [_run_summary(run) for run in runs]}

        # pause/resume/cancel/run 都要先读当前 version，交给 repository 做乐观锁。
        task = tasks.get(task_id, owner_id=owner_id)
        if request.type == "automation.run":
            run = TaskRunRepository(database).enqueue(
                task,
                scheduled_for=datetime.now(UTC),
                idempotency_key=f"desktop:{uuid4().hex}",
            )
            return {"run": _run_summary(run)}
        # 按需取方法：写成字典字面量会求值全部分支，让一次 pause 也要求
        # repository 具备 resume/cancel，平白扩大依赖面。
        method_names = {
            "automation.pause": "pause",
            "automation.resume": "resume",
            "automation.cancel": "cancel",
        }
        action = getattr(tasks, method_names[request.type])
        updated = action(task.id, owner_id=owner_id, expected_version=task.version)
        return {"task": _task_summary(updated)}

    def _create_automation(
        self,
        tasks: ScheduledTaskRepository,
        request: BridgeRequest,
    ) -> object:
        """用收窄字段创建定时任务，其余参数取 Core 默认值。

        ``skills``/``delivery``/``budget`` 不从桌面端接收（见 D2a 设计 §6.2），
        这里显式传入默认值，让"未开放"这件事在代码里可见，而不是隐式依赖签名默认。
        """
        payload = request.payload
        schedule = payload["schedule"]
        assert isinstance(schedule, dict)
        spec = parse_schedule(
            {key: value for key, value in schedule.items() if value is not None},
            now=datetime.now(UTC),
            misfire_grace_seconds=0,
        )
        name = payload["name"]
        prompt = payload["prompt"]
        assert isinstance(name, str) and isinstance(prompt, str)
        return tasks.create(
            owner_id=self._runtime.owner_id,
            name=name,
            schedule=spec,
            prompt=prompt,
            skill_names=(),
            delivery=DeliveryTarget(route="none", channel="none"),
            policy_profile="automation-default",
            budget=TaskBudget(),
        )

    def _list_automations(self, limit: int) -> dict[str, JsonValue]:
        """返回当前 Owner 的有限只读 Automation 摘要。"""
        tasks = ScheduledTaskRepository(self._runtime.database).list(
            owner_id=self._runtime.owner_id,
            limit=limit,
        )
        return {
            "enabled": self._runtime.automation_enabled,
            "tasks": [_task_summary(task) for task in tasks],
        }

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
                        "message": "Lobster0 Core 运行失败",
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


def _task_summary(task: object) -> dict[str, JsonValue]:
    """把一条 Task 投影成可安全展示的只读摘要。

    只暴露列表所需字段；``prompt``、``delivery`` 与 ``budget`` 一律不出现在 Bridge
    响应里——它们可能含有敏感指令或投递目标。
    """
    schedule = task.schedule
    next_run_at = schedule.next_run_at
    return {
        "task_id": task.id,
        "name": task.name,
        "status": task.status.value,
        "schedule_kind": schedule.kind.value,
        "next_run_at": None if next_run_at is None else next_run_at.isoformat(),
    }


def _run_summary(run: object) -> dict[str, JsonValue]:
    """把一次运行记录投影成只读摘要，错误只保留稳定错误码。"""
    return {
        "run_id": run.id,
        "task_id": run.task_id,
        "status": run.status.value,
        "scheduled_for": run.scheduled_for.isoformat(),
        "error_code": run.error_code,
    }


def _automation_error_code(error: Exception) -> str:
    """把 Core 的自动化异常映射为稳定、可展示的 Bridge 错误码。"""
    if isinstance(error, ScheduleError):
        return str(error) or "automation_schedule_invalid"
    if isinstance(error, AutomationStateError):
        return "automation_state_conflict"
    if isinstance(error, AutomationDataError):
        return "automation_data_invalid"
    return "automation_invalid"
