"""飞书图片消息必须能进入管线，并带出已缓存的本地图片路径。"""

import unittest
from pathlib import Path

from lobster0.channels.base import IgnoredInbound, InboundMessage
from lobster0.channels.feishu import FeishuAdapter
from lobster0.config import FeishuConfig
from tests.fakes.fake_channel import FakeFeishuMessage


class _Resource:
    """模拟 SDK 已缓存好的一个资源。"""

    def __init__(
        self,
        *,
        decision: str = "cached",
        mime_type: str = "image/png",
        path: str = "/tmp/cached.png",
        resource_type: str = "image",
    ) -> None:
        self.decision = decision
        self.mime_type = mime_type
        self.path = Path(path)
        self.resource_type = resource_type
        self.sha256 = "a" * 64
        self.size = 4


class FeishuImageAdmissionTest(unittest.TestCase):
    """验证图片消息不再被当作 unsupported 丢弃。"""

    def setUp(self) -> None:
        """只允许 Owner 私聊的最小配置。"""
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

    def test_image_message_is_admitted_with_its_cached_path(self) -> None:
        """图片消息必须进入管线，并带出本地缓存路径。"""
        message = FakeFeishuMessage(
            raw_content_type="image",
            body_text="",
            resources=[_Resource()],
        )

        result = self.adapter.normalize(message)

        self.assertIsInstance(result, InboundMessage)
        assert isinstance(result, InboundMessage)
        self.assertEqual(len(result.image_paths), 1)
        self.assertEqual(result.image_paths[0], (Path("/tmp/cached.png"), "image/png"))

    def test_image_without_text_still_carries_a_prompt(self) -> None:
        """只发图不配文字时也要有可用正文，否则整轮会被当成空消息丢弃。"""
        message = FakeFeishuMessage(
            raw_content_type="image", body_text="", resources=[_Resource()]
        )

        result = self.adapter.normalize(message)

        assert isinstance(result, InboundMessage)
        self.assertTrue(result.text.strip())

    def test_uncached_resource_is_not_exposed(self) -> None:
        """SDK 没能缓存下来的资源不能被当成可读路径带出去。"""
        for decision in ("skipped", "rejected"):
            with self.subTest(decision=decision):
                message = FakeFeishuMessage(
                    raw_content_type="image",
                    body_text="看看这张",
                    resources=[_Resource(decision=decision)],
                )

                result = self.adapter.normalize(message)

                assert isinstance(result, InboundMessage)
                self.assertEqual(result.image_paths, ())

    def test_non_image_resource_is_not_exposed_as_an_image(self) -> None:
        """音频、文件等不能被当作图片送进视觉模型。"""
        message = FakeFeishuMessage(
            raw_content_type="file",
            body_text="这是文件",
            resources=[_Resource(mime_type="application/pdf", resource_type="file")],
        )

        result = self.adapter.normalize(message)

        if isinstance(result, InboundMessage):
            self.assertEqual(result.image_paths, ())

    def test_text_message_still_has_no_images(self) -> None:
        """纯文字消息必须保持原行为，不受本次改动影响。"""
        result = self.adapter.normalize(FakeFeishuMessage(body_text="你好"))

        assert isinstance(result, InboundMessage)
        self.assertEqual(result.image_paths, ())

    def test_still_unsupported_types_are_rejected(self) -> None:
        """贴纸、位置等仍然不受支持，必须继续被拒绝。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(raw_content_type="sticker", body_text="")
        )

        self.assertIsInstance(result, IgnoredInbound)


if __name__ == "__main__":
    unittest.main()
