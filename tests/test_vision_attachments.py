"""上传的图片必须真的进入模型请求，而不是只留下一段文字摘要。"""

import tempfile
import unittest
from pathlib import Path

from lobster0.media.attachments import build_image_parts
from lobster0.providers.base import ImagePart


class _FakeArtifact:
    """最小 Artifact 元数据，``path`` 指向真实临时文件。"""

    def __init__(self, artifact_id: str, media_type: str, path: Path) -> None:
        import hashlib

        self.artifact_id = artifact_id
        self.media_type = media_type
        self.path = path
        data = path.read_bytes()
        self.content_hash = hashlib.sha256(data).hexdigest()
        self.byte_size = len(data)


class _FakeStore:
    """按 id 返回落在临时目录里的真实 Artifact。"""

    def __init__(self, root: Path, items: dict[str, tuple[str, bytes]]) -> None:
        self._artifacts: dict[str, _FakeArtifact] = {}
        self.read_calls: list[str] = []
        for artifact_id, (media_type, data) in items.items():
            path = root / artifact_id
            path.write_bytes(data)
            self._artifacts[artifact_id] = _FakeArtifact(artifact_id, media_type, path)

    def read_metadata(self, artifact_id: str) -> _FakeArtifact:
        self.read_calls.append(artifact_id)
        return self._artifacts[artifact_id]


class BuildImagePartsTest(unittest.TestCase):
    """验证只有图片被读取字节，其他类型只留摘要。"""

    def setUp(self) -> None:
        """为每个用例准备独立的 Artifact 目录。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)

    def test_image_attachment_becomes_an_image_part(self) -> None:
        """图片必须被真的读出字节并变成 ImagePart。"""
        store = _FakeStore(self.root, {"art_1": ("image/png", b"\x89PNG")})

        parts = build_image_parts(store, ({"artifact_id": "art_1", "media_type": "image/png"},))

        self.assertEqual(len(parts), 1)
        self.assertIsInstance(parts[0], ImagePart)
        self.assertEqual(parts[0].media_type, "image/png")
        self.assertEqual(parts[0].data, b"\x89PNG")

    def test_non_image_attachment_is_skipped_without_reading_bytes(self) -> None:
        """PDF、压缩包等不该被读进模型请求，也不该白白读盘。"""
        store = _FakeStore(self.root, {"art_pdf": ("application/pdf", b"%PDF")})

        parts = build_image_parts(
            store, ({"artifact_id": "art_pdf", "media_type": "application/pdf"},)
        )

        self.assertEqual(parts, ())
        self.assertEqual(store.read_calls, [])

    def test_no_attachments_reads_nothing(self) -> None:
        """没有附件时不能有任何读盘。"""
        store = _FakeStore(self.root, {})

        self.assertEqual(build_image_parts(store, ()), ())
        self.assertEqual(store.read_calls, [])

    def test_content_hash_is_verified_against_the_bytes(self) -> None:
        """字节必须与 Store 记录的哈希一致，防止读到被替换的文件。"""
        store = _FakeStore(self.root, {"art_1": ("image/png", b"\x89PNG")})
        # 元数据记的还是旧哈希，磁盘上的字节已被换掉。
        (self.root / "art_1").write_bytes(b"TAMPERED")

        with self.assertRaises(ValueError):
            build_image_parts(
                store, ({"artifact_id": "art_1", "media_type": "image/png"},)
            )

    def test_unsupported_image_type_is_skipped(self) -> None:
        """视觉模型不支持的图片类型只留摘要，不硬塞进请求。"""
        store = _FakeStore(self.root, {"art_tiff": ("image/tiff", b"II*\x00")})

        self.assertEqual(
            build_image_parts(
                store, ({"artifact_id": "art_tiff", "media_type": "image/tiff"},)
            ),
            (),
        )


if __name__ == "__main__":
    unittest.main()


class AttachImagesToRequestTest(unittest.TestCase):
    """图片必须挂在最后一条用户消息上，而不是散落或覆盖历史。"""

    def setUp(self) -> None:
        """准备一张合法图片分片。"""
        import hashlib

        from lobster0.providers.base import ImagePart

        data = b"\x89PNG"
        self.part = ImagePart(
            media_type="image/png",
            content_hash=hashlib.sha256(data).hexdigest(),
            data=data,
        )

    def _request(self, *roles: str):
        """构造一个只含指定角色序列的最小请求。"""
        from lobster0.providers.base import ModelMessage, ModelRequest

        return ModelRequest(
            model="deepseek-v4-pro",
            messages=tuple(
                ModelMessage(role=role, content=f"{role}-text") for role in roles
            ),
        )

    def test_images_attach_to_the_last_user_message(self) -> None:
        """必须挂到最后一条用户消息，而不是 system 或历史里的旧消息。"""
        from lobster0.media.attachments import attach_images_to_request

        request = self._request("system", "user", "assistant", "user")

        updated = attach_images_to_request(request, (self.part,))

        self.assertEqual(updated.messages[-1].images, (self.part,))
        self.assertEqual(updated.messages[1].images, ())
        self.assertEqual(updated.messages[0].images, ())

    def test_no_images_returns_the_request_unchanged(self) -> None:
        """没有图片时必须原样返回，不产生任何多余拷贝语义。"""
        from lobster0.media.attachments import attach_images_to_request

        request = self._request("system", "user")

        self.assertIs(attach_images_to_request(request, ()), request)

    def test_request_without_a_user_message_is_left_alone(self) -> None:
        """没有用户消息时不能硬塞，避免把图挂到 system 上。"""
        from lobster0.media.attachments import attach_images_to_request

        request = self._request("system")

        updated = attach_images_to_request(request, (self.part,))

        self.assertTrue(all(message.images == () for message in updated.messages))

    def test_other_message_fields_survive(self) -> None:
        """挂图不能丢掉正文、角色或其他字段。"""
        from lobster0.media.attachments import attach_images_to_request

        request = self._request("system", "user")

        updated = attach_images_to_request(request, (self.part,))

        self.assertEqual(updated.messages[-1].content, "user-text")
        self.assertEqual(updated.messages[-1].role, "user")
        self.assertEqual(updated.model, "deepseek-v4-pro")


class HandleForwardsImagePathsTest(unittest.IsolatedAsyncioTestCase):
    """``TurnService.handle`` 必须把本地图片路径交给 ``handle_inbound``。

    真实缺陷：``handle`` 声明了 ``image_paths`` 参数却没有往下传，于是 CLI 与桌面端
    直接给本地图片这条路静默失效——没有报错，只是模型看不见图。症状与飞书那次
    完全一样，而且同样不会有任何日志。
    """

    async def test_image_paths_reach_handle_inbound(self) -> None:
        """转发必须真的发生；只声明参数不算实现。"""
        from unittest.mock import AsyncMock, patch

        from lobster0.agent.turn import TurnService

        paths = ((Path("/tmp/a.png"), "image/png"),)
        service = TurnService.__new__(TurnService)
        with patch.object(
            TurnService, "handle_inbound", new=AsyncMock(return_value=None)
        ) as inbound:
            await TurnService.handle(
                service,
                user_id=1,
                text="看看这张图",
                conversation_id="c1",
                image_paths=paths,
            )

        self.assertEqual(inbound.await_args.kwargs["image_paths"], paths)
