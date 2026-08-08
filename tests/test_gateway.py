"""Gateway 配置、组件生命周期与信号取消测试。"""

import asyncio
import tempfile
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.config import load_config
from miniclaw.gateway import (
    GatewayComponents,
    GatewayConfigError,
    run_gateway_components,
    validate_gateway_environment,
)
from miniclaw.paths import build_state_paths


@dataclass(slots=True)
class FakeRuntime:
    """记录 Provider Runtime 关闭。"""

    log: list[str]

    async def aclose(self) -> None:
        """记录关闭。"""
        self.log.append("runtime.close")


@dataclass(slots=True)
class FakeManager:
    """记录 Inbox Worker 启停。"""

    log: list[str]

    async def start(self) -> None:
        """记录启动。"""
        self.log.append("manager.start")

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """记录 drain。"""
        del drain_timeout
        self.log.append("manager.stop")


@dataclass(slots=True)
class FakeDelivery:
    """记录 Outbox Worker 启停。"""

    log: list[str]
    block_stop: bool = False
    stop_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        """记录启动。"""
        self.log.append("delivery.start")

    async def stop(self) -> None:
        """可选阻塞，用于第二信号强制取消测试。"""
        self.log.append("delivery.stop")
        self.stop_started.set()
        if self.block_stop:
            await asyncio.Event().wait()


@dataclass(slots=True)
class FakeTransport:
    """记录 WebSocket 生命周期。"""

    log: list[str]

    async def connect(self) -> None:
        """记录连接就绪。"""
        self.log.append("transport.connect")

    def stop_receiving(self) -> None:
        """记录停止接收新事件。"""
        self.log.append("transport.stop_receiving")

    async def disconnect(self) -> None:
        """记录断连。"""
        self.log.append("transport.disconnect")


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    """验证 Gateway 不联网前失败和优雅停止顺序。"""

    def setUp(self) -> None:
        """创建可加载的本地状态和 enabled Feishu 配置。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialize_state(self.paths)
        with self.paths.config.open("a", encoding="utf-8") as config_file:
            config_file.write(
                "\n[channels.feishu]\n"
                "enabled = true\n"
                'owner_open_id = "ou_owner"\n'
                'allowed_open_ids = ["ou_owner"]\n'
            )
        self.config = load_config(self.paths)

    def test_validation_fails_before_network_and_never_exposes_credentials(self) -> None:
        """缺 SDK/模型或飞书凭据使用稳定配置错误，不能回显已有 secret。"""
        secret = "feishu-secret-private"
        cases = (
            ({}, "MINICLAW_MODEL_API_KEY"),
            ({"MINICLAW_MODEL_API_KEY": "model-key"}, "MINICLAW_FEISHU_APP_ID"),
            (
                {
                    "MINICLAW_MODEL_API_KEY": "model-key",
                    "MINICLAW_FEISHU_APP_ID": "cli_test",
                    "MINICLAW_FEISHU_APP_SECRET": secret,
                },
                "official Feishu SDK",
            ),
        )
        for index, (environment, expected) in enumerate(cases):
            with self.subTest(index=index):
                with self.assertRaises(GatewayConfigError) as raised:
                    validate_gateway_environment(
                        self.config,
                        environment,
                        sdk_available=index != 2,
                    )
                self.assertIn(expected, str(raised.exception))
                self.assertNotIn(secret, str(raised.exception))

    def test_disabled_channel_is_rejected(self) -> None:
        """未显式启用 Feishu 时 gateway 不应尝试构造 SDK。"""
        disabled = replace(
            self.config,
            channels=replace(
                self.config.channels,
                feishu=replace(self.config.channels.feishu, enabled=False),
            ),
        )
        with self.assertRaises(GatewayConfigError) as raised:
            validate_gateway_environment(disabled, {}, sdk_available=True)
        self.assertIn("disabled", str(raised.exception))

    async def test_startup_ready_and_shutdown_use_safe_order(self) -> None:
        """连接就绪后才启动 Worker；停止时先关入口再 drain，最后关 Provider。"""
        log: list[str] = []
        ready: list[str] = []
        shutdown = asyncio.Event()
        shutdown.set()
        components = GatewayComponents(
            runtime=FakeRuntime(log),
            manager=FakeManager(log),
            delivery=FakeDelivery(log),
            transport=FakeTransport(log),
            account_id="default",
        )

        await run_gateway_components(
            components,
            shutdown_event=shutdown,
            force_event=asyncio.Event(),
            ready=ready.append,
        )

        self.assertEqual(ready, ["MiniClaw gateway ready: feishu/default"])
        self.assertEqual(
            log,
            [
                "transport.connect",
                "delivery.start",
                "manager.start",
                "transport.stop_receiving",
                "manager.stop",
                "delivery.stop",
                "transport.disconnect",
                "runtime.close",
            ],
        )

    async def test_second_signal_cancels_blocked_shutdown_without_process_kill(self) -> None:
        """force event 应取消阻塞组件并继续关闭其余资源。"""
        log: list[str] = []
        delivery = FakeDelivery(log, block_stop=True)
        shutdown = asyncio.Event()
        force = asyncio.Event()
        components = GatewayComponents(
            runtime=FakeRuntime(log),
            manager=FakeManager(log),
            delivery=delivery,
            transport=FakeTransport(log),
            account_id="default",
        )
        task = asyncio.create_task(
            run_gateway_components(
                components,
                shutdown_event=shutdown,
                force_event=force,
                ready=lambda _: None,
            )
        )
        await asyncio.sleep(0)
        shutdown.set()
        await delivery.stop_started.wait()
        force.set()
        await asyncio.wait_for(task, timeout=1)

        self.assertIn("delivery.stop", log)
        self.assertEqual(log[-2:], ["transport.disconnect", "runtime.close"])


if __name__ == "__main__":
    unittest.main()
