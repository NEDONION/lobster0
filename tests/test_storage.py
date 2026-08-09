"""MiniClaw SQLite migration 与 Owner Repository 的行为测试。"""

import sqlite3
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from miniclaw.storage.database import Database, DatabaseError
from miniclaw.storage.migrations import MigrationError, apply_migrations, current_schema_version
from miniclaw.storage.repositories import OwnerRepository

EXPECTED_TABLES = {
    "approvals",
    "automation_control",
    "audit_events",
    "channel_identities",
    "checkpoints",
    "deliveries",
    "execution_plans",
    "eval_runs",
    "feedback",
    "messages",
    "memory_audit",
    "memory_buffers",
    "memory_candidates",
    "memory_conflicts",
    "memory_flush_runs",
    "memory_legacy_imports",
    "memory_manifests",
    "memory_reviews",
    "memory_sources",
    "memory_units",
    "policy_rules",
    "processed_events",
    "proposals",
    "schema_migrations",
    "sessions",
    "scheduled_tasks",
    "task_runs",
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

        self.assertEqual(first, (1, 2, 3, 4, 5))
        self.assertEqual(second, ())
        self.assertEqual(tables, EXPECTED_TABLES)
        self.assertEqual(current_schema_version(database), 5)
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

    def test_phase6_v5_schema_has_durable_automation_and_delivery_bindings(self) -> None:
        """v5 必须包含 Task Ledger、E-stop、Checkpoint、Plan 与主动投递关联。"""
        database = Database(self.database_path)

        self.assertEqual(apply_migrations(database), (1, 2, 3, 4, 5))

        with database.connect_read_only() as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            task_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(scheduled_tasks)")
            }
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(task_runs)")
            }
            delivery_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
            }
            approval_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(approvals)")
            }
            checkpoint_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(checkpoints)")
            }

        self.assertTrue(
            {"scheduled_tasks", "task_runs", "automation_control", "checkpoints",
             "execution_plans"}.issubset(tables)
        )
        self.assertIn("system_key", task_columns)
        self.assertTrue({"approval_id", "response_json"}.issubset(run_columns))
        self.assertIn("task_run_id", delivery_columns)
        self.assertIn("execution_plan_hash", approval_columns)
        self.assertIn("tool_run_id", checkpoint_columns)
        self.assertEqual(current_schema_version(database), 5)

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

    def test_read_only_connection_does_not_create_missing_database(self) -> None:
        """只读诊断连接在数据库缺失时必须失败，不能创建空文件。"""
        database = Database(self.database_path)

        with self.assertRaises(DatabaseError):
            with database.connect_read_only():
                self.fail("missing database unexpectedly opened")

        self.assertFalse(self.database_path.exists())

    def test_v1_database_upgrades_without_losing_channel_rows(self) -> None:
        """已有 v1 消息、事件和 Delivery 必须在 v2 迁移后完整保留。"""
        database = Database(self.database_path)
        schema = (
            resources.files("miniclaw.storage")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        now = "2026-08-08T00:00:00+00:00"
        with database.connect() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
                (now,),
            )
            connection.execute(
                "INSERT INTO users (id, display_name, created_at) VALUES (1, 'Owner', ?)",
                (now,),
            )
            connection.execute(
                """
                INSERT INTO sessions (
                    id, user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (1, 1, 'feishu', 'default', 'oc_legacy', 'active', ?, ?)
                """,
                (now, now),
            )
            connection.execute(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES (1, 1, 'assistant', 'legacy reply', ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO processed_events (
                    channel, account_id, event_id, external_message_id,
                    session_id, received_at
                ) VALUES ('feishu', 'default', 'evt_legacy', 'om_legacy', 1, ?)
                """,
                (now,),
            )
            connection.execute(
                """
                INSERT INTO deliveries (
                    id, message_id, channel, account_id, external_conversation_id,
                    part_index, content_hash, status, attempts, created_at
                ) VALUES (
                    1, 1, 'feishu', 'default', 'oc_legacy', 0,
                    'legacy-hash', 'queued', 0, ?
                )
                """,
                (now,),
            )

        self.assertEqual(apply_migrations(database), (2, 3, 4, 5))

        with database.connect() as connection:
            event = connection.execute(
                "SELECT * FROM processed_events WHERE external_message_id = 'om_legacy'"
            ).fetchone()
            delivery = connection.execute("SELECT * FROM deliveries WHERE id = 1").fetchone()
            event_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(processed_events)")
            }
            delivery_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
            }

        self.assertEqual(event["content"], "")
        self.assertEqual(event["status"], "queued")
        self.assertEqual(event["updated_at"], now)
        self.assertEqual(delivery["delivery_kind"], "message")
        self.assertEqual(delivery["status"], "queued")
        self.assertEqual(delivery["updated_at"], now)
        self.assertTrue(delivery["idempotency_key"].startswith("legacy-"))
        self.assertTrue(
            {
                "external_user_id",
                "external_conversation_id",
                "chat_type",
                "message_type",
                "content",
                "reply_to_message_id",
                "status",
                "attempts",
                "last_error_code",
                "updated_at",
            }.issubset(event_columns)
        )
        self.assertTrue(
            {
                "reply_to_message_id",
                "delivery_kind",
                "content",
                "idempotency_key",
                "updated_at",
                "next_attempt_at",
                "last_error_detail",
            }.issubset(delivery_columns)
        )

    def test_failed_v2_migration_rolls_back_all_statements(self) -> None:
        """一个版本中后续 SQL 失败时，前面的表修改和版本账本都必须回滚。"""
        database = Database(self.database_path)
        schema = (
            resources.files("miniclaw.storage")
            .joinpath("schema.sql")
            .read_text(encoding="utf-8")
        )
        with database.connect() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (1, 'now')"
            )
        with patch(
            "miniclaw.storage.migrations._load_migration",
            return_value="CREATE TABLE partial_v2 (id INTEGER); INVALID SQL;",
        ):
            with self.assertRaisesRegex(MigrationError, "migration 2"):
                apply_migrations(database)

        with database.connect() as connection:
            partial = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_v2'"
            ).fetchone()
        self.assertIsNone(partial)
        self.assertEqual(current_schema_version(database), 1)

    def test_older_binary_rejects_newer_schema_version(self) -> None:
        """程序不能在更高版本 Schema 上继续写入，避免不可逆数据损坏。"""
        database = Database(self.database_path)
        with database.connect() as connection:
            connection.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (6, "future"),
            )

        with self.assertRaisesRegex(MigrationError, "newer schema version 6"):
            apply_migrations(database)


if __name__ == "__main__":
    unittest.main()
