"""HttpGetTool pinned peer、重定向与响应预算测试。"""

import io
import unittest
from pathlib import Path
from unittest import mock

from lobster0.policy.network import NetworkRule, validate_https_target
from lobster0.tools.base import ToolContext, ToolValidationError
from lobster0.tools.web import HttpGetTool, PinnedHTTPSConnection


class FakeResponse:
    """提供 http.client response 使用的最小接口。"""

    def __init__(
        self,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self._body = io.BytesIO(body)
        self._headers = {
            key.casefold(): value for key, value in (headers or {}).items()
        }

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name.casefold(), default)

    def read(self, amount: int | None = None) -> bytes:
        return self._body.read(amount)


class FakeConnection:
    """记录请求并返回一个预设响应。"""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.requests: list[tuple[str, str, object, dict[str, str]]] = []
        self.closed = False

    def request(
        self,
        method: str,
        target: str,
        body: object = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests.append((method, target, body, headers or {}))

    def getresponse(self) -> FakeResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


class FakeConnectionFactory:
    """按顺序创建连接，并记录每次重新验证后的 NetworkTarget。"""

    def __init__(self, responses: tuple[FakeResponse, ...]) -> None:
        self.responses = list(responses)
        self.targets = []
        self.connections: list[FakeConnection] = []

    def __call__(self, target: object, timeout: float) -> FakeConnection:
        self.targets.append(target)
        connection = FakeConnection(self.responses.pop(0))
        self.connections.append(connection)
        return connection


class HttpGetToolTest(unittest.IsolatedAsyncioTestCase):
    """验证 GET 不会继承首次 host 信任或读取无界二进制。"""

    def setUp(self) -> None:
        self.workspace = Path.cwd().resolve()
        self.context = ToolContext(1, 1, 1, self.workspace / ".state", self.workspace, ())
        self.resolve_calls: list[tuple[str, int]] = []

    def resolver(self, hostname: str, port: int) -> tuple[str, ...]:
        self.resolve_calls.append((hostname, port))
        answers = {
            "example.com": ("93.184.216.34",),
            "other.example": ("1.1.1.1",),
            "private.example": ("10.0.0.1",),
        }
        return answers[hostname]

    def test_pinned_connection_uses_validated_ip_and_original_tls_hostname(self) -> None:
        """TCP 连接必须使用已验证 IP，TLS SNI/证书名仍是原 hostname。"""
        target = validate_https_target("https://example.com/path", self.resolver)
        tls_context = mock.Mock()
        raw_socket = object()
        tls_socket = object()
        tls_context.wrap_socket.return_value = tls_socket
        connection = PinnedHTTPSConnection(target, 5, context=tls_context)

        with mock.patch(
            "lobster0.tools.web.socket.create_connection",
            return_value=raw_socket,
        ) as create:
            connection.connect()

        create.assert_called_once_with(("93.184.216.34", 443), 5, None)
        tls_context.wrap_socket.assert_called_once_with(
            raw_socket,
            server_hostname="example.com",
        )
        self.assertIs(connection.sock, tls_socket)

    async def test_request_sends_a_user_agent(self) -> None:
        """实测 GitHub API 对无 User-Agent 的请求直接返回 403。

        这条不是理论洁癖：修好网络层之后请求真的到达了 GitHub，却因为缺这一个
        头被拒，等于功能仍然不可用。
        """
        factory = FakeConnectionFactory(
            (FakeResponse(200, b"ok", {"Content-Type": "text/plain"}),)
        )
        tool = HttpGetTool(resolver=self.resolver, connection_factory=factory)

        await tool.execute(self.context, tool.validate({"url": "https://example.com/"}))

        headers = factory.connections[0].requests[0][3]
        agent = next(
            (value for key, value in headers.items() if key.casefold() == "user-agent"),
            None,
        )
        self.assertIsNotNone(agent, "http_get 必须发送 User-Agent")
        self.assertIn("Lobster0", agent)

    async def test_returns_untrusted_text_with_get_only_and_no_auth_headers(self) -> None:
        """合法文本响应必须标为 untrusted，且请求没有 body/认证 Header。"""
        factory = FakeConnectionFactory(
            (
                FakeResponse(
                    200,
                    "你好".encode(),
                    {"Content-Type": "text/plain; charset=utf-8"},
                ),
            )
        )
        tool = HttpGetTool(resolver=self.resolver, connection_factory=factory)

        result = await tool.execute(
            self.context,
            tool.validate({"url": "https://example.com/a?q=1"}),
        )

        self.assertTrue(result.ok)
        assert isinstance(result.data, dict)
        self.assertIs(result.data["untrusted"], True)
        self.assertEqual(result.data["text"], "你好")
        method, target, body, headers = factory.connections[0].requests[0]
        self.assertEqual((method, target, body), ("GET", "/a?q=1", None))
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Cookie", headers)
        self.assertIs(factory.connections[0].closed, True)

    async def test_each_redirect_is_revalidated_and_cross_host_needs_exact_rule(self) -> None:
        """同 host redirect 重查 DNS；跨 host 只有精确规则才可继承执行。"""
        factory = FakeConnectionFactory(
            (
                FakeResponse(302, headers={"Location": "/next"}),
                FakeResponse(302, headers={"Location": "https://other.example/final"}),
                FakeResponse(200, b"done", {"Content-Type": "text/plain"}),
            )
        )
        tool = HttpGetTool(
            resolver=self.resolver,
            connection_factory=factory,
            allow_rules=(NetworkRule("other.example"),),
        )

        result = await tool.execute(
            self.context,
            tool.validate({"url": "https://example.com/start"}),
        )

        self.assertTrue(result.ok)
        self.assertEqual(
            self.resolve_calls,
            [("example.com", 443), ("example.com", 443), ("other.example", 443)],
        )
        self.assertEqual(factory.targets[-1].addresses, ("1.1.1.1",))

    async def test_redirect_to_private_or_unapproved_public_host_fails(self) -> None:
        """重定向不能把首次审批扩大到私网或另一个未允许公网 host。"""
        cases = (
            ("https://private.example/secret", "non_public_address"),
            ("https://other.example/path", "redirect_not_allowed"),
        )
        for location, code in cases:
            with self.subTest(location=location):
                factory = FakeConnectionFactory(
                    (FakeResponse(302, headers={"Location": location}),)
                )
                tool = HttpGetTool(
                    resolver=self.resolver,
                    connection_factory=factory,
                )
                result = await tool.execute(
                    self.context,
                    tool.validate({"url": "https://example.com/start"}),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)
                self.assertEqual(len(factory.connections), 1)

    async def test_fourth_redirect_is_rejected(self) -> None:
        """最多跟随三次；看到第四个 redirect 时稳定失败。"""
        factory = FakeConnectionFactory(
            tuple(
                FakeResponse(302, headers={"Location": f"/step-{index}"})
                for index in range(4)
            )
        )
        tool = HttpGetTool(resolver=self.resolver, connection_factory=factory)

        result = await tool.execute(
            self.context,
            tool.validate({"url": "https://example.com/start"}),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "too_many_redirects")
        self.assertEqual(len(factory.connections), 4)

    async def test_response_size_binary_encoding_and_invalid_utf8_fail(self) -> None:
        """超预算、二进制、压缩和非法文本不能进入模型。"""
        responses = (
            (FakeResponse(200, b"x" * 11, {"Content-Type": "text/plain"}), "response_too_large"),
            (FakeResponse(200, b"png", {"Content-Type": "image/png"}), "unsupported_content_type"),
            (
                FakeResponse(
                    200,
                    b"compressed",
                    {"Content-Type": "text/plain", "Content-Encoding": "gzip"},
                ),
                "unsupported_content_encoding",
            ),
            (FakeResponse(200, b"\xff", {"Content-Type": "text/plain"}), "invalid_text"),
        )
        for response, code in responses:
            with self.subTest(code=code):
                factory = FakeConnectionFactory((response,))
                tool = HttpGetTool(
                    resolver=self.resolver,
                    connection_factory=factory,
                    max_response_bytes=10,
                )
                result = await tool.execute(
                    self.context,
                    tool.validate({"url": "https://example.com"}),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, code)

    def test_schema_has_no_body_headers_or_method_and_budgets_are_bounded(self) -> None:
        """模型只能提供 URL 和受限 timeout，不能注入认证请求。"""
        tool = HttpGetTool(timeout_seconds=20, max_response_bytes=2 * 1024 * 1024)
        properties = tool.definition.parameters["properties"]
        self.assertEqual(set(properties), {"url", "timeout_seconds"})
        for arguments in (
            {"url": "https://example.com", "headers": {"Authorization": "x"}},
            {"url": "https://example.com", "body": "x"},
            {"url": "https://example.com", "method": "POST"},
            {"url": "https://example.com", "timeout_seconds": 121},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ToolValidationError):
                tool.validate(arguments)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
