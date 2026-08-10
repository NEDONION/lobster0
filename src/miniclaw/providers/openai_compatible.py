"""通过 HTTPX 调用 OpenAI-compatible Chat Completions。"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from miniclaw.providers.base import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    StreamHandler,
    ToolCall,
)

type Sleep = Callable[[float], Awaitable[None]]

_MAX_ATTEMPTS = 2
_DEFAULT_RETRY_DELAY = 0.5
_MAX_RETRY_DELAY = 30.0


@dataclass(slots=True)
class _ToolAccumulator:
    """聚合同一个 SSE Tool Call 在多个 delta 中的字段。"""

    call_id: str | None = None
    name: str | None = None
    argument_parts: list[str] = field(default_factory=list)


class OpenAICompatibleProvider:
    """把稳定模型契约映射为异步 Chat Completions 请求。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep | None = None,
    ) -> None:
        """创建持有连接池但不主动联网的 Provider。

        Args:
            base_url: 已经由配置边界校验的服务根地址。
            api_key: 只用于当前实例请求头的完整 Bearer 凭据。
            timeout_seconds: HTTPX 完整超时配置使用的正整数秒数。
            transport: 测试可注入的异步传输；生产环境省略。
            sleep: 重试等待函数；测试可注入无等待实现。

        Raises:
            ValueError: 地址、凭据为空或超时不是正整数。
        """
        if not base_url.strip() or not api_key or timeout_seconds <= 0:
            raise ValueError("base URL, API Key, and positive timeout are required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._sleep = asyncio.sleep if sleep is None else sleep
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
        )

    async def aclose(self) -> None:
        """关闭 Provider 拥有的 HTTP 连接池。"""
        await self._client.aclose()

    async def complete(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> ModelResponse:
        """发送一次兼容请求并聚合流式或非流式响应。

        Args:
            request: 已由 ContextBuilder 或 Runner 构造的稳定请求。
            on_text: 可选异步最终文本增量回调。

        Returns:
            已校验文本、工具调用、reasoning、用量和请求 ID 的响应。

        Raises:
            ProviderAuthenticationError: 远端返回 401 或 403。
            ProviderRateLimitError: 429 在一次重试后仍失败。
            ProviderTimeoutError: 超时在一次重试后仍失败。
            ProviderServerError: 连接或 5xx 在一次重试后仍失败。
            ProviderProtocolError: 其他状态或响应不符合兼容协议。
        """
        payload = _request_payload(request)
        emitted_text = False

        async def forward_text(text: str) -> None:
            """记录已经对外可见的增量，再交给原始回调施加背压。"""
            nonlocal emitted_text
            emitted_text = True
            if on_text is not None:
                await on_text(text)

        stream_handler = forward_text if on_text is not None else None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                async with self._client.stream(
                    "POST",
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=self._request_headers(),
                ) as response:
                    if await _status_error(response, attempt):
                        delay = _retry_delay(response)
                    else:
                        try:
                            return await _parse_response(response, stream_handler)
                        except ProviderProtocolError:
                            if emitted_text or attempt + 1 >= _MAX_ATTEMPTS:
                                raise
                            delay = _DEFAULT_RETRY_DELAY
                await self._sleep(delay)
            except httpx.TimeoutException as error:
                if not emitted_text and attempt + 1 < _MAX_ATTEMPTS:
                    await self._sleep(_DEFAULT_RETRY_DELAY)
                    continue
                raise ProviderTimeoutError("model provider request timed out") from error
            except httpx.TransportError as error:
                if not emitted_text and attempt + 1 < _MAX_ATTEMPTS:
                    await self._sleep(_DEFAULT_RETRY_DELAY)
                    continue
                raise ProviderServerError("model provider connection failed") from error
        raise ProviderServerError("model provider request exhausted retries")

    def _request_headers(self) -> dict[str, str]:
        """为当前请求构造不会进入日志的认证与内容协商 Header。"""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "text/event-stream, application/json",
            "Content-Type": "application/json",
        }


def _request_payload(request: ModelRequest) -> dict[str, object]:
    """把稳定请求转换为 OpenAI-compatible JSON 对象。"""
    payload: dict[str, object] = {
        "model": request.model,
        "messages": [_message_payload(message) for message in request.messages],
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if request.tools:
        payload["tools"] = list(request.tools)
    if request.temperature is not None:
        payload["temperature"] = request.temperature
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens
    return payload


def _message_payload(message: ModelMessage) -> dict[str, object]:
    """把一条稳定消息转换为兼容协议字段并保留 reasoning。"""
    payload: dict[str, object] = {"role": message.role, "content": message.content}
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            }
            for call in message.tool_calls
        ]
    if message.tool_call_id is not None:
        payload["tool_call_id"] = message.tool_call_id
    if message.reasoning_content is not None:
        payload["reasoning_content"] = message.reasoning_content
    return payload


_MAX_ERROR_BODY_CHARS = 500


async def _status_error(response: httpx.Response, attempt: int) -> bool:
    """校验 HTTP 状态；成功返回假，可重试返回真，其他情况抛错。

    非重试状态会把响应正文（有界截断）附带进异常信息，只落库到 Turn 的
    error_message／日志供事后诊断，绝不会被拼进 Channel 展示给用户的安全文案
    （见 channels/manager.py 的 ``_failure_diagnostics``）。
    """
    status = response.status_code
    if 200 <= status < 300:
        return False
    if status in {401, 403}:
        raise ProviderAuthenticationError("model provider authentication failed")
    if status == 429:
        if attempt + 1 < _MAX_ATTEMPTS:
            return True
        raise ProviderRateLimitError("model provider rate limit exceeded")
    if 500 <= status < 600:
        if attempt + 1 < _MAX_ATTEMPTS:
            return True
        raise ProviderServerError("model provider server error")
    detail = await _read_error_body(response)
    message = f"model provider rejected the request with status {status}"
    if detail:
        message = f"{message}: {detail}"
    raise ProviderProtocolError(message)


async def _read_error_body(response: httpx.Response) -> str:
    """尽力读取并截断错误响应正文；读取失败时静默返回空串。"""
    try:
        raw = await response.aread()
    except Exception:
        return ""
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) > _MAX_ERROR_BODY_CHARS:
        text = f"{text[:_MAX_ERROR_BODY_CHARS]}…"
    return text


def _retry_delay(response: httpx.Response) -> float:
    """解析数值 Retry-After，并把异常值限制在安全等待区间。"""
    value = response.headers.get("retry-after")
    if value is None:
        return _DEFAULT_RETRY_DELAY
    try:
        return min(max(float(value), 0.0), _MAX_RETRY_DELAY)
    except ValueError:
        return _DEFAULT_RETRY_DELAY


async def _parse_response(
    response: httpx.Response,
    on_text: StreamHandler | None,
) -> ModelResponse:
    """按 Content-Type 选择 SSE 或普通 JSON 响应解析器。"""
    content_type = response.headers.get("content-type", "").lower()
    if "text/event-stream" in content_type:
        return await _parse_sse(response, on_text)
    try:
        body = json.loads(await response.aread())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderProtocolError("model provider returned invalid JSON") from error
    return await _parse_json_response(body, response, on_text)


async def _parse_sse(
    response: httpx.Response,
    on_text: StreamHandler | None,
) -> ModelResponse:
    """聚合 SSE data 事件中的文本、reasoning、工具片段和用量。"""
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tools: dict[int, _ToolAccumulator] = {}
    finish_reason: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    body_request_id: str | None = None
    saw_data = False

    async for line in response.aiter_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith(":"):
            continue
        if not stripped.startswith("data:"):
            raise ProviderProtocolError("model provider returned invalid SSE")
        data = stripped[5:].strip()
        if data == "[DONE]":
            break
        event = _decode_object(data)
        saw_data = True
        body_request_id = _optional_string(event.get("id")) or body_request_id
        input_tokens, output_tokens = _usage(event.get("usage"), input_tokens, output_tokens)
        choices = event.get("choices")
        if not isinstance(choices, list):
            raise ProviderProtocolError("model provider response choices are invalid")
        if not choices:
            continue
        choice = _object(choices[0], "model provider response choice is invalid")
        delta = _object(choice.get("delta"), "model provider response delta is invalid")
        content = _optional_string(delta.get("content"))
        reasoning = _optional_string(delta.get("reasoning_content"))
        if content:
            content_parts.append(content)
            if on_text is not None:
                await on_text(content)
        if reasoning:
            reasoning_parts.append(reasoning)
        _merge_tool_fragments(delta.get("tool_calls"), tools)
        if choice.get("finish_reason") is not None:
            finish_reason = _required_string(choice.get("finish_reason"), "finish reason")

    if not saw_data or finish_reason is None:
        raise ProviderProtocolError("model provider returned an incomplete SSE response")
    return ModelResponse(
        content="".join(content_parts),
        tool_calls=_finish_tools(tools),
        reasoning_content="".join(reasoning_parts) or None,
        finish_reason=finish_reason,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id=_request_id(response, body_request_id),
    )


async def _parse_json_response(
    body: object,
    response: httpx.Response,
    on_text: StreamHandler | None,
) -> ModelResponse:
    """解析被兼容端点返回的非流式 Chat Completion JSON。"""
    root = _object(body, "model provider JSON response is invalid")
    choices = root.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderProtocolError("model provider response choices are invalid")
    choice = _object(choices[0], "model provider response choice is invalid")
    message = _object(choice.get("message"), "model provider response message is invalid")
    content = _optional_string(message.get("content")) or ""
    reasoning = _optional_string(message.get("reasoning_content"))
    if content and on_text is not None:
        await on_text(content)
    tools: dict[int, _ToolAccumulator] = {}
    _merge_tool_fragments(message.get("tool_calls"), tools)
    input_tokens, output_tokens = _usage(root.get("usage"), None, None)
    return ModelResponse(
        content=content,
        tool_calls=_finish_tools(tools),
        reasoning_content=reasoning,
        finish_reason=_required_string(choice.get("finish_reason"), "finish reason"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        provider_request_id=_request_id(response, _optional_string(root.get("id"))),
    )


def _merge_tool_fragments(value: object, tools: dict[int, _ToolAccumulator]) -> None:
    """按 index 合并流式或完整响应中的 Tool Call 字段。

    Args:
        value: 当前响应分片中的动态 ``tool_calls`` 值。
        tools: 按调用序号保存的聚合器，函数会原地追加合法分片。

    Raises:
        ProviderProtocolError: Tool Call 结构、索引、名称或参数不符合兼容协议。
    """
    if value is None:
        return
    if not isinstance(value, list):
        raise ProviderProtocolError("model provider tool calls are invalid")
    for fallback_index, raw_call in enumerate(value):
        call = _object(raw_call, "model provider tool call is invalid")
        index_value = call.get("index", fallback_index)
        if type(index_value) is not int or index_value < 0:
            raise ProviderProtocolError("model provider tool call index is invalid")
        accumulator = tools.setdefault(index_value, _ToolAccumulator())
        if call.get("id") is not None:
            accumulator.call_id = _required_string(call.get("id"), "tool call id")
        function = _object(call.get("function"), "model provider tool function is invalid")
        name = function.get("name")
        if name is not None and name != "":
            accumulator.name = _required_string(name, "tool name")
        arguments = function.get("arguments")
        if arguments is not None:
            if isinstance(arguments, str):
                accumulator.argument_parts.append(arguments)
            elif isinstance(arguments, dict) and all(
                isinstance(key, str) for key in arguments
            ):
                # OpenAI 的正式协议要求 JSON 字符串，但部分兼容服务（包括
                # 某些 DeepSeek 网关）会直接返回已经解码的 object。这里先
                # 规范化为紧凑 JSON，再统一走 `_finish_tools` 的 object 校验。
                accumulator.argument_parts.append(
                    json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                )
            else:
                raise ProviderProtocolError("model provider tool arguments is invalid")


def _finish_tools(tools: dict[int, _ToolAccumulator]) -> tuple[ToolCall, ...]:
    """完成 Tool Call 校验与 JSON object 参数解码。"""
    finished: list[ToolCall] = []
    for index in sorted(tools):
        accumulator = tools[index]
        if not accumulator.call_id or not accumulator.name:
            raise ProviderProtocolError("model provider returned an incomplete tool call")
        try:
            arguments = json.loads("".join(accumulator.argument_parts) or "{}")
        except json.JSONDecodeError as error:
            raise ProviderProtocolError("model provider tool arguments are invalid JSON") from error
        if not isinstance(arguments, dict) or not all(isinstance(key, str) for key in arguments):
            raise ProviderProtocolError("model provider tool arguments must be a JSON object")
        finished.append(
            ToolCall(
                call_id=accumulator.call_id,
                name=accumulator.name,
                arguments=arguments,
            )
        )
    return tuple(finished)


def _usage(
    value: object,
    current_input: int | None,
    current_output: int | None,
) -> tuple[int | None, int | None]:
    """读取可选 usage，并保留流中上一事件已经提供的数值。"""
    if value is None:
        return current_input, current_output
    usage = _object(value, "model provider usage is invalid")
    return (
        _optional_token_count(usage.get("prompt_tokens"), current_input),
        _optional_token_count(usage.get("completion_tokens"), current_output),
    )


def _optional_token_count(value: object, previous: int | None) -> int | None:
    """校验非负整数 Token；字段缺失时保留上一流事件数值。"""
    if value is None:
        return previous
    if type(value) is not int or value < 0:
        raise ProviderProtocolError("model provider token usage is invalid")
    return value


def _decode_object(text: str) -> dict[str, object]:
    """解码一条 SSE JSON object，拒绝数组和标量顶层。"""
    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProviderProtocolError("model provider returned invalid SSE JSON") from error
    return _object(value, "model provider SSE event is invalid")


def _object(value: object, message: str) -> dict[str, object]:
    """把动态 JSON 值收窄为字符串键对象。"""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ProviderProtocolError(message)
    return value


def _optional_string(value: object) -> str | None:
    """校验可选字符串；协议中的 null 映射为 ``None``。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProviderProtocolError("model provider response string field is invalid")
    return value


def _required_string(value: object, field_name: str) -> str:
    """校验远端必填非空字符串并返回安全协议错误。"""
    if not isinstance(value, str) or not value:
        raise ProviderProtocolError(f"model provider {field_name} is invalid")
    return value


def _request_id(response: httpx.Response, body_request_id: str | None) -> str | None:
    """优先使用响应 Header 的请求 ID，再退回正文 ID。"""
    return response.headers.get("x-request-id") or body_request_id
