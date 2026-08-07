"""OpenAI-compatible Provider 的请求、流解析和错误测试。"""

import asyncio
import json
import unittest
from collections.abc import Awaitable, Callable

import httpx

from miniclaw.providers.base import (
    ModelMessage,
    ModelRequest,
    ProviderAuthenticationError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
)
from miniclaw.providers.openai_compatible import OpenAICompatibleProvider


def sse(*events: object) -> str:
    """把手写事件对象编码为测试用 SSE，不复用生产解析逻辑。"""
    lines = [
        f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}"
        for event in events
    ]
    return "\n\n".join((*lines, "data: [DONE]", ""))


SUCCESS_SSE = sse(
    {
        "id": "chat_1",
        "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
    },
    {
        "id": "chat_1",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 8, "completion_tokens": 2},
    },
)

TOOL_SSE = sse(
    {
        "id": "chat_tools",
        "choices": [
            {"index": 0, "delta": {"reasoning_content": "need "}, "finish_reason": None}
        ],
    },
    {
        "id": "chat_tools",
        "choices": [
            {"index": 0, "delta": {"reasoning_content": "file"}, "finish_reason": None}
        ],
    },
    {
        "id": "chat_tools",
        "choices": [{"index": 0, "delta": {"content": "读"}, "finish_reason": None}],
    },
    {
        "id": "chat_tools",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":'},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    },
    {
        "id": "chat_tools",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": '"README.md","limit":20}'},
                        }
                    ],
                    "content": "取完成",
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7},
    },
)


class GatedSseStream(httpx.AsyncByteStream):
    """只有文本回调释放事件后才发送结束帧，用于证明真正增量消费。"""

    def __init__(self, released: asyncio.Event) -> None:
        """保存由 on_text 回调设置的门控事件。"""
        self.released = released

    async def __aiter__(self):
        """先发送内容帧，等待回调，再发送 finish 与 DONE 帧。"""
        yield sse(
            {
                "id": "chat_gated",
                "choices": [
                    {"index": 0, "delta": {"content": "live"}, "finish_reason": None}
                ],
            }
        ).replace("data: [DONE]\n", "").encode()
        await asyncio.wait_for(self.released.wait(), timeout=0.25)
        yield sse(
            {
                "id": "chat_gated",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }
        ).encode()


class PartialTimeoutStream(httpx.AsyncByteStream):
    """发送一个可见 delta 后模拟网络读超时。"""

    async def __aiter__(self):
        """先产出内容事件，再抛出 HTTPX 超时异常。"""
        yield (
            b'data: {"id":"chat_partial","choices":['
            b'{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n'
        )
        raise httpx.ReadTimeout(
            "stream interrupted",
            request=httpx.Request("POST", "https://api.deepseek.com/chat/completions"),
        )


def simple_request() -> ModelRequest:
    """创建一个包含 DeepSeek reasoning 继续字段的固定请求。"""
    return ModelRequest(
        model="deepseek-v4-pro",
        messages=(
            ModelMessage(role="system", content="Be concise."),
            ModelMessage(
                role="assistant",
                content="",
                reasoning_content="previous reasoning",
            ),
            ModelMessage(role="user", content="hello"),
        ),
    )


class OpenAICompatibleProviderTest(unittest.IsolatedAsyncioTestCase):
    """验证 Provider 的可观察协议行为而不访问真实网络。"""

    async def _provider(
        self,
        handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> OpenAICompatibleProvider:
        """创建使用 HTTPX MockTransport 的 Provider，并注册异步清理。"""
        provider = OpenAICompatibleProvider(
            "https://api.deepseek.com",
            "secret-test-key",
            10,
            transport=httpx.MockTransport(handler),
            sleep=sleep,
        )
        self.addAsyncCleanup(provider.aclose)
        return provider

    async def test_sse_assembles_text_reasoning_tool_arguments_and_usage(self) -> None:
        """分片 SSE 必须按 index 合并为一个可继续执行的完整模型响应。"""
        observed_payload: dict[str, object] = {}
        observed_authorization = ""

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal observed_payload, observed_authorization
            observed_payload = json.loads(request.content)
            observed_authorization = request.headers["authorization"]
            return httpx.Response(
                200,
                headers={
                    "content-type": "text/event-stream",
                    "x-request-id": "req_header",
                },
                text=TOOL_SSE,
            )

        provider = await self._provider(respond)
        chunks: list[str] = []

        async def collect(text: str) -> None:
            chunks.append(text)

        response = await provider.complete(simple_request(), collect)

        self.assertEqual(chunks, ["读", "取完成"])
        self.assertEqual(response.content, "读取完成")
        self.assertEqual(response.reasoning_content, "need file")
        self.assertEqual(response.tool_calls[0].call_id, "call_1")
        self.assertEqual(
            response.tool_calls[0].arguments,
            {"path": "README.md", "limit": 20},
        )
        self.assertEqual(response.finish_reason, "tool_calls")
        self.assertEqual((response.input_tokens, response.output_tokens), (12, 7))
        self.assertEqual(response.provider_request_id, "req_header")
        self.assertEqual(observed_authorization, "Bearer secret-test-key")
        self.assertEqual(observed_payload["model"], "deepseek-v4-pro")
        self.assertEqual(observed_payload["stream"], True)
        self.assertEqual(observed_payload["stream_options"], {"include_usage": True})
        messages = observed_payload["messages"]
        self.assertIsInstance(messages, list)
        self.assertEqual(messages[1]["reasoning_content"], "previous reasoning")

    async def test_json_response_is_supported_without_stream_callback(self) -> None:
        """兼容端点忽略 stream 时仍应解析同一语义的非流式 JSON。"""
        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "chat_json",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"content": "json answer", "reasoning_content": "think"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3},
                },
            )

        provider = await self._provider(respond)

        response = await provider.complete(simple_request())

        self.assertEqual(response.content, "json answer")
        self.assertEqual(response.reasoning_content, "think")
        self.assertEqual(response.provider_request_id, "chat_json")

    async def test_text_callback_runs_before_stream_is_fully_read(self) -> None:
        """Provider 必须边读取边回调，不能先缓冲完整 SSE 再伪装流式输出。"""
        released = asyncio.Event()
        chunks: list[str] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=GatedSseStream(released),
            )

        async def collect(text: str) -> None:
            chunks.append(text)
            released.set()

        provider = await self._provider(respond)

        response = await asyncio.wait_for(provider.complete(simple_request(), collect), timeout=1)

        self.assertEqual(chunks, ["live"])
        self.assertEqual(response.content, "live")

    async def test_server_error_is_retried_once_then_succeeds(self) -> None:
        """首次 5xx 可恢复时只重试一次，并采用固定短退避。"""
        attempts = 0
        delays: list[float] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(503, text="temporary")
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text=SUCCESS_SSE,
            )

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        provider = await self._provider(respond, sleep=record_sleep)

        response = await provider.complete(simple_request())

        self.assertEqual(response.content, "ok")
        self.assertEqual(attempts, 2)
        self.assertEqual(delays, [0.5])

    async def test_second_server_error_stops_after_one_retry(self) -> None:
        """连续 5xx 不得无限重试，第二次失败应转换为稳定服务端错误。"""
        attempts = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500, text="secret-test-key echoed upstream")

        async def no_wait(delay: float) -> None:
            return None

        provider = await self._provider(respond, sleep=no_wait)

        with self.assertRaises(ProviderServerError) as caught:
            await provider.complete(simple_request())

        self.assertEqual(attempts, 2)
        self.assertNotIn("secret-test-key", str(caught.exception))

    async def test_authentication_error_is_not_retried_or_leaked(self) -> None:
        """401 必须立即停止，远端回显的 API Key 不得进入异常。"""
        attempts = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(401, text="invalid secret-test-key")

        provider = await self._provider(respond)

        with self.assertRaises(ProviderAuthenticationError) as caught:
            await provider.complete(simple_request())

        self.assertEqual(attempts, 1)
        self.assertNotIn("secret-test-key", str(caught.exception))

    async def test_rate_limit_uses_retry_after_then_raises(self) -> None:
        """429 应等待受限 Retry-After，但第二次仍失败时返回速率错误。"""
        delays: list[float] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "1.25"})

        async def record_sleep(delay: float) -> None:
            delays.append(delay)

        provider = await self._provider(respond, sleep=record_sleep)

        with self.assertRaises(ProviderRateLimitError):
            await provider.complete(simple_request())

        self.assertEqual(delays, [1.25])

    async def test_timeout_is_retried_once_without_leaking_detail(self) -> None:
        """网络超时只重试一次，底层异常内容不得越过 Provider 边界。"""
        attempts = 0

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("secret-test-key timeout", request=request)

        async def no_wait(delay: float) -> None:
            return None

        provider = await self._provider(respond, sleep=no_wait)

        with self.assertRaises(ProviderTimeoutError) as caught:
            await provider.complete(simple_request())

        self.assertEqual(attempts, 2)
        self.assertNotIn("secret-test-key", str(caught.exception))

    async def test_timeout_after_visible_delta_is_not_retried(self) -> None:
        """已经回调的流不能重试，否则 Channel 会收到重复前缀。"""
        attempts = 0
        chunks: list[str] = []

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=PartialTimeoutStream(),
            )

        async def collect(text: str) -> None:
            chunks.append(text)

        async def no_wait(delay: float) -> None:
            return None

        provider = await self._provider(respond, sleep=no_wait)

        with self.assertRaises(ProviderTimeoutError):
            await provider.complete(simple_request(), collect)

        self.assertEqual(attempts, 1)
        self.assertEqual(chunks, ["partial"])

    async def test_invalid_tool_arguments_are_protocol_error(self) -> None:
        """Tool arguments 不是 JSON object 时不能把动态字符串交给 AgentRunner。"""
        invalid = sse(
            {
                "id": "chat_invalid",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "function": {"name": "read_file", "arguments": "[1]"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        )

        async def respond(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=invalid)

        provider = await self._provider(respond)

        with self.assertRaises(ProviderProtocolError):
            await provider.complete(simple_request())


if __name__ == "__main__":
    unittest.main()
