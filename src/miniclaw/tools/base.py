"""模型可见 Tool 的稳定数据契约。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from miniclaw.providers.base import JsonValue


class ToolRisk(StrEnum):
    """表示 Tool 动作在执行前使用的默认风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """描述一个 Tool 的模型 Schema 与默认风险。"""

    name: str
    description: str
    parameters: dict[str, JsonValue]
    risk: ToolRisk

    def to_model_schema(self) -> dict[str, JsonValue]:
        """转换成 OpenAI-compatible function Tool Schema。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True, slots=True)
class ToolContext:
    """保存一次 Tool Call 不可从模型参数伪造的运行期边界。"""

    user_id: int
    session_id: int
    turn_id: int
    state_home: Path
    workspace: Path
    read_only_roots: tuple[Path, ...]


class ToolValidationError(ValueError):
    """表示模型提供的 Tool 参数不符合公开 Schema。"""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """表示可安全编码为模型 Tool Message 的成功或失败结果。"""

    ok: bool
    data: JsonValue = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False

    @classmethod
    def success(cls, data: JsonValue) -> ToolResult:
        """创建一个包含 JSON 数据的成功结果。"""
        return cls(ok=True, data=data)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> ToolResult:
        """创建一个不包含内部异常细节的失败结果。"""
        return cls(
            ok=False,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )

    def to_model_text(self, tool_name: str) -> str:
        """编码为字段顺序稳定的紧凑 JSON Tool Message。"""
        body: dict[str, JsonValue] = {"ok": self.ok, "tool": tool_name}
        if self.ok:
            body["data"] = self.data
        else:
            body["error"] = {
                "code": self.error_code,
                "message": self.error_message,
                "retryable": self.retryable,
            }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class Tool(Protocol):
    """定义 Registry 与 Executor 使用的最小异步 Tool 能力。"""

    definition: ToolDefinition

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """校验并返回供 Policy 与执行共用的规范参数。"""
        ...

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """在 Policy 已放行后执行一个工具动作。"""
        ...
