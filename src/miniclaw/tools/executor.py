"""Tool 参数校验、Policy、执行和持久化的唯一入口。"""

import asyncio
import time

from miniclaw.policy.engine import PolicyAction, PolicyEngine
from miniclaw.providers.base import JsonValue, ToolCall
from miniclaw.storage.tooling import ToolRunRepository
from miniclaw.tools.base import ToolContext, ToolResult, ToolValidationError
from miniclaw.tools.registry import ToolRegistry


class ToolExecutor:
    """确保任何 Tool 都不能绕过验证、Policy 与 ToolRun。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        runs: ToolRunRepository,
        *,
        result_max_chars: int = 20_000,
    ) -> None:
        if type(result_max_chars) is not int or result_max_chars <= 0:
            raise ValueError("result_max_chars must be a positive integer")
        self._registry = registry
        self._policy = policy
        self._runs = runs
        self._result_max_chars = result_max_chars

    @property
    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        """返回模型可见的稳定 Tool Schema。"""
        return self._registry.schemas

    async def execute(self, context: ToolContext, call: ToolCall) -> str:
        """按 get → validate → policy → start → execute → finish 执行。"""
        tool = self._registry.get(call.name)
        if tool is None:
            return ToolResult.failure(
                "tool_not_found",
                f"tool is not available: {call.name}",
            ).to_model_text(call.name)
        try:
            arguments = tool.validate(call.arguments)
        except ToolValidationError as error:
            return ToolResult.failure(
                "invalid_arguments",
                str(error),
            ).to_model_text(call.name)

        decision = self._policy.authorize(tool.definition, context, arguments)
        if decision.action is not PolicyAction.ALLOW:
            code = (
                "approval_required"
                if decision.action is PolicyAction.REQUIRE_APPROVAL
                else "denied"
            )
            return ToolResult.failure(code, decision.reason).to_model_text(call.name)

        run_id = self._runs.start(context, call, arguments, decision)
        started = time.monotonic()
        try:
            result = await tool.execute(context, arguments)
            if not isinstance(result, ToolResult):
                raise TypeError("tool returned an invalid result")
            model_text = result.to_model_text(call.name)
        except asyncio.CancelledError:
            self._runs.interrupt(run_id, _elapsed_ms(started))
            raise
        except Exception:  # noqa: BLE001 - 内部异常必须在 Tool 边界脱敏
            result = ToolResult.failure("tool_failed", "tool execution failed")
            model_text = result.to_model_text(call.name)
        if len(model_text) > self._result_max_chars:
            result = ToolResult.failure(
                "tool_result_too_large",
                "tool result exceeded the configured size limit",
            )
            model_text = result.to_model_text(call.name)
        duration_ms = _elapsed_ms(started)
        if result.ok:
            self._runs.succeed(run_id, model_text, duration_ms)
        else:
            self._runs.fail(run_id, model_text, duration_ms, result.error_code)
        return model_text


def _elapsed_ms(started: float) -> int:
    """把 monotonic 秒安全转成非负毫秒。"""
    return max(0, round((time.monotonic() - started) * 1000))
