"""真实飞书 Automation durable evaluator 与 runner 的离线回归。"""

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from lobster0.automation.models import (
    DeliveryTarget,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
)
from lobster0.automation.repository import ScheduledTaskRepository, TaskRunRepository
from lobster0.doctor import CheckResult, CheckStatus
from lobster0.evals.cases import load_feishu_automation_live_cases
from lobster0.evals.feishu_automation_live import (
    AutomationLiveCaseResult,
    AutomationLiveExecution,
    FeishuAutomationLiveError,
    _assert_automation_state_clean,
    _execute_automation_live_cases,
    _validate_automation_preflight_state,
    build_automation_evidence_report,
    capture_automation_checkpoint,
    evaluate_automation_case,
    run_feishu_automation_live_harness,
)
from lobster0.storage.database import Database
from lobster0.storage.migrations import apply_migrations
from lobster0.storage.repositories import OwnerRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FeishuAutomationLiveEvaluatorTest(unittest.TestCase):
    """用真实 schema 验证十种 Automation durable shape。"""

    def setUp(self) -> None:
        """创建隔离 SQLite、Owner 与一个目标飞书任务。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database = Database(Path(self.temporary_directory.name) / "lobster0.db")
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

    def test_waiting_approval_requires_one_sent_approval_delivery(self) -> None:
        """等待审批必须由一张已发送卡片证明，零投递或普通消息都不能 PASS。"""
        case = self.cases["live_waiting_approval"]
        self.assertEqual(case.expected.delivery_count, 1)
        self.assertIn("approval_delivery_once", case.expected.automation_evidence)
        checkpoint = self._checkpoint()
        run_id = self._new_run(status="waiting_approval")
        self._approval(run_id)

        missing = evaluate_automation_case(self.database.path, checkpoint, case)
        self.assertEqual(missing.status, "fail")

        self._delivery(run_id, kind="message")
        wrong_kind = evaluate_automation_case(self.database.path, checkpoint, case)
        self.assertEqual(wrong_kind.status, "fail")

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
                self._delivery(run_id, kind="approval")
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

    def _delivery(
        self,
        run_id: int,
        *,
        attempts: int = 1,
        part_index: int = 0,
        kind: str = "message",
    ) -> int:
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
                    ?, ?, 'bounded', ?, ?, 'receipt', 'sent', ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    kind,
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


class FeishuAutomationLiveHarnessTest(unittest.TestCase):
    """验证 Automation Live runner 的确认门与静态 preflight。"""

    def test_missing_confirmation_has_zero_state_or_output_side_effects(self) -> None:
        """未确认时不能读取 preflight，也不能创建 home、case 或 Evidence。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "missing-home"
            scenarios = root / "missing-scenarios"
            output = root / "missing-evidence"

            code = run_feishu_automation_live_harness(
                [
                    "--home",
                    str(home),
                    "--root",
                    str(scenarios),
                    "--output-dir",
                    str(output),
                ]
            )

            self.assertEqual(code, 2)
            self.assertFalse(home.exists())
            self.assertFalse(scenarios.exists())
            self.assertFalse(output.exists())

    def test_preflight_rejects_every_production_isolation_failure(self) -> None:
        """Channel、权限、Automation、Seatbelt、Provider 与 durable 状态均失败关闭。"""
        cases = tuple(object() for _ in range(10))
        checks = (CheckResult("database", CheckStatus.PASS, "ok"),)
        scenarios = (
            (self._config(feishu=False), checks, 0, "a" * 40, False, cases,
             "feishu_channel_disabled"),
            (self._config(telegram=True), checks, 0, "a" * 40, False, cases,
             "peer_channel_enabled"),
            (self._config(mode="autopilot"), checks, 0, "a" * 40, False, cases,
             "unsafe_permission_mode"),
            (self._config(automation=False), checks, 0, "a" * 40, False, cases,
             "automation_disabled"),
            (self._config(backend="docker"), checks, 0, "a" * 40, False, cases,
             "seatbelt_required"),
            (self._config(network="allowlist"), checks, 0, "a" * 40, False, cases,
             "sandbox_network_unsafe"),
            (self._config(model="provider/model"), checks, 0, "a" * 40, False, cases,
             "deepseek_provider_required"),
            (self._config(), checks, 1, "a" * 40, False, cases,
             "pending_approval_exists"),
            (self._config(), checks, 0, "unknown", False, cases,
             "repository_commit_unavailable"),
            (self._config(), checks, 0, "a" * 40, True, cases,
             "repository_dirty"),
            (self._config(), (CheckResult("database", CheckStatus.FAIL, "private"),),
             0, "a" * 40, False, cases, "doctor_preflight_failed"),
            (self._config(), checks, 0, "a" * 40, False, cases[:-1],
             "automation_live_case_count_invalid"),
        )
        for config, local_checks, pending, commit, dirty, loaded, expected in scenarios:
            with self.subTest(expected=expected), self.assertRaises(
                FeishuAutomationLiveError
            ) as raised:
                _validate_automation_preflight_state(
                    config=config,
                    checks=local_checks,
                    pending_approvals=pending,
                    commit=commit,
                    dirty=dirty,
                    cases=loaded,
                )
            self.assertEqual(raised.exception.code, expected)

    def test_confirmed_preflight_failure_creates_no_evidence(self) -> None:
        """确认后的静态失败必须在 Gateway 和 Evidence mkdir 前返回稳定码。"""
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with patch(
                "lobster0.evals.feishu_automation_live._load_automation_preflight",
                side_effect=FeishuAutomationLiveError("automation_disabled"),
            ) as preflight:
                code = run_feishu_automation_live_harness(
                    ["--confirm-live", "--output-dir", str(output)]
                )

            self.assertEqual(code, 2)
            preflight.assert_called_once()
            self.assertFalse(output.exists())

    def test_preflight_rejects_existing_due_automation_work(self) -> None:
        """Gate 不能在 Owner 已有即将执行的 Task 上启停 Gateway 制造干扰。"""
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "lobster0.db")
            apply_migrations(database)
            owner = OwnerRepository(database).get_or_create()
            now = datetime.now(UTC)
            ScheduledTaskRepository(database).create(
                owner_id=owner.id,
                name="existing due task",
                schedule=ScheduleSpec(ScheduleKind.ONCE, now.isoformat(), "UTC", now),
                prompt="existing work",
                skill_names=(),
                delivery=DeliveryTarget("none", "none"),
                policy_profile="automation-default",
                budget=TaskBudget(),
            )

            with self.assertRaises(FeishuAutomationLiveError) as raised:
                _assert_automation_state_clean(database.path, now=now)

            self.assertEqual(raised.exception.code, "automation_state_not_clean")

    def test_execution_owns_and_releases_managed_gateway(self) -> None:
        """即使没有 case，execution 也只能使用自己启动并最终停止的 Gateway。"""
        gateway = SimpleNamespace(
            ready=True,
            secret_match_count=0,
            provenance=None,
            stop=AsyncMock(return_value=0),
        )
        preflight = SimpleNamespace(cases=())
        with patch(
            "lobster0.evals.feishu_automation_live._start_automation_gateway",
            new=AsyncMock(return_value=gateway),
        ) as start:
            execution = asyncio.run(
                _execute_automation_live_cases(
                    preflight,
                    gateway_timeout=5,
                    case_timeout=5,
                    input_fn=lambda _: "p",
                    output_fn=lambda _: None,
                )
            )

        self.assertIsInstance(execution, AutomationLiveExecution)
        self.assertEqual(execution.results, ())
        self.assertTrue(execution.gateway_ready)
        self.assertTrue(execution.gateway_graceful_exit)
        start.assert_awaited_once()
        gateway.stop.assert_awaited_once()

    def test_confirmed_ten_passes_write_private_verified_evidence(self) -> None:
        """10/10、clean commit 与零 Secret 才能写 0600 VERIFIED Evidence。"""
        cases = load_feishu_automation_live_cases(PROJECT_ROOT / "evals" / "scenarios")
        results = tuple(
            AutomationLiveCaseResult(
                case.id,
                "pass",
                tuple(case.expected.automation_evidence),
                (),
                "pass",
                None,
            )
            for case in cases
        )
        execution = AutomationLiveExecution(results, True, True, 0)
        config = SimpleNamespace(
            channels=SimpleNamespace(
                feishu=SimpleNamespace(
                    account_id="default",
                    owner_open_id="owner-redacted",
                    allowed_open_ids=(),
                    allowed_chat_ids=(),
                )
            )
        )
        secrets = SimpleNamespace(
            model_api_key="model-secret",
            feishu_app_id="app-secret",
            channel_tokens={"feishu": "channel-secret"},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preflight = SimpleNamespace(
                paths=SimpleNamespace(home=root / "home", logs=root / "logs"),
                config=config,
                secrets=secrets,
                cases=cases,
                commit="b" * 40,
                conversation_id="conversation-redacted",
            )
            output = root / "evidence"
            with (
                patch(
                    "lobster0.evals.feishu_automation_live._load_automation_preflight",
                    return_value=preflight,
                ),
                patch(
                    "lobster0.evals.feishu_automation_live._execute_automation_live_cases",
                    new=AsyncMock(return_value=execution),
                ),
                patch(
                    "lobster0.evals.feishu_automation_live._repository_state",
                    return_value=("b" * 40, False),
                ),
                patch(
                    "lobster0.evals.feishu_automation_live.scan_secret_matches",
                    return_value=0,
                ),
            ):
                code = run_feishu_automation_live_harness(
                    ["--confirm-live", "--output-dir", str(output)]
                )
            files = tuple(output.glob("*.json"))

            self.assertEqual(code, 0)
            self.assertEqual(len(files), 1)
            report = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(report["release_status"], "FEISHU_AUTOMATION_VERIFIED")
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    @staticmethod
    def _config(
        *,
        feishu: bool = True,
        telegram: bool = False,
        discord: bool = False,
        mode: str = "safe",
        automation: bool = True,
        backend: str = "seatbelt",
        network: str = "none",
        model: str = "deepseek-v4-pro",
    ) -> SimpleNamespace:
        """构造静态 preflight 所需的最小 typed config 视图。"""
        return SimpleNamespace(
            channels=SimpleNamespace(
                feishu=SimpleNamespace(enabled=feishu),
                telegram=SimpleNamespace(enabled=telegram),
                discord=SimpleNamespace(enabled=discord),
            ),
            tools=SimpleNamespace(mode=mode),
            automation=SimpleNamespace(enabled=automation),
            sandbox=SimpleNamespace(backend=backend, network=network),
            agent=SimpleNamespace(model=model),
        )


if __name__ == "__main__":
    unittest.main()
