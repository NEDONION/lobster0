"""复用既有离线 Eval Runner，为一个不可变 ProposalVersion 生成绑定哈希的 receipt。

范围说明：这里实现的是文档第 9 节 Eval Gate 中"确定性全量回归 + 安全子集 + 预算"这一条
可以在当前 Core 上真实执行的路径。两项前提尚不存在，故意没有假装实现：

* **failure case**：由 ``/bad`` 反馈自动生成可执行 case 需要脚本化的 Provider 响应序列，
  而反馈记录里只有脱敏后的自然语言，无法凭空合成；
* **baseline / candidate 对比**：候选真正生效依赖 Task 5 的 active revision overlay，
  当前 ``run_offline_suite`` 跑的始终是本机现状，跑两遍只会得到同一个结果。

因此本模块产出的 receipt 只声明"该 commit 的确定性回归在候选评测时点全绿"，不声明
"候选修复了那条反馈"。
"""

import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from lobster0 import __version__
from lobster0.evals.cases import EvalCase, EvalCaseError, load_cases
from lobster0.evals.runner import EvalSuiteResult, run_offline_suite
from lobster0.evolution.models import EvalCaseStatus, EvalRun, EvalRunStatus
from lobster0.evolution.repository import EvalRepository

_SAFETY_CAPABILITY = "safety"
_RECEIPT_VERSION = 1

type SuiteRunner = Callable[[tuple[EvalCase, ...]], Awaitable[EvalSuiteResult]]


class EvaluationError(RuntimeError):
    """表示评测输入不可用；不包含 case 正文或本机路径。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和安全消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class EvaluationBudget:
    """保存一次评测允许的确定性上界。

    Args:
        max_total_duration_ms: 整个 suite 的墙钟上限；``None`` 表示不设时间预算。

    刻意不包含 token / 费用预算：离线 suite 使用脚本化 Provider 响应，其 token 数是
    case 文件里的固定常量而不是真实用量，拿它当预算会产生一个看起来在把关、实际恒真的门。
    真实 token / 费用预算属于 live Provider 证据路径，当前不存在。
    """

    max_total_duration_ms: int | None = None

    def __post_init__(self) -> None:
        """拒绝可被 bool 绕过的非正上限。"""
        if self.max_total_duration_ms is not None and (
            type(self.max_total_duration_ms) is not int or self.max_total_duration_ms <= 0
        ):
            raise ValueError("max_total_duration_ms must be a positive integer")


@dataclass(frozen=True, slots=True)
class GateOutcome:
    """描述确定性 Gate 的判定与全部违规码。"""

    passed: bool
    violations: tuple[str, ...]
    total_cases: int
    passed_cases: int
    safety_failures: int
    duration_ms: int


@dataclass(frozen=True, slots=True)
class EvalReceipt:
    """保存一次评测的封闭结论与可复核绑定哈希。"""

    proposal_version_id: int
    suite_manifest_hash: str
    receipt_hash: str
    runner_version: str
    outcome: GateOutcome


def suite_manifest_hash(root: Path) -> str:
    """对场景目录下全部 JSONL 文件的名称与内容计算稳定 manifest 哈希。

    Args:
        root: versioned 场景目录，例如 ``evals/scenarios``。

    Returns:
        小写 64 位十六进制摘要；任何 case 的增删改都会改变它。

    Raises:
        EvaluationError: 目录不存在或文件不可读。
    """
    if not root.is_dir():
        raise EvaluationError("suite_root_invalid", "eval suite root is not a directory")
    entries: list[tuple[str, str]] = []
    try:
        for path in sorted(root.glob("*.jsonl"), key=lambda item: item.name):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append((path.name, digest))
    except OSError as error:
        raise EvaluationError("suite_root_unreadable", "eval suite is not readable") from error
    if not entries:
        raise EvaluationError("suite_empty", "eval suite contains no versioned case files")
    return hashlib.sha256(_canonical_json(entries).encode("utf-8")).hexdigest()


def case_result_hash(case_id: str, *, status: EvalCaseStatus, failures: tuple[str, ...]) -> str:
    """对单条 case 的判定计算稳定哈希，用于绑定进 receipt。"""
    payload = {"case_id": case_id, "status": status.value, "failures": sorted(failures)}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def eval_receipt_hash(
    *,
    proposal_version_id: int,
    suite_manifest_hash: str,
    case_result_hashes: tuple[str, ...],
    budget: EvaluationBudget,
    runner_version: str,
    outcome: GateOutcome,
) -> str:
    """绑定 version、suite manifest、逐 case 结果、预算与 Runner 版本。

    任何一项变化都会产生不同 receipt，从而使基于旧 receipt 的 Approval 失效。
    """
    payload = {
        "receipt_version": _RECEIPT_VERSION,
        "proposal_version_id": proposal_version_id,
        "suite_manifest_hash": suite_manifest_hash,
        "case_result_hashes": sorted(case_result_hashes),
        "budget": {"max_total_duration_ms": budget.max_total_duration_ms},
        "runner_version": runner_version,
        "outcome": {
            "passed": outcome.passed,
            "violations": sorted(outcome.violations),
            "total_cases": outcome.total_cases,
            "passed_cases": outcome.passed_cases,
            "safety_failures": outcome.safety_failures,
        },
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def evaluate_gate(
    cases: tuple[EvalCase, ...],
    suite: EvalSuiteResult,
    budget: EvaluationBudget,
) -> GateOutcome:
    """按"全量不下降 + 安全为 0 + 未超预算"判定确定性 Gate。

    Args:
        cases: 本次实际运行的 case 集合，用于识别安全子集。
        suite: 既有 Runner 返回的聚合结果。
        budget: 本次评测的确定性上界。

    Returns:
        含稳定违规码、按码排序的判定结果。
    """
    safety_ids = {case.id for case in cases if case.capability == _SAFETY_CAPABILITY}
    safety_failures = sum(
        1 for result in suite.cases if not result.passed and result.case_id in safety_ids
    )
    violations: list[str] = []
    if suite.total == 0:
        violations.append("suite_empty")
    if suite.failed:
        violations.append("regression_failed")
    if safety_failures:
        violations.append("safety_failed")
    if (
        budget.max_total_duration_ms is not None
        and suite.duration_ms > budget.max_total_duration_ms
    ):
        violations.append("duration_budget_exceeded")
    return GateOutcome(
        passed=not violations,
        violations=tuple(sorted(violations)),
        total_cases=suite.total,
        passed_cases=suite.passed,
        safety_failures=safety_failures,
        duration_ms=suite.duration_ms,
    )


async def evaluate_proposal_version(
    evals: EvalRepository,
    *,
    proposal_version_id: int,
    suite_root: Path,
    budget: EvaluationBudget | None = None,
    suite_runner: SuiteRunner | None = None,
) -> EvalReceipt:
    """为一个不可变 ProposalVersion 运行确定性回归并结算 EvalRun。

    Args:
        evals: 持久化 EvalRun 与逐 case 结果的 Repository。
        proposal_version_id: 被评测的 immutable version。
        suite_root: versioned 场景目录。
        budget: 确定性上界；省略时不设时间预算。
        suite_runner: 可注入的 suite 执行函数，默认使用既有 ``run_offline_suite``。

    Returns:
        含 Gate 判定与绑定哈希的 receipt；同时已写入 ``eval_runs``。

    Raises:
        EvaluationError: 场景目录不可用或 case 文件不合法。
    """
    selected_budget = budget or EvaluationBudget()
    manifest = suite_manifest_hash(suite_root)
    try:
        cases = load_cases(suite_root)
    except EvalCaseError as error:
        raise EvaluationError("suite_invalid", "eval suite could not be loaded") from error
    runnable = tuple(case for case in cases if case.status == "active")
    run = evals.start_run(
        proposal_version_id=proposal_version_id, suite_manifest_hash=manifest
    )
    started = time.monotonic()
    try:
        suite = await (suite_runner or run_offline_suite)(runnable)
    except Exception:
        evals.complete_run(
            run.id,
            status=EvalRunStatus.ERROR,
            receipt_hash=None,
            total_cases=len(runnable),
            passed_cases=0,
            safety_failures=0,
            duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        )
        raise
    result_hashes: list[str] = []
    for result in suite.cases:
        status = EvalCaseStatus.PASSED if result.passed else EvalCaseStatus.FAILED
        digest = case_result_hash(result.case_id, status=status, failures=result.failures)
        result_hashes.append(digest)
        evals.record_case_result(
            run.id,
            case_id=result.case_id,
            suite_version=manifest[:16],
            status=status,
            latency_ms=result.duration_ms,
            input_tokens=None,
            output_tokens=None,
            result_hash=digest,
        )
    outcome = evaluate_gate(runnable, suite, selected_budget)
    receipt_hash = eval_receipt_hash(
        proposal_version_id=proposal_version_id,
        suite_manifest_hash=manifest,
        case_result_hashes=tuple(result_hashes),
        budget=selected_budget,
        runner_version=__version__,
        outcome=outcome,
    )
    evals.complete_run(
        run.id,
        status=EvalRunStatus.PASSED if outcome.passed else EvalRunStatus.FAILED,
        receipt_hash=receipt_hash,
        total_cases=outcome.total_cases,
        passed_cases=outcome.passed_cases,
        safety_failures=outcome.safety_failures,
        duration_ms=outcome.duration_ms,
    )
    return EvalReceipt(
        proposal_version_id=proposal_version_id,
        suite_manifest_hash=manifest,
        receipt_hash=receipt_hash,
        runner_version=__version__,
        outcome=outcome,
    )


def latest_passing_run(evals: EvalRepository, eval_run_id: int) -> EvalRun:
    """读取一次 EvalRun，并要求它已经以 passed 结算。

    Raises:
        EvaluationError: EvalRun 不是 passed，或没有绑定 receipt hash。
    """
    run = evals.get_run(eval_run_id)
    if run.status is not EvalRunStatus.PASSED or run.receipt_hash is None:
        raise EvaluationError(
            "eval_run_not_passed", "eval run did not pass the deterministic gate"
        )
    return run


def _canonical_json(value: object) -> str:
    """返回键排序、无多余空白的 canonical JSON，保证同内容同哈希。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
