"""``miniclaw approvals`` 的离线端到端测试。"""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniclaw.cli import main
from miniclaw.providers.base import ModelResponse, ToolCall
from tests.fakes.fake_provider import FakeProvider


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    """运行真实 CLI main，并收集退出码与输出。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = main(arguments)
    return code, stdout.getvalue(), stderr.getvalue()


class CliApprovalTest(unittest.TestCase):
    """验证审批可查询、跨进程续执行并且拒绝不产生副作用。"""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / "state"
        code, _, error = run_cli(["init", "--home", str(self.home)])
        self.assertEqual((code, error), (0, ""))

    def make_waiting(self, filename: str) -> int:
        """通过真实 chat 装配创建一个 write_file pending Approval。"""
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "write-1",
                            "write_file",
                            {"path": filename, "content": "approved"},
                        ),
                    ),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=4,
                    output_tokens=2,
                    provider_request_id="req-write",
                ),
            )
        )
        provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch("miniclaw.cli.OpenAICompatibleProvider", return_value=provider),
        ):
            code, output, error = run_cli(
                ["chat", "--home", str(self.home), "--message", "写文件"]
            )
        self.assertEqual((code, error), (0, ""))
        self.assertIn("Approval", output)
        with contextlib.closing(sqlite3.connect(self.home / "miniclaw.db")) as connection:
            return connection.execute("SELECT id FROM approvals").fetchone()[0]

    def test_list_and_show_need_no_model_key_and_support_json(self) -> None:
        """只读审批命令不得要求或读取模型 Key。"""
        approval_id = self.make_waiting("listed.txt")

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("miniclaw.cli.OpenAICompatibleProvider") as provider,
        ):
            list_code, list_output, list_error = run_cli(
                [
                    "approvals",
                    "--home",
                    str(self.home),
                    "list",
                    "--status",
                    "pending",
                    "--json",
                ]
            )
            show_code, show_output, show_error = run_cli(
                ["approvals", "--home", str(self.home), "show", str(approval_id)]
            )

        payload = json.loads(list_output)
        self.assertEqual((list_code, list_error, show_code, show_error), (0, "", 0, ""))
        self.assertEqual(payload[0]["id"], approval_id)
        self.assertEqual(payload[0]["status"], "pending")
        self.assertIn("write_file", show_output)
        self.assertNotIn("approved", show_output)
        provider.assert_not_called()

    def test_approve_executes_once_continues_model_and_replay_fails(self) -> None:
        """CLI approve 应完成写入和 child Turn；重复批准返回业务冲突。"""
        approval_id = self.make_waiting("approved.txt")
        provider = FakeProvider(
            (
                ModelResponse(
                    content="写入完成",
                    tool_calls=(),
                    reasoning_content=None,
                    finish_reason="stop",
                    input_tokens=6,
                    output_tokens=2,
                    provider_request_id="req-finish",
                ),
            )
        )
        provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch("miniclaw.cli.OpenAICompatibleProvider", return_value=provider),
        ):
            code, output, error = run_cli(
                ["approvals", "--home", str(self.home), "approve", str(approval_id)]
            )
            replay_code, _, replay_error = run_cli(
                ["approvals", "--home", str(self.home), "approve", str(approval_id)]
            )

        self.assertEqual((code, output, error), (0, "写入完成\n", ""))
        self.assertEqual(replay_code, 2)
        self.assertIn("not pending", replay_error)
        self.assertEqual(
            (self.home / "workspace" / "approved.txt").read_text(encoding="utf-8"),
            "approved",
        )
        with contextlib.closing(sqlite3.connect(self.home / "miniclaw.db")) as connection:
            child = connection.execute(
                "SELECT parent_turn_id, status FROM turns ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(child, (1, "completed"))

    def test_deny_continues_model_but_never_writes(self) -> None:
        """CLI deny 只回传拒绝 Tool Result，不执行原工具。"""
        approval_id = self.make_waiting("denied.txt")
        provider = FakeProvider(
            (
                ModelResponse(
                    content="已取消",
                    tool_calls=(),
                    reasoning_content=None,
                    finish_reason="stop",
                    input_tokens=5,
                    output_tokens=1,
                    provider_request_id="req-deny",
                ),
            )
        )
        provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch("miniclaw.cli.OpenAICompatibleProvider", return_value=provider),
        ):
            code, output, error = run_cli(
                ["approvals", "--home", str(self.home), "deny", str(approval_id)]
            )

        self.assertEqual((code, output, error), (0, "已取消\n", ""))
        self.assertFalse((self.home / "workspace" / "denied.txt").exists())
        tool_message = provider.requests[0].messages[-1]
        self.assertEqual(json.loads(tool_message.content)["error"]["code"], "approval_denied")

    def test_command_approve_always_persists_exact_rule_and_auto_allows_same_argv(self) -> None:
        """命令永久放行只匹配同一 resolved executable 与完整 argv。"""
        helper = self.home / "workspace" / "command.py"
        helper.write_text("print('COMMAND-MARKER')\n", encoding="utf-8")
        call = ToolCall(
            "command-1",
            "run_command",
            {"program": sys.executable, "args": [str(helper)]},
        )
        waiting_provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(call,),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=3,
                    output_tokens=1,
                    provider_request_id="req-command",
                ),
            )
        )
        waiting_provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch(
                "miniclaw.cli.OpenAICompatibleProvider",
                return_value=waiting_provider,
            ),
        ):
            waiting_code, _, waiting_error = run_cli(
                ["chat", "--home", str(self.home), "--message", "运行命令"]
            )
        self.assertEqual((waiting_code, waiting_error), (0, ""))
        with contextlib.closing(sqlite3.connect(self.home / "miniclaw.db")) as connection:
            approval_id = connection.execute("SELECT id FROM approvals").fetchone()[0]
        show_code, show_output, show_error = run_cli(
            ["approvals", "--home", str(self.home), "show", str(approval_id)]
        )
        self.assertEqual((show_code, show_error), (0, ""))
        self.assertIn(str(helper), show_output)
        self.assertIn(Path(sys.executable).name, show_output)

        finish_provider = FakeProvider(
            (
                ModelResponse(
                    content="命令完成",
                    tool_calls=(),
                    reasoning_content=None,
                    finish_reason="stop",
                    input_tokens=5,
                    output_tokens=1,
                    provider_request_id="req-command-finish",
                ),
            )
        )
        finish_provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch(
                "miniclaw.cli.OpenAICompatibleProvider",
                return_value=finish_provider,
            ),
        ):
            approve_code, approve_output, approve_error = run_cli(
                [
                    "approvals",
                    "--home",
                    str(self.home),
                    "approve",
                    str(approval_id),
                    "--always",
                ]
            )

        self.assertEqual((approve_code, approve_output, approve_error), (0, "命令完成\n", ""))
        approved_payload = json.loads(finish_provider.requests[0].messages[-1].content)
        self.assertIn("COMMAND-MARKER", approved_payload["data"]["stdout"])
        with contextlib.closing(sqlite3.connect(self.home / "miniclaw.db")) as connection:
            rule = json.loads(
                connection.execute("SELECT rule_json FROM policy_rules").fetchone()[0]
            )
        self.assertEqual(rule["type"], "exact_argv")
        self.assertEqual(rule["args"], [str(helper)])
        self.assertTrue(Path(rule["resolved_program"]).is_absolute())

        repeat_provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(ToolCall("command-2", "run_command", call.arguments),),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=3,
                    output_tokens=1,
                    provider_request_id="req-command-repeat",
                ),
                ModelResponse(
                    content="自动完成",
                    tool_calls=(),
                    reasoning_content=None,
                    finish_reason="stop",
                    input_tokens=4,
                    output_tokens=1,
                    provider_request_id="req-command-repeat-finish",
                ),
            )
        )
        repeat_provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch(
                "miniclaw.cli.OpenAICompatibleProvider",
                return_value=repeat_provider,
            ),
        ):
            repeat_code, repeat_output, repeat_error = run_cli(
                ["chat", "--home", str(self.home), "--message", "再运行一次"]
            )

        self.assertEqual((repeat_code, repeat_output, repeat_error), (0, "自动完成\n", ""))
        with contextlib.closing(sqlite3.connect(self.home / "miniclaw.db")) as connection:
            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM approvals), "
                "(SELECT COUNT(*) FROM tool_runs WHERE status = 'succeeded')"
            ).fetchone()
        self.assertEqual(counts, (1, 2))

    def test_forbidden_shell_is_denied_without_creating_approval_or_tool_run(self) -> None:
        """硬禁止命令必须直接审计拒绝，Owner 也不能 approve。"""
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            "shell-1",
                            "run_command",
                            {"program": "bash", "args": ["-lc", "id"]},
                        ),
                    ),
                    reasoning_content=None,
                    finish_reason="tool_calls",
                    input_tokens=3,
                    output_tokens=1,
                    provider_request_id="req-shell",
                ),
                ModelResponse(
                    content="该命令被安全策略拒绝",
                    tool_calls=(),
                    reasoning_content=None,
                    finish_reason="stop",
                    input_tokens=4,
                    output_tokens=2,
                    provider_request_id="req-shell-finish",
                ),
            )
        )
        provider.aclose = mock.AsyncMock()  # type: ignore[attr-defined]
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch("miniclaw.cli.OpenAICompatibleProvider", return_value=provider),
        ):
            code, output, error = run_cli(
                ["chat", "--home", str(self.home), "--message", "运行 shell"]
            )

        self.assertEqual((code, output, error), (0, "该命令被安全策略拒绝\n", ""))
        denied = json.loads(provider.requests[1].messages[-1].content)
        self.assertEqual(denied["error"]["code"], "command_forbidden")
        with contextlib.closing(sqlite3.connect(self.home / "miniclaw.db")) as connection:
            counts = connection.execute(
                "SELECT (SELECT COUNT(*) FROM approvals), "
                "(SELECT COUNT(*) FROM tool_runs), "
                "(SELECT COUNT(*) FROM audit_events WHERE event_type = 'tool.denied')"
            ).fetchone()
        self.assertEqual(counts, (0, 0, 1))

    def test_write_file_cannot_create_always_rule(self) -> None:
        """文件审批只能 allow-once，不能扩大成永久写权限。"""
        approval_id = self.make_waiting("once.txt")
        with (
            mock.patch.dict(os.environ, {"MINICLAW_MODEL_API_KEY": "offline"}, clear=True),
            mock.patch("miniclaw.cli.OpenAICompatibleProvider") as provider,
        ):
            code, output, error = run_cli(
                [
                    "approvals",
                    "--home",
                    str(self.home),
                    "approve",
                    str(approval_id),
                    "--always",
                ]
            )

        self.assertEqual((code, output), (2, ""))
        self.assertIn("only supports exact command", error)
        provider.assert_not_called()


if __name__ == "__main__":
    unittest.main()
