"""MiniClaw Feishu Gateway 的生产装配、信号与有界生命周期。"""

import asyncio
import importlib.util
import os
import signal
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from miniclaw.channels.approvals import ChannelApprovalController
from miniclaw.channels.capabilities import ChannelCapabilities
from miniclaw.channels.delivery import DeliveryWorker, split_message
from miniclaw.channels.feishu import FeishuTransport
from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.paths import StatePaths
from miniclaw.runtime import AgentRuntime, create_channel_manager, create_runtime
from miniclaw.storage.channels import DeliveryRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository


class GatewayConfigError(ValueError):
    """表示 Gateway 在联网前即可发现的安全配置问题。"""


class GatewayRuntimeError(RuntimeError):
    """表示已完成配置校验后的启动或运行失败。"""


@dataclass(frozen=True, slots=True, repr=False)
class GatewayCredentials:
    """短暂保存生产组件构造需要的三个 secret，repr 永不显示值。"""

    model_api_key: str
    app_id: str
    app_secret: str

    def __repr__(self) -> str:
        """只报告字段均已配置。"""
        return "GatewayCredentials(configured=True)"


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
    sdk_available: bool | None = None,
) -> GatewayCredentials:
    """在任何平台网络调用前校验开关、SDK 和三个凭据变量。"""
    feishu = config.channels.feishu
    if not feishu.enabled:
        raise GatewayConfigError("Feishu channel is disabled in config.toml")
    if sdk_available is None:
        sdk_available = importlib.util.find_spec("lark_channel") is not None
    names = (
        config.provider.api_key_env,
        feishu.app_id_env,
        feishu.app_secret_env,
    )
    values = tuple(str(environ.get(name, "")).strip() for name in names)
    for name, value in zip(names, values, strict=True):
        if not value:
            raise GatewayConfigError(f"{name} is not configured")
    if not sdk_available:
        raise GatewayConfigError(
            "official Feishu SDK is not installed; run uv sync --extra feishu"
        )
    return GatewayCredentials(values[0], values[1], values[2])


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
        credentials = validate_gateway_environment(config, target)
    except (ConfigError, DotEnvError, GatewayConfigError):
        raise
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

    for item in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(item, request_shutdown)
        except (NotImplementedError, RuntimeError, ValueError):
            continue
        installed.append(item)
    try:
        components = await _create_components(config, paths, credentials)
        await run_gateway_components(
            components,
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


async def run_gateway_components(
    components: GatewayComponents,
    *,
    shutdown_event: asyncio.Event,
    force_event: asyncio.Event,
    ready: Callable[[str], None],
) -> None:
    """按确定顺序启动，并在任意失败路径反向释放已创建组件。"""
    transport_connected = False
    delivery_started = False
    manager_started = False
    try:
        await components.transport.connect()
        transport_connected = True
        await components.delivery.start()
        delivery_started = True
        await components.manager.start()
        manager_started = True
        ready(f"MiniClaw gateway ready: feishu/{components.account_id}")
        await shutdown_event.wait()
    finally:
        try:
            components.transport.stop_receiving()
        except Exception:
            pass
        if manager_started:
            await _force_aware(
                components.manager.stop(drain_timeout=5.0),
                force_event,
            )
        if delivery_started:
            await _force_aware(components.delivery.stop(), force_event)
        if transport_connected:
            await _force_aware(components.transport.disconnect(), force_event)
        await _force_aware(components.runtime.aclose(), force_event)


async def _force_aware(operation, force_event: asyncio.Event) -> None:
    """让第二信号只取消当前阻塞清理，并继续释放后续资源。"""
    task = asyncio.create_task(operation)
    force = asyncio.create_task(force_event.wait())
    done, _ = await asyncio.wait((task, force), return_when=asyncio.FIRST_COMPLETED)
    if force in done and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        force_event.clear()
    else:
        await task
    force.cancel()
    await asyncio.gather(force, return_exceptions=True)


async def _create_components(
    config: AppConfig,
    paths: StatePaths,
    credentials: GatewayCredentials,
) -> GatewayComponents:
    """装配唯一 Runtime、Manager、Transport、能力、审批和 DeliveryWorker。"""
    runtime: AgentRuntime = create_runtime(config, paths, credentials.model_api_key)
    try:
        database = Database(paths.database)
        manager = create_channel_manager(config, paths, runtime)
        approval_controller = ChannelApprovalController(
            owner_open_id=config.channels.feishu.owner_open_id,
            approvals=ApprovalRepository(database),
            service=runtime.service,
        )
        deliveries = DeliveryRepository(database)
        transport: FeishuTransport

        async def on_card_action(
            actor_open_id: str,
            value: Any,
            chat_id: str,
            message_id: str,
        ) -> None:
            """把按钮决定交给 Core，并用 durable Delivery 发送 continuation。"""
            outcome = await approval_controller.handle_card_action(
                user_id=runtime.owner_id,
                actor_open_id=actor_open_id,
                value=value,
            )
            visible = outcome.result.content if outcome.result is not None else outcome.notice
            if outcome.result is not None and outcome.result.message_id is not None:
                deliveries.create_parts(
                    message_id=outcome.result.message_id,
                    channel="feishu",
                    account_id=config.channels.feishu.account_id,
                    external_conversation_id=chat_id,
                    reply_to_message_id=message_id,
                    kind="message",
                    contents=split_message(
                        outcome.result.content,
                        max_chars=config.channels.feishu.message_max_chars,
                    ),
                )
            if isinstance(visible, str) and visible:
                await _best_effort_card_status(transport, message_id, visible)

        transport = FeishuTransport(
            config.channels.feishu,
            app_id=credentials.app_id,
            app_secret=credentials.app_secret,
            on_inbound=manager.receive,
            on_card_action=on_card_action,
        )
        manager.attach_approvals(approval_controller)
        manager.attach_capabilities(
            ChannelCapabilities(
                transport=transport,
                streaming_card=config.channels.feishu.streaming_card,
                max_visible_chars=config.channels.feishu.message_max_chars,
            )
        )
        delivery = DeliveryWorker(
            transport=transport,
            repository=deliveries,
            channel="feishu",
            account_id=config.channels.feishu.account_id,
            message_max_chars=config.channels.feishu.message_max_chars,
        )
        return GatewayComponents(
            runtime=runtime,
            manager=manager,
            delivery=delivery,
            transport=transport,
            account_id=config.channels.feishu.account_id,
        )
    except Exception:
        await runtime.aclose()
        raise


async def _best_effort_card_status(
    transport: FeishuTransport,
    message_id: str,
    visible: str,
) -> None:
    """更新审批卡为有限可见状态；失败不影响 Core 终态或文本 fallback。"""
    text = visible[:2000]
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "green",
            "title": {"tag": "plain_text", "content": "MiniClaw 审批已处理"},
        },
        "elements": [{"tag": "markdown", "content": text}],
    }
    try:
        await transport.update_card(message_id, card)
    except Exception:
        return
