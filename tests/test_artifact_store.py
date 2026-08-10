"""Browser Artifact 私有存储、MIME、配额与 TTL 回归。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.artifacts.store import ArtifactError, ArtifactStore
from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.storage.database import Database
from lobster0.storage.migrations import LATEST_SCHEMA_VERSION, current_schema_version


class ArtifactStoreTest(unittest.TestCase):
    """验证 Worker staging 文件只能变成有界、可过期的私有 Artifact。"""

    def setUp(self) -> None:
        """创建完成迁移的临时状态、固定时钟和 ArtifactStore。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = build_state_paths(Path(self.temporary.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.now = datetime(2026, 8, 9, 12, tzinfo=UTC)
        self.store = ArtifactStore(
            self.database,
            owner_id=self.owner.id,
            root=self.paths.artifacts,
            staging_root=self.paths.downloads,
            max_bytes=1024,
            ttl_seconds=60,
            clock=lambda: self.now,
        )

    def stage(self, name: str, content: bytes) -> Path:
        """在 Worker staging root 写入一个 owner-only 测试文件。"""
        path = self.paths.downloads / name
        path.write_bytes(content)
        path.chmod(0o600)
        return path

    def test_png_is_content_addressed_private_and_tool_payload_has_no_path(self) -> None:
        """同内容去重；模型只看到 ID/hash/大小，不看到路径或 base64。"""
        first = self.store.put(
            self.stage("first.png", png(3, 2)),
            declared_media_type="image/png",
            source="browser_screenshot",
        )
        second = self.store.put(
            self.stage("second.png", png(3, 2)),
            declared_media_type="image/png",
            source="browser_screenshot",
        )

        self.assertEqual(first.artifact_id, second.artifact_id)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertTrue(first.path.is_file())
        self.assertEqual(first.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.paths.downloads / "first.png").exists())
        payload = first.to_tool_payload()
        self.assertEqual(payload["artifact_id"], first.artifact_id)
        self.assertEqual(payload["width"], 3)
        self.assertEqual(payload["height"], 2)
        self.assertNotIn("path", payload)
        self.assertNotIn("base64", str(payload).casefold())
        self.assertEqual(current_schema_version(self.database), LATEST_SCHEMA_VERSION)

    def test_mime_mismatch_symlink_escape_and_oversize_are_rejected(self) -> None:
        """声明类型、symlink、staging escape 与字节上限都必须 fail closed。"""
        outside = Path(self.temporary.name) / "outside.png"
        outside.write_bytes(png(1, 1))
        link = self.paths.downloads / "linked.png"
        link.symlink_to(outside)
        cases = (
            (link, "image/png", "artifact_source_denied"),
            (outside, "image/png", "artifact_source_denied"),
            (
                self.stage("wrong.png", b"plain text"),
                "image/png",
                "artifact_media_mismatch",
            ),
            (
                self.stage("large.txt", b"x" * 1025),
                "text/plain",
                "artifact_too_large",
            ),
        )
        for path, media_type, code in cases:
            with self.subTest(code=code), self.assertRaises(ArtifactError) as raised:
                self.store.put(
                    path,
                    declared_media_type=media_type,
                    source="browser_download",
                )
            self.assertEqual(raised.exception.code, code)
        self.assertEqual(outside.read_bytes(), png(1, 1))

    def test_expired_artifact_is_deleted_without_touching_fresh_one(self) -> None:
        """TTL cleanup 只删除到期内容和 metadata，未到期文件保持可读。"""
        expired = self.store.put(
            self.stage("expired.txt", b"old artifact"),
            declared_media_type="text/plain",
            source="browser_download",
        )
        self.now += timedelta(seconds=30)
        fresh = self.store.put(
            self.stage("fresh.txt", b"fresh artifact"),
            declared_media_type="text/plain",
            source="browser_download",
        )
        self.now += timedelta(seconds=31)

        deleted = self.store.delete_expired()

        self.assertEqual(deleted, 1)
        self.assertFalse(expired.path.exists())
        self.assertTrue(fresh.path.exists())
        with self.assertRaises(ArtifactError) as raised:
            self.store.read_metadata(expired.artifact_id)
        self.assertEqual(raised.exception.code, "artifact_not_found")
        self.assertEqual(self.store.read_metadata(fresh.artifact_id), fresh)


def png(width: int, height: int) -> bytes:
    """返回含合法 PNG signature/IHDR 尺寸的最小测试字节。"""
    return (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x06\x00\x00\x00"
        + b"\x00\x00\x00\x00"
    )


if __name__ == "__main__":
    unittest.main()
