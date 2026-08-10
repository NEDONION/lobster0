"""读取和经审批追加 Lobster0 Markdown Memory 的 Tool。"""

import asyncio
import hashlib

from lobster0.memory.policy import MemoryDisclosurePolicy, MemoryPolicyError
from lobster0.memory.store import MemoryError, MemoryStore
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)


class ReadMemoryTool:
    """读取固定 scope 的长期或近期 Memory，不接受任意路径。"""

    definition = ToolDefinition(
        name="read_memory",
        description=(
            "Read Lobster0's bounded Markdown memory. Use long_term for durable facts, "
            "today for today's notes, or recent for today and yesterday."
        ),
        parameters={
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["long_term", "today", "recent"],
                }
            },
            "required": ["scope"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self, store: MemoryStore) -> None:
        """绑定只能访问固定状态路径的 MemoryStore。"""
        self._store = store

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受封闭枚举中的单个 scope。"""
        if set(arguments) != {"scope"}:
            raise ToolValidationError("read_memory requires only scope")
        scope = arguments.get("scope")
        if scope not in {"long_term", "today", "recent"}:
            raise ToolValidationError("scope must be long_term, today, or recent")
        assert isinstance(scope, str)
        return {"scope": scope}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """异步读取有界 Memory 文本和可回放哈希。"""
        denied = _private_memory_denial(context, require_capture=False)
        if denied is not None:
            return denied
        scope = arguments["scope"]
        assert isinstance(scope, str)
        try:
            content = await asyncio.to_thread(self._store.read, scope)
        except MemoryError as error:
            return ToolResult.failure(error.code, str(error))
        return ToolResult.success(
            {
                "scope": scope,
                "content": content,
                "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            }
        )


class ProposeMemoryTool:
    """经参数绑定审批后把明确事实追加到当日 Memory。"""

    definition = ToolDefinition(
        name="propose_memory",
        description=(
            "Propose one durable fact for today's Lobster0 memory. Use only when the owner "
            "explicitly asks you to remember something. This action requires approval and "
            "must never contain credentials, tokens, passwords, or private keys."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "maxLength": 2000},
                "source": {"type": "string", "maxLength": 200},
            },
            "required": ["content", "source"],
            "additionalProperties": False,
        },
        risk=ToolRisk.MEDIUM,
    )

    def __init__(self, store: MemoryStore) -> None:
        """绑定同时负责写盘前二次校验的 MemoryStore。"""
        self._store = store

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """在 Approval 创建前规范化文本并拒绝敏感候选。"""
        if set(arguments) != {"content", "source"}:
            raise ToolValidationError("propose_memory requires content and source")
        content = arguments.get("content")
        source = arguments.get("source")
        if not isinstance(content, str) or not isinstance(source, str):
            raise ToolValidationError("content and source must be text")
        try:
            fact, origin = self._store.validate_candidate(content, source)
        except MemoryError as error:
            raise ToolValidationError(str(error)) from None
        return {"content": fact, "source": origin}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """把 Policy 已放行的绑定事实追加到当前 UTC 日期文件。"""
        denied = _private_memory_denial(context, require_capture=True)
        if denied is not None:
            return denied
        content = arguments["content"]
        source = arguments["source"]
        assert isinstance(content, str)
        assert isinstance(source, str)
        try:
            result = await asyncio.to_thread(
                self._store.append_daily,
                content,
                source=source,
                session_id=context.session_id,
            )
        except MemoryError as error:
            return ToolResult.failure(error.code, str(error))
        return ToolResult.success(
            {
                "status": result.status,
                "day": result.day,
                "content_hash": result.content_hash,
            }
        )


def _private_memory_denial(
    context: ToolContext,
    *,
    require_capture: bool,
) -> ToolResult | None:
    """在旧 Memory Tool 执行前应用与 Context 相同的 Core 披露策略。"""
    if context.disclosure is None:
        return ToolResult.failure(
            "memory_disclosure_denied",
            "private memory is not disclosed in this conversation",
        )
    try:
        decision = MemoryDisclosurePolicy().decide(context.disclosure)
    except MemoryPolicyError:
        return ToolResult.failure(
            "memory_disclosure_denied",
            "private memory is not disclosed in this conversation",
        )
    allowed = (
        decision.capture_scope == "private"
        if require_capture
        else decision.private_access == "full"
    )
    if allowed:
        return None
    return ToolResult.failure(
        "memory_disclosure_denied",
        "private memory is not disclosed in this conversation",
    )
