"""飞书消息 Adapter 与 official lark-channel-sdk Transport。"""

import importlib
import re
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol
from uuid import uuid4

from lobster0.channels.base import (
    ChannelTransportError,
    IgnoredInbound,
    InboundMessage,
    OutboundMessage,
    SendReceipt,
    sanitize_inbound_text,
)
from lobster0.channels.experience import ProgressReceipt
from lobster0.channels.feishu_cards import render_agent_progress_card
from lobster0.channels.observability import ChannelObserver
from lobster0.channels.progress import AgentProgress
from lobster0.config import FeishuConfig
from lobster0.storage.channels import StoredInboundEvent

_MESSAGE_ID = re.compile(r"om_[A-Za-z0-9_-]{1,128}\Z")
_OPEN_ID = re.compile(r"ou_[A-Za-z0-9_-]{1,128}\Z")
_CHAT_ID = re.compile(r"oc_[A-Za-z0-9_-]{1,128}\Z")


class FeishuMessageView(Protocol):
    """描述官方 SDK InboundMessage 中被 Lobster0 使用的有限字段。"""

    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_type: str | None
    sender_is_bot: bool
    mentioned_bot: bool
    body_text: str
    raw_content_type: str
    parent_message_id: str
    resources: list
    create_time: datetime | str | int | None


class FeishuAdapter:
    """执行飞书白名单、消息类型和文本安全校验。"""

    def __init__(
        self,
        config: FeishuConfig,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))
        self._allowed_open_ids = frozenset(config.allowed_open_ids)
        self._allowed_chat_ids = frozenset(config.allowed_chat_ids)

    def normalize(
        self,
        message: FeishuMessageView,
    ) -> InboundMessage | IgnoredInbound:
        """把官方 SDK 消息变成内部消息，拒绝所有未明确允许的输入。"""
        if message.sender_is_bot or message.sender_type in {"app", "bot", "system"}:
            return IgnoredInbound("bot_message")
        if message.raw_content_type not in {"text", "post", "image"}:
            return IgnoredInbound("unsupported_message")
        if not self._valid_identifiers(message):
            return IgnoredInbound("invalid_message")
        if message.sender_id not in self._allowed_open_ids:
            return IgnoredInbound("sender_denied")
        if message.chat_type == "group":
            group_result = self._validate_group(message)
            if group_result is not None:
                return group_result
        elif message.chat_type != "p2p":
            return IgnoredInbound("unsupported_message")

        images = _cached_image_paths(message)
        text = sanitize_inbound_text(message.body_text).strip()
        if not text and images:
            # 只发图不配文字是常见用法；给一句中性提示，让这一轮不至于被当成空消息丢弃。
            text = "请看这张图片。"
        if not text:
            return IgnoredInbound("empty_message")
        if len(text) > self._config.message_max_chars:
            return IgnoredInbound("message_too_large")

        return InboundMessage(
            channel="feishu",
            account_id=self._config.account_id,
            event_id=message.event_id,
            message_id=message.message_id,
            external_user_id=message.sender_id,
            external_conversation_id=message.chat_id,
            chat_type=message.chat_type,
            message_type="text",
            text=text,
            reply_to_message_id=message.message_id,
            replied_to_message_id=_valid_message_id(message.parent_message_id),
            image_paths=images,
            received_at=_received_at(message.create_time, self._clock),
        )

    def _valid_identifiers(self, message: FeishuMessageView) -> bool:
        """校验进入持久化层的三类平台标识。"""
        return (
            _MESSAGE_ID.fullmatch(message.message_id) is not None
            and _OPEN_ID.fullmatch(message.sender_id) is not None
            and _CHAT_ID.fullmatch(message.chat_id) is not None
        )

    def _validate_group(self, message: FeishuMessageView) -> IgnoredInbound | None:
        """应用群聊开关、Chat 白名单和明确 mention 三道门。"""
        if not self._config.allow_group_mentions:
            return IgnoredInbound("group_disabled")
        if message.chat_id not in self._allowed_chat_ids:
            return IgnoredInbound("chat_denied")
        if not message.mentioned_bot:
            return IgnoredInbound("mention_required")
        return None


@dataclass(frozen=True, slots=True)
class _OfficialMessageView:
    """把 official SDK 对象压缩为 Adapter 需要的安全字段。"""

    event_id: str
    message_id: str
    chat_id: str
    chat_type: str
    sender_id: str
    sender_type: str | None
    sender_is_bot: bool
    mentioned_bot: bool
    body_text: str
    raw_content_type: str
    parent_message_id: str
    create_time: datetime | str | int | None


class FeishuTransport:
    """通过官方 Channel SDK 建立 WS 长连接并收发标准消息。"""

    def __init__(
        self,
        config: FeishuConfig,
        *,
        app_id: str,
        app_secret: str,
        on_inbound: Callable[[InboundMessage], Awaitable[None]],
        on_card_action: (
            Callable[[str, Any, str, str], Awaitable[None]] | None
        ) = None,
        sdk: ModuleType | Any | None = None,
        observer: ChannelObserver | None = None,
    ) -> None:
        """构造严格安全、默认关闭的单账号飞书 Transport。"""
        if not app_id or not app_secret:
            raise ValueError("Feishu credentials must not be empty")
        self._config = config
        self._adapter = FeishuAdapter(config)
        self._on_inbound = on_inbound
        self._on_card_action = on_card_action
        self._observer = observer
        self._sdk = sdk or importlib.import_module("lark_channel")
        self._unsubscribers: list[Callable[[], Any]] = []
        self._connected = False
        self._connection_state = "disconnected"
        self._typing_tokens: dict[str, tuple[str, str]] = {}
        self._channel = self._build_channel(app_id, app_secret)

    def __repr__(self) -> str:
        """只显示非秘密本地路由标识。"""
        return (
            "FeishuTransport("
            f"account_id={self._config.account_id!r}, domain={self._config.domain!r})"
        )

    async def connect(self) -> None:
        """注册回调并等待 official SDK 确认 WebSocket 就绪。"""
        if self._connected:
            return
        self._set_connection_state("connecting")
        self._unsubscribers.append(
            self._channel.on("message", self._handle_message)
        )
        if self._on_card_action is not None:
            self._unsubscribers.append(
                self._channel.on("cardAction", self._handle_card_action)
            )
        self._unsubscribers.extend(
            (
                self._channel.on("reconnecting", self._handle_reconnecting),
                self._channel.on("reconnected", self._handle_reconnected),
                self._channel.on("error", self._handle_sdk_error),
            )
        )
        try:
            connect_until_ready = getattr(self._channel, "connect_until_ready", None)
            if callable(connect_until_ready):
                await connect_until_ready()
            else:
                await self._channel.connect()
        except Exception as error:
            self._unsubscribe_handler()
            mapped = _transport_error(error)
            self._set_connection_state("failed", error_code=mapped.code)
            raise mapped from None
        self._connected = True
        self._set_connection_state("connected")

    async def disconnect(self) -> None:
        """先停止新回调，再优雅排空并断开 SDK。"""
        self._unsubscribe_handler()
        if not self._connected:
            return
        self._set_connection_state("stopping")
        self._connected = False
        try:
            await self._channel.disconnect()
        except Exception as error:
            mapped = _transport_error(error)
            self._set_connection_state("failed", error_code=mapped.code)
            raise mapped from None
        self._set_connection_state("disconnected")

    @property
    def connection_state(self) -> str:
        """返回不含 SDK 原始错误的当前连接状态快照。"""
        return self._connection_state

    def stop_receiving(self) -> None:
        """解除入站回调但保持连接，供 Gateway drain 已接收工作。"""
        self._unsubscribe_handler()

    async def send(
        self,
        message: OutboundMessage,
        *,
        idempotency_key: str,
    ) -> SendReceipt:
        """用 reply_to 与稳定 UUID 发送 Markdown 回复。"""
        options = self._send_options(
            reply_to_message_id=message.reply_to_message_id,
            idempotency_key=idempotency_key,
        )
        try:
            result = await self._channel.send(
                message.external_conversation_id,
                message.content,
                options,
            )
        except Exception as error:
            raise _transport_error(error) from None
        return _send_receipt(result)

    async def add_typing(self, message_id: str) -> str | None:
        """best-effort 添加飞书 Typing reaction。"""
        try:
            reaction_id = await self._channel.add_typing_reaction(message_id)
        except Exception:
            return None
        return reaction_id if isinstance(reaction_id, str) and reaction_id else None

    async def remove_typing(self, message_id: str, reaction_id: str | None) -> bool:
        """best-effort 移除之前添加的 Typing reaction。"""
        if not reaction_id:
            return False
        try:
            return bool(
                await self._channel.remove_typing_reaction(message_id, reaction_id)
            )
        except Exception:
            return False

    async def send_card(
        self,
        *,
        conversation_id: str,
        reply_to_message_id: str,
        card: dict[str, Any],
        idempotency_key: str,
    ) -> SendReceipt:
        """使用相同投递语义发送交互卡片。"""
        options = self._send_options(
            reply_to_message_id=reply_to_message_id,
            idempotency_key=idempotency_key,
        )
        try:
            result = await self._channel.send(
                conversation_id,
                {"card": card},
                options,
            )
        except Exception as error:
            raise _transport_error(error) from None
        return _send_receipt(result)

    async def update_card(
        self,
        platform_message_id: str,
        card: dict[str, Any],
    ) -> SendReceipt:
        """更新已发送卡片并校验 official SDK 返回值。"""
        try:
            result = await self._channel.update_card(platform_message_id, card)
        except Exception as error:
            raise _transport_error(error) from None
        receipt = _send_receipt(result, default_message_id=platform_message_id)
        return receipt

    async def start_typing(self, event: StoredInboundEvent) -> str | None:
        """把平台无关 Typing 意图映射为飞书 reaction，返回 opaque token。"""
        reaction_id = await self.add_typing(event.reply_to_message_id)
        if reaction_id is None:
            return None
        if len(self._typing_tokens) >= 256:
            self._typing_tokens.pop(next(iter(self._typing_tokens)))
        token = uuid4().hex
        self._typing_tokens[token] = (event.reply_to_message_id, reaction_id)
        return token

    async def stop_typing(self, token: str | None) -> None:
        """用 opaque token 清理飞书 reaction；缺失或失败均为 best-effort。"""
        if token is None:
            return
        target = self._typing_tokens.pop(token, None)
        if target is None:
            return
        await self.remove_typing(*target)

    async def create_progress(
        self,
        event: StoredInboundEvent,
        progress: AgentProgress,
        *,
        idempotency_key: str,
    ) -> ProgressReceipt:
        """把结构化 progress 创建意图映射为飞书 Agent 卡片。"""
        rendered = render_agent_progress_card(progress)
        receipt = await self.send_card(
            conversation_id=event.external_conversation_id,
            reply_to_message_id=event.reply_to_message_id,
            card=rendered.card,
            idempotency_key=idempotency_key,
        )
        return ProgressReceipt(receipt.platform_message_id, rendered.visible_answer_chars)

    async def update_progress(
        self,
        platform_message_id: str,
        progress: AgentProgress,
    ) -> ProgressReceipt:
        """把结构化 progress 状态映射为同一飞书 Agent 卡片更新。"""
        rendered = render_agent_progress_card(progress)
        receipt = await self.update_card(platform_message_id, rendered.card)
        return ProgressReceipt(receipt.platform_message_id, rendered.visible_answer_chars)

    def _build_channel(self, app_id: str, app_secret: str) -> Any:
        """创建 explicit secure configs；凭据只在此传给 official SDK。"""
        security = self._sdk.SecurityConfig(
            mode="strict",
            strict_content_text=True,
            allow_unsigned_encrypted_webhook=False,
            allow_insecure_ws=False,
            allow_local_insecure_ws=False,
            max_ws_fragment_parts=32,
            max_ws_fragment_bytes=2 * 1024 * 1024,
            max_concurrent_ws_handlers=16,
            resource_overflow_policy="drop",
        )
        group_enabled = self._config.allow_group_mentions
        policy = self._sdk.PolicyConfig(
            dm_policy="allowlist",
            allow_from=list(self._config.allowed_open_ids),
            group_policy="allowlist" if group_enabled else "disabled",
            group_allowlist=(
                list(self._config.allowed_chat_ids) if group_enabled else None
            ),
            require_mention=True,
            respond_to_mention_all=False,
            sender_identity_fields=["open_id"],
        )
        # 开启媒体缓存，SDK 才会把图片真正下载到本地并给出可读路径；
        # 不开的话 resources 里只有 file_key，图片永远到不了视觉模型。
        # 缓存落在进程私有临时目录并设硬上限：图片是用户私人内容，不该长期留存，
        # 也不该让一次批量转发把磁盘写满。
        cache_root = Path(tempfile.gettempdir()) / "lobster0-feishu-media"
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        inbound = self._sdk.InboundConfig(
            drop_self_sent=True,
            include_raw=True,
            emit_raw_events=False,
        )
        transport = self._sdk.TransportConfig(
            kind="ws",
            auto_reconnect=True,
            http_timeout_seconds=30.0,
        )
        return self._sdk.FeishuChannel(
            app_id=app_id,
            app_secret=app_secret,
            media_cache=self._sdk.MediaCacheConfig(
                enabled=True,
                root_dir=str(cache_root),
                ttl_seconds=3600,
                image_max_bytes=8 * 1024 * 1024,
            ),
            domain=_sdk_domain(self._sdk, self._config.domain),
            transport=transport,
            policy=policy,
            inbound=inbound,
            security=security,
        )

    async def _handle_message(self, message: Any) -> None:
        """提取有限 SDK 字段，并让本地 Adapter 再做一次 fail-closed admission。"""
        view = _official_message_view(message)
        normalized = self._adapter.normalize(view)
        if isinstance(normalized, InboundMessage):
            await self._on_inbound(normalized)
        elif self._observer is not None:
            try:
                self._observer.inbound(
                    channel="feishu",
                    account_id=self._config.account_id,
                    external_message_id=view.message_id or view.event_id or "invalid",
                    external_conversation_id=view.chat_id or None,
                    status="ignored",
                    reason=normalized.reason,
                )
            except Exception:
                pass

    def _handle_reconnecting(self) -> None:
        """同步接收 SDK 自动重连通知并更新安全状态。"""
        self._set_connection_state("reconnecting")

    def _handle_reconnected(self) -> None:
        """同步接收 SDK 重连成功通知。"""
        self._connected = True
        self._set_connection_state("connected")

    def _handle_sdk_error(self, error: Any) -> None:
        """忽略混合 SDK error fan-out；连接状态只由明确 lifecycle 事件改变。"""
        del error

    def _set_connection_state(
        self,
        state: str,
        *,
        error_code: str | None = None,
    ) -> None:
        """原子更新本地状态并发出同一份脱敏事件。"""
        self._connection_state = state
        if self._observer is not None:
            try:
                self._observer.transport_state(
                    channel="feishu",
                    account_id=self._config.account_id,
                    state=state,
                    error_code=error_code,
                )
            except Exception:
                pass

    async def _handle_card_action(self, event: Any) -> None:
        """只转交 Controller 需要的操作者、值和回复目标。"""
        if self._on_card_action is None:
            return
        operator = getattr(event, "operator", None)
        action = getattr(event, "action", None)
        await self._on_card_action(
            str(getattr(operator, "open_id", "") or ""),
            getattr(action, "value", None),
            str(getattr(event, "chat_id", "") or ""),
            str(getattr(event, "message_id", "") or ""),
        )

    def _send_options(
        self,
        *,
        reply_to_message_id: str,
        idempotency_key: str,
    ) -> Any:
        """构建 Chat 回复使用的 official SendOpts。"""
        return self._sdk.SendOpts(
            reply_to=reply_to_message_id,
            receive_id_type="chat_id",
            uuid=idempotency_key,
            reply_target_gone="fresh",
        )

    def _unsubscribe_handler(self) -> None:
        """幂等解除 SDK handler，避免断线期间接收新工作。"""
        while self._unsubscribers:
            unsubscribe = self._unsubscribers.pop()
            unsubscribe()


def _official_message_view(message: Any) -> _OfficialMessageView:
    """从 official InboundMessage 提取 Adapter 需要的只读字段。"""
    conversation = getattr(message, "conversation", None)
    sender = getattr(message, "sender", None)
    message_id = str(getattr(message, "id", "") or "")
    return _OfficialMessageView(
        event_id=_event_id(getattr(message, "raw", None), fallback=message_id),
        message_id=message_id,
        chat_id=str(getattr(conversation, "chat_id", "") or ""),
        chat_type=str(getattr(conversation, "chat_type", "") or ""),
        sender_id=str(getattr(sender, "open_id", "") or ""),
        sender_type=getattr(sender, "sender_type", None),
        sender_is_bot=bool(getattr(sender, "is_bot", False)),
        mentioned_bot=bool(getattr(message, "mentioned_bot", False)),
        body_text=str(getattr(message, "body_text", "") or ""),
        raw_content_type=str(getattr(message, "raw_content_type", "") or ""),
        parent_message_id=_parent_message_id(message),
        create_time=getattr(message, "create_time", None),
    )


def _cached_image_paths(message: Any) -> tuple[tuple[Path, str], ...]:
    """取出 SDK 已经下载并缓存到本地的图片资源。

    只接受 ``decision == "cached"``：``skipped``/``rejected`` 表示 SDK 因为超限或策略
    没有真的把文件落盘，此时 ``path`` 不可读，当成图片带出去只会在读取时炸掉。

    同时按 MIME 二次过滤：``resources`` 里也可能有音频、文件，它们不能被当作图片
    送进视觉模型。
    """
    resources = getattr(message, "resources", None)
    if not resources:
        return ()
    found: list[tuple[Path, str]] = []
    for item in resources:
        if getattr(item, "decision", None) != "cached":
            continue
        mime = str(getattr(item, "mime_type", "") or "")
        if mime not in {"image/png", "image/jpeg"}:
            continue
        path = getattr(item, "path", None)
        if path is None:
            continue
        found.append((Path(path), mime))
    return tuple(found)


def _parent_message_id(message: Any) -> str:
    """读取"这条消息回复了哪条消息"的平台 message ID。

    ``lark_channel`` 已经把飞书的回复关系解析成 ``message.reply``（``ReplyRef``，
    含 ``message_id``），这是权威来源。随后依次回退到 ``parent_id`` 属性与原始事件 JSON
    的 ``event.message.parent_id``，兼容 SDK 未填充 ``reply`` 的情况。

    任何一步取不到都返回空字符串——不是回复时本来就该为空，调用方据此判定"这不是一条回复"。
    """
    reply = getattr(message, "reply", None)
    reply_id = getattr(reply, "message_id", None)
    if isinstance(reply_id, str) and reply_id:
        return reply_id
    direct = getattr(message, "parent_id", None)
    if isinstance(direct, str) and direct:
        return direct
    raw = getattr(message, "raw", None)
    if isinstance(raw, dict):
        event = raw.get("event")
        if isinstance(event, dict):
            payload = event.get("message")
            if isinstance(payload, dict):
                parent = payload.get("parent_id")
                if isinstance(parent, str) and parent:
                    return parent
    return ""


def _valid_message_id(value: str) -> str:
    """只接受形状合法的飞书 message ID，其余一律当作"不是回复"。"""
    return value if _MESSAGE_ID.fullmatch(value) is not None else ""


def _event_id(raw: Any, *, fallback: str) -> str:
    """优先读取飞书事件头 event_id，不存在时退化为 message_id。"""
    if isinstance(raw, dict):
        header = raw.get("header")
        if isinstance(header, dict) and isinstance(header.get("event_id"), str):
            return header["event_id"]
        if isinstance(raw.get("event_id"), str):
            return raw["event_id"]
    return fallback


def _sdk_domain(sdk: Any, domain: str) -> str:
    """把 Lobster0 的稳定枚举映射为 official SDK endpoint。"""
    return sdk.FEISHU_DOMAIN if domain == "feishu" else sdk.LARK_DOMAIN


def _send_receipt(result: Any, *, default_message_id: str = "") -> SendReceipt:
    """把 SendResult 成功/失败映射为平台无关契约。"""
    if not bool(getattr(result, "success", False)):
        raise _transport_error(getattr(result, "error", result))
    message_id = getattr(result, "message_id", None) or default_message_id
    if not isinstance(message_id, str) or not message_id:
        raise ChannelTransportError("feishu_delivery_unknown", unknown=True)
    return SendReceipt(message_id)


def _transport_error(error: Any) -> ChannelTransportError:
    """丢弃 SDK 原文，只保留有限稳定码和恢复属性。"""
    raw_code = getattr(error, "code", "")
    code = getattr(raw_code, "value", raw_code)
    if not isinstance(code, str):
        code = ""
    mapping = {
        "format_error": (False, False),
        "target_revoked": (False, False),
        "rate_limited": (True, False),
        "permission_denied": (False, False),
        "upload_failed": (True, False),
        "download_failed": (True, False),
        "ssrf_blocked": (False, False),
        "send_timeout": (False, True),
        "not_connected": (True, False),
        "unknown": (False, True),
    }
    if code not in mapping:
        return ChannelTransportError("feishu_send_failed")
    retryable, unknown = mapping[code]
    if code == "rate_limited":
        retryable = bool(getattr(error, "retryable", True))
    return ChannelTransportError(
        f"feishu_{code}",
        retryable=retryable,
        unknown=unknown,
    )


def _received_at(
    value: datetime | str | int | None,
    clock: Callable[[], datetime],
) -> datetime:
    """规范 SDK 毫秒时间戳，缺失或非法值退化为本地接收时间。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        milliseconds = int(value) if value is not None else 0
    except (TypeError, ValueError):
        milliseconds = 0
    if milliseconds > 0:
        try:
            return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    received_at = clock()
    return received_at if received_at.tzinfo is not None else received_at.replace(tzinfo=UTC)
