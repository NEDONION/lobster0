"""三平台 Gateway preflight 与 single-runtime multi-pipeline lifecycle。"""

import asyncio
import importlib.util
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Protocol

from miniclaw.config import AppConfig

type ChannelName = Literal["feishu", "telegram", "discord"]
type ChannelRuntimeState = Literal[
    "created",
    "starting",
    "ready",
    "degraded",
    "stopping",
    "stopped",
]

_CHANNEL_ORDER: tuple[ChannelName, ...] = ("feishu", "telegram", "discord")
_SDK_MODULES: Mapping[ChannelName, str] = MappingProxyType(
    {
        "feishu": "lark_channel",
        "telegram": "telegram",
        "discord": "discord",
    }
)


class GatewayConfigError(ValueError):
    """表示 Gateway 在创建 Runtime 或网络对象前发现的静态错误。"""


@dataclass(frozen=True, slots=True, repr=False)
class GatewaySecrets:
    """短暂保存 preflight 通过后的 secret；repr 只列平台名。"""

    model_api_key: str
    channel_tokens: Mapping[str, str]
    feishu_app_id: str = ""

    def __repr__(self) -> str:
        names = ",".join(sorted(self.channel_tokens))
        return f"GatewaySecrets(configured={names})"


class RuntimeComponent(Protocol):
    async def aclose(self) -> None: ...


class ManagerComponent(Protocol):
    async def start(self) -> None: ...

    async def stop(self, *, drain_timeout: float = 5.0) -> None: ...


class DeliveryComponent(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...


class TransportComponent(Protocol):
    async def connect(self) -> None: ...

    def stop_receiving(self) -> None: ...

    async def disconnect(self) -> None: ...


@dataclass(slots=True)
class ChannelRuntime:
    """保存一条平台 pipeline 及其精确启动层级。"""

    channel: str
    account_id: str
    manager: ManagerComponent
    delivery: DeliveryComponent
    transport: TransportComponent
    state: ChannelRuntimeState = "created"
    observer: Any | None = field(default=None, repr=False)
    _transport_connected: bool = field(default=False, init=False, repr=False)
    _delivery_started: bool = field(default=False, init=False, repr=False)
    _manager_started: bool = field(default=False, init=False, repr=False)

    def observe(self, state: str, *, error_code: str | None = None) -> None:
        """best-effort 记录 supervisor 状态，不让 Audit 失败改变 lifecycle。"""
        callback = getattr(self.observer, "supervisor", None)
        if not callable(callback):
            return
        try:
            callback(
                channel=self.channel,
                account_id=self.account_id,
                state=state,
                error_code=error_code,
            )
        except Exception:
            pass


@dataclass(slots=True)
class GatewaySupervisor:
    """复用一个 AgentRuntime，管理按平台隔离的 durable pipelines。"""

    runtime: RuntimeComponent
    channels: tuple[ChannelRuntime, ...]
    monitor_interval: float = field(default=0.25, repr=False)
    _runtime_closed: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.channels:
            raise ValueError("GatewaySupervisor requires at least one channel")
        if (
            type(self.monitor_interval) not in {int, float}
            or self.monitor_interval <= 0
        ):
            raise ValueError("supervisor monitor interval must be positive")
        keys = [(item.channel, item.account_id) for item in self.channels]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate channel runtime")

    async def start(self, *, ready: Callable[[str], None]) -> None:
        """按固定 pipeline 内顺序启动，所有平台 ready 后才发全局 ready。"""
        runtime_start = getattr(self.runtime, "astart", None)
        if callable(runtime_start):
            await runtime_start()
        for channel in self.channels:
            channel.state = "starting"
            try:
                await channel.transport.connect()
                channel._transport_connected = True
                await channel.delivery.start()
                channel._delivery_started = True
                await channel.manager.start()
                channel._manager_started = True
            except Exception:
                channel.state = "degraded"
                channel.observe("degraded", error_code="channel_start_failed")
                raise
            channel.state = "ready"
            channel.observe("ready")
        labels = ", ".join(
            f"{channel.channel}/{channel.account_id}" for channel in self.channels
        )
        ready(f"MiniClaw gateway ready: {labels}")

    async def run(
        self,
        *,
        shutdown_event: asyncio.Event,
        force_event: asyncio.Event,
        ready: Callable[[str], None],
    ) -> None:
        """启动后等待关停信号；任意失败路径都完整反向释放。"""
        try:
            await self.start(ready=ready)
            await self._monitor_until_shutdown(shutdown_event)
        finally:
            await self.shutdown(force_event=force_event)

    def report_degraded(self, channel_name: str, error_code: str) -> None:
        """把单平台运行期异常局部化，不停止其他 pipeline 或 Runtime。"""
        for channel in self.channels:
            if channel.channel == channel_name:
                if channel.state in {"ready", "degraded"}:
                    channel.state = "degraded"
                    channel.observe("degraded", error_code=error_code)
                return
        raise ValueError("unknown channel runtime")

    async def shutdown(self, *, force_event: asyncio.Event) -> None:
        """先关闭所有入口，再按反向平台/反向层级清理，Runtime 仅一次。"""
        if self._runtime_closed:
            return
        stop_background = getattr(self.runtime, "astop_background", None)
        if callable(stop_background):
            await _cleanup(stop_background(), force_event)
        for channel in self.channels:
            if channel._transport_connected:
                try:
                    channel.transport.stop_receiving()
                except Exception:
                    pass
        for channel in reversed(self.channels):
            if not any(
                (
                    channel._transport_connected,
                    channel._delivery_started,
                    channel._manager_started,
                )
            ):
                continue
            channel.state = "stopping"
            channel.observe("stopping")
            if channel._manager_started:
                await _cleanup(
                    channel.manager.stop(drain_timeout=5.0),
                    force_event,
                )
                channel._manager_started = False
            if channel._delivery_started:
                await _cleanup(channel.delivery.stop(), force_event)
                channel._delivery_started = False
            if channel._transport_connected:
                await _cleanup(channel.transport.disconnect(), force_event)
                channel._transport_connected = False
            channel.state = "stopped"
        await _cleanup(self.runtime.aclose(), force_event)
        self._runtime_closed = True

    async def _monitor_until_shutdown(self, shutdown_event: asyncio.Event) -> None:
        """只同步各 Transport 的公开连接状态，不重建 Runtime 或平台 Client。"""
        while not shutdown_event.is_set():
            for channel in self.channels:
                transport_state = getattr(
                    channel.transport,
                    "connection_state",
                    None,
                )
                if transport_state in {"failed", "degraded", "reconnecting"}:
                    if channel.state == "ready":
                        self.report_degraded(
                            channel.channel,
                            f"{channel.channel}_runtime_degraded",
                        )
                elif transport_state == "connected" and channel.state == "degraded":
                    channel.state = "ready"
                    channel.observe("ready")
            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=self.monitor_interval,
                )
            except TimeoutError:
                continue


def collect_enabled_channels(config: AppConfig) -> tuple[ChannelName, ...]:
    """以稳定平台顺序返回 enabled Channels，空集合 fail closed。"""
    enabled = tuple(
        name
        for name in _CHANNEL_ORDER
        if bool(getattr(config.channels, name).enabled)
    )
    if not enabled:
        raise GatewayConfigError("no_channels_enabled: all channels are disabled")
    return enabled


def validate_gateway_preflight(
    config: AppConfig,
    environ: Mapping[str, str],
    *,
    sdk_available: Mapping[str, bool] | None = None,
) -> GatewaySecrets:
    """一次验证所有 enabled 平台；不创建 DB、Runtime、SDK Client 或网络。"""
    enabled = collect_enabled_channels(config)
    model_api_key = _required_secret(environ, config.provider.api_key_env)
    availability = {
        channel: (
            bool(sdk_available.get(channel, False))
            if sdk_available is not None
            else importlib.util.find_spec(_SDK_MODULES[channel]) is not None
        )
        for channel in enabled
    }
    tokens: dict[str, str] = {}
    feishu_app_id = ""
    for channel in enabled:
        selected = getattr(config.channels, channel)
        if not availability[channel]:
            display = {"feishu": "Feishu", "telegram": "Telegram", "discord": "Discord"}[
                channel
            ]
            raise GatewayConfigError(f"official {display} SDK is not installed")
        _validate_owner_relation(channel, selected)
        if channel == "feishu":
            feishu_app_id = _required_secret(environ, selected.app_id_env)
            tokens[channel] = _required_secret(environ, selected.app_secret_env)
        else:
            tokens[channel] = _required_secret(environ, selected.bot_token_env)
    keys = tuple((channel, getattr(config.channels, channel).account_id) for channel in enabled)
    if len(set(keys)) != len(keys):
        raise GatewayConfigError("duplicate_channel_account")
    return GatewaySecrets(
        model_api_key=model_api_key,
        channel_tokens=MappingProxyType(tokens),
        feishu_app_id=feishu_app_id,
    )


def _validate_owner_relation(channel: str, selected: Any) -> None:
    if channel == "feishu":
        if (
            not selected.owner_open_id
            or selected.owner_open_id not in selected.allowed_open_ids
        ):
            raise GatewayConfigError("feishu_owner_not_allowed")
        return
    if (
        type(selected.owner_user_id) is not int
        or selected.owner_user_id <= 0
        or selected.owner_user_id not in selected.allowed_user_ids
    ):
        raise GatewayConfigError(f"{channel}_owner_not_allowed")


def _required_secret(environ: Mapping[str, str], name: str) -> str:
    value = str(environ.get(name, "")).strip()
    if not value:
        raise GatewayConfigError(f"{name} is not configured")
    return value


async def _cleanup(operation: Any, force_event: asyncio.Event) -> None:
    """单项清理失败/被第二信号取消后继续后续平台，不泄露异常原文。"""
    try:
        await _force_aware(operation, force_event)
    except Exception:
        return


async def _force_aware(operation: Any, force_event: asyncio.Event) -> None:
    task = asyncio.create_task(operation)
    force = asyncio.create_task(force_event.wait())
    done, _ = await asyncio.wait((task, force), return_when=asyncio.FIRST_COMPLETED)
    if force in done:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        force_event.clear()
    else:
        await task
    force.cancel()
    await asyncio.gather(force, return_exceptions=True)
