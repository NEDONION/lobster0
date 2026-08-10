"""Automation Agent 用于结构化结束 TaskRun 的 terminal Tool。"""

from lobster0.automation.models import TaskResponse
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)


class CompleteTaskTool:
    """只允许绑定 TaskRun 的 automation context 返回 TaskResponse。"""

    definition = ToolDefinition(
        name="complete_task",
        description=(
            "Finish the current automation task. Set notify=true with the final user-visible "
            "text, or notify=false with empty text for a silent successful run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "notify": {"type": "boolean"},
                "text": {"type": "string", "maxLength": 262144},
            },
            "required": ["notify", "text"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """严格校验 notify/text，并复用 TaskResponse 的 UTF-8 上限。"""
        if set(arguments) != {"notify", "text"}:
            raise ToolValidationError("complete_task requires exactly notify and text")
        notify = arguments["notify"]
        text = arguments["text"]
        if type(notify) is not bool or not isinstance(text, str):
            raise ToolValidationError("complete_task notify/text types are invalid")
        try:
            TaskResponse(notify=notify, text=text)
        except ValueError as exc:
            raise ToolValidationError("complete_task response is invalid") from exc
        return {"notify": notify, "text": text}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """从可信 context 产生 canonical TaskResponse data，否则稳定拒绝。"""
        if context.source != "automation" or context.task_run_id is None:
            return ToolResult.failure(
                "automation_context_required",
                "automation context required",
            )
        notify = arguments["notify"]
        text = arguments["text"]
        assert type(notify) is bool and isinstance(text, str)
        response = TaskResponse(notify=notify, text=text)
        return ToolResult.success(
            {"notify": response.notify, "text": response.text}
        )
