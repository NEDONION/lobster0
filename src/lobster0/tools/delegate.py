"""把一个明确的子目标交给 depth-1 子 Agent 的受控 Tool。"""

from collections.abc import Awaitable, Callable

from lobster0.config import DELEGATE_TOOL_NAME, SubagentConfig
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_MAX_GOAL_CHARS = 8000

Dispatch = Callable[..., Awaitable[dict[str, JsonValue]]]


def subagent_tool_names(
    subagent: SubagentConfig, *, parent_tools: frozenset[str]
) -> frozenset[str]:
    """算出子 Agent 真正可用的工具集。

    取「声明」与「父此刻可用」的交集，而不是直接用声明——父自己可能已经被上一层
    收窄过（例如自动化档），只有交集才是真实可用集。

    无论两边写了什么，结果都不含 :data:`DELEGATE_TOOL_NAME`：这是 max depth = 1
    的实现点，子 Agent 根本看不到派发工具，比任何深度计数器都可靠。

    Raises:
        ValueError: 交集为空。一个工具都用不了的子 Agent 只会白烧一次模型调用。
    """
    names = (set(subagent.tools) & set(parent_tools)) - {DELEGATE_TOOL_NAME}
    if not names:
        raise ValueError(f"subagent {subagent.id} has no usable tools")
    return frozenset(names)


class DelegateTaskTool:
    """把一个子目标交给已声明的子 Agent，并同步等待结果。"""

    def __init__(self, subagents: tuple[SubagentConfig, ...], *, dispatch: Dispatch | None) -> None:
        """绑定已声明的子 Agent 与真正的派发实现。

        Args:
            subagents: 配置里声明的 depth-1 子 Agent。
            dispatch: 执行一次子任务的可调用对象；为空时工具只能用于展示定义。
        """
        self._subagents = {item.id: item for item in subagents}
        self._dispatch = dispatch
        self.definition = ToolDefinition(
            name=DELEGATE_TOOL_NAME,
            description=_describe(subagents),
            parameters={
                "type": "object",
                "properties": {
                    "subagent_id": {
                        "type": "string",
                        "enum": sorted(self._subagents),
                    },
                    "goal": {"type": "string", "minLength": 1, "maxLength": _MAX_GOAL_CHARS},
                    "timeout_seconds": {"type": "integer", "minimum": 1},
                },
                "required": ["subagent_id", "goal"],
                "additionalProperties": False,
            },
            risk=ToolRisk.HIGH,
        )

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受已声明的子 Agent、有界的子目标与不超过声明上限的超时。"""
        unexpected = set(arguments) - {"subagent_id", "goal", "timeout_seconds"}
        if unexpected:
            raise ToolValidationError(
                f"{DELEGATE_TOOL_NAME} only accepts 'subagent_id', 'goal' and "
                "'timeout_seconds'"
            )
        identifier = arguments.get("subagent_id")
        if not isinstance(identifier, str) or identifier not in self._subagents:
            raise ToolValidationError("subagent_id is not a declared subagent")
        subagent = self._subagents[identifier]
        goal = arguments.get("goal")
        if (
            not isinstance(goal, str)
            or not goal.strip()
            or len(goal) > _MAX_GOAL_CHARS
        ):
            raise ToolValidationError("goal must be a bounded non-empty string")
        # 默认取声明值而不是某个全局默认：声明是这个子 Agent 的预算上限。
        timeout = arguments.get("timeout_seconds", subagent.timeout_seconds)
        if (
            type(timeout) is not int
            or isinstance(timeout, bool)
            or not 1 <= timeout <= subagent.timeout_seconds
        ):
            # 只能调低。允许调高等于让模型自己扩预算。
            raise ToolValidationError("timeout_seconds must not exceed the declared ceiling")
        return {"subagent_id": identifier, "goal": goal, "timeout_seconds": timeout}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """派发一次子任务；失败如实返回而不是抛出。"""
        identifier = arguments["subagent_id"]
        goal = arguments["goal"]
        timeout = arguments["timeout_seconds"]
        assert isinstance(identifier, str) and isinstance(goal, str)
        assert isinstance(timeout, int)

        # 已经在子 Agent 里时上下文不含派发工具——这是运行期的 depth-1 复核，
        # 配置层与工具集收窄之外的第三道。
        allowed = context.allowed_tool_names
        if allowed is not None and DELEGATE_TOOL_NAME not in allowed:
            return ToolResult.failure(
                "subagent_depth_exceeded", "subagents cannot delegate again"
            )
        if self._dispatch is None:
            return ToolResult.failure("subagent_unavailable", "delegation is unavailable")

        try:
            outcome = await self._dispatch(
                self._subagents[identifier], goal, timeout_seconds=timeout
            )
        except TimeoutError:
            return ToolResult.failure("subagent_timeout", "subagent task timed out")
        except Exception:  # noqa: BLE001 - 子任务失败不该让父回合直接崩
            return ToolResult.failure("subagent_failed", "subagent task failed")
        return ToolResult.success({"subagent_id": identifier, **outcome})


def _describe(subagents: tuple[SubagentConfig, ...]) -> str:
    """把已声明的子 Agent 写进工具描述。

    模型要能从描述里知道有谁可派、各自擅长什么，否则只能猜 id——manage_task 的
    schedule 字段就是这么被猜错三次的。
    """
    if not subagents:
        return "No subagents are declared; delegation is unavailable."
    roster = "; ".join(f"{item.id}: {item.description}" for item in subagents)
    return (
        "Hand one clearly scoped subgoal to a declared subagent and wait for its "
        "result. The subagent runs with an isolated context and a narrowed tool set, "
        f"and cannot delegate further. Available subagents — {roster}."
    )
