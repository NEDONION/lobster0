"""Telegram Transport 测试使用的窄 SDK facade。"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any


class RetryAfter(Exception):
    """模拟 official RetryAfter。"""

    def __init__(self, retry_after: float, secret: str = "private") -> None:
        super().__init__(secret)
        self.retry_after = retry_after


class TimedOut(Exception):
    """模拟发送结果不确定的 official TimedOut。"""


class NetworkError(Exception):
    """模拟可重试网络错误。"""


class Forbidden(Exception):
    """模拟无权限错误。"""


class InvalidToken(Exception):
    """模拟 Token 认证错误。"""


class BadRequest(Exception):
    """模拟 Telegram BadRequest。"""


class FakeTelegramApplication:
    """记录生命周期、消息调用并支持回放 Update 的 facade。"""

    def __init__(
        self,
        *,
        send_outcomes: Sequence[int | BaseException] = (),
        edit_outcomes: Sequence[int | bool | BaseException] = (),
        typing_outcomes: Sequence[bool | BaseException] = (),
        get_me_outcome: Any | BaseException | None = None,
        during_start_update: Any | None = None,
    ) -> None:
        self.events: list[str] = []
        self.allowed_updates: tuple[str, ...] | None = None
        self.callback: Callable[[Any], Awaitable[None]] | None = None
        self.send_outcomes = list(send_outcomes)
        self.edit_outcomes = list(edit_outcomes)
        self.typing_outcomes = list(typing_outcomes)
        self.get_me_outcome = get_me_outcome or SimpleNamespace(
            id=999,
            username="lobster0_bot",
        )
        self.during_start_update = during_start_update
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.typing: list[dict[str, Any]] = []

    async def initialize(self) -> None:
        self.events.append("initialize")

    async def get_me(self) -> Any:
        self.events.append("get_me")
        if isinstance(self.get_me_outcome, BaseException):
            raise self.get_me_outcome
        return self.get_me_outcome

    async def start_polling(
        self,
        callback: Callable[[Any], Awaitable[None]],
        *,
        allowed_updates: tuple[str, ...],
    ) -> None:
        self.events.append("start_polling")
        self.callback = callback
        self.allowed_updates = allowed_updates
        if self.during_start_update is not None:
            await callback(self.during_start_update)

    async def start(self) -> None:
        self.events.append("start")

    async def stop_polling(self) -> None:
        self.events.append("stop_polling")

    async def stop(self) -> None:
        self.events.append("stop")

    async def shutdown(self) -> None:
        self.events.append("shutdown")

    async def emit(self, update: Any) -> None:
        if self.callback is None:
            raise AssertionError("polling callback is not configured")
        await self.callback(update)

    async def send_message(self, **values: Any) -> int:
        self.sent.append(values)
        outcome = self.send_outcomes.pop(0) if self.send_outcomes else 700
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def edit_message(self, **values: Any) -> int | bool:
        self.edited.append(values)
        outcome = self.edit_outcomes.pop(0) if self.edit_outcomes else values["message_id"]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def send_typing(self, **values: Any) -> bool:
        self.typing.append(values)
        outcome = self.typing_outcomes.pop(0) if self.typing_outcomes else True
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def fake_update(
    *,
    update_id: int = 100,
    message_id: int = 200,
    user_id: int = 300,
    chat_id: int = 300,
    chat_type: str = "private",
    text: str | None = "你好",
    username: str | None = None,
    reply_bot_id: int | None = None,
    topic_id: int | None = None,
    is_bot: bool = False,
) -> Any:
    """构造 official Update 形状的最小对象。"""
    entities: list[Any] = []
    if username is not None and text is not None:
        token = f"@{username}"
        python_offset = text.index(token)
        offset = len(text[:python_offset].encode("utf-16-le")) // 2
        length = len(token.encode("utf-16-le")) // 2
        entities.append(
            SimpleNamespace(type="mention", offset=offset, length=length, user=None)
        )
    reply = (
        None
        if reply_bot_id is None
        else SimpleNamespace(from_user=SimpleNamespace(id=reply_bot_id))
    )
    message = SimpleNamespace(
        message_id=message_id,
        from_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        text=text,
        date=datetime(
            2026,
            8,
            8,
            tzinfo=UTC,
        ),
        entities=entities,
        reply_to_message=reply,
        message_thread_id=topic_id,
        new_chat_members=None,
        left_chat_member=None,
        group_chat_created=False,
        supergroup_chat_created=False,
        channel_chat_created=False,
        migrate_to_chat_id=None,
        migrate_from_chat_id=None,
    )
    return SimpleNamespace(
        update_id=update_id,
        message=message,
        edited_message=None,
        channel_post=None,
    )
