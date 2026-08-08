"""MiniClaw Feishu Gateway 的生产装配、信号与有界生命周期。"""

import asyncio
import importlib
import importlib.util
import logging
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from miniclaw import gateway_lease
from miniclaw.channels import sdk_logging
from miniclaw.channels.approvals import ChannelApprovalController
from miniclaw.channels.delivery import DeliveryWorker
from miniclaw.channels.discord import DiscordTransport
from miniclaw.channels.experience import ChannelExperience
from miniclaw.channels.feishu import FeishuTransport
from miniclaw.channels.feishu_approval import FeishuApprovalActionHandler
from miniclaw.channels.observability import ChannelObserver
from miniclaw.channels.supervisor import (
    ChannelRuntime,
    GatewayConfigError,
    GatewaySecrets,
    GatewaySupervisor,
    collect_enabled_channels,
    validate_gateway_preflight,
)
from miniclaw.channels.telegram import TelegramTransport
from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.paths import StatePaths
from miniclaw.runtime import (
    AgentRuntime,
    create_channel_manager,
    create_runtime,
    limits_for_channel,
)
from miniclaw.storage.channels import DeliveryRepository
from miniclaw.storage.conversations import MessageRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository


class GatewayRuntimeError(RuntimeError):
    """表示已完成配置校验后的启动或运行失败。"""


def prepare_gateway_sdk_runtime() -> None:
    """在 asyncio.run 前加载会捕获进程事件循环的飞书 SDK。"""
    try:
        if importlib.util.find_spec("lark_channel") is not None:
            importlib.import_module("lark_channel")
    except (ImportError, RuntimeError) as error:
        raise GatewayConfigError(
            "official Feishu SDK could not be initialized"
        ) from error


class RuntimeComponent(Protocol):
    """Gateway 使用的 Runtime 关闭契约。"""

    async def aclose(self) -> None:
        """关闭 Provider。"""
        ...


class ManagerComponent(Protocol):
    """Gateway 使用的 Inbox Manager 生命周期。"""

    async def start(self) -> None:
        """启动 Worker。"""
        ...

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        """有限 drain 并停止。"""
        ...


class DeliveryComponent(Protocol):
    """Gateway 使用的 Outbox Worker 生命周期。"""

    async def start(self) -> None:
        """启动发送循环。"""
        ...

    async def stop(self) -> None:
        """停止发送循环。"""
        ...


class TransportComponent(Protocol):
    """Gateway 使用的 official Transport 生命周期。"""

    async def connect(self) -> None:
        """连接并等待就绪。"""
        ...

    def stop_receiving(self) -> None:
        """停止接收新入站事件。"""
        ...

    async def disconnect(self) -> None:
        """断开并释放连接。"""
        ...


@dataclass(slots=True)
class GatewayComponents:
    """保存单进程 Gateway 唯一组件实例。"""

    runtime: RuntimeComponent
    manager: ManagerComponent
    delivery: DeliveryComponent
    transport: TransportComponent
    account_id: str


def validate_gateway_environment(
    config: AppConfig,
    environ: Mapping[str, str],
    *,
    sdk_available: bool | Mapping[str, bool] | None = None,
) -> GatewaySecrets:
    """兼容旧 bool 注入，并委托 multi-channel preflight。"""
    availability: Mapping[str, bool] | None
    if isinstance(sdk_available, bool):
        availability = {
            channel: sdk_available for channel in collect_enabled_channels(config)
        }
    else:
        availability = sdk_available
    return validate_gateway_preflight(
        config,
        environ,
        sdk_available=availability,
    )


async def run_gateway(
    paths: StatePaths,
    *,
    environ: dict[str, str] | None = None,
    ready: Callable[[str], None] = print,
) -> None:
    """加载安全环境、安装信号并运行 production Gateway。"""
    target = os.environ if environ is None else environ
    try:
        load_dotenv(Path.cwd() / ".env", target)
        config = load_config(paths, target)
        secrets = validate_gateway_environment(config, target)
    except (ConfigError, DotEnvError, GatewayConfigError):
        raise
    _configure_channel_logging()
    sdk_logging.install_feishu_sdk_log_filter()
    try:
        lease = gateway_lease.GatewayLease.acquire(
            paths.run / "gateway.lock",
            commit=target.get("MINICLAW_GATEWAY_COMMIT", "unknown"),
        )
    except gateway_lease.GatewayLeaseError as error:
        raise GatewayConfigError(error.code) from None
    shutdown_event = asyncio.Event()
    force_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    signal_count = 0

    def request_shutdown() -> None:
        """首个信号优雅停止，第二个信号取消当前阻塞清理。"""
        nonlocal signal_count
        signal_count += 1
        if signal_count == 1:
            shutdown_event.set()
        else:
            force_event.set()

    try:
        for item in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(item, request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed.append(item)
        try:
            supervisor = await create_gateway_supervisor(config, paths, secrets)
            await supervisor.run(
                shutdown_event=shutdown_event,
                force_event=force_event,
                ready=ready,
            )
        except (ConfigError, DotEnvError, GatewayConfigError):
            raise
        except Exception:
            raise GatewayRuntimeError("gateway startup or runtime failed") from None
    finally:
        for item in installed:
            loop.remove_signal_handler(item)
        lease.close()


async def run_gateway_components(
    components: GatewayComponents,
    *,
    shutdown_event: asyncio.Event,
    force_event: asyncio.Event,
    ready: Callable[[str], None],
) -> None:
    """保留 Phase 4 单飞书测试入口，内部使用同一个 Supervisor。"""
    supervisor = GatewaySupervisor(
        runtime=components.runtime,
        channels=(
            ChannelRuntime(
                channel="feishu",
                account_id=components.account_id,
                manager=components.manager,
                delivery=components.delivery,
                transport=components.transport,
            ),
        ),
    )
    await supervisor.run(
        shutdown_event=shutdown_event,
        force_event=force_event,
        ready=ready,
    )


async def create_gateway_supervisor(
    config: AppConfig,
    paths: StatePaths,
    secrets: GatewaySecrets,
    *,
    runtime_factory: Callable[[AppConfig, StatePaths, str], AgentRuntime] | None = None,
    channel_factories: Mapping[str, Callable[..., ChannelRuntime]] | None = None,
) -> GatewaySupervisor:
    """创建唯一 AgentRuntime，并按稳定顺序各装配一条 Channel pipeline。"""
    build_runtime = runtime_factory or create_runtime
    runtime = build_runtime(config, paths, secrets.model_api_key)
    factories: Mapping[str, Callable[..., ChannelRuntime]] = channel_factories or {
        "feishu": _build_feishu_channel,
        "telegram": _build_telegram_channel,
        "discord": _build_discord_channel,
    }
    try:
        channels = tuple(
            factories[channel](config, paths, runtime, secrets)
            for channel in collect_enabled_channels(config)
        )
        return GatewaySupervisor(runtime=runtime, channels=channels)
    except Exception:
        await runtime.aclose()
        raise


def _channel_common(
    config: AppConfig,
    paths: StatePaths,
    runtime: AgentRuntime,
    channel: str,
    owner_external_user_id: str,
) -> tuple[
    Database,
    ChannelObserver,
    Any,
    ChannelApprovalController,
    DeliveryRepository,
]:
    """创建每个平台独立 Repository/Observer/Manager，但共享 TurnService。"""
    database = Database(paths.database)
    observer = ChannelObserver(database)
    manager = create_channel_manager(
        paths,
        runtime,
        limits_for_channel(config, channel),
        owner_external_user_id=owner_external_user_id,
        observer=observer,
    )
    approvals = ChannelApprovalController(
        owner_external_user_id=owner_external_user_id,
        approvals=ApprovalRepository(database),
        service=runtime.service,
    )
    manager.attach_approvals(approvals)
    return database, observer, manager, approvals, DeliveryRepository(database)


def _build_feishu_channel(
    config: AppConfig,
    paths: StatePaths,
    runtime: AgentRuntime,
    secrets: GatewaySecrets,
) -> ChannelRuntime:
    """装配 Feishu WS、Card approval、Experience 和 durable workers。"""
    selected = config.channels.feishu
    database, observer, manager, approvals, deliveries = _channel_common(
        config,
        paths,
        runtime,
        "feishu",
        selected.owner_open_id,
    )
    transport: FeishuTransport
    action_handler: FeishuApprovalActionHandler | None = None

    async def on_card_action(
        actor_open_id: str,
        value: Any,
        chat_id: str,
        message_id: str,
    ) -> None:
        if action_handler is not None:
            await action_handler(actor_open_id, value, chat_id, message_id)

    transport = FeishuTransport(
        selected,
        app_id=secrets.feishu_app_id,
        app_secret=secrets.channel_tokens["feishu"],
        on_inbound=manager.receive,
        on_card_action=on_card_action,
        observer=observer,
    )
    action_handler = FeishuApprovalActionHandler(
        user_id=runtime.owner_id,
        owner_external_user_id=selected.owner_open_id,
        account_id=selected.account_id,
        message_max_chars=selected.message_max_chars,
        controller=approvals,
        transport=transport,
        messages=MessageRepository(database),
        deliveries=deliveries,
    )
    limits = limits_for_channel(config, "feishu")
    manager.attach_experience(
        ChannelExperience(
            transport=transport,
            progress_enabled=selected.streaming_card,
            progress_is_final=True,
            update_interval=limits.progress_update_interval,
            max_visible_chars=selected.message_max_chars,
            observer=observer,
        )
    )
    delivery = DeliveryWorker(
        transport=transport,
        repository=deliveries,
        channel="feishu",
        account_id=selected.account_id,
        message_max_chars=selected.message_max_chars,
        observer=observer,
    )
    return ChannelRuntime(
        channel="feishu",
        account_id=selected.account_id,
        manager=manager,
        delivery=delivery,
        transport=transport,
        observer=observer,
    )


def _build_telegram_channel(
    config: AppConfig,
    paths: StatePaths,
    runtime: AgentRuntime,
    secrets: GatewaySecrets,
) -> ChannelRuntime:
    """装配 Telegram long polling、文本审批、Experience 和 durable workers。"""
    selected = config.channels.telegram
    _, observer, manager, _, deliveries = _channel_common(
        config,
        paths,
        runtime,
        "telegram",
        str(selected.owner_user_id),
    )
    transport = TelegramTransport(
        selected,
        token=secrets.channel_tokens["telegram"],
        on_inbound=manager.receive,
        observer=observer,
    )
    limits = limits_for_channel(config, "telegram")
    manager.attach_experience(
        ChannelExperience(
            transport=transport,
            progress_enabled=True,
            update_interval=limits.progress_update_interval,
            max_visible_chars=selected.message_max_chars,
            observer=observer,
        )
    )
    delivery = DeliveryWorker(
        transport=transport,
        repository=deliveries,
        channel="telegram",
        account_id=selected.account_id,
        message_max_chars=selected.message_max_chars,
        observer=observer,
    )
    return ChannelRuntime(
        channel="telegram",
        account_id=selected.account_id,
        manager=manager,
        delivery=delivery,
        transport=transport,
        observer=observer,
    )


def _build_discord_channel(
    config: AppConfig,
    paths: StatePaths,
    runtime: AgentRuntime,
    secrets: GatewaySecrets,
) -> ChannelRuntime:
    """装配 Discord Gateway、文本审批、Experience 和 durable workers。"""
    selected = config.channels.discord
    _, observer, manager, _, deliveries = _channel_common(
        config,
        paths,
        runtime,
        "discord",
        str(selected.owner_user_id),
    )
    transport = DiscordTransport(
        selected,
        token=secrets.channel_tokens["discord"],
        on_inbound=manager.receive,
        observer=observer,
    )
    limits = limits_for_channel(config, "discord")
    manager.attach_experience(
        ChannelExperience(
            transport=transport,
            progress_enabled=True,
            update_interval=limits.progress_update_interval,
            max_visible_chars=selected.message_max_chars,
            observer=observer,
        )
    )
    delivery = DeliveryWorker(
        transport=transport,
        repository=deliveries,
        channel="discord",
        account_id=selected.account_id,
        message_max_chars=selected.message_max_chars,
        observer=observer,
    )
    return ChannelRuntime(
        channel="discord",
        account_id=selected.account_id,
        manager=manager,
        delivery=delivery,
        transport=transport,
        observer=observer,
    )


def _configure_channel_logging() -> None:
    """为 Gateway 安装单个纯 JSON stderr handler，避免重复日志。"""
    logger = logging.getLogger("miniclaw.channel")
    if not any(getattr(handler, "_miniclaw_channel", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        handler._miniclaw_channel = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
