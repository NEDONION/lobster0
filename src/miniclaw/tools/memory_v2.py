"""Memory Autopilot v2 的来源绑定模型 Tool。"""

from collections.abc import Callable
from datetime import UTC, datetime

from miniclaw.memory.models import SourceRef
from miniclaw.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError
from miniclaw.memory.repository import MemoryUnit
from miniclaw.memory.retrieval import MemoryRetrieval, SearchRequest
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


class MemorySearchTool:
    """按当前 ToolContext 的 Owner/Disclosure 搜索可召回 Memory。"""

    definition = ToolDefinition(
        name="memory_search",
        description="Search verified owner memory; owner and disclosure are Core-bound.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 1000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self, retrieval: MemoryRetrieval) -> None:
        """绑定 Owner-scoped Retrieval。"""
        self._retrieval = retrieval

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受 query 和可选有界 limit，拒绝模型提供身份字段。"""
        if not set(arguments).issubset({"query", "limit"}) or "query" not in arguments:
            raise ToolValidationError("memory_search requires query and optional limit")
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        if not isinstance(query, str) or not query.strip() or len(query) > 1_000:
            raise ToolValidationError("memory_search query is invalid")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ToolValidationError("memory_search limit is invalid")
        return {"query": query.strip(), "limit": limit}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """执行 fail-closed Recall，并返回完整 Unit 摘要与证据 ID。"""
        if context.disclosure is None:
            return ToolResult.success(
                {"items": [], "reason_code": "memory_disclosure_denied"}
            )
        query = arguments.get("query")
        limit = arguments.get("limit", 5)
        assert isinstance(query, str) and type(limit) is int
        result = self._retrieval.search(
            SearchRequest(context.disclosure, query, limit),
        )
        return ToolResult.success(
            {
                "items": [_unit_data(hit.unit) for hit in result.items],
                "reason_code": result.reason_code,
            }
        )


class MemoryGetTool:
    """按不可跨 Owner 的 Unit ID 展示 Memory 详情与来源。"""

    definition = ToolDefinition(
        name="memory_get",
        description="Get one verified owner memory unit and its evidence source ids.",
        parameters={
            "type": "object",
            "properties": {"unit_id": {"type": "string", "maxLength": 160}},
            "required": ["unit_id"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self, retrieval: MemoryRetrieval) -> None:
        """绑定 Owner-scoped Retrieval。"""
        self._retrieval = retrieval

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受一个有界 Unit ID。"""
        if set(arguments) != {"unit_id"}:
            raise ToolValidationError("memory_get requires only unit_id")
        unit_id = arguments.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id.strip() or len(unit_id) > 160:
            raise ToolValidationError("memory_get unit_id is invalid")
        return {"unit_id": unit_id.strip()}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """读取 Unit；拒绝、跨 Owner 与缺失统一返回 not_found。"""
        unit_id = arguments.get("unit_id")
        assert isinstance(unit_id, str)
        unit = (
            None
            if context.disclosure is None
            else self._retrieval.get(context.disclosure, unit_id)
        )
        if unit is None:
            return ToolResult.failure("memory_not_found", "memory unit was not found")
        return ToolResult.success(_unit_data(unit))


class MemoryListTool:
    """稳定列出当前 Owner 的可召回 Memory Unit。"""

    definition = ToolDefinition(
        name="memory_list",
        description="List verified owner memory units; owner is Core-bound.",
        parameters={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50}
            },
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self, retrieval: MemoryRetrieval) -> None:
        """绑定 Owner-scoped Retrieval。"""
        self._retrieval = retrieval

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受可选有界 limit。"""
        if not set(arguments).issubset({"limit"}):
            raise ToolValidationError("memory_list accepts only limit")
        limit = arguments.get("limit", 20)
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ToolValidationError("memory_list limit is invalid")
        return {"limit": limit}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """列出完整 Unit 摘要；Disclosure 缺失时返回空集合。"""
        limit = arguments.get("limit", 20)
        assert type(limit) is int
        units = (
            ()
            if context.disclosure is None
            else self._retrieval.list(context.disclosure, limit=limit)
        )
        return ToolResult.success({"items": [_unit_data(unit) for unit in units]})


class MemoryFlushTool:
    """只调度后台 Flush，不在前台 Tool Call 内运行提取器。"""

    definition = ToolDefinition(
        name="memory_flush",
        description="Schedule a background memory flush without waiting for extraction.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        risk=ToolRisk.LOW,
    )

    def __init__(self, schedule: Callable[[], None]) -> None:
        """绑定无参数、非阻塞的 worker 唤醒函数。"""
        self._schedule = schedule
        self._policy = MemoryDisclosurePolicy()

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """拒绝全部模型参数，避免传入 Owner 或执行策略。"""
        if arguments:
            raise ToolValidationError("memory_flush accepts no arguments")
        return {}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """验证本地/Owner private 边界后仅唤醒 worker。"""
        del arguments
        try:
            decision = (
                None
                if context.disclosure is None
                else self._policy.decide(context.disclosure)
            )
        except MemoryPolicyError:
            decision = None
        if decision is None or decision.private_access != "full":
            return ToolResult.failure(
                "memory_disclosure_denied",
                "memory flush is unavailable in this conversation",
            )
        self._schedule()
        return ToolResult.success({"scheduled": True})


def _unit_data(unit: MemoryUnit) -> dict[str, JsonValue]:
    """把 Unit 编码为不含外部平台用户 ID 的稳定 Tool payload。"""
    return {
        "unit_id": unit.id,
        "key": unit.key,
        "text": unit.text,
        "kind": unit.kind,
        "status": unit.status,
        "confidence": unit.confidence,
        "source_message_ids": [source.message_id for source in unit.sources],
    }
