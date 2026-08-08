"""Telegram long polling、发送、Experience 和错误映射测试。"""

import asyncio
import unittest
from datetime import UTC, datetime

from miniclaw.agent.events import RunEvent
from miniclaw.channels.base import ChannelTransportError, InboundMessage, OutboundMessage
from miniclaw.channels.progress import AgentProgress, ProgressProjector
from miniclaw.channels.telegram import TelegramTransport
from miniclaw.config import TelegramConfig
from miniclaw.storage.channels import InboundEventKey, StoredInboundEvent
from tests.fakes.fake_telegram import (
    BadRequest,
    FakeTelegramApplication,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TimedOut,
    fake_update,
)


def _progress(text: str, *, completed: bool = False, failed: bool = False) -> AgentProgress:
    """构造 Telegram Transport 使用的结构化公开进度。"""
    projector = ProgressProjector(clock=lambda: 0.0)
    projector.apply(RunEvent("model_text_delta", 1, {"text": text}))
    if completed or failed:
        return projector.finish(text if completed else None, failed=failed)
    return projector.snapshot()


class TelegramTransportTest(unittest.IsolatedAsyncioTestCase):
    """以注入 facade 锁定 official SDK 边界而不访问网络。"""

    def setUp(self) -> None:
        self.config = TelegramConfig(
            enabled=True,
            account_id="personal",
            owner_user_id=300,
            allowed_user_ids=(300,),
            allowed_chat_ids=(-100123,),
            allow_group_mentions=True,
            message_max_chars=4096,
            progress_update_interval=0.01,
        )
        self.received: list[InboundMessage] = []

    async def _receive(self, message: InboundMessage) -> None:
        self.received.append(message)

    def _transport(
        self,
        application: FakeTelegramApplication,
        *,
        token: str = "123456:private-token",
    ) -> TelegramTransport:
        return TelegramTransport(
            self.config,
            token=token,
            on_inbound=self._receive,
            application_factory=lambda _token: application,
            typing_renew_interval=0.01,
        )

    async def test_connect_get_me_ready_gate_and_exact_shutdown_order(self) -> None:
        """ready 前丢弃回调，ready 后 normalize；停止先关入口再释放 SDK。"""
        early = fake_update(text="early-private")
        app = FakeTelegramApplication(during_start_update=early)
        transport = self._transport(app)

        await transport.connect()
        self.assertEqual(
            app.events,
            ["initialize", "get_me", "start_polling", "start"],
        )
        self.assertEqual(app.allowed_updates, ("message",))
        self.assertEqual(self.received, [])

        await app.emit(fake_update(text="ready"))
        self.assertEqual([item.text for item in self.received], ["ready"])

        transport.stop_receiving()
        await app.emit(fake_update(update_id=101, text="late-private"))
        self.assertEqual([item.text for item in self.received], ["ready"])
        await transport.disconnect()
        await transport.disconnect()
        self.assertEqual(
            app.events,
            [
                "initialize",
                "get_me",
                "start_polling",
                "start",
                "stop_polling",
                "stop",
                "shutdown",
            ],
        )

    async def test_sdk_update_maps_bot_mention_reply_and_topic_without_retention(self) -> None:
        """Mapper 只复制窄字段，识别自身 mention/reply 和 forum topic。"""
        app = FakeTelegramApplication()
        transport = self._transport(app)
        await transport.connect()

        text = "🙂 @miniclaw_bot 帮我看 @alice"
        await app.emit(
            fake_update(
                user_id=300,
                chat_id=-100123,
                chat_type="supergroup",
                text=text,
                username="miniclaw_bot",
                topic_id=42,
            )
        )
        await app.emit(
            fake_update(
                update_id=101,
                message_id=201,
                user_id=300,
                chat_id=-100123,
                chat_type="supergroup",
                text="继续",
                reply_bot_id=999,
                topic_id=42,
            )
        )

        self.assertEqual(
            [item.text for item in self.received],
            ["🙂  帮我看 @alice", "继续"],
        )
        self.assertEqual(
            self.received[0].external_conversation_id,
            "chat:-100123:topic:42",
        )
        self.assertNotIn(text, repr(transport))
        self.assertNotIn("123456:private-token", repr(transport))
        await transport.disconnect()

    async def test_auth_failure_is_stable_redacted_and_partially_initialized_is_closed(
        self,
    ) -> None:
        """get_me 失败不启动 polling，并关闭已初始化资源且不回显 Token。"""
        app = FakeTelegramApplication(get_me_outcome=InvalidToken("private-token"))
        transport = self._transport(app)

        with self.assertRaises(ChannelTransportError) as raised:
            await transport.connect()

        self.assertEqual(raised.exception.code, "telegram_auth_failed")
        self.assertEqual(app.events, ["initialize", "get_me", "shutdown"])
        self.assertNotIn("private-token", repr(raised.exception))

    async def test_send_targets_reply_only_first_part_and_preserves_topic(self) -> None:
        """第一片 reply 原消息，后续片只发到相同 chat/topic，parse mode 由 facade 禁用。"""
        app = FakeTelegramApplication(send_outcomes=(700, 701))
        transport = self._transport(app)
        await transport.connect()
        first = OutboundMessage(
            channel="telegram",
            account_id="personal",
            external_conversation_id="chat:-100123:topic:42",
            reply_to_message_id="chat:-100123:message:200",
            content="[1/2] 第一段",
        )
        second = OutboundMessage(
            channel="telegram",
            account_id="personal",
            external_conversation_id="chat:-100123:topic:42",
            reply_to_message_id="chat:-100123:message:200",
            content="[2/2] 第二段",
        )

        first_receipt = await transport.send(first, idempotency_key="local-key-1")
        second_receipt = await transport.send(second, idempotency_key="local-key-2")

        self.assertEqual(first_receipt.platform_message_id, "chat:-100123:message:700")
        self.assertEqual(second_receipt.platform_message_id, "chat:-100123:message:701")
        self.assertEqual(app.sent[0]["reply_to_message_id"], 200)
        self.assertIsNone(app.sent[1]["reply_to_message_id"])
        self.assertEqual(app.sent[0]["message_thread_id"], 42)
        self.assertFalse(app.sent[0]["link_preview_enabled"])
        await transport.disconnect()

    async def test_send_errors_map_retry_after_unknown_permission_and_network(self) -> None:
        """official 异常只映射稳定码、retry-after 与 unknown 属性。"""
        cases = (
            (RetryAfter(7.5), "telegram_rate_limited", True, False, 7.5),
            (TimedOut("private"), "telegram_delivery_unknown", False, True, None),
            (NetworkError("private"), "telegram_send_failed", True, False, None),
            (Forbidden("private"), "telegram_permission_denied", False, False, None),
        )
        for error, code, retryable, unknown, retry_after in cases:
            with self.subTest(code=code):
                app = FakeTelegramApplication(send_outcomes=(error,))
                transport = self._transport(app)
                await transport.connect()
                message = OutboundMessage(
                    channel="telegram",
                    account_id="personal",
                    external_conversation_id="chat:300",
                    reply_to_message_id="chat:300:message:200",
                    content="reply",
                )
                with self.assertRaises(ChannelTransportError) as raised:
                    await transport.send(message, idempotency_key="local-key")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(raised.exception.unknown, unknown)
                self.assertEqual(raised.exception.retry_after, retry_after)
                self.assertNotIn("private", repr(raised.exception))
                await transport.disconnect()

    async def test_typing_renews_then_cancels_and_progress_uses_send_edit(self) -> None:
        """Typing renewal 有界可取消，preview 创建/终态更新使用普通消息和 edit。"""
        app = FakeTelegramApplication(send_outcomes=(700,))
        transport = self._transport(app)
        await transport.connect()
        event = self._event()

        token = await transport.start_typing(event)
        await asyncio.sleep(0.025)
        receipt = await transport.create_progress(
            event,
            _progress("第一段"),
            idempotency_key="progress-key",
        )
        await transport.update_progress(
            receipt.platform_message_id,
            _progress("完整回答", completed=True),
        )
        await transport.stop_typing(token)
        count_after_stop = len(app.typing)
        await asyncio.sleep(0.02)

        self.assertGreaterEqual(count_after_stop, 2)
        self.assertEqual(len(app.typing), count_after_stop)
        self.assertIn("Claw Trail", app.sent[0]["text"])
        self.assertIn("第一段", app.sent[0]["text"])
        self.assertIn("最终内容见下一条消息", app.edited[-1]["text"])
        await transport.disconnect()

    async def test_message_not_modified_is_success_but_other_edit_error_is_stable(self) -> None:
        """幂等 edit 的 not-modified 不算失败，其他异常仍是脱敏稳定码。"""
        app = FakeTelegramApplication(
            edit_outcomes=(BadRequest("Message is not modified"), Forbidden("private"))
        )
        transport = self._transport(app)
        await transport.connect()

        receipt = await transport.update_progress(
            "chat:300:message:700",
            _progress("same"),
        )
        self.assertEqual(receipt.platform_message_id, "chat:300:message:700")
        with self.assertRaises(ChannelTransportError) as raised:
            await transport.update_progress(
                "chat:300:message:700",
                _progress("new", failed=True),
            )
        self.assertEqual(raised.exception.code, "telegram_permission_denied")
        await transport.disconnect()

    def _event(self) -> StoredInboundEvent:
        now = datetime.now(UTC)
        return StoredInboundEvent(
            key=InboundEventKey("telegram", "personal", "chat:300:message:200"),
            event_id="update:100",
            external_user_id="300",
            external_conversation_id="chat:300",
            chat_type="p2p",
            message_type="text",
            content="private",
            reply_to_message_id="chat:300:message:200",
            session_id=1,
            status="running",
            attempts=1,
            last_error_code=None,
            received_at=now,
            updated_at=now,
        )


if __name__ == "__main__":
    unittest.main()
