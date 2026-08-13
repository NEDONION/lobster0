"""子 Agent 派发链路：收窄、隔离与结算。"""

import unittest
from types import SimpleNamespace

from lobster0.agent.turn import TurnExecutionProfile
from lobster0.config import SubagentConfig
from lobster0.subagents.dispatch import SubagentDispatcher


class _Runs:
    """记录子 Run 的建立与结算。"""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.finished: list[tuple[int, str]] = []
        self.next_id = 100

    def enqueue_child(self, task, *, parent_run_id, subagent_id, scheduled_for, **rest):
        del rest
        self.next_id += 1
        self.created.append(
            {
                "parent_run_id": parent_run_id,
                "subagent_id": subagent_id,
                "task_id": task.id,
            }
        )
        return SimpleNamespace(id=self.next_id, task_id=task.id)

    def mark_running(self, run_id, worker_id, *, now):
        del worker_id, now
        return SimpleNamespace(id=run_id)

    def finish(self, run_id, *, status, now, worker_id, **rest):
        del now, worker_id, rest
        self.finished.append((run_id, status.value))
        return SimpleNamespace(id=run_id)


class SubagentDispatcherTest(unittest.IsolatedAsyncioTestCase):
    """派发必须真的跑一次受限回合，而不是假装成功。"""

    def setUp(self) -> None:
        """准备一个只读子 Agent 与记录型依赖。"""
        self.subagent = SubagentConfig(
            id="researcher",
            description="只读检索",
            tools=("read_file", "glob"),
            max_turns=3,
            timeout_seconds=120,
        )
        self.runs = _Runs()
        self.calls: list[TurnExecutionProfile] = []

        async def handle_automation(*, task_id, task_run_id, text, profile):
            del task_id, task_run_id
            self.calls.append(profile)
            self.text = text
            return SimpleNamespace(
                content="子任务完成", input_tokens=10, output_tokens=3, error_code=None
            )

        self.service = SimpleNamespace(handle_automation=handle_automation)
        self.dispatcher = SubagentDispatcher(
            service=self.service,
            runs=self.runs,
            task=SimpleNamespace(id=1),
            parent_run_id=7,
            parent_tools=frozenset({"read_file", "glob", "run_command", "delegate_task"}),
        )

    async def test_dispatch_runs_a_turn_with_the_narrowed_tool_set(self) -> None:
        """子回合只能用交集，且永远不含派发工具本身。"""
        await self.dispatcher.dispatch(self.subagent, "查一下", timeout_seconds=60)

        profile = self.calls[0]
        self.assertEqual(profile.allowed_tool_names, frozenset({"read_file", "glob"}))
        self.assertEqual(profile.source, "automation")

    async def test_the_child_run_is_recorded_and_settled(self) -> None:
        """派发要留下 durable 痕迹，否则重启后无从恢复也无从展示。"""
        await self.dispatcher.dispatch(self.subagent, "查一下", timeout_seconds=60)

        self.assertEqual(
            self.runs.created,
            [{"parent_run_id": 7, "subagent_id": "researcher", "task_id": 1}],
        )
        self.assertEqual(self.runs.finished, [(101, "succeeded")])

    async def test_only_the_goal_crosses_into_the_child(self) -> None:
        """上下文默认隔离：子 Agent 只拿到父明确写下的子目标。"""
        await self.dispatcher.dispatch(self.subagent, "只看 README", timeout_seconds=60)

        self.assertEqual(self.text, "只看 README")

    async def test_a_failing_turn_settles_the_run_as_failed(self) -> None:
        """子回合失败要落到 Run 状态上，不能只在返回值里说一声。"""

        async def failing(*, task_id, task_run_id, text, profile):
            del task_id, task_run_id, text, profile
            return SimpleNamespace(
                content="", input_tokens=0, output_tokens=0, error_code="provider_timeout"
            )

        dispatcher = SubagentDispatcher(
            service=SimpleNamespace(handle_automation=failing),
            runs=self.runs,
            task=SimpleNamespace(id=1),
            parent_run_id=7,
            parent_tools=frozenset({"read_file", "glob"}),
        )

        outcome = await dispatcher.dispatch(self.subagent, "查一下", timeout_seconds=60)

        self.assertEqual(outcome["status"], "failed")
        self.assertEqual(self.runs.finished, [(101, "failed")])

    async def test_an_empty_tool_intersection_is_refused_before_any_run(self) -> None:
        """一个工具都用不了就不该建 Run，更不该烧一次模型调用。"""
        dispatcher = SubagentDispatcher(
            service=self.service,
            runs=self.runs,
            task=SimpleNamespace(id=1),
            parent_run_id=7,
            parent_tools=frozenset({"run_command"}),
        )

        with self.assertRaises(ValueError):
            await dispatcher.dispatch(self.subagent, "查一下", timeout_seconds=60)

        self.assertEqual(self.runs.created, [])


if __name__ == "__main__":
    unittest.main()
