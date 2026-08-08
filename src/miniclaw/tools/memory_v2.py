"""Memory Autopilot v2 的来源绑定模型 Tool。"""

from datetime import UTC, datetime

from miniclaw.memory.models import SourceRef
from miniclaw.memory.service import ExplicitMemoryRequest, MemoryService
from miniclaw.memory.store import MemoryError
from miniclaw.providers.base import JsonValue
from miniclaw.storage.conversations import MessageRepository
from miniclaw.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)


class MemoryRememberTool:
    """只在当前 Turn 含明确 Owner remember 意图时原子保存事实。"""

    definition = ToolDefinition(
        name="memory_remember",
        description=(
            "Store one durable fact only when the latest owner message explicitly asks "
            "MiniClaw to remember it. Owner, source, scope, and status are Core-bound."
        ),
        parameters={
            "type": "object",
            "properties": {"fact": {"type": "string", "maxLength": 2000}},
            "required": ["fact"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self, service: MemoryService, messages: MessageRepository) -> None:
        """绑定 Memory Service 与只读 Message 来源 Repository。"""
        self._service = service
        self._messages = messages

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受一个非空有界 fact，身份与来源字段一律拒绝。"""
        if set(arguments) != {"fact"}:
            raise ToolValidationError("memory_remember requires only fact")
        fact = arguments.get("fact")
        if not isinstance(fact, str) or not fact.strip() or len(fact) > 2_000:
            raise ToolValidationError("memory_remember fact is invalid")
        return {"fact": fact.strip()}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """从当前 Turn 解析可信 SourceRef 并同步完成明确事实提交。"""
        disclosure = context.disclosure
        if disclosure is None:
            return ToolResult.failure(
                "memory_disclosure_denied",
                "memory was not stored in this conversation",
            )
        source_message = next(
            (
                message
                for message in reversed(self._messages.list_recent(context.session_id, limit=20))
                if message.role == "user" and message.turn_id == context.turn_id
            ),
            None,
        )
        if source_message is None:
            return ToolResult.failure(
                "invalid_memory_source",
                "memory source could not be verified",
            )
        fact = arguments["fact"]
        assert isinstance(fact, str)
        try:
            result = self._service.remember_explicit(
                ExplicitMemoryRequest(
                    disclosure=disclosure,
                    source=SourceRef(
                        source_message.id,
                        source_message.session_id,
                        disclosure.channel,
                    ),
                    latest_user_text=source_message.content,
                    fact=fact,
                    now=datetime.now(UTC),
                )
            )
        except MemoryError as error:
            return ToolResult.failure(error.code, str(error))
        return ToolResult.success(
            {
                "unit_id": result.unit_id,
                "status": result.status,
                "review_id": result.review_id,
            }
        )
