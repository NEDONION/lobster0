"""Discord Transport 测试使用的 injected client facade。"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any


class LoginFailure(Exception):
    """模拟 invalid token。"""


class PrivilegedIntentsRequired(Exception):
    """模拟 intent 配置被 Discord 拒绝。"""


class ConnectionClosed(Exception):
    """模拟 Gateway close code。"""

    def __init__(self, code: int, secret: str = "private") -> None:
        super().__init__(secret)
        self.code = code


class HTTPException(Exception):
    """模拟 discord.py HTTPException。"""

    def __init__(
        self,
        status: int,
        *,
        retry_after: float | None = None,
        secret: str = "private",
    ) -> None:
        super().__init__(secret)
        self.status = status
        self.retry_after = retry_after


class FakeDiscordClient:
    """记录 Gateway 生命周期、安全发送、edit 和 typing context。"""

    def __init__(
        self,
        *,
        login_error: BaseException | None = None,
        connect_error: BaseException | None = None,
        send_outcomes: Sequence[int | BaseException] = (),
        edit_outcomes: Sequence[int | bool | BaseException] = (),
        typing_outcomes: Sequence[str | BaseException] = (),
    ) -> None:
        self.login_error = login_error
        self.connect_error = connect_error
        self.send_outcomes = list(send_outcomes)
        self.edit_outcomes = list(edit_outcomes)
        self.typing_outcomes = list(typing_outcomes)
        self.events: list[str] = []
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.typing_started: list[int] = []
        self.typing_stopped: list[str] = []
        self.user_id = 999
        self._closed = asyncio.Event()
        self._on_ready: Callable[[], Awaitable[None]] | None = None
        self._on_message: Callable[[Any], Awaitable[None]] | None = None
        self._on_resumed: Callable[[], Awaitable[None]] | None = None
        self._on_disconnect: Callable[[], Awaitable[None]] | None = None

    def set_handlers(
        self,
        *,
        on_ready: Callable[[], Awaitable[None]],
        on_message: Callable[[Any], Awaitable[None]],
        on_resumed: Callable[[], Awaitable[None]],
        on_disconnect: Callable[[], Awaitable[None]],
    ) -> None:
        self._on_ready = on_ready
        self._on_message = on_message
        self._on_resumed = on_resumed
        self._on_disconnect = on_disconnect

    async def login(self) -> None:
        self.events.append("login")
        if self.login_error is not None:
            raise self.login_error

    async def connect(self) -> None:
        self.events.append("connect")
        if self.connect_error is not None:
            raise self.connect_error
        if self._on_ready is None:
            raise AssertionError("handlers are not configured")
        self.events.append("ready")
        await self._on_ready()
        await self._closed.wait()

    async def close(self) -> None:
        if not self._closed.is_set():
            self.events.append("close")
            self._closed.set()

    async def emit_message(self, message: Any) -> None:
        if self._on_message is None:
            raise AssertionError("message handler is not configured")
        await self._on_message(message)

    async def emit_disconnect(self) -> None:
        self.events.append("disconnect")
        if self._on_disconnect is not None:
            await self._on_disconnect()

    async def emit_resumed(self) -> None:
        self.events.append("resumed")
        if self._on_resumed is not None:
            await self._on_resumed()

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

    async def begin_typing(self, target_id: int) -> str:
        self.typing_started.append(target_id)
        outcome = self.typing_outcomes.pop(0) if self.typing_outcomes else "typing-handle"
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def end_typing(self, handle: Any) -> None:
        self.typing_stopped.append(str(handle))


def fake_message(
    *,
    message_id: int = 700,
    author_id: int = 300,
    channel_id: int = 400,
    guild_id: int | None = None,
    thread_id: int | None = None,
    content: str = "你好",
    mentioned_bot: bool = False,
    replied_to_bot: bool = False,
    author_is_bot: bool = False,
    webhook_id: int | None = None,
    is_system: bool = False,
) -> Any:
    """构造 discord.Message 的最小可读对象图。"""
    channel = SimpleNamespace(
        id=thread_id if thread_id is not None else channel_id,
        parent_id=channel_id if thread_id is not None else None,
    )
    reply_author = SimpleNamespace(id=999 if replied_to_bot else 123)
    reference = (
        SimpleNamespace(resolved=SimpleNamespace(author=reply_author))
        if replied_to_bot
        else None
    )
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, bot=author_is_bot),
        channel=channel,
        guild=None if guild_id is None else SimpleNamespace(id=guild_id),
        content=content,
        created_at=datetime(2026, 8, 8, tzinfo=UTC),
        webhook_id=webhook_id,
        mentions=[SimpleNamespace(id=999)] if mentioned_bot else [],
        reference=reference,
        is_system=lambda: is_system,
    )
