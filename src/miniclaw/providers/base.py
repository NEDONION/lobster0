"""Agent Core 与模型服务之间的稳定异步数据契约。"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]


class ProviderError(RuntimeError):
    """表示模型调用失败且公开消息已经过安全收窄。"""


class ProviderAuthenticationError(ProviderError):
    """表示 API Key 缺失、无效或没有访问目标模型的权限。"""


class ProviderRateLimitError(ProviderError):
    """表示模型服务在有限重试后仍拒绝当前请求速率。"""


class ProviderTimeoutError(ProviderError):
    """表示模型连接、写入、读取或完整请求超过允许时间。"""


class ProviderProtocolError(ProviderError):
    """表示请求参数或远端响应不符合支持的兼容协议。"""


class ProviderServerError(ProviderError):
    """表示模型服务在有限重试后仍返回服务端错误。"""


@dataclass(frozen=True, slots=True)
class ToolCall:
    """描述模型要求执行的一次结构化工具调用。"""

    call_id: str
    name: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """表示不依赖具体厂商 SDK 的单条模型上下文消息。"""

    role: str
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    reasoning_content: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """汇总一次模型补全需要的消息、工具与可选生成预算。"""

    model: str
    messages: tuple[ModelMessage, ...]
    tools: tuple[dict[str, JsonValue], ...] = ()
    temperature: float | None = None
    max_output_tokens: int | None = None
    runtime_snapshot: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """表示 Provider 完整聚合并校验后的单次模型响应。"""

    content: str
    tool_calls: tuple[ToolCall, ...]
    reasoning_content: str | None
    finish_reason: str
    input_tokens: int | None
    output_tokens: int | None
    provider_request_id: str | None


type StreamHandler = Callable[[str], Awaitable[None]]


class ModelProvider(Protocol):
    """定义 AgentRunner 可调用的最小异步模型能力。"""

    async def complete(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> ModelResponse:
        """完成一次模型请求并返回已聚合的文本、工具调用和用量。

        Args:
            request: 与厂商无关的消息和工具 Schema。
            on_text: 可选异步回调，仅接收最终答案文本增量。

        Returns:
            通过协议校验的完整模型响应。

        Raises:
            ProviderError: 认证、速率、超时、协议或服务端调用失败。
        """
        ...
