"""真实飞书 E2E 的只读 SQLite 证据与后续编排接口。"""

import argparse
import asyncio
import json
import os
import re
import signal
import sqlite3
import stat
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Container, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from miniclaw.channels.supervisor import GatewaySecrets
from miniclaw.config import AppConfig, ConfigError, load_config
from miniclaw.doctor import CheckResult, CheckStatus, run_local_checks
from miniclaw.env import DotEnvError, load_dotenv
from miniclaw.evals.cases import EvalCase, EvalCaseError, load_feishu_live_cases
from miniclaw.gateway import GatewayConfigError, validate_gateway_environment
from miniclaw.paths import (
    PathConfigurationError,
    StatePaths,
    build_state_paths,
    resolve_home,
)
from miniclaw.storage.database import Database, DatabaseError


class FeishuLiveError(RuntimeError):
    """表示 Live E2E 只能向操作者公开的稳定错误码。"""

    def __init__(self, code: str) -> None:
        """保存不含路径、SQL、正文或平台标识的错误码。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DatabaseCheckpoint:
    """保存一次人工动作前六张事实表的最大内部 ID。"""

    processed_event_rowid: int
    turn_id: int
    tool_run_id: int
    approval_id: int
    delivery_id: int
    audit_event_id: int
    pending_approval_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class EvidenceEvaluation:
    """按请求顺序保存已经满足与尚未满足的 Live evidence key。"""

    passed: tuple[str, ...]
    failed: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FeishuCaseResult:
    """保存一个 Live case 的封闭自动/人工结论，不包含原始数据。"""

    case_id: str
    status: str
    local_passed: tuple[str, ...]
    local_failed: tuple[str, ...]
    human_statuses: tuple[tuple[str, str], ...]
    error_code: str | None


@dataclass(frozen=True, slots=True)
class LivePreflight:
    """保存显式确认后得到的只读静态输入与内存 Secret。"""

    project_root: Path
    paths: StatePaths
    config: AppConfig
    secrets: GatewaySecrets
    cases: tuple[EvalCase, ...]
    commit: str


@dataclass(frozen=True, slots=True)
class LiveExecution:
    """保存 Gateway 运行结束后的封闭 case 与生命周期结论。"""

    results: tuple[FeishuCaseResult, ...]
    gateway_ready: bool
    gateway_graceful_exit: bool


_CASE_STATUSES = frozenset({"pass", "fail", "skip"})
_HUMAN_EVIDENCE = frozenset(
    {
        "reply_visible",
        "context_answer_correct",
        "system_info_visible",
        "sentinel_visible",
        "approval_prompt_visible",
        "approved_result_visible",
        "denial_visible",
        "bot_silent",
        "group_reply_visible",
        "long_content_intact",
        "restart_answer_correct",
        "reconnect_reply_visible",
    }
)
_RELEASE_STATUSES = frozenset(
    {"FEISHU_E2E_VERIFIED", "FEISHU_LIVE_PARTIAL", "FEISHU_LIVE_FAILED"}
)
_COUNT_KEYS = frozenset(
    {
        "cases_total",
        "cases_passed",
        "cases_failed",
        "cases_skipped",
        "local_evidence_passed",
        "local_evidence_failed",
        "human_evidence_passed",
        "human_evidence_failed",
        "human_evidence_skipped",
        "secret_matches",
    }
)
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "channel",
        "commit",
        "started_at",
        "finished_at",
        "gateway",
        "checks",
        "counts",
        "release_status",
    }
)
_MAX_SCAN_FILES = 1000
_MAX_SCAN_FILE_BYTES = 1024 * 1024


def run_feishu_live_harness(argv: Sequence[str] | None = None) -> int:
    """运行显式确认、真实 Gateway、人工动作和自动 SQLite 取证闭环。"""
    arguments = _build_live_parser().parse_args(argv)
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
        project_root / ".local" / "eval-results" / "feishu",
    )
    try:
        preflight = _load_preflight(
            project_root=project_root,
            home=arguments.home,
            root=scenario_root,
        )
    except FeishuLiveError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 2

    started_at = _utc_timestamp()
    runtime_error: str | None = None
    try:
        execution = asyncio.run(
            _execute_live_cases(
                preflight,
                gateway_timeout=arguments.gateway_timeout,
                case_timeout=arguments.case_timeout,
                input_fn=input,
                output_fn=print,
            )
        )
    except FeishuLiveError as error:
        runtime_error = error.code
        execution = LiveExecution((), False, False)

    needles = _sensitive_values(preflight)
    try:
        secret_matches = scan_secret_matches((preflight.paths.logs, output_dir), needles)
    except FeishuLiveError:
        secret_matches = 1
    results = execution.results
    if runtime_error is not None:
        results = _record_runtime_failure(results, runtime_error)
    if not _repository_unchanged(preflight.project_root, preflight.commit):
        results = _force_case_failure(
            results,
            case_id="FEISHU-LIVE-015",
            evidence_key="secret_scan_zero",
            error_code="repository_changed",
        )
    if secret_matches and not any(result.case_id == "FEISHU-LIVE-015" for result in results):
        results = (*results, _failed_secret_case("secret_scan_match"))

    finished_at = _utc_timestamp()
    try:
        report = build_evidence_report(
            commit=preflight.commit,
            started_at=started_at,
            finished_at=finished_at,
            gateway_ready=execution.gateway_ready,
            gateway_graceful_exit=execution.gateway_graceful_exit,
            results=results,
            secret_matches=secret_matches,
        )
        _prepare_output_directory(output_dir)
        target = output_dir / (_filename_timestamp(finished_at) + ".json")
        write_evidence(target, report)
    except FeishuLiveError as error:
        print(f"error: {error.code}", file=sys.stderr)
        return 1

    print(f"Saved redacted evidence: {target.name}")
    if runtime_error is not None:
        print(f"error: {runtime_error}", file=sys.stderr)
    return 0 if report["release_status"] == "FEISHU_E2E_VERIFIED" else 1


def _build_live_parser() -> argparse.ArgumentParser:
    """创建未确认阶段只解析标量、不会解析或创建路径的 CLI parser。"""
    parser = argparse.ArgumentParser(
        description="Run human-driven Feishu Bot E2E with read-only local evidence."
    )
    parser.add_argument("--home", help="absolute MiniClaw state directory")
    parser.add_argument("--root", help="versioned Feishu Live scenario directory")
    parser.add_argument("--output-dir", help="ignored redacted evidence directory")
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help="confirm real interaction with the configured private Feishu Bot",
    )
    parser.add_argument(
        "--gateway-timeout",
        type=_bounded_seconds(5.0, 120.0),
        default=30.0,
    )
    parser.add_argument(
        "--case-timeout",
        type=_bounded_seconds(5.0, 300.0),
        default=60.0,
    )
    return parser


def _bounded_seconds(minimum: float, maximum: float) -> Callable[[str], float]:
    """构造 argparse 使用的有限正数解析器。"""

    def parse(value: str) -> float:
        try:
            parsed = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError("timeout must be a number") from None
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"timeout must be between {minimum:g} and {maximum:g} seconds"
            )
        return parsed

    return parse


def _confirmed_path(value: str | None, default: Path) -> Path:
    """只在 confirm gate 之后展开并解析路径。"""
    candidate = default if value is None else Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve(strict=False)


def _load_preflight(*, project_root: Path, home: str | None, root: Path) -> LivePreflight:
    """加载私密环境并在创建网络对象前完成全部 fail-closed 检查。"""
    try:
        environment = dict(os.environ)
        load_dotenv(project_root / ".env", environment)
        paths = build_state_paths(resolve_home(home, environment))
        config = load_config(paths, environment)
        cases = load_feishu_live_cases(root)
        checks = run_local_checks(paths, environment)
        commit, dirty = _repository_state(project_root)
        pending = _pending_approval_count(paths.database)
        _validate_preflight_state(
            config=config,
            checks=checks,
            pending_approvals=pending,
            commit=commit,
            dirty=dirty,
            cases=cases,
        )
        secrets = validate_gateway_environment(config, environment)
        return LivePreflight(project_root, paths, config, secrets, cases, commit)
    except FeishuLiveError:
        raise
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
        raise FeishuLiveError("feishu_live_preflight_failed") from None


def _validate_preflight_state(
    *,
    config: Any,
    checks: Sequence[CheckResult],
    pending_approvals: int,
    commit: str,
    dirty: bool,
    cases: Sequence[object],
) -> None:
    """验证 Live evidence 需要的单 Channel、clean commit 与空审批状态。"""
    channels = config.channels
    if not channels.feishu.enabled:
        raise FeishuLiveError("feishu_channel_disabled")
    if channels.telegram.enabled or channels.discord.enabled:
        raise FeishuLiveError("peer_channel_enabled")
    if not _is_commit(commit):
        raise FeishuLiveError("repository_commit_unavailable")
    if dirty:
        raise FeishuLiveError("repository_dirty")
    if any(check.status is CheckStatus.FAIL for check in checks):
        raise FeishuLiveError("doctor_preflight_failed")
    if type(pending_approvals) is not int or pending_approvals < 0:
        raise FeishuLiveError("approval_state_unavailable")
    if pending_approvals:
        raise FeishuLiveError("pending_approval_exists")
    if len(cases) != 15:
        raise FeishuLiveError("live_case_count_invalid")


def _repository_state(project_root: Path) -> tuple[str, bool]:
    """有界读取 HEAD 与 worktree 状态，不读取 diff 或文件正文。"""
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=project_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown", True
    commit = head.stdout.strip().lower()
    if head.returncode != 0 or not _is_commit(commit) or status.returncode != 0:
        return "unknown", True
    return commit, bool(status.stdout.strip())


def _repository_unchanged(project_root: Path, commit: str) -> bool:
    """判断运行结束时 HEAD 未变化且 worktree 仍然干净。"""
    current, dirty = _repository_state(project_root)
    return current == commit and not dirty


def _pending_approval_count(database: Path) -> int:
    """只读统计所有旧 pending Approval，Runner 从不消费或决定它们。"""
    try:
        with Database(database).connect_read_only() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM approvals WHERE status = 'pending'"
                ).fetchone()[0]
            )
    except (DatabaseError, OSError, sqlite3.Error):
        raise FeishuLiveError("approval_state_unavailable") from None


async def _execute_live_cases(
    preflight: LivePreflight,
    *,
    gateway_timeout: float,
    case_timeout: float,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> LiveExecution:
    """启动唯一 production Gateway，逐案取证，并在任何路径 finally 停止。"""
    gateway = await GatewayProcess.start(
        project_root=preflight.project_root,
        home=preflight.paths.home,
        ready_timeout=gateway_timeout,
    )
    all_graceful = True
    results: list[FeishuCaseResult] = []

    async def restart_gateway() -> None:
        """为 restart case 进行一次有界 stop/start，保留同一数据库与配置。"""
        nonlocal gateway, all_graceful
        exit_code = await gateway.stop()
        all_graceful = all_graceful and exit_code == 0
        gateway = await GatewayProcess.start(
            project_root=preflight.project_root,
            home=preflight.paths.home,
            ready_timeout=gateway_timeout,
        )

    try:
        output_fn("MiniClaw Feishu Live E2E")
        output_fn("Use only the configured Owner DM and dedicated test group.")
        for case in preflight.cases:
            result = await _run_case(
                case=case,
                database=preflight.paths.database,
                workspace=preflight.config.workspace.path,
                gateway=gateway,
                case_timeout=case_timeout,
                input_fn=input_fn,
                output_fn=output_fn,
                restart_fn=restart_gateway if case.id == "FEISHU-LIVE-013" else None,
            )
            results.append(result)
    finally:
        exit_code = await gateway.stop()
        all_graceful = all_graceful and exit_code == 0
    return LiveExecution(tuple(results), True, all_graceful)


async def _run_case(
    *,
    case: Any,
    database: Path,
    workspace: Path,
    gateway: Any,
    case_timeout: float,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    restart_fn: Callable[[], Any] | None = None,
) -> FeishuCaseResult:
    """执行单 case 的 checkpoint→人工动作→自动证据→人工证据顺序。"""
    _prepare_case_files(workspace, case.setup_files)
    checkpoint = capture_checkpoint(database)
    output_fn(f"\n{case.id}: {case.title}")
    requirements = tuple(case.expected.live_local_evidence)
    human_requirements = tuple(case.expected.live_human_evidence)

    if case.id not in {"FEISHU-LIVE-001", "FEISHU-LIVE-015"}:
        actions = (case.query, *case.turns)
        for index, action in enumerate(actions):
            if restart_fn is not None:
                await restart_fn()
            output_fn(f"Action {index + 1}: {action}")
            if _read_action(input_fn) == "skip":
                return FeishuCaseResult(
                    case.id,
                    "skip",
                    (),
                    (),
                    tuple((key, "skip") for key in human_requirements),
                    "operator_skipped",
                )

    if requirements == ("gateway_ready",):
        evaluation = EvidenceEvaluation(
            ("gateway_ready",) if gateway.ready else (),
            () if gateway.ready else ("gateway_ready",),
        )
    elif requirements == ("secret_scan_zero",):
        evaluation = EvidenceEvaluation(("secret_scan_zero",), ())
    else:
        evaluation = await _wait_for_local_evidence(
            database=database,
            checkpoint=checkpoint,
            requirements=requirements,
            timeout=case_timeout,
        )
    if evaluation.failed:
        return FeishuCaseResult(
            case.id,
            "fail",
            evaluation.passed,
            evaluation.failed,
            (),
            "local_evidence_failed",
        )

    human_statuses = tuple(
        (key, _read_human_status(input_fn, key)) for key in human_requirements
    )
    statuses = tuple(status for _, status in human_statuses)
    if "fail" in statuses:
        status, error_code = "fail", "human_evidence_failed"
    elif "skip" in statuses:
        status, error_code = "skip", "operator_skipped"
    else:
        status, error_code = "pass", None
    return FeishuCaseResult(
        case.id,
        status,
        evaluation.passed,
        (),
        human_statuses,
        error_code,
    )


async def _wait_for_local_evidence(
    *,
    database: Path,
    checkpoint: DatabaseCheckpoint,
    requirements: tuple[str, ...],
    timeout: float,
) -> EvidenceEvaluation:
    """在有限窗口内轮询正证据；静默证据必须等待完整窗口后再判断。"""
    if "no_new_turn" in requirements:
        await asyncio.sleep(timeout)
        return evaluate_local_evidence(database, checkpoint, requirements)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    latest = EvidenceEvaluation((), requirements)
    while True:
        latest = evaluate_local_evidence(database, checkpoint, requirements)
        if not latest.failed or loop.time() >= deadline:
            return latest
        await asyncio.sleep(min(0.25, max(0.0, deadline - loop.time())))


def _read_action(input_fn: Callable[[str], str]) -> str:
    """等待操作者完成动作，只允许 Enter 或明确 skip。"""
    while True:
        try:
            value = input_fn(
                "Complete the action, press Enter; or enter s to skip: "
            ).strip().lower()
        except (EOFError, StopIteration):
            return "skip"
        if value == "":
            return "continue"
        if value == "s":
            return "skip"


def _read_human_status(input_fn: Callable[[str], str], key: str) -> str:
    """为一个 human evidence key 只接受 p/f/s。"""
    while True:
        try:
            value = input_fn(f"{key} [p/f/s]: ").strip().lower()
        except (EOFError, StopIteration):
            return "skip"
        if value in {"p", "f", "s"}:
            return {"p": "pass", "f": "fail", "s": "skip"}[value]


def _prepare_case_files(workspace: Path, files: Sequence[tuple[str, str]]) -> None:
    """安全准备版本化合成 fixture；已有内容不一致时拒绝覆盖。"""
    try:
        root = workspace.resolve(strict=True)
    except (OSError, RuntimeError):
        raise FeishuLiveError("workspace_unavailable") from None
    for relative, content in files:
        target = root / relative
        try:
            lexical = Path(os.path.abspath(target))
            if not lexical.is_relative_to(root):
                raise FeishuLiveError("fixture_path_unsafe")
            current = root
            for part in lexical.relative_to(root).parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise FeishuLiveError("fixture_path_unsafe")
                current.mkdir(mode=0o700, exist_ok=True)
            if lexical.exists():
                if lexical.is_symlink() or not lexical.is_file():
                    raise FeishuLiveError("fixture_path_unsafe")
                if lexical.read_text(encoding="utf-8") != content:
                    raise FeishuLiveError("fixture_conflict")
                continue
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(lexical, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except FeishuLiveError:
            raise
        except (OSError, UnicodeError):
            raise FeishuLiveError("fixture_write_failed") from None


def _sensitive_values(preflight: LivePreflight) -> tuple[str, ...]:
    """只在内存汇总 Secret、外部 ID、正文和本机路径，供 exact scan 使用。"""
    feishu = preflight.config.channels.feishu
    candidates: list[object] = [
        preflight.secrets.model_api_key,
        preflight.secrets.feishu_app_id,
        *preflight.secrets.channel_tokens.values(),
        feishu.owner_open_id,
        *feishu.allowed_open_ids,
        *feishu.allowed_chat_ids,
        str(preflight.paths.home),
        str(Path.home()),
    ]
    for case in preflight.cases:
        candidates.extend((case.query, *case.turns))
        candidates.extend(content for _, content in getattr(case, "setup_files", ()))
    return tuple(
        dict.fromkeys(
            value
            for value in candidates
            if isinstance(value, str) and len(value.encode("utf-8")) >= 4
        )
    )


def _record_runtime_failure(
    results: tuple[FeishuCaseResult, ...],
    error_code: str,
) -> tuple[FeishuCaseResult, ...]:
    """把 Gateway/runner 稳定错误绑定到首个缺失 case，不保存 diagnostics。"""
    if not _is_safe_error_code(error_code):
        error_code = "live_runtime_failed"
    existing = {result.case_id for result in results}
    case_id = next(
        (
            f"FEISHU-LIVE-{index:03d}"
            for index in range(1, 16)
            if f"FEISHU-LIVE-{index:03d}" not in existing
        ),
        "FEISHU-LIVE-015",
    )
    if case_id in existing:
        return _force_case_failure(
            results,
            case_id=case_id,
            evidence_key="secret_scan_zero",
            error_code=error_code,
        )
    evidence = "gateway_ready" if case_id == "FEISHU-LIVE-001" else "secret_scan_zero"
    return (
        *results,
        FeishuCaseResult(case_id, "fail", (), (evidence,), (), error_code),
    )


def _force_case_failure(
    results: tuple[FeishuCaseResult, ...],
    *,
    case_id: str,
    evidence_key: str,
    error_code: str,
) -> tuple[FeishuCaseResult, ...]:
    """以稳定错误码把一个已有或缺失 case 降级为失败。"""
    replaced: list[FeishuCaseResult] = []
    found = False
    for result in results:
        if result.case_id != case_id:
            replaced.append(result)
            continue
        found = True
        passed = tuple(key for key in result.local_passed if key != evidence_key)
        failed = tuple(dict.fromkeys((*result.local_failed, evidence_key)))
        replaced.append(
            FeishuCaseResult(case_id, "fail", passed, failed, result.human_statuses, error_code)
        )
    if not found:
        replaced.append(FeishuCaseResult(case_id, "fail", (), (evidence_key,), (), error_code))
    return tuple(replaced)


def _failed_secret_case(error_code: str) -> FeishuCaseResult:
    """构造缺失 015 时的最终隐私失败结果。"""
    return FeishuCaseResult(
        "FEISHU-LIVE-015",
        "fail",
        (),
        ("secret_scan_zero",),
        (),
        error_code,
    )


def _prepare_output_directory(path: Path) -> None:
    """创建本地 ignored Evidence 目录并拒绝最终路径 symlink。"""
    try:
        if path.is_symlink():
            raise FeishuLiveError("evidence_directory_unsafe")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.is_dir() or path.is_symlink():
            raise FeishuLiveError("evidence_directory_unsafe")
    except FeishuLiveError:
        raise
    except OSError:
        raise FeishuLiveError("evidence_write_failed") from None


def _utc_timestamp() -> str:
    """返回 Evidence 契约接受的微秒 UTC 时间。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _filename_timestamp(value: str) -> str:
    """把已验证 UTC 时间转换成不冲突的安全文件名。"""
    return value.replace("-", "").replace(":", "").replace(".", "").removesuffix("Z") + "Z"


def build_evidence_report(
    *,
    commit: str,
    started_at: str,
    finished_at: str,
    gateway_ready: bool,
    gateway_graceful_exit: bool,
    results: Sequence[FeishuCaseResult],
    secret_matches: int,
) -> dict[str, object]:
    """把封闭 case 结果转成不含正文和平台 ID 的 Evidence 报告。

    Secret scan 命中会强制把 ``FEISHU-LIVE-015`` 与发布结果改为失败，人工结果
    无权覆盖这一结论。
    """
    if not _is_commit(commit) or not _is_timestamp(started_at) or not _is_timestamp(finished_at):
        raise FeishuLiveError("invalid_evidence_report")
    if type(gateway_ready) is not bool or type(gateway_graceful_exit) is not bool:
        raise FeishuLiveError("invalid_evidence_report")
    if type(secret_matches) is not int or secret_matches < 0:
        raise FeishuLiveError("invalid_evidence_report")

    normalized = [_validate_case_result(result) for result in results]
    case_ids = [result.case_id for result in normalized]
    if len(case_ids) != len(set(case_ids)):
        raise FeishuLiveError("invalid_evidence_report")
    if secret_matches:
        normalized = [_force_secret_failure(result) for result in normalized]

    checks = [_case_result_payload(result) for result in normalized]
    counts = _report_counts(normalized, secret_matches)
    expected_ids = {f"FEISHU-LIVE-{index:03d}" for index in range(1, 16)}
    if secret_matches or not gateway_ready or not gateway_graceful_exit:
        release_status = "FEISHU_LIVE_FAILED"
    elif any(result.status == "fail" for result in normalized):
        release_status = "FEISHU_LIVE_FAILED"
    elif set(case_ids) == expected_ids and all(result.status == "pass" for result in normalized):
        release_status = "FEISHU_E2E_VERIFIED"
    else:
        release_status = "FEISHU_LIVE_PARTIAL"

    report: dict[str, object] = {
        "schema_version": 1,
        "channel": "feishu",
        "commit": commit,
        "started_at": started_at,
        "finished_at": finished_at,
        "gateway": {"ready": gateway_ready, "graceful_exit": gateway_graceful_exit},
        "checks": checks,
        "counts": counts,
        "release_status": release_status,
    }
    if not _is_valid_report(report):
        raise FeishuLiveError("invalid_evidence_report")
    return report


def write_evidence(path: Path, report: Mapping[str, object]) -> None:
    """以 0600、O_EXCL 和 fsync 写入一份经过严格验证的新 Evidence。"""
    if not _is_valid_report(report):
        raise FeishuLiveError("invalid_evidence_report")
    try:
        rendered = json.dumps(
            report,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError):
        raise FeishuLiveError("invalid_evidence_report") from None

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise FeishuLiveError("evidence_already_exists") from None
    except OSError:
        raise FeishuLiveError("evidence_write_failed") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise FeishuLiveError("evidence_write_failed") from None


def scan_secret_matches(paths: Sequence[Path], secrets: Sequence[str]) -> int:
    """有界扫描普通小文件并只返回 exact secret 的匿名命中次数。"""
    if any(not isinstance(secret, str) or not 1 <= len(secret) <= 4096 for secret in secrets):
        raise FeishuLiveError("invalid_secret_scan")
    needles = tuple(dict.fromkeys(secret.encode("utf-8") for secret in secrets))
    if not needles:
        return 0

    matches = 0
    visited = 0
    for candidate in _iter_scan_candidates(paths):
        if visited >= _MAX_SCAN_FILES:
            break
        visited += 1
        content = _read_bounded_regular_file(candidate)
        if content is None:
            continue
        matches += sum(content.count(needle) for needle in needles)
    return matches


def _validate_case_result(result: FeishuCaseResult) -> FeishuCaseResult:
    """验证一个 case 结果只使用封闭 ID、状态、evidence 和错误码。"""
    if not isinstance(result, FeishuCaseResult):
        raise FeishuLiveError("invalid_evidence_report")
    if not re.fullmatch(r"FEISHU-LIVE-(?:00[1-9]|01[0-5])", result.case_id):
        raise FeishuLiveError("invalid_evidence_report")
    if result.status not in _CASE_STATUSES:
        raise FeishuLiveError("invalid_evidence_report")
    local = (*result.local_passed, *result.local_failed)
    if len(local) != len(set(local)) or any(key not in _EVIDENCE_CHECKS for key in local):
        raise FeishuLiveError("invalid_evidence_report")
    human_keys = tuple(key for key, _ in result.human_statuses)
    if len(human_keys) != len(set(human_keys)):
        raise FeishuLiveError("invalid_evidence_report")
    if any(
        key not in _HUMAN_EVIDENCE or status not in _CASE_STATUSES
        for key, status in result.human_statuses
    ):
        raise FeishuLiveError("invalid_evidence_report")
    if result.error_code is not None and not _is_safe_error_code(result.error_code):
        raise FeishuLiveError("invalid_evidence_report")
    if result.status == "pass" and (
        result.local_failed
        or any(status != "pass" for _, status in result.human_statuses)
        or result.error_code is not None
    ):
        raise FeishuLiveError("invalid_evidence_report")
    return result


def _force_secret_failure(result: FeishuCaseResult) -> FeishuCaseResult:
    """只重写 015 的封闭 secret scan 结论，不触碰其他 case。"""
    if result.case_id != "FEISHU-LIVE-015":
        return result
    passed = tuple(key for key in result.local_passed if key != "secret_scan_zero")
    failed = tuple(dict.fromkeys((*result.local_failed, "secret_scan_zero")))
    return FeishuCaseResult(
        case_id=result.case_id,
        status="fail",
        local_passed=passed,
        local_failed=failed,
        human_statuses=result.human_statuses,
        error_code="secret_scan_match",
    )


def _case_result_payload(result: FeishuCaseResult) -> dict[str, object]:
    """把验证后的结果转换成固定 nested schema。"""
    local = [
        {"key": key, "status": status}
        for status, keys in (("pass", result.local_passed), ("fail", result.local_failed))
        for key in keys
    ]
    human = [{"key": key, "status": status} for key, status in result.human_statuses]
    return {
        "case_id": result.case_id,
        "status": result.status,
        "local_evidence": local,
        "human_evidence": human,
        "error_code": result.error_code,
    }


def _report_counts(results: Sequence[FeishuCaseResult], secret_matches: int) -> dict[str, int]:
    """从封闭结果派生匿名计数，调用方不能注入字段名。"""
    return {
        "cases_total": len(results),
        "cases_passed": sum(result.status == "pass" for result in results),
        "cases_failed": sum(result.status == "fail" for result in results),
        "cases_skipped": sum(result.status == "skip" for result in results),
        "local_evidence_passed": sum(len(result.local_passed) for result in results),
        "local_evidence_failed": sum(len(result.local_failed) for result in results),
        "human_evidence_passed": sum(
            status == "pass" for result in results for _, status in result.human_statuses
        ),
        "human_evidence_failed": sum(
            status == "fail" for result in results for _, status in result.human_statuses
        ),
        "human_evidence_skipped": sum(
            status == "skip" for result in results for _, status in result.human_statuses
        ),
        "secret_matches": secret_matches,
    }


def _is_valid_report(report: Mapping[str, object]) -> bool:
    """递归验证 Evidence 所有对象的精确字段和安全值。"""
    if set(report) != _REPORT_KEYS:
        return False
    if report.get("schema_version") != 1 or report.get("channel") != "feishu":
        return False
    if not _is_commit(report.get("commit")):
        return False
    if not _is_timestamp(report.get("started_at")) or not _is_timestamp(report.get("finished_at")):
        return False
    if report.get("release_status") not in _RELEASE_STATUSES:
        return False
    gateway = report.get("gateway")
    if not isinstance(gateway, Mapping) or set(gateway) != {"ready", "graceful_exit"}:
        return False
    if any(type(gateway[key]) is not bool for key in ("ready", "graceful_exit")):
        return False
    counts = report.get("counts")
    if not isinstance(counts, Mapping) or set(counts) != _COUNT_KEYS:
        return False
    if any(type(value) is not int or value < 0 for value in counts.values()):
        return False
    checks = report.get("checks")
    if not isinstance(checks, list) or not all(_is_valid_check_payload(check) for check in checks):
        return False
    case_ids = [check["case_id"] for check in checks]
    if len(case_ids) != len(set(case_ids)):
        return False
    expected_counts = _payload_counts(checks, int(counts["secret_matches"]))
    if dict(counts) != expected_counts:
        return False
    if counts["secret_matches"]:
        case_fifteen = next(
            (check for check in checks if check["case_id"] == "FEISHU-LIVE-015"),
            None,
        )
        if case_fifteen is None or case_fifteen["status"] != "fail":
            return False
        if {"key": "secret_scan_zero", "status": "fail"} not in case_fifteen["local_evidence"]:
            return False
    expected_release = _payload_release_status(checks, gateway, int(counts["secret_matches"]))
    return report["release_status"] == expected_release


def _is_valid_check_payload(check: object) -> bool:
    """验证已经序列化的单 case nested schema。"""
    if not isinstance(check, Mapping):
        return False
    if set(check) != {"case_id", "status", "local_evidence", "human_evidence", "error_code"}:
        return False
    if not isinstance(check.get("case_id"), str) or not re.fullmatch(
        r"FEISHU-LIVE-(?:00[1-9]|01[0-5])", check["case_id"]
    ):
        return False
    if check.get("status") not in _CASE_STATUSES:
        return False
    error_code = check.get("error_code")
    if error_code is not None and not _is_safe_error_code(error_code):
        return False
    local = check.get("local_evidence")
    human = check.get("human_evidence")
    if not _is_valid_evidence_payload(local, _EVIDENCE_CHECKS) or not _is_valid_evidence_payload(
        human, _HUMAN_EVIDENCE
    ):
        return False
    if check["status"] == "pass" and (
        error_code is not None
        or any(item["status"] != "pass" for item in local)
        or any(item["status"] != "pass" for item in human)
    ):
        return False
    return True


def _is_valid_evidence_payload(payload: object, allowed: Container[str]) -> bool:
    """验证 evidence 数组只有 key/status，且 key 不重复。"""
    if not isinstance(payload, list):
        return False
    keys: list[str] = []
    for item in payload:
        if not isinstance(item, Mapping) or set(item) != {"key", "status"}:
            return False
        key = item.get("key")
        if (
            not isinstance(key, str)
            or key not in allowed
            or item.get("status") not in _CASE_STATUSES
        ):
            return False
        keys.append(key)
    return len(keys) == len(set(keys))


def _payload_counts(checks: list[Mapping[str, object]], secret_matches: int) -> dict[str, int]:
    """从已验证 JSON payload 重新推导计数，用于拒绝报告篡改。"""
    return {
        "cases_total": len(checks),
        "cases_passed": sum(check["status"] == "pass" for check in checks),
        "cases_failed": sum(check["status"] == "fail" for check in checks),
        "cases_skipped": sum(check["status"] == "skip" for check in checks),
        "local_evidence_passed": sum(
            item["status"] == "pass" for check in checks for item in check["local_evidence"]
        ),
        "local_evidence_failed": sum(
            item["status"] == "fail" for check in checks for item in check["local_evidence"]
        ),
        "human_evidence_passed": sum(
            item["status"] == "pass" for check in checks for item in check["human_evidence"]
        ),
        "human_evidence_failed": sum(
            item["status"] == "fail" for check in checks for item in check["human_evidence"]
        ),
        "human_evidence_skipped": sum(
            item["status"] == "skip" for check in checks for item in check["human_evidence"]
        ),
        "secret_matches": secret_matches,
    }


def _payload_release_status(
    checks: list[Mapping[str, object]],
    gateway: Mapping[str, object],
    secret_matches: int,
) -> str:
    """从已验证 payload 重新推导发布判定。"""
    if secret_matches or not gateway["ready"] or not gateway["graceful_exit"]:
        return "FEISHU_LIVE_FAILED"
    if any(check["status"] == "fail" for check in checks):
        return "FEISHU_LIVE_FAILED"
    expected = {f"FEISHU-LIVE-{index:03d}" for index in range(1, 16)}
    if {check["case_id"] for check in checks} == expected and all(
        check["status"] == "pass" for check in checks
    ):
        return "FEISHU_E2E_VERIFIED"
    return "FEISHU_LIVE_PARTIAL"


def _is_safe_error_code(value: object) -> bool:
    """接受稳定小写错误码，同时拒绝飞书外部 ID 前缀。"""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value) is not None
        and not value.startswith(("ou_", "oc_", "om_"))
    )


def _is_commit(value: object) -> bool:
    """判断是否是完整小写 Git SHA-1。"""
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _is_timestamp(value: object) -> bool:
    """判断是否是不含本地路径信息的 UTC ISO-8601 字符串。"""
    return isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z", value
    ) is not None


def _iter_scan_candidates(paths: Sequence[Path]) -> Iterator[Path]:
    """按稳定顺序枚举普通文件候选，从不跟随目录或文件 symlink。"""
    for root in sorted(paths, key=lambda item: str(item)):
        try:
            if root.is_symlink():
                continue
            if root.is_file():
                yield root
                continue
            if not root.is_dir():
                continue
        except OSError:
            continue
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            base = Path(directory)
            directory_names[:] = sorted(
                name for name in directory_names if not (base / name).is_symlink()
            )
            for name in sorted(file_names):
                yield base / name


def _read_bounded_regular_file(path: Path) -> bytes | None:
    """使用 no-follow fd 读取至多 1 MiB，竞态变大时安全跳过。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SCAN_FILE_BYTES:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            content = handle.read(_MAX_SCAN_FILE_BYTES + 1)
        if len(content) > _MAX_SCAN_FILE_BYTES:
            return None
        return content
    except OSError:
        return None
    finally:
        os.close(descriptor)


class GatewayProcess:
    """持续排空输出、按精确 marker 就绪并有界退出的 Gateway 子进程。"""

    _READY_LINE = "MiniClaw gateway ready: feishu/default"
    _DIAGNOSTIC_LINES = 200
    _DIAGNOSTIC_CHARS = 4096

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        """保存子进程；生产调用方应通过 :meth:`start` 创建实例。"""
        self._process = process
        self._ready_event = asyncio.Event()
        self._ready = False
        self._diagnostics: deque[str] = deque(maxlen=self._DIAGNOSTIC_LINES)
        assert process.stdout is not None
        assert process.stderr is not None
        self._drain_tasks = (
            asyncio.create_task(self._drain(process.stdout, "stdout")),
            asyncio.create_task(self._drain(process.stderr, "stderr")),
        )

    @classmethod
    async def start(
        cls,
        *,
        project_root: Path,
        home: Path,
        ready_timeout: float,
        command: tuple[str, ...] | None = None,
    ) -> "GatewayProcess":
        """启动 Gateway，并等待精确的 Feishu ready marker。

        Args:
            project_root: 子进程工作目录。
            home: 传给 MiniClaw CLI 的状态目录。
            ready_timeout: 等待 ready marker 的最长秒数。
            command: 测试专用显式命令；省略时启动当前 Python 的 MiniClaw。

        Raises:
            FeishuLiveError: 子进程提前结束、未按时就绪或无法启动。
        """
        executable = command or (
            sys.executable,
            "-m",
            "miniclaw",
            "--home",
            str(home),
            "gateway",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *executable,
                cwd=project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
        except (OSError, ValueError):
            raise FeishuLiveError("gateway_start_failed") from None

        gateway = cls(process)
        ready_wait = asyncio.create_task(gateway._ready_event.wait())
        exit_wait = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_wait, exit_wait),
                timeout=ready_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_wait in done and process.returncode is None:
                gateway._ready = True
                return gateway
            failure = (
                "gateway_exited_before_ready"
                if exit_wait in done
                else "gateway_ready_timeout"
            )
            await gateway._stop_after_failed_start()
            raise FeishuLiveError(failure)
        finally:
            for waiter in (ready_wait, exit_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(ready_wait, exit_wait, return_exceptions=True)

    @property
    def ready(self) -> bool:
        """返回当前实例是否见过精确 ready marker。"""
        return self._ready

    @property
    def bounded_diagnostics(self) -> tuple[str, ...]:
        """返回最多 200 行、每行最多 4096 字符的内存诊断快照。"""
        return tuple(self._diagnostics)

    async def stop(self, *, timeout: float = 10.0) -> int:
        """最多发送两次 SIGTERM，并等待子进程和输出管道结束。

        自动化验收刻意不发送 SIGKILL；第二次等待后仍不退出时，由操作者决定。
        """
        if self._process.returncode is None:
            self._send_sigterm()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
            except TimeoutError:
                self._send_sigterm()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=timeout)
                except TimeoutError:
                    raise FeishuLiveError("gateway_shutdown_timeout") from None
        await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        if self._process.returncode is None:
            raise FeishuLiveError("gateway_shutdown_timeout")
        return self._process.returncode

    async def _drain(self, stream: asyncio.StreamReader, source: str) -> None:
        """持续排空一个 pipe，并只保存有界单行诊断。"""
        while line_bytes := await stream.readline():
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if source == "stdout" and line == self._READY_LINE:
                self._ready_event.set()
            rendered = f"{source}:{line}"[: self._DIAGNOSTIC_CHARS]
            self._diagnostics.append(rendered)

    def _send_sigterm(self) -> None:
        """优先终止整个子进程组，平台不支持时退回单进程 terminate。"""
        if self._process.returncode is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            try:
                self._process.terminate()
            except ProcessLookupError:
                return

    async def _stop_after_failed_start(self) -> None:
        """启动失败时尽力回收子进程，不用内部诊断覆盖稳定错误码。"""
        try:
            await self.stop(timeout=1.0)
        except FeishuLiveError:
            return


type _EvidenceCheck = Callable[[sqlite3.Connection, DatabaseCheckpoint], bool]


def capture_checkpoint(database: Path) -> DatabaseCheckpoint:
    """只读捕获当前最大内部 ID，旧运行不能满足新案例。

    Args:
        database: 已初始化的 MiniClaw SQLite 文件。

    Returns:
        六张事实表的最大内部 ID。

    Raises:
        FeishuLiveError: 数据库不存在、损坏或无法只读查询。
    """
    try:
        with Database(database).connect_read_only() as connection:
            return DatabaseCheckpoint(
                processed_event_rowid=_maximum(connection, "processed_events", "rowid"),
                turn_id=_maximum(connection, "turns", "id"),
                tool_run_id=_maximum(connection, "tool_runs", "id"),
                approval_id=_maximum(connection, "approvals", "id"),
                delivery_id=_maximum(connection, "deliveries", "id"),
                audit_event_id=_maximum(connection, "audit_events", "id"),
                pending_approval_ids=tuple(
                    int(row[0])
                    for row in connection.execute(
                        "SELECT id FROM approvals WHERE status = 'pending' ORDER BY id"
                    )
                ),
            )
    except (DatabaseError, OSError, sqlite3.Error):
        raise FeishuLiveError("evidence_database_unavailable") from None


def evaluate_local_evidence(
    database: Path,
    checkpoint: DatabaseCheckpoint,
    requirements: tuple[str, ...],
) -> EvidenceEvaluation:
    """只读判断 checkpoint 后的 Feishu 状态是否满足封闭证据集合。

    Args:
        database: MiniClaw SQLite 文件。
        checkpoint: 人工动作前捕获的内部 ID。
        requirements: 需要按原顺序判断的证据 key。

    Returns:
        已满足与未满足 key，均保持输入顺序。

    Raises:
        FeishuLiveError: key 未注册或数据库无法只读查询。
    """
    if any(requirement not in _EVIDENCE_CHECKS for requirement in requirements):
        raise FeishuLiveError("unknown_local_evidence")
    passed: list[str] = []
    failed: list[str] = []
    try:
        with Database(database).connect_read_only() as connection:
            for requirement in requirements:
                target = passed if _EVIDENCE_CHECKS[requirement](connection, checkpoint) else failed
                target.append(requirement)
    except (DatabaseError, OSError, sqlite3.Error, ValueError):
        raise FeishuLiveError("evidence_database_unavailable") from None
    return EvidenceEvaluation(tuple(passed), tuple(failed))


def _maximum(connection: sqlite3.Connection, table: str, column: str) -> int:
    """读取固定事实表的最大内部整数，不接受外部输入。"""
    allowed = {
        ("processed_events", "rowid"),
        ("turns", "id"),
        ("tool_runs", "id"),
        ("approvals", "id"),
        ("deliveries", "id"),
        ("audit_events", "id"),
    }
    if (table, column) not in allowed:
        raise ValueError("unsupported checkpoint table")
    row = connection.execute(f"SELECT COALESCE(MAX({column}), 0) FROM {table}").fetchone()
    return int(row[0])


def _has_completed_inbox(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断是否出现新的 completed Feishu Inbox。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM processed_events
        WHERE rowid > ? AND channel = 'feishu' AND status = 'completed'
        LIMIT 1
        """,
        (checkpoint.processed_event_rowid,),
    )


def _has_completed_turn(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断是否出现绑定 Feishu Session 的 completed Turn。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu' AND t.status = 'completed'
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _has_sent_delivery(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断是否出现新的 sent Feishu Delivery。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM deliveries
        WHERE id > ? AND channel = 'feishu' AND status = 'sent'
        LIMIT 1
        """,
        (checkpoint.delivery_id,),
    )


def _has_succeeded_tool(tool_name: str) -> _EvidenceCheck:
    """构造只匹配一个固定 Tool 名的成功检查。"""

    def check(connection: sqlite3.Connection, checkpoint: DatabaseCheckpoint) -> bool:
        return _exists(
            connection,
            """
            SELECT 1 FROM tool_runs AS r
            JOIN turns AS t ON t.id = r.turn_id
            JOIN sessions AS s ON s.id = t.session_id
            WHERE r.id > ? AND s.channel = 'feishu'
              AND r.tool_name = ? AND r.status = 'succeeded'
            LIMIT 1
            """,
            (checkpoint.tool_run_id, tool_name),
        )

    return check


def _has_three_completed_turns_in_one_session(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断同一 Feishu Session 是否完成至少三个新 Turn。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu' AND t.status = 'completed'
        GROUP BY t.session_id HAVING COUNT(*) >= 3
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _has_approval(status: str, tool_status: str) -> _EvidenceCheck:
    """构造审批和绑定 ToolRun 必须同时满足的检查。"""

    def check(connection: sqlite3.Connection, checkpoint: DatabaseCheckpoint) -> bool:
        if _exists(
            connection,
            """
            SELECT 1 FROM approvals AS a
            JOIN tool_runs AS r ON r.id = a.tool_run_id
            JOIN turns AS t ON t.id = a.turn_id
            JOIN sessions AS s ON s.id = t.session_id
            WHERE a.id > ? AND s.channel = 'feishu'
              AND a.status = ? AND r.status = ?
            LIMIT 1
            """,
            (checkpoint.approval_id, status, tool_status),
        ):
            return True
        if status != "consumed":
            return False
        return any(
            _exists(
                connection,
                """
                SELECT 1 FROM approvals AS a
                JOIN tool_runs AS r ON r.id = a.tool_run_id
                JOIN turns AS t ON t.id = a.turn_id
                JOIN sessions AS s ON s.id = t.session_id
                WHERE a.id = ? AND s.channel = 'feishu'
                  AND a.status = 'consumed' AND r.status = ?
                LIMIT 1
                """,
                (approval_id, tool_status),
            )
            for approval_id in checkpoint.pending_approval_ids
        )

    return check


def _has_no_new_turn(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断 checkpoint 后没有任何 Feishu Turn。"""
    return not _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu'
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _has_multiple_sent_parts(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断一个新 Feishu Message 是否有连续且全部 sent 的多分片。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM deliveries
        WHERE id > ? AND channel = 'feishu'
        GROUP BY message_id
        HAVING COUNT(*) >= 2
           AND SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) = COUNT(*)
           AND MIN(part_index) = 0
           AND MAX(part_index) = COUNT(*) - 1
        LIMIT 1
        """,
        (checkpoint.delivery_id,),
    )


def _has_gateway_ready(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断 checkpoint 后是否记录 Feishu supervisor ready。"""
    return _audit_count(
        connection,
        checkpoint,
        "channel.supervisor.ready",
    ) >= 1


def _has_transport_reconnected(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断真实连接是否先 reconnecting 后再次 connected。"""
    return (
        _audit_count(connection, checkpoint, "channel.transport.reconnecting") >= 1
        and _audit_count(connection, checkpoint, "channel.transport.connected") >= 1
    )


def _has_memory_restart_shape(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断两次 ready 之间同一 Session 至少完成两个新 Turn。"""
    if _audit_count(connection, checkpoint, "channel.supervisor.ready") < 2:
        return False
    return _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu' AND t.status = 'completed'
        GROUP BY t.session_id HAVING COUNT(*) >= 2
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _audit_count(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
    event_type: str,
) -> int:
    """按解析后的安全 metadata 统计 Feishu Audit，不做字符串猜测。"""
    rows = connection.execute(
        """
        SELECT metadata_json FROM audit_events
        WHERE id > ? AND event_type = ? ORDER BY id
        """,
        (checkpoint.audit_event_id, event_type),
    ).fetchall()
    count = 0
    for row in rows:
        metadata = json.loads(str(row["metadata_json"]))
        if isinstance(metadata, dict) and metadata.get("channel") == "feishu":
            count += 1
    return count


def _exists(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> bool:
    """执行固定只读查询并判断是否至少有一行。"""
    return connection.execute(statement, parameters).fetchone() is not None


def _unsupported_until_secret_scan(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """Secret scan 由 Evidence 阶段执行，数据库本身不能证明无泄露。"""
    del connection, checkpoint
    return False


_EVIDENCE_CHECKS: dict[str, _EvidenceCheck] = {
    "gateway_ready": _has_gateway_ready,
    "inbox_completed": _has_completed_inbox,
    "turn_completed": _has_completed_turn,
    "delivery_sent": _has_sent_delivery,
    "one_session_three_turns": _has_three_completed_turns_in_one_session,
    "system_info_succeeded": _has_succeeded_tool("system_info"),
    "read_file_succeeded": _has_succeeded_tool("read_file"),
    "approval_pending": _has_approval("pending", "waiting_approval"),
    "approval_consumed_once": _has_approval("consumed", "succeeded"),
    "approval_denied": _has_approval("denied", "denied"),
    "no_new_turn": _has_no_new_turn,
    "multiple_parts_sent": _has_multiple_sent_parts,
    "memory_survived_restart": _has_memory_restart_shape,
    "transport_reconnected": _has_transport_reconnected,
    "secret_scan_zero": _unsupported_until_secret_scan,
}
