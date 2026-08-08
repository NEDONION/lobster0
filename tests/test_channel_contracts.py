"""Channel 公共消息与 Transport 契约测试。"""

import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

from miniclaw.channels.base import (
    ChannelTransport,
    InboundMessage,
    OutboundMessage,
    SendReceipt,
)


class ChannelContractTest(unittest.TestCase):
    """验证平台 Adapter 共享的最小、不可变且可脱敏契约。"""

    def test_messages_are_immutable_and_repr_omits_content_and_external_ids(self) -> None:
        """标准消息不能被 Worker 改写，repr 不能泄露正文和平台标识。"""
        inbound = InboundMessage(
            channel="feishu",
            account_id="default",
            event_id="evt_sensitive",
            message_id="om_sensitive",
            external_user_id="ou_sensitive",
            external_conversation_id="oc_sensitive",
            chat_type="p2p",
            message_type="text",
            text="private content",
            reply_to_message_id="om_sensitive",
            received_at=datetime(2026, 8, 8, tzinfo=UTC),
        )
        outbound = OutboundMessage(
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_sensitive",
            reply_to_message_id="om_sensitive",
            content="private reply",
            kind="message",
        )

        with self.assertRaises(FrozenInstanceError):
            inbound.text = "changed"  # type: ignore[misc]
        for secret in (
            "evt_sensitive",
            "om_sensitive",
            "ou_sensitive",
            "oc_sensitive",
            "private content",
            "private reply",
        ):
            self.assertNotIn(secret, repr(inbound) + repr(outbound))

    def test_transport_protocol_and_send_receipt_are_runtime_checkable(self) -> None:
        """实现者应能在测试中被识别为 Transport，并返回稳定发送凭据。"""

        class CompleteTransport:
            async def connect(self) -> None:
                return None

            async def disconnect(self) -> None:
                return None

            async def send(
                self,
                message: OutboundMessage,
                *,
                idempotency_key: str,
            ) -> SendReceipt:
                return SendReceipt("om_sent")

        self.assertIsInstance(CompleteTransport(), ChannelTransport)
        self.assertEqual(SendReceipt("om_sent").platform_message_id, "om_sent")


if __name__ == "__main__":
    unittest.main()
