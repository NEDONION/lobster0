"""Official lark-channel-sdk Transport 边界测试。"""

import unittest
from types import SimpleNamespace

from miniclaw.channels.base import ChannelTransportError, InboundMessage, OutboundMessage
from miniclaw.channels.feishu import FeishuTransport
from miniclaw.config import FeishuConfig
from tests.fakes.fake_channel import (
    FakeOfficialSdk,
    FakeSdkSendResult,
)


class FeishuTransportTest(unittest.IsolatedAsyncioTestCase):
    """验证 SDK 安全配置、生命周期、映射、发送与稳定错误。"""

    def setUp(self) -> None:
        """创建严格私聊/群聊白名单配置。"""
        self.config = FeishuConfig(
            enabled=True,
            account_id="work",
            domain="feishu",
            owner_open_id="ou_owner",
            allowed_open_ids=("ou_owner", "ou_friend"),
            allowed_chat_ids=("oc_allowed",),
            allow_group_mentions=True,
            message_max_chars=1000,
        )
        self.received: list[InboundMessage] = []

    async def _receive(self, message: InboundMessage) -> None:
        """记录通过两层 admission 的标准消息。"""
        self.received.append(message)

    def _transport(
        self,
        sdk: FakeOfficialSdk,
        *,
        app_secret: str = "app-secret-private",
    ) -> FeishuTransport:
        """用注入 SDK 创建 Transport。"""
        return FeishuTransport(
            self.config,
            app_id="cli_test",
            app_secret=app_secret,
            on_inbound=self._receive,
            sdk=sdk,
        )

    async def test_constructor_sets_strict_security_and_closed_policies(self) -> None:
        """Transport 必须显式使用 WS、strict、用户/群 allowlist 与 require mention。"""
        sdk = FakeOfficialSdk()
        transport = self._transport(sdk)

        arguments = sdk.channel.constructor_kwargs
        self.assertEqual(arguments["app_id"], "cli_test")
        self.assertEqual(arguments["app_secret"], "app-secret-private")
        self.assertEqual(arguments["domain"], sdk.FEISHU_DOMAIN)
        self.assertEqual(arguments["transport"].values["kind"], "ws")
        self.assertEqual(arguments["security"].values["mode"], "strict")
        self.assertTrue(arguments["security"].values["strict_content_text"])
        self.assertLessEqual(arguments["security"].values["max_ws_fragment_parts"], 64)
        self.assertLessEqual(
            arguments["security"].values["max_concurrent_ws_handlers"],
            32,
        )
        policy = arguments["policy"].values
        self.assertEqual(policy["dm_policy"], "allowlist")
        self.assertEqual(policy["allow_from"], ["ou_owner", "ou_friend"])
        self.assertEqual(policy["group_policy"], "allowlist")
        self.assertEqual(policy["group_allowlist"], ["oc_allowed"])
        self.assertTrue(policy["require_mention"])
        self.assertNotIn("app-secret-private", repr(transport))
        self.assertNotIn("cli_test", repr(transport))

    async def test_connect_maps_official_inbound_and_disconnects(self) -> None:
        """连接就绪后注册消息可进入 Adapter，关闭时解除 handler 并断连。"""
        sdk = FakeOfficialSdk()
        transport = self._transport(sdk)

        await transport.connect()
        self.assertTrue(sdk.channel.connected)
        await sdk.channel.handlers["message"](
            SimpleNamespace(
                id="om_inbound",
                create_time=1_786_118_400_000,
                conversation=SimpleNamespace(chat_id="oc_allowed", chat_type="p2p"),
                sender=SimpleNamespace(
                    open_id="ou_owner",
                    sender_type="user",
                    is_bot=False,
                ),
                mentioned_bot=False,
                body_text="  帮我总结  ",
                raw_content_type="text",
                raw={"header": {"event_id": "evt_inbound"}},
            )
        )

        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0].message_id, "om_inbound")
        self.assertEqual(self.received[0].event_id, "evt_inbound")
        self.assertEqual(self.received[0].text, "帮我总结")
        await transport.disconnect()
        self.assertTrue(sdk.channel.disconnected)
        self.assertNotIn("message", sdk.channel.handlers)

    async def test_send_replies_with_stable_uuid_and_capabilities(self) -> None:
        """回复、Typing 和 Card 都通过 official SDK 的显式公共 API。"""
        sdk = FakeOfficialSdk(
            (
                FakeSdkSendResult(True, "om_reply"),
                FakeSdkSendResult(True, "om_card"),
            )
        )
        transport = self._transport(sdk)
        message = OutboundMessage(
            channel="feishu",
            account_id="work",
            external_conversation_id="oc_allowed",
            reply_to_message_id="om_inbound",
            content="**完成**",
        )

        receipt = await transport.send(message, idempotency_key="stable-uuid")
        to, payload, opts = sdk.channel.sent[0]
        self.assertEqual(receipt.platform_message_id, "om_reply")
        self.assertEqual(to, "oc_allowed")
        self.assertEqual(payload, "**完成**")
        self.assertEqual(opts.reply_to, "om_inbound")
        self.assertEqual(opts.receive_id_type, "chat_id")
        self.assertEqual(opts.uuid, "stable-uuid")

        reaction_id = await transport.add_typing("om_inbound")
        self.assertEqual(reaction_id, "reaction_typing")
        self.assertTrue(await transport.remove_typing("om_inbound", reaction_id))
        card_receipt = await transport.send_card(
            conversation_id="oc_allowed",
            reply_to_message_id="om_inbound",
            card={"schema": "2.0"},
            idempotency_key="card-uuid",
        )
        self.assertEqual(card_receipt.platform_message_id, "om_card")
        self.assertEqual(sdk.channel.sent[1][1], {"card": {"schema": "2.0"}})
        await transport.update_card("om_card", {"schema": "2.0", "body": {}})
        self.assertEqual(sdk.channel.cards_updated[-1][0], "om_card")

    async def test_sdk_failures_map_to_stable_redacted_errors(self) -> None:
        """SDK 失败只暴露 MiniClaw 稳定码、重试属性和不确定发送属性。"""
        cases = (
            (
                FakeSdkSendResult(
                    False,
                    error=SimpleNamespace(code="rate_limited", retryable=True),
                ),
                "feishu_rate_limited",
                True,
                False,
            ),
            (
                SimpleNamespace(code="send_timeout", secret="token-private"),
                "feishu_send_timeout",
                False,
                True,
            ),
            (
                RuntimeError("authorization=token-private"),
                "feishu_send_failed",
                False,
                False,
            ),
        )
        for outcome, code, retryable, unknown in cases:
            with self.subTest(code=code):
                sdk = FakeOfficialSdk((outcome,))
                transport = self._transport(sdk)
                message = OutboundMessage(
                    channel="feishu",
                    account_id="work",
                    external_conversation_id="oc_allowed",
                    reply_to_message_id="om_inbound",
                    content="reply",
                )
                with self.assertRaises(ChannelTransportError) as raised:
                    await transport.send(message, idempotency_key="stable-uuid")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.retryable, retryable)
                self.assertEqual(raised.exception.unknown, unknown)
                self.assertNotIn("token-private", repr(raised.exception))


if __name__ == "__main__":
    unittest.main()
