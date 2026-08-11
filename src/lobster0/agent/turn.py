"""把一次 CLI 输入编排为可持久化的 Agent Turn。"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol, cast
from uuid import uuid4

from lobster0.agent.compaction import ContextCompactor
from lobster0.agent.context import ContextBuilder, ContextError
from lobster0.agent.events import RunEvent, RunEventHandler, emit
from lobster0.agent.runner import (
    AgentError,
    AgentLoopLimitError,
    AgentNoProgressError,
    AgentRunBudget,
    AgentRunner,
    AgentRunResult,
    AgentRunStatus,
    EmptyModelResponseError,
    UnparsedToolCallError,
)
from lobster0.artifacts.store import ArtifactError, ArtifactStore, display_filename
from lobster0.automation.models import TaskResponse
from lobster0.config import WorkspaceConfig
from lobster0.memory.flush import MemoryCapture
from lobster0.memory.models import ConversationKind, DisclosureContext
from lobster0.policy.approvals import ApprovalDecision, ApprovalError
from lobster0.providers.base import (
    JsonValue,
    ModelMessage,
    ProviderAuthenticationError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    StreamHandler,
    ToolCall,
)
from lobster0.storage.conversations import (
    ConversationDataError,
    ConversationStateError,
    MessageRepository,
    SessionRepository,
    StoredMessage,
    TurnRepository,
)
from lobster0.storage.tooling import ApprovalRepository, StoredToolRun
from lobster0.tools.base import ToolContext, ToolResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TurnResult:
    """表示 Channel 可发送且可关联数据库记录的一次成功 Turn。"""

    turn_id: int
    session_id: int
    content: str
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None
    message_id: int | None
    approval_id: int | None
    terminal_response: TaskResponse | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class TurnExecutionProfile:
    """保存交互式或后台 Turn 的 Tool、预算和 provenance 边界。"""

    source: Literal["interactive", "automation"] = "interactive"
    task_run_id: int | None = None
    allowed_tool_names: frozenset[str] | None = None
    budget: AgentRunBudget | None = None
    automation_gate: Callable[[], bool] | None = None

    def __post_init__(self) -> None:
        """要求 automation profile 绑定 Run、Tool allowlist 与预算。"""
        if self.source not in {"interactive", "automation"}:
            raise ValueError("turn execution source is invalid")
        if self.source == "automation" and (
            type(self.task_run_id) is not int
            or self.task_run_id <= 0
            or not isinstance(self.allowed_tool_names, frozenset)
            or self.budget is None
        ):
            raise ValueError("automation turn profile is incomplete")


class AutomationApprovalContinuation(Protocol):
    """收窄 TurnService 对 durable TaskRun 审批续跑的状态机依赖。"""

    def begin(self, profile: TurnExecutionProfile, approval_id: int) -> None:
        """在任何已批准 Tool side effect 前恢复绑定的 waiting Run。"""
        ...

    def settle(
        self,
        profile: TurnExecutionProfile,
        approval_id: int,
        result: TurnResult,
    ) -> None:
        """把 continuation 的 Approval 或 terminal result 写回 TaskRun。"""
        ...

    def fail(
        self,
        profile: TurnExecutionProfile,
        approval_id: int,
        *,
        error_code: str,
        session_id: int,
        turn_id: int | None,
        interrupted: bool = False,
        timed_out: bool = False,
    ) -> None:
        """以稳定错误码结算已经开始但失败的 continuation。"""
        ...


class TurnService:
    """协调 Session、Context、Runner 与终态持久化。"""

    def __init__(
        self,
        *,
        owner_id: int,
        model: str,
        sessions: SessionRepository,
        messages: MessageRepository,
        turns: TurnRepository,
        context: ContextBuilder,
        runner: AgentRunner,
        approvals: ApprovalRepository | None = None,
        compactor: ContextCompactor | None = None,
        memory_capture: MemoryCapture | None = None,
        automation_gate: Callable[[], bool] | None = None,
        automation_continuation: AutomationApprovalContinuation | None = None,
        artifacts: ArtifactStore | None = None,
        state_home: Path,
        workspace: WorkspaceConfig,
    ) -> None:
        """绑定一次应用运行期共用的模型配置和协作组件。

        Args:
            owner_id: 当前 Memory Space 的唯一 Owner 数据库 ID。
            model: 所有新 Turn 固定使用并记录的模型 ID。
            sessions: 负责 CLI Session 幂等解析的 Repository。
            messages: 负责按顺序读取最近历史的 Repository。
            turns: 负责 User Message 和 Turn 终态事务的 Repository。
            context: 负责身份文件与历史组合的 ContextBuilder。
            runner: 负责模型与 Tool Call 循环的 AgentRunner。
            artifacts: 可选的 ArtifactStore；缺省时附件功能不可用。
            memory_capture: 可选的 completed Turn durable capture，不运行提取器。
            automation_continuation: 可选的 durable TaskRun 审批续跑结算器。
            state_home: 当前实例的状态根目录。
            workspace: 当前可写与额外只读文件边界。
        """
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("owner_id must be a positive integer")
        self._owner_id = owner_id
        self._model = model
        self._sessions = sessions
        self._messages = messages
        self._turns = turns
        self._context = context
        self._runner = runner
        self._approvals = approvals
        self._compactor = compactor
        self._memory_capture = memory_capture
        self._automation_gate = automation_gate
        self._automation_continuation = automation_continuation
        self._artifacts = artifacts
        self._state_home = state_home
        self._workspace = workspace

    async def handle(
        self,
        user_id: int,
        text: str,
        conversation_id: str,
        on_text: StreamHandler | None = None,
        *,
        on_event: RunEventHandler | None = None,
        attachments: tuple[tuple[str, str], ...] = (),
    ) -> TurnResult:
        """执行并持久化一条 CLI 用户消息。

        Args:
            user_id: 当前唯一 Owner 的数据库 ID。
            text: 非空用户输入原文。
            conversation_id: CLI 指定或默认的稳定会话标识。
            on_text: 可选的 Channel 文本流回调。
            attachments: ``(artifact_id, filename)`` 序列；为空时行为与无附件完全
                一致。文件名只作展示，类型与大小一律由 Core 从 Store 读。

        Returns:
            最终回答与内部 Turn/Session/用量标识。

        Raises:
            ValueError: 输入文本为空。
            ContextError: 身份文件无法读取。
            AgentError: 模型循环为空或达到上限。
            ProviderError: 模型认证、速率、超时、协议或服务端失败。
            asyncio.CancelledError: 调用方取消，数据库已保存 cancelled。
        """
        return await self.handle_inbound(
            user_id=user_id,
            channel="cli",
            account_id="local",
            external_conversation_id=conversation_id,
            inbound_event_id=f"cli:{uuid4()}",
            text=text,
            on_text=on_text,
            on_event=on_event,
            trusted_owner=True,
            conversation_kind="local",
            identity_verified=True,
            attachments=attachments,
        )

    def _attachment_summaries(
        self, attachments: tuple[tuple[str, str], ...]
    ) -> tuple[dict[str, JsonValue], ...]:
        """把 id 解析成可安全展示的附件摘要。

        每个字段都从 Store 读，调用方只能给 id——Renderer 无法谎报文件名或类型。
        ``read_metadata`` 同时完成存在性、归属与本地文件形状校验，伪造 id 在这里
        就会抛错，还没轮到建 Turn。
        """
        if not attachments:
            return ()
        if self._artifacts is None:
            raise ArtifactError("artifact_unavailable", "artifact store is unavailable")
        summaries: list[dict[str, JsonValue]] = []
        for artifact_id, filename in attachments:
            artifact = self._artifacts.read_metadata(artifact_id)
            summaries.append(
                {
                    "artifact_id": artifact.artifact_id,
                    "filename": display_filename(filename),
                    "media_type": artifact.media_type,
                    "byte_size": artifact.byte_size,
                }
            )
        return tuple(summaries)

    async def handle_automation(
        self,
        *,
        task_id: int,
        task_run_id: int,
        text: str,
        profile: TurnExecutionProfile,
    ) -> TurnResult:
        """在 fresh automation Session 中执行一次受限后台 Turn。

        参数：
            task_id: 仅用于构造隔离会话键的持久 Task ID。
            task_run_id: 与 profile 绑定的当前 durable Run ID。
            text: Run snapshot 中已经过 Guard 的 Prompt。
            profile: Tool allowlist、Agent budget 与 E-stop gate。

        返回：
            包含 terminal response、Approval 或稳定错误码的 TurnResult。

        异常：
            AgentError: Agent Loop 或 Context 失败，Turn 已标记 failed。
            ProviderError: Provider 失败，Turn 已标记 failed。
            asyncio.CancelledError: timeout/shutdown 取消，Turn 已标记 cancelled。
        """
        if (
            type(task_id) is not int
            or task_id <= 0
            or task_run_id != profile.task_run_id
            or profile.source != "automation"
        ):
            raise ValueError("automation turn identity is invalid")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("automation prompt must not be empty")
        session_key = f"task:{task_id}:run:{task_run_id}"
        session = self._sessions.get_or_create(
            self._owner_id,
            "automation",
            "local",
            session_key,
        )
        inbound_event_id = f"automation:{task_run_id}"
        turn = self._turns.create_with_user_message(
            session.id,
            inbound_event_id,
            self._model,
            text,
        )
        self._turns.mark_running(turn.id)
        disclosure = DisclosureContext(
            owner_id=self._owner_id,
            requester_user_id=self._owner_id,
            channel="cli",
            conversation_kind="local",
            identity_verified=True,
        )
        try:
            history = tuple(
                _model_message(message)
                for message in self._messages.list_context(session.id)
            )
            request = self._context.build(
                self._model,
                history,
                disclosure=disclosure,
                tools=self._runner.tool_schemas_for(profile.allowed_tool_names),
            )
            request = replace(
                request,
                runtime_snapshot={
                    **request.runtime_snapshot,
                    **_automation_snapshot(profile),
                },
            )
            result = await self._runner.run(
                request,
                tool_context=ToolContext(
                    user_id=self._owner_id,
                    session_id=session.id,
                    turn_id=turn.id,
                    state_home=self._state_home,
                    workspace=self._workspace.path,
                    read_only_roots=self._workspace.read_only_roots,
                    write_roots=self._workspace.write_roots,
                    owner_home=self._workspace.owner_home,
                    trusted_owner=True,
                    disclosure=disclosure,
                    source="automation",
                    task_run_id=task_run_id,
                    account_id="local",
                    external_conversation_id=session_key,
                    allowed_tool_names=profile.allowed_tool_names,
                    automation_gate=profile.automation_gate,
                ),
                on_intermediate=lambda batch: self._turns.append_intermediate_messages(
                    turn.id,
                    session.id,
                    batch,
                ),
                budget=profile.budget,
            )
            if result.error_code is not None:
                self._turns.fail(turn.id, result.error_code, result.error_code)
                return TurnResult(
                    turn_id=turn.id,
                    session_id=session.id,
                    content="",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    provider_request_id=result.provider_request_id,
                    message_id=None,
                    approval_id=None,
                    error_code=result.error_code,
                )
            assistant = self._persist_result(
                turn.id,
                session.id,
                result,
                request.runtime_snapshot,
            )
            return TurnResult(
                turn_id=turn.id,
                session_id=session.id,
                content=result.content,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                provider_request_id=result.provider_request_id,
                message_id=None if assistant is None else assistant.id,
                approval_id=result.approval_id,
                terminal_response=result.terminal_response,
            )
        except asyncio.CancelledError:
            self._turns.cancel(turn.id)
            raise
        except (ContextError, ConversationDataError, AgentError, ProviderError) as error:
            error_code = _error_code(error)
            self._turns.fail(turn.id, error_code, error_code)
            raise

    async def handle_inbound(
        self,
        *,
        user_id: int,
        channel: str,
        account_id: str,
        external_conversation_id: str,
        inbound_event_id: str,
        text: str,
        on_text: StreamHandler | None = None,
        on_event: RunEventHandler | None = None,
        trusted_owner: bool = False,
        conversation_kind: ConversationKind = "unknown",
        identity_verified: bool = False,
        attachments: tuple[tuple[str, str], ...] = (),
    ) -> TurnResult:
        """执行 Channel 消息，并分别绑定自动化信任与 Memory 披露边界。"""
        if not text.strip():
            raise ValueError("message must not be empty")
        _validate_inbound_event_id(inbound_event_id)
        disclosure = DisclosureContext(
            owner_id=self._owner_id,
            requester_user_id=user_id if identity_verified else None,
            channel=channel,
            conversation_kind=conversation_kind,
            identity_verified=identity_verified,
        )
        session = self._sessions.get_or_create(
            user_id,
            channel,
            account_id,
            external_conversation_id,
        )
        # 清单在建 Turn 之前解析：伪造 id 应该整体拒绝，而不是留下半条 Turn。
        summaries = self._attachment_summaries(attachments)
        stored_text = _with_attachment_manifest(text, summaries)
        try:
            turn = self._turns.create_with_user_message(
                session.id,
                inbound_event_id,
                self._model,
                stored_text,
                attachments=summaries,
            )
        except sqlite3.IntegrityError:
            return self._completed_duplicate(session.id, inbound_event_id)
        for summary in summaries:
            assert self._artifacts is not None
            self._artifacts.link(
                str(summary["artifact_id"]),
                session_id=session.id,
                origin="user_upload",
                filename=str(summary["filename"]) or None,
            )
        self._turns.mark_running(turn.id)
        started = time.monotonic()

        try:
            await emit(
                on_event,
                RunEvent("turn_started", turn.id, {"session_id": session.id}),
            )
            history = tuple(
                _model_message(message)
                for message in self._messages.list_context(session.id)
            )
            request = self._context.build(
                self._model,
                history,
                disclosure=disclosure,
                tools=self._runner.tool_schemas,
            )
            if self._compactor is not None and self._compactor.should_compact(request):
                if self._memory_capture is not None:
                    self._memory_capture.flush()
                compacted = await self._compactor.compact(session.id)
                if compacted is not None:
                    history = tuple(
                        _model_message(message)
                        for message in self._messages.list_context(session.id)
                    )
                    request = self._context.build(
                        self._model,
                        history,
                        disclosure=disclosure,
                        tools=self._runner.tool_schemas,
                    )
            tool_context = ToolContext(
                user_id=user_id,
                session_id=session.id,
                turn_id=turn.id,
                state_home=self._state_home,
                workspace=self._workspace.path,
                read_only_roots=(
                    self._workspace.read_only_roots if trusted_owner else ()
                ),
                write_roots=self._workspace.write_roots if trusted_owner else (),
                owner_home=self._workspace.owner_home if trusted_owner else None,
                trusted_owner=trusted_owner,
                disclosure=disclosure,
                account_id=account_id,
                external_conversation_id=external_conversation_id,
            )
            result = await self._runner.run(
                request,
                on_text,
                tool_context=tool_context,
                on_intermediate=lambda batch: self._turns.append_intermediate_messages(
                    turn.id,
                    session.id,
                    batch,
                ),
                on_event=on_event,
            )
            assistant = self._persist_result(
                turn.id,
                session.id,
                result,
                request.runtime_snapshot,
            )
            if (
                result.status is AgentRunStatus.COMPLETED
                and assistant is not None
                and self._memory_capture is not None
            ):
                try:
                    self._memory_capture.capture_completed(
                        owner_id=self._owner_id,
                        session_id=session.id,
                        turn_id=turn.id,
                        disclosure=disclosure,
                    )
                except Exception:
                    _LOGGER.warning("memory_capture_failed", exc_info=False)
            if result.status is AgentRunStatus.COMPLETED:
                await emit(
                    on_event,
                    RunEvent(
                        "turn_finished",
                        turn.id,
                        {
                            "status": "completed",
                            "content": result.content,
                            **_telemetry(result, started),
                        },
                    ),
                )
        except asyncio.CancelledError:
            self._turns.cancel(turn.id)
            await emit(
                on_event,
                RunEvent(
                    "turn_cancelled",
                    turn.id,
                    {"duration_ms": _elapsed_ms(started)},
                ),
            )
            raise
        except (ContextError, ConversationDataError, AgentError, ProviderError) as error:
            error_code = _error_code(error)
            self._turns.fail(turn.id, error_code, str(error))
            await emit(
                on_event,
                RunEvent(
                    "turn_failed",
                    turn.id,
                    {
                        "error_code": error_code,
                        "duration_ms": _elapsed_ms(started),
                    },
                ),
            )
            raise

        return TurnResult(
            turn_id=turn.id,
            session_id=session.id,
            content=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider_request_id=result.provider_request_id,
            message_id=None if assistant is None else assistant.id,
            approval_id=result.approval_id,
        )

    async def continue_approval(
        self,
        user_id: int,
        approval_id: int,
        *,
        decision: ApprovalDecision,
        on_text: StreamHandler | None = None,
        on_event: RunEventHandler | None = None,
    ) -> TurnResult:
        """批准或拒绝绑定 ToolRun，并用没有假 User Message 的 child Turn 继续。"""
        if self._approvals is None:
            raise AgentError("approval repository is required")
        approval = self._approvals.get(user_id, approval_id)
        parent = self._turns.get(approval.turn_id)
        if parent.status != "waiting_approval":
            raise ApprovalError("already_decided", "approval Turn is not waiting")
        profile = self._continuation_profile(parent.runtime_snapshot)

        approved = decision is not ApprovalDecision.DENY
        continuation_started = False
        try:
            if approved:
                self._approvals.validate_decision(user_id, approval_id, decision)
                if approval.status == "pending":
                    self._approvals.approve(user_id, approval_id)
                elif approval.status != "approved":
                    raise ApprovalError("already_decided", "approval is not pending")
                if profile is not None and self._automation_continuation is not None:
                    self._automation_continuation.begin(profile, approval_id)
                    continuation_started = True
                run = self._approvals.consume(user_id, approval_id)
            else:
                if profile is not None and self._automation_continuation is not None:
                    self._automation_continuation.begin(profile, approval_id)
                    continuation_started = True
                run = self._approvals.deny(user_id, approval_id)
        except Exception:
            if continuation_started:
                assert profile is not None and self._automation_continuation is not None
                self._automation_continuation.fail(
                    profile,
                    approval_id,
                    error_code="approval_continuation_failed",
                    session_id=parent.session_id,
                    turn_id=None,
                )
            raise

        child = self._turns.create_continuation(
            parent.session_id,
            approval_id,
            parent.id,
            self._model,
        )
        self._turns.mark_running(child.id)
        disclosure = self._continuation_disclosure(
            parent.runtime_snapshot,
            user_id=user_id,
        )
        started = time.monotonic()
        deadline = (
            None
            if profile is None or profile.budget is None
            else asyncio.get_running_loop().time() + profile.budget.timeout_seconds
        )
        try:
            await emit(
                on_event,
                RunEvent("turn_started", child.id, {"session_id": parent.session_id}),
            )
            if approved:
                approved_execution = self._runner.execute_approved(
                    self._tool_context(
                        user_id,
                        parent.session_id,
                        parent.id,
                        disclosure,
                        profile=profile,
                    ),
                    run,
                    approval_id,
                    decision,
                    on_event,
                )
                if deadline is None:
                    model_text = await approved_execution
                else:
                    async with asyncio.timeout_at(deadline):
                        model_text = await approved_execution
            else:
                model_text = ToolResult.failure(
                    "approval_denied",
                    f"approval {approval_id} was denied",
                ).to_model_text(run.tool_name)
                await emit(
                    on_event,
                    RunEvent(
                        "tool_finished",
                        parent.id,
                        {
                            "call_id": run.tool_call_id,
                            "tool_name": run.tool_name,
                            "status": "denied",
                            "preview": model_text,
                        },
                    ),
                )
            history_before = self._messages.list_recent(parent.session_id, limit=20)
            self._turns.append_intermediate_messages(
                child.id,
                parent.session_id,
                _continuation_messages(history_before, run, model_text),
            )
            history = tuple(
                _model_message(message)
                for message in self._messages.list_context(parent.session_id, limit=20)
            )
            request = self._context.build(
                self._model,
                history,
                disclosure=disclosure,
                tools=(
                    self._runner.tool_schemas
                    if profile is None
                    else self._runner.tool_schemas_for(profile.allowed_tool_names)
                ),
            )
            if profile is not None:
                request = replace(
                    request,
                    runtime_snapshot={
                        **request.runtime_snapshot,
                        **_automation_snapshot(profile),
                    },
                )
            agent_execution = self._runner.run(
                request,
                on_text,
                tool_context=self._tool_context(
                    user_id,
                    parent.session_id,
                    child.id,
                    disclosure,
                    profile=profile,
                ),
                on_intermediate=lambda batch: self._turns.append_intermediate_messages(
                    child.id,
                    parent.session_id,
                    batch,
                ),
                on_event=on_event,
                budget=None if profile is None else profile.budget,
            )
            if deadline is None:
                result = await agent_execution
            else:
                async with asyncio.timeout_at(deadline):
                    result = await agent_execution
            if result.error_code is not None:
                self._turns.fail(child.id, result.error_code, result.error_code)
                turn_result = TurnResult(
                    turn_id=child.id,
                    session_id=parent.session_id,
                    content="",
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    provider_request_id=result.provider_request_id,
                    message_id=None,
                    approval_id=None,
                    error_code=result.error_code,
                )
                if continuation_started:
                    assert profile is not None and self._automation_continuation is not None
                    self._automation_continuation.settle(profile, approval_id, turn_result)
                return turn_result
            assistant = self._persist_result(
                child.id,
                parent.session_id,
                result,
                request.runtime_snapshot,
            )
            if result.status is AgentRunStatus.COMPLETED:
                await emit(
                    on_event,
                    RunEvent(
                        "turn_finished",
                        child.id,
                        {
                            "status": "completed",
                            "content": result.content,
                            **_telemetry(result, started),
                        },
                    ),
                )
        except TimeoutError:
            self._turns.fail(child.id, "task_timeout", "task_timeout")
            if continuation_started:
                assert profile is not None and self._automation_continuation is not None
                self._automation_continuation.fail(
                    profile,
                    approval_id,
                    error_code="task_timeout",
                    session_id=parent.session_id,
                    turn_id=child.id,
                    timed_out=True,
                )
            await emit(
                on_event,
                RunEvent(
                    "turn_failed",
                    child.id,
                    {"error_code": "task_timeout", "duration_ms": _elapsed_ms(started)},
                ),
            )
            return TurnResult(
                turn_id=child.id,
                session_id=parent.session_id,
                content="",
                input_tokens=0,
                output_tokens=0,
                provider_request_id=None,
                message_id=None,
                approval_id=None,
                error_code="task_timeout",
            )
        except asyncio.CancelledError:
            self._turns.cancel(child.id)
            if continuation_started:
                assert profile is not None and self._automation_continuation is not None
                self._automation_continuation.fail(
                    profile,
                    approval_id,
                    error_code="task_cancelled",
                    session_id=parent.session_id,
                    turn_id=child.id,
                    interrupted=True,
                )
            await emit(
                on_event,
                RunEvent(
                    "turn_cancelled",
                    child.id,
                    {"duration_ms": _elapsed_ms(started)},
                ),
            )
            raise
        except (ContextError, ConversationDataError, AgentError, ProviderError) as error:
            error_code = _error_code(error)
            self._turns.fail(child.id, error_code, str(error))
            if continuation_started:
                assert profile is not None and self._automation_continuation is not None
                self._automation_continuation.fail(
                    profile,
                    approval_id,
                    error_code=error_code,
                    session_id=parent.session_id,
                    turn_id=child.id,
                )
            await emit(
                on_event,
                RunEvent(
                    "turn_failed",
                    child.id,
                    {
                        "error_code": error_code,
                        "duration_ms": _elapsed_ms(started),
                    },
                ),
            )
            raise

        turn_result = TurnResult(
            turn_id=child.id,
            session_id=parent.session_id,
            content=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider_request_id=result.provider_request_id,
            message_id=None if assistant is None else assistant.id,
            approval_id=result.approval_id,
            terminal_response=result.terminal_response,
            error_code=result.error_code,
        )
        if continuation_started:
            assert profile is not None and self._automation_continuation is not None
            self._automation_continuation.settle(profile, approval_id, turn_result)
        return turn_result

    def _tool_context(
        self,
        user_id: int,
        session_id: int,
        turn_id: int,
        disclosure: DisclosureContext,
        *,
        profile: TurnExecutionProfile | None = None,
    ) -> ToolContext:
        """构造模型无法覆盖的统一 Tool 与 Memory 运行边界。"""
        return ToolContext(
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            state_home=self._state_home,
            workspace=self._workspace.path,
            read_only_roots=self._workspace.read_only_roots,
            write_roots=self._workspace.write_roots,
            owner_home=self._workspace.owner_home,
            disclosure=disclosure,
            source="interactive" if profile is None else profile.source,
            task_run_id=None if profile is None else profile.task_run_id,
            allowed_tool_names=(
                None if profile is None else profile.allowed_tool_names
            ),
            automation_gate=(
                None if profile is None else profile.automation_gate
            ),
        )

    def _continuation_profile(
        self,
        snapshot: dict[str, JsonValue],
    ) -> TurnExecutionProfile | None:
        """从 Parent snapshot 恢复 automation Tool 与预算边界。"""
        if snapshot.get("source") != "automation":
            return None
        task_run_id = snapshot.get("task_run_id")
        names = snapshot.get("allowed_tool_names")
        budget = snapshot.get("automation_budget")
        if (
            type(task_run_id) is not int
            or not isinstance(names, list)
            or any(not isinstance(name, str) or not name for name in names)
            or not isinstance(budget, dict)
        ):
            raise ConversationDataError("automation continuation profile is invalid")
        try:
            parsed_budget = AgentRunBudget(
                max_turns=cast(int, budget.get("max_turns")),
                max_tool_calls=cast(int, budget.get("max_tool_calls")),
                timeout_seconds=cast(int, budget.get("timeout_seconds")),
                max_input_tokens=cast(int, budget.get("max_input_tokens")),
                max_output_tokens=cast(int, budget.get("max_output_tokens")),
                max_cost_microusd=cast(int | None, budget.get("max_cost_microusd")),
            )
        except (TypeError, ValueError) as error:
            raise ConversationDataError(
                "automation continuation budget is invalid"
            ) from error
        return TurnExecutionProfile(
            source="automation",
            task_run_id=task_run_id,
            allowed_tool_names=frozenset(names),
            budget=parsed_budget,
            automation_gate=self._automation_gate or (lambda: False),
        )

    def _continuation_disclosure(
        self,
        snapshot: dict[str, JsonValue],
        *,
        user_id: int,
    ) -> DisclosureContext:
        """从 Parent Turn 的安全元数据恢复审批 continuation 披露边界。"""
        channel = snapshot.get("memory_channel")
        kind = snapshot.get("memory_conversation_kind")
        if channel in {"cli", "feishu", "telegram", "discord"} and kind in {
            "local",
            "direct",
            "group",
            "unknown",
        }:
            return DisclosureContext(
                owner_id=self._owner_id,
                requester_user_id=user_id,
                channel=cast(str, channel),
                conversation_kind=cast(ConversationKind, kind),
                identity_verified=user_id == self._owner_id,
            )
        return DisclosureContext(
            owner_id=self._owner_id,
            requester_user_id=None,
            channel="feishu",
            conversation_kind="unknown",
            identity_verified=False,
        )

    def _persist_result(
        self,
        turn_id: int,
        session_id: int,
        result: AgentRunResult,
        runtime_snapshot: dict[str, JsonValue],
    ) -> StoredMessage | None:
        """把 Agent 正常完成或等待审批写为对应 Turn 状态。"""
        if result.status is AgentRunStatus.WAITING_APPROVAL:
            assert result.approval_id is not None
            self._turns.wait_for_approval(
                turn_id,
                session_id,
                result.approval_id,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                provider_request_id=result.provider_request_id,
                iterations=result.iterations,
                runtime_snapshot=runtime_snapshot,
            )
            return None
        return self._turns.complete_with_assistant_message(
            turn_id,
            session_id,
            result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider_request_id=result.provider_request_id,
            iterations=result.iterations,
            finish_reason=result.finish_reason,
            runtime_snapshot=runtime_snapshot,
        )

    def _completed_duplicate(
        self,
        session_id: int,
        inbound_event_id: str,
    ) -> TurnResult:
        """从持久化完成态恢复重复入站结果，绝不重放 Provider 或 Tool。"""
        turn = self._turns.get_by_inbound(session_id, inbound_event_id)
        if turn.status == "completed":
            message = self._messages.final_assistant_for_turn(turn.id)
            request_id = turn.runtime_snapshot.get("provider_request_id")
            if request_id is not None and not isinstance(request_id, str):
                raise ConversationDataError("provider request ID is invalid")
            return TurnResult(
                turn_id=turn.id,
                session_id=session_id,
                content=message.content,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                provider_request_id=request_id,
                message_id=message.id,
                approval_id=None,
            )
        if turn.status == "waiting_approval":
            approval_id = turn.runtime_snapshot.get("approval_id")
            if type(approval_id) is not int:
                raise ConversationDataError("approval ID is invalid")
            return TurnResult(
                turn_id=turn.id,
                session_id=session_id,
                content="",
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
                provider_request_id=None,
                message_id=None,
                approval_id=approval_id,
            )
        raise ConversationStateError("duplicate inbound Turn is not replayable")


def _continuation_messages(
    history: tuple[StoredMessage, ...],
    run: StoredToolRun,
    model_text: str,
) -> tuple[ModelMessage, ...]:
    """补齐当前 Tool Result，并标记同批后续调用未执行。"""
    assistant: ModelMessage | None = None
    for message in reversed(history):
        if message.role != "assistant":
            continue
        candidate = _model_message(message)
        if any(call.call_id == run.tool_call_id for call in candidate.tool_calls):
            assistant = candidate
            break
    if assistant is None:
        raise ConversationDataError("approval tool call is missing from conversation history")
    index = next(
        index
        for index, call in enumerate(assistant.tool_calls)
        if call.call_id == run.tool_call_id
    )
    skipped = ToolResult.failure(
        "not_executed",
        "tool call was skipped after an earlier call required approval",
    )
    return (
        ModelMessage(role="tool", content=model_text, tool_call_id=run.tool_call_id),
        *(
            ModelMessage(
                role="tool",
                content=skipped.to_model_text(call.name),
                tool_call_id=call.call_id,
            )
            for call in assistant.tool_calls[index + 1 :]
        ),
    )


def _automation_snapshot(profile: TurnExecutionProfile) -> dict[str, JsonValue]:
    """把可跨重启恢复的 automation profile 编码为安全 JSON。"""
    if profile.source != "automation" or profile.budget is None:
        raise ValueError("automation snapshot requires an automation profile")
    budget = profile.budget
    return {
        "source": "automation",
        "task_run_id": profile.task_run_id,
        "allowed_tool_names": sorted(profile.allowed_tool_names or ()),
        "automation_budget": {
            "max_turns": budget.max_turns,
            "max_tool_calls": budget.max_tool_calls,
            "timeout_seconds": budget.timeout_seconds,
            "max_input_tokens": budget.max_input_tokens,
            "max_output_tokens": budget.max_output_tokens,
            "max_cost_microusd": budget.max_cost_microusd,
        },
    }


def _model_message(message: StoredMessage) -> ModelMessage:
    """把持久消息恢复为 Provider 可接受的结构化历史。"""
    calls_value = message.metadata.get("tool_calls", [])
    reasoning_value = message.metadata.get("reasoning_content")
    if not isinstance(calls_value, list) or not isinstance(
        reasoning_value,
        (str, type(None)),
    ):
        raise ConversationDataError(f"invalid message metadata for message {message.id}")

    calls: list[ToolCall] = []
    for value in calls_value:
        if not isinstance(value, dict):
            raise ConversationDataError(f"invalid tool call metadata for message {message.id}")
        call_id = value.get("call_id")
        name = value.get("name")
        arguments = value.get("arguments")
        if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(
            arguments,
            dict,
        ):
            raise ConversationDataError(f"invalid tool call metadata for message {message.id}")
        calls.append(
            ToolCall(
                call_id=call_id,
                name=name,
                arguments=cast(dict[str, JsonValue], arguments),
            )
        )
    if message.role == "tool" and message.tool_call_id is None:
        raise ConversationDataError(f"tool message {message.id} has no tool_call_id")
    return ModelMessage(
        role=message.role,
        content=message.content,
        tool_calls=tuple(calls),
        tool_call_id=message.tool_call_id,
        reasoning_content=reasoning_value,
        metadata=message.metadata,
    )


def _telemetry(result: AgentRunResult, started: float) -> dict[str, JsonValue]:
    """返回只含可信标量的 Turn 运行指标。"""
    return {
        "context_tokens": result.context_tokens,
        "input_tokens": result.reported_input_tokens,
        "output_tokens": result.reported_output_tokens,
        "iterations": result.iterations,
        "tool_calls": result.tool_calls_count,
        "provider_request_id": result.provider_request_id,
        "duration_ms": _elapsed_ms(started),
    }


def _elapsed_ms(started: float) -> int:
    """把单调时钟差值转换为非负毫秒。"""
    return max(0, round((time.monotonic() - started) * 1000))


def _with_attachment_manifest(text: str, attachments: tuple[dict[str, JsonValue], ...]) -> str:
    """在正文后追加附件清单。

    清单**永远追加在最后**，所以用户在正文里手写一段同样格式的假清单不会被当成
    真的。没有附件时原样返回 ``text``——这条路径必须与引入附件之前逐字节一致。
    """
    if not attachments:
        return text
    listing = "\n".join(
        f"- {item['artifact_id']} · {item['filename']} · "
        f"{item['media_type']} · {item['byte_size']} B"
        for item in attachments
    )
    return f"{text}\n\n[附件]\n{listing}"


def _validate_inbound_event_id(value: str) -> None:
    """拒绝空白、超长或含控制字符的平台 Turn 幂等键。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)
    ):
        raise ValueError("inbound_event_id is invalid")


def _error_code(
    error: ContextError | ConversationDataError | AgentError | ProviderError,
) -> str:
    """把稳定异常类型映射为 SQLite 和 CLI 可共享的错误码。"""
    mappings = (
        (ProviderAuthenticationError, "provider_authentication"),
        (ProviderRateLimitError, "provider_rate_limit"),
        (ProviderTimeoutError, "provider_timeout"),
        (ProviderProtocolError, "provider_protocol"),
        (ProviderServerError, "provider_server"),
        (EmptyModelResponseError, "empty_response"),
        (UnparsedToolCallError, "unparsed_tool_call"),
        (AgentNoProgressError, "loop_no_progress"),
        (AgentLoopLimitError, "loop_limit"),
        (ConversationDataError, "conversation_data"),
        (ContextError, "context"),
        (ProviderError, "provider"),
        (AgentError, "agent"),
    )
    return next(code for error_type, code in mappings if isinstance(error, error_type))
