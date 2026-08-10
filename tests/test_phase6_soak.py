"""Phase 6 production soak 的只读 invariant 与稳定错误码测试。"""

import tempfile
import unittest
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.evals.phase6_soak import (
    SoakMonitorError,
    SoakSnapshot,
    collect_soak_snapshot,
    evaluate_snapshot,
    finish_soak,
    gateway_lease_is_fresh,
    record_restart_result,
    record_snapshot,
    render_progress,
    resume_soak,
    start_soak,
    write_progress,
)
from lobster0.gateway_service import ServiceStatus
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations


class Phase6SoakSnapshotTest(unittest.TestCase):
    """验证 monitor 不采集私人内容，只输出封闭 aggregate。"""

    def setUp(self) -> None:
        """创建 owner-only 状态目录和完整临时 SQLite。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir(mode=0o700)
        self.database = Database(self.root / "lobster0.db")
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


class Phase6SoakSessionTest(unittest.TestCase):
    """验证 checkpoint 精确时长、恢复和 fail-closed 状态机。"""

    def setUp(self) -> None:
        """创建私有 checkpoint 目录和固定 release identity。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.checkpoint = self.root / "soak.json"
        self.progress = self.root / "progress.txt"
        self.state_home = self.root / "state"
        self.state_home.mkdir(mode=0o700)
        self.commit = "a" * 40
        self.token = "run-token-123456"
        self.now = datetime(2026, 8, 10, tzinfo=UTC)

    def tearDown(self) -> None:
        """删除隔离 checkpoint。"""
        self.temporary.cleanup()

    def _healthy(self, offset: int) -> SoakSnapshot:
        """返回指定秒数的健康匿名采样。"""
        observed = self.now + timedelta(seconds=offset)
        return SoakSnapshot(
            observed.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
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

    def _start(self, *, duration: int = 86_400):
        """用固定输入启动测试 session。"""
        return start_soak(
            self.checkpoint,
            commit=self.commit,
            run_token=self.token,
            state_home=self.state_home,
            duration_seconds=duration,
            now=self.now,
            monotonic_now=100.0,
        )

    def test_start_is_exclusive_private_and_resume_binds_all_inputs(self) -> None:
        """checkpoint 只能新建一次，resume 必须绑定 commit/run/home/duration。"""
        session, checkpoint = self._start()
        self.assertEqual(checkpoint.status, "running")
        self.assertEqual(checkpoint.elapsed_seconds, 0)
        self.assertEqual(checkpoint.sample_count, 0)
        self.assertEqual(checkpoint.restart_status, "pending")
        self.assertEqual(self.checkpoint.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, self.checkpoint.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(SoakMonitorError, "checkpoint_exists"):
            self._start()

        resumed, same = resume_soak(
            self.checkpoint,
            commit=self.commit,
            run_token=self.token,
            state_home=self.state_home,
            duration_seconds=86_400,
            now=self.now + timedelta(seconds=30),
            monotonic_now=200.0,
        )
        self.assertEqual(same, checkpoint)
        self.assertEqual(resumed.commit, session.commit)
        mismatches = (
            {"commit": "b" * 40},
            {"run_token": "different-token"},
            {"state_home": self.root / "other"},
            {"duration_seconds": 10},
        )
        for changed in mismatches:
            values = {
                "commit": self.commit,
                "run_token": self.token,
                "state_home": self.state_home,
                "duration_seconds": 86_400,
            }
            values.update(changed)
            with self.subTest(changed=changed), self.assertRaises(SoakMonitorError):
                resume_soak(
                    self.checkpoint,
                    **values,
                    now=self.now + timedelta(seconds=30),
                    monotonic_now=200.0,
                )

    def test_samples_are_idempotent_and_time_anomalies_fail_closed(self) -> None:
        """重复 sample 不计数，gap、时钟回退和 sleep jump 都终止本次 soak。"""
        session, _ = self._start()
        first = record_snapshot(session, self._healthy(60), monotonic_now=160.0)
        duplicate = record_snapshot(session, self._healthy(60), monotonic_now=160.0)
        self.assertEqual(duplicate, first)
        self.assertEqual(first.sample_count, 1)

        failed = record_snapshot(session, self._healthy(241), monotonic_now=341.0)
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.violation_codes, ("monitor_gap",))
        self.assertEqual(finish_soak(session).status, "failed")
        with self.assertRaisesRegex(SoakMonitorError, "soak_failed"):
            resume_soak(
                self.checkpoint,
                commit=self.commit,
                run_token=self.token,
                state_home=self.state_home,
                duration_seconds=86_400,
                now=self.now + timedelta(seconds=242),
                monotonic_now=342.0,
            )

        for name, snapshot, monotonic, code in (
            ("clock", self._healthy(59), 159.0, "clock_rollback"),
            ("sleep", self._healthy(120), 280.0, "clock_jump"),
            (
                "invariant",
                replace(self._healthy(120), secret_matches=1),
                220.0,
                "secret_match",
            ),
        ):
            path = self.root / f"{name}.json"
            other, _ = start_soak(
                path,
                commit=self.commit,
                run_token=f"{self.token}-{name}",
                state_home=self.state_home,
                duration_seconds=86_400,
                now=self.now,
                monotonic_now=100.0,
            )
            if name == "clock":
                record_snapshot(other, self._healthy(60), monotonic_now=160.0)
            result = record_snapshot(other, snapshot, monotonic_now=monotonic)
            self.assertEqual(result.status, "failed")
            self.assertIn(code, result.violation_codes)

    def test_exact_duration_requires_healthy_samples_and_restart_pass(self) -> None:
        """不足一秒不能 PASS，达到 required time 且恢复通过才可终结。"""
        session, _ = self._start(duration=10)
        record_restart_result(session, passed=True)
        before = record_snapshot(session, self._healthy(9), monotonic_now=109.0)
        self.assertEqual(before.elapsed_seconds, 9)
        self.assertEqual(finish_soak(session).status, "running")

        at_required = record_snapshot(session, self._healthy(10), monotonic_now=110.0)
        self.assertEqual(at_required.elapsed_seconds, 10)
        passed = finish_soak(session)
        self.assertEqual(passed.status, "passed")
        self.assertEqual(resume_soak(
            self.checkpoint,
            commit=self.commit,
            run_token=self.token,
            state_home=self.state_home,
            duration_seconds=10,
            now=self.now + timedelta(seconds=11),
            monotonic_now=111.0,
        )[1].status, "passed")

    def test_progress_is_bounded_and_atomic(self) -> None:
        """外部进度只含状态、时长、sample 和 violation 数。"""
        session, _ = self._start(duration=10)
        record_restart_result(session, passed=True)
        record_snapshot(session, self._healthy(10), monotonic_now=110.0)
        checkpoint = finish_soak(session)
        rendered = render_progress(checkpoint)
        self.assertEqual(
            rendered,
            "status=passed elapsed=00:00:10 required=00:00:10 samples=1 violations=0",
        )
        write_progress(self.progress, checkpoint)
        self.assertEqual(self.progress.read_text(encoding="utf-8"), rendered + "\n")
        self.assertEqual(self.progress.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
