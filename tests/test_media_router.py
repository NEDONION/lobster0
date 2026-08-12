"""附件路由的能力裁决与"不请求就不发送"测试。"""

import unittest

from lobster0.media.router import (
    MediaRouteError,
    MediaRouter,
    ModelCapabilities,
)

_IMAGE = ("art_0123456789abcdef", "image/jpeg")
_TEXT_FILE = ("art_fedcba9876543210", "text/plain")


def _router(*, vision: bool, model: str = "deepseek-v4-pro") -> MediaRouter:
    """构造一个绑定固定模型能力的路由器。"""
    return MediaRouter(ModelCapabilities(model=model, vision=vision))


class ExplicitRequestTest(unittest.TestCase):
    """规则一：Owner 没有明确要求处理图像时，图像绝不进入模型请求。"""

    def test_unrequested_attachment_is_not_automatically_sent(self) -> None:
        """上传一张图只说"谢谢"，不该产生任何图像发送。"""
        route = _router(vision=True).resolve((_IMAGE,), user_text="谢谢")

        self.assertFalse(route.sends_images)
        self.assertEqual(route.image_artifact_ids, ())

    def test_explicit_request_routes_the_image(self) -> None:
        """明确要求"看看里面的文字"时才路由。"""
        route = _router(vision=True).resolve(
            (_IMAGE,), user_text="你能看到里面的文字吗"
        )

        self.assertTrue(route.sends_images)
        self.assertEqual(route.image_artifact_ids, (_IMAGE[0],))

    def test_various_explicit_intents_are_recognised(self) -> None:
        """常见的中英文请求措辞都应被识别。"""
        router = _router(vision=True)
        for text in (
            "帮我识别这张图",
            "这张图里是什么",
            "描述一下这个截图",
            "read the text in this image",
            "extract the table",
        ):
            with self.subTest(text=text):
                self.assertTrue(router.resolve((_IMAGE,), user_text=text).sends_images)

    def test_polite_or_empty_text_never_routes(self) -> None:
        """礼貌用语与空文本都不构成请求。"""
        router = _router(vision=True)
        for text in ("谢谢", "收到", "", "好的"):
            with self.subTest(text=text):
                self.assertFalse(router.resolve((_IMAGE,), user_text=text).sends_images)


class CapabilityGateTest(unittest.TestCase):
    """规则二：模型不支持视觉时明确失败，绝不静默改用其他模型。"""

    def test_image_is_sent_only_to_vision_capable_model(self) -> None:
        """视觉模型才允许接收图像。"""
        route = _router(vision=True).resolve((_IMAGE,), user_text="看看这张图")

        self.assertTrue(route.capabilities.vision)
        self.assertTrue(route.sends_images)

    def test_non_vision_model_fails_with_an_actionable_error(self) -> None:
        """不支持视觉时必须报错，且提示要改配置而不是自动换模型。"""
        with self.assertRaises(MediaRouteError) as raised:
            _router(vision=False).resolve((_IMAGE,), user_text="看看这张图")

        self.assertEqual(raised.exception.code, "model_lacks_vision")
        self.assertIn("deepseek-v4-pro", str(raised.exception))
        self.assertIn("配置", str(raised.exception))

    def test_non_vision_model_without_a_request_does_not_fail(self) -> None:
        """没有请求处理图像时，非视觉模型不该被打断正常对话。"""
        route = _router(vision=False).resolve((_IMAGE,), user_text="谢谢")

        self.assertFalse(route.sends_images)


class NonImageAttachmentTest(unittest.TestCase):
    """非图像附件不走视觉路由。"""

    def test_text_attachment_never_routes_as_image(self) -> None:
        """文本附件即使被明确询问也不进入图像通道。"""
        route = _router(vision=False).resolve((_TEXT_FILE,), user_text="看看里面写的什么")

        self.assertFalse(route.sends_images)

    def test_only_image_attachments_are_selected(self) -> None:
        """混合附件里只挑出图像。"""
        route = _router(vision=True).resolve(
            (_TEXT_FILE, _IMAGE), user_text="识别这张图"
        )

        self.assertEqual(route.image_artifact_ids, (_IMAGE[0],))


if __name__ == "__main__":
    unittest.main()
