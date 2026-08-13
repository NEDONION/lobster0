"""飞书消息归一化、白名单和群 mention 行为测试。"""

import unittest
from dataclasses import replace

from lobster0.channels.base import IgnoredInbound, InboundMessage
from lobster0.channels.feishu import FeishuAdapter
from lobster0.config import FeishuConfig
from tests.fakes.fake_channel import FakeFeishuMessage


class FeishuAdapterTest(unittest.TestCase):
    """验证不可信飞书消息只在显式允许后进入 Agent。"""

    def setUp(self) -> None:
        """建立包含 Owner、同伴和一个群的严格配置。"""
        self.config = FeishuConfig(
            enabled=True,
            account_id="default",
            owner_open_id="ou_owner",
            allowed_open_ids=("ou_owner", "ou_friend"),
            allowed_chat_ids=("oc_allowed",),
            allow_group_mentions=True,
            message_max_chars=1000,
        )
        self.adapter = FeishuAdapter(self.config)

    def test_private_text_is_normalized_without_raw_sdk_object(self) -> None:
        """允许的私聊文本应成为完整内部消息，并保留稳定 message_id。"""
        result = self.adapter.normalize(FakeFeishuMessage(body_text="  你好\n世界  "))

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.channel, "feishu")
        self.assertEqual(result.account_id, "default")
        self.assertEqual(result.message_id, "om_test")
        self.assertEqual(result.external_user_id, "ou_owner")
        self.assertEqual(result.external_conversation_id, "oc_allowed")
        self.assertEqual(result.chat_type, "p2p")
        self.assertEqual(result.text, "你好\n世界")
        self.assertEqual(result.reply_to_message_id, "om_test")
        self.assertEqual(result.replied_to_message_id, "")
        self.assertFalse(hasattr(result, "raw"))

    def test_private_post_uses_sdk_flattened_safe_text(self) -> None:
        """飞书客户端生成的 post 应使用 SDK 已安全扁平化的正文进入 Agent。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(
                raw_content_type="post",
                body_text="你好，请回复 Lobster0 飞书链路已打通",
            )
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.text, "你好，请回复 Lobster0 飞书链路已打通")

    def test_group_requires_allowlisted_chat_sender_and_bot_mention(self) -> None:
        """群聊只有 Chat、发送者和明确 mention 三道门同时通过才可进入。"""
        allowed = FakeFeishuMessage(
            chat_type="group",
            mentioned_bot=True,
            body_text="帮我总结",
        )
        result = self.adapter.normalize(allowed)

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.chat_type, "group")
        self.assertEqual(result.text, "帮我总结")

        denied_cases = (
            (replace(allowed, mentioned_bot=False), "mention_required"),
            (replace(allowed, chat_id="oc_denied"), "chat_denied"),
            (replace(allowed, sender_id="ou_denied"), "sender_denied"),
        )
        for message, reason in denied_cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.adapter.normalize(message), IgnoredInbound(reason))

    def test_bot_unsupported_invalid_and_oversized_messages_are_ignored(self) -> None:
        """机器人回声、非文本、非法 ID、空文本和超限输入必须给稳定忽略原因。"""
        cases = (
            (FakeFeishuMessage(sender_is_bot=True), "bot_message"),
            (FakeFeishuMessage(sender_type="app"), "bot_message"),
            # image 现在受支持（走视觉模型）；仍然不受支持的是贴纸这类。
            (FakeFeishuMessage(raw_content_type="sticker"), "unsupported_message"),
            (FakeFeishuMessage(message_id="bad"), "invalid_message"),
            (FakeFeishuMessage(sender_id="bad"), "invalid_message"),
            (FakeFeishuMessage(body_text=" \n\t "), "empty_message"),
            (FakeFeishuMessage(body_text="x" * 1001), "message_too_large"),
        )
        for message, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(self.adapter.normalize(message), IgnoredInbound(reason))

    def test_control_characters_are_removed_but_layout_is_preserved(self) -> None:
        """C0/C1/ANSI 控制字符不能进入 Agent，正常换行和 Tab 应保留。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(body_text="hello\x00\x1b[31m\n\tworld\x7f\x85")
        )

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(result.text, "hello[31m\n\tworld")

    def test_repr_and_errors_never_include_raw_message_or_secret_values(self) -> None:
        """错误和内部消息表示不得包含正文、完整 ID 或凭证样例。"""
        message = FakeFeishuMessage(
            event_id="evt_private",
            message_id="om_private",
            sender_id="ou_owner",
            body_text="secret body",
        )
        result = self.adapter.normalize(message)

        self.assertIsInstance(result, InboundMessage)
        representation = repr(result)
        for value in ("evt_private", "om_private", "ou_owner", "secret body"):
            self.assertNotIn(value, representation)


if __name__ == "__main__":
    unittest.main()


class FeishuRepliedToMessageTest(unittest.TestCase):
    """验证"这条消息回复了哪条"能被安全取出，取不到时不影响正常收消息。"""

    def setUp(self) -> None:
        """复用允许 Owner 私聊的最小配置。"""
        self.adapter = FeishuAdapter(
            FeishuConfig(
                enabled=True,
                account_id="default",
                owner_open_id="ou_owner",
                allowed_open_ids=("ou_owner",),
                allowed_chat_ids=(),
                allow_group_mentions=False,
                message_max_chars=1000,
            )
        )

    def test_parent_message_id_is_carried_through(self) -> None:
        """回复某条消息时，被回复的平台 message ID 必须原样带进内部消息。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(body_text="/good", parent_message_id="om_parent")
        )

        assert isinstance(result, InboundMessage)
        self.assertEqual(result.replied_to_message_id, "om_parent")

    def test_absent_parent_degrades_to_empty_not_an_error(self) -> None:
        """不是回复时必须安全退化为空字符串，而不是让整条消息失败。"""
        result = self.adapter.normalize(FakeFeishuMessage(body_text="你好"))

        assert isinstance(result, InboundMessage)
        self.assertEqual(result.replied_to_message_id, "")

    def test_malformed_parent_id_is_treated_as_not_a_reply(self) -> None:
        """形状非法的 parent ID 不能被当作有效目标，必须按"不是回复"处理。"""
        for invalid in ("", "not-a-message-id", "om_" + "x" * 300):
            with self.subTest(invalid=invalid):
                result = self.adapter.normalize(
                    FakeFeishuMessage(body_text="/good", parent_message_id=invalid)
                )
                assert isinstance(result, InboundMessage)
                self.assertEqual(result.replied_to_message_id, "")


class FeishuReplyRefExtractionTest(unittest.TestCase):
    """锁定真实 SDK 的回复来源：``message.reply.message_id``。

    真实事故：首版按飞书 OpenAPI 文档猜的 ``parent_id``，但 ``lark_channel`` 已经把回复
    关系解析成了 ``reply: ReplyRef``，``parent_id`` 属性根本不存在，导致飞书里 /good
    永远提示"不是回复"。这条测试固定住真实 SDK 的字段来源。
    """

    def test_reply_ref_is_preferred_over_parent_id(self) -> None:
        """SDK 提供 reply.message_id 时必须优先使用它。"""
        from lobster0.channels.feishu import _parent_message_id

        class _ReplyRef:
            message_id = "om_from_reply_ref"

        class _Message:
            reply = _ReplyRef()
            parent_id = "om_from_parent_id"
            raw = None

        self.assertEqual(_parent_message_id(_Message()), "om_from_reply_ref")

    def test_falls_back_to_parent_id_then_raw_event(self) -> None:
        """SDK 未填充 reply 时依次回退到 parent_id 与原始事件 JSON。"""
        from lobster0.channels.feishu import _parent_message_id

        class _OnlyParent:
            reply = None
            parent_id = "om_from_parent_id"
            raw = None

        class _OnlyRaw:
            reply = None
            raw = {"event": {"message": {"parent_id": "om_from_raw"}}}

        self.assertEqual(_parent_message_id(_OnlyParent()), "om_from_parent_id")
        self.assertEqual(_parent_message_id(_OnlyRaw()), "om_from_raw")

    def test_no_reply_anywhere_degrades_to_empty(self) -> None:
        """三个来源都取不到时必须安全退化为空，而不是抛异常。"""
        from lobster0.channels.feishu import _parent_message_id

        class _Nothing:
            reply = None
            raw = None

        self.assertEqual(_parent_message_id(_Nothing()), "")
        self.assertEqual(_parent_message_id(object()), "")
