"""Provider 图像内容序列化与"字节不外泄"边界测试。"""

import base64
import hashlib
import json
import unittest

from lobster0.providers.base import ImagePart, ModelMessage
from lobster0.providers.openai_compatible import _message_payload

_PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 4


def _part(media_type: str = "image/png", data: bytes = _PNG) -> ImagePart:
    """构造一段带正确哈希的图像。"""
    return ImagePart(
        media_type=media_type,
        content_hash=hashlib.sha256(data).hexdigest(),
        data=data,
    )


class ImagePartBoundaryTest(unittest.TestCase):
    """图像字节不得进入 repr、日志或任何诊断输出。"""

    def test_repr_never_contains_image_bytes(self) -> None:
        """repr 只显示类型、哈希前缀与大小。"""
        rendered = repr(_part())

        self.assertNotIn("fake-image-bytes", rendered)
        self.assertNotIn(base64.b64encode(_PNG).decode("ascii")[:20], rendered)
        self.assertIn("image/png", rendered)
        self.assertIn("byte_size=", rendered)

    def test_message_repr_does_not_leak_images(self) -> None:
        """整条消息的 repr 也不能带出字节。"""
        message = ModelMessage(role="user", content="看看这张图", images=(_part(),))

        self.assertNotIn("fake-image-bytes", repr(message))

    def test_unsupported_media_type_is_rejected(self) -> None:
        """只接受 png/jpeg，其他类型必须在构造期拒绝。"""
        with self.assertRaises(ValueError):
            ImagePart(
                media_type="image/gif",
                content_hash="a" * 64,
                data=_PNG,
            )

    def test_empty_or_unhashed_image_is_rejected(self) -> None:
        """空字节与缺失哈希都不构成可发送的图像。"""
        with self.assertRaises(ValueError):
            ImagePart(media_type="image/png", content_hash="a" * 64, data=b"")
        with self.assertRaises(ValueError):
            ImagePart(media_type="image/png", content_hash="short", data=_PNG)


class SerializationTest(unittest.TestCase):
    """验证只有携带图像时才切换成 content parts 数组。"""

    def test_text_only_message_keeps_a_string_content(self) -> None:
        """纯文本消息必须保持字符串 content，不给兼容实现增加失败面。"""
        payload = _message_payload(ModelMessage(role="user", content="你好"))

        self.assertEqual(payload["content"], "你好")

    def test_image_message_becomes_content_parts(self) -> None:
        """带图消息序列化为 text + image_url 数组。"""
        payload = _message_payload(
            ModelMessage(role="user", content="看看这张图", images=(_part(),))
        )

        parts = payload["content"]
        self.assertIsInstance(parts, list)
        self.assertEqual(parts[0], {"type": "text", "text": "看看这张图"})
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertTrue(
            parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
        )

    def test_encoded_image_round_trips(self) -> None:
        """base64 必须可还原为原始字节，避免静默截断。"""
        payload = _message_payload(
            ModelMessage(role="user", content="识别", images=(_part(),))
        )

        encoded = payload["content"][1]["image_url"]["url"].split(",", 1)[1]

        self.assertEqual(base64.b64decode(encoded), _PNG)

    def test_multiple_images_are_all_serialized_after_the_text(self) -> None:
        """多张图按顺序跟在文本之后。"""
        second = _part(media_type="image/jpeg", data=_PNG + b"second")
        payload = _message_payload(
            ModelMessage(role="user", content="对比这两张", images=(_part(), second))
        )

        parts = payload["content"]
        self.assertEqual(len(parts), 3)
        self.assertEqual(parts[0]["type"], "text")
        self.assertTrue(
            parts[2]["image_url"]["url"].startswith("data:image/jpeg;base64,")
        )

    def test_payload_remains_json_serializable(self) -> None:
        """带图 payload 必须能被 json 编码，否则请求发不出去。"""
        payload = _message_payload(
            ModelMessage(role="user", content="识别", images=(_part(),))
        )

        self.assertIn("image_url", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
