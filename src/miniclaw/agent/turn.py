"""把一次 CLI 输入编排为可持久化的 Agent Turn。"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from miniclaw.agent.compaction import ContextCompactor
from miniclaw.agent.context import ContextBuilder, ContextError
from miniclaw.agent.events import RunEvent, RunEventHandler, emit
from miniclaw.agent.runner import (
    AgentError,
    AgentLoopLimitError,
    AgentRunner,
    AgentRunResult,
    AgentRunStatus,
    EmptyModelResponseError,
)
from miniclaw.config import WorkspaceConfig
from miniclaw.policy.approvals import ApprovalDecision, ApprovalError
from miniclaw.providers.base import (
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
from miniclaw.storage.conversations import (
    ConversationDataError,
    MessageRepository,
    SessionRepository,
    StoredMessage,
    TurnRepository,
)
from miniclaw.storage.tooling import ApprovalRepository, StoredToolRun
from miniclaw.tools.base import ToolContext, ToolResult


@dataclass(frozen=True, slots=True)
class TurnResult:
    """表示 Channel 可发送且可关联数据库记录的一次成功 Turn。"""

    turn_id: int
    session_id: int
    content: str
    input_tokens: int
    output_tokens: int
    provider_request_id: str | None


class TurnService:
    """协调 Session、Context、Runner 与终态持久化。"""

    def __init__(
        self,
        *,
        model: str,
        sessions: SessionRepository,
        messages: MessageRepository,
        turns: TurnRepository,
        context: ContextBuilder,
        runner: AgentRunner,
        approvals: ApprovalRepository | None = None,
        compactor: ContextCompactor | None = None,
        state_home: Path,
        workspace: WorkspaceConfig,
    ) -> None:
        """绑定一次应用运行期共用的模型配置和协作组件。

        Args:
            model: 所有新 Turn 固定使用并记录的模型 ID。
            sessions: 负责 CLI Session 幂等解析的 Repository。
            messages: 负责按顺序读取最近历史的 Repository。
            turns: 负责 User Message 和 Turn 终态事务的 Repository。
            context: 负责身份文件与历史组合的 ContextBuilder。
            runner: 负责模型与 Tool Call 循环的 AgentRunner。
            state_home: 当前实例的状态根目录。
            workspace: 当前可写与额外只读文件边界。
        """
        self._model = model
        self._sessions = sessions
        self._messages = messages
        self._turns = turns
        self._context = context
        self._runner = runner
        self._approvals = approvals
        self._compactor = compactor
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
    ) -> TurnResult:
        """执行并持久化一条 CLI 用户消息。

        Args:
            user_id: 当前唯一 Owner 的数据库 ID。
            text: 非空用户输入原文。
            conversation_id: CLI 指定或默认的稳定会话标识。
            on_text: 可选的 Channel 文本流回调。

        Returns:
            最终回答与内部 Turn/Session/用量标识。

        Raises:
            ValueError: 输入文本为空。
            ContextError: 身份文件无法读取。
            AgentError: 模型循环为空或达到上限。
            ProviderError: 模型认证、速率、超时、协议或服务端失败。
            asyncio.CancelledError: 调用方取消，数据库已保存 cancelled。
        """
        if not text.strip():
            raise ValueError("message must not be empty")
        session = self._sessions.get_or_create_cli(user_id, conversation_id)
        turn = self._turns.create_with_user_message(
            session.id,
            f"cli:{uuid4()}",
            self._model,
            text,
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
                tools=self._runner.tool_schemas,
            )
            if self._compactor is not None and self._compactor.should_compact(request):
                compacted = await self._compactor.compact(session.id)
                if compacted is not None:
                    history = tuple(
                        _model_message(message)
                        for message in self._messages.list_context(session.id)
                    )
                    request = self._context.build(
                        self._model,
                        history,
                        tools=self._runner.tool_schemas,
                    )
            tool_context = ToolContext(
                user_id=user_id,
                session_id=session.id,
                turn_id=turn.id,
                state_home=self._state_home,
                workspace=self._workspace.path,
                read_only_roots=self._workspace.read_only_roots,
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
            self._persist_result(
                turn.id,
                session.id,
                result,
                request.runtime_snapshot,
            )
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

        approved = decision is not ApprovalDecision.DENY
        if approved:
            self._approvals.validate_decision(user_id, approval_id, decision)
            if approval.status == "pending":
                self._approvals.approve(user_id, approval_id)
            elif approval.status != "approved":
                raise ApprovalError("already_decided", "approval is not pending")
            run = self._approvals.consume(user_id, approval_id)
        else:
            run = self._approvals.deny(user_id, approval_id)

        child = self._turns.create_continuation(
            parent.session_id,
            approval_id,
            parent.id,
            self._model,
        )
        self._turns.mark_running(child.id)
        started = time.monotonic()
        try:
            await emit(
                on_event,
                RunEvent("turn_started", child.id, {"session_id": parent.session_id}),
            )
            if approved:
                model_text = await self._runner.execute_approved(
                    self._tool_context(user_id, parent.session_id, parent.id),
                    run,
                    approval_id,
                    decision,
                    on_event,
                )
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
                for message in self._messages.list_recent(parent.session_id, limit=20)
            )
            request = self._context.build(
                self._model,
                history,
                tools=self._runner.tool_schemas,
            )
            result = await self._runner.run(
                request,
                on_text,
                tool_context=self._tool_context(user_id, parent.session_id, child.id),
                on_intermediate=lambda batch: self._turns.append_intermediate_messages(
                    child.id,
                    parent.session_id,
                    batch,
                ),
                on_event=on_event,
            )
            self._persist_result(
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
        except asyncio.CancelledError:
            self._turns.cancel(child.id)
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

        return TurnResult(
            turn_id=child.id,
            session_id=parent.session_id,
            content=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider_request_id=result.provider_request_id,
        )

    def _tool_context(self, user_id: int, session_id: int, turn_id: int) -> ToolContext:
        """构造模型无法覆盖的统一 Tool 运行边界。"""
        return ToolContext(
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
            state_home=self._state_home,
            workspace=self._workspace.path,
            read_only_roots=self._workspace.read_only_roots,
        )

    def _persist_result(
        self,
        turn_id: int,
        session_id: int,
        result: AgentRunResult,
        runtime_snapshot: dict[str, JsonValue],
    ) -> None:
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
            return
        self._turns.complete_with_assistant_message(
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
        (AgentLoopLimitError, "loop_limit"),
        (ConversationDataError, "conversation_data"),
        (ContextError, "context"),
        (ProviderError, "provider"),
        (AgentError, "agent"),
    )
    return next(code for error_type, code in mappings if isinstance(error, error_type))
