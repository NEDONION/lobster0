"""Owner-scoped FTS5/CJK Memory Recall 的安全回归测试。"""

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.bootstrap import initialize_state
from miniclaw.memory.models import DisclosureContext, SourceRef
from miniclaw.memory.repository import MemoryUnitRepository
from miniclaw.memory.retrieval import MemoryRetrieval, SearchRequest
from miniclaw.paths import build_state_paths
from miniclaw.storage.conversations import SessionRepository, TurnRepository
from miniclaw.storage.database import Database


class MemoryRetrievalTest(unittest.TestCase):
    """验证中文召回、证据链与 Disclosure fail-closed。"""

    def setUp(self) -> None:
        """创建一个真实 Owner、来源消息和两条可检索 Unit。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        self.owner = initialize_state(paths).owner
        self.database = Database(paths.database)
        session = SessionRepository(self.database).get_or_create_cli(self.owner.id, "recall")
        turn = TurnRepository(self.database).create_with_user_message(
            session.id,
            "recall-source",
            "test-model",
            "我默认希望你使用中文回复",
        )
        with self.database.connect_read_only() as connection:
            message_id = int(
                connection.execute(
                    "SELECT id FROM messages WHERE turn_id = ?",
                    (turn.id,),
                ).fetchone()[0]
            )
        source = SourceRef(message_id, session.id, "cli")
        now = datetime(2026, 8, 9, tzinfo=UTC)
        units = MemoryUnitRepository(self.database)
        units.create(
            unit_id="mem-language",
            owner_id=self.owner.id,
            key="preference.language",
            text="用户偏好使用中文回复",
            kind="preference",
            scope="private",
            status="active",
            confidence=1.0,
            sensitivity="low",
            valid_from=now,
            valid_until=None,
            sources=(source,),
            now=now,
        )
        units.create(
            unit_id="mem-style",
            owner_id=self.owner.id,
            key="preference.style",
            text="用户喜欢简洁的回答",
            kind="preference",
            scope="private",
            status="short_term",
            confidence=0.8,
            sensitivity="low",
            valid_from=now,
            valid_until=None,
            sources=(source,),
            now=now,
        )
        self.retrieval = MemoryRetrieval(self.database)
        self.local = DisclosureContext(self.owner.id, self.owner.id, "cli", "local", True)

    def test_chinese_recall_returns_complete_unit_and_sources(self) -> None:
        """中文 bigram 查询应命中完整事实并保留可核验 message source。"""
        result = self.retrieval.search(
            SearchRequest(self.local, "默认回复语言 中文", 5),
            now=datetime(2026, 8, 9, tzinfo=UTC),
        )

        self.assertEqual(result.reason_code, "verified_owner_local")
        self.assertEqual(result.items[0].unit.id, "mem-language")
        self.assertEqual(result.items[0].unit.text, "用户偏好使用中文回复")
        self.assertTrue(result.items[0].unit.sources)

    def test_group_and_other_owner_return_no_private_hits(self) -> None:
        """群聊与非 Owner direct 查询不能观察私人 Unit 是否存在。"""
        group = DisclosureContext(self.owner.id, self.owner.id, "discord", "group", True)
        other = DisclosureContext(self.owner.id, self.owner.id + 1, "discord", "direct", True)

        self.assertEqual(
            self.retrieval.search(SearchRequest(group, "中文", 5)).items,
            (),
        )
        self.assertEqual(
            self.retrieval.search(SearchRequest(other, "中文", 5)).items,
            (),
        )

    def test_list_and_get_are_owner_scoped_and_filter_review_units(self) -> None:
        """列表和详情沿用同一披露边界，且只返回可召回状态。"""
        current = datetime(2026, 8, 9, tzinfo=UTC)
        listed = self.retrieval.list(self.local, limit=10, now=current)
        found = self.retrieval.get(self.local, "mem-language", now=current)

        self.assertEqual([item.id for item in listed], ["mem-language", "mem-style"])
        self.assertIsNotNone(found)
        self.assertIsNone(
            self.retrieval.get(
                DisclosureContext(
                    self.owner.id,
                    self.owner.id,
                    "feishu",
                    "group",
                    True,
                ),
                "mem-language",
            )
        )


if __name__ == "__main__":
    unittest.main()
