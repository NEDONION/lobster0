"""MiniClaw SQLite migration 与 Owner Repository 的行为测试。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations, current_schema_version
from miniclaw.storage.repositories import OwnerRepository

EXPECTED_TABLES = {
    "approvals",
    "audit_events",
    "channel_identities",
    "deliveries",
    "eval_runs",
    "feedback",
    "messages",
    "policy_rules",
    "processed_events",
    "proposals",
    "schema_migrations",
    "sessions",
    "tool_runs",
    "turns",
    "users",
}


class StorageTest(unittest.TestCase):
    """验证数据库初始化、约束和单 Owner 幂等语义。"""

    def setUp(self) -> None:
        """为每个测试创建独立的磁盘 SQLite 数据库路径。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "miniclaw.db"

    def test_migration_creates_complete_schema_once_with_required_pragmas(self) -> None:
        """第一次迁移应创建完整 Schema，第二次不得重复应用。"""
        database = Database(self.database_path)

        first = apply_migrations(database)
        second = apply_migrations(database)

        with database.connect() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(first, (1,))
        self.assertEqual(second, ())
        self.assertEqual(tables, EXPECTED_TABLES)
        self.assertEqual(current_schema_version(database), 1)
        self.assertEqual(foreign_keys, 1)
        self.assertEqual(busy_timeout, 5000)
        self.assertEqual(journal_mode, "wal")

    def test_foreign_key_constraints_are_enforced(self) -> None:
        """每个连接都必须启用外键，避免产生无法关联的 Session。"""
        database = Database(self.database_path)
        apply_migrations(database)

        with self.assertRaises(sqlite3.IntegrityError):
            with database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO sessions (
                        user_id, channel, account_id, external_conversation_id,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (999, "cli", "local", "missing-owner", "active", "now", "now"),
                )

    def test_owner_is_created_once_and_preserved(self) -> None:
        """重复初始化不能插入第二个 Owner 或覆盖已有显示名。"""
        database = Database(self.database_path)
        apply_migrations(database)
        repository = OwnerRepository(database)

        first = repository.get_or_create("Owner")
        second = repository.get_or_create("Replacement")

        with database.connect() as connection:
            owner_count = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.display_name, "Owner")
        self.assertIsNotNone(second.created_at.tzinfo)
        self.assertEqual(owner_count, 1)


if __name__ == "__main__":
    unittest.main()
