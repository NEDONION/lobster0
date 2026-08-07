"""通过真实 Agent/Policy/Tool/SQLite 链路运行确定性离线场景。"""

import json
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from miniclaw.agent.context import ContextBuilder
from miniclaw.agent.runner import AgentRunner
from miniclaw.agent.turn import TurnService
from miniclaw.bootstrap import initialize_state
from miniclaw.config import AppConfig, load_config
from miniclaw.evals.cases import EvalCase
from miniclaw.memory.store import MemoryStore
from miniclaw.paths import StatePaths, build_state_paths
from miniclaw.policy.approvals import ApprovalDecision, ApprovalError
from miniclaw.policy.engine import PolicyEngine
from miniclaw.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderProtocolError,
    StreamHandler,
)
from miniclaw.storage.conversations import MessageRepository, SessionRepository, TurnRepository
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalRepository, ToolRunRepository
from miniclaw.tools.command import RunCommandTool
from miniclaw.tools.executor import ToolExecutor
from miniclaw.tools.filesystem import EditFileTool, ReadFileTool, WriteFileTool
from miniclaw.tools.memory import ProposeMemoryTool, ReadMemoryTool
from miniclaw.tools.registry import ToolRegistry
from miniclaw.tools.search import GlobTool, GrepTool
from miniclaw.tools.system import SystemInfoTool
from miniclaw.tools.web import HttpGetTool


@dataclass(frozen=True, slots=True)
class EvalCaseResult:
    """保存单条场景的脱敏判定与最小诊断数据。"""

    case_id: str
    passed: bool
    duration_ms: int
    failures: tuple[str, ...]
    tool_runs: tuple[tuple[str, str], ...]
    audit_events: tuple[str, ...]
    approval_statuses: tuple[str, ...]
    request_count: int


@dataclass(frozen=True, slots=True)
class EvalSuiteResult:
    """汇总一次顺序离线回归的 case 数与结果。"""

    total: int
    passed: int
    failed: int
    duration_ms: int
    cases: tuple[EvalCaseResult, ...]


class ScriptedProvider:
    """顺序返回场景响应，供离线 Agent 回归复用真实循环。"""

    def __init__(self, responses: tuple[ModelResponse, ...]) -> None:
        """保存有限响应序列并初始化请求观测。"""
        self._responses = responses
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> ModelResponse:
        """记录请求并返回同位置响应，序列耗尽时稳定失败。"""
        index = len(self.requests)
        self.requests.append(request)
        if index >= len(self._responses):
            raise ProviderProtocolError("eval scripted responses exhausted")
        response = self._responses[index]
        if on_text is not None and response.content:
            await on_text(response.content)
        return response


async def run_offline_case(case: EvalCase) -> EvalCaseResult:
    """在独立临时 MiniClaw 实例运行并验证一条离线场景。

    Args:
        case: 已通过 JSONL loader 校验的场景。

    Returns:
        不含 Prompt、回答、路径或工具原始结果的判定。
    """
    started = time.monotonic()
    provider = ScriptedProvider(case.responses)
    with TemporaryDirectory(prefix="miniclaw-eval-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        initialized = initialize_state(paths)
        config = load_config(paths, environ={})
        _write_setup(paths.workspace, case.setup_files)
        database = Database(paths.database)
        approvals = ApprovalRepository(database)
        service = _build_service(database, paths, config, provider, approvals)
        answer = ""
        execution_error_code: str | None = None
        try:
            for text in (case.query, *case.turns):
                answer = (await service.handle(initialized.owner.id, text, case.id)).content
            approval_id: int | None = None
            for action in case.approval_actions:
                if approval_id is None:
                    stored = approvals.list(initialized.owner.id)
                    if not stored:
                        raise ApprovalError("not_found", "eval approval was not created")
                    approval_id = stored[-1].id
                if action == "approve":
                    answer = (
                        await service.continue_approval(
                            initialized.owner.id,
                            approval_id,
                            decision=ApprovalDecision.ONCE,
                        )
                    ).content
                elif action == "deny":
                    answer = (
                        await service.continue_approval(
                            initialized.owner.id,
                            approval_id,
                            decision=ApprovalDecision.DENY,
                        )
                    ).content
                elif action == "tamper":
                    approvals.approve(initialized.owner.id, approval_id)
                    with database.connect() as connection:
                        connection.execute(
                            """
                            UPDATE tool_runs SET arguments_json = '{}'
                            WHERE id = (SELECT tool_run_id FROM approvals WHERE id = ?)
                            """,
                            (approval_id,),
                        )
                    await service.continue_approval(
                        initialized.owner.id,
                        approval_id,
                        decision=ApprovalDecision.ONCE,
                    )
                else:
                    await service.continue_approval(
                        initialized.owner.id,
                        approval_id,
                        decision=ApprovalDecision.ONCE,
                    )
        except ApprovalError as error:
            execution_error_code = error.code
        except Exception:  # noqa: BLE001 - eval 边界只输出短码并继续后续 case
            execution_error_code = "execution_error"
        tool_runs, audit_events, approval_statuses = _observations(database)
        failures = _verify(
            case,
            answer,
            provider.requests,
            tool_runs,
            audit_events,
            approval_statuses,
            paths.workspace,
            execution_error_code,
        )
    return EvalCaseResult(
        case_id=case.id,
        passed=not failures,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        failures=failures,
        tool_runs=tool_runs,
        audit_events=audit_events,
        approval_statuses=approval_statuses,
        request_count=len(provider.requests),
    )


async def run_offline_suite(cases: tuple[EvalCase, ...]) -> EvalSuiteResult:
    """顺序运行全部场景，使资源占用和输出顺序保持确定。"""
    started = time.monotonic()
    results = tuple([await run_offline_case(case) for case in cases])
    passed = sum(result.passed for result in results)
    return EvalSuiteResult(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        cases=results,
    )


def _build_service(
    database: Database,
    paths: StatePaths,
    config: AppConfig,
    provider: ScriptedProvider,
    approvals: ApprovalRepository,
) -> TurnService:
    """按生产 CLI 的稳定顺序组装真实 Turn 与 Tool 依赖。"""
    memory = MemoryStore(paths)
    executor = ToolExecutor(
        ToolRegistry(
            (
                SystemInfoTool(),
                ReadFileTool(),
                WriteFileTool(),
                EditFileTool(),
                GlobTool(),
                GrepTool(),
                HttpGetTool(),
                RunCommandTool(),
                ReadMemoryTool(memory),
                ProposeMemoryTool(memory),
            )
        ),
        PolicyEngine(),
        ToolRunRepository(database),
        result_max_chars=config.agent.tool_result_max_chars,
        approvals=approvals,
        approval_ttl_seconds=config.tools.approval_ttl_seconds,
    )
    return TurnService(
        model=config.agent.model,
        sessions=SessionRepository(database),
        messages=MessageRepository(database),
        turns=TurnRepository(database),
        context=ContextBuilder(paths, memory),
        runner=AgentRunner(provider, executor, max_iterations=config.agent.max_tool_iterations),
        approvals=approvals,
        state_home=paths.home,
        workspace=config.workspace,
    )


def _write_setup(workspace: Path, files: tuple[tuple[str, str], ...]) -> None:
    """把合成文本写入临时 Workspace，并再次约束解析后路径。"""
    root = workspace.resolve()
    for relative, content in files:
        target = (root / relative).resolve(strict=False)
        if not target.is_relative_to(root):
            raise ValueError("eval setup path escaped workspace")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _observations(
    database: Database,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...], tuple[str, ...]]:
    """读取 verifier 所需的 ToolRun 状态和审计事件类型。"""
    with database.connect_read_only() as connection:
        runs = tuple(
            (str(row["tool_name"]), str(row["status"]))
            for row in connection.execute(
                "SELECT tool_name, status FROM tool_runs ORDER BY id"
            ).fetchall()
        )
        events = tuple(
            str(row["event_type"])
            for row in connection.execute(
                "SELECT event_type FROM audit_events ORDER BY id"
            ).fetchall()
        )
        approvals = tuple(
            str(row["status"])
            for row in connection.execute(
                "SELECT status FROM approvals ORDER BY id"
            ).fetchall()
        )
    return runs, events, approvals


def _verify(
    case: EvalCase,
    answer: str,
    requests: list[ModelRequest],
    tool_runs: tuple[tuple[str, str], ...],
    audit_events: tuple[str, ...],
    approval_statuses: tuple[str, ...],
    workspace: Path,
    execution_error_code: str | None,
) -> tuple[str, ...]:
    """执行不依赖自然语言 Judge 的稳定可观察断言。"""
    expected = case.expected
    failures: list[str] = []
    if execution_error_code != expected.error_code:
        return (
            "execution_error"
            if expected.error_code is None
            else "error_code_mismatch",
        )
    if any(fragment not in answer for fragment in expected.answer_contains):
        failures.append("answer_missing")
    if any(fragment in answer for fragment in expected.answer_excludes):
        failures.append("answer_leaked")
    run_names = tuple(name for name, _ in tool_runs)
    if any(name not in run_names for name in expected.tool_runs):
        failures.append("tool_run_missing")
    if any(pair not in tool_runs for pair in expected.tool_statuses):
        failures.append("tool_status_mismatch")
    if any(event not in audit_events for event in expected.audit_events):
        failures.append("audit_missing")
    request_text = _request_text(requests[-1]) if requests else ""
    if any(fragment not in request_text for fragment in expected.request_contains):
        failures.append("request_missing")
    if expected.max_tool_runs is not None and len(tool_runs) > expected.max_tool_runs:
        failures.append("too_many_tool_runs")
    if approval_statuses != expected.approval_statuses:
        failures.append("approval_status_mismatch")
    for relative, content in expected.files:
        try:
            actual = (workspace / relative).read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            failures.append("file_mismatch")
            continue
        if actual != content:
            failures.append("file_mismatch")
    if any((workspace / relative).exists() for relative in expected.absent_files):
        failures.append("unexpected_file")
    return tuple(failures)


def _request_text(request: ModelRequest) -> str:
    """把最后一次请求规范成仅用于内存断言的文本，结果不会持久化。"""
    parts: list[str] = []
    for message in request.messages:
        parts.append(message.content)
        for call in message.tool_calls:
            parts.extend(
                (
                    call.name,
                    json.dumps(call.arguments, ensure_ascii=False, sort_keys=True),
                )
            )
    return "\n".join(parts)
