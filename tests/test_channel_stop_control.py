"""Owner ``/stop`` 端到端验收：真实 TurnService + Runner + ToolExecutor。

与 `tests/test_channel_manager.py` 里的 Manager 单元测试互补——那边用 Fake Service
验证路由与 Worker 行为，这里用**真实**执行栈验证最关键的那条断言：

    /stop 能打断一个**正卡在工具执行中间**的 Turn，而不只是打断停在迭代边界的 Turn。

墙钟预算默认关闭之后，``/stop`` 是唯一的兜底手段，所以它必须在工具执行途中也生效。
"""

import asyncio
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from lobster0.agent.context import ContextBuilder
from lobster0.agent.runner import AgentRunner
from lobster0.agent.turn import TurnService
from lobster0.bootstrap import initialize_state
from lobster0.channels.base import InboundMessage
from lobster0.channels.manager import ChannelManager
from lobster0.config import WorkspaceConfig
from lobster0.paths import build_state_paths
from lobster0.policy.engine import PolicyEngine
from lobster0.policy.modes import PermissionMode, PermissionState
from lobster0.providers.base import (
    JsonValue,
    ModelResponse,
    ToolCall,
)
from lobster0.storage.channels import (
    ChannelIdentityRepository,
    DeliveryRepository,
    InboundEventRepository,
)
from lobster0.storage.conversations import (
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from lobster0.storage.database import Database
from lobster0.storage.tooling import ToolRunRepository
from lobster0.tools.base import ToolContext, ToolDefinition, ToolResult, ToolRisk
from lobster0.tools.command import RunCommandTool
from lobster0.tools.executor import ToolExecutor
from lobster0.tools.registry import ToolRegistry
from tests.fakes.fake_provider import FakeProvider


class _BlockingTool:
    """一个永远不会自己返回的工具，用来把 Turn 钉在"工具执行中"这个状态。"""

    definition = ToolDefinition(
        name="blocking",
        description="Block until cancelled.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self) -> None:
        """创建"已进入工具"信号与取消观测位。"""
        self.entered = asyncio.Event()
        self.cancelled = False

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受空参数。"""
        if arguments:
            raise ValueError("blocking accepts no arguments")
        return arguments

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """进入后一直挂起，直到被取消。"""
        del context, arguments
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("blocking tool must never return on its own")


class ChannelStopControlTest(unittest.IsolatedAsyncioTestCase):
    """验证 /stop 在真实执行栈上打断执行中的 Turn 并留下一致状态。"""

    def setUp(self) -> None:
        """创建完整状态目录、Repository 与权限状态。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.owner = initialized.owner
        self.database = Database(self.paths.database)
        self.sessions = SessionRepository(self.database)
        self.messages = MessageRepository(self.database)
        self.turns = TurnRepository(self.database)
        self.inbound = InboundEventRepository(self.database)
        self.deliveries = DeliveryRepository(self.database)
        self.permission_state = PermissionState(PermissionMode.YOLO)

    async def test_stop_interrupts_a_turn_blocked_inside_a_tool(self) -> None:
        """核心验收：Turn 卡在工具执行中间时，``/stop`` 必须真的把它打断。

        墙钟预算只在**工具边界**检查，所以一次 120 秒的 run_command 能让 90 秒的
        预算实际跑到 197 秒（真实事故）。/stop 走的是 asyncio 取消，取消点在工具
        自己的 await 上，因此不受工具边界限制。
        """
        tool = _BlockingTool()
        service = self._service(tool)
        manager = self._manager(service)
        await manager.start()
        try:
            await manager.receive(self._message("om_long", "跑个长活"))
            # 等到 Turn 真的进入工具内部，而不是停在迭代边界。
            await asyncio.wait_for(tool.entered.wait(), timeout=3)

            await manager.receive(self._message("om_stop", "/stop"))
            await manager.wait_idle(timeout=3)
        finally:
            await manager.stop()

        self.assertTrue(tool.cancelled, "工具必须在执行中途收到取消")
        session = self.sessions.get_or_create(
            self.owner.id, "feishu", "default", "oc_chat"
        )
        turn = self.turns.get_by_inbound(session.id, "om_long")
        self.assertEqual(turn.status, "cancelled")
        self.assertIsNone(turn.error_code)

        # 被中断的 ToolRun 落成 interrupted，不会被误记成成功或失败。
        with self.database.connect_read_only() as connection:
            statuses = [
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM tool_runs ORDER BY id"
                )
            ]
        self.assertEqual(statuses, ["interrupted"])

        # 入站事件到达终态；重启后不会重放这条已经开始过的消息。
        self.assertEqual(
            tuple(self.inbound.list_by_status("feishu", "default", "running")),
            (),
        )
        self.assertEqual(
            tuple(self.inbound.list_by_status("feishu", "default", "queued")),
            (),
        )

        # Owner 拿到了停止诊断和确认，两条都投递出去了。
        with self.database.connect_read_only() as connection:
            delivered = [
                row["content"]
                for row in connection.execute(
                    "SELECT content FROM deliveries ORDER BY id"
                )
            ]
        self.assertTrue(
            any("turn_stopped" in content for content in delivered), delivered
        )
        self.assertTrue(
            any("已停止本轮任务" in content for content in delivered), delivered
        )

    async def test_stop_kills_a_real_long_running_command(self) -> None:
        """``/stop`` 必须打断真实的 ``run_command`` 子进程，而不是等它自己超时。

        真实事故卡片里 step 4 的 ``python3`` 独占了 120.1 秒——正好是 run_command
        的完整超时。HostSandbox 在 ``except asyncio.CancelledError`` 里会
        ``_terminate_process_group``，所以取消能穿透到子进程。
        """
        command = RunCommandTool(
            executable_path="/usr/bin:/bin:/usr/sbin:/sbin",
            owner_home=self.paths.home,
        )
        marker = self.paths.home / "heartbeat"
        script = self.paths.home / "long_running.py"
        # 内联 -c 被 command policy 永久禁止，脚本文件是唯一合法形态。
        script.write_text(
            "import pathlib, time\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            "while True:\n"
            "    marker.write_text(str(time.time()))\n"
            "    time.sleep(0.02)\n",
            encoding="utf-8",
        )
        service = self._service(
            command,
            tool_name="run_command",
            arguments={
                # 与真实事故同形：`run_command python3`，独占了完整的 120 秒超时。
                "program": "python3",
                # 子进程活着就一直刷新 marker；被杀之后 marker 不再更新。
                "args": [str(script)],
                "timeout_seconds": 120,
            },
        )
        manager = self._manager(service)
        await manager.start()
        started = datetime.now(UTC)
        try:
            await manager.receive(self._message("om_sleep", "跑个长命令"))
            # 等 tool_runs 出现 running，再等 marker 出现——后者才证明子进程
            # 真的在跑，而不只是刚被 fork 出来。
            await self._wait_for_running_tool_run(timeout=5)
            await self._wait_for_path(marker, timeout=10)

            await manager.receive(self._message("om_stop", "/stop"))
            await manager.wait_idle(timeout=5)
        finally:
            await manager.stop()
        elapsed = (datetime.now(UTC) - started).total_seconds()

        # 远小于 120 秒的工具超时：说明是被取消的，不是等超时等回来的。
        self.assertLess(elapsed, 30)
        # 子进程真的死了：停止后 marker 不再被刷新。
        self.assertTrue(marker.exists(), "子进程应该至少刷新过一次 marker")
        settled = marker.stat().st_mtime_ns
        await asyncio.sleep(0.3)
        self.assertEqual(marker.stat().st_mtime_ns, settled)
        session = self.sessions.get_or_create(
            self.owner.id, "feishu", "default", "oc_chat"
        )
        self.assertEqual(
            self.turns.get_by_inbound(session.id, "om_sleep").status,
            "cancelled",
        )
        with self.database.connect_read_only() as connection:
            statuses = [
                row["status"]
                for row in connection.execute(
                    "SELECT status FROM tool_runs ORDER BY id"
                )
            ]
        self.assertEqual(statuses, ["interrupted"])

    async def _wait_for_path(self, path: Path, *, timeout: float) -> None:
        """轮询到给定路径出现为止。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if path.exists():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"{path.name} never appeared")

    async def _wait_for_running_tool_run(self, *, timeout: float) -> None:
        """轮询到出现一条 running ToolRun 为止；不 sleep 固定时长。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            with self.database.connect_read_only() as connection:
                row = connection.execute(
                    "SELECT id FROM tool_runs WHERE status = 'running' LIMIT 1"
                ).fetchone()
            if row is not None:
                return
            await asyncio.sleep(0.01)
        raise AssertionError("tool run never reached running")

    def _service(
        self,
        tool: object,
        *,
        tool_name: str = "blocking",
        arguments: dict[str, JsonValue] | None = None,
    ) -> TurnService:
        """用真实 Runner/Executor 装配一个只会调用给定工具的 TurnService。"""
        executor = ToolExecutor(
            ToolRegistry((tool,)),
            # YOLO：本测试关心的是"取消能不能穿透到工具内部"，不是审批门禁。
            PolicyEngine(
                security="full",
                ask="off",
                permission_state=self.permission_state,
                executable_path="/usr/bin:/bin:/usr/sbin:/sbin",
            ),
            ToolRunRepository(self.database),
        )
        provider = FakeProvider(
            (
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall("call_stop_0", tool_name, arguments or {}),
                    ),
                    reasoning_content="",
                    finish_reason="tool_calls",
                    input_tokens=5,
                    output_tokens=2,
                    provider_request_id="req_stop",
                ),
            )
        )
        return TurnService(
            owner_id=self.owner.id,
            model="deepseek-v4-pro",
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            context=ContextBuilder(self.paths),
            runner=AgentRunner(provider, executor),
            state_home=self.paths.home,
            workspace=WorkspaceConfig(path=self.paths.workspace),
        )

    def _manager(self, service: TurnService) -> ChannelManager:
        """构造与生产同参数、worker_count=2 的 Manager。"""
        return ChannelManager(
            owner_id=self.owner.id,
            owner_external_user_id="ou_owner",
            permission_state=self.permission_state,
            service=service,
            sessions=self.sessions,
            messages=self.messages,
            turns=self.turns,
            identities=ChannelIdentityRepository(self.database),
            inbound=self.inbound,
            deliveries=self.deliveries,
            channel="feishu",
            account_id="default",
            queue_size=8,
            worker_count=2,
            feeder_interval=0.01,
        )

    def _message(self, message_id: str, text: str) -> InboundMessage:
        """构造一条 Owner 私聊消息。"""
        return InboundMessage(
            channel="feishu",
            account_id="default",
            event_id=f"evt_{message_id}",
            message_id=message_id,
            external_user_id="ou_owner",
            external_conversation_id="oc_chat",
            chat_type="p2p",
            message_type="text",
            text=text,
            reply_to_message_id=message_id,
            replied_to_message_id="",
            received_at=datetime(2026, 8, 12, tzinfo=UTC),
        )


if __name__ == "__main__":
    unittest.main()
