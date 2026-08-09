"""Phase 6 production soak 的只读 invariant 与稳定错误码测试。"""

import tempfile
import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.evals.phase6_soak import (
    SoakMonitorError,
    SoakSnapshot,
    collect_soak_snapshot,
    evaluate_snapshot,
    gateway_lease_is_fresh,
)
from miniclaw.install.service import ServiceStatus
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations


class Phase6SoakSnapshotTest(unittest.TestCase):
    """验证 monitor 不采集私人内容，只输出封闭 aggregate。"""

    def setUp(self) -> None:
        """创建 owner-only 状态目录和完整临时 SQLite。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.database = Database(self.root / "miniclaw.db")
        apply_migrations(self.database)
        self.database.path.chmod(0o600)
        self.now = datetime(2026, 8, 10, tzinfo=UTC)

    def tearDown(self) -> None:
        """删除隔离的测试状态。"""
        self.temporary.cleanup()

    def test_collects_only_closed_healthy_snapshot(self) -> None:
        """健康状态只能产生计划允许的十一个匿名字段。"""
        snapshot = collect_soak_snapshot(
            service_status=lambda: ServiceStatus(True, True, True),
            database=self.database,
            lease_check=lambda: True,
            private_paths=(self.root, self.database.path, self.evidence),
            evidence_paths=(self.evidence,),
            secrets=(),
            now=self.now,
        )

        self.assertEqual(
            snapshot,
            SoakSnapshot(
                observed_at="2026-08-10T00:00:00.000000Z",
                service_loaded=True,
                service_running=True,
                gateway_lease_fresh=True,
                database_healthy=True,
                running_turns=0,
                stale_task_runs=0,
                pending_deliveries=0,
                failed_deliveries=0,
                pending_approvals=0,
                secret_matches=0,
                owner_only_state=True,
            ),
        )
        self.assertEqual(
            tuple(field.name for field in fields(SoakSnapshot)),
            (
                "observed_at",
                "service_loaded",
                "service_running",
                "gateway_lease_fresh",
                "database_healthy",
                "running_turns",
                "stale_task_runs",
                "pending_deliveries",
                "failed_deliveries",
                "pending_approvals",
                "secret_matches",
                "owner_only_state",
            ),
        )
        self.assertEqual(evaluate_snapshot(snapshot), ())

    def test_each_unhealthy_fact_has_a_stable_violation_code(self) -> None:
        """每个 production invariant 都必须独立失败且按 code 排序。"""
        healthy = SoakSnapshot(
            "2026-08-10T00:00:00.000000Z",
            True,
            True,
            True,
            True,
            0,
            0,
            0,
            0,
            0,
            0,
            True,
        )
        cases = {
            "service_unloaded": replace(healthy, service_loaded=False),
            "service_not_running": replace(healthy, service_running=False),
            "gateway_lease_unhealthy": replace(healthy, gateway_lease_fresh=False),
            "database_unhealthy": replace(healthy, database_healthy=False),
            "stuck_turn": replace(healthy, running_turns=1),
            "stale_task_run": replace(healthy, stale_task_runs=1),
            "delivery_backlog": replace(healthy, pending_deliveries=1),
            "delivery_failed": replace(healthy, failed_deliveries=1),
            "orphan_approval": replace(healthy, pending_approvals=1),
            "secret_match": replace(healthy, secret_matches=1),
            "state_permissions_unsafe": replace(healthy, owner_only_state=False),
        }
        for code, snapshot in cases.items():
            with self.subTest(code=code):
                self.assertEqual(tuple(item.code for item in evaluate_snapshot(snapshot)), (code,))

        combined = replace(
            healthy,
            service_loaded=False,
            database_healthy=False,
            secret_matches=2,
        )
        self.assertEqual(
            tuple(item.code for item in evaluate_snapshot(combined)),
            ("database_unhealthy", "secret_match", "service_unloaded"),
        )

    def test_clock_rollback_and_query_errors_do_not_expose_details(self) -> None:
        """时钟回退和 SQLite 查询异常只返回固定码。"""
        snapshot = collect_soak_snapshot(
            service_status=lambda: ServiceStatus(True, True, True),
            database=self.database,
            lease_check=lambda: True,
            private_paths=(self.root,),
            now=self.now,
        )
        self.assertEqual(
            tuple(
                item.code
                for item in evaluate_snapshot(
                    snapshot,
                    previous_observed_at=self.now + timedelta(seconds=1),
                )
            ),
            ("clock_rollback",),
        )
        with self.assertRaisesRegex(SoakMonitorError, "database_query_failed"):
            collect_soak_snapshot(
                service_status=lambda: ServiceStatus(True, True, True),
                database=Database(self.root / "missing.db"),
                lease_check=lambda: True,
                private_paths=(self.root,),
                now=self.now,
            )

    def test_unheld_or_malformed_gateway_lease_is_not_fresh(self) -> None:
        """存在 lease 文件不等于有匹配 commit 的活跃持锁者。"""
        lease = self.root / "gateway.lock"
        lease.write_text("{}", encoding="utf-8")
        lease.chmod(0o600)
        self.assertFalse(gateway_lease_is_fresh(lease, "a" * 40))


if __name__ == "__main__":
    unittest.main()
