"""把一个子目标交给 depth-1 子 Agent 并结算它的 durable Run。"""

from datetime import UTC, datetime
from typing import Protocol

from lobster0.agent.runner import AgentRunBudget
from lobster0.agent.turn import TurnExecutionProfile
from lobster0.automation.models import RunStatus
from lobster0.config import SubagentConfig
from lobster0.providers.base import JsonValue
from lobster0.tools.delegate import subagent_tool_names

_WORKER_ID = "subagent"


class _TurnService(Protocol):
    """只收窄到派发需要的那一个方法。"""

    async def handle_automation(self, *, task_id, task_run_id, text, profile): ...


class SubagentDispatcher:
    """执行一次 depth-1 派发：建子 Run、跑受限回合、结算。"""

    def __init__(
        self,
        *,
        service: _TurnService,
        runs: object,
        task: object,
        parent_run_id: int,
        parent_tools: frozenset[str],
    ) -> None:
        """绑定这次派发的父上下文。

        Args:
            parent_tools: 父此刻真正可用的工具集。子 Agent 取它与声明的交集——
                父自己可能已被上一层收窄过，只有交集才是真实可用集。
        """
        self._service = service
        self._runs = runs
        self._task = task
        self._parent_run_id = parent_run_id
        self._parent_tools = parent_tools

    async def dispatch(
        self,
        subagent: SubagentConfig,
        goal: str,
        *,
        timeout_seconds: int,
    ) -> dict[str, JsonValue]:
        """跑一次子任务并返回可安全展示的结果。

        Raises:
            ValueError: 工具交集为空。**在建 Run 之前就抛**——一个工具都用不了
                的子 Agent 只会白烧一次模型调用，也不该在历史里留下一条注定
                失败的 Run。
        """
        # 先算交集：为空时不建 Run、不调模型。
        allowed = subagent_tool_names(subagent, parent_tools=self._parent_tools)
        now = datetime.now(UTC)
        child = self._runs.enqueue_child(
            self._task,
            parent_run_id=self._parent_run_id,
            subagent_id=subagent.id,
            scheduled_for=now,
        )
        self._runs.mark_running(child.id, _WORKER_ID, now=now)

        profile = TurnExecutionProfile(
            source="automation",
            task_run_id=child.id,
            allowed_tool_names=allowed,
            budget=AgentRunBudget(
                max_turns=subagent.max_turns,
                max_tool_calls=subagent.max_tool_calls,
                timeout_seconds=timeout_seconds,
            ),
        )
        # 上下文默认隔离：只有父明确写下的子目标越界，父会话历史不复制。
        result = await self._service.handle_automation(
            task_id=self._task.id,
            task_run_id=child.id,
            text=goal,
            profile=profile,
        )

        failed = result.error_code is not None
        self._runs.finish(
            child.id,
            status=RunStatus.FAILED if failed else RunStatus.SUCCEEDED,
            now=datetime.now(UTC),
            worker_id=_WORKER_ID,
            error_code=result.error_code,
            result_preview=result.content[:500] or None,
            usage={
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            },
        )
        return {
            "status": "failed" if failed else "succeeded",
            "run_id": child.id,
            "summary": result.content[:2000],
            "error_code": result.error_code,
        }
