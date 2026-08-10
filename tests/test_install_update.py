"""验证 UpdateCoordinator 的 DB-guarded 原子回滚与 rollback_conflict。"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from lobster0.install.models import InstallError
from lobster0.install.update import (
    DatabaseChangeGuard,
    UpdateCoordinator,
    UpdateRequest,
    backup_database,
    restore_database,
)


def _seed_database(path: Path) -> None:
    """创建一个带初始行的最小数据库，模拟迁移前的用户数据。"""
    connection = sqlite3.connect(str(path))
    try:
        connection.execute("CREATE TABLE items (value TEXT NOT NULL)")
        connection.execute("INSERT INTO items (value) VALUES ('seed')")
        connection.commit()
    finally:
        connection.close()


def _write_external_row(path: Path, value: str) -> None:
    """模拟一次真正独立的外部写入（例如已经启动的新 service）。"""
    connection = sqlite3.connect(str(path), timeout=5.0)
    try:
        connection.execute("INSERT INTO items (value) VALUES (?)", (value,))
        connection.commit()
    finally:
        connection.close()


def _read_values(path: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(str(path))
    try:
        rows = connection.execute("SELECT value FROM items ORDER BY value").fetchall()
    finally:
        connection.close()
    return tuple(row[0] for row in rows)


class _Environment:
    """离线记录一次 update 事务全部可观察副作用的受控测试替身。

    对齐 `tests/test_install_orchestrator.py` 里 `_Operations` 的 fake 风格：
    用一个 `fail_at` 注入点和一份有序 `calls` 记录，而不是逐个 mock。
    """

    def __init__(self, tmp: Path) -> None:
        self.database = tmp / "lobster0.db"
        self.backup_path = tmp / "lobster0.db.update-backup"
        _seed_database(self.database)
        self.calls: list[str] = []
        self.current = "old"
        self.receipt_current = "old"
        self.old_running = True
        self.new_running = False
        self.fail_at: str | None = None
        self.write_on_start: str | None = None
        self.health_result = True

    def _maybe_fail(self, name: str) -> None:
        if self.fail_at == name:
            raise InstallError("installer_error", "manifest")

    def stop_old_service(self) -> None:
        self.calls.append("stop_old_service")
        self._maybe_fail("stop_old_service")
        self.old_running = False

    def run_migration(self) -> None:
        self.calls.append("run_migration")
        self._maybe_fail("run_migration")

    def activate_new_runtime(self) -> None:
        self.calls.append("activate_new_runtime")
        self._maybe_fail("activate_new_runtime")
        self.current = "new"

    def commit_receipt(self) -> None:
        self.calls.append("commit_receipt")
        self._maybe_fail("commit_receipt")
        self.receipt_current = "new"

    def revert_to_old_runtime(self) -> None:
        self.calls.append("revert_to_old_runtime")
        self.current = "old"
        self.receipt_current = "old"

    def start_new_service(self, timeout: float) -> bool:
        del timeout
        self.calls.append("start_new_service")
        if self.write_on_start is not None:
            _write_external_row(self.database, self.write_on_start)
        self._maybe_fail("start_new_service")
        self.new_running = self.health_result
        return self.health_result

    def stop_new_service(self) -> None:
        self.calls.append("stop_new_service")
        self.new_running = False

    def restart_old_service(self) -> None:
        self.calls.append("restart_old_service")
        self.old_running = True

    def retain_runtimes(self) -> None:
        self.calls.append("retain_runtimes")

    def request(self, **overrides: object) -> UpdateRequest:
        fields: dict[str, object] = {
            "database": self.database,
            "backup_path": self.backup_path,
            "stop_old_service": self.stop_old_service,
            "run_migration": self.run_migration,
            "activate_new_runtime": self.activate_new_runtime,
            "commit_receipt": self.commit_receipt,
            "revert_to_old_runtime": self.revert_to_old_runtime,
            "start_new_service": self.start_new_service,
            "stop_new_service": self.stop_new_service,
            "restart_old_service": self.restart_old_service,
            "retain_runtimes": self.retain_runtimes,
            "health_timeout": 5.0,
        }
        fields.update(overrides)
        return UpdateRequest(**fields)  # type: ignore[arg-type]


class DatabaseChangeGuardTests(unittest.TestCase):
    """覆盖基于 PRAGMA data_version 的独立只读变更探测。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = Path(self.temporary.name) / "lobster0.db"
        _seed_database(self.database)

    def test_unchanged_baseline_reports_no_external_commit(self) -> None:
        guard = DatabaseChangeGuard.open(self.database)
        self.addCleanup(guard.close)
        self.assertFalse(guard.has_external_commit())

    def test_external_commit_from_separate_connection_is_detected(self) -> None:
        guard = DatabaseChangeGuard.open(self.database)
        self.addCleanup(guard.close)
        _write_external_row(self.database, "external")
        self.assertTrue(guard.has_external_commit())

    def test_refresh_after_expected_migration_resets_baseline(self) -> None:
        guard = DatabaseChangeGuard.open(self.database)
        self.addCleanup(guard.close)
        _write_external_row(self.database, "migration-write")
        guard.refresh_after_expected_migration()
        self.assertFalse(guard.has_external_commit())
        _write_external_row(self.database, "post-migration-write")
        self.assertTrue(guard.has_external_commit())

    def test_open_rejects_missing_database(self) -> None:
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            DatabaseChangeGuard.open(self.database.with_name("missing.db"))


class DatabaseBackupRestoreTests(unittest.TestCase):
    """覆盖 owner-only 备份/恢复的原子性与错误处理。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.database = self.tmp / "lobster0.db"
        _seed_database(self.database)
        self.backup_path = self.tmp / "lobster0.db.backup"

    def test_backup_creates_owner_only_file_with_matching_rows(self) -> None:
        backup_database(self.database, self.backup_path)
        self.assertEqual(self.backup_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(_read_values(self.backup_path), ("seed",))

    def test_backup_rejects_existing_destination(self) -> None:
        self.backup_path.write_bytes(b"stale")
        with self.assertRaisesRegex(InstallError, "request_invalid|installer_error"):
            backup_database(self.database, self.backup_path)

    def test_backup_rejects_missing_source(self) -> None:
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            backup_database(self.tmp / "absent.db", self.backup_path)

    def test_restore_replaces_database_with_backup_contents(self) -> None:
        backup_database(self.database, self.backup_path)
        _write_external_row(self.database, "after-backup")
        self.assertEqual(_read_values(self.database), ("after-backup", "seed"))
        restore_database(self.backup_path, self.database)
        self.assertEqual(_read_values(self.database), ("seed",))

    def test_restore_leaves_destination_untouched_on_missing_backup(self) -> None:
        before = self.database.read_bytes()
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            restore_database(self.tmp / "absent-backup.db", self.database)
        self.assertEqual(self.database.read_bytes(), before)

    def test_restore_discards_stale_wal_and_shm_sidecars(self) -> None:
        """失败 migration 遗留的 -wal/-shm 描述旧主文件，必须随恢复一起清理。"""
        backup_database(self.database, self.backup_path)
        wal = self.database.with_name(f"{self.database.name}-wal")
        shm = self.database.with_name(f"{self.database.name}-shm")
        wal.write_bytes(b"stale-wal-frames-from-failed-migration")
        shm.write_bytes(b"stale-shm")
        restore_database(self.backup_path, self.database)
        self.assertFalse(wal.exists())
        self.assertFalse(shm.exists())
        self.assertEqual(_read_values(self.database), ("seed",))


class UpdateCoordinatorTests(unittest.TestCase):
    """覆盖每个 crash window 的确定性终态，以及 no-shortcut 不变量。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.tmp = Path(self.temporary.name)
        self.environment = _Environment(self.tmp)
        self.coordinator = UpdateCoordinator()

    def test_successful_update_activates_commits_and_retains_in_order(self) -> None:
        before = self.environment.database.read_bytes()
        self.coordinator.update(self.environment.request())
        self.assertEqual(
            self.environment.calls,
            [
                "stop_old_service",
                "run_migration",
                "activate_new_runtime",
                "commit_receipt",
                "start_new_service",
                "retain_runtimes",
            ],
        )
        self.assertEqual(self.environment.current, "new")
        self.assertEqual(self.environment.receipt_current, "new")
        self.assertTrue(self.environment.new_running)
        # 迁移前的备份必须真实存在，即便一切都成功了也不会被清理/隐藏。
        self.assertTrue(self.environment.backup_path.exists())
        self.assertNotEqual(self.environment.database.read_bytes(), b"")
        self.assertNotEqual(before, b"")

    def test_no_compatible_schema_shortcut_backup_always_happens(self) -> None:
        """即便版本号相同（无需真正迁移），也必须无条件备份数据库。"""
        self.coordinator.update(self.environment.request())
        self.assertIn("run_migration", self.environment.calls)
        self.assertTrue(self.environment.backup_path.is_file())
        self.assertEqual(_read_values(self.environment.backup_path), ("seed",))

    def test_failure_before_backup_restarts_old_service_without_touching_database(self) -> None:
        before = self.environment.database.read_bytes()
        self.environment.fail_at = "stop_old_service"
        with self.assertRaisesRegex(InstallError, "installer_error"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(self.environment.calls, ["stop_old_service"])
        self.assertEqual(self.environment.database.read_bytes(), before)
        self.assertFalse(self.environment.backup_path.exists())

    def test_failure_during_backup_itself_restarts_old_service(self) -> None:
        before = self.environment.database.read_bytes()
        # 预先占用备份路径，让 backup_database 内部的 O_EXCL 创建失败；
        # stage 停在 "stopped"，从未尝试 restore，字节必须原样不变。
        self.environment.backup_path.write_bytes(b"stale-leftover")
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(self.environment.database.read_bytes(), before)
        self.assertTrue(self.environment.old_running)
        self.assertEqual(self.environment.current, "old")
        self.assertNotIn("run_migration", self.environment.calls)

    def test_failure_during_migration_restores_database_and_old_service(self) -> None:
        # backup 之后才失败，走 restore_database（备份→恢复不保证逐字节一致，
        # 只保证逻辑行一致），因此用行级内容而不是原始字节比较。
        self.environment.fail_at = "run_migration"
        with self.assertRaisesRegex(InstallError, "installer_error"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(_read_values(self.environment.database), ("seed",))
        self.assertEqual(self.environment.current, "old")
        self.assertTrue(self.environment.old_running)
        self.assertNotIn("activate_new_runtime", self.environment.calls)
        self.assertNotIn("retain_runtimes", self.environment.calls)

    def test_failure_during_current_replacement_restores_database_and_old_runtime(self) -> None:
        self.environment.fail_at = "activate_new_runtime"
        with self.assertRaisesRegex(InstallError, "installer_error"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(_read_values(self.environment.database), ("seed",))
        self.assertEqual(self.environment.current, "old")
        self.assertTrue(self.environment.old_running)
        self.assertNotIn("commit_receipt", self.environment.calls)

    def test_failure_during_receipt_commit_restores_database_and_old_runtime(self) -> None:
        self.environment.fail_at = "commit_receipt"
        with self.assertRaisesRegex(InstallError, "installer_error"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(_read_values(self.environment.database), ("seed",))
        self.assertEqual(self.environment.current, "old")
        self.assertEqual(self.environment.receipt_current, "old")
        self.assertTrue(self.environment.old_running)
        self.assertIn("revert_to_old_runtime", self.environment.calls)

    def test_failed_new_service_restores_database_and_previous_runtime_without_new_write(
        self,
    ) -> None:
        """健康检查超时（不写入任何数据）必须走干净回滚而非 conflict。"""
        self.environment.health_result = False
        with self.assertRaisesRegex(InstallError, "activation_failed"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(self.environment.current, "old")
        self.assertEqual(_read_values(self.environment.database), ("seed",))
        self.assertTrue(self.environment.old_running)
        self.assertFalse(self.environment.new_running)

    def test_service_refresh_raising_directly_also_restores_cleanly(self) -> None:
        self.environment.fail_at = "start_new_service"
        with self.assertRaisesRegex(InstallError, "installer_error"):
            self.coordinator.update(self.environment.request())
        self.assertEqual(self.environment.current, "old")
        self.assertEqual(_read_values(self.environment.database), ("seed",))
        self.assertTrue(self.environment.old_running)

    def test_external_write_after_new_runtime_starts_blocks_destructive_restore(self) -> None:
        """新 service 已经写入真实数据后，绝不能销毁性恢复。"""
        self.environment.write_on_start = "written-by-new-service"
        self.environment.health_result = False
        with self.assertRaisesRegex(InstallError, "rollback_conflict"):
            self.coordinator.update(self.environment.request())
        self.assertTrue(self.environment.backup_path.exists())
        self.assertIn("written-by-new-service", _read_values(self.environment.database))
        self.assertFalse(self.environment.old_running)
        self.assertFalse(self.environment.new_running)
        # conflict 时绝不允许恢复 current/receipt：新状态原样保留。
        self.assertNotIn("revert_to_old_runtime", self.environment.calls)
        self.assertNotIn("retain_runtimes", self.environment.calls)

    def test_external_write_during_commit_failure_also_blocks_destructive_restore(self) -> None:
        """即使失败点是 receipt commit，只要已经发生外部写入也必须是 conflict。"""

        def commit_with_external_write() -> None:
            self.environment.calls.append("commit_receipt")
            _write_external_row(self.environment.database, "written-during-commit")
            raise InstallError("installer_error", "manifest")

        with self.assertRaisesRegex(InstallError, "rollback_conflict"):
            self.coordinator.update(self.environment.request(commit_receipt=commit_with_external_write))
        self.assertTrue(self.environment.backup_path.exists())
        self.assertIn("written-during-commit", _read_values(self.environment.database))
        self.assertNotIn("revert_to_old_runtime", self.environment.calls)

    def test_guard_open_failure_after_stop_still_restarts_old_service(self) -> None:
        request = self.environment.request(database=self.tmp / "does-not-exist.db")
        with self.assertRaisesRegex(InstallError, "request_invalid"):
            self.coordinator.update(request)
        self.assertEqual(self.environment.calls, ["stop_old_service", "restart_old_service"])
        self.assertTrue(self.environment.old_running)

    def test_new_service_always_stopped_before_any_recovery_decision(self) -> None:
        self.environment.fail_at = "start_new_service"
        with self.assertRaises(InstallError):
            self.coordinator.update(self.environment.request())
        self.assertIn("stop_new_service", self.environment.calls)
        self.assertLess(
            self.environment.calls.index("stop_new_service"),
            len(self.environment.calls),
        )


class UpdateStateHomeIsolationTests(unittest.TestCase):
    """验证 update 事务绝不触碰 Memory/Skills/Workspace 之类的用户数据。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_home = Path(self.temporary.name) / "state"
        self.state_home.mkdir()
        self.environment = _Environment(self.state_home)
        self.memory_file = self.state_home / "MEMORY.md"
        self.memory_file.write_text("# Long-term Memory\nkeep me\n", encoding="utf-8")
        self.skills_dir = self.state_home / "skills" / "example"
        self.skills_dir.mkdir(parents=True)
        (self.skills_dir / "SKILL.md").write_text("skill content", encoding="utf-8")
        self.workspace_dir = self.state_home / "workspace"
        self.workspace_dir.mkdir()
        (self.workspace_dir / "note.txt").write_text("workspace content", encoding="utf-8")
        self.watched = {
            self.memory_file: self.memory_file.read_bytes(),
            self.skills_dir / "SKILL.md": (self.skills_dir / "SKILL.md").read_bytes(),
            self.workspace_dir / "note.txt": (self.workspace_dir / "note.txt").read_bytes(),
        }
        self.coordinator = UpdateCoordinator()

    def _assert_untouched(self) -> None:
        for path, original in self.watched.items():
            self.assertTrue(path.is_file(), path)
            self.assertEqual(path.read_bytes(), original, path)

    def test_successful_update_never_touches_user_state_files(self) -> None:
        self.coordinator.update(self.environment.request())
        self._assert_untouched()

    def test_clean_rollback_never_touches_user_state_files(self) -> None:
        self.environment.fail_at = "activate_new_runtime"
        with self.assertRaises(InstallError):
            self.coordinator.update(self.environment.request())
        self._assert_untouched()

    def test_rollback_conflict_never_touches_user_state_files(self) -> None:
        self.environment.write_on_start = "conflict-write"
        self.environment.health_result = False
        with self.assertRaisesRegex(InstallError, "rollback_conflict"):
            self.coordinator.update(self.environment.request())
        self._assert_untouched()


if __name__ == "__main__":
    unittest.main()
