"""delegate_task 的收窄边界：工具集、预算与 depth-1。"""

import unittest

from lobster0.config import SubagentConfig
from lobster0.tools.base import ToolContext, ToolValidationError
from lobster0.tools.delegate import DelegateTaskTool, subagent_tool_names


class SubagentNarrowingTest(unittest.TestCase):
    """子 Agent 的可用工具只能是父的子集，且永远不含派发工具本身。"""

    def test_tools_are_intersected_with_what_the_parent_can_use(self) -> None:
        """声明里写了、但父此刻不可用的工具，不能因为声明就获得。

        父自己可能已经被上一层收窄过（例如自动化档），交集才是真实可用集。
        """
        declared = SubagentConfig(
            id="researcher",
            description="只读检索",
            tools=("read_file", "glob", "http_get"),
        )

        names = subagent_tool_names(declared, parent_tools=frozenset({"read_file", "glob"}))

        self.assertEqual(names, frozenset({"read_file", "glob"}))

    def test_delegate_task_is_always_removed(self) -> None:
        """max depth = 1 的实现点：子 Agent 根本看不到派发工具。

        配置层已经拒绝把它写进声明，这里是第二道——即使声明与父集都含它，
        子 Agent 也拿不到。
        """
        declared = SubagentConfig(
            id="researcher",
            description="只读检索",
            tools=("read_file", "delegate_task"),
        )

        names = subagent_tool_names(
            declared, parent_tools=frozenset({"read_file", "delegate_task"})
        )

        self.assertNotIn("delegate_task", names)
        self.assertEqual(names, frozenset({"read_file"}))

    def test_an_empty_intersection_is_refused(self) -> None:
        """一个工具都用不了的子 Agent 只会白烧一次模型调用。"""
        declared = SubagentConfig(
            id="researcher", description="只读检索", tools=("http_get",)
        )

        with self.assertRaises(ValueError):
            subagent_tool_names(declared, parent_tools=frozenset({"read_file"}))


class DelegateTaskToolTest(unittest.IsolatedAsyncioTestCase):
    """工具本身的参数校验与派发前置条件。"""

    def setUp(self) -> None:
        """准备一个声明了单个子 Agent 的工具实例。"""
        self.subagents = (
            SubagentConfig(
                id="researcher",
                description="只读检索与汇总",
                tools=("read_file", "glob"),
                max_turns=4,
                timeout_seconds=300,
            ),
        )
        self.dispatched: list[tuple[str, str, int]] = []

        async def dispatch(subagent, goal, *, timeout_seconds):
            self.dispatched.append((subagent.id, goal, timeout_seconds))
            return {"status": "succeeded", "summary": "已完成"}

        self.tool = DelegateTaskTool(self.subagents, dispatch=dispatch)

    def context(self, *, allowed: frozenset[str] | None = None) -> ToolContext:
        """返回一个最小可信 ToolContext。"""
        from pathlib import Path

        return ToolContext(
            1, 1, 1, Path("/state"), Path("/work"), (), allowed_tool_names=allowed
        )

    def test_description_lists_the_declared_subagents(self) -> None:
        """模型要能从工具描述里知道有谁可派，否则只能猜 id。"""
        description = DelegateTaskTool(self.subagents, dispatch=None).definition.description

        self.assertIn("researcher", description)
        self.assertIn("只读检索与汇总", description)

    def test_unknown_subagent_is_refused_at_validation(self) -> None:
        """未声明的 id 在校验阶段就拒绝，不进入派发。"""
        for identifier in ("ghost", "", "../etc", 1, None):
            with self.assertRaises(ToolValidationError):
                self.tool.validate({"subagent_id": identifier, "goal": "查一下"})

    def test_goal_must_be_a_bounded_non_empty_string(self) -> None:
        """子目标是隔离上下文里唯一的输入，空目标等于白跑一次。"""
        for goal in ("", "   ", "x" * 8001, 1, None):
            with self.assertRaises(ToolValidationError):
                self.tool.validate({"subagent_id": "researcher", "goal": goal})

    def test_timeout_cannot_exceed_the_declared_ceiling(self) -> None:
        """调用方只能调低，不能把声明的上限调高。"""
        narrowed = self.tool.validate(
            {"subagent_id": "researcher", "goal": "查一下", "timeout_seconds": 60}
        )
        self.assertEqual(narrowed["timeout_seconds"], 60)

        with self.assertRaises(ToolValidationError):
            self.tool.validate(
                {"subagent_id": "researcher", "goal": "查一下", "timeout_seconds": 9999}
            )

    async def test_a_subagent_cannot_delegate_again(self) -> None:
        """已经在子 Agent 里时（上下文不含派发工具）不允许再派发。"""
        result = await self.tool.execute(
            self.context(allowed=frozenset({"read_file"})),
            self.tool.validate({"subagent_id": "researcher", "goal": "查一下"}),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "subagent_depth_exceeded")
        self.assertEqual(self.dispatched, [])

    async def test_dispatch_receives_the_declared_timeout_by_default(self) -> None:
        """不传超时就用声明值，不是某个全局默认。"""
        await self.tool.execute(
            self.context(),
            self.tool.validate({"subagent_id": "researcher", "goal": "查一下"}),
        )

        self.assertEqual(self.dispatched, [("researcher", "查一下", 300)])

    async def test_dispatch_failure_is_reported_not_raised(self) -> None:
        """子任务失败不该让父回合直接崩，父 Agent 要能拿到原因自行决定。"""

        async def failing(subagent, goal, *, timeout_seconds):
            raise TimeoutError

        tool = DelegateTaskTool(self.subagents, dispatch=failing)

        result = await tool.execute(
            self.context(),
            tool.validate({"subagent_id": "researcher", "goal": "查一下"}),
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "subagent_timeout")


if __name__ == "__main__":
    unittest.main()
