"""带图的请求必须自动走视觉模型，纯文字请求必须留在主模型。"""

import hashlib
import unittest

from lobster0.media.switching import VisionSwitchingProvider
from lobster0.providers.base import ImagePart, ModelMessage, ModelRequest, ModelResponse

_DATA = b"\x89PNG"


def _image() -> ImagePart:
    """构造一张合法图片分片。"""
    return ImagePart(
        media_type="image/png",
        content_hash=hashlib.sha256(_DATA).hexdigest(),
        data=_DATA,
    )


def _request(*, with_image: bool, model: str = "deepseek-v4-pro") -> ModelRequest:
    """构造带图或不带图的请求。"""
    message = ModelMessage(
        role="user",
        content="这是什么",
        images=(_image(),) if with_image else (),
    )
    return ModelRequest(model=model, messages=(message,))


class _RecordingProvider:
    """记录收到的请求并返回固定响应。"""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, on_text=None) -> ModelResponse:
        del on_text
        self.requests.append(request)
        return ModelResponse(
            content=self.tag,
            tool_calls=(),
            reasoning_content=None,
            finish_reason="stop",
            input_tokens=1,
            output_tokens=1,
            provider_request_id=f"req-{self.tag}",
        )


class VisionSwitchingTest(unittest.IsolatedAsyncioTestCase):
    """验证按"这一轮有没有图"选择后端。"""

    def setUp(self) -> None:
        """准备主 provider 与视觉 provider。"""
        self.text = _RecordingProvider("text")
        self.vision = _RecordingProvider("vision")
        self.provider = VisionSwitchingProvider(
            self.text, vision=self.vision, vision_model="qwen3-vl-flash"
        )

    async def test_text_only_request_stays_on_the_main_provider(self) -> None:
        """没有图时必须留在主模型，纯文字对话不该多花钱。"""
        response = await self.provider.complete(_request(with_image=False))

        self.assertEqual(response.content, "text")
        self.assertEqual(len(self.text.requests), 1)
        self.assertEqual(self.vision.requests, [])
        self.assertEqual(self.text.requests[0].model, "deepseek-v4-pro")

    async def test_request_with_images_switches_provider_and_model(self) -> None:
        """带图时必须换到视觉 provider，并把模型名一起换掉。"""
        response = await self.provider.complete(_request(with_image=True))

        self.assertEqual(response.content, "vision")
        self.assertEqual(self.text.requests, [])
        self.assertEqual(len(self.vision.requests), 1)
        self.assertEqual(self.vision.requests[0].model, "qwen3-vl-flash")

    async def test_images_survive_the_switch(self) -> None:
        """换 provider 不能把图弄丢。"""
        await self.provider.complete(_request(with_image=True))

        sent = self.vision.requests[0].messages[-1]
        self.assertEqual(len(sent.images), 1)
        self.assertEqual(sent.images[0].data, _DATA)

    async def test_without_a_vision_provider_images_fail_loudly(self) -> None:
        """未配置视觉后端时带图必须报错。

        绝不静默发给看不了图的模型——那会让它对着看不见的图编内容，
        比直接失败危险得多。
        """
        from lobster0.media.router import MediaRouteError

        provider = VisionSwitchingProvider(self.text, vision=None, vision_model=None)

        with self.assertRaises(MediaRouteError) as raised:
            await provider.complete(_request(with_image=True))
        self.assertEqual(raised.exception.code, "vision_not_configured")
        self.assertEqual(self.text.requests, [])

    async def test_without_a_vision_provider_text_still_works(self) -> None:
        """没配视觉后端不影响正常的纯文字对话。"""
        provider = VisionSwitchingProvider(self.text, vision=None, vision_model=None)

        response = await provider.complete(_request(with_image=False))

        self.assertEqual(response.content, "text")

    async def test_streaming_handler_is_passed_through(self) -> None:
        """流式回调必须原样传给被选中的后端。"""
        seen: list[str] = []

        class _Streaming(_RecordingProvider):
            async def complete(self, request, on_text=None):
                if on_text is not None:
                    await on_text("chunk")
                return await super().complete(request)

        streaming = _Streaming("vision")
        provider = VisionSwitchingProvider(
            self.text, vision=streaming, vision_model="qwen3-vl-flash"
        )

        async def collect(text: str) -> None:
            seen.append(text)

        await provider.complete(_request(with_image=True), collect)

        self.assertEqual(seen, ["chunk"])


if __name__ == "__main__":
    unittest.main()
