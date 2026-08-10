"""模型 Provider 公共数据与错误契约测试。"""

import unittest
from dataclasses import FrozenInstanceError

from lobster0.providers.base import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderAuthenticationError,
    ProviderError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderServerError,
    ProviderTimeoutError,
    ToolCall,
)


class ProviderContractTest(unittest.TestCase):
    """验证 Agent 与具体 HTTP 实现之间不会漂移的值语义。"""

    def test_reasoning_and_tool_arguments_survive_request_continuation(self) -> None:
        """工具调用后的继续请求必须保留 DeepSeek reasoning 与结构化参数。"""
        call = ToolCall("call_1", "read_file", {"path": "README.md", "limit": 20})
        assistant = ModelMessage(
            role="assistant",
            content="",
            tool_calls=(call,),
            reasoning_content="need the file before answering",
        )
        request = ModelRequest("deepseek-v4-pro", (assistant,))

        self.assertEqual(request.messages[0].tool_calls[0].arguments["path"], "README.md")
        self.assertEqual(
            request.messages[0].reasoning_content,
            "need the file before answering",
        )

    def test_contract_values_are_immutable(self) -> None:
        """一次请求交给 Provider 后不得被并发回调原地替换字段。"""
        response = ModelResponse(
            content="done",
            tool_calls=(),
            reasoning_content=None,
            finish_reason="stop",
            input_tokens=12,
            output_tokens=3,
            provider_request_id="req_1",
        )

        with self.assertRaises(FrozenInstanceError):
            response.content = "mutated"  # type: ignore[misc]

    def test_specific_provider_failures_share_one_public_base(self) -> None:
        """Turn 层应能统一处理 Provider 失败，同时 CLI 仍可映射精确退出码。"""
        failures = (
            ProviderAuthenticationError("auth"),
            ProviderRateLimitError("rate"),
            ProviderTimeoutError("timeout"),
            ProviderProtocolError("protocol"),
            ProviderServerError("server"),
        )

        self.assertTrue(all(isinstance(error, ProviderError) for error in failures))
        self.assertEqual(len({type(error) for error in failures}), 5)


if __name__ == "__main__":
    unittest.main()
