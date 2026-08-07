"""``miniclaw chat`` 的离线端到端行为测试。"""

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.cli import main  # noqa: E402


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    """调用真实 CLI main，并返回退出码、标准输出和标准错误。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        exit_code = main(arguments)
    return exit_code, stdout.getvalue(), stderr.getvalue()


@contextlib.contextmanager
def change_directory(path: Path) -> Iterator[None]:
    """临时切换当前目录，退出后恢复原目录。"""
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class _ModelHandler(BaseHTTPRequestHandler):
    """返回固定 SSE Completion，且只记录是否携带认证而不保存凭据。"""

    server_version = "MiniClawTestModel/1"

    def do_POST(self) -> None:  # noqa: N802
        """校验请求边界，并发送当前测试指定的兼容响应。"""
        server = self.server
        assert isinstance(server, _ModelServer)
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        observation = {
            "path": self.path,
            "authorized": self.headers.get("Authorization", "").startswith("Bearer "),
            "model": payload.get("model"),
            "stream": payload.get("stream"),
            "tools": payload.get("tools", []),
            "messages": payload.get("messages", []),
        }
        server.observation = observation
        server.observations.append(observation)
        if server.status != 200:
            self.send_response(server.status)
            self.end_headers()
            return

        if server.tool_mode and len(server.observations) == 1:
            body = (
                b'data: {"id":"offline-tool","choices":[{"index":0,"delta":'
                b'{"tool_calls":[{"index":0,"id":"call_system","type":"function",'
                b'"function":{"name":"system_info","arguments":"{}"}}]},'
                b'"finish_reason":"tool_calls"}],"usage":'
                b'{"prompt_tokens":8,"completion_tokens":2}}\n\n'
                b"data: [DONE]\n\n"
            )
        else:
            body = (
                b'data: {"id":"offline-request","choices":[{"index":0,'
                b'"delta":{"content":"offline "},"finish_reason":null}]}\n\n'
                b'data: {"id":"offline-request","choices":[{"index":0,'
                b'"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
                b'data: {"id":"offline-request","choices":[],"usage":'
                b'{"prompt_tokens":12,"completion_tokens":3}}\n\n'
                b"data: [DONE]\n\n"
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """关闭测试 HTTP Server 的标准错误访问日志。"""


class _ModelServer(ThreadingHTTPServer):
    """携带测试响应状态和脱敏观测值的本机 HTTP Server。"""

    def __init__(self, status: int = 200, *, tool_mode: bool = False) -> None:
        """绑定随机 loopback 端口并设置响应状态。"""
        super().__init__(("127.0.0.1", 0), _ModelHandler)
        self.status = status
        self.tool_mode = tool_mode
        self.observation: dict[str, object] = {}
        self.observations: list[dict[str, object]] = []


class _TtyInput(io.StringIO):
    """让确定性字符串输入表现为交互终端。"""

    def isatty(self) -> bool:
        """声明测试输入支持 CLI 交互模式。"""
        return True


@contextlib.contextmanager
def model_server(status: int = 200, *, tool_mode: bool = False) -> Iterator[_ModelServer]:
    """在后台线程运行可自动关闭的本机兼容模型端点。"""
    server = _ModelServer(status, tool_mode=tool_mode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CliChatTest(unittest.TestCase):
    """验证 CLI 聊天的配置边界、真实装配、持久化和退出码。"""

    def test_chat_requires_configured_model_key_without_leaking_environment(self) -> None:
        """缺少 API Key 应返回配置错误，且错误只包含变量名。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "state"
            run_cli(["init", "--home", str(home)])

            with mock.patch.dict(os.environ, {}, clear=True), change_directory(root):
                code, output, error = run_cli(
                    ["chat", "--home", str(home), "--message", "hello"]
                )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("MINICLAW_MODEL_API_KEY is not configured", error)

    def test_chat_requires_initialized_state(self) -> None:
        """聊天不得暗中初始化状态目录，应给出可执行的 init 提示。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "missing"
            with mock.patch.dict(os.environ, {}, clear=True), change_directory(root):
                code, output, error = run_cli(
                    ["chat", "--home", str(home), "--message", "hello"]
                )

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("miniclaw init", error)

    def test_chat_rejects_non_tty_without_message(self) -> None:
        """脚本环境省略 message 时应失败，而不是等待永远不可达的输入。"""
        with mock.patch.object(sys, "stdin", io.StringIO()):
            code, output, error = run_cli(["chat"])

        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("--message", error)

    def test_chat_runs_real_http_sse_and_persists_completed_turn(self) -> None:
        """CLI 应真实调用本机 SSE 端点并事务保存完成 Turn 与两条消息。"""
        with tempfile.TemporaryDirectory() as directory, model_server() as server:
            root = Path(directory)
            home = root / "state"
            run_cli(["init", "--home", str(home)])
            config = (home / "config.toml").read_text(encoding="utf-8")
            config = config.replace(
                "https://api.deepseek.com",
                f"http://127.0.0.1:{server.server_port}",
            )
            (home / "config.toml").write_text(config, encoding="utf-8")
            env_file = root / ".env"
            env_file.write_text("MINICLAW_MODEL_API_KEY=offline-secret\n", encoding="utf-8")
            env_file.chmod(0o600)

            with mock.patch.dict(os.environ, {}, clear=True), change_directory(root):
                code, output, error = run_cli(
                    [
                        "chat",
                        "--home",
                        str(home),
                        "--session",
                        "e2e",
                        "--message",
                        "hello",
                    ]
                )

            with contextlib.closing(sqlite3.connect(home / "miniclaw.db")) as connection:
                turn = connection.execute(
                    "SELECT status, model, input_tokens, output_tokens FROM turns"
                ).fetchone()
                messages = connection.execute(
                    "SELECT role, content FROM messages ORDER BY id"
                ).fetchall()

        self.assertEqual((code, output, error), (0, "offline answer\n", ""))
        self.assertEqual(server.observation["path"], "/chat/completions")
        self.assertIs(server.observation["authorized"], True)
        self.assertEqual(server.observation["model"], "deepseek-v4-pro")
        self.assertIs(server.observation["stream"], True)
        self.assertEqual(turn, ("completed", "deepseek-v4-pro", 12, 3))
        self.assertEqual(messages, [("user", "hello"), ("assistant", "offline answer")])

    def test_chat_maps_provider_authentication_to_exit_three(self) -> None:
        """Provider 认证失败应使用专用退出码，且不泄露本地 Key。"""
        with tempfile.TemporaryDirectory() as directory, model_server(401) as server:
            root = Path(directory)
            home = root / "state"
            run_cli(["init", "--home", str(home)])
            config_path = home / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "https://api.deepseek.com",
                    f"http://127.0.0.1:{server.server_port}",
                ),
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("MINICLAW_MODEL_API_KEY=must-not-leak\n", encoding="utf-8")
            env_file.chmod(0o600)

            with mock.patch.dict(os.environ, {}, clear=True), change_directory(root):
                code, output, error = run_cli(
                    ["chat", "--home", str(home), "--message", "hello"]
                )

        self.assertEqual(code, 3)
        self.assertEqual(output, "")
        self.assertIn("authentication failed", error)
        self.assertNotIn("must-not-leak", error)

    def test_chat_executes_system_info_tool_and_persists_trace(self) -> None:
        """真实 CLI 应发送 Schema、执行 system_info 并保存完整 Tool 轨迹。"""
        with tempfile.TemporaryDirectory() as directory, model_server(tool_mode=True) as server:
            root = Path(directory)
            home = root / "state"
            run_cli(["init", "--home", str(home)])
            config_path = home / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "https://api.deepseek.com",
                    f"http://127.0.0.1:{server.server_port}",
                ),
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("MINICLAW_MODEL_API_KEY=offline-secret\n", encoding="utf-8")
            env_file.chmod(0o600)

            with (
                mock.patch.dict(os.environ, {}, clear=True),
                change_directory(root),
                mock.patch(
                    "miniclaw.tools.system._collect_system_info",
                    return_value={
                        "cpu": {"model": "Test CPU", "logical_cores": 8},
                        "unavailable_sections": [],
                    },
                ),
            ):
                code, output, error = run_cli(
                    ["chat", "--home", str(home), "--message", "查看我的电脑配置"]
                )

            with contextlib.closing(sqlite3.connect(home / "miniclaw.db")) as connection:
                tool_run = connection.execute(
                    "SELECT tool_name, status, policy_action FROM tool_runs"
                ).fetchone()
                messages = connection.execute(
                    "SELECT role FROM messages ORDER BY id"
                ).fetchall()

        self.assertEqual((code, output, error), (0, "offline answer\n", ""))
        tools = server.observations[0]["tools"]
        self.assertIsInstance(tools, list)
        assert isinstance(tools, list)
        self.assertEqual(tools[0]["function"]["name"], "system_info")
        second_messages = server.observations[1]["messages"]
        self.assertIsInstance(second_messages, list)
        assert isinstance(second_messages, list)
        self.assertEqual(second_messages[-1]["role"], "tool")
        self.assertEqual(tool_run, ("system_info", "succeeded", "allow"))
        self.assertEqual(
            messages,
            [("user",), ("assistant",), ("tool",), ("assistant",)],
        )

    def test_chat_interactive_mode_reuses_session_until_exit(self) -> None:
        """TTY 模式应处理消息、打印角色提示，并在显式指令后成功退出。"""
        with tempfile.TemporaryDirectory() as directory, model_server() as server:
            root = Path(directory)
            home = root / "state"
            run_cli(["init", "--home", str(home)])
            config_path = home / "config.toml"
            config_path.write_text(
                config_path.read_text(encoding="utf-8").replace(
                    "https://api.deepseek.com",
                    f"http://127.0.0.1:{server.server_port}",
                ),
                encoding="utf-8",
            )
            env_file = root / ".env"
            env_file.write_text("MINICLAW_MODEL_API_KEY=interactive-secret\n", encoding="utf-8")
            env_file.chmod(0o600)

            with (
                mock.patch.dict(os.environ, {}, clear=True),
                change_directory(root),
                mock.patch.object(sys, "stdin", _TtyInput("hello\n/exit\n")),
            ):
                code, output, error = run_cli(
                    ["chat", "--home", str(home), "--session", "interactive"]
                )

            with contextlib.closing(sqlite3.connect(home / "miniclaw.db")) as connection:
                session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                turn_count = connection.execute("SELECT COUNT(*) FROM turns").fetchone()[0]

        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("You> ", output)
        self.assertIn("MiniClaw> offline answer", output)
        self.assertEqual((session_count, turn_count), (1, 1))


if __name__ == "__main__":
    unittest.main()
