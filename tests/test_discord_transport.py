"""Discord Gateway lifecycle、safe delivery 与 Experience 测试。"""

import unittest
from datetime import UTC, datetime

from miniclaw.channels.base import ChannelTransportError, InboundMessage, OutboundMessage
from miniclaw.channels.discord import DiscordIntents, DiscordTransport
from miniclaw.config import DiscordConfig
from miniclaw.storage.channels import InboundEventKey, StoredInboundEvent
from tests.fakes.fake_discord import (
    ConnectionClosed,
    FakeDiscordClient,
    HTTPException,
    LoginFailure,
    PrivilegedIntentsRequired,
    fake_message,
)


class DiscordTransportTest(unittest.IsolatedAsyncioTestCase):
    """使用 injected facade 验证 discord.py 边界，不访问真实 Gateway。"""

    def setUp(self) -> None:
        self.config = DiscordConfig(
            enabled=True,
            account_id="personal",
            owner_user_id=300,
            allowed_user_ids=(300,),
            allowed_guild_ids=(500,),
            allowed_channel_ids=(400,),
            allow_guild_mentions=True,
            message_max_chars=2000,
            progress_update_interval=0.01,
            typing_renew_interval=0.01,
        )
        self.received: list[InboundMessage] = []
        self.factory_calls: list[tuple[str, DiscordIntents]] = []

    async def _receive(self, message: InboundMessage) -> None:
        self.received.append(message)

    def _transport(
        self,
        client: FakeDiscordClient,
        *,
        token: str = "discord-private-token",
    ) -> DiscordTransport:
        def factory(value: str, intents: DiscordIntents) -> FakeDiscordClient:
            self.factory_calls.append((value, intents))
            return client

        return DiscordTransport(
            self.config,
            token=token,
            on_inbound=self._receive,
            client_factory=factory,
        )

    async def test_exact_intents_ready_lifecycle_resume_and_idempotent_close(self) -> None:
        """只启用必要 intents，ready 后开放，断线由 SDK resume，不重建 Transport。"""
        client = FakeDiscordClient()
        transport = self._transport(client)

        await transport.connect()
        self.assertEqual(client.events, ["login", "connect", "ready"])
        intents = self.factory_calls[0][1]
        self.assertEqual(
            intents,
            DiscordIntents(
                guilds=True,
                guild_messages=True,
                dm_messages=True,
                message_content=True,
                members=False,
                presences=False,
                reactions=False,
                typing=False,
            ),
        )
        await client.emit_message(fake_message(content="ready"))
        self.assertEqual([item.text for item in self.received], ["ready"])

        await client.emit_disconnect()
        self.assertEqual(transport.connection_state, "degraded")
        await client.emit_resumed()
        self.assertEqual(transport.connection_state, "connected")

        transport.stop_receiving()
        await client.emit_message(fake_message(message_id=701, content="late"))
        self.assertEqual([item.text for item in self.received], ["ready"])
        await transport.disconnect()
        await transport.disconnect()
        self.assertEqual(client.events[-1], "close")
        self.assertEqual(client.events.count("close"), 1)

    async def test_sdk_message_maps_guild_thread_mention_and_reply_without_retention(self) -> None:
        """SDK object 立即压缩为 narrow view；Thread parent admission 和 reply Bot 均有效。"""
        client = FakeDiscordClient()
        transport = self._transport(client)
        await transport.connect()

        raw = fake_message(
            author_id=300,
            channel_id=400,
            guild_id=500,
            thread_id=600,
            content="<@999> 帮我看 <@123>",
            mentioned_bot=True,
        )
        await client.emit_message(raw)
        await client.emit_message(
            fake_message(
                message_id=701,
                author_id=300,
                channel_id=400,
                guild_id=500,
                thread_id=600,
                content="继续",
                replied_to_bot=True,
            )
        )

        self.assertEqual([item.text for item in self.received], ["帮我看 <@123>", "继续"])
        self.assertEqual(
            self.received[0].external_conversation_id,
            "guild:500:channel:400:thread:600",
        )
        self.assertNotIn("帮我看", repr(transport))
        self.assertNotIn("discord-private-token", repr(transport))
        await transport.disconnect()

    async def test_login_intent_and_gateway_close_errors_are_stable(self) -> None:
        """认证、privileged intent 与 fatal close code 不泄露 SDK 原文。"""
        cases = (
            (LoginFailure("private"), None, "discord_auth_failed"),
            (None, PrivilegedIntentsRequired("private"), "discord_intents_invalid"),
            (None, ConnectionClosed(4014, "private"), "discord_intents_invalid"),
        )
        for login_error, connect_error, code in cases:
            with self.subTest(code=code):
                client = FakeDiscordClient(
                    login_error=login_error,
                    connect_error=connect_error,
                )
                transport = self._transport(client)
                with self.assertRaises(ChannelTransportError) as raised:
                    await transport.connect()
                self.assertEqual(raised.exception.code, code)
                self.assertNotIn("private", repr(raised.exception))

    async def test_safe_send_uses_target_thread_no_mentions_and_first_reply_only(self) -> None:
        """所有消息禁用 mentions；Thread 是实际目标，只有 multipart 首片 reply。"""
        client = FakeDiscordClient(send_outcomes=(700, 701))
        transport = self._transport(client)
        await transport.connect()
        first = self._outbound("[1/2] hello @everyone")
        second = self._outbound("[2/2] <@123> world")

        first_receipt = await transport.send(first, idempotency_key="local-1")
        second_receipt = await transport.send(second, idempotency_key="local-2")

        self.assertEqual(first_receipt.platform_message_id, "channel:600:message:700")
        self.assertEqual(second_receipt.platform_message_id, "channel:600:message:701")
        self.assertEqual(client.sent[0]["target_id"], 600)
        self.assertEqual(client.sent[0]["reply_to_message_id"], 700)
        self.assertIsNone(client.sent[1]["reply_to_message_id"])
        self.assertTrue(all(item["suppress_mentions"] for item in client.sent))
        await transport.disconnect()

    async def test_http_errors_map_rate_limit_forbidden_not_found_retry_and_unknown(self) -> None:
        """HTTP/Gateway 错误只保留稳定恢复属性，429 传递 retry-after。"""
        cases = (
            (HTTPException(429, retry_after=6.0), "discord_rate_limited", True, False, 6.0),
            (HTTPException(403), "discord_forbidden", False, False, None),
            (HTTPException(404), "discord_target_not_found", False, False, None),
            (HTTPException(503), "discord_send_failed", True, False, None),
            (TimeoutError("private"), "discord_delivery_unknown", False, True, None),
        )
        for error, code, retryable, unknown, retry_after in cases:
            with self.subTest(code=code):
                client = FakeDiscordClient(send_outcomes=(error,))
                transport = self._transport(client)
                await transport.connect()
                with self.assertRaises(ChannelTransportError) as raised:
                    await transport.send(self._outbound("reply"), idempotency_key="local")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(raised.exception.unknown, unknown)
                self.assertEqual(raised.exception.retry_after, retry_after)
                self.assertNotIn("private", repr(raised.exception))
                await transport.disconnect()

    async def test_typing_context_and_progress_send_edit_are_always_cleaned(self) -> None:
        """Typing context 使用 opaque token 清理，preview 不替代最终 durable delivery。"""
        client = FakeDiscordClient(send_outcomes=(700,))
        transport = self._transport(client)
        await transport.connect()
        event = self._event()

        token = await transport.start_typing(event)
        receipt = await transport.create_progress(
            event,
            "第一段",
            idempotency_key="progress",
        )
        await transport.update_progress(
            receipt.platform_message_id,
            "完整回答",
            incomplete=False,
            completed=True,
        )
        await transport.stop_typing(token)
        await transport.stop_typing(token)

        self.assertEqual(client.typing_started, [600])
        self.assertEqual(client.typing_stopped, ["typing-handle"])
        self.assertEqual(client.sent[0]["text"], "⏳ 第一段")
        self.assertEqual(client.edited[0]["text"], "✅ 回复完成，最终内容见下一条消息")
        self.assertTrue(client.edited[0]["suppress_mentions"])
        await transport.disconnect()

    def _outbound(self, content: str) -> OutboundMessage:
        return OutboundMessage(
            channel="discord",
            account_id="personal",
            external_conversation_id="guild:500:channel:400:thread:600",
            reply_to_message_id="700",
            content=content,
        )

    def _event(self) -> StoredInboundEvent:
        now = datetime.now(UTC)
        return StoredInboundEvent(
            key=InboundEventKey("discord", "personal", "700"),
            event_id="700",
            external_user_id="300",
            external_conversation_id="guild:500:channel:400:thread:600",
            chat_type="group",
            message_type="text",
            content="private",
            reply_to_message_id="700",
            session_id=1,
            status="running",
            attempts=1,
            last_error_code=None,
            received_at=now,
            updated_at=now,
        )


if __name__ == "__main__":
    unittest.main()
