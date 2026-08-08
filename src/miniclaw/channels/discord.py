"""Discord 纯 Adapter 与 official discord.py Gateway Transport。"""

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from miniclaw.channels.base import (
    ChannelTransportError,
    IgnoredInbound,
    InboundMessage,
    OutboundMessage,
    SendReceipt,
    sanitize_inbound_text,
)
from miniclaw.channels.experience import ProgressReceipt
from miniclaw.channels.feishu_cards import render_compact_progress
from miniclaw.channels.observability import ChannelObserver
from miniclaw.channels.progress import AgentProgress
from miniclaw.config import DiscordConfig
from miniclaw.storage.channels import StoredInboundEvent

_SNOWFLAKE_MAX = 2**64 - 1
_DM_CONVERSATION = re.compile(r"channel:([1-9][0-9]*)\Z")
_GUILD_CONVERSATION = re.compile(
    r"guild:([1-9][0-9]*):channel:([1-9][0-9]*)(?::thread:([1-9][0-9]*))?\Z"
)
_PLATFORM_MESSAGE = re.compile(
    r"channel:([1-9][0-9]*):message:([1-9][0-9]*)\Z"
)
_MULTIPART_PREFIX = re.compile(r"\[([1-9][0-9]*)/([1-9][0-9]*)\] ")


class DiscordMessageView(Protocol):
    """描述 Adapter 可接收的有限 Discord Message 字段。"""

    message_id: int
    author_id: int
    channel_id: int
    guild_id: int | None
    thread_id: int | None
    content: str
    created_at: datetime
    author_is_bot: bool
    webhook_id: int | None
    is_system: bool
    mentioned_bot: bool
    replied_to_bot: bool


class DiscordAdapter:
    """把不可信 Discord Message 视图转换为内部标准消息。"""

    def __init__(self, config: DiscordConfig, *, bot_user_id: int) -> None:
        """冻结白名单和 Bot snowflake，不读取 discord.py 或网络。"""
        if not _snowflake(bot_user_id):
            raise ValueError("invalid Discord bot identity")
        self._config = config
        self._bot_user_id = bot_user_id
        self._allowed_user_ids = frozenset(config.allowed_user_ids)
        self._allowed_guild_ids = frozenset(config.allowed_guild_ids)
        self._allowed_channel_ids = frozenset(config.allowed_channel_ids)

    def __repr__(self) -> str:
        """只显示本地账号，不显示任何平台 snowflake。"""
        return f"DiscordAdapter(account_id={self._config.account_id!r})"

    def normalize(
        self,
        message: DiscordMessageView,
    ) -> InboundMessage | IgnoredInbound:
        """按稳定顺序 fail-closed 校验 DM/Guild/Thread 消息。"""
        if message.author_is_bot:
            return IgnoredInbound("bot_message")
        if message.webhook_id is not None:
            return IgnoredInbound("webhook_message")
        if message.is_system:
            return IgnoredInbound("system_message")
        if not self._valid_shape(message):
            return IgnoredInbound("invalid_message")
        if message.author_id not in self._allowed_user_ids:
            return IgnoredInbound("user_not_allowed")

        if message.guild_id is None:
            chat_type = "p2p"
            conversation = f"channel:{message.channel_id}"
        else:
            chat_type = "group"
            denied = self._validate_guild(message)
            if denied is not None:
                return denied
            conversation = (
                f"guild:{message.guild_id}:channel:{message.channel_id}"
            )
            if message.thread_id is not None:
                conversation += f":thread:{message.thread_id}"

        text = sanitize_inbound_text(
            _remove_bot_mentions(message.content, self._bot_user_id)
        ).strip()
        if not text:
            return IgnoredInbound("empty_message")
        if len(text) > self._config.message_max_chars:
            return IgnoredInbound("message_too_large")
        snowflake = str(message.message_id)
        return InboundMessage(
            channel="discord",
            account_id=self._config.account_id,
            event_id=snowflake,
            message_id=snowflake,
            external_user_id=str(message.author_id),
            external_conversation_id=conversation,
            chat_type=chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=snowflake,
            received_at=message.created_at.astimezone(UTC),
        )

    def _valid_shape(self, message: DiscordMessageView) -> bool:
        """校验 unsigned 64-bit snowflake、Thread 关系与 aware time。"""
        return (
            _snowflake(message.message_id)
            and _snowflake(message.author_id)
            and _snowflake(message.channel_id)
            and (
                message.guild_id is None
                or _snowflake(message.guild_id)
            )
            and (
                message.thread_id is None
                or _snowflake(message.thread_id)
            )
            and not (message.thread_id is not None and message.guild_id is None)
            and isinstance(message.content, str)
            and isinstance(message.created_at, datetime)
            and message.created_at.tzinfo is not None
        )

    def _validate_guild(
        self,
        message: DiscordMessageView,
    ) -> IgnoredInbound | None:
        """Guild 需要开关、guild/channel allowlist 和明确 addressing。"""
        if not self._config.allow_guild_mentions:
            return IgnoredInbound("guild_disabled")
        if message.guild_id not in self._allowed_guild_ids:
            return IgnoredInbound("guild_not_allowed")
        if message.channel_id not in self._allowed_channel_ids:
            return IgnoredInbound("channel_not_allowed")
        if not message.mentioned_bot and not message.replied_to_bot:
            return IgnoredInbound("bot_not_addressed")
        return None


@dataclass(frozen=True, slots=True)
class DiscordIntents:
    """显式列出 MiniClaw 唯一允许启用的 Gateway intents。"""

    guilds: bool = True
    guild_messages: bool = True
    dm_messages: bool = True
    message_content: bool = True
    members: bool = False
    presences: bool = False
    reactions: bool = False
    typing: bool = False


class DiscordClientFacade(Protocol):
    """隔离 Core 与 discord.py Client/Message/Channel 对象图。"""

    user_id: int | None

    def set_handlers(
        self,
        *,
        on_ready: Callable[[], Awaitable[None]],
        on_message: Callable[[Any], Awaitable[None]],
        on_resumed: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> None: ...

    async def login(self) -> None: ...

    async def connect(self) -> None: ...

    async def close(self) -> None: ...

    async def send_message(self, **values: Any) -> int: ...

    async def edit_message(self, **values: Any) -> int | bool: ...

    async def begin_typing(self, target_id: int) -> Any: ...

    async def end_typing(self, handle: Any) -> None: ...


@dataclass(frozen=True, slots=True, repr=False)
class _OfficialDiscordMessageView:
    """从 discord.py Message 复制出的短生命周期安全视图。"""

    message_id: int
    author_id: int
    channel_id: int
    guild_id: int | None
    thread_id: int | None
    content: str
    created_at: datetime
    author_is_bot: bool
    webhook_id: int | None
    is_system: bool
    mentioned_bot: bool
    replied_to_bot: bool

    def __repr__(self) -> str:
        return "_OfficialDiscordMessageView(redacted=True)"


class DiscordTransport:
    """以单一 discord.py Client 实现 reconnectable Gateway 与安全发送。"""

    def __init__(
        self,
        config: DiscordConfig,
        *,
        token: str,
        on_inbound: Callable[[InboundMessage], Awaitable[None]],
        client_factory: (
            Callable[[str, DiscordIntents], DiscordClientFacade] | None
        ) = None,
        observer: ChannelObserver | None = None,
    ) -> None:
        """创建 facade 并注册回调；Token 只交给 SDK facade。"""
        if not isinstance(token, str) or not token.strip():
            raise ChannelTransportError("discord_auth_failed")
        intents = DiscordIntents()
        factory = client_factory or _default_client_factory
        try:
            self._client = factory(token, intents)
        except (ImportError, ModuleNotFoundError):
            raise ChannelTransportError("discord_sdk_missing") from None
        self._config = config
        self._on_inbound = on_inbound
        self._observer = observer
        self._adapter: DiscordAdapter | None = None
        self._bot_user_id: int | None = None
        self._ready = asyncio.Event()
        self._gateway_task: asyncio.Task[None] | None = None
        self._accepting = False
        self._closing = False
        self._closed = False
        self._connection_state = "disconnected"
        self._typing_handles: dict[str, Any] = {}
        self._client.set_handlers(
            on_ready=self._handle_ready,
            on_message=self._handle_message,
            on_resumed=self._handle_resumed,
            on_disconnect=self._handle_disconnect,
        )

    def __repr__(self) -> str:
        return f"DiscordTransport(account_id={self._config.account_id!r})"

    @property
    def connection_state(self) -> str:
        return self._connection_state

    async def connect(self) -> None:
        """login 后启动 reconnecting Gateway，并只在 READY 到达后返回。"""
        if self._gateway_task is not None and not self._gateway_task.done():
            return
        if self._closed:
            raise ChannelTransportError("discord_gateway_closed")
        self._closing = False
        self._ready.clear()
        self._set_state("connecting")
        try:
            await self._client.login()
            bot_user_id = self._client.user_id
            if not _snowflake(bot_user_id):
                raise ChannelTransportError("discord_auth_failed")
            self._bot_user_id = bot_user_id
            self._adapter = DiscordAdapter(self._config, bot_user_id=bot_user_id)
            self._gateway_task = asyncio.create_task(
                self._client.connect(),
                name="discord-gateway",
            )
            ready_task = asyncio.create_task(
                self._ready.wait(),
                name="discord-ready",
            )
            done, _ = await asyncio.wait(
                (self._gateway_task, ready_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if self._gateway_task in done:
                ready_task.cancel()
                await asyncio.gather(ready_task, return_exceptions=True)
                await self._gateway_task
                raise ChannelTransportError(
                    "discord_gateway_disconnected",
                    retryable=True,
                )
            ready_task.cancel()
            await asyncio.gather(ready_task, return_exceptions=True)
        except Exception as error:
            mapped = (
                error
                if isinstance(error, ChannelTransportError)
                else _discord_error(error, operation="gateway")
            )
            await self._close_client()
            self._set_state("failed", error_code=mapped.code)
            raise mapped from None
        self._accepting = True
        self._set_state("connected")
        if self._gateway_task is not None:
            self._gateway_task.add_done_callback(self._gateway_finished)

    def stop_receiving(self) -> None:
        """同步关闭 on_message admission；SDK Gateway 稍后由 disconnect 释放。"""
        self._accepting = False

    async def disconnect(self) -> None:
        """幂等退出 typing contexts、关闭 Client，并回收 Gateway task。"""
        self._accepting = False
        if self._closed:
            return
        self._closing = True
        self._set_state("stopping")
        await self._stop_all_typing()
        await self._close_client()
        self._adapter = None
        self._bot_user_id = None
        self._closed = True
        self._set_state("disconnected")

    async def send(
        self,
        message: OutboundMessage,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """发送禁用所有 mentions 的 plain text；local key 不冒充平台幂等。"""
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        target_id = _target_from_conversation(message.external_conversation_id)
        reply_to = _parse_snowflake_text(message.reply_to_message_id)
        match = _MULTIPART_PREFIX.match(message.content)
        if match is not None and int(match.group(1)) > 1:
            reply_to = None
        return await self._send_text(
            target_id=target_id,
            reply_to_message_id=reply_to,
            text=message.content,
        )

    async def start_typing(self, event: StoredInboundEvent) -> str | None:
        """进入 discord.py typing context，并以 opaque token 持有 cleanup handle。"""
        target_id = _target_from_conversation(event.external_conversation_id)
        try:
            handle = await self._client.begin_typing(target_id)
        except Exception as error:
            raise _discord_error(error, operation="typing") from None
        if handle is None:
            raise ChannelTransportError("discord_typing_failed")
        if len(self._typing_handles) >= 256:
            oldest_token = next(iter(self._typing_handles))
            oldest = self._typing_handles.pop(oldest_token)
            try:
                await self._client.end_typing(oldest)
            except Exception:
                pass
        token = uuid4().hex
        self._typing_handles[token] = handle
        return token

    async def stop_typing(self, token: str | None) -> None:
        """幂等退出 opaque token 对应的 SDK typing context。"""
        if token is None:
            return
        handle = self._typing_handles.pop(token, None)
        if handle is None:
            return
        try:
            await self._client.end_typing(handle)
        except Exception as error:
            raise _discord_error(error, operation="typing") from None

    async def create_progress(
        self,
        event: StoredInboundEvent,
        progress: AgentProgress,
        *,
        idempotency_key: str,
    ) -> ProgressReceipt:
        """创建结构化 progress；所有 mention token 都不能触发通知。"""
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        receipt = await self._send_text(
            target_id=_target_from_conversation(event.external_conversation_id),
            reply_to_message_id=_parse_snowflake_text(event.reply_to_message_id),
            text=_bounded_discord_text(render_compact_progress(progress)),
        )
        return ProgressReceipt(receipt.platform_message_id)

    async def update_progress(
        self,
        platform_message_id: str,
        progress: AgentProgress,
    ) -> ProgressReceipt:
        """edit 同一 progress，终态只作提示，durable final reply 仍独立发送。"""
        target_id, message_id = _parse_platform_message(platform_message_id)
        display = replace(progress, final_answer="")
        visible = render_compact_progress(display)
        if progress.status == "completed":
            visible += "\n\n最终内容见下一条消息"
        visible = _bounded_discord_text(visible)
        try:
            result = await self._client.edit_message(
                target_id=target_id,
                message_id=message_id,
                text=visible,
                suppress_mentions=True,
            )
        except Exception as error:
            raise _discord_error(error, operation="progress") from None
        if result is False:
            raise ChannelTransportError("discord_progress_failed")
        return ProgressReceipt(platform_message_id)

    async def _send_text(
        self,
        *,
        target_id: int,
        reply_to_message_id: int | None,
        text: str,
    ) -> SendReceipt:
        if not text or len(text) > self._config.message_max_chars:
            raise ChannelTransportError("discord_format_error")
        try:
            message_id = await self._client.send_message(
                target_id=target_id,
                reply_to_message_id=reply_to_message_id,
                text=text,
                suppress_mentions=True,
            )
        except Exception as error:
            raise _discord_error(error, operation="send") from None
        if not _snowflake(message_id):
            raise ChannelTransportError("discord_delivery_unknown", unknown=True)
        return SendReceipt(f"channel:{target_id}:message:{message_id}")

    async def _handle_ready(self) -> None:
        self._ready.set()

    async def _handle_message(self, message: Any) -> None:
        if not self._accepting or self._adapter is None or self._bot_user_id is None:
            return
        view = _discord_message_view(message, bot_user_id=self._bot_user_id)
        del message
        if view is None:
            return
        normalized = self._adapter.normalize(view)
        if isinstance(normalized, InboundMessage):
            try:
                await self._on_inbound(normalized)
            except Exception:
                self._observe_inbound(view.message_id, "channel_receive_failed")
            return
        self._observe_inbound(view.message_id, normalized.reason)

    async def _handle_resumed(self) -> None:
        if not self._closing:
            self._set_state("connected")

    async def _handle_disconnect(self) -> None:
        if not self._closing:
            self._set_state(
                "degraded",
                error_code="discord_gateway_disconnected",
            )

    def _gateway_finished(self, task: asyncio.Task[None]) -> None:
        if self._closing or task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        mapped = (
            ChannelTransportError(
                "discord_gateway_disconnected",
                retryable=True,
            )
            if error is None
            else _discord_error(error, operation="gateway")
        )
        self._accepting = False
        self._set_state("failed", error_code=mapped.code)

    async def _close_client(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass
        task = self._gateway_task
        self._gateway_task = None
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

    async def _stop_all_typing(self) -> None:
        handles = tuple(self._typing_handles.values())
        self._typing_handles.clear()
        for handle in handles:
            try:
                await self._client.end_typing(handle)
            except Exception:
                pass

    def _set_state(self, state: str, *, error_code: str | None = None) -> None:
        self._connection_state = state
        if self._observer is None:
            return
        try:
            self._observer.transport_state(
                channel="discord",
                account_id=self._config.account_id,
                state=state,
                error_code=error_code,
            )
        except Exception:
            pass

    def _observe_inbound(self, message_id: int, reason: str) -> None:
        if self._observer is None:
            return
        try:
            self._observer.inbound(
                channel="discord",
                account_id=self._config.account_id,
                external_message_id=_opaque_id(str(message_id)),
                external_conversation_id=None,
                status="ignored",
                reason=reason,
            )
        except Exception:
            pass


class _DiscordPyClientFacade:
    """对 discord.py 2.x Client 的最小官方 API 包装。"""

    def __init__(self, token: str, intents: DiscordIntents) -> None:
        import discord

        sdk_intents = discord.Intents.none()
        sdk_intents.guilds = intents.guilds
        sdk_intents.guild_messages = intents.guild_messages
        sdk_intents.dm_messages = intents.dm_messages
        sdk_intents.message_content = intents.message_content
        self._discord = discord
        self._client = discord.Client(intents=sdk_intents)
        self._token = token

    @property
    def user_id(self) -> int | None:
        user = self._client.user
        return None if user is None else int(user.id)

    def set_handlers(
        self,
        *,
        on_ready: Callable[[], Awaitable[None]],
        on_message: Callable[[Any], Awaitable[None]],
        on_resumed: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> None:
        async def sdk_on_ready() -> None:
            await on_ready()

        async def sdk_on_message(message: Any) -> None:
            await on_message(message)

        async def sdk_on_resumed() -> None:
            await on_resumed()

        async def sdk_on_disconnect() -> None:
            await on_disconnect()

        sdk_on_ready.__name__ = "on_ready"
        sdk_on_message.__name__ = "on_message"
        sdk_on_resumed.__name__ = "on_resumed"
        sdk_on_disconnect.__name__ = "on_disconnect"
        self._client.event(sdk_on_ready)
        self._client.event(sdk_on_message)
        self._client.event(sdk_on_resumed)
        self._client.event(sdk_on_disconnect)

    async def login(self) -> None:
        token, self._token = self._token, ""
        await self._client.login(token)

    async def connect(self) -> None:
        await self._client.connect(reconnect=True)

    async def close(self) -> None:
        if not self._client.is_closed():
            await self._client.close()

    async def send_message(self, **values: Any) -> int:
        channel = await self._channel(values["target_id"])
        reference = (
            None
            if values["reply_to_message_id"] is None
            else channel.get_partial_message(values["reply_to_message_id"])
        )
        message = await channel.send(
            values["text"],
            reference=reference,
            mention_author=False,
            allowed_mentions=self._discord.AllowedMentions.none(),
        )
        return int(message.id)

    async def edit_message(self, **values: Any) -> int | bool:
        channel = await self._channel(values["target_id"])
        message = channel.get_partial_message(values["message_id"])
        edited = await message.edit(
            content=values["text"],
            allowed_mentions=self._discord.AllowedMentions.none(),
        )
        return int(edited.id)

    async def begin_typing(self, target_id: int) -> Any:
        channel = await self._channel(target_id)
        context = channel.typing()
        await context.__aenter__()
        return context

    async def end_typing(self, handle: Any) -> None:
        await handle.__aexit__(None, None, None)

    async def _channel(self, target_id: int) -> Any:
        channel = self._client.get_channel(target_id)
        if channel is None:
            channel = await self._client.fetch_channel(target_id)
        return channel


def _default_client_factory(
    token: str,
    intents: DiscordIntents,
) -> DiscordClientFacade:
    return _DiscordPyClientFacade(token, intents)


def _discord_message_view(
    message: Any,
    *,
    bot_user_id: int,
) -> _OfficialDiscordMessageView | None:
    """立即从 official Message 复制有限标量，不让 SDK object 进入 Core。"""
    author = getattr(message, "author", None)
    channel = getattr(message, "channel", None)
    guild = getattr(message, "guild", None)
    if author is None or channel is None:
        return None
    actual_channel_id = getattr(channel, "id", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        parent_id = getattr(getattr(channel, "parent", None), "id", None)
    thread_id = actual_channel_id if parent_id is not None else None
    channel_id = parent_id if parent_id is not None else actual_channel_id
    mentions = getattr(message, "mentions", None)
    mentioned_bot = isinstance(mentions, (list, tuple)) and any(
        getattr(user, "id", None) == bot_user_id for user in mentions
    )
    reference = getattr(message, "reference", None)
    resolved = getattr(reference, "resolved", None)
    if resolved is None:
        resolved = getattr(reference, "cached_message", None)
    replied_to_bot = (
        getattr(getattr(resolved, "author", None), "id", None) == bot_user_id
    )
    is_system_method = getattr(message, "is_system", None)
    try:
        is_system = bool(is_system_method()) if callable(is_system_method) else False
    except Exception:
        is_system = True
    return _OfficialDiscordMessageView(
        message_id=getattr(message, "id", None),
        author_id=getattr(author, "id", None),
        channel_id=channel_id,
        guild_id=getattr(guild, "id", None),
        thread_id=thread_id,
        content=getattr(message, "content", None),
        created_at=getattr(message, "created_at", None),
        author_is_bot=bool(getattr(author, "bot", False)),
        webhook_id=getattr(message, "webhook_id", None),
        is_system=is_system,
        mentioned_bot=mentioned_bot,
        replied_to_bot=replied_to_bot,
    )


def _target_from_conversation(value: str) -> int:
    dm = _DM_CONVERSATION.fullmatch(value)
    if dm is not None:
        target_id = int(dm.group(1))
        if _snowflake(target_id):
            return target_id
        raise ChannelTransportError("discord_target_invalid")
    guild = _GUILD_CONVERSATION.fullmatch(value)
    if guild is None:
        raise ChannelTransportError("discord_target_invalid")
    values = tuple(int(item) if item is not None else None for item in guild.groups())
    guild_id, channel_id, thread_id = values
    if (
        not _snowflake(guild_id)
        or not _snowflake(channel_id)
        or (thread_id is not None and not _snowflake(thread_id))
    ):
        raise ChannelTransportError("discord_target_invalid")
    return thread_id if thread_id is not None else channel_id


def _parse_snowflake_text(value: str) -> int:
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        raise ChannelTransportError("discord_target_invalid")
    parsed = int(value)
    if not _snowflake(parsed):
        raise ChannelTransportError("discord_target_invalid")
    return parsed


def _parse_platform_message(value: str) -> tuple[int, int]:
    match = _PLATFORM_MESSAGE.fullmatch(value)
    if match is None:
        raise ChannelTransportError("discord_target_invalid")
    target_id, message_id = (int(item) for item in match.groups())
    if not _snowflake(target_id) or not _snowflake(message_id):
        raise ChannelTransportError("discord_target_invalid")
    return target_id, message_id


def _bounded_discord_text(text: str) -> str:
    return text[:2000]


def _discord_error(error: BaseException, *, operation: str) -> ChannelTransportError:
    """把 discord.py 异常压缩为稳定恢复属性，不复制 response body。"""
    name = type(error).__name__
    if name == "LoginFailure":
        return ChannelTransportError("discord_auth_failed")
    if name == "PrivilegedIntentsRequired":
        return ChannelTransportError("discord_intents_invalid")
    if name == "ConnectionClosed":
        code = getattr(error, "code", None)
        if code == 4004:
            return ChannelTransportError("discord_auth_failed")
        if code in {4013, 4014}:
            return ChannelTransportError("discord_intents_invalid")
        return ChannelTransportError(
            "discord_gateway_disconnected",
            retryable=True,
        )
    status = getattr(error, "status", None)
    if status == 429:
        raw_retry_after = getattr(error, "retry_after", None)
        retry_after = (
            float(raw_retry_after)
            if type(raw_retry_after) in {int, float} and raw_retry_after >= 0
            else None
        )
        return ChannelTransportError(
            "discord_rate_limited",
            retryable=True,
            retry_after=retry_after,
        )
    if status == 403:
        return ChannelTransportError("discord_forbidden")
    if status == 404:
        return ChannelTransportError("discord_target_not_found")
    if type(status) is int and status >= 500:
        return ChannelTransportError(f"discord_{operation}_failed", retryable=True)
    if isinstance(error, TimeoutError):
        if operation == "send":
            return ChannelTransportError(
                "discord_delivery_unknown",
                unknown=True,
            )
        return ChannelTransportError(f"discord_{operation}_failed", retryable=True)
    if isinstance(error, OSError):
        return ChannelTransportError(f"discord_{operation}_failed", retryable=True)
    return ChannelTransportError(f"discord_{operation}_failed")


def _opaque_id(value: str) -> str:
    return "message:" + hashlib.sha256(value.encode()).hexdigest()[:12]


def _remove_bot_mentions(content: str, bot_user_id: int) -> str:
    """只删除 Discord 当前 Bot 的 standard/nickname mention token。"""
    return content.replace(f"<@{bot_user_id}>", "").replace(
        f"<@!{bot_user_id}>",
        "",
    )


def _snowflake(value: object) -> bool:
    return type(value) is int and 0 < value <= _SNOWFLAKE_MAX
