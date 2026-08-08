"""Telegram 纯 Adapter 与 official python-telegram-bot Transport。"""

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
from miniclaw.channels.observability import ChannelObserver
from miniclaw.config import TelegramConfig
from miniclaw.storage.channels import StoredInboundEvent

_SIGNED_64_MIN = -(2**63)
_SIGNED_64_MAX = 2**63 - 1
_CONVERSATION_KEY = re.compile(
    r"chat:(-?[1-9][0-9]*)(?::topic:([1-9][0-9]*))?\Z"
)
_MESSAGE_KEY = re.compile(r"chat:(-?[1-9][0-9]*):message:([1-9][0-9]*)\Z")
_MULTIPART_PREFIX = re.compile(r"\[([1-9][0-9]*)/([1-9][0-9]*)\] ")


class TelegramMessageView(Protocol):
    """描述 Adapter 允许从 official SDK 边界接收的有限字段。"""

    update_id: int
    message_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str | None
    date: datetime
    is_bot: bool
    is_service: bool
    is_edited: bool
    mentioned_bot: bool
    replied_to_bot: bool
    topic_id: int | None
    bot_mention_spans: tuple[tuple[int, int], ...]


class TelegramAdapter:
    """把不可信 Telegram Update 视图转换为 MiniClaw 标准消息。"""

    def __init__(self, config: TelegramConfig, *, bot_user_id: int) -> None:
        """冻结白名单和当前 Bot 身份；构造不读取网络或 SDK。"""
        if not _positive_id(bot_user_id):
            raise ValueError("invalid Telegram bot identity")
        self._config = config
        self._bot_user_id = bot_user_id
        self._allowed_user_ids = frozenset(config.allowed_user_ids)
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)

    def __repr__(self) -> str:
        """只暴露本地账号，不显示 Bot/user/chat 平台标识。"""
        return f"TelegramAdapter(account_id={self._config.account_id!r})"

    def normalize(
        self,
        message: TelegramMessageView,
    ) -> InboundMessage | IgnoredInbound:
        """按稳定顺序 fail-closed 校验并生成平台无关入站消息。"""
        if message.is_bot:
            return IgnoredInbound("bot_message")
        if message.is_service:
            return IgnoredInbound("service_message")
        if message.is_edited:
            return IgnoredInbound("edited_message")
        if message.text is None:
            return IgnoredInbound("unsupported_message")
        if not self._valid_shape(message):
            return IgnoredInbound("invalid_message")
        if message.user_id not in self._allowed_user_ids:
            return IgnoredInbound("user_not_allowed")

        chat_type: str
        if message.chat_type == "private":
            chat_type = "p2p"
        elif message.chat_type in {"group", "supergroup"}:
            chat_type = "group"
            denied = self._validate_group(message)
            if denied is not None:
                return denied
        else:
            return IgnoredInbound("invalid_message")

        text = _remove_bot_mentions(message.text, message.bot_mention_spans)
        if text is None:
            return IgnoredInbound("invalid_message")
        text = sanitize_inbound_text(text).strip()
        if not text:
            return IgnoredInbound("empty_message")
        if len(text) > self._config.message_max_chars:
            return IgnoredInbound("message_too_large")

        message_key = f"chat:{message.chat_id}:message:{message.message_id}"
        conversation_key = f"chat:{message.chat_id}"
        if message.topic_id is not None:
            conversation_key += f":topic:{message.topic_id}"
        return InboundMessage(
            channel="telegram",
            account_id=self._config.account_id,
            event_id=f"update:{message.update_id}",
            message_id=message_key,
            external_user_id=str(message.user_id),
            external_conversation_id=conversation_key,
            chat_type=chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=message_key,
            received_at=message.date.astimezone(UTC),
        )

    def _valid_shape(self, message: TelegramMessageView) -> bool:
        """校验 Telegram signed IDs、时间和 topic，不接受 bool-as-int。"""
        return (
            _nonnegative_id(message.update_id)
            and _positive_id(message.message_id)
            and _positive_id(message.user_id)
            and _signed_id(message.chat_id)
            and message.chat_id != 0
            and (
                message.topic_id is None
                or _positive_id(message.topic_id)
            )
            and isinstance(message.date, datetime)
            and message.date.tzinfo is not None
            and isinstance(message.bot_mention_spans, tuple)
        )

    def _validate_group(
        self,
        message: TelegramMessageView,
    ) -> IgnoredInbound | None:
        """群聊需要显式开关、chat allowlist 与 Bot addressing。"""
        if not self._config.allow_group_mentions:
            return IgnoredInbound("group_disabled")
        if message.chat_id not in self._allowed_chat_ids:
            return IgnoredInbound("chat_not_allowed")
        if not message.mentioned_bot and not message.replied_to_bot:
            return IgnoredInbound("bot_not_addressed")
        return None


class TelegramApplicationFacade(Protocol):
    """隔离 MiniClaw 与 python-telegram-bot 具体对象图。"""

    async def initialize(self) -> None: ...

    async def get_me(self) -> Any: ...

    async def start_polling(
        self,
        callback: Callable[[Any], Awaitable[None]],
        *,
        allowed_updates: tuple[str, ...],
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop_polling(self) -> None: ...

    async def stop(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def send_message(self, **values: Any) -> int: ...

    async def edit_message(self, **values: Any) -> int | bool: ...

    async def send_typing(self, **values: Any) -> bool: ...


@dataclass(frozen=True, slots=True, repr=False)
class _OfficialTelegramMessageView:
    """从 SDK Update 复制出的唯一安全、短生命周期视图。"""

    update_id: int
    message_id: int
    user_id: int
    chat_id: int
    chat_type: str
    text: str | None
    date: datetime
    is_bot: bool
    is_service: bool
    is_edited: bool
    mentioned_bot: bool
    replied_to_bot: bool
    topic_id: int | None
    bot_mention_spans: tuple[tuple[int, int], ...]

    def __repr__(self) -> str:
        return "_OfficialTelegramMessageView(redacted=True)"


class TelegramTransport:
    """通过 official SDK long polling 实现 Telegram ChannelTransport。"""

    def __init__(
        self,
        config: TelegramConfig,
        *,
        token: str,
        on_inbound: Callable[[InboundMessage], Awaitable[None]],
        application_factory: Callable[[str], TelegramApplicationFacade] | None = None,
        typing_renew_interval: float = 4.0,
        observer: ChannelObserver | None = None,
    ) -> None:
        """创建单 Bot runtime；Token 只传给 SDK facade，不保存在实例字段。"""
        if not isinstance(token, str) or not token.strip():
            raise ChannelTransportError("telegram_auth_failed")
        if (
            type(typing_renew_interval) not in {int, float}
            or typing_renew_interval <= 0
        ):
            raise ValueError("Telegram typing interval must be positive")
        factory = application_factory or _default_application_factory
        try:
            self._application = factory(token)
        except (ImportError, ModuleNotFoundError):
            raise ChannelTransportError("telegram_sdk_missing") from None
        self._config = config
        self._on_inbound = on_inbound
        self._observer = observer
        self._typing_renew_interval = float(typing_renew_interval)
        self._adapter: TelegramAdapter | None = None
        self._bot_user_id: int | None = None
        self._bot_username: str | None = None
        self._accepting = False
        self._initialized = False
        self._polling = False
        self._started = False
        self._connection_state = "disconnected"
        self._stop_polling_task: asyncio.Task[None] | None = None
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}

    def __repr__(self) -> str:
        return f"TelegramTransport(account_id={self._config.account_id!r})"

    @property
    def connection_state(self) -> str:
        return self._connection_state

    async def connect(self) -> None:
        """验证 Bot 身份、注册 message-only polling，并在完全 ready 后开放入口。"""
        if self._started and self._polling:
            return
        self._set_state("connecting")
        try:
            await self._application.initialize()
            self._initialized = True
            identity = await self._application.get_me()
            bot_user_id = getattr(identity, "id", None)
            username = getattr(identity, "username", None)
            if not _positive_id(bot_user_id):
                raise ChannelTransportError("telegram_auth_failed")
            self._bot_user_id = bot_user_id
            self._bot_username = (
                username if isinstance(username, str) and username else None
            )
            self._adapter = TelegramAdapter(self._config, bot_user_id=bot_user_id)
            self._polling = True
            await self._application.start_polling(
                self._handle_update,
                allowed_updates=("message",),
            )
            await self._application.start()
            self._started = True
        except Exception as error:
            mapped = (
                error
                if isinstance(error, ChannelTransportError)
                else _telegram_error(error, operation="poll")
            )
            await self._cleanup_after_connect_failure()
            self._set_state("failed", error_code=mapped.code)
            raise mapped from None
        self._accepting = True
        self._set_state("connected")

    def stop_receiving(self) -> None:
        """同步关闭 admission，并立即安排 updater.stop。"""
        self._accepting = False
        if self._polling and self._stop_polling_task is None:
            self._stop_polling_task = asyncio.create_task(
                self._stop_polling(),
                name="telegram-stop-polling",
            )

    async def disconnect(self) -> None:
        """幂等停止 polling/Application 并取消所有 typing renewal。"""
        self._accepting = False
        self._set_state("stopping")
        await self._cancel_all_typing()
        if self._polling:
            self.stop_receiving()
        if self._stop_polling_task is not None:
            await asyncio.gather(self._stop_polling_task, return_exceptions=True)
            self._stop_polling_task = None
        if self._started:
            try:
                await self._application.stop()
            finally:
                self._started = False
        if self._initialized:
            try:
                await self._application.shutdown()
            finally:
                self._initialized = False
        self._adapter = None
        self._bot_user_id = None
        self._bot_username = None
        self._set_state("disconnected")

    async def send(
        self,
        message: OutboundMessage,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """发送 plain text；Telegram 不支持 client idempotency，key 仅留在 Outbox。"""
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        chat_id, topic_id = _parse_conversation_key(
            message.external_conversation_id
        )
        reply_to = _parse_reply_key(message.reply_to_message_id, chat_id)
        match = _MULTIPART_PREFIX.match(message.content)
        if match is not None and int(match.group(1)) > 1:
            reply_to = None
        return await self._send_text(
            chat_id=chat_id,
            topic_id=topic_id,
            reply_to_message_id=reply_to,
            text=message.content,
        )

    async def start_typing(self, event: StoredInboundEvent) -> str | None:
        """立即发送一次 typing，并启动有界 renewal task。"""
        chat_id, topic_id = _parse_conversation_key(event.external_conversation_id)
        try:
            await self._application.send_typing(
                chat_id=chat_id,
                message_thread_id=topic_id,
            )
        except Exception as error:
            raise _telegram_error(error, operation="typing") from None
        if len(self._typing_tasks) >= 256:
            oldest_token = next(iter(self._typing_tasks))
            oldest = self._typing_tasks.pop(oldest_token)
            oldest.cancel()
        token = uuid4().hex
        self._typing_tasks[token] = asyncio.create_task(
            self._renew_typing(chat_id, topic_id),
            name="telegram-typing-renewal",
        )
        return token

    async def stop_typing(self, token: str | None) -> None:
        """取消 opaque token 对应的 renewal；重复停止安全无副作用。"""
        if token is None:
            return
        task = self._typing_tasks.pop(token, None)
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def create_progress(
        self,
        event: StoredInboundEvent,
        text: str,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """发送第一帧普通 preview 消息。"""
        if not idempotency_key:
            raise ValueError("idempotency key must not be empty")
        chat_id, topic_id = _parse_conversation_key(event.external_conversation_id)
        reply_to = _parse_reply_key(event.reply_to_message_id, chat_id)
        return await self._send_text(
            chat_id=chat_id,
            topic_id=topic_id,
            reply_to_message_id=reply_to,
            text=_bounded_telegram_text(f"⏳ {text}"),
        )

    async def update_progress(
        self,
        platform_message_id: str,
        text: str,
        *,
        incomplete: bool,
        completed: bool,
    ) -> SendReceipt:
        """edit 同一 preview；not-modified 被视为幂等成功。"""
        chat_id, message_id = _parse_message_key(platform_message_id)
        if completed:
            visible = "✅ 回复完成，最终内容见下一条消息"
        elif incomplete:
            visible = _bounded_telegram_text(f"⚠️ 回复未完成\n\n{text}")
        else:
            visible = _bounded_telegram_text(f"⏳ {text}")
        try:
            result = await self._application.edit_message(
                chat_id=chat_id,
                message_id=message_id,
                text=visible,
                link_preview_enabled=False,
            )
        except Exception as error:
            if _is_message_not_modified(error):
                return SendReceipt(platform_message_id)
            raise _telegram_error(error, operation="progress") from None
        if result is False:
            raise ChannelTransportError("telegram_progress_failed")
        return SendReceipt(platform_message_id)

    async def _send_text(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        reply_to_message_id: int | None,
        text: str,
    ) -> SendReceipt:
        if not text or len(text) > self._config.message_max_chars:
            raise ChannelTransportError("telegram_format_error")
        try:
            message_id = await self._application.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=topic_id,
                link_preview_enabled=False,
            )
        except Exception as error:
            raise _telegram_error(error, operation="send") from None
        if not _positive_id(message_id):
            raise ChannelTransportError("telegram_delivery_unknown", unknown=True)
        return SendReceipt(f"chat:{chat_id}:message:{message_id}")

    async def _handle_update(self, update: Any) -> None:
        """ready gate 后立即复制窄字段；原始 Update 不跨 await 传入 Core。"""
        if not self._accepting or self._adapter is None or self._bot_user_id is None:
            return
        view = _telegram_message_view(
            update,
            bot_user_id=self._bot_user_id,
            bot_username=self._bot_username,
        )
        del update
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

    async def _renew_typing(self, chat_id: int, topic_id: int | None) -> None:
        """固定间隔续发 action；首次异常即结束，避免后台异常风暴。"""
        while True:
            await asyncio.sleep(self._typing_renew_interval)
            try:
                await self._application.send_typing(
                    chat_id=chat_id,
                    message_thread_id=topic_id,
                )
            except Exception:
                return

    async def _stop_polling(self) -> None:
        try:
            await self._application.stop_polling()
        finally:
            self._polling = False

    async def _cleanup_after_connect_failure(self) -> None:
        self._accepting = False
        if self._polling:
            try:
                await self._application.stop_polling()
            except Exception:
                pass
            self._polling = False
        if self._started:
            try:
                await self._application.stop()
            except Exception:
                pass
            self._started = False
        if self._initialized:
            try:
                await self._application.shutdown()
            except Exception:
                pass
            self._initialized = False

    async def _cancel_all_typing(self) -> None:
        tasks = tuple(self._typing_tasks.values())
        self._typing_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _set_state(self, state: str, *, error_code: str | None = None) -> None:
        self._connection_state = state
        if self._observer is None:
            return
        try:
            self._observer.transport_state(
                channel="telegram",
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
                channel="telegram",
                account_id=self._config.account_id,
                external_message_id=_opaque_id(str(message_id)),
                external_conversation_id=None,
                status="ignored",
                reason=reason,
            )
        except Exception:
            pass


class _PtbApplicationFacade:
    """对 python-telegram-bot 22.x 的最小官方 API 包装。"""

    def __init__(self, token: str) -> None:
        from telegram.ext import Application

        self._application = Application.builder().token(token).build()
        self._handler: Any | None = None

    async def initialize(self) -> None:
        await self._application.initialize()

    async def get_me(self) -> Any:
        return await self._application.bot.get_me()

    async def start_polling(
        self,
        callback: Callable[[Any], Awaitable[None]],
        *,
        allowed_updates: tuple[str, ...],
    ) -> None:
        from telegram.ext import MessageHandler, filters

        async def handler(update: Any, context: Any) -> None:
            del context
            await callback(update)

        self._handler = MessageHandler(filters.TEXT, handler)
        self._application.add_handler(self._handler)
        updater = self._application.updater
        if updater is None:
            raise RuntimeError("telegram updater is unavailable")
        await updater.start_polling(allowed_updates=allowed_updates)

    async def start(self) -> None:
        await self._application.start()

    async def stop_polling(self) -> None:
        updater = self._application.updater
        if updater is not None and updater.running:
            await updater.stop()
        if self._handler is not None:
            self._application.remove_handler(self._handler)
            self._handler = None

    async def stop(self) -> None:
        if self._application.running:
            await self._application.stop()

    async def shutdown(self) -> None:
        await self._application.shutdown()

    async def send_message(self, **values: Any) -> int:
        message = await self._application.bot.send_message(
            chat_id=values["chat_id"],
            text=values["text"],
            parse_mode=None,
            disable_web_page_preview=not values["link_preview_enabled"],
            reply_to_message_id=values["reply_to_message_id"],
            message_thread_id=values["message_thread_id"],
        )
        return int(message.message_id)

    async def edit_message(self, **values: Any) -> int | bool:
        result = await self._application.bot.edit_message_text(
            chat_id=values["chat_id"],
            message_id=values["message_id"],
            text=values["text"],
            parse_mode=None,
            disable_web_page_preview=not values["link_preview_enabled"],
        )
        return result if isinstance(result, bool) else int(result.message_id)

    async def send_typing(self, **values: Any) -> bool:
        return await self._application.bot.send_chat_action(
            chat_id=values["chat_id"],
            action="typing",
            message_thread_id=values["message_thread_id"],
        )


def _default_application_factory(token: str) -> TelegramApplicationFacade:
    return _PtbApplicationFacade(token)


def _telegram_message_view(
    update: Any,
    *,
    bot_user_id: int,
    bot_username: str | None,
) -> _OfficialTelegramMessageView | None:
    """立即从 official Update 复制 Adapter 所需字段，拒绝缺失对象。"""
    edited = getattr(update, "edited_message", None)
    message = getattr(update, "message", None) or edited
    if message is None:
        return None
    sender = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    text = getattr(message, "text", None)
    reply = getattr(message, "reply_to_message", None)
    reply_sender = getattr(reply, "from_user", None)
    spans = _bot_mention_spans(
        text,
        getattr(message, "entities", None),
        bot_user_id=bot_user_id,
        bot_username=bot_username,
    )
    return _OfficialTelegramMessageView(
        update_id=getattr(update, "update_id", None),
        message_id=getattr(message, "message_id", None),
        user_id=getattr(sender, "id", None),
        chat_id=getattr(chat, "id", None),
        chat_type=str(getattr(chat, "type", "") or ""),
        text=text if isinstance(text, str) else None,
        date=getattr(message, "date", None),
        is_bot=bool(getattr(sender, "is_bot", False)),
        is_service=_is_service_message(message),
        is_edited=edited is not None,
        mentioned_bot=bool(spans),
        replied_to_bot=getattr(reply_sender, "id", None) == bot_user_id,
        topic_id=getattr(message, "message_thread_id", None),
        bot_mention_spans=spans,
    )


def _bot_mention_spans(
    text: Any,
    entities: Any,
    *,
    bot_user_id: int,
    bot_username: str | None,
) -> tuple[tuple[int, int], ...]:
    """从 Telegram UTF-16 entity 中只挑出明确指向当前 Bot 的 spans。"""
    if not isinstance(text, str) or not isinstance(entities, (list, tuple)):
        return ()
    spans: list[tuple[int, int]] = []
    for entity in entities:
        kind = str(getattr(entity, "type", "") or "")
        span = _utf16_span(
            text,
            getattr(entity, "offset", None),
            getattr(entity, "length", None),
        )
        if span is None:
            continue
        start, end = span
        targets_bot = False
        if kind == "mention" and bot_username is not None:
            targets_bot = text[start:end].casefold() == f"@{bot_username}".casefold()
        elif kind == "text_mention":
            targets_bot = getattr(getattr(entity, "user", None), "id", None) == bot_user_id
        if targets_bot:
            spans.append(span)
    return tuple(spans)


def _utf16_span(text: str, offset: Any, length: Any) -> tuple[int, int] | None:
    """把 Telegram UTF-16 code-unit offset 严格转换为 Python 字符索引。"""
    if (
        type(offset) is not int
        or type(length) is not int
        or offset < 0
        or length <= 0
    ):
        return None
    boundaries = {0: 0}
    units = 0
    for index, character in enumerate(text, 1):
        units += len(character.encode("utf-16-le")) // 2
        boundaries[units] = index
    end_units = offset + length
    if offset not in boundaries or end_units not in boundaries:
        return None
    return boundaries[offset], boundaries[end_units]


def _is_service_message(message: Any) -> bool:
    """识别不会作为用户请求进入 Agent 的 Telegram service fields。"""
    values = (
        getattr(message, "new_chat_members", None),
        getattr(message, "left_chat_member", None),
        getattr(message, "group_chat_created", None),
        getattr(message, "supergroup_chat_created", None),
        getattr(message, "channel_chat_created", None),
        getattr(message, "migrate_to_chat_id", None),
        getattr(message, "migrate_from_chat_id", None),
    )
    return any(bool(value) for value in values)


def _parse_conversation_key(value: str) -> tuple[int, int | None]:
    match = _CONVERSATION_KEY.fullmatch(value)
    if match is None:
        raise ChannelTransportError("telegram_target_invalid")
    chat_id = int(match.group(1))
    topic_id = int(match.group(2)) if match.group(2) is not None else None
    if not _signed_id(chat_id) or chat_id == 0 or (
        topic_id is not None and not _positive_id(topic_id)
    ):
        raise ChannelTransportError("telegram_target_invalid")
    return chat_id, topic_id


def _parse_message_key(value: str) -> tuple[int, int]:
    match = _MESSAGE_KEY.fullmatch(value)
    if match is None:
        raise ChannelTransportError("telegram_target_invalid")
    chat_id = int(match.group(1))
    message_id = int(match.group(2))
    if not _signed_id(chat_id) or chat_id == 0 or not _positive_id(message_id):
        raise ChannelTransportError("telegram_target_invalid")
    return chat_id, message_id


def _parse_reply_key(value: str, expected_chat_id: int) -> int:
    chat_id, message_id = _parse_message_key(value)
    if chat_id != expected_chat_id:
        raise ChannelTransportError("telegram_target_invalid")
    return message_id


def _bounded_telegram_text(text: str) -> str:
    return text[:4096]


def _is_message_not_modified(error: Exception) -> bool:
    return type(error).__name__ == "BadRequest" and "message is not modified" in str(
        error
    ).casefold()


def _telegram_error(error: Exception, *, operation: str) -> ChannelTransportError:
    """把 official 异常压缩为稳定码；绝不保留异常正文或平台目标。"""
    name = type(error).__name__
    if name == "RetryAfter":
        raw_retry_after = getattr(error, "retry_after", None)
        if isinstance(raw_retry_after, timedelta):
            retry_after = max(0.0, raw_retry_after.total_seconds())
        elif type(raw_retry_after) in {int, float} and raw_retry_after >= 0:
            retry_after = float(raw_retry_after)
        else:
            retry_after = None
        return ChannelTransportError(
            "telegram_rate_limited",
            retryable=True,
            retry_after=retry_after,
        )
    if name in {"InvalidToken", "Unauthorized"}:
        return ChannelTransportError("telegram_auth_failed")
    if name == "Forbidden":
        return ChannelTransportError("telegram_permission_denied")
    if name == "TimedOut":
        if operation == "send":
            return ChannelTransportError(
                "telegram_delivery_unknown",
                unknown=True,
            )
        return ChannelTransportError(f"telegram_{operation}_failed", retryable=True)
    if name == "NetworkError":
        return ChannelTransportError(f"telegram_{operation}_failed", retryable=True)
    if name == "BadRequest":
        code = (
            "telegram_format_error"
            if operation == "send"
            else f"telegram_{operation}_failed"
        )
        return ChannelTransportError(code)
    return ChannelTransportError(f"telegram_{operation}_failed")


def _opaque_id(value: str) -> str:
    return "message:" + hashlib.sha256(value.encode()).hexdigest()[:12]


def _remove_bot_mentions(
    text: str,
    spans: tuple[tuple[int, int], ...],
) -> str | None:
    """只删除 SDK mapper 已确认指向当前 Bot 的合法、不重叠 spans。"""
    normalized: list[tuple[int, int]] = []
    for span in spans:
        if (
            not isinstance(span, tuple)
            or len(span) != 2
            or type(span[0]) is not int
            or type(span[1]) is not int
        ):
            return None
        start, end = span
        if start < 0 or end <= start or end > len(text):
            return None
        normalized.append((start, end))
    normalized.sort()
    if any(
        left[1] > right[0]
        for left, right in zip(normalized, normalized[1:], strict=False)
    ):
        return None
    result = text
    for start, end in reversed(normalized):
        result = result[:start] + result[end:]
    return result


def _signed_id(value: object) -> bool:
    return type(value) is int and _SIGNED_64_MIN <= value <= _SIGNED_64_MAX


def _positive_id(value: object) -> bool:
    return _signed_id(value) and value > 0  # type: ignore[operator]


def _nonnegative_id(value: object) -> bool:
    return _signed_id(value) and value >= 0  # type: ignore[operator]
