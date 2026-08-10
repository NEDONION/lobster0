"""Phase 6 Automation versioned regression suite 测试。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from lobster0.evals.automation import run_automation_suite
from lobster0.evals.cases import EvalCaseError, load_automation_cases

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = PROJECT_ROOT / "evals" / "scenarios"


class AutomationEvalTest(unittest.TestCase):
    """验证 15 条 Automation case 的 schema、执行与脱敏结果。"""

    def test_automation_v1_has_all_required_cases(self) -> None:
        """版本化数据集必须精确包含 AUTO-001..015 且顺序稳定。"""
        cases = load_automation_cases(SCENARIO_ROOT)

        self.assertEqual(len(cases), 15)
        self.assertEqual(
            [case.id for case in cases],
            [f"AUTO-{index:03d}" for index in range(1, 16)],
        )
        self.assertTrue(all(case.automation_fixture is not None for case in cases))
        self.assertTrue(all(case.expected.automation_status for case in cases))

    def test_automation_schema_rejects_unknown_fields_and_duplicate_ids(self) -> None:
        """Automation payload 拼写错误和重复 ID 必须在执行前失败关闭。"""
        first = json.loads(
            (SCENARIO_ROOT / "automation.v1.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()[0]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = dict(first)
            invalid["automation"] = {**invalid["automation"], "unknown": True}
            (root / "automation.v1.jsonl").write_text(
                json.dumps(invalid, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvalCaseError, "unknown field"):
                load_automation_cases(root)
            (root / "automation.v1.jsonl").write_text(
                "\n".join(json.dumps(first, ensure_ascii=False) for _ in range(2)) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvalCaseError, "duplicate case id"):
                load_automation_cases(root)

    def test_all_cases_pass_real_offline_components_without_sensitive_output(self) -> None:
        """固定时钟、Fake backend/transport 的真实组件纵切必须 15/15 PASS。"""
        suite = asyncio.run(run_automation_suite(load_automation_cases(SCENARIO_ROOT)))

        self.assertEqual((suite.passed, suite.failed, suite.total), (15, 0, 15))
        self.assertTrue(all(result.passed for result in suite.cases))
        serialized = repr(suite).lower()
        self.assertNotIn("secret_sentinel", serialized)
        self.assertNotIn("private prompt", serialized)
        self.assertNotIn("conversation_id", serialized)


if __name__ == "__main__":
    unittest.main()
