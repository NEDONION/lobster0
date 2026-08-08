"""Official lark-channel-sdk Transport 边界测试。"""

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from miniclaw.channels.base import ChannelTransportError, InboundMessage, OutboundMessage
from miniclaw.channels.feishu import FeishuTransport
from miniclaw.config import FeishuConfig
from miniclaw.storage.channels import InboundEventKey, StoredInboundEvent
from tests.fakes.fake_channel import (
    FakeOfficialSdk,
    FakeSdkSendResult,
)


class RecordingObserver:
    """记录 Transport 发出的安全可观测事件。"""

    def __init__(self) -> None:
        self.transport_events: list[dict[str, Any]] = []
        self.inbound_events: list[dict[str, Any]] = []

    def transport_state(self, **event: Any) -> None:
        """记录连接状态。"""
        self.transport_events.append(event)

    def inbound(self, **event: Any) -> None:
        """记录 admission 结果。"""
        self.inbound_events.append(event)


class FailingObserver(RecordingObserver):
    """模拟 Audit/日志后端异常，验证它不是 Transport 成败边界。"""

    def transport_state(self, **event: Any) -> None:
        """连接观测失败。"""
        del event
        raise RuntimeError("private-observer-failure")

    def inbound(self, **event: Any) -> None:
        """过滤观测失败。"""
        del event
        raise RuntimeError("private-observer-failure")


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
        self.card_actions: list[tuple[str, Any, str, str]] = []

    async def _receive(self, message: InboundMessage) -> None:
        """记录通过两层 admission 的标准消息。"""
        self.received.append(message)

    async def _card_action(
        self,
        actor_open_id: str,
        value: Any,
        chat_id: str,
        message_id: str,
    ) -> None:
        """记录 official CardActionEvent 的有限字段。"""
        self.card_actions.append((actor_open_id, value, chat_id, message_id))

    def _transport(
        self,
        sdk: FakeOfficialSdk,
        *,
        app_secret: str = "app-secret-private",
        observer: RecordingObserver | None = None,
    ) -> FeishuTransport:
        """用注入 SDK 创建 Transport。"""
        return FeishuTransport(
            self.config,
            app_id="cli_test",
            app_secret=app_secret,
            on_inbound=self._receive,
            on_card_action=self._card_action,
            sdk=sdk,
            observer=observer,
        )

    async def test_connection_reconnect_and_ignored_inbound_are_observable(self) -> None:
        """official 重连事件和安全过滤必须发出脱敏状态，不能依赖正文。"""
        sdk = FakeOfficialSdk()
        observer = RecordingObserver()
        transport = self._transport(sdk, observer=observer)

        self.assertEqual(transport.connection_state, "disconnected")
        await transport.connect()
        self.assertEqual(transport.connection_state, "connected")
        sdk.channel.handlers["reconnecting"]()
        self.assertEqual(transport.connection_state, "reconnecting")
        sdk.channel.handlers["reconnected"]()
        self.assertEqual(transport.connection_state, "connected")
        await sdk.channel.handlers["message"](
            SimpleNamespace(
                id="om_denied",
                create_time=1_786_118_400_000,
                conversation=SimpleNamespace(chat_id="oc_allowed", chat_type="p2p"),
                sender=SimpleNamespace(
                    open_id="ou_intruder",
                    sender_type="user",
                    is_bot=False,
                ),
                mentioned_bot=False,
                body_text="private-body-must-not-be-observed",
                raw_content_type="text",
                raw={"header": {"event_id": "evt_denied"}},
            )
        )
        await transport.disconnect()

        self.assertEqual(
            [event["state"] for event in observer.transport_events],
            [
                "connecting",
                "connected",
                "reconnecting",
                "connected",
                "stopping",
                "disconnected",
            ],
        )
        self.assertEqual(observer.inbound_events[0]["status"], "ignored")
        self.assertEqual(observer.inbound_events[0]["reason"], "sender_denied")
        self.assertNotIn("private-body", repr(observer.inbound_events))

    async def test_observer_failure_never_breaks_transport_lifecycle_or_filtering(self) -> None:
        """可观测性后端失败时 WebSocket 仍能启停，拒绝消息仍被静默忽略。"""
        sdk = FakeOfficialSdk()
        transport = self._transport(sdk, observer=FailingObserver())

        await transport.connect()
        await sdk.channel.handlers["message"](
            SimpleNamespace(
                id="om_denied",
                create_time=1_786_118_400_000,
                conversation=SimpleNamespace(chat_id="oc_allowed", chat_type="p2p"),
                sender=SimpleNamespace(
                    open_id="ou_intruder",
                    sender_type="user",
                    is_bot=False,
                ),
                mentioned_bot=False,
                body_text="must-remain-ignored",
                raw_content_type="text",
                raw={"header": {"event_id": "evt_denied"}},
            )
        )
        await transport.disconnect()

        self.assertTrue(sdk.channel.disconnected)
        self.assertEqual(self.received, [])

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
        self.assertNotIn("cardAction", sdk.channel.handlers)

    async def test_card_action_callback_extracts_only_actor_value_and_targets(self) -> None:
        """按钮事件通过有限字段回调，严格 payload 解析留给 Approval Controller。"""
        sdk = FakeOfficialSdk()
        transport = self._transport(sdk)
        await transport.connect()
        value = {
            "miniclaw_action": "approval",
            "approval_id": 7,
            "decision": "once",
        }

        await sdk.channel.handlers["cardAction"](
            SimpleNamespace(
                operator=SimpleNamespace(open_id="ou_owner"),
                action=SimpleNamespace(value=value),
                chat_id="oc_allowed",
                message_id="om_card",
                raw={"authorization": "must-not-forward"},
            )
        )

        self.assertEqual(
            self.card_actions,
            [("ou_owner", value, "oc_allowed", "om_card")],
        )

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

    async def test_generic_experience_maps_to_feishu_reaction_and_card(self) -> None:
        """通用 Experience 意图应在 Transport 内映射为飞书 reaction/card。"""
        sdk = FakeOfficialSdk((FakeSdkSendResult(True, "om_progress"),))
        transport = self._transport(sdk)
        now = datetime.now(UTC)
        event = StoredInboundEvent(
            key=InboundEventKey("feishu", "work", "om_inbound"),
            event_id="evt_inbound",
            external_user_id="ou_owner",
            external_conversation_id="oc_allowed",
            chat_type="p2p",
            message_type="text",
            content="private question",
            reply_to_message_id="om_inbound",
            session_id=1,
            status="running",
            attempts=1,
            last_error_code=None,
            received_at=now,
            updated_at=now,
        )

        token = await transport.start_typing(event)
        receipt = await transport.create_progress(
            event,
            "第一段",
            idempotency_key="progress-uuid",
        )
        await transport.update_progress(
            receipt.platform_message_id,
            "完整回答",
            incomplete=False,
            completed=True,
        )
        await transport.stop_typing(token)

        self.assertIsNotNone(token)
        self.assertNotEqual(token, "reaction_typing")
        self.assertEqual(sdk.channel.typing_added, ["om_inbound"])
        self.assertEqual(
            sdk.channel.typing_removed,
            [("om_inbound", "reaction_typing")],
        )
        self.assertEqual(sdk.channel.sent[0][0], "oc_allowed")
        self.assertEqual(sdk.channel.sent[0][2].uuid, "progress-uuid")
        self.assertEqual(
            sdk.channel.sent[0][1]["card"]["body"]["elements"][0]["content"],
            "第一段",
        )
        self.assertEqual(
            sdk.channel.cards_updated[-1][1]["body"]["elements"][0]["content"],
            "完整回答",
        )
        self.assertNotIn("reaction_typing", repr(transport))
        self.assertNotIn("private question", repr(transport))

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
