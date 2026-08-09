"""Phase 6 飞书 Automation Live 的 durable evaluator 与确认式 runner。"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from miniclaw.automation.delivery import TaskDeliveryService
from miniclaw.automation.models import (
    DeliveryTarget,
    RunStatus,
    ScheduledTask,
    ScheduleKind,
    ScheduleSpec,
    TaskBudget,
    TaskResponse,
    TaskStatus,
)
from miniclaw.automation.repository import (
    AutomationControlRepository,
    ScheduledTaskRepository,
    TaskRunRepository,
)
from miniclaw.channels.supervisor import GatewaySecrets
from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.doctor import CheckResult, CheckStatus, run_local_checks
from miniclaw.env import DotEnvError
from miniclaw.evals.cases import (
    EvalCase,
    EvalCaseError,
    load_feishu_automation_live_cases,
)
from miniclaw.evals.feishu_live import (
    _load_live_environment,
    _pending_approval_count,
    _repository_state,
)
from miniclaw.evals.gateway_process import ManagedGateway, ManagedGatewayError
from miniclaw.evals.production_evidence import (
    ProductionEvidenceError,
    scan_secret_matches,
    utc_timestamp,
    validate_commit,
    write_private_json,
)
from miniclaw.gateway import GatewayConfigError, validate_gateway_environment
from miniclaw.gateway_lease import GatewayLease, GatewayLeaseError
from miniclaw.paths import (
    PathConfigurationError,
    StatePaths,
)
from miniclaw.storage.channels import DeliveryRepository
from miniclaw.storage.database import Database, DatabaseError
from miniclaw.storage.tooling import ApprovalRepository

_CASE_STATUSES = frozenset({"pass", "fail", "skip"})
_EVIDENCE_KEYS = frozenset(
    {
        "approval_id_bound",
        "approval_delivery_once",
        "budget_stopped",
        "continuation_terminal",
        "delivery_once",
        "gateway_restart_recovered",
        "idempotency_key_reused",
        "lease_released",
        "no_side_effect",
        "one_slot_only",
        "original_budget_preserved",
        "provider_request_observed",
        "stale_run_interrupted",
        "structured_silence",
        "task_identity_preserved",
        "two_slots_once",
        "zero_claim",
    }
)
_EXPECTED_FIXTURES = frozenset(
    {
        "live_approval_continuation",
        "live_budget_stop",
        "live_delivery_unknown_recovery",
        "live_durable_estop",
        "live_gateway_restart",
        "live_interrupted_recovery",
        "live_interval_two_slots",
        "live_one_shot_delivery",
        "live_structured_silence",
        "live_waiting_approval",
    }
)


class FeishuAutomationLiveError(RuntimeError):
    """表示 Automation Live runner 只公开稳定错误码。"""

    def __init__(self, code: str) -> None:
        """保存不含路径、正文、平台 ID 或 Secret 的错误码。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class AutomationLivePreflight:
    """保存确认后通过静态门禁的运行输入；外部会话 ID 不参与 repr。"""

    project_root: Path
    paths: StatePaths
    config: AppConfig
    secrets: GatewaySecrets = field(repr=False)
    cases: tuple[EvalCase, ...]
    commit: str
    owner_id: int
    conversation_id: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AutomationLiveExecution:
    """保存 10-case 结果与受管 Gateway 的封闭生命周期结论。"""

    results: tuple[AutomationLiveCaseResult, ...]
    gateway_ready: bool
    gateway_graceful_exit: bool
    gateway_secret_matches: int


def _validate_automation_preflight_state(
    *,
    config: AppConfig,
    checks: Sequence[CheckResult],
    pending_approvals: int,
    commit: str,
    dirty: bool,
    cases: Sequence[object],
    detached: bool = False,
) -> None:
    """验证 Automation Live 所需的单飞书、Seatbelt 与 clean durable 起点。"""
    channels = config.channels
    if not channels.feishu.enabled:
        raise FeishuAutomationLiveError("feishu_channel_disabled")
    if channels.telegram.enabled or channels.discord.enabled:
        raise FeishuAutomationLiveError("peer_channel_enabled")
    if config.tools.mode != "safe":
        raise FeishuAutomationLiveError("unsafe_permission_mode")
    if not config.automation.enabled:
        raise FeishuAutomationLiveError("automation_disabled")
    if config.sandbox.backend != "seatbelt":
        raise FeishuAutomationLiveError("seatbelt_required")
    if config.sandbox.network != "none":
        raise FeishuAutomationLiveError("sandbox_network_unsafe")
    if "deepseek" not in config.agent.model.lower():
        raise FeishuAutomationLiveError("deepseek_provider_required")
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise FeishuAutomationLiveError("repository_commit_unavailable")
    if dirty:
        raise FeishuAutomationLiveError("repository_dirty")
    if detached:
        raise FeishuAutomationLiveError("repository_detached")
    if any(check.status is CheckStatus.FAIL for check in checks):
        raise FeishuAutomationLiveError("doctor_preflight_failed")
    if type(pending_approvals) is not int or pending_approvals < 0:
        raise FeishuAutomationLiveError("approval_state_unavailable")
    if pending_approvals:
        raise FeishuAutomationLiveError("pending_approval_exists")
    if len(cases) != 10:
        raise FeishuAutomationLiveError("automation_live_case_count_invalid")


def _load_automation_preflight(
    *,
    project_root: Path,
    home: str | None,
    root: Path,
) -> AutomationLivePreflight:
    """加载配置并在建网前验证 clean commit、单飞书和 Owner DM 路由。"""
    try:
        environment, paths = _load_live_environment(project_root=project_root, home=home)
        config = load_config(paths, environment)
        cases = load_feishu_automation_live_cases(root)
        checks = run_local_checks(paths, environment)
        commit, dirty = _repository_state(project_root)
        _validate_automation_preflight_state(
            config=config,
            checks=checks,
            pending_approvals=_pending_approval_count(paths.database),
            commit=commit,
            dirty=dirty,
            cases=cases,
            detached=_repository_detached(project_root),
        )
        _assert_automation_state_clean(paths.database, now=datetime.now(UTC))
        owner_id, conversation_id = _owner_dm_route(
            paths.database,
            account_id=config.channels.feishu.account_id,
            owner_external_id=config.channels.feishu.owner_open_id,
        )
        lease = GatewayLease.acquire(paths.run / "gateway.lock", commit=commit)
        lease.close()
        secrets = validate_gateway_environment(config, environment)
        return AutomationLivePreflight(
            project_root,
            paths,
            config,
            secrets,
            cases,
            commit,
            owner_id,
            conversation_id,
        )
    except FeishuAutomationLiveError:
        raise
    except GatewayLeaseError as error:
        raise FeishuAutomationLiveError(error.code) from None
    except (
        ConfigError,
        DatabaseError,
        DotEnvError,
        EvalCaseError,
        GatewayConfigError,
        OSError,
        PathConfigurationError,
        sqlite3.Error,
        ValueError,
    ):
        raise FeishuAutomationLiveError("automation_live_preflight_failed") from None


def _assert_automation_state_clean(database: Path, *, now: datetime) -> None:
    """拒绝 active Run/Delivery 或 20 分钟内到期的既有 Task，避免干扰 Owner 工作。"""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise FeishuAutomationLiveError("automation_state_unavailable")
    horizon = now.astimezone(UTC) + timedelta(minutes=20)
    with Database(database).connect_read_only() as connection:
        row = connection.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM task_runs
               WHERE status IN ('queued', 'claimed', 'running', 'waiting_approval'))
              +
              (SELECT COUNT(*) FROM deliveries
               WHERE status IN ('queued', 'sending', 'retry_wait', 'unknown'))
              +
              (SELECT COUNT(*) FROM scheduled_tasks
               WHERE status = 'active' AND next_run_at IS NOT NULL AND next_run_at <= ?)
            """,
            (horizon.isoformat(),),
        ).fetchone()
    if row is None:
        raise FeishuAutomationLiveError("automation_state_unavailable")
    if int(row[0]) != 0:
        raise FeishuAutomationLiveError("automation_state_not_clean")


def _repository_detached(project_root: Path) -> bool:
    """判断当前 clean worktree 是否处于 detached HEAD；命令失败按 detached 处理。"""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return True
    return result.returncode != 0 or not result.stdout.strip()


def _owner_dm_route(
    database: Path,
    *,
    account_id: str,
    owner_external_id: str,
) -> tuple[int, str]:
    """从 durable Inbox 读取当前 Owner 最近一条飞书私聊路由，不返回正文。"""
    with Database(database).connect_read_only() as connection:
        owner = connection.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchone()
        route = connection.execute(
            """
            SELECT external_conversation_id FROM processed_events
            WHERE channel = 'feishu' AND account_id = ?
              AND external_user_id = ? AND chat_type = 'p2p'
              AND status = 'completed'
            ORDER BY rowid DESC LIMIT 1
            """,
            (account_id, owner_external_id),
        ).fetchone()
    if owner is None or route is None or not str(route[0]).strip():
        raise FeishuAutomationLiveError("owner_dm_route_unavailable")
    return int(owner[0]), str(route[0])


async def _start_automation_gateway(
    preflight: AutomationLivePreflight,
    timeout: float,
) -> ManagedGateway:
    """启动绑定当前 clean commit 的唯一飞书 Gateway，并等待精确 ready marker。"""
    try:
        return await ManagedGateway.start(
            project_root=preflight.project_root,
            home=preflight.paths.home,
            ready_line=(
                "MiniClaw gateway ready: "
                f"feishu/{preflight.config.channels.feishu.account_id}"
            ),
            commit=preflight.commit,
            ready_timeout=timeout,
            secret_values=_automation_sensitive_values(preflight),
        )
    except ManagedGatewayError as error:
        raise FeishuAutomationLiveError(error.code) from None


async def _execute_automation_live_cases(
    preflight: AutomationLivePreflight,
    *,
    gateway_timeout: float,
    case_timeout: float,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> AutomationLiveExecution:
    """拥有唯一受管 Gateway，按版本顺序执行 case，并在所有路径有界停止。"""
    gateway = await _start_automation_gateway(preflight, gateway_timeout)
    results: list[AutomationLiveCaseResult] = []
    all_graceful = True
    secret_matches = 0
    gateway_active = True

    async def stop_gateway() -> None:
        """停止当前 Gateway 并累计其流式 Secret 匿名计数。"""
        nonlocal gateway_active, all_graceful, secret_matches
        if not gateway_active:
            return
        try:
            exit_code = await gateway.stop()
        except ManagedGatewayError as error:
            raise FeishuAutomationLiveError(error.code) from None
        all_graceful = all_graceful and exit_code == 0
        secret_matches += gateway.secret_match_count
        gateway_active = False

    async def restart_gateway() -> ManagedGateway:
        """停止并从同一 commit/state 启动新的受管 Gateway。"""
        nonlocal gateway, gateway_active
        await stop_gateway()
        gateway = await _start_automation_gateway(preflight, gateway_timeout)
        gateway_active = True
        return gateway

    async def start_gateway() -> ManagedGateway:
        """启动新 Gateway，并立刻把 finally 的所有权切换到该实例。"""
        nonlocal gateway, gateway_active
        gateway = await _start_automation_gateway(preflight, gateway_timeout)
        gateway_active = True
        return gateway

    try:
        output_fn("MiniClaw Feishu Automation Live")
        for case in preflight.cases:
            if case.id != "FEISHU-AUTO-006":
                await stop_gateway()
            result, gateway = await _run_automation_case(
                preflight,
                case,
                gateway=(gateway if case.id == "FEISHU-AUTO-006" else None),
                start_gateway=start_gateway,
                restart_gateway=restart_gateway,
                timeout=case_timeout,
                input_fn=input_fn,
                output_fn=output_fn,
            )
            results.append(result)
    finally:
        if gateway_active:
            try:
                await stop_gateway()
            except FeishuAutomationLiveError:
                raise
    return AutomationLiveExecution(
        tuple(results),
        True,
        all_graceful,
        secret_matches,
    )


def _automation_sensitive_values(
    preflight: AutomationLivePreflight,
) -> tuple[str, ...]:
    """只在内存汇总 Secret、外部路由、正文和本机路径供 exact scan。"""
    feishu = preflight.config.channels.feishu
    candidates: list[object] = [
        preflight.secrets.model_api_key,
        preflight.secrets.feishu_app_id,
        *preflight.secrets.channel_tokens.values(),
        feishu.owner_open_id,
        *feishu.allowed_open_ids,
        *feishu.allowed_chat_ids,
        preflight.conversation_id,
        str(preflight.paths.home),
        str(Path.home()),
    ]
    for case in preflight.cases:
        candidates.extend((case.query, *case.turns))
    return tuple(
        dict.fromkeys(
            value
            for value in candidates
            if isinstance(value, str) and len(value.encode("utf-8")) >= 4
        )
    )


async def _run_automation_case(
    preflight: AutomationLivePreflight,
    case: EvalCase,
    *,
    gateway: ManagedGateway | None,
    start_gateway: Callable[[], Awaitable[ManagedGateway]],
    restart_gateway: Callable[[], Awaitable[ManagedGateway]],
    timeout: float,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> tuple[AutomationLiveCaseResult, ManagedGateway]:
    """编排一个固定 case，真实发送仅由 Gateway 的 durable Outbox 完成。"""
    output_fn(f"\n{case.id}: {case.title}")
    fixture = case.automation_fixture or ""
    if fixture == "live_approval_continuation":
        if gateway is None:
            raise FeishuAutomationLiveError("approval_gateway_unavailable")
        task_id = _single_waiting_task(preflight.paths.database)
        checkpoint = capture_automation_checkpoint(
            preflight.paths.database,
            task_ids=(task_id,),
        )
        output_fn("在刚收到的飞书审批卡中选择“仅本次”，完成后按 Enter。")
        if await asyncio.to_thread(_read_live_action, input_fn) == "skip":
            return _skipped_case(case), gateway
        result = await _wait_for_automation_case(
            preflight.paths.database,
            checkpoint,
            case,
            timeout=timeout,
        )
        return await _with_human_status(result, input_fn), gateway

    now = datetime.now(UTC)
    task = _create_automation_live_task(preflight, case, now=now)
    checkpoint = capture_automation_checkpoint(
        preflight.paths.database,
        task_ids=(task.id,),
    )
    control = AutomationControlRepository(Database(preflight.paths.database))
    try:
        if fixture == "live_interrupted_recovery":
            _inject_running_lease(preflight.paths.database, task)
            await asyncio.sleep(10.1)
            gateway = await start_gateway()
        elif fixture == "live_durable_estop":
            control.halt("phase6-production-live", now=datetime.now(UTC))
            gateway = await start_gateway()
            await asyncio.sleep(min(3.0, timeout))
        elif fixture == "live_delivery_unknown_recovery":
            _inject_unknown_delivery(preflight, task)
            gateway = await start_gateway()
        else:
            gateway = await start_gateway()
            if fixture == "live_gateway_restart":
                gateway = await restart_gateway()

        result = await _wait_for_automation_case(
            preflight.paths.database,
            checkpoint,
            case,
            timeout=timeout,
        )
    finally:
        if fixture == "live_durable_estop":
            _cancel_task_if_active(preflight.paths.database, task, preflight.owner_id)
            control.unhalt(now=datetime.now(UTC))
        elif fixture in {
            "live_interval_two_slots",
            "live_interrupted_recovery",
            "live_delivery_unknown_recovery",
        }:
            _cancel_task_if_active(preflight.paths.database, task, preflight.owner_id)
    assert gateway is not None
    return await _with_human_status(result, input_fn), gateway


def _create_automation_live_task(
    preflight: AutomationLivePreflight,
    case: EvalCase,
    *,
    now: datetime,
) -> ScheduledTask:
    """为固定 fixture 创建一条冻结飞书路由、Prompt 和预算的真实 Task。"""
    fixture = case.automation_fixture or ""
    interval = fixture == "live_interval_two_slots"
    delayed = fixture == "live_gateway_restart"
    injected = fixture in {"live_interrupted_recovery", "live_delivery_unknown_recovery"}
    next_run = (
        now + timedelta(hours=1)
        if injected
        else now + timedelta(seconds=10 if delayed else 0)
    )
    budget = TaskBudget(
        timeout_seconds=180,
        max_turns=8,
        max_tool_calls=1 if fixture == "live_budget_stop" else 6,
        max_input_tokens=64_000,
        max_output_tokens=4_000,
    )
    return ScheduledTaskRepository(Database(preflight.paths.database)).create(
        owner_id=preflight.owner_id,
        name=f"phase6-live-{case.id}",
        schedule=ScheduleSpec(
            ScheduleKind.INTERVAL if interval else ScheduleKind.ONCE,
            "60" if interval else next_run.isoformat(),
            "UTC",
            next_run,
        ),
        prompt=_automation_prompt(fixture, now),
        skill_names=(),
        delivery=DeliveryTarget(
            "owner",
            "feishu",
            preflight.config.channels.feishu.account_id,
            preflight.conversation_id,
        ),
        policy_profile="automation-default",
        budget=budget,
    )


def _automation_prompt(fixture: str, now: datetime) -> str:
    """返回只允许固定 Tool 路径的短 Prompt；未知 fixture 失败关闭。"""
    prompts = {
        "live_one_shot_delivery": (
            "Call complete_task exactly once with notify=true and text "
            "'Phase 6 one-shot delivery verified'."
        ),
        "live_interval_two_slots": (
            "Call system_info once, then call complete_task with notify=true and text "
            "'Phase 6 interval slot verified'."
        ),
        "live_gateway_restart": (
            "Call complete_task exactly once with notify=true and text "
            "'Phase 6 restart recovery verified'."
        ),
        "live_interrupted_recovery": "Do not execute; recovery fixture.",
        "live_waiting_approval": (
            "Call write_file with path 'phase6-live/approval-"
            f"{now.strftime('%Y%m%d%H%M%S')}.txt' and content 'approved'. "
            "After it succeeds, call complete_task with notify=true and text "
            "'Phase 6 approval continuation verified'."
        ),
        "live_structured_silence": (
            "Call complete_task exactly once with notify=false and empty text."
        ),
        "live_durable_estop": "Call complete_task with notify=true and text 'must not run'.",
        "live_budget_stop": (
            "Call system_info twice in two distinct tool calls. Do not call complete_task "
            "until both calls succeeded."
        ),
        "live_delivery_unknown_recovery": "Do not execute; delivery recovery fixture.",
    }
    try:
        return prompts[fixture]
    except KeyError:
        raise FeishuAutomationLiveError("automation_fixture_unsupported") from None


def _inject_running_lease(database: Path, task: ScheduledTask) -> None:
    """经 TaskRun Repository 构造真实 running lease，供启动恢复结算。"""
    now = datetime.now(UTC)
    runs = TaskRunRepository(Database(database))
    runs.enqueue(task, scheduled_for=now)
    claimed = runs.claim_next("phase6-live-interrupted", now=now, lease_seconds=10)
    if claimed is None or claimed.task_id != task.id:
        raise FeishuAutomationLiveError("interrupted_fixture_claim_failed")
    runs.mark_running(claimed.id, "phase6-live-interrupted", now=now)


def _inject_unknown_delivery(
    preflight: AutomationLivePreflight,
    task: ScheduledTask,
) -> None:
    """经真实 Run/Outbox transitions 构造 unknown，留给 Gateway recovery 重试。"""
    now = datetime.now(UTC)
    database = Database(preflight.paths.database)
    runs = TaskRunRepository(database)
    runs.enqueue(task, scheduled_for=now)
    claimed = runs.claim_next("phase6-live-delivery", now=now, lease_seconds=30)
    if claimed is None or claimed.task_id != task.id:
        raise FeishuAutomationLiveError("delivery_fixture_claim_failed")
    runs.mark_running(claimed.id, "phase6-live-delivery", now=now)
    response = TaskResponse(True, "Phase 6 unknown delivery recovery verified")
    completed = runs.finish(
        claimed.id,
        status=RunStatus.SUCCEEDED,
        now=now,
        worker_id="phase6-live-delivery",
        response=response,
    )
    deliveries = DeliveryRepository(database)
    projected = TaskDeliveryService(
        deliveries,
        runs,
        approvals=ApprovalRepository(database),
        channel_max_chars={
            "feishu": preflight.config.channels.feishu.message_max_chars,
            "telegram": preflight.config.channels.telegram.message_max_chars,
            "discord": preflight.config.channels.discord.message_max_chars,
        },
    ).project(completed, response)
    if len(projected) != 1:
        raise FeishuAutomationLiveError("delivery_fixture_projection_failed")
    sending = deliveries.claim_next(
        "feishu",
        preflight.config.channels.feishu.account_id,
    )
    if sending is None or sending.id != projected[0].id:
        raise FeishuAutomationLiveError("delivery_fixture_claim_failed")
    deliveries.mark_unknown(sending.id, "channel_delivery_unknown")


def _single_waiting_task(database: Path) -> int:
    """读取唯一 waiting Automation Task；零条或歧义都失败关闭。"""
    with Database(database).connect_read_only() as connection:
        rows = connection.execute(
            """
            SELECT task_id FROM task_runs
            WHERE status = 'waiting_approval' AND approval_id IS NOT NULL
            ORDER BY id
            """
        ).fetchall()
    if len(rows) != 1:
        raise FeishuAutomationLiveError("waiting_approval_run_ambiguous")
    return int(rows[0][0])


async def _wait_for_automation_case(
    database: Path,
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
    *,
    timeout: float,
) -> AutomationLiveCaseResult:
    """有界轮询 durable evaluator，成功即返回，超时返回最后一个稳定失败。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    latest = evaluate_automation_case(database, checkpoint, case)
    while latest.status != "pass" and loop.time() < deadline:
        await asyncio.sleep(min(0.5, max(0.0, deadline - loop.time())))
        latest = evaluate_automation_case(database, checkpoint, case)
    return latest


async def _with_human_status(
    result: AutomationLiveCaseResult,
    input_fn: Callable[[str], str],
) -> AutomationLiveCaseResult:
    """只在 durable evidence 通过后收集一个有限人工可见性结论。"""
    if result.status != "pass":
        return result
    status = await asyncio.to_thread(_read_live_human_status, input_fn)
    if status == "pass":
        return replace(result, human_status="pass")
    return replace(
        result,
        status=status,
        human_status=status,
        error_code="operator_skipped" if status == "skip" else "human_evidence_failed",
    )


def _read_live_action(input_fn: Callable[[str], str]) -> str:
    """审批动作只接受 Enter 继续或 s 跳过。"""
    while True:
        try:
            value = input_fn("完成飞书动作后按 Enter，或输入 s 跳过：").strip().lower()
        except (EOFError, StopIteration):
            return "skip"
        if value == "":
            return "continue"
        if value == "s":
            return "skip"


def _read_live_human_status(input_fn: Callable[[str], str]) -> str:
    """人工可见性只接受 p/f/s，EOF 按 skip。"""
    while True:
        try:
            value = input_fn("飞书可见结果符合本 case 吗？[p/f/s]：").strip().lower()
        except (EOFError, StopIteration):
            return "skip"
        if value in {"p", "f", "s"}:
            return {"p": "pass", "f": "fail", "s": "skip"}[value]


def _skipped_case(case: EvalCase) -> AutomationLiveCaseResult:
    """构造操作者明确跳过的封闭结果。"""
    requirements = tuple(case.expected.automation_evidence)
    return AutomationLiveCaseResult(
        case.id,
        "skip",
        (),
        requirements,
        "skip",
        "operator_skipped",
    )


def _cancel_task_if_active(database: Path, task: ScheduledTask, owner_id: int) -> None:
    """清理只属于本 Gate 的 active/paused Task；终态 Task 保持证据。"""
    tasks = ScheduledTaskRepository(Database(database))
    current = tasks.get(task.id, owner_id=owner_id)
    if current.status in {TaskStatus.ACTIVE, TaskStatus.PAUSED}:
        tasks.cancel(task.id, owner_id=owner_id, expected_version=current.version)


def run_feishu_automation_live_harness(argv: Sequence[str] | None = None) -> int:
    """运行显式确认的飞书 Automation 生产验收；未确认时不触碰状态。"""
    parser = argparse.ArgumentParser(description="Run confirmed Feishu Automation Live gate.")
    parser.add_argument("--home")
    parser.add_argument("--root")
    parser.add_argument("--output-dir")
    parser.add_argument("--confirm-live", action="store_true")
    parser.add_argument("--gateway-timeout", type=float, default=30.0)
    parser.add_argument("--case-timeout", type=float, default=180.0)
    arguments = parser.parse_args(argv)
    if not arguments.confirm_live:
        print(
            "error: --confirm-live is required; no config, secret, state, or network was read",
            file=sys.stderr,
        )
        return 2
    project_root = Path(__file__).resolve().parents[3]
    scenario_root = _confirmed_path(
        arguments.root,
        project_root / "evals" / "scenarios",
    )
    output_dir = _confirmed_path(
        arguments.output_dir,
        project_root / ".local" / "eval-results" / "feishu-automation",
    )
    if not 5 <= arguments.gateway_timeout <= 120 or not 10 <= arguments.case_timeout <= 600:
        print("error: automation_live_timeout_invalid", file=sys.stderr)
        return 2
    try:
        preflight = _load_automation_preflight(
            project_root=project_root,
            home=arguments.home,
            root=scenario_root,
        )
    except FeishuAutomationLiveError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2
    started_at = utc_timestamp()
    try:
        execution = asyncio.run(
            _execute_automation_live_cases(
                preflight,
                gateway_timeout=arguments.gateway_timeout,
                case_timeout=arguments.case_timeout,
                input_fn=input,
                output_fn=print,
            )
        )
    except FeishuAutomationLiveError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 1

    results = execution.results
    current_commit, dirty = _repository_state(project_root)
    if current_commit != preflight.commit or dirty:
        results = _force_automation_failure(results, "repository_changed")
    if not execution.gateway_ready or not execution.gateway_graceful_exit:
        results = _force_automation_failure(results, "gateway_lifecycle_failed")
    try:
        secret_matches = scan_secret_matches(
            (preflight.paths.logs, output_dir),
            _automation_sensitive_values(preflight),
        ) + execution.gateway_secret_matches
        report = build_automation_evidence_report(
            commit=preflight.commit,
            started_at=started_at,
            finished_at=utc_timestamp(),
            results=results,
            secret_matches=secret_matches,
        )
        _prepare_automation_output_directory(output_dir)
        target = output_dir / f"{_evidence_filename(report['finished_at'])}.json"
        write_private_json(target, report)
    except (FeishuAutomationLiveError, ProductionEvidenceError, ValueError):
        print("error: automation_evidence_write_failed", file=sys.stderr)
        return 1
    print(f"Saved redacted evidence: {target.name}")
    return 0 if report["release_status"] == "FEISHU_AUTOMATION_VERIFIED" else 1


def _confirmed_path(value: str | None, default: Path) -> Path:
    """只在 confirm gate 后展开并规范化 CLI 路径。"""
    candidate = default if value is None else Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


def _prepare_automation_output_directory(path: Path) -> None:
    """创建 owner-only Evidence 目录，并拒绝 symlink、他人 owner 或宽权限。"""
    try:
        if path.is_symlink():
            raise FeishuAutomationLiveError("evidence_directory_unsafe")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or path.is_symlink()
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise FeishuAutomationLiveError("evidence_directory_unsafe")
    except FeishuAutomationLiveError:
        raise
    except OSError:
        raise FeishuAutomationLiveError("evidence_directory_unsafe") from None


def _evidence_filename(value: object) -> str:
    """把已验证 UTC 字符串变成安全且不含路径的 Evidence 文件名。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        raise FeishuAutomationLiveError("evidence_timestamp_invalid")
    return value.replace("-", "").replace(":", "").replace(".", "")


def _force_automation_failure(
    results: tuple[AutomationLiveCaseResult, ...],
    error_code: str,
) -> tuple[AutomationLiveCaseResult, ...]:
    """把最后一个已有 case 降级为稳定失败，绝不加入正文或外部 ID。"""
    if not results:
        return ()
    last = results[-1]
    failed = replace(last, status="fail", human_status=None, error_code=error_code)
    return (*results[:-1], failed)


@dataclass(frozen=True, slots=True)
class PendingAutomationRun:
    """保存 checkpoint 时一个 waiting Run 的绑定事实与 snapshot hash。"""

    run_id: int
    approval_id: int
    turn_id: int
    snapshot_hash: str


@dataclass(frozen=True, slots=True)
class AutomationLiveCheckpoint:
    """保存人工动作前相关事实表的高水位和目标 Task。"""

    task_ids: tuple[int, ...]
    task_id: int
    run_id: int
    turn_id: int
    tool_run_id: int
    approval_id: int
    delivery_id: int
    control_revision: int
    control_halted: bool
    captured_at: str
    pending_runs: tuple[PendingAutomationRun, ...]


@dataclass(frozen=True, slots=True)
class AutomationLiveCaseResult:
    """保存单个 Live case 的封闭结论，不包含数据库行或外部 ID。"""

    case_id: str
    status: str
    evidence_passed: tuple[str, ...]
    evidence_failed: tuple[str, ...]
    human_status: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class _Facts:
    """保存 evaluator 内存中的相关 SQLite 行。"""

    runs: tuple[sqlite3.Row, ...]
    turns: tuple[sqlite3.Row, ...]
    tools: tuple[sqlite3.Row, ...]
    approvals: tuple[sqlite3.Row, ...]
    deliveries: tuple[sqlite3.Row, ...]
    new_task_count: int
    control_revision: int
    control_halted: bool


def capture_automation_checkpoint(
    database: Path,
    *,
    task_ids: Sequence[int],
    now: datetime | None = None,
) -> AutomationLiveCheckpoint:
    """只读捕获目标 Task 与事实表高水位。

    Args:
        database: 已初始化的 MiniClaw SQLite 文件。
        task_ids: 本 case 允许关联的内部 Task ID。
        now: 可注入的 UTC checkpoint 时间。

    Returns:
        不含业务正文的不可变 checkpoint。

    Raises:
        ValueError: Task ID 或时钟无效。
        DatabaseError: 数据库不可读或 schema 不完整。
    """
    targets = tuple(sorted(set(task_ids)))
    if not targets or any(type(task_id) is not int or task_id <= 0 for task_id in targets):
        raise ValueError("automation_live_task_ids_invalid")
    captured = datetime.now(UTC) if now is None else now
    if not isinstance(captured, datetime) or captured.tzinfo is None:
        raise ValueError("automation_live_clock_invalid")
    captured_at = captured.astimezone(UTC).isoformat()
    with Database(database).connect_read_only() as connection:
        pending_rows = connection.execute(
            f"""
            SELECT id, approval_id, turn_id, snapshot_json
            FROM task_runs
            WHERE task_id IN ({','.join('?' for _ in targets)})
              AND status = 'waiting_approval'
              AND approval_id IS NOT NULL AND turn_id IS NOT NULL
            ORDER BY id
            """,
            targets,
        ).fetchall()
        control = connection.execute(
            "SELECT halted, revision FROM automation_control WHERE singleton = 1"
        ).fetchone()
        if control is None:
            raise DatabaseError("automation control is unavailable")
        return AutomationLiveCheckpoint(
            task_ids=targets,
            task_id=_maximum(connection, "scheduled_tasks"),
            run_id=_maximum(connection, "task_runs"),
            turn_id=_maximum(connection, "turns"),
            tool_run_id=_maximum(connection, "tool_runs"),
            approval_id=_maximum(connection, "approvals"),
            delivery_id=_maximum(connection, "deliveries"),
            control_revision=int(control["revision"]),
            control_halted=bool(control["halted"]),
            captured_at=captured_at,
            pending_runs=tuple(
                PendingAutomationRun(
                    run_id=int(row["id"]),
                    approval_id=int(row["approval_id"]),
                    turn_id=int(row["turn_id"]),
                    snapshot_hash=hashlib.sha256(
                        str(row["snapshot_json"]).encode("utf-8")
                    ).hexdigest(),
                )
                for row in pending_rows
            ),
        )


def evaluate_automation_case(
    database: Path,
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
) -> AutomationLiveCaseResult:
    """只读评价一个版本化 Automation Live case。

    Args:
        database: MiniClaw SQLite 文件。
        checkpoint: 人工动作前捕获的高水位。
        case: 固定 `FEISHU-AUTO-001..010` 场景。

    Returns:
        只含封闭 evidence key 和稳定错误码的结果。
    """
    requirements = tuple(case.expected.automation_evidence)
    if not _valid_input(checkpoint, case, requirements):
        return _failed_result(case.id, requirements, "automation_case_invalid")
    try:
        with Database(database).connect_read_only() as connection:
            facts = _load_facts(connection, checkpoint)
    except (DatabaseError, OSError, sqlite3.Error, TypeError, ValueError):
        return _failed_result(case.id, requirements, "automation_evidence_unavailable")

    if _clock_rolled_back(facts.runs, checkpoint):
        return _failed_result(case.id, requirements, "clock_rollback")
    if _has_pending_leak(facts, case.automation_fixture or ""):
        return _failed_result(case.id, requirements, "pending_approval_leak")

    checks = _evidence_checks(facts, checkpoint, case)
    passed = tuple(key for key in requirements if checks.get(key, False))
    failed = tuple(key for key in requirements if key not in passed)
    common = _common_expectations(facts, case)
    if failed or not common:
        return AutomationLiveCaseResult(
            case.id,
            "fail",
            passed,
            failed,
            None,
            "automation_evidence_failed",
        )
    return AutomationLiveCaseResult(case.id, "pass", passed, (), None, None)


def build_automation_evidence_report(
    *,
    commit: str,
    started_at: str,
    finished_at: str,
    results: Sequence[AutomationLiveCaseResult],
    secret_matches: int,
) -> dict[str, object]:
    """构造十条 Automation Live 的封闭生产 Evidence 报告。

    Args:
        commit: clean repository commit。
        started_at: UTC 起始时间。
        finished_at: UTC 结束时间。
        results: 单 case 结论。
        secret_matches: exact Secret scan 匿名计数。

    Returns:
        可交给 private writer 的标准 JSON object。

    Raises:
        ValueError: report 字段、结果或时间不符合闭合契约。
    """
    try:
        normalized_commit = validate_commit(commit)
    except ProductionEvidenceError:
        raise ValueError("invalid_automation_evidence_report") from None
    if not _is_timestamp(started_at) or not _is_timestamp(finished_at):
        raise ValueError("invalid_automation_evidence_report")
    if type(secret_matches) is not int or secret_matches < 0:
        raise ValueError("invalid_automation_evidence_report")
    normalized = tuple(_validate_result(result) for result in results)
    if len({result.case_id for result in normalized}) != len(normalized):
        raise ValueError("invalid_automation_evidence_report")
    checks = [
        {
            "case_id": result.case_id,
            "status": result.status,
            "evidence": [
                {"key": key, "status": status}
                for status, keys in (
                    ("pass", result.evidence_passed),
                    ("fail", result.evidence_failed),
                )
                for key in keys
            ],
            "human_status": result.human_status,
            "error_code": result.error_code,
        }
        for result in normalized
    ]
    counts = {
        "cases_total": len(normalized),
        "cases_passed": sum(result.status == "pass" for result in normalized),
        "cases_failed": sum(result.status == "fail" for result in normalized),
        "cases_skipped": sum(result.status == "skip" for result in normalized),
        "secret_matches": secret_matches,
    }
    expected = {f"FEISHU-AUTO-{index:03d}" for index in range(1, 11)}
    verified = (
        secret_matches == 0
        and {result.case_id for result in normalized} == expected
        and all(result.status == "pass" for result in normalized)
        and all(result.human_status in {None, "pass"} for result in normalized)
    )
    return {
        "schema_version": 1,
        "suite": "feishu-automation",
        "commit": normalized_commit,
        "started_at": started_at,
        "finished_at": finished_at,
        "checks": checks,
        "counts": counts,
        "secret_matches": secret_matches,
        "release_status": (
            "FEISHU_AUTOMATION_VERIFIED" if verified else "FEISHU_AUTOMATION_FAILED"
        ),
    }


def validate_automation_evidence_report(report: Mapping[str, object]) -> bool:
    """严格重建并验证已经读取的 Automation Evidence。

    Args:
        report: private JSON 文件中的候选 object。

    Returns:
        exact schema、case、计数与 release status 全部可重建时返回 ``True``。
    """
    expected_keys = {
        "schema_version",
        "suite",
        "commit",
        "started_at",
        "finished_at",
        "checks",
        "counts",
        "secret_matches",
        "release_status",
    }
    if not isinstance(report, Mapping) or set(report) != expected_keys:
        return False
    checks = report.get("checks")
    if not isinstance(checks, list):
        return False
    results: list[AutomationLiveCaseResult] = []
    for check in checks:
        if not isinstance(check, Mapping) or set(check) != {
            "case_id",
            "status",
            "evidence",
            "human_status",
            "error_code",
        }:
            return False
        evidence = check.get("evidence")
        if not isinstance(evidence, list):
            return False
        passed: list[str] = []
        failed: list[str] = []
        for item in evidence:
            if (
                not isinstance(item, Mapping)
                or set(item) != {"key", "status"}
                or not isinstance(item.get("key"), str)
                or item.get("status") not in {"pass", "fail"}
            ):
                return False
            (passed if item["status"] == "pass" else failed).append(item["key"])
        try:
            results.append(
                AutomationLiveCaseResult(
                    check["case_id"],
                    check["status"],
                    tuple(passed),
                    tuple(failed),
                    check["human_status"],
                    check["error_code"],
                )
            )
        except (TypeError, ValueError):
            return False
    try:
        rebuilt = build_automation_evidence_report(
            commit=report["commit"],
            started_at=report["started_at"],
            finished_at=report["finished_at"],
            results=results,
            secret_matches=report["secret_matches"],
        )
    except (TypeError, ValueError):
        return False
    return rebuilt == dict(report)


def _valid_input(
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
    requirements: tuple[str, ...],
) -> bool:
    """验证 evaluator 输入只使用固定 case、fixture 与 evidence。"""
    return (
        isinstance(checkpoint, AutomationLiveCheckpoint)
        and re.fullmatch(r"FEISHU-AUTO-(?:00[1-9]|010)", case.id) is not None
        and case.capability == "feishu_automation_e2e"
        and case.automation_fixture in _EXPECTED_FIXTURES
        and bool(requirements)
        and len(requirements) == len(set(requirements))
        and all(key in _EVIDENCE_KEYS for key in requirements)
    )


def _load_facts(
    connection: sqlite3.Connection,
    checkpoint: AutomationLiveCheckpoint,
) -> _Facts:
    """读取 checkpoint 后或 continuation 绑定的最小相关行。"""
    targets = checkpoint.task_ids
    pending_ids = tuple(item.run_id for item in checkpoint.pending_runs)
    parameters: tuple[object, ...] = (*targets, checkpoint.run_id, *pending_ids)
    pending_clause = ""
    if pending_ids:
        pending_clause = f" OR id IN ({','.join('?' for _ in pending_ids)})"
    runs = tuple(
        connection.execute(
            f"""
            SELECT id, task_id, turn_id, approval_id, scheduled_for, idempotency_key,
                   snapshot_json, status, worker_id, lease_expires_at, completed_at,
                   response_json, error_code, created_at
            FROM task_runs
            WHERE task_id IN ({','.join('?' for _ in targets)})
              AND (id > ?{pending_clause})
            ORDER BY id
            """,
            parameters,
        ).fetchall()
    )
    turn_ids = tuple(
        sorted(
            {
                *(
                    int(row["turn_id"])
                    for row in runs
                    if row["turn_id"] is not None
                ),
                *(item.turn_id for item in checkpoint.pending_runs),
            }
        )
    )
    turns = _select_by_ids(
        connection,
        "turns",
        "id, parent_turn_id, runtime_snapshot_json, status, started_at, completed_at",
        turn_ids,
    )
    tools = _select_by_foreign_ids(
        connection,
        "tool_runs",
        "turn_id",
        "id, turn_id, tool_name, status, created_at, completed_at",
        turn_ids,
    )
    approval_ids = tuple(
        sorted(
            {
                *(item.approval_id for item in checkpoint.pending_runs),
                *(int(row["approval_id"]) for row in runs if row["approval_id"] is not None),
            }
        )
    )
    approvals = _select_by_ids(
        connection,
        "approvals",
        "id, turn_id, tool_run_id, status, created_at, decided_at",
        approval_ids,
    )
    run_ids = tuple(int(row["id"]) for row in runs)
    deliveries = _select_by_foreign_ids(
        connection,
        "deliveries",
        "task_run_id",
        "id, task_run_id, channel, part_index, delivery_kind, idempotency_key, "
        "status, attempts, created_at, sent_at",
        run_ids,
        minimum_id=checkpoint.delivery_id,
    )
    control = connection.execute(
        "SELECT halted, revision FROM automation_control WHERE singleton = 1"
    ).fetchone()
    if control is None:
        raise ValueError("automation control missing")
    new_task_count = int(
        connection.execute(
            "SELECT COUNT(*) FROM scheduled_tasks WHERE id > ?",
            (checkpoint.task_id,),
        ).fetchone()[0]
    )
    return _Facts(
        runs=runs,
        turns=turns,
        tools=tools,
        approvals=approvals,
        deliveries=deliveries,
        new_task_count=new_task_count,
        control_revision=int(control["revision"]),
        control_halted=bool(control["halted"]),
    )


def _evidence_checks(
    facts: _Facts,
    checkpoint: AutomationLiveCheckpoint,
    case: EvalCase,
) -> dict[str, bool]:
    """从最小 facts 派生固定 evidence key，不接受调用方表达式。"""
    runs = facts.runs
    deliveries = facts.deliveries
    sent_once = (
        len(deliveries) == (case.expected.delivery_count or 0)
        and all(row["channel"] == "feishu" and row["status"] == "sent" for row in deliveries)
        and len({row["idempotency_key"] for row in deliveries}) == len(deliveries)
    )
    provider_observed = any(_provider_observed(row) for row in facts.turns)
    pending = checkpoint.pending_runs
    continuation = False
    budget_preserved = False
    if len(pending) == 1 and len(runs) == 1:
        bound = pending[0]
        run = runs[0]
        approval = next(
            (row for row in facts.approvals if int(row["id"]) == bound.approval_id),
            None,
        )
        turn = next(
            (row for row in facts.turns if row["parent_turn_id"] == bound.turn_id),
            None,
        )
        tool_names = {(row["tool_name"], row["status"]) for row in facts.tools}
        continuation = (
            int(run["id"]) == bound.run_id
            and run["status"] == "succeeded"
            and approval is not None
            and approval["status"] == "consumed"
            and turn is not None
            and ("write_file", "succeeded") in tool_names
            and ("complete_task", "succeeded") in tool_names
        )
        budget_preserved = (
            hashlib.sha256(str(run["snapshot_json"]).encode("utf-8")).hexdigest()
            == bound.snapshot_hash
        )
    waiting_bound = _waiting_is_bound(facts)
    approval_delivery_once = (
        len(deliveries) == 1
        and deliveries[0]["channel"] == "feishu"
        and deliveries[0]["delivery_kind"] == "approval"
        and deliveries[0]["status"] == "sent"
    )
    structured_silence = len(runs) == 1 and _is_structured_silence(runs[0])
    budget_stopped = (
        len(runs) == 1
        and runs[0]["status"] == "failed"
        and isinstance(runs[0]["error_code"], str)
        and str(runs[0]["error_code"]).startswith("task_budget_")
        and len(facts.tools) <= 1
        and not deliveries
    )
    no_side_effect = (
        not any(row["status"] == "succeeded" for row in facts.tools)
        if case.automation_fixture == "live_waiting_approval"
        else budget_stopped
    )
    return {
        "one_slot_only": len(runs) == 1 and len({runs[0]["idempotency_key"]}) == 1,
        "delivery_once": sent_once,
        "provider_request_observed": provider_observed,
        "two_slots_once": (
            len(runs) == 2
            and len({row["task_id"] for row in runs}) == 1
            and len({row["scheduled_for"] for row in runs}) == 2
            and len({row["idempotency_key"] for row in runs}) == 2
            and all(row["status"] == "succeeded" for row in runs)
            and sent_once
        ),
        "task_identity_preserved": (
            bool(runs)
            and all(int(row["task_id"]) in checkpoint.task_ids for row in runs)
            and facts.new_task_count == 0
        ),
        "gateway_restart_recovered": len(runs) == 1 and runs[0]["status"] == "succeeded",
        "stale_run_interrupted": len(runs) == 1 and runs[0]["status"] == "interrupted",
        "lease_released": bool(runs)
        and all(row["worker_id"] is None and row["lease_expires_at"] is None for row in runs),
        "approval_id_bound": waiting_bound,
        "approval_delivery_once": approval_delivery_once,
        "continuation_terminal": continuation,
        "original_budget_preserved": budget_preserved,
        "structured_silence": structured_silence and not deliveries,
        "zero_claim": (
            facts.control_halted
            and facts.control_revision > checkpoint.control_revision
            and not runs
            and not deliveries
        ),
        "budget_stopped": budget_stopped,
        "no_side_effect": no_side_effect,
        "idempotency_key_reused": (
            len(deliveries) == 1
            and deliveries[0]["status"] == "sent"
            and int(deliveries[0]["attempts"]) >= 2
        ),
    }


def _common_expectations(facts: _Facts, case: EvalCase) -> bool:
    """验证版本化 status 与 Delivery 数量的公共约束。"""
    expected = case.expected.automation_status
    if expected == "halted":
        status_matches = facts.control_halted and not facts.runs
    elif expected == "failed":
        status_matches = bool(facts.runs) and all(
            row["status"] in {"failed", "interrupted", "timed_out", "cancelled"}
            for row in facts.runs
        )
    else:
        status_matches = bool(facts.runs) and all(row["status"] == expected for row in facts.runs)
    delivery_count = case.expected.delivery_count
    return status_matches and (
        delivery_count is None or len(facts.deliveries) == delivery_count
    )


def _waiting_is_bound(facts: _Facts) -> bool:
    """判断 waiting Run、Approval 与 ToolRun 是否同一条绑定链。"""
    if len(facts.runs) != 1 or len(facts.approvals) != 1:
        return False
    run = facts.runs[0]
    approval = facts.approvals[0]
    tool = next(
        (row for row in facts.tools if int(row["id"]) == int(approval["tool_run_id"])),
        None,
    )
    return (
        run["status"] == "waiting_approval"
        and run["worker_id"] is None
        and run["lease_expires_at"] is None
        and run["approval_id"] == approval["id"]
        and approval["status"] == "pending"
        and tool is not None
        and tool["status"] == "waiting_approval"
    )


def _has_pending_leak(facts: _Facts, fixture: str) -> bool:
    """除 waiting case 外拒绝相关 Approval 保持 pending。"""
    return fixture != "live_waiting_approval" and any(
        row["status"] == "pending" for row in facts.approvals
    )


def _provider_observed(turn: sqlite3.Row) -> bool:
    """只判断 Provider request existence bit，不返回 ID。"""
    try:
        value = json.loads(str(turn["runtime_snapshot_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return isinstance(value, Mapping) and isinstance(value.get("provider_request_id"), str)


def _is_structured_silence(run: sqlite3.Row) -> bool:
    """判断 terminal response 是否为 notify=false + empty text。"""
    try:
        value = json.loads(str(run["response_json"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        return False
    return value == {"notify": False, "text": ""}


def _clock_rolled_back(
    runs: Sequence[sqlite3.Row],
    checkpoint: AutomationLiveCheckpoint,
) -> bool:
    """拒绝 checkpoint 后新建但时间早于 checkpoint 的 Run。"""
    captured = _parse_timestamp(checkpoint.captured_at)
    for row in runs:
        if int(row["id"]) <= checkpoint.run_id:
            continue
        created = _parse_timestamp(str(row["created_at"]))
        if created is None or captured is None or created < captured:
            return True
    return False


def _select_by_ids(
    connection: sqlite3.Connection,
    table: str,
    columns: str,
    ids: tuple[int, ...],
) -> tuple[sqlite3.Row, ...]:
    """从固定表按内部 ID 读取稳定顺序行。"""
    if table not in {"turns", "approvals"} or not ids:
        return ()
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM {table} WHERE id IN "
            f"({','.join('?' for _ in ids)}) ORDER BY id",
            ids,
        ).fetchall()
    )


def _select_by_foreign_ids(
    connection: sqlite3.Connection,
    table: str,
    foreign_key: str,
    columns: str,
    ids: tuple[int, ...],
    *,
    minimum_id: int | None = None,
) -> tuple[sqlite3.Row, ...]:
    """从固定 Tool/Delivery 表按内部外键读取相关行。"""
    allowed = {("tool_runs", "turn_id"), ("deliveries", "task_run_id")}
    if (table, foreign_key) not in allowed or not ids:
        return ()
    suffix = "" if minimum_id is None else " AND id > ?"
    parameters: tuple[object, ...] = ids
    if minimum_id is not None:
        parameters = (*parameters, minimum_id)
    return tuple(
        connection.execute(
            f"SELECT {columns} FROM {table} WHERE {foreign_key} IN "
            f"({','.join('?' for _ in ids)}){suffix} ORDER BY id",
            parameters,
        ).fetchall()
    )


def _maximum(connection: sqlite3.Connection, table: str) -> int:
    """读取固定事实表的最大内部 ID。"""
    if table not in {
        "scheduled_tasks",
        "task_runs",
        "turns",
        "tool_runs",
        "approvals",
        "deliveries",
    }:
        raise ValueError("unsupported automation checkpoint table")
    return int(connection.execute(f"SELECT COALESCE(MAX(id), 0) FROM {table}").fetchone()[0])


def _failed_result(
    case_id: str,
    requirements: tuple[str, ...],
    error_code: str,
) -> AutomationLiveCaseResult:
    """构造不回显数据库事实的稳定失败结果。"""
    return AutomationLiveCaseResult(case_id, "fail", (), requirements, None, error_code)


def _validate_result(result: AutomationLiveCaseResult) -> AutomationLiveCaseResult:
    """验证 report 输入使用封闭 ID、状态和 evidence。"""
    if (
        not isinstance(result, AutomationLiveCaseResult)
        or re.fullmatch(r"FEISHU-AUTO-(?:00[1-9]|010)", result.case_id) is None
        or result.status not in _CASE_STATUSES
        or result.human_status not in {None, "pass", "fail", "skip"}
        or any(
            key not in _EVIDENCE_KEYS
            for key in (*result.evidence_passed, *result.evidence_failed)
        )
        or len({*result.evidence_passed, *result.evidence_failed})
        != len(result.evidence_passed) + len(result.evidence_failed)
        or (
            result.error_code is not None
            and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", result.error_code) is None
        )
    ):
        raise ValueError("invalid_automation_evidence_report")
    if result.status == "pass" and (result.evidence_failed or result.error_code is not None):
        raise ValueError("invalid_automation_evidence_report")
    return result


def _is_timestamp(value: object) -> bool:
    """判断值是否为不含本地信息的 UTC ISO-8601 时间。"""
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    return _parse_timestamp(value) is not None


def _parse_timestamp(value: str) -> datetime | None:
    """解析 UTC/offset ISO-8601 时间并规范化为 UTC。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)
