"""把一次 CLI 输入编排为可持久化的 Agent Turn。"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from uuid import uuid4

from miniclaw.agent.context import ContextBuilder, ContextError
from miniclaw.agent.runner import (
    AgentError,
    AgentLoopLimitError,
    AgentRunner,
    EmptyModelResponseError,
)
from miniclaw.config import WorkspaceConfig
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
from miniclaw.tools.base import ToolContext


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
        self._state_home = state_home
        self._workspace = workspace

    async def handle(
        self,
        user_id: int,
        text: str,
        conversation_id: str,
        on_text: StreamHandler | None = None,
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

        try:
            history = tuple(
                _model_message(message)
                for message in self._messages.list_recent(session.id, limit=20)
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
            )
            self._turns.complete_with_assistant_message(
                turn.id,
                session.id,
                result.content,
                intermediate_messages=result.intermediate_messages,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                provider_request_id=result.provider_request_id,
                iterations=result.iterations,
                finish_reason=result.finish_reason,
            )
        except asyncio.CancelledError:
            self._turns.cancel(turn.id)
            raise
        except (ContextError, ConversationDataError, AgentError, ProviderError) as error:
            self._turns.fail(turn.id, _error_code(error), str(error))
            raise

        return TurnResult(
            turn_id=turn.id,
            session_id=session.id,
            content=result.content,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            provider_request_id=result.provider_request_id,
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
    )


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
