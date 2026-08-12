"""HTTPS URL、DNS 与 SSRF 硬禁止测试。"""

import ipaddress
import unittest
from pathlib import Path

from lobster0.policy.engine import PolicyAction, PolicyEngine
from lobster0.policy.network import (
    NetworkPolicyError,
    NetworkRule,
    normalize_network_rule,
    validate_https_target,
)
from lobster0.tools.base import ToolContext
from lobster0.tools.web import HttpGetTool


class NetworkPolicyTest(unittest.TestCase):
    """验证所有解析地址都必须是明确公网地址。"""

    def resolver(self, hostname: str, port: int) -> tuple[str, ...]:
        """返回测试域名对应的确定性地址集合。"""
        answers = {
            "example.com": ("93.184.216.34",),
            "mixed.example": ("93.184.216.34", "10.0.0.8"),
            "private.example": ("192.168.1.5",),
            "malformed.example": ("not-an-ip",),
        }
        return answers.get(hostname, ("93.184.216.34",))

    def test_non_https_credentials_and_non_public_addresses_are_denied(self) -> None:
        """scheme、凭据和所有特殊 IP 类别都不能进入 Approval。"""
        urls = (
            "http://example.com",
            "https://user:pass@example.com",
            "https://127.0.0.1",
            "https://[::1]",
            "https://10.0.0.1",
            "https://172.16.0.1",
            "https://192.168.1.1",
            "https://169.254.169.254",
            "https://224.0.0.1",
            "https://0.0.0.0",
            "https://192.0.2.1",
            "https://private.example",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(NetworkPolicyError):
                validate_https_target(url, self.resolver)

    def test_mixed_dns_answers_fail_instead_of_selecting_only_public_ip(self) -> None:
        """一个私网答案就必须拒绝整个目标，不能挑公网答案蒙混过关。"""
        with self.assertRaises(NetworkPolicyError) as error:
            validate_https_target("https://mixed.example/path", self.resolver)

        self.assertEqual(error.exception.code, "non_public_address")

    def test_ambiguous_hostname_control_fragment_and_unapproved_port_are_denied(self) -> None:
        """拒绝歧义 host 编码、控制字符、fragment 和未精确授权端口。"""
        urls = (
            "https://127.1",
            "https://2130706433",
            "https://example.com.",
            "https://éxample.com",
            "https://example.com/%0aheader",
            "https://example.com/path#fragment",
            "https://example.com:8443/path",
        )
        for url in urls:
            with self.subTest(url=url), self.assertRaises(NetworkPolicyError):
                validate_https_target(url, self.resolver)

    def test_public_target_is_canonical_and_non_443_needs_exact_rule(self) -> None:
        """合法目标保留 path/query；显式规则才能打开同 authority 非 443 端口。"""
        target = validate_https_target(
            "https://EXAMPLE.com/a%20b?q=1",
            self.resolver,
        )
        port_rule = normalize_network_rule("example.com:8443")
        non_default = validate_https_target(
            "https://example.com:8443/path",
            self.resolver,
            allowed_ports=(443, port_rule.port),
        )

        self.assertEqual(target.url, "https://example.com/a%20b?q=1")
        self.assertEqual(target.hostname, "example.com")
        self.assertEqual(target.port, 443)
        self.assertEqual(target.addresses, ("93.184.216.34",))
        self.assertEqual(target.request_target, "/a%20b?q=1")
        self.assertEqual((port_rule.hostname, port_rule.port), ("example.com", 8443))
        self.assertEqual(non_default.port, 8443)

    def test_empty_dns_and_malformed_answers_fail_closed(self) -> None:
        """DNS 无答案、异常或非 IP 文本都必须返回稳定错误。"""
        for resolver in (
            lambda hostname, port: (),
            self.resolver,
            lambda hostname, port: (_ for _ in ()).throw(OSError("secret DNS detail")),
        ):
            url = (
                "https://malformed.example"
                if resolver == self.resolver
                else "https://empty.example"
            )
            with self.subTest(url=url), self.assertRaises(NetworkPolicyError) as error:
                validate_https_target(url, resolver)
            self.assertNotIn("secret DNS detail", str(error.exception))

    def test_http_policy_uses_exact_authority_and_security_ask_matrix(self) -> None:
        """HTTPS 公网目标使用与 command 一致的 security × ask 语义。"""
        workspace = Path.cwd().resolve()
        context = ToolContext(1, 1, 1, workspace / ".state", workspace, ())
        definition = HttpGetTool().definition
        arguments = {"url": "https://EXAMPLE.com/a?q=secret", "timeout_seconds": 20}
        cases = (
            (
                PolicyEngine(
                    network_rules=(NetworkRule("example.com"),),
                    network_resolver=self.resolver,
                ),
                PolicyAction.ALLOW,
            ),
            (
                PolicyEngine(
                    ask="always",
                    network_rules=(NetworkRule("example.com"),),
                    network_resolver=self.resolver,
                ),
                PolicyAction.REQUIRE_APPROVAL,
            ),
            (
                PolicyEngine(ask="off", network_resolver=self.resolver),
                PolicyAction.DENY,
            ),
            (
                PolicyEngine(security="full", ask="off", network_resolver=self.resolver),
                PolicyAction.ALLOW,
            ),
            (
                PolicyEngine(security="deny", network_resolver=self.resolver),
                PolicyAction.DENY,
            ),
        )

        for engine, expected in cases:
            with self.subTest(expected=expected):
                decision = engine.authorize(definition, context, arguments)
                self.assertEqual(decision.action, expected)
                self.assertEqual(
                    decision.normalized_arguments,
                    {"url": "https://example.com/a?q=secret", "timeout_seconds": 20},
                )

    def test_http_policy_hard_denies_private_target_before_approval(self) -> None:
        """SSRF 目标不论 full/always 都不能变成 Owner 可批准请求。"""
        workspace = Path.cwd().resolve()
        context = ToolContext(1, 1, 1, workspace / ".state", workspace, ())
        decision = PolicyEngine(
            security="full",
            ask="always",
            network_resolver=self.resolver,
        ).authorize(
            HttpGetTool().definition,
            context,
            {"url": "https://private.example/secret", "timeout_seconds": 20},
        )

        self.assertEqual(decision.action, PolicyAction.DENY)
        self.assertEqual(decision.error_code, "non_public_address")


if __name__ == "__main__":
    unittest.main()


class ProxyFakeIpTest(unittest.TestCase):
    """fake-IP 代理环境下的显式豁免。"""

    def test_fake_ip_range_is_refused_by_default(self) -> None:
        """默认必须保持严格：不声明就不放行。"""
        with self.assertRaises(NetworkPolicyError) as raised:
            validate_https_target(
                "https://api.github.com/x", resolver=lambda h, p: ("198.18.0.56",)
            )

        self.assertEqual(raised.exception.code, "non_public_address")

    def test_explicitly_allowed_cidr_passes(self) -> None:
        """用户显式声明代理网段后放行——这是他自己的网络事实。"""
        target = validate_https_target(
            "https://api.github.com/x",
            resolver=lambda h, p: ("198.18.0.56",),
            allow_cidrs=(ipaddress.ip_network("198.18.0.0/15"),),
        )

        self.assertEqual(target.hostname, "api.github.com")

    def test_allowlist_cannot_open_loopback_or_metadata(self) -> None:
        """豁免只对声明的网段生效，回环与云元数据永远拒绝。

        否则一条 0.0.0.0/0 就能把整道 SSRF 防护关掉。
        """
        for address, cidr in (
            ("127.0.0.1", "127.0.0.0/8"),
            ("169.254.169.254", "169.254.0.0/16"),
            ("10.0.0.5", "0.0.0.0/0"),
        ):
            with self.assertRaises(NetworkPolicyError) as raised:
                validate_https_target(
                    "https://evil.example/x",
                    resolver=lambda h, p, value=address: (value,),
                    allow_cidrs=(ipaddress.ip_network(cidr),),
                )
            self.assertEqual(raised.exception.code, "non_public_address", address)


class TrustedCidrWiringTest(unittest.TestCase):
    """配置必须真的传到调用点——只落在 config 上等于没做。"""

    def test_http_get_tool_honours_trusted_cidrs(self) -> None:
        """HttpGetTool 拿到网段后，fake-IP 地址不再被拒。"""
        from lobster0.tools.web import HttpGetTool

        tool = HttpGetTool(
            resolver=lambda hostname, port: ("198.18.0.56",),
            trusted_cidrs=(ipaddress.ip_network("198.18.0.0/15"),),
        )

        # 不真正发请求，只验证目标校验这一步不再拒绝。
        target = validate_https_target(
            "https://api.github.com/x",
            resolver=lambda h, p: ("198.18.0.56",),
            allow_cidrs=tool._trusted_cidrs,
        )
        self.assertEqual(target.hostname, "api.github.com")

    def test_policy_engine_honours_trusted_cidrs(self) -> None:
        """PolicyEngine 的 http_get 判定同样要带上网段。"""
        from lobster0.policy.engine import PolicyEngine

        engine = PolicyEngine(
            network_resolver=lambda hostname, port: ("198.18.0.56",),
            trusted_cidrs=(ipaddress.ip_network("198.18.0.0/15"),),
        )

        self.assertEqual(
            engine._trusted_cidrs, (ipaddress.ip_network("198.18.0.0/15"),)
        )
