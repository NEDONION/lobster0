"""Phase 6.5 Browser Agent versioned regression suite 测试。"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from miniclaw.evals.browser import run_browser_suite
from miniclaw.evals.cases import EvalCaseError, load_browser_cases

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = PROJECT_ROOT / "evals" / "scenarios"


class BrowserEvalTest(unittest.TestCase):
    """验证 18 条 Browser case 的 schema、真实组件执行与脱敏结果。"""

    def test_browser_v1_has_all_required_cases(self) -> None:
        """版本化数据集必须精确包含 BROWSER-001..018 且顺序稳定。"""
        cases = load_browser_cases(SCENARIO_ROOT)

        self.assertEqual(
            [case.id for case in cases],
            [f"BROWSER-{index:03d}" for index in range(1, 19)],
        )
        self.assertTrue(all(case.browser_fixture for case in cases))
        self.assertTrue(all(case.expected.browser_evidence for case in cases))

    def test_browser_schema_rejects_unknown_fields_and_duplicate_ids(self) -> None:
        """Browser payload 拼写错误和重复 ID 必须在执行前失败关闭。"""
        first = json.loads(
            (SCENARIO_ROOT / "browser.v1.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = dict(first)
            invalid["browser"] = {**invalid["browser"], "unknown": True}
            (root / "browser.v1.jsonl").write_text(
                json.dumps(invalid, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvalCaseError, "unknown field"):
                load_browser_cases(root)
            (root / "browser.v1.jsonl").write_text(
                "\n".join(json.dumps(first, ensure_ascii=False) for _ in range(2)) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(EvalCaseError, "duplicate case id"):
                load_browser_cases(root)

    def test_all_cases_pass_real_offline_components_without_sensitive_output(self) -> None:
        """Policy、Tool、Protocol、Artifact 与生命周期纵切必须 18/18 PASS。"""
        suite = asyncio.run(run_browser_suite(load_browser_cases(SCENARIO_ROOT)))

        self.assertEqual((suite.passed, suite.failed, suite.total), (18, 0, 18))
        self.assertTrue(all(result.passed for result in suite.cases))
        serialized = repr(suite).lower()
        self.assertNotIn("private typed value", serialized)
        self.assertNotIn("must-not-display", serialized)
        self.assertNotIn("staging_path", serialized)


if __name__ == "__main__":
    unittest.main()
