"""Browser Tool 风险分级与公网导航 Policy 回归。"""

import unittest
from pathlib import Path

from miniclaw.browser.policy import classify_browser_action
from miniclaw.policy.engine import PolicyAction, PolicyEngine
from miniclaw.tools.base import ToolContext, ToolRisk
from miniclaw.tools.browser import browser_tools


class _Client:
    """提供不执行请求的最小 BrowserClient fake。"""

    async def request(self, action: object) -> dict[str, object]:
        """测试若意外执行 Worker 请求则立即失败。"""
        raise AssertionError(action)


class BrowserPolicyTest(unittest.TestCase):
    """验证 Browser 动作沿用 Core Policy 且敏感输入 fail closed。"""

    def setUp(self) -> None:
        """创建确定性公网 DNS、Tool 定义和可信 Context。"""
        root = Path.cwd().resolve()
        self.context = ToolContext(1, 2, 3, root / ".state", root, ())
        self.tools = {tool.definition.name: tool for tool in browser_tools(_Client())}
        self.policy = PolicyEngine(
            network_resolver=lambda hostname, port: ("93.184.216.34",)
        )

    def test_read_actions_are_low_risk_but_click_and_enter_are_high(self) -> None:
        """只读动作自动放行，未知点击和可能提交的 Enter 必须审批。"""
        for name in (
            "browser_open",
            "browser_snapshot",
            "browser_type",
            "browser_scroll",
            "browser_screenshot",
            "browser_close",
        ):
            with self.subTest(name=name):
                self.assertEqual(classify_browser_action(name, {}).risk, ToolRisk.LOW)
        self.assertEqual(
            classify_browser_action("browser_click", {}).risk,
            ToolRisk.HIGH,
        )
        self.assertEqual(
            classify_browser_action("browser_press", {"key": "Enter"}).risk,
            ToolRisk.HIGH,
        )

    def test_password_and_otp_inputs_are_hard_denied(self) -> None:
        """密码与 OTP 不能通过 Autopilot 或人工 Approval 绕过。"""
        tool = self.tools["browser_type"]
        for input_kind in ("password", "one-time-code", "otp"):
            arguments = tool.validate(
                {
                    "origin": "https://example.com",
                    "generation": "generation-1",
                    "ref": "@e1",
                    "role": "textbox",
                    "input_kind": input_kind,
                    "text": "private-value",
                }
            )
            decision = self.policy.authorize(tool.definition, self.context, arguments)
            with self.subTest(input_kind=input_kind):
                self.assertEqual(decision.action, PolicyAction.DENY)
                self.assertEqual(decision.error_code, "browser_sensitive_input")

    def test_open_normalizes_public_https_and_denies_localhost(self) -> None:
        """Browser 导航必须复用 HTTPS、DNS 和 SSRF 硬边界。"""
        tool = self.tools["browser_open"]
        public = self.policy.authorize(
            tool.definition,
            self.context,
            tool.validate({"url": "https://EXAMPLE.com/path?q=1"}),
        )
        local = PolicyEngine(
            network_resolver=lambda hostname, port: ("127.0.0.1",)
        ).authorize(
            tool.definition,
            self.context,
            tool.validate({"url": "https://localhost/private"}),
        )

        self.assertEqual(public.action, PolicyAction.ALLOW)
        self.assertEqual(
            public.normalized_arguments,
            {"url": "https://example.com/path?q=1"},
        )
        self.assertEqual(local.action, PolicyAction.DENY)
        self.assertEqual(local.error_code, "non_public_address")


if __name__ == "__main__":
    unittest.main()
