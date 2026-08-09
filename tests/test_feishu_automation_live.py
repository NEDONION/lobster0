"""真实飞书 Automation durable evaluator 的离线 SQLite 回归。"""

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.automation.models import (
    DeliveryTarget,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
)
from miniclaw.automation.repository import ScheduledTaskRepository, TaskRunRepository
from miniclaw.evals.cases import load_feishu_automation_live_cases
from miniclaw.evals.feishu_automation_live import (
    AutomationLiveCaseResult,
    build_automation_evidence_report,
    capture_automation_checkpoint,
    evaluate_automation_case,
)
from miniclaw.storage.database import Database
from miniclaw.storage.migrations import apply_migrations
from miniclaw.storage.repositories import OwnerRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FeishuAutomationLiveEvaluatorTest(unittest.TestCase):
    """用真实 schema 验证十种 Automation durable shape。"""

    def setUp(self) -> None:
        """创建隔离 SQLite、Owner 与一个目标飞书任务。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "miniclaw.db")
        apply_migrations(self.database)
        self.owner = OwnerRepository(self.database).get_or_create()
        self.now = datetime(2026, 8, 10, 8, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.task = self.tasks.create(
            owner_id=self.owner.id,
            name="production live task",
            schedule=ScheduleSpec(
                kind=ScheduleKind.INTERVAL,
                expression="60",
                timezone="Asia/Shanghai",
                next_run_at=self.now,
            ),
            prompt="Return a bounded production status.",
            skill_names=(),
            delivery=DeliveryTarget(
                route="owner",
                channel="feishu",
                account_id="synthetic-account",
                conversation_id="synthetic-conversation",
            ),
            policy_profile="automation-default",
            budget=TaskBudget(max_tool_calls=1),
        )
        cases = load_feishu_automation_live_cases(PROJECT_ROOT / "evals" / "scenarios")
        self.cases = {case.automation_fixture: case for case in cases}

    def test_all_ten_durable_shapes_pass_only_their_closed_evidence(self) -> None:
        """每种 fixture 都必须由 checkpoint 后的真实表关系独立证明。"""
        fixtures = tuple(self.cases)
        for fixture in fixtures:
            with self.subTest(fixture=fixture):
                self._reset_case_database()
                result = self._evaluate_positive(fixture)
                self.assertEqual(result.status, "pass", result)
                self.assertEqual(result.evidence_failed, ())
                self.assertEqual(
                    set(result.evidence_passed),
                    set(self.cases[fixture].expected.automation_evidence),
                )

    def test_corruption_is_stable_failure_without_row_content(self) -> None:
        """错误目标、重复投递、pending 泄漏、running 与时钟回退都不能 PASS。"""
        corruptions = (
            "wrong_target",
            "duplicate_delivery",
            "pending_leak",
            "stale_running",
            "clock_rollback",
        )
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                self._reset_case_database()
                checkpoint = self._checkpoint()
                run_id = self._new_run(status="succeeded", provider=True)
                self._delivery(run_id)
                if corruption == "wrong_target":
                    checkpoint = capture_automation_checkpoint(
                        self.database.path,
                        task_ids=(self.task.id + 999,),
                        now=self.now,
                    )
                elif corruption == "duplicate_delivery":
                    self._delivery(run_id, part_index=1)
                elif corruption == "pending_leak":
                    self._approval(run_id, status="pending", tool_status="waiting_approval")
                elif corruption == "stale_running":
                    self._update_run(run_id, status="running", worker_id="stale", lease=True)
                else:
                    self._update_run(run_id, created_at=self.now - timedelta(seconds=1))

                result = evaluate_automation_case(
                    self.database.path,
                    checkpoint,
                    self.cases["live_one_shot_delivery"],
                )

                self.assertEqual(result.status, "fail")
                self.assertRegex(result.error_code or "", r"^[a-z][a-z0-9_]+$")
                self.assertNotIn("synthetic", result.error_code or "")

    def test_verified_report_requires_exact_ten_passes_and_zero_secrets(self) -> None:
        """Automation report 只有 10/10、零 skip/fail/secret 才能 VERIFIED。"""
        results = tuple(
            AutomationLiveCaseResult(
                case_id=f"FEISHU-AUTO-{index:03d}",
                status="pass",
                evidence_passed=("delivery_once",),
                evidence_failed=(),
                human_status="pass",
                error_code=None,
            )
            for index in range(1, 11)
        )

        report = build_automation_evidence_report(
            commit="a" * 40,
            started_at="2026-08-10T08:00:00Z",
            finished_at="2026-08-10T08:10:00Z",
            results=results,
            secret_matches=0,
        )

        self.assertEqual(report["suite"], "feishu-automation")
        self.assertEqual(report["counts"]["cases_passed"], 10)
        self.assertEqual(report["release_status"], "FEISHU_AUTOMATION_VERIFIED")
        failed = build_automation_evidence_report(
            commit="a" * 40,
            started_at="2026-08-10T08:00:00Z",
            finished_at="2026-08-10T08:10:00Z",
            results=results,
            secret_matches=1,
        )
        self.assertEqual(failed["release_status"], "FEISHU_AUTOMATION_FAILED")

    def _evaluate_positive(self, fixture: str) -> AutomationLiveCaseResult:
        """为一个 fixture 写入最小合法事实并调用只读 evaluator。"""
        if fixture == "live_approval_continuation":
            run_id = self._new_run(status="waiting_approval")
            approval_id, parent_turn = self._approval(run_id)
            checkpoint = self._checkpoint()
            child = self._turn(parent_turn_id=parent_turn, provider=True)
            self._update_run(
                run_id,
                status="succeeded",
                turn_id=child,
                approval_id=None,
                completed=True,
            )
            with self.database.connect() as connection:
                connection.execute(
                    "UPDATE approvals SET status = 'consumed', decided_at = ? WHERE id = ?",
                    ((self.now + timedelta(seconds=1)).isoformat(), approval_id),
                )
                connection.execute(
                    "UPDATE tool_runs SET status = 'succeeded', completed_at = ? "
                    "WHERE id = (SELECT tool_run_id FROM approvals WHERE id = ?)",
                    ((self.now + timedelta(seconds=1)).isoformat(), approval_id),
                )
            self._tool(child, "complete_task", "succeeded")
            self._delivery(run_id)
        else:
            checkpoint = self._checkpoint()
            if fixture == "live_interval_two_slots":
                for offset in (0, 60):
                    run_id = self._new_run(
                        status="succeeded",
                        provider=True,
                        scheduled_for=self.now + timedelta(seconds=offset),
                    )
                    self._delivery(run_id)
            elif fixture == "live_interrupted_recovery":
                self._new_run(status="interrupted")
            elif fixture == "live_waiting_approval":
                run_id = self._new_run(status="waiting_approval")
                self._approval(run_id)
            elif fixture == "live_structured_silence":
                self._new_run(
                    status="succeeded",
                    response={"notify": False, "text": ""},
                )
            elif fixture == "live_durable_estop":
                with self.database.connect() as connection:
                    connection.execute(
                        "UPDATE automation_control SET halted = 1, revision = revision + 1, "
                        "updated_at = ? WHERE singleton = 1",
                        ((self.now + timedelta(seconds=1)).isoformat(),),
                    )
            elif fixture == "live_budget_stop":
                run_id = self._new_run(
                    status="failed",
                    provider=True,
                    error_code="task_budget_tool_calls",
                )
                turn_id = self._run_turn(run_id)
                assert turn_id is not None
                self._tool(turn_id, "system_info", "succeeded")
            elif fixture == "live_delivery_unknown_recovery":
                run_id = self._new_run(status="succeeded")
                self._delivery(run_id, attempts=2)
            else:
                run_id = self._new_run(status="succeeded", provider=True)
                self._delivery(run_id)
        return evaluate_automation_case(self.database.path, checkpoint, self.cases[fixture])

    def _reset_case_database(self) -> None:
        """清除上个 subtest 的运行事实并复位 E-stop。"""
        with self.database.connect() as connection:
            for table in (
                "audit_events",
                "deliveries",
                "execution_plans",
                "checkpoints",
                "task_runs",
                "approvals",
                "tool_runs",
                "turns",
                "sessions",
            ):
                connection.execute(f"DELETE FROM {table}")
            connection.execute(
                "UPDATE automation_control SET halted = 0, revision = revision + 1, "
                "updated_at = ? WHERE singleton = 1",
                (self.now.isoformat(),),
            )

    def _checkpoint(self):
        """捕获当前目标 Task 的 Automation checkpoint。"""
        return capture_automation_checkpoint(
            self.database.path,
            task_ids=(self.task.id,),
            now=self.now,
        )

    def _new_run(
        self,
        *,
        status: str,
        provider: bool = False,
        scheduled_for: datetime | None = None,
        response: dict[str, object] | None = None,
        error_code: str | None = None,
    ) -> int:
        """创建一个 Run 并直接结算为测试需要的合法 shape。"""
        run = self.runs.enqueue(self.task, scheduled_for=scheduled_for or self.now)
        turn_id = self._turn(provider=provider) if provider else None
        self._update_run(
            run.id,
            status=status,
            turn_id=turn_id,
            response=response,
            error_code=error_code,
            completed=status in {"succeeded", "failed", "interrupted"},
        )
        return run.id

    def _update_run(
        self,
        run_id: int,
        *,
        status: str | None = None,
        turn_id: int | None = None,
        approval_id: int | None = None,
        response: dict[str, object] | None = None,
        error_code: str | None = None,
        completed: bool = False,
        worker_id: str | None = None,
        lease: bool = False,
        created_at: datetime | None = None,
    ) -> None:
        """按公开 schema 更新 Run，不插入业务正文。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                UPDATE task_runs SET status = COALESCE(?, status), turn_id = COALESCE(?, turn_id),
                    approval_id = ?, response_json = ?, error_code = ?, completed_at = ?,
                    worker_id = ?, lease_expires_at = ?, created_at = COALESCE(?, created_at)
                WHERE id = ?
                """,
                (
                    status,
                    turn_id,
                    approval_id,
                    None if response is None else json.dumps(response, sort_keys=True),
                    error_code,
                    (self.now + timedelta(seconds=1)).isoformat() if completed else None,
                    worker_id,
                    (self.now + timedelta(minutes=1)).isoformat() if lease else None,
                    None if created_at is None else created_at.isoformat(),
                    run_id,
                ),
            )

    def _turn(self, *, parent_turn_id: int | None = None, provider: bool = False) -> int:
        """插入合成 automation Session/Turn，只保存 request existence bit。"""
        now = (self.now + timedelta(seconds=1)).isoformat()
        with self.database.connect() as connection:
            session = connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'automation', ?, 'active', ?, ?)
                """,
                (self.owner.id, f"session-{self._next_id('sessions')}", now, now),
            )
            turn = connection.execute(
                """
                INSERT INTO turns (
                    session_id, parent_turn_id, inbound_event_id, status, model,
                    started_at, completed_at, runtime_snapshot_json
                ) VALUES (?, ?, ?, 'completed', 'deepseek-v4-pro', ?, ?, ?)
                """,
                (
                    int(session.lastrowid),
                    parent_turn_id,
                    f"event-{self._next_id('turns')}",
                    now,
                    now,
                    json.dumps({"provider_request_id": "present"} if provider else {}),
                ),
            )
        return int(turn.lastrowid)

    def _tool(self, turn_id: int, name: str, status: str) -> int:
        """插入一条最小 ToolRun。"""
        now = (self.now + timedelta(seconds=1)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO tool_runs (
                    turn_id, tool_call_id, tool_name, arguments_json, arguments_hash,
                    policy_action, status, created_at, completed_at
                ) VALUES (?, ?, ?, '{}', ?, 'allow', ?, ?, ?)
                """,
                (turn_id, f"call-{self._next_id('tool_runs')}", name, "a" * 64, status, now, now),
            )
        return int(cursor.lastrowid)

    def _approval(
        self,
        run_id: int,
        *,
        status: str = "pending",
        tool_status: str = "waiting_approval",
    ) -> tuple[int, int]:
        """为 Run 插入参数绑定的 write_file Approval。"""
        turn_id = self._run_turn(run_id) or self._turn()
        tool_id = self._tool(turn_id, "write_file", tool_status)
        now = (self.now + timedelta(seconds=1)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO approvals (
                    user_id, turn_id, tool_run_id, tool_name, arguments_hash,
                    summary, status, expires_at, created_at
                ) VALUES (?, ?, ?, 'write_file', ?, 'bounded write', ?, ?, ?)
                """,
                (
                    self.owner.id,
                    turn_id,
                    tool_id,
                    "a" * 64,
                    status,
                    (self.now + timedelta(minutes=5)).isoformat(),
                    now,
                ),
            )
            approval_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE task_runs SET approval_id = ?, turn_id = ? WHERE id = ?",
                (approval_id, turn_id, run_id),
            )
        return approval_id, turn_id

    def _delivery(self, run_id: int, *, attempts: int = 1, part_index: int = 0) -> int:
        """插入一条已发送且绑定 TaskRun 的 Feishu Delivery。"""
        now = (self.now + timedelta(seconds=1)).isoformat()
        with self.database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO deliveries (
                    message_id, task_run_id, channel, account_id,
                    external_conversation_id, reply_to_message_id, delivery_kind,
                    part_index, content, content_hash, idempotency_key,
                    platform_message_id, status, attempts, created_at, updated_at, sent_at
                ) VALUES (
                    NULL, ?, 'feishu', 'synthetic-account', 'synthetic-conversation', '',
                    'message', ?, 'bounded', ?, ?, 'receipt', 'sent', ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    part_index,
                    "b" * 64,
                    f"task-run:{run_id}:part:{part_index}",
                    attempts,
                    now,
                    now,
                    now,
                ),
            )
        return int(cursor.lastrowid)

    def _run_turn(self, run_id: int) -> int | None:
        """读取 Run 当前 Turn ID。"""
        with self.database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT turn_id FROM task_runs WHERE id = ?", (run_id,)
            ).fetchone()
        return None if row is None or row[0] is None else int(row[0])

    def _next_id(self, table: str) -> int:
        """为测试生成稳定、非外部的唯一 suffix。"""
        if table not in {"sessions", "turns", "tool_runs"}:
            raise ValueError("unsupported table")
        with self.database.connect_read_only() as connection:
            row = connection.execute(
                f"SELECT COALESCE(MAX(id), 0) + 1 FROM {table}"
            ).fetchone()
        return int(row[0])


if __name__ == "__main__":
    unittest.main()
