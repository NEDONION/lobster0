"""Multi-channel Gateway preflight、单 Runtime 与反向生命周期测试。"""

import asyncio
import unittest
from dataclasses import dataclass, field, replace
from pathlib import Path

from lobster0.channels.supervisor import (
    ChannelRuntime,
    GatewayConfigError,
    GatewaySecrets,
    GatewaySupervisor,
    collect_enabled_channels,
    validate_gateway_preflight,
)
from lobster0.config import (
    AgentConfig,
    AppConfig,
    DiscordConfig,
    ProviderConfig,
    TelegramConfig,
    WorkspaceConfig,
)
from lobster0.gateway import create_gateway_supervisor


@dataclass(slots=True)
class FakeRuntime:
    log: list[str]
    closes: int = 0

    async def aclose(self) -> None:
        self.closes += 1
        self.log.append("runtime.close")


@dataclass(slots=True)
class FakeLifecycleRuntime(FakeRuntime):
    """记录 Runtime-owned background workers 的独立停止阶段。"""

    async def astart(self) -> None:
        """记录 background startup。"""
        self.log.append("runtime.start")

    async def astop_background(self) -> None:
        """记录停止 Scheduler intake/Runner。"""
        self.log.append("runtime.background.stop")


@dataclass(slots=True)
class FakeManager:
    name: str
    log: list[str]

    async def start(self) -> None:
        self.log.append(f"{self.name}.manager.start")

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        del drain_timeout
        self.log.append(f"{self.name}.manager.stop")


@dataclass(slots=True)
class FakeDelivery:
    name: str
    log: list[str]
    fail_start: bool = False
    block_stop: bool = False
    stop_started: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        self.log.append(f"{self.name}.delivery.start")
        if self.fail_start:
            raise RuntimeError("private-delivery-error")

    async def stop(self) -> None:
        self.log.append(f"{self.name}.delivery.stop")
        self.stop_started.set()
        if self.block_stop:
            await asyncio.Event().wait()


@dataclass(slots=True)
class FakeTransport:
    name: str
    log: list[str]

    async def connect(self) -> None:
        self.log.append(f"{self.name}.transport.connect")

    def stop_receiving(self) -> None:
        self.log.append(f"{self.name}.transport.stop_receiving")

    async def disconnect(self) -> None:
        self.log.append(f"{self.name}.transport.disconnect")


class GatewayPreflightTest(unittest.TestCase):
    """验证全平台凭据/SDK 在任何 runtime 或网络构造前一次性通过。"""

    def setUp(self) -> None:
        base = AppConfig(
            agent=AgentConfig(),
            provider=ProviderConfig(),
            workspace=WorkspaceConfig(Path("/tmp/lobster0-supervisor-test")),
        )
        self.config = replace(
            base,
            channels=replace(
                base.channels,
                feishu=replace(
                    base.channels.feishu,
                    enabled=True,
                    account_id="work",
                    owner_open_id="ou_owner",
                    allowed_open_ids=("ou_owner",),
                ),
                telegram=TelegramConfig(
                    enabled=True,
                    account_id="personal",
                    owner_user_id=300,
                    allowed_user_ids=(300,),
                ),
                discord=DiscordConfig(
                    enabled=True,
                    account_id="personal",
                    owner_user_id=300,
                    allowed_user_ids=(300,),
                ),
            ),
        )
        self.environment = {
            "LOBSTER0_MODEL_API_KEY": "model-private",
            "LOBSTER0_FEISHU_APP_ID": "cli_private",
            "LOBSTER0_FEISHU_APP_SECRET": "feishu-private",
            "LOBSTER0_TELEGRAM_BOT_TOKEN": "telegram-private",
            "LOBSTER0_DISCORD_BOT_TOKEN": "discord-private",
        }

    def test_enabled_order_and_secret_bundle_are_fixed_and_redacted(self) -> None:
        """顺序固定为 Feishu/Telegram/Discord，相同 account_id 跨平台合法。"""
        self.assertEqual(
            collect_enabled_channels(self.config),
            ("feishu", "telegram", "discord"),
        )

        secrets = validate_gateway_preflight(
            self.config,
            self.environment,
            sdk_available={"feishu": True, "telegram": True, "discord": True},
        )

        self.assertEqual(
            repr(secrets),
            "GatewaySecrets(configured=discord,feishu,telegram)",
        )
        for secret in self.environment.values():
            self.assertNotIn(secret, repr(secrets))

    def test_empty_missing_sdk_token_and_owner_relation_fail_closed(self) -> None:
        """任一静态错误都只返回稳定配置原因，不泄露已存在凭据。"""
        empty = replace(
            self.config,
            channels=replace(
                self.config.channels,
                feishu=replace(self.config.channels.feishu, enabled=False),
                telegram=replace(self.config.channels.telegram, enabled=False),
                discord=replace(self.config.channels.discord, enabled=False),
            ),
        )
        cases = (
            (empty, self.environment, {"feishu": True}, "no_channels_enabled"),
            (
                self.config,
                {**self.environment, "LOBSTER0_DISCORD_BOT_TOKEN": ""},
                {"feishu": True, "telegram": True, "discord": True},
                "LOBSTER0_DISCORD_BOT_TOKEN",
            ),
            (
                self.config,
                self.environment,
                {"feishu": True, "telegram": False, "discord": True},
                "official Telegram SDK",
            ),
            (
                replace(
                    self.config,
                    channels=replace(
                        self.config.channels,
                        discord=replace(
                            self.config.channels.discord,
                            owner_user_id=999,
                        ),
                    ),
                ),
                self.environment,
                {"feishu": True, "telegram": True, "discord": True},
                "discord_owner_not_allowed",
            ),
        )
        for config, environment, availability, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(GatewayConfigError) as raised:
                    validate_gateway_preflight(
                        config,
                        environment,
                        sdk_available=availability,
                    )
                self.assertIn(code, str(raised.exception))
                self.assertNotIn("private", str(raised.exception))


class GatewaySupervisorTest(unittest.IsolatedAsyncioTestCase):
    """验证三条 pipeline 的精确启动、隔离、失败补偿和关闭顺序。"""

    def _channel(
        self,
        name: str,
        log: list[str],
        *,
        fail_delivery: bool = False,
        block_delivery_stop: bool = False,
    ) -> ChannelRuntime:
        return ChannelRuntime(
            channel=name,
            account_id="default",
            manager=FakeManager(name, log),
            delivery=FakeDelivery(
                name,
                log,
                fail_start=fail_delivery,
                block_stop=block_delivery_stop,
            ),
            transport=FakeTransport(name, log),
        )

    async def test_three_channels_start_and_reverse_shutdown_with_one_runtime(self) -> None:
        """每条 pipeline 内固定启动；全局先关入口，再反序 drain，Runtime 只关一次。"""
        log: list[str] = []
        runtime = FakeRuntime(log)
        channels = tuple(
            self._channel(name, log) for name in ("feishu", "telegram", "discord")
        )
        supervisor = GatewaySupervisor(runtime=runtime, channels=channels)
        shutdown = asyncio.Event()
        shutdown.set()
        ready: list[str] = []

        await supervisor.run(
            shutdown_event=shutdown,
            force_event=asyncio.Event(),
            ready=ready.append,
        )

        self.assertEqual(
            ready,
            [
                "Lobster0 gateway ready: "
                "feishu/default, telegram/default, discord/default"
            ],
        )
        self.assertEqual(runtime.closes, 1)
        self.assertEqual(
            log,
            [
                "feishu.transport.connect",
                "feishu.delivery.start",
                "feishu.manager.start",
                "telegram.transport.connect",
                "telegram.delivery.start",
                "telegram.manager.start",
                "discord.transport.connect",
                "discord.delivery.start",
                "discord.manager.start",
                "feishu.transport.stop_receiving",
                "telegram.transport.stop_receiving",
                "discord.transport.stop_receiving",
                "discord.manager.stop",
                "discord.delivery.stop",
                "discord.transport.disconnect",
                "telegram.manager.stop",
                "telegram.delivery.stop",
                "telegram.transport.disconnect",
                "feishu.manager.stop",
                "feishu.delivery.stop",
                "feishu.transport.disconnect",
                "runtime.close",
            ],
        )
        self.assertTrue(all(channel.state == "stopped" for channel in channels))

    async def test_startup_failure_cleans_only_started_components_in_reverse(self) -> None:
        """第二平台 delivery 启动失败时第三平台不启动，已启动层精确补偿。"""
        log: list[str] = []
        runtime = FakeRuntime(log)
        channels = (
            self._channel("feishu", log),
            self._channel("telegram", log, fail_delivery=True),
            self._channel("discord", log),
        )
        supervisor = GatewaySupervisor(runtime=runtime, channels=channels)

        with self.assertRaises(RuntimeError):
            await supervisor.run(
                shutdown_event=asyncio.Event(),
                force_event=asyncio.Event(),
                ready=lambda _: None,
            )

        self.assertNotIn("discord.transport.connect", log)
        self.assertIn("telegram.transport.disconnect", log)
        self.assertNotIn("telegram.delivery.stop", log)
        self.assertEqual(log[-1], "runtime.close")
        self.assertEqual(runtime.closes, 1)

    async def test_degraded_channel_does_not_stop_other_ready_pipeline(self) -> None:
        """运行期单平台降级只改本地状态，不触发全局 Runtime 或其他 pipeline 清理。"""
        log: list[str] = []
        channels = (
            self._channel("feishu", log),
            self._channel("telegram", log),
        )
        supervisor = GatewaySupervisor(runtime=FakeRuntime(log), channels=channels)
        await supervisor.start(ready=lambda _: None)

        supervisor.report_degraded("telegram", "telegram_poll_failed")

        self.assertEqual(channels[0].state, "ready")
        self.assertEqual(channels[1].state, "degraded")
        self.assertNotIn("runtime.close", log)
        await supervisor.shutdown(force_event=asyncio.Event())

    async def test_background_intake_stops_before_channel_ingress_and_delivery(self) -> None:
        """关停先停止 Scheduler/Runner，再停止 Channel 接收与 Outbox。"""
        log: list[str] = []
        runtime = FakeLifecycleRuntime(log)
        channel = self._channel("feishu", log)
        supervisor = GatewaySupervisor(runtime=runtime, channels=(channel,))
        await supervisor.start(ready=lambda _: None)

        await supervisor.shutdown(force_event=asyncio.Event())

        self.assertLess(
            log.index("runtime.background.stop"),
            log.index("feishu.transport.stop_receiving"),
        )
        self.assertEqual(log.count("runtime.background.stop"), 1)

    async def test_gateway_factory_creates_one_runtime_and_one_pipeline_per_channel(
        self,
    ) -> None:
        """生产装配边界必须复用同一 Runtime，且每个平台 factory 精确一次。"""
        base = AppConfig(
            agent=AgentConfig(),
            provider=ProviderConfig(),
            workspace=WorkspaceConfig(Path("/tmp/lobster0-factory-test")),
        )
        config = replace(
            base,
            channels=replace(
                base.channels,
                feishu=replace(base.channels.feishu, enabled=True),
                telegram=replace(base.channels.telegram, enabled=True),
                discord=replace(base.channels.discord, enabled=True),
            ),
        )
        log: list[str] = []
        runtime = FakeRuntime(log)
        runtime_calls: list[str] = []
        channel_calls: list[tuple[str, FakeRuntime]] = []

        def runtime_factory(config, paths, api_key: str):
            del config, paths
            runtime_calls.append(api_key)
            return runtime

        def channel_factory(name: str):
            def build(config, paths, shared_runtime, secrets):
                del config, paths, secrets
                channel_calls.append((name, shared_runtime))
                return self._channel(name, log)

            return build

        supervisor = await create_gateway_supervisor(
            config,
            object(),  # type: ignore[arg-type]
            GatewaySecrets(
                model_api_key="model-private",
                channel_tokens={
                    "feishu": "f",
                    "telegram": "t",
                    "discord": "d",
                },
            ),
            runtime_factory=runtime_factory,
            channel_factories={
                name: channel_factory(name)
                for name in ("feishu", "telegram", "discord")
            },
        )

        self.assertEqual(runtime_calls, ["model-private"])
        self.assertEqual(
            [name for name, _ in channel_calls],
            ["feishu", "telegram", "discord"],
        )
        self.assertTrue(all(item is runtime for _, item in channel_calls))
        self.assertIs(supervisor.runtime, runtime)
        await supervisor.shutdown(force_event=asyncio.Event())
        self.assertEqual(runtime.closes, 1)


if __name__ == "__main__":
    unittest.main()
