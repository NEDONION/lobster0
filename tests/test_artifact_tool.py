"""read_artifact Tool 的越权、类型与截断边界。"""

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.artifacts.store import ArtifactStore
from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.storage.conversations import SessionRepository
from lobster0.storage.database import Database
from lobster0.tools.artifacts import ReadArtifactTool
from lobster0.tools.base import ToolContext, ToolValidationError


def png(width: int, height: int) -> bytes:
    """构造一张最小合法 PNG。"""
    import struct
    import zlib

    raw = b"".join(b"\x00" + b"\xff\xff\xff" * width for _ in range(height))
    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body))
            + kind
            + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class ReadArtifactToolTest(unittest.IsolatedAsyncioTestCase):
    """验证模型只能读到属于本会话、且类型允许的 Artifact 正文。"""

    def setUp(self) -> None:
        """准备状态、Store、会话与 Tool。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = build_state_paths(Path(self.temporary.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        self.now = datetime(2026, 8, 11, 12, tzinfo=UTC)
        self.store = ArtifactStore(
            self.database,
            owner_id=self.owner.id,
            root=self.paths.artifacts,
            staging_root=self.paths.downloads,
            max_bytes=4096,
            ttl_seconds=60,
            clock=lambda: self.now,
        )
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id, "s-1"
        )
        self.other = SessionRepository(self.database).get_or_create_cli(
            self.owner.id, "s-2"
        )
        self.tool = ReadArtifactTool(self.store)

    def put(self, name: str, body: bytes, media_type: str = "text/plain") -> str:
        """放一个 Artifact 并关联到默认会话，返回 id。"""
        staged = self.paths.downloads / name
        staged.write_bytes(body)
        staged.chmod(0o600)
        artifact = self.store.put(
            staged, declared_media_type=media_type, source="user_upload"
        )
        self.store.link(
            artifact.artifact_id,
            session_id=self.session.id,
            origin="user_upload",
            filename=name,
        )
        return artifact.artifact_id

    def context(self) -> ToolContext:
        """返回一个最小可信 ToolContext。"""
        # session_id 来自运行期 Context，模型参数伪造不了。
        return ToolContext(
            self.owner.id, self.session.id, 1, self.paths.home, self.paths.workspace, ()
        )

    async def test_reads_bounded_utf8_text(self) -> None:
        """文本类返回正文，并声明是否被截断。"""
        artifact_id = self.put("note.txt", "你好 Lobster0".encode())

        result = await self.tool.execute(
            self.context(), self.tool.validate({"artifact_id": artifact_id})
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data["text"], "你好 Lobster0")
        self.assertFalse(result.data["truncated"])

    async def test_truncation_is_declared_not_silent(self) -> None:
        """静默截断会让模型把半截内容当成文件的全部。"""
        artifact_id = self.put("big.txt", b"x" * 2000)

        result = await self.tool.execute(
            self.context(),
            self.tool.validate({"artifact_id": artifact_id, "max_bytes": 100}),
        )

        self.assertTrue(result.data["truncated"])
        self.assertEqual(len(result.data["text"]), 100)

    async def test_binary_types_return_metadata_without_body(self) -> None:
        """图片不进上下文：D3 不做 Vision，也不塞 base64。"""
        artifact_id = self.put("shot.png", png(3, 2), media_type="image/png")

        result = await self.tool.execute(
            self.context(), self.tool.validate({"artifact_id": artifact_id})
        )

        self.assertTrue(result.ok)
        self.assertNotIn("text", result.data)
        self.assertEqual(result.data["media_type"], "image/png")

    async def test_artifact_from_another_session_is_refused(self) -> None:
        """只能读当前会话的产物，跨会话即拒绝。"""
        staged = self.paths.downloads / "other.txt"
        staged.write_bytes(b"secret")
        staged.chmod(0o600)
        artifact = self.store.put(
            staged, declared_media_type="text/plain", source="user_upload"
        )
        self.store.link(
            artifact.artifact_id, session_id=self.other.id, origin="user_upload"
        )

        result = await self.tool.execute(
            self.context(), self.tool.validate({"artifact_id": artifact.artifact_id})
        )

        self.assertFalse(result.ok)
        self.assertNotIn("secret", str(result.error_message))

    async def test_forged_id_is_refused_by_validation(self) -> None:
        """形状不对的 id 在校验阶段就拒绝，不进入 Store 查询。"""
        for value in ("../etc/passwd", "art_short", "", 1, None):
            with self.assertRaises(ToolValidationError):
                self.tool.validate({"artifact_id": value})

    async def test_expired_artifact_is_refused(self) -> None:
        """过期回收后不能再读到正文。"""
        artifact_id = self.put("note.txt", b"hello")
        self.now = self.now + timedelta(seconds=120)
        self.store.delete_expired()

        result = await self.tool.execute(
            self.context(), self.tool.validate({"artifact_id": artifact_id})
        )

        self.assertFalse(result.ok)

    async def test_invalid_utf8_never_enters_the_store_as_text(self) -> None:
        """非法 UTF-8 在入库时就被 magic byte 检查拦掉。

        所以模型不可能读到「声明为文本、实际是二进制」的内容。Tool 里的解码
        保护是第二道，正常路径到不了那里。
        """
        from lobster0.artifacts.store import ArtifactError

        with self.assertRaises(ArtifactError) as raised:
            self.put("bad.txt", b"\xff\xfe\x00bad")

        self.assertEqual(raised.exception.code, "artifact_media_mismatch")


if __name__ == "__main__":
    unittest.main()
