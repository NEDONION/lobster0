"""Browser Artifact 私有存储、MIME、配额与 TTL 回归。"""

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

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

    def external(self, name: str, content: bytes, mode: int = 0o644) -> Path:
        """在 staging 之外写一个普通用户文件，默认 0644——这正是现实中的权限。"""
        directory = Path(self.temporary.name) / "outside"
        directory.mkdir(exist_ok=True)
        path = directory / name
        path.write_bytes(content)
        path.chmod(mode)
        return path

    def test_external_file_with_group_readable_mode_is_accepted(self) -> None:
        """用户从「文稿」选的文件通常是 0644，必须能通过。

        现有的 _read_staging 要求 owner-only，对 Worker 自建文件成立，对用户
        选的文件不成立——这条正是外部读取需要独立实现的原因。
        """
        source = self.external("note.txt", b"hello")

        staged = self.store.stage_from_external_path(source, max_bytes=1024)

        self.assertEqual(staged.parent, self.paths.downloads)
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual(staged.read_bytes(), b"hello")
        # 源文件不能被移动或删除，它是用户自己的文件。
        self.assertTrue(source.is_file())

    def test_external_staging_feeds_put_as_a_user_upload(self) -> None:
        """stage → put 全链路，source 为 user_upload。"""
        source = self.external("shot.png", png(3, 2))

        staged = self.store.stage_from_external_path(source, max_bytes=1024)
        artifact = self.store.put(
            staged, declared_media_type="image/png", source="user_upload"
        )

        self.assertEqual(artifact.media_type, "image/png")
        self.assertTrue(artifact.path.is_file())
        self.assertFalse(staged.exists())

    def test_external_symlink_is_refused(self) -> None:
        """symlink 是最直接的越权读取手段，必须在 open 阶段就拒绝。"""
        target = self.external("secret.txt", b"secret")
        link = Path(self.temporary.name) / "outside" / "link.txt"
        link.symlink_to(target)

        with self.assertRaises(ArtifactError) as raised:
            self.store.stage_from_external_path(link, max_bytes=1024)

        self.assertEqual(raised.exception.code, "artifact_source_denied")

    def test_external_directory_is_refused(self) -> None:
        """目录不是普通文件。"""
        directory = Path(self.temporary.name) / "outside"
        directory.mkdir(exist_ok=True)

        with self.assertRaises(ArtifactError) as raised:
            self.store.stage_from_external_path(directory, max_bytes=1024)

        self.assertEqual(raised.exception.code, "artifact_source_denied")

    def test_external_file_over_the_attachment_limit_is_refused(self) -> None:
        """附件上限比 Store 上限更小，超过即拒绝。"""
        source = self.external("big.txt", b"x" * 200)

        with self.assertRaises(ArtifactError) as raised:
            self.store.stage_from_external_path(source, max_bytes=100)

        self.assertEqual(raised.exception.code, "artifact_too_large")

    def test_failed_external_staging_leaves_no_partial_file(self) -> None:
        """拒绝路径不能在 staging 里留下垃圾，否则会被下一次 put 误当作输入。"""
        source = self.external("big.txt", b"x" * 200)
        before = set(self.paths.downloads.iterdir())

        with self.assertRaises(ArtifactError):
            self.store.stage_from_external_path(source, max_bytes=100)

        self.assertEqual(set(self.paths.downloads.iterdir()), before)

    def test_external_staging_refuses_a_source_rewritten_mid_read(self) -> None:
        """读取期间源被原地改写时必须拒绝，而不是落一个内容不确定的文件。

        注意这里是**原地改写**而不是 os.replace：替换换不掉已经打开的 fd，
        re-fstat 真正能发现的是同一 inode 上的改动。
        """
        source = self.external("rewrite.txt", b"y" * 300)
        original_read = os.read
        rewritten = False

        def read_then_rewrite(descriptor: int, size: int) -> bytes:
            nonlocal rewritten
            chunk = original_read(descriptor, size)
            if chunk and not rewritten:
                rewritten = True
                with open(source, "r+b") as stream:
                    stream.write(b"CHANGED")
                os.utime(source, (0, 0))
            return chunk

        with patch("lobster0.artifacts.store.os.read", side_effect=read_then_rewrite):
            with self.assertRaises(ArtifactError) as raised:
                self.store.stage_from_external_path(source, max_bytes=1024)

        self.assertEqual(raised.exception.code, "artifact_source_changed")

    def session(self, external_id: str) -> int:
        """创建一个可供关联的会话，返回内部 id。"""
        from lobster0.storage.conversations import SessionRepository

        return SessionRepository(self.database).get_or_create_cli(self.owner.id, external_id).id

    def test_one_artifact_can_belong_to_two_sessions(self) -> None:
        """Artifact 跨会话去重，所以归属必须是多对多而不是一列。"""
        artifact = self.store.put(
            self.stage("shot.png", png(3, 2)),
            declared_media_type="image/png",
            source="browser_screenshot",
        )
        first = self.session("s-1")
        second = self.session("s-2")

        self.store.link(artifact.artifact_id, session_id=first, origin="agent_output")
        self.store.link(artifact.artifact_id, session_id=second, origin="user_upload")

        self.assertEqual(
            [item.artifact_id for item in self.store.list_for_session(first, limit=10)],
            [artifact.artifact_id],
        )
        self.assertEqual(
            [item.origin for item in self.store.list_for_session(second, limit=10)],
            ["user_upload"],
        )

    def test_listing_is_scoped_to_one_session(self) -> None:
        """右栏只展示当前会话的产物，跨会话不能串。"""
        first = self.session("s-1")
        second = self.session("s-2")
        one = self.store.put(
            self.stage("a.txt", b"alpha"),
            declared_media_type="text/plain",
            source="user_upload",
        )
        two = self.store.put(
            self.stage("b.txt", b"beta"),
            declared_media_type="text/plain",
            source="user_upload",
        )
        self.store.link(one.artifact_id, session_id=first, origin="user_upload")
        self.store.link(two.artifact_id, session_id=second, origin="user_upload")

        self.assertEqual(
            [item.artifact_id for item in self.store.list_for_session(first, limit=10)],
            [one.artifact_id],
        )

    def test_linking_twice_in_one_session_is_idempotent(self) -> None:
        """同一条消息重复关联不该产生两行，否则右栏会重复显示。"""
        artifact = self.store.put(
            self.stage("a.txt", b"alpha"),
            declared_media_type="text/plain",
            source="user_upload",
        )
        session = self.session("s-1")

        self.store.link(artifact.artifact_id, session_id=session, origin="user_upload")
        self.store.link(artifact.artifact_id, session_id=session, origin="user_upload")

        self.assertEqual(len(self.store.list_for_session(session, limit=10)), 1)

    def test_listing_refuses_an_unbounded_limit(self) -> None:
        """列表必须有界，避免一次把整个会话的产物读进内存。"""
        session = self.session("s-1")
        for limit in (0, -1, 501):
            with self.assertRaises(ArtifactError):
                self.store.list_for_session(session, limit=limit)

    def test_linking_an_unknown_artifact_is_refused(self) -> None:
        """伪造的 artifact id 不能凭空建立关联。"""
        session = self.session("s-1")

        with self.assertRaises(ArtifactError) as raised:
            self.store.link("art_" + "f" * 64, session_id=session, origin="user_upload")

        self.assertEqual(raised.exception.code, "artifact_not_found")

    def test_deleted_artifacts_disappear_from_the_listing(self) -> None:
        """过期回收后右栏不该继续显示已经不存在的产物。"""
        session = self.session("s-1")
        artifact = self.store.put(
            self.stage("a.txt", b"alpha"),
            declared_media_type="text/plain",
            source="user_upload",
        )
        self.store.link(artifact.artifact_id, session_id=session, origin="user_upload")
        self.now = self.now + timedelta(seconds=120)
        self.store.delete_expired()

        self.assertEqual(self.store.list_for_session(session, limit=10), [])

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
