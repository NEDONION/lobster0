"""exact-argv 命令规范化与硬禁止测试。"""

import sys
import tempfile
import unittest
from pathlib import Path

from lobster0.policy.command import (
    CommandPolicyError,
    command_rule_is_persistable,
    normalize_command,
)
from lobster0.policy.engine import PolicyAction, PolicyEngine
from lobster0.tools.base import ToolContext
from lobster0.tools.command import RunCommandTool


class CommandPolicyTest(unittest.TestCase):
    """验证命令不能退化为 Shell、远程、删除或提权入口。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.workspace = Path(self.temporary_directory.name).resolve()

    def test_shell_eval_delete_upload_privilege_and_git_push_are_hard_denied(self) -> None:
        """危险程序或参数组合必须硬拒绝，不能产生可批准动作。"""
        cases = (
            ("bash", ("-lc", "id")),
            (sys.executable, ("-c", "print(1)")),
            ("rm", ("target",)),
            ("ssh", ("host",)),
            ("sudo", ("id",)),
            ("git", ("push",)),
            ("git", ("clean", "-fd")),
            ("git", ("reset", "--hard")),
            ("git", ("config", "credential.helper", "store")),
            ("docker", ("ps",)),
            ("pip", ("install", "package")),
            ("systemctl", ("restart", "service")),
        )
        for program, args in cases:
            with self.subTest(program=program, args=args), self.assertRaises(
                CommandPolicyError
            ):
                normalize_command(program, args, self.workspace)

    def test_symlink_loop_executable_resolution_is_redacted(self) -> None:
        """异常 executable 路径只能变成稳定 not-found，不能冒出 resolve 异常。"""
        loop = self.workspace / "loop"
        loop.symlink_to("loop")

        with self.assertRaises(CommandPolicyError) as error:
            normalize_command(str(loop), (), self.workspace)

        self.assertEqual(error.exception.code, "command_not_found")

    def test_unknown_executable_and_control_characters_fail_closed(self) -> None:
        """找不到程序或 argv 含控制字符时不得猜测或交给 Shell。"""
        cases = (
            ("definitely-not-a-lobster0-command", ()),
            (sys.executable, ("bad\0arg",)),
            (sys.executable, ("bad\narg",)),
        )
        for program, args in cases:
            with self.subTest(program=program, args=args), self.assertRaises(
                CommandPolicyError
            ):
                normalize_command(program, args, self.workspace)

    def test_exact_argv_preserves_boundaries_and_does_not_match_extra_args(self) -> None:
        """参数边界、空参数和重复参数必须原样保留。"""
        allowed = normalize_command(sys.executable, ("script.py", "", "x", "x"), self.workspace)
        extra = normalize_command(
            sys.executable,
            ("script.py", "", "x", "x", "--extra"),
            self.workspace,
        )

        self.assertEqual(allowed.args, ("script.py", "", "x", "x"))
        self.assertNotEqual(allowed, extra)
        self.assertTrue(Path(allowed.resolved_program).is_absolute())

    def test_policy_allows_exact_rule_but_extra_argument_requires_approval(self) -> None:
        """allowlist 只能匹配 resolved executable 与完整 argv。"""
        rule = normalize_command(sys.executable, ("script.py",), self.workspace)
        engine = PolicyEngine(command_rules=(rule,))
        context = ToolContext(1, 1, 1, self.workspace / "state", self.workspace, ())
        definition = RunCommandTool().definition

        exact = engine.authorize(
            definition,
            context,
            {"program": sys.executable, "args": ["script.py"], "timeout_seconds": 30},
        )
        extra = engine.authorize(
            definition,
            context,
            {
                "program": sys.executable,
                "args": ["script.py", "--extra"],
                "timeout_seconds": 30,
            },
        )

        self.assertEqual(exact.action, PolicyAction.ALLOW)
        self.assertEqual(exact.normalized_arguments["program"], rule.resolved_program)
        self.assertEqual(extra.action, PolicyAction.REQUIRE_APPROVAL)

    def test_custom_executable_path_resolves_user_cli_for_policy(self) -> None:
        """Policy 与规则必须使用同一条发现 PATH 解析 NVM 等用户 CLI。"""
        executable_root = self.workspace / "nvm-bin"
        executable_root.mkdir()
        lark_cli = executable_root / "lark-cli"
        lark_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        lark_cli.chmod(0o700)
        executable_path = str(executable_root)
        rule = normalize_command(
            "lark-cli",
            ("--version",),
            self.workspace,
            executable_path=executable_path,
        )
        engine = PolicyEngine(
            command_rules=(rule,),
            executable_path=executable_path,
        )
        context = ToolContext(1, 1, 1, self.workspace / "state", self.workspace, ())

        exact = engine.authorize(
            RunCommandTool().definition,
            context,
            {
                "program": "lark-cli",
                "args": ["--version"],
                "timeout_seconds": 30,
            },
        )
        extra = engine.authorize(
            RunCommandTool().definition,
            context,
            {
                "program": "lark-cli",
                "args": ["--version", "--json"],
                "timeout_seconds": 30,
            },
        )

        self.assertEqual(exact.action, PolicyAction.ALLOW)
        self.assertEqual(exact.normalized_arguments["program"], str(lark_cli))
        self.assertEqual(extra.action, PolicyAction.REQUIRE_APPROVAL)

    def test_policy_security_and_ask_matrix_fail_closed(self) -> None:
        """deny/off/always/full 组合必须遵守显式配置而非泛化放行。"""
        context = ToolContext(1, 1, 1, self.workspace / "state", self.workspace, ())
        definition = RunCommandTool().definition
        arguments = {
            "program": sys.executable,
            "args": ["script.py"],
            "timeout_seconds": 30,
        }
        decisions = (
            (PolicyEngine(security="deny"), PolicyAction.DENY),
            (PolicyEngine(security="allowlist", ask="off"), PolicyAction.DENY),
            (PolicyEngine(security="allowlist", ask="on-miss"), PolicyAction.REQUIRE_APPROVAL),
            (PolicyEngine(security="full", ask="off"), PolicyAction.ALLOW),
            (PolicyEngine(security="full", ask="always"), PolicyAction.REQUIRE_APPROVAL),
        )

        for engine, expected in decisions:
            with self.subTest(expected=expected):
                self.assertEqual(
                    engine.authorize(definition, context, arguments).action,
                    expected,
                )

    def test_persistent_command_rule_rejects_inline_applescript(self) -> None:
        """Always 规则不能把动态 AppleScript 正文变成永久执行能力。"""
        osascript = self.workspace / "osascript"
        osascript.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        osascript.chmod(0o700)
        inline = normalize_command(
            str(osascript),
            ("-e", 'tell application "Notes" to make new note'),
            self.workspace,
        )
        safe = normalize_command(sys.executable, ("script.py",), self.workspace)

        self.assertFalse(command_rule_is_persistable(inline))
        self.assertTrue(command_rule_is_persistable(safe))

if __name__ == "__main__":
    unittest.main()
