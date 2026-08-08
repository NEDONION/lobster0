"""确定性 Agent 场景 runner 的真实链路测试。"""

import sys
import unittest
from dataclasses import replace
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.evals.cases import load_cases  # noqa: E402
from miniclaw.evals.runner import (  # noqa: E402
    ScriptedProvider,
    run_offline_case,
    run_offline_suite,
)
from miniclaw.providers.base import (  # noqa: E402
    ModelMessage,
    ModelRequest,
    ProviderProtocolError,
)


def repository_case(case_id: str):
    """按稳定 ID 读取仓库场景。"""
    cases = load_cases(PROJECT_ROOT / "evals" / "scenarios")
    return next(case for case in cases if case.id == case_id)


class ScriptedProviderTest(unittest.IsolatedAsyncioTestCase):
    """验证离线 Provider 只提供最小、可预测的模型边界。"""

    async def test_returns_responses_in_order_and_records_requests(self) -> None:
        """脚本结果必须逐个消费，并保留 Agent 实际发出的请求供 verifier 使用。"""
        case = repository_case("STATE-001")
        provider = ScriptedProvider(case.responses)
        first = ModelRequest(model="test", messages=(ModelMessage("user", "first"),))
        second = ModelRequest(model="test", messages=(ModelMessage("user", "second"),))

        first_response = await provider.complete(first)
        second_response = await provider.complete(second)

        self.assertEqual(first_response.content, "我记住了本次会话代号 ALPHA-27。")
        self.assertEqual(second_response.content, "刚才的代号是 ALPHA-27。")
        self.assertEqual(provider.requests, [first, second])

    async def test_exhausted_script_raises_stable_protocol_error(self) -> None:
        """响应不足是场景配置错误，不能变成索引异常或访问真实模型。"""
        provider = ScriptedProvider(())
        request = ModelRequest(model="test", messages=(ModelMessage("user", "hello"),))

        with self.assertRaisesRegex(ProviderProtocolError, "scripted responses exhausted"):
            await provider.complete(request)


class OfflineEvalRunnerTest(unittest.IsolatedAsyncioTestCase):
    """验证场景经过真实 Turn、Policy、Tool 与 SQLite 后再判定结果。"""

    async def test_read_file_case_passes_real_tool_and_grounding_checks(self) -> None:
        """文件哨兵必须由 Tool Result 进入下一次模型请求并留下成功 ToolRun。"""
        result = await run_offline_case(repository_case("FILE-READ-001"))

        self.assertTrue(result.passed)
        self.assertEqual(result.failures, ())
        self.assertEqual(result.tool_runs, (("read_file", "succeeded"),))
        self.assertEqual(result.audit_events, ("tool.started", "tool.succeeded"))
        self.assertEqual(result.request_count, 2)

    async def test_sensitive_file_case_records_denial_without_tool_run_or_leak(self) -> None:
        """敏感路径只允许脱敏拒绝审计，不能创建 ToolRun 或泄露合成内容。"""
        result = await run_offline_case(repository_case("SAFE-001"))

        self.assertTrue(result.passed)
        self.assertEqual(result.tool_runs, ())
        self.assertEqual(result.audit_events, ("tool.denied",))

    async def test_mismatch_returns_short_failure_without_stopping_suite(self) -> None:
        """断言失败只返回稳定短码，后续场景仍会执行。"""
        original = repository_case("CORE-001")
        mismatch = replace(
            original,
            id="CORE-999",
            expected=replace(original.expected, answer_contains=("NEVER_PRESENT",)),
        )

        suite = await run_offline_suite((mismatch, original))

        self.assertEqual(suite.total, 2)
        self.assertEqual(suite.passed, 1)
        self.assertEqual(suite.failed, 1)
        self.assertEqual(suite.cases[0].failures, ("answer_missing",))
        self.assertTrue(suite.cases[1].passed)

    async def test_exhausted_responses_become_sanitized_execution_failure(self) -> None:
        """场景脚本耗尽不能让整个 suite 崩溃或回显内部响应。"""
        original = repository_case("FILE-READ-001")
        exhausted = replace(original, responses=original.responses[:1])

        result = await run_offline_case(exhausted)

        self.assertFalse(result.passed)
        self.assertEqual(result.failures, ("execution_error",))

    async def test_write_approval_case_executes_continuation_and_checks_file(self) -> None:
        """审批场景必须走真实 waiting/consume/child Turn 并验证文件副作用。"""
        result = await run_offline_case(repository_case("WRITE-APPROVE-001"))

        self.assertTrue(result.passed, result.failures)
        self.assertEqual(result.tool_runs, (("write_file", "succeeded"),))
        self.assertEqual(result.approval_statuses, ("consumed",))

    async def test_memory_autopilot_case_runs_fixed_production_fixture(self) -> None:
        """Memory versioned case 必须执行封闭 fixture 并返回脱敏 evidence。"""
        result = await run_offline_case(repository_case("MEM-AUTO-001"))

        self.assertTrue(result.passed, result.failures)
        self.assertEqual(
            result.memory_evidence,
            ("owner_space_shared", "group_denied", "non_owner_denied"),
        )
        self.assertEqual(result.request_count, 0)

    async def test_all_repository_cases_pass_offline_gate(self) -> None:
        """当前发布的所有 active offline case 必须 100% 通过。"""
        cases = tuple(
            case
            for case in load_cases(PROJECT_ROOT / "evals" / "scenarios")
            if case.status == "active" and "offline" in case.layers
        )

        suite = await run_offline_suite(cases)

        self.assertEqual(suite.total, 39)
        self.assertEqual(suite.passed, 39, suite.cases)
        self.assertEqual(suite.failed, 0)


if __name__ == "__main__":
    unittest.main()
