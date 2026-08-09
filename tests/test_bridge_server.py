"""MiniClaw stdio Bridge 的异步请求、事件和生命周期测试。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from miniclaw.agent.events import RunEvent
from miniclaw.bootstrap import initialize_state
from miniclaw.bridge.server import BridgeServer
from miniclaw.paths import build_state_paths
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.policy.modes import PermissionMode, PermissionState


def _request(request_id: str, request_type: str, payload: dict) -> bytes:
    """构造测试客户端发送的一条 protocol v1 请求。"""
    return (
        json.dumps(
            {"v": 1, "id": request_id, "type": request_type, "payload": payload},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


class QueueReader:
    """允许测试逐帧驱动 Server 的异步行读取器。"""

    def __init__(self) -> None:
        self._lines: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        """等待下一条测试帧或 EOF。"""
        return await self._lines.get()

    async def feed(self, line: bytes) -> None:
        """向 Server 交付一条完整行。"""
        await self._lines.put(line)

    async def eof(self) -> None:
        """用空字节表示客户端关闭 stdin。"""
        await self._lines.put(b"")


class CaptureWriter:
    """保存 Server 输出的完整 NDJSON 帧。"""

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.changed = asyncio.Event()

    async def write(self, data: bytes) -> None:
        """验证每次写入恰好是一条 JSON 行并保存解码结果。"""
        self.frames.append(json.loads(data))
        self.changed.set()

    async def wait_for_type(self, frame_type: str, *, timeout: float = 1) -> dict:
        """等待目标类型出现，避免用固定 sleep 制造竞态测试。"""
        async with asyncio.timeout(timeout):
            while True:
                found = next(
                    (frame for frame in self.frames if frame["type"] == frame_type),
                    None,
                )
                if found is not None:
                    return found
                self.changed.clear()
                await self.changed.wait()

    async def wait_for_id(self, request_id: str, *, timeout: float = 1) -> dict:
        """等待指定请求的响应帧。"""
        async with asyncio.timeout(timeout):
            while True:
                found = next(
                    (frame for frame in self.frames if frame.get("id") == request_id),
                    None,
                )
                if found is not None:
                    return found
                self.changed.clear()
                await self.changed.wait()


class EventTurnService:
    """用真实 RunEvent 顺序模拟完成和审批续跑。"""

    def __init__(self) -> None:
        self.decisions: list[ApprovalDecision] = []

    async def handle(self, owner_id, text, conversation_id, *, on_event=None):
        assert owner_id == 1
        assert text == "你好"
        assert conversation_id == "default"
        assert on_event is not None
        await on_event(RunEvent("turn_started", 21, {"session_id": 3}))
        await on_event(RunEvent("model_reasoning", 21, {"text": "先理解问题"}))
        await on_event(RunEvent("model_text_delta", 21, {"text": "你好"}))
        await on_event(
            RunEvent(
                "approval_required",
                21,
                {
                    "approval_id": 7,
                    "call_id": "call-7",
                    "tool_name": "run_command",
                    "summary": "run lark-cli",
                    "arguments": {"program": "/usr/local/bin/lark-cli", "args": ["doc"]},
                    "grant_modes": ["once", "session", "always"],
                },
            )
        )

    async def continue_approval(
        self,
        owner_id,
        approval_id,
        *,
        decision,
        on_event=None,
    ):
        assert owner_id == 1
        assert approval_id == 7
        assert on_event is not None
        self.decisions.append(decision)
        await on_event(
            RunEvent(
                "tool_finished",
                21,
                {"call_id": "call-7", "tool_name": "run_command", "status": "succeeded"},
            )
        )
        await on_event(
            RunEvent("turn_finished", 22, {"status": "completed", "content": "完成"})
        )


class BlockingTurnService:
    """让 Turn 保持运行，验证 busy 与 cancel。"""

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def handle(self, owner_id, text, conversation_id, *, on_event=None):
        del owner_id, text, conversation_id
        assert on_event is not None
        await on_event(RunEvent("turn_started", 30, {"session_id": 3}))
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


def _runtime(service) -> SimpleNamespace:
    """创建只暴露 Bridge 所需公开字段的 Runtime。"""
    return SimpleNamespace(
        owner_id=1,
        model="deepseek-v4-pro",
        workspace=SimpleNamespace(name="workspace"),
        ui_language="zh-CN",
        context_budget_tokens=32_000,
        tool_definitions=(SimpleNamespace(name="run_command"),),
        permission_state=PermissionState(PermissionMode.SAFE),
        service=service,
        memory_console=SimpleNamespace(
            command=lambda **values: {"echo": values},
        ),
    )


async def _run_bridge_process(
    home: Path,
    cwd: Path,
    environ: dict[str, str],
) -> tuple[int, bytes, bytes]:
    """在隔离环境中启动真实 Bridge 并完成握手与关闭。"""
    project = Path(__file__).resolve().parent.parent
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"MINICLAW_ENV_FILE", "MINICLAW_MODEL_API_KEY"}
    }
    environment.update({"PYTHONPATH": str(project / "src"), **environ})
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "miniclaw.bridge",
        "--home",
        str(home),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=environment,
    )
    stdin = b"".join(
        (
            _request(
                "hello-1",
                "client.hello",
                {
                    "client_name": "test-client",
                    "client_version": "0.1.0",
                    "protocols": [1],
                },
            ),
            _request("stop-1", "bridge.shutdown", {}),
        )
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=3)
    assert process.returncode is not None
    return process.returncode, stdout, stderr


class BridgeServerTest(unittest.IsolatedAsyncioTestCase):
    """验证客户端请求只能通过 Core 发布受控事件。"""

    async def test_handshake_turn_events_and_approval_share_one_protocol_stream(self) -> None:
        """握手、Turn、审批与续跑必须按请求和 RunEvent 顺序输出。"""
        reader = QueueReader()
        writer = CaptureWriter()
        service = EventTurnService()
        server = BridgeServer(_runtime(service), reader, writer)
        task = asyncio.create_task(server.run())

        await reader.feed(
            _request(
                "hello-1",
                "client.hello",
                {"client_name": "miniclaw-pi-tui", "client_version": "0.1.0", "protocols": [1]},
            )
        )
        hello = await writer.wait_for_type("response.ok")
        self.assertEqual(hello["id"], "hello-1")
        self.assertEqual(hello["payload"]["protocol"], 1)
        self.assertEqual(hello["payload"]["model"], "deepseek-v4-pro")
        self.assertEqual(hello["payload"]["tools"], ["run_command"])
        self.assertEqual(hello["payload"]["permission_mode"], "safe")

        await reader.feed(
            _request("turn-1", "turn.start", {"session_key": "default", "text": "你好"})
        )
        approval = await writer.wait_for_type("event.approval_required")
        self.assertEqual(approval["payload"]["turn_id"], 21)
        self.assertEqual(approval["payload"]["grant_modes"], ["once", "session", "always"])

        await reader.feed(
            _request(
                "approval-1",
                "approval.resolve",
                {"approval_id": 7, "decision": "session"},
            )
        )
        final = await writer.wait_for_type("event.turn_finished")
        self.assertEqual(
            final["payload"],
            {"turn_id": 22, "status": "completed", "content": "完成"},
        )
        self.assertEqual(service.decisions, [ApprovalDecision.SESSION])

        await reader.feed(_request("stop-1", "bridge.shutdown", {}))
        self.assertEqual(await task, 0)

    async def test_permission_mode_changes_only_while_idle_and_returns_current_mode(self) -> None:
        """CLI 可动态切换共享模式；运行中必须拒绝且不能静默扩权。"""
        reader = QueueReader()
        writer = CaptureWriter()
        service = BlockingTurnService()
        runtime = _runtime(service)
        server = BridgeServer(runtime, reader, writer)
        task = asyncio.create_task(server.run())

        await reader.feed(
            _request("mode-1", "permissions.set", {"mode": "autopilot"})
        )
        changed = await writer.wait_for_id("mode-1")
        self.assertEqual(changed["type"], "response.ok")
        self.assertEqual(changed["payload"], {"permission_mode": "autopilot"})
        self.assertEqual(runtime.permission_state.mode, PermissionMode.AUTOPILOT)

        await reader.feed(
            _request("turn-1", "turn.start", {"session_key": "default", "text": "first"})
        )
        await asyncio.wait_for(service.started.wait(), timeout=1)
        await reader.feed(_request("mode-busy", "permissions.set", {"mode": "yolo"}))
        rejected = await writer.wait_for_id("mode-busy")
        self.assertEqual(rejected["type"], "response.error")
        self.assertEqual(rejected["payload"]["code"], "permissions_busy")
        self.assertEqual(runtime.permission_state.mode, PermissionMode.AUTOPILOT)

        await reader.feed(_request("cancel-1", "turn.cancel", {}))
        await writer.wait_for_id("cancel-1")
        await reader.feed(_request("stop-1", "bridge.shutdown", {}))
        self.assertEqual(await task, 0)
        self.assertEqual(writer.frames[-1]["type"], "response.ok")
        self.assertEqual(writer.frames[-1]["id"], "stop-1")

    async def test_memory_command_routes_only_validated_core_arguments(self) -> None:
        """Bridge 把已验证 action/query/limit 路由到 Runtime Console。"""
        reader = QueueReader()
        writer = CaptureWriter()
        server = BridgeServer(_runtime(EventTurnService()), reader, writer)
        task = asyncio.create_task(server.run())

        await reader.feed(
            _request(
                "memory-1",
                "memory.command",
                {"action": "search", "query": "中文", "limit": 3},
            )
        )
        response = await writer.wait_for_id("memory-1")

        self.assertEqual(response["type"], "response.ok")
        self.assertEqual(
            response["payload"],
            {"echo": {"action": "search", "query": "中文", "limit": 3}},
        )
        await reader.feed(_request("stop-1", "bridge.shutdown", {}))
        self.assertEqual(await task, 0)

    async def test_busy_turn_rejects_second_start_and_cancel_stops_the_task(self) -> None:
        """同时只能执行一个 Turn，取消必须回收正在运行的 Core task。"""
        reader = QueueReader()
        writer = CaptureWriter()
        service = BlockingTurnService()
        server = BridgeServer(_runtime(service), reader, writer)
        task = asyncio.create_task(server.run())

        await reader.feed(
            _request("turn-1", "turn.start", {"session_key": "default", "text": "first"})
        )
        await asyncio.wait_for(service.started.wait(), timeout=1)
        await reader.feed(
            _request("turn-2", "turn.start", {"session_key": "default", "text": "second"})
        )
        busy = await writer.wait_for_type("response.error")
        self.assertEqual(busy["id"], "turn-2")
        self.assertEqual(busy["payload"]["code"], "turn_busy")

        await reader.feed(_request("cancel-1", "turn.cancel", {}))
        await asyncio.wait_for(service.cancelled.wait(), timeout=1)
        cancel = await writer.wait_for_id("cancel-1")
        self.assertEqual(cancel["type"], "response.ok")

        await reader.feed(_request("stop-1", "bridge.shutdown", {}))
        self.assertEqual(await task, 0)

    async def test_invalid_frame_and_wrong_approval_are_safe_and_server_continues(self) -> None:
        """畸形请求和错误 approval id 只返回稳定错误，不能终止 Bridge。"""
        reader = QueueReader()
        writer = CaptureWriter()
        server = BridgeServer(_runtime(EventTurnService()), reader, writer)
        task = asyncio.create_task(server.run())

        await reader.feed(b"{bad json}\n")
        invalid = await writer.wait_for_type("response.error")
        self.assertEqual(invalid["payload"]["code"], "invalid_json")
        self.assertNotIn("column", invalid["payload"]["message"])

        await reader.feed(
            _request(
                "approval-bad",
                "approval.resolve",
                {"approval_id": 99, "decision": "once"},
            )
        )
        rejected = await writer.wait_for_id("approval-bad")
        self.assertEqual(rejected["payload"]["code"], "approval_not_pending")

        await reader.eof()
        self.assertEqual(await task, 0)

    async def test_module_process_reserves_stdout_for_protocol_frames(self) -> None:
        """真实 Bridge 子进程的 stdout 必须只有可独立解析的 NDJSON。"""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            initialize_state(build_state_paths(home))
            returncode, stdout, stderr = await _run_bridge_process(
                home,
                Path(__file__).resolve().parent.parent,
                {"MINICLAW_MODEL_API_KEY": "offline-test-key"},
            )

        self.assertEqual(returncode, 0, stderr.decode("utf-8", errors="replace"))
        frames = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual([frame["id"] for frame in frames], ["hello-1", "stop-1"])
        self.assertEqual([frame["type"] for frame in frames], ["response.ok", "response.ok"])
        self.assertEqual(stderr, b"")

    async def test_module_process_prefers_absolute_installed_env_file(self) -> None:
        """显式安装态 Secret 文件必须优先于 cwd/.env，且两者内容都不能输出。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "state"
            cwd = root / "cwd"
            cwd.mkdir()
            paths = build_state_paths(home)
            initialize_state(paths)
            installed_secret = "BRIDGE_INSTALLED_SECRET_SENTINEL"
            cwd_secret = "BRIDGE_CWD_SECRET_SENTINEL"
            paths.secrets_file.write_text(
                f"MINICLAW_MODEL_API_KEY={installed_secret}\n",
                encoding="utf-8",
            )
            paths.secrets_file.chmod(0o600)
            (cwd / ".env").write_text(
                f"export MINICLAW_MODEL_API_KEY={cwd_secret}\n",
                encoding="utf-8",
            )
            (cwd / ".env").chmod(0o600)

            returncode, stdout, stderr = await _run_bridge_process(
                home,
                cwd,
                {"MINICLAW_ENV_FILE": str(paths.secrets_file)},
            )

        output = stdout + stderr
        self.assertEqual(returncode, 0, stderr.decode("utf-8", errors="replace"))
        self.assertNotIn(installed_secret.encode(), output)
        self.assertNotIn(cwd_secret.encode(), output)

    async def test_module_process_rejects_relative_installed_env_file(self) -> None:
        """相对安装态路径必须在读取可用 cwd/.env 前失败关闭，且不能输出凭据。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "state"
            cwd = root / "cwd"
            cwd.mkdir()
            initialize_state(build_state_paths(home))
            cwd_secret = "BRIDGE_RELATIVE_PATH_SECRET_SENTINEL"
            (cwd / ".env").write_text(
                f"MINICLAW_MODEL_API_KEY={cwd_secret}\n",
                encoding="utf-8",
            )
            (cwd / ".env").chmod(0o600)

            returncode, stdout, stderr = await _run_bridge_process(
                home,
                cwd,
                {"MINICLAW_ENV_FILE": "relative.env"},
            )

        output = stdout + stderr
        self.assertEqual(returncode, 2)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"error: MiniClaw Bridge startup failed\n")
        self.assertNotIn(cwd_secret.encode(), output)

    async def test_module_process_keeps_development_cwd_dotenv(self) -> None:
        """未选择安装态文件时，Bridge 仍应从固定 cwd/.env 启动。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            home = root / "state"
            cwd = root / "cwd"
            cwd.mkdir()
            initialize_state(build_state_paths(home))
            (cwd / ".env").write_text(
                "MINICLAW_MODEL_API_KEY=offline-development-key\n",
                encoding="utf-8",
            )
            (cwd / ".env").chmod(0o600)

            returncode, _stdout, stderr = await _run_bridge_process(home, cwd, {})

        self.assertEqual(returncode, 0, stderr.decode("utf-8", errors="replace"))


if __name__ == "__main__":
    unittest.main()
