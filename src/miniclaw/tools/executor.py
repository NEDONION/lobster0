"""Tool 参数校验、Policy、执行和持久化的唯一入口。"""

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from miniclaw.policy.engine import PolicyAction, PolicyEngine
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.tooling import ApprovalRepository, StoredToolRun, ToolRunRepository
from miniclaw.tools.base import Tool, ToolContext, ToolResult, ToolValidationError
from miniclaw.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class ToolExecution:
    """返回模型文本，并在等待人工确认时携带持久 Approval ID。"""

    model_text: str
    approval_id: int | None = None


class ToolExecutor:
    """确保任何 Tool 都不能绕过验证、Policy 与 ToolRun。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        runs: ToolRunRepository,
        *,
        result_max_chars: int = 20_000,
        approvals: ApprovalRepository | None = None,
        approval_ttl_seconds: int = 600,
    ) -> None:
        if type(result_max_chars) is not int or result_max_chars <= 0:
            raise ValueError("result_max_chars must be a positive integer")
        if type(approval_ttl_seconds) is not int or approval_ttl_seconds <= 0:
            raise ValueError("approval_ttl_seconds must be a positive integer")
        self._registry = registry
        self._policy = policy
        self._runs = runs
        self._result_max_chars = result_max_chars
        self._approvals = approvals
        self._approval_ttl_seconds = approval_ttl_seconds

    @property
    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        """返回模型可见的稳定 Tool Schema。"""
        return self._registry.schemas

    async def execute(self, context: ToolContext, call: ToolCall) -> ToolExecution:
        """按 get → validate → policy → start → execute → finish 执行。"""
        tool = self._registry.get(call.name)
        if tool is None:
            return ToolExecution(
                ToolResult.failure(
                    "tool_not_found",
                    f"tool is not available: {call.name}",
                ).to_model_text(call.name)
            )
        try:
            arguments = tool.validate(call.arguments)
        except ToolValidationError as error:
            return ToolExecution(
                ToolResult.failure(
                    "invalid_arguments",
                    str(error),
                ).to_model_text(call.name)
            )

        decision = self._policy.authorize(tool.definition, context, arguments)
        arguments = decision.normalized_arguments or arguments
        if decision.action is not PolicyAction.ALLOW:
            if decision.action is PolicyAction.DENY:
                self._runs.deny(context, call, arguments, decision.error_code)
            if decision.action is PolicyAction.REQUIRE_APPROVAL and self._approvals is not None:
                approval = self._approvals.create_waiting(
                    context,
                    call,
                    arguments,
                    decision,
                    ttl_seconds=self._approval_ttl_seconds,
                    summary=_approval_summary(call.name, arguments),
                )
                return ToolExecution(
                    ToolResult.failure(
                        "approval_required",
                        f"approval {approval.id} is required for {call.name}",
                    ).to_model_text(call.name),
                    approval.id,
                )
            code = (
                "approval_required"
                if decision.action is PolicyAction.REQUIRE_APPROVAL
                else decision.error_code
            )
            return ToolExecution(
                ToolResult.failure(code, decision.reason).to_model_text(call.name)
            )

        run_id = self._runs.start(context, call, arguments, decision)
        return await self._execute_started(context, tool, arguments, run_id)

    async def execute_approved(
        self,
        context: ToolContext,
        run: StoredToolRun,
    ) -> ToolExecution:
        """执行已由 Approval 原子 claim 的唯一 running ToolRun。"""
        if run.status != "running":
            raise ValueError("approved ToolRun must be running")
        tool = self._registry.get(run.tool_name)
        if tool is None:
            result = ToolResult.failure("tool_not_found", "approved tool is not available")
            model_text = result.to_model_text(run.tool_name)
            self._runs.fail(run.id, model_text, 0, result.error_code)
            return ToolExecution(model_text)
        try:
            arguments = tool.validate(run.arguments)
        except ToolValidationError:
            result = ToolResult.failure(
                "invalid_arguments",
                "approved tool arguments are no longer valid",
            )
            model_text = result.to_model_text(run.tool_name)
            self._runs.fail(run.id, model_text, 0, result.error_code)
            return ToolExecution(model_text)
        return await self._execute_started(context, tool, arguments, run.id)

    async def _execute_started(
        self,
        context: ToolContext,
        tool: Tool,
        arguments: dict[str, JsonValue],
        run_id: int,
    ) -> ToolExecution:
        """执行并终结一个已经处于 running 的 ToolRun。"""
        started = time.monotonic()
        try:
            result = await tool.execute(context, arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("tool returned an invalid result")
            model_text = result.to_model_text(tool.definition.name)
        except asyncio.CancelledError:
            self._runs.interrupt(run_id, _elapsed_ms(started))
            raise
        except Exception:  # noqa: BLE001 - 内部异常必须在 Tool 边界脱敏
            result = ToolResult.failure("tool_failed", "tool execution failed")
            model_text = result.to_model_text(tool.definition.name)
        if len(model_text) > self._result_max_chars:
            result = ToolResult.failure(
                "tool_result_too_large",
                "tool result exceeded the configured size limit",
            )
            model_text = result.to_model_text(tool.definition.name)
        duration_ms = _elapsed_ms(started)
        if result.ok:
            self._runs.succeed(run_id, model_text, duration_ms)
        else:
            self._runs.fail(run_id, model_text, duration_ms, result.error_code)
        return ToolExecution(model_text)


def _elapsed_ms(started: float) -> int:
    """把 monotonic 秒安全转成非负毫秒。"""
    return max(0, round((time.monotonic() - started) * 1000))


def _approval_summary(tool_name: str, arguments: dict[str, JsonValue]) -> str:
    """生成不含写入内容或完整绝对路径的最小人工确认摘要。"""
    path = arguments.get("path")
    if isinstance(path, str):
        return f"{tool_name} {Path(path).name}"
    return f"{tool_name} request"
