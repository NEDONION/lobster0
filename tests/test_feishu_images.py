"""飞书图片消息必须进入管线，并在准入之后换成本地可读的图片路径。

## 这个文件为什么分成两段

图片链路有两个各自独立的环节，早前把它们混成一段测，结果两段都没被真正验证：

1. **准入**（``FeishuAdapter.normalize``）：只看描述符判断"带没带图"。判断是免费的。
2. **取回**（``FeishuTransport._resolve_images``）：真的走一次网络下载。

早前的实现以为 SDK 会自动把图片下载好，直接在 ``message.resources`` 上找
``decision`` 和 ``path``——这两个字段在 ``ResourceDescriptor`` 上根本不存在，于是
循环每次都跳过，整条链路静默失效。签名层面的守卫在
``tests/test_feishu_sdk_contract.py``；这里验证行为。
"""

import unittest
from pathlib import Path
from types import SimpleNamespace

from lobster0.channels.base import IgnoredInbound, InboundMessage
from lobster0.channels.feishu import FeishuAdapter, FeishuTransport
from lobster0.config import FeishuConfig
from tests.fakes.fake_channel import FakeFeishuMessage, FakeOfficialSdk


def _descriptor(resource_type: str = "image", file_key: str = "img_v3_x"):
    """构造一个与真实 ``ResourceDescriptor`` 同形的描述符：没有 path，没有 decision。"""
    return SimpleNamespace(
        type=resource_type, file_key=file_key, file_name=None, duration_ms=None
    )


def _cached(
    *,
    decision: str = "cached",
    mime_type: str = "image/png",
    path: str = "/tmp/cached.png",
):
    """构造一个与真实 ``CachedResource`` 同形的下载结果。"""
    return SimpleNamespace(
        decision=decision,
        mime_type=mime_type,
        path=Path(path),
        sha256="a" * 64,
        size=4,
        reason=None,
    )


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

    def test_image_message_is_admitted(self) -> None:
        """图片消息必须进入管线，不再被当作不支持的类型丢弃。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(
                raw_content_type="image",
                body_text="看看这张",
                image_descriptors=(_descriptor(),),
            )
        )

        self.assertIsInstance(result, InboundMessage)

    def test_image_without_text_still_carries_a_prompt(self) -> None:
        """只发图不配文字时也要有可用正文，否则整轮会被当成空消息丢弃。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(
                raw_content_type="image",
                body_text="",
                image_descriptors=(_descriptor(),),
            )
        )

        assert isinstance(result, InboundMessage)
        self.assertTrue(result.text.strip())

    def test_admission_does_not_carry_paths(self) -> None:
        """准入阶段不得带出任何图片路径——那时还没有下载过任何东西。"""
        result = self.adapter.normalize(
            FakeFeishuMessage(
                raw_content_type="image",
                body_text="看看这张",
                image_descriptors=(_descriptor(),),
            )
        )

        assert isinstance(result, InboundMessage)
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


class FeishuImageResolutionTest(unittest.IsolatedAsyncioTestCase):
    """验证取回阶段：必须真的发起下载，且只认真正落盘的结果。"""

    def setUp(self) -> None:
        """构造一个记录 resolve 调用的假 SDK Transport。"""
        self.sdk = FakeOfficialSdk()
        self.transport = FeishuTransport(
            FeishuConfig(
                enabled=True,
                account_id="default",
                owner_open_id="ou_owner",
                allowed_open_ids=("ou_owner",),
            ),
            app_id="cli_x",
            app_secret="secret",
            on_inbound=self._collect,
            sdk=self.sdk,
        )
        self.inbound: list[InboundMessage] = []

    async def _collect(self, message: InboundMessage) -> None:
        """记录被投递到管线的消息。"""
        self.inbound.append(message)

    async def test_descriptors_are_resolved_into_local_paths(self) -> None:
        """描述符必须被显式下载成本地路径——SDK 不会自动做这件事。"""
        self.sdk.channel.cached_resources = [_cached()]

        paths = await self.transport._resolve_images("om_x", (_descriptor(),))

        self.assertEqual(paths, ((Path("/tmp/cached.png"), "image/png"),))
        self.assertEqual(
            self.sdk.channel.resolve_calls,
            [("om_x", 1)],
            "必须调用一次 resolve_resources_to_cache，否则永远拿不到图片",
        )

    async def test_uncached_results_are_not_exposed(self) -> None:
        """SDK 没能落盘的结果不能被当成可读路径带出去。"""
        for decision in ("skipped", "rejected"):
            with self.subTest(decision=decision):
                self.sdk.channel.cached_resources = [_cached(decision=decision)]

                paths = await self.transport._resolve_images("om_x", (_descriptor(),))

                self.assertEqual(paths, ())

    async def test_non_image_mime_is_not_exposed_as_an_image(self) -> None:
        """即便 SDK 缓存成功，非图片 MIME 也不能送进视觉模型。"""
        self.sdk.channel.cached_resources = [_cached(mime_type="application/pdf")]

        paths = await self.transport._resolve_images("om_x", (_descriptor(),))

        self.assertEqual(paths, ())

    async def test_no_descriptors_means_no_network_call(self) -> None:
        """没有图片描述符时不得发起任何下载——纯文字轮次不该付网络代价。"""
        paths = await self.transport._resolve_images("om_x", ())

        self.assertEqual(paths, ())
        self.assertEqual(self.sdk.channel.resolve_calls, [])

    async def test_download_failure_degrades_instead_of_dropping_the_message(
        self,
    ) -> None:
        """下载失败只让这一轮没有图，不能让 Owner 的消息整条丢掉。"""
        self.sdk.channel.resolve_error = RuntimeError("network down")

        paths = await self.transport._resolve_images("om_x", (_descriptor(),))

        self.assertEqual(paths, ())


if __name__ == "__main__":
    unittest.main()
