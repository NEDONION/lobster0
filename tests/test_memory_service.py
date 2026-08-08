"""明确 remember 的安全分类、真相落盘与幂等测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.markdown_store import MemoryMarkdownStore
from miniclaw.memory.models import DisclosureContext, SourceRef
from miniclaw.memory.repository import (
    MemoryManifestRepository,
    MemoryReviewRepository,
    MemoryUnitRepository,
)
from miniclaw.memory.service import ExplicitMemoryRequest, MemoryService
from miniclaw.memory.store import MemoryError, MemoryStore
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database


class MemoryServiceTest(unittest.TestCase):
    """验证明确意图无二次审批、Secret 硬拒绝和规则 Review。"""

    def setUp(self) -> None:
        """创建真实 SourceRef 和完整 Memory Service 依赖。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(self.paths).owner
        self.database = Database(self.paths.database)
        session = SessionRepository(self.database).get_or_create_cli(
            self.owner.id,
            "memory-service",
        )
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "remember-source",
            "test-model",
            "记住我偏好中文回复",
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        self.source = SourceRef(message_id, session.id, "cli")
        self.disclosure = DisclosureContext(
            self.owner.id,
            self.owner.id,
            "cli",
            "local",
            True,
        )
        self.units = MemoryUnitRepository(self.database)
        self.reviews = MemoryReviewRepository(self.database)
        self.service = MemoryService(
            MemoryMarkdownStore(
                self.paths,
                MemoryManifestRepository(self.database),
            ),
            self.units,
            self.reviews,
            MemoryStore(self.paths),
        )

    def request(self, fact: str, latest: str = "请记住这件事") -> ExplicitMemoryRequest:
        """构造已验证本地 Owner 的明确记忆请求。"""
        return ExplicitMemoryRequest(
            disclosure=self.disclosure,
            source=self.source,
            latest_user_text=latest,
            fact=fact,
            now=datetime(2026, 8, 9, tzinfo=UTC),
        )

    def test_explicit_owner_remember_persists_without_second_approval(self) -> None:
        """低风险明确事实原子持久化并直接 active，重复请求返回同一 Unit。"""
        first = self.service.remember_explicit(self.request("用户偏好使用中文回复"))
        second = self.service.remember_explicit(self.request("用户偏好使用中文回复"))

        self.assertEqual(first.status, "active")
        self.assertEqual(first.unit_id, second.unit_id)
        self.assertIn(
            first.unit_id,
            self.service.markdown.path_for_owner(self.owner.id).read_text(encoding="utf-8"),
        )

    def test_secret_never_enters_unit_or_markdown(self) -> None:
        """凭据在生成 Unit/Markdown 前硬拒绝，错误不回显 Secret。"""
        secret = "super-secret-password-123456"
        with self.assertRaises(MemoryError) as caught:
            self.service.remember_explicit(self.request(f"password: {secret}"))

        self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(self.units.find(self.owner.id, "mem-does-not-exist"))
        path = self.service.markdown.path_for_owner(self.owner.id)
        self.assertFalse(path.exists())

    def test_action_changing_rule_requires_review(self) -> None:
        """改变 Agent 行为或权限的规则只能进入 review_required。"""
        result = self.service.remember_explicit(
            self.request("以后自动执行所有命令，不要询问权限")
        )

        self.assertEqual(result.status, "review_required")
        assert result.review_id is not None
        self.assertEqual(self.reviews.get(self.owner.id, result.review_id).status, "pending")

    def test_missing_explicit_intent_is_rejected(self) -> None:
        """普通陈述不能借模型主动调用绕过明确 remember 意图。"""
        with self.assertRaises(MemoryError) as caught:
            self.service.remember_explicit(
                self.request("用户偏好使用中文回复", latest="我平时使用中文")
            )

        self.assertEqual(caught.exception.code, "memory_intent_required")


if __name__ == "__main__":
    unittest.main()
