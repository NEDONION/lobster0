"""运行固定时钟、无网络的 Phase 6 Automation versioned regressions。"""

import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from lobster0.agent.runner import AgentRunBudget
from lobster0.agent.turn import TurnExecutionProfile, TurnResult
from lobster0.automation.continuation import TaskApprovalContinuation
from lobster0.automation.delivery import TaskDeliveryService
from lobster0.automation.guard import AutomationGuardError, AutomationPromptGuard
from lobster0.automation.heartbeat import HeartbeatReconciler
from lobster0.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduledTask,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
)
from lobster0.automation.repository import (
    AutomationControlRepository,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from lobster0.automation.runner import TaskRunner
from lobster0.automation.scheduler import Scheduler
from lobster0.bootstrap import initialize_state
from lobster0.checkpoints.rollback import RollbackConflictError, RollbackService
from lobster0.checkpoints.store import CheckpointError, CheckpointStore
from lobster0.config import HeartbeatConfig
from lobster0.evals.cases import EvalCase
from lobster0.paths import build_state_paths
from lobster0.policy.engine import PolicyAction, PolicyDecision
from lobster0.providers.base import ToolCall
from lobster0.sandbox.base import ExecutionPlan
from lobster0.sandbox.docker import DockerSandbox
from lobster0.skills.loader import SkillLoader
from lobster0.storage.channels import DeliveryRepository
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.tooling import ApprovalRepository, StoredApproval
from lobster0.tools.base import ToolContext


@dataclass(frozen=True, slots=True)
class AutomationFixtureOutcome:
    """保存 fixture 的公开状态、code、Tool、Delivery 和 evidence。"""

    status: str
    error_code: str | None = None
    tool_runs: tuple[str, ...] = ()
    delivery_count: int = 0
    evidence: tuple[str, ...] = ()
    violations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AutomationCaseResult:
    """保存单条 Automation case 的脱敏判定。"""

    case_id: str
    passed: bool
    duration_ms: int
    failures: tuple[str, ...]
    status: str
    error_code: str | None
    tool_runs: tuple[str, ...]
    delivery_count: int
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AutomationSuiteResult:
    """汇总一次 Automation v1 suite 的有限执行结果。"""

    total: int
    passed: int
    failed: int
    duration_ms: int
    cases: tuple[AutomationCaseResult, ...]


class _Harness:
    """为每条 case 创建独立真实 SQLite、Workspace 与固定时钟。"""

    def __init__(self, root: Path) -> None:
        """初始化 v5 schema 和 Automation repositories。"""
        self.paths = build_state_paths(root / "state")
        self.database = Database(self.paths.database)
        initialized = initialize_state(self.paths)
        self.owner_id = initialized.owner.id
        self.now = datetime(2026, 8, 9, 8, tzinfo=UTC)
        self.tasks = ScheduledTaskRepository(self.database, clock=lambda: self.now)
        self.runs = TaskRunRepository(self.database, clock=lambda: self.now)
        self.control = AutomationControlRepository(self.database, clock=lambda: self.now)

    def task(
        self,
        name: str,
        *,
        kind: ScheduleKind = ScheduleKind.INTERVAL,
        expression: str = "3600",
        next_run_at: datetime | None = None,
        prompt: str = "Read reports/status.json and summarize it.",
        delivery: DeliveryTarget | None = None,
        budget: TaskBudget | None = None,
    ) -> ScheduledTask:
        """创建一条有界、无 Secret 的真实 ScheduledTask。"""
        return self.tasks.create(
            owner_id=self.owner_id,
            name=name,
            schedule=ScheduleSpec(
                kind,
                expression,
                "UTC",
                self.now if next_run_at is None else next_run_at,
            ),
            prompt=prompt,
            skill_names=(),
            delivery=delivery or DeliveryTarget("none", "none"),
            policy_profile="automation-default",
            budget=budget or TaskBudget(timeout_seconds=30),
        )

    def turn(self, key: str = "automation-eval") -> tuple[int, int]:
        """创建 Approval/TaskRun 外键需要的 Session 与 running Turn。"""
        session = SessionRepository(self.database).get_or_create_cli(self.owner_id, key)
        turns = TurnRepository(self.database)
        turn = turns.create_with_user_message(session.id, f"{key}-event", "eval", "bounded")
        turns.mark_running(turn.id)
        return session.id, turn.id

    def approval(
        self,
        call_id: str,
        *,
        session_id: int,
        turn_id: int,
    ) -> StoredApproval:
        """创建绑定 write_file 的真实 pending Approval。"""
        return ApprovalRepository(self.database, clock=lambda: self.now).create_waiting(
            ToolContext(
                user_id=self.owner_id,
                session_id=session_id,
                turn_id=turn_id,
                state_home=self.paths.home,
                workspace=self.paths.workspace,
                read_only_roots=(),
                source="automation",
                task_run_id=1,
                allowed_tool_names=frozenset({"write_file", "complete_task"}),
                automation_gate=lambda: True,
            ),
            ToolCall(call_id, "write_file", {"path": "status.txt", "content": "ok"}),
            {"path": "status.txt", "content": "ok"},
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="write_file status.txt",
        )


class _StaticAutomationTurns:
    """向 TaskRunner 返回一条固定 TurnResult。"""

    def __init__(self, result: TurnResult) -> None:
        """保存无网络结果。"""
        self._result = result

    async def handle_automation(
        self,
        *,
        task_id: int,
        task_run_id: int,
        text: str,
        profile: TurnExecutionProfile,
    ) -> TurnResult:
        """验证不可扩大的 profile 后返回固定结果。"""
        del task_id, text
        if profile.task_run_id != task_run_id or "manage_task" in (
            profile.allowed_tool_names or ()
        ):
            raise ValueError("automation profile mismatch")
        return self._result


async def run_automation_case(case: EvalCase) -> AutomationCaseResult:
    """在独立临时状态中执行并验证一条 Automation fixture。"""
    started = time.monotonic()
    failures: tuple[str, ...]
    outcome = AutomationFixtureOutcome("failed", "execution_error")
    try:
        if case.automation_fixture is None:
            raise ValueError("automation fixture is missing")
        with TemporaryDirectory(prefix="lobster0-automation-eval-") as directory:
            harness = _Harness(Path(directory).resolve())
            outcome = await _run_fixture(case.automation_fixture, harness)
        failures = _verify(case, outcome)
    except Exception:  # noqa: BLE001 - eval 只暴露稳定 execution_error
        failures = ("execution_error",)
    return AutomationCaseResult(
        case_id=case.id,
        passed=not failures,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        failures=failures,
        status=outcome.status,
        error_code=outcome.error_code,
        tool_runs=outcome.tool_runs,
        delivery_count=outcome.delivery_count,
        evidence=outcome.evidence,
    )


async def run_automation_suite(cases: tuple[EvalCase, ...]) -> AutomationSuiteResult:
    """顺序运行 Automation suite，保持资源和结果顺序确定。"""
    started = time.monotonic()
    results = tuple([await run_automation_case(case) for case in cases])
    passed = sum(result.passed for result in results)
    return AutomationSuiteResult(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        cases=results,
    )


async def _run_fixture(name: str, harness: _Harness) -> AutomationFixtureOutcome:
    """路由封闭 fixture name；数据文件不能选择任意函数。"""
    fixtures = {
        "scheduler_idempotency": _scheduler_idempotency,
        "bounded_misfire": _bounded_misfire,
        "durable_estop": _durable_estop,
        "secret_prompt_guard": _secret_prompt_guard,
        "recursive_prompt_guard": _recursive_prompt_guard,
        "immutable_run_snapshot": _immutable_run_snapshot,
        "terminal_and_recovery": _terminal_and_recovery,
        "waiting_approval": _waiting_approval,
        "approval_continuation": _approval_continuation,
        "delivery_idempotency": _delivery_idempotency,
        "execution_plan_binding": _execution_plan_binding,
        "docker_hardening": _docker_hardening,
        "checkpoint_quota": _checkpoint_quota,
        "rollback_conflict": _rollback_conflict,
        "heartbeat_reconcile": _heartbeat_reconcile,
    }
    fixture = fixtures.get(name)
    if fixture is None:
        raise ValueError("unknown automation fixture")
    return await fixture(harness)


async def _scheduler_idempotency(harness: _Harness) -> AutomationFixtureOutcome:
    """重复 tick 只能入队同一 slot 一次。"""
    task = harness.task("same-slot")
    scheduler = _scheduler(harness)
    first = await scheduler.tick(harness.now)
    second = await scheduler.tick(harness.now)
    if (first.enqueued, second.enqueued, len(harness.runs.list(task_id=task.id))) != (1, 0, 1):
        raise AssertionError("slot idempotency failed")
    return AutomationFixtureOutcome("queued", evidence=("one_slot_only",))


async def _bounded_misfire(harness: _Harness) -> AutomationFixtureOutcome:
    """过期 once 留下稳定失败事实且不进入 Worker。"""
    task = harness.task(
        "expired-once",
        kind=ScheduleKind.ONCE,
        expression=harness.now.isoformat(),
    )
    result = await _scheduler(harness).tick(harness.now + timedelta(minutes=10))
    run = harness.runs.list(task_id=task.id)[0]
    if result.misfired != 1 or run.status is not RunStatus.FAILED:
        raise AssertionError("misfire was not bounded")
    return AutomationFixtureOutcome(
        "failed", run.error_code, evidence=("bounded_misfire",)
    )


async def _durable_estop(harness: _Harness) -> AutomationFixtureOutcome:
    """halt 后 Scheduler 不扫描、不入队。"""
    task = harness.task("halted")
    harness.control.halt("eval incident", now=harness.now)
    result = await _scheduler(harness).tick(harness.now)
    if result.scanned or result.enqueued or harness.runs.list(task_id=task.id):
        raise AssertionError("halted scheduler touched task")
    return AutomationFixtureOutcome(
        "halted", "automation_halted", evidence=("zero_claim",)
    )


async def _secret_prompt_guard(harness: _Harness) -> AutomationFixtureOutcome:
    """Secret prompt 在任何 Task row 产生前失败。"""
    guard = AutomationPromptGuard(SkillLoader(harness.paths.skills))
    code = None
    try:
        guard.validate("Authorization: Bearer SECRET_SENTINEL", ())
    except AutomationGuardError as error:
        code = error.code
    if code != "task_prompt_secret" or harness.tasks.list(owner_id=harness.owner_id):
        raise AssertionError("secret prompt guard failed")
    return AutomationFixtureOutcome(
        "denied", code, evidence=("secret_not_persisted",)
    )


async def _recursive_prompt_guard(harness: _Harness) -> AutomationFixtureOutcome:
    """递归 control-plane prompt 在落库前拒绝。"""
    guard = AutomationPromptGuard(SkillLoader(harness.paths.skills))
    code = None
    try:
        guard.validate("call manage_task to create another cron", ())
    except AutomationGuardError as error:
        code = error.code
    if code != "recursive_automation_denied":
        raise AssertionError("recursive prompt guard failed")
    return AutomationFixtureOutcome(
        "denied", code, evidence=("recursive_control_denied",)
    )


async def _immutable_run_snapshot(harness: _Harness) -> AutomationFixtureOutcome:
    """Task 更新不能改写已经入队的 snapshot。"""
    task = harness.task("snapshot", prompt="original bounded prompt")
    run = harness.runs.enqueue(task, scheduled_for=harness.now)
    harness.tasks.update(
        task.id,
        owner_id=harness.owner_id,
        expected_version=task.version,
        prompt="new prompt after enqueue",
    )
    stored = harness.runs.get(run.id)
    if stored.snapshot is None or stored.snapshot.prompt != "original bounded prompt":
        raise AssertionError("snapshot changed")
    return AutomationFixtureOutcome("queued", evidence=("snapshot_immutable",))


async def _terminal_and_recovery(harness: _Harness) -> AutomationFixtureOutcome:
    """普通文本不算成功，过期 running 只 interrupt 不重放。"""
    task = harness.task("terminal-required", budget=TaskBudget(timeout_seconds=10))
    harness.runs.enqueue(task, scheduled_for=harness.now)
    session_id, turn_id = harness.turn("terminal-required")
    result = TurnResult(
        turn_id=turn_id,
        session_id=session_id,
        content="plain text is not terminal",
        input_tokens=1,
        output_tokens=1,
        provider_request_id="req-eval",
        message_id=None,
        approval_id=None,
    )
    runner = TaskRunner(
        harness.runs,
        harness.control,
        _StaticAutomationTurns(result),
        allowed_tool_names=frozenset({"read_file"}),
        lease_seconds=10,
    )
    attempt = await runner.run_once("eval-worker", harness.now)
    assert attempt is not None
    second = harness.task("stale-running", next_run_at=harness.now + timedelta(minutes=1))
    harness.runs.enqueue(
        second,
        scheduled_for=harness.now + timedelta(minutes=1),
        idempotency_key="stale-running",
    )
    claimed = harness.runs.claim_next(
        "stale-worker",
        now=harness.now + timedelta(minutes=1),
        lease_seconds=10,
    )
    assert claimed is not None
    harness.runs.mark_running(
        claimed.id,
        "stale-worker",
        now=harness.now + timedelta(minutes=1),
    )
    recovered = harness.runs.recover_stale(
        now=harness.now + timedelta(minutes=1, seconds=11)
    )
    if attempt.error_code != "automation_terminal_response_missing" or recovered.interrupted != 1:
        raise AssertionError("terminal/recovery contract failed")
    return AutomationFixtureOutcome(
        "failed",
        attempt.error_code,
        evidence=("terminal_required", "stale_run_interrupted"),
    )


async def _waiting_approval(harness: _Harness) -> AutomationFixtureOutcome:
    """Approval 使 Run 释放 lease 并保存精确 binding。"""
    task = harness.task("waiting-approval")
    run = harness.runs.enqueue(task, scheduled_for=harness.now)
    session_id, turn_id = harness.turn("waiting-approval")
    approval = harness.approval("call-wait", session_id=session_id, turn_id=turn_id)
    result = TurnResult(
        turn_id=turn_id,
        session_id=session_id,
        content="",
        input_tokens=1,
        output_tokens=1,
        provider_request_id="req-wait",
        message_id=None,
        approval_id=approval.id,
    )
    attempt = await TaskRunner(
        harness.runs,
        harness.control,
        _StaticAutomationTurns(result),
        allowed_tool_names=frozenset({"write_file"}),
        lease_seconds=10,
    ).run_once("eval-worker", harness.now)
    assert attempt is not None
    stored = harness.runs.get(run.id)
    if stored.worker_id is not None or stored.approval_id != approval.id:
        raise AssertionError("waiting approval binding failed")
    return AutomationFixtureOutcome(
        "waiting_approval",
        "approval_required",
        tool_runs=("write_file",),
        evidence=("lease_released", "approval_id_bound"),
    )


async def _approval_continuation(harness: _Harness) -> AutomationFixtureOutcome:
    """Continuation 用原 budget 从 waiting 精确结算 terminal response。"""
    task = harness.task("approval-resume", budget=TaskBudget(timeout_seconds=37))
    run = harness.runs.enqueue(task, scheduled_for=harness.now)
    claimed = harness.runs.claim_next("worker", now=harness.now, lease_seconds=60)
    assert claimed is not None
    harness.runs.mark_running(claimed.id, "worker", now=harness.now)
    session_id, turn_id = harness.turn("approval-resume")
    approval = harness.approval("call-resume", session_id=session_id, turn_id=turn_id)
    harness.runs.mark_waiting(
        run.id,
        "worker",
        session_id=session_id,
        turn_id=turn_id,
        approval_id=approval.id,
    )
    profile = TurnExecutionProfile(
        source="automation",
        task_run_id=run.id,
        allowed_tool_names=frozenset({"write_file", "complete_task"}),
        budget=AgentRunBudget(max_turns=3, max_tool_calls=2, timeout_seconds=37),
        automation_gate=lambda: True,
    )
    coordinator = TaskApprovalContinuation(harness.runs, clock=lambda: harness.now)
    coordinator.begin(profile, approval.id)
    response = TaskResponse(False, "")
    coordinator.settle(
        profile,
        approval.id,
        TurnResult(
            turn_id=turn_id,
            session_id=session_id,
            content="",
            input_tokens=2,
            output_tokens=1,
            provider_request_id="req-resume",
            message_id=None,
            approval_id=None,
            terminal_response=response,
        ),
    )
    stored = harness.runs.get(run.id)
    if stored.status is not RunStatus.SUCCEEDED or profile.budget.timeout_seconds != 37:
        raise AssertionError("approval continuation failed")
    return AutomationFixtureOutcome(
        "succeeded",
        tool_runs=("write_file", "complete_task"),
        evidence=("continuation_terminal", "original_budget_preserved"),
    )


async def _delivery_idempotency(harness: _Harness) -> AutomationFixtureOutcome:
    """重复 project 复用同一 Outbox row 与目的地。"""
    task = harness.task(
        "delivery",
        delivery=DeliveryTarget("owner", "feishu", "default", "oc_eval"),
    )
    harness.runs.enqueue(task, scheduled_for=harness.now)
    claimed = harness.runs.claim_next("delivery-worker", now=harness.now, lease_seconds=30)
    assert claimed is not None
    harness.runs.mark_running(claimed.id, "delivery-worker", now=harness.now)
    response = TaskResponse(True, "done")
    run = harness.runs.finish(
        claimed.id,
        status=RunStatus.SUCCEEDED,
        now=harness.now,
        worker_id="delivery-worker",
        response=response,
    )
    deliveries = DeliveryRepository(harness.database, clock=lambda: harness.now)
    service = TaskDeliveryService(
        deliveries,
        harness.runs,
        channel_max_chars={"feishu": 100, "telegram": 100, "discord": 100},
    )
    first = service.project(run, response)
    second = service.project(run, response)
    if len(first) != 1 or [item.id for item in first] != [item.id for item in second]:
        raise AssertionError("delivery was duplicated")
    return AutomationFixtureOutcome(
        "succeeded",
        tool_runs=("complete_task",),
        delivery_count=1,
        evidence=("delivery_once", "destination_immutable"),
    )


async def _execution_plan_binding(harness: _Harness) -> AutomationFixtureOutcome:
    """canonical Plan hash 对环境顺序稳定，对 argv 修改敏感。"""
    first = _plan(harness, backend="host", environment_names=("PATH", "LANG"))
    reordered = _plan(harness, backend="host", environment_names=("LANG", "PATH"))
    changed = ExecutionPlan(
        argv=(sys.executable, "changed.py"),
        cwd=first.cwd,
        environment_names=first.environment_names,
        read_roots=first.read_roots,
        write_roots=first.write_roots,
        timeout_seconds=first.timeout_seconds,
        memory_mib=first.memory_mib,
        cpu_seconds=first.cpu_seconds,
        pids_limit=first.pids_limit,
        network_mode=first.network_mode,
        backend=first.backend,
    )
    if first.sha256 != reordered.sha256 or first.sha256 == changed.sha256:
        raise AssertionError("plan hash binding failed")
    return AutomationFixtureOutcome(
        "allowed",
        tool_runs=("run_command",),
        evidence=("plan_hash_bound", "exact_argv"),
    )


async def _docker_hardening(harness: _Harness) -> AutomationFixtureOutcome:
    """只编译 Docker argv，验证固定 hardening flags，不连接 daemon。"""
    plan = _plan(harness, backend="docker", environment_names=("PATH",))
    argv = DockerSandbox(
        image="example/lobster0@sha256:" + "a" * 64,
        docker_executable="/usr/bin/docker",
    ).build_argv(plan)
    required = {"--network", "none", "--read-only", "--cap-drop", "ALL"}
    if not required.issubset(argv) or "--privileged" in argv or plan.argv != argv[-2:]:
        raise AssertionError("docker hardening flags missing")
    return AutomationFixtureOutcome(
        "allowed",
        tool_runs=("run_command",),
        evidence=("network_none", "read_only_rootfs", "exact_argv"),
    )


async def _checkpoint_quota(harness: _Harness) -> AutomationFixtureOutcome:
    """超额 capture 返回稳定码且原文件保持不变。"""
    target = harness.paths.workspace / "large.txt"
    target.write_bytes(b"x" * 17)
    store = CheckpointStore(
        harness.database,
        owner_id=harness.owner_id,
        workspace=harness.paths.workspace,
        state_home=harness.paths.home,
        max_entries=4,
        max_total_bytes=32,
        max_file_bytes=16,
        max_count=3,
    )
    code = None
    try:
        store.capture((target,), reason="write_file", now=harness.now)
    except CheckpointError as error:
        code = error.code
    if code != "checkpoint_budget_exceeded" or target.read_bytes() != b"x" * 17:
        raise AssertionError("checkpoint quota failed open")
    return AutomationFixtureOutcome(
        "denied",
        code,
        tool_runs=("write_file",),
        evidence=("quota_fail_closed", "no_side_effect"),
    )


async def _rollback_conflict(harness: _Harness) -> AutomationFixtureOutcome:
    """preview 后并发编辑必须保留并拒绝旧 hash apply。"""
    target = harness.paths.workspace / "note.txt"
    target.write_text("before", encoding="utf-8")
    store = CheckpointStore(
        harness.database,
        owner_id=harness.owner_id,
        workspace=harness.paths.workspace,
        state_home=harness.paths.home,
        max_entries=4,
        max_total_bytes=128,
        max_file_bytes=64,
        max_count=3,
    )
    checkpoint = store.capture((target,), reason="write_file", now=harness.now)
    target.write_text("after", encoding="utf-8")
    rollback = RollbackService(store)
    preview = rollback.preview(checkpoint.id)
    target.write_text("concurrent", encoding="utf-8")
    code = None
    try:
        rollback.apply(checkpoint.id, preview.sha256)
    except RollbackConflictError as error:
        code = error.code
    if code != "rollback_conflict" or target.read_text(encoding="utf-8") != "concurrent":
        raise AssertionError("rollback conflict failed")
    return AutomationFixtureOutcome(
        "denied",
        code,
        evidence=("preview_hash_bound", "concurrent_edit_preserved"),
    )


async def _heartbeat_reconcile(harness: _Harness) -> AutomationFixtureOutcome:
    """重复 reconcile 只保留唯一 system Task 和一个到期 slot。"""
    reconciler = HeartbeatReconciler(
        HeartbeatConfig(
            enabled=True,
            interval_seconds=1800,
            timezone="Asia/Shanghai",
            active_hours_start="08:00",
            active_hours_end="23:00",
        ),
        owner_id=harness.owner_id,
        tasks=harness.tasks,
        runs=harness.runs,
        max_concurrent_runs=2,
        delivery=DeliveryTarget("none", "none"),
    )
    first = reconciler.reconcile(harness.now)
    second = reconciler.reconcile(harness.now)
    if (
        first.task_id != second.task_id
        or (first.enqueued, second.enqueued) != (1, 0)
        or harness.tasks.count_system_owned("heartbeat") != 1
        or first.next_run_at is None
        or first.next_run_at <= harness.now
    ):
        raise AssertionError("heartbeat reconcile failed")
    return AutomationFixtureOutcome(
        "queued", evidence=("one_system_task", "active_hours_bounded")
    )


def _scheduler(harness: _Harness) -> Scheduler:
    """构造固定预算、固定时钟的真实 Scheduler。"""
    return Scheduler(
        harness.tasks,
        harness.runs,
        harness.control,
        max_active_tasks=50,
        misfire_grace_seconds=300,
        clock=lambda: harness.now,
    )


def _plan(
    harness: _Harness,
    *,
    backend: str,
    environment_names: tuple[str, ...],
) -> ExecutionPlan:
    """构造无网络、精确 argv 的 canonical eval Plan。"""
    return ExecutionPlan(
        argv=(sys.executable, "status.py"),
        cwd=harness.paths.workspace,
        environment_names=environment_names,
        read_roots=(),
        write_roots=(harness.paths.workspace,),
        timeout_seconds=30,
        memory_mib=256,
        cpu_seconds=10,
        pids_limit=32,
        network_mode="none",
        backend=backend,
    )


def _verify(case: EvalCase, outcome: AutomationFixtureOutcome) -> tuple[str, ...]:
    """严格比较数据集声明，不在运行时改写 expected。"""
    expected = case.expected
    failures: list[str] = []
    if outcome.status != expected.automation_status:
        failures.append("status_mismatch")
    if outcome.error_code != expected.error_code:
        failures.append("error_code_mismatch")
    if outcome.tool_runs != expected.tool_runs:
        failures.append("tool_set_mismatch")
    if outcome.delivery_count != expected.delivery_count:
        failures.append("delivery_count_mismatch")
    if outcome.evidence != expected.automation_evidence:
        failures.append("evidence_mismatch")
    if set(outcome.violations) & set(expected.forbidden_automation):
        failures.append("forbidden_behavior")
    return tuple(failures)
