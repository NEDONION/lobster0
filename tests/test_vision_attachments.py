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
