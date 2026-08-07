"""Agent 场景 JSONL 契约与仓库数据集测试。"""

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.evals.cases import EvalCaseError, load_cases  # noqa: E402


def valid_case(case_id: str = "CORE-001") -> dict[str, object]:
    """返回可按测试需要局部修改的最小合法场景。"""
    return {
        "schema_version": 1,
        "id": case_id,
        "title": "basic greeting",
        "status": "active",
        "layers": ["offline"],
        "capability": "core",
        "query": "你好，你是谁？",
        "setup": {"files": {"notes/hello.txt": "MINICLAW_SENTINEL"}},
        "offline": {
            "responses": [
                {
                    "content": "我是 MiniClaw。",
                    "tool_calls": [],
                    "reasoning_content": None,
                    "finish_reason": "stop",
                    "input_tokens": 2,
                    "output_tokens": 3,
                    "provider_request_id": "offline-1",
                }
            ]
        },
        "expected": {
            "answer_contains": ["MiniClaw"],
            "answer_excludes": [],
            "tool_runs": [],
            "tool_statuses": {},
            "audit_events": [],
            "request_contains": ["你好"],
            "max_tool_runs": 0,
        },
        "introduced_by": "initial-suite",
        "tags": ["smoke"],
    }


def write_cases(root: Path, name: str, rows: list[dict[str, object]]) -> Path:
    """把测试场景逐行写成 UTF-8 JSONL。"""
    path = root / name
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


class EvalCaseLoaderTest(unittest.TestCase):
    """验证版本化场景在进入 runner 前已严格且安全地收窄。"""

    def test_loads_valid_files_in_stable_file_and_line_order(self) -> None:
        """多个 JSONL 必须按文件名和行号产生稳定顺序与来源。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_cases(root, "b.jsonl", [valid_case("CORE-003")])
            write_cases(root, "a.jsonl", [valid_case("CORE-001"), valid_case("CORE-002")])

            cases = load_cases(root)

        self.assertEqual([case.id for case in cases], ["CORE-001", "CORE-002", "CORE-003"])
        self.assertEqual(cases[0].source, "a.jsonl:1")
        self.assertEqual(cases[0].setup_files, (("notes/hello.txt", "MINICLAW_SENTINEL"),))
        self.assertEqual(cases[0].responses[0].content, "我是 MiniClaw。")

    def test_rejects_duplicate_case_ids_across_files(self) -> None:
        """重复 ID 必须在执行前失败并报告两个安全来源位置。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_cases(root, "a.jsonl", [valid_case()])
            write_cases(root, "b.jsonl", [valid_case()])

            with self.assertRaisesRegex(EvalCaseError, r"duplicate case id CORE-001.*a.jsonl:1"):
                load_cases(root)

    def test_rejects_unknown_top_level_and_nested_fields(self) -> None:
        """拼错或未定义字段不能被静默忽略。"""
        for mutation, expected in (
            (lambda case: case.update({"surprise": True}), "unknown field surprise"),
            (
                lambda case: case["expected"].update({"answer_include": ["x"]}),
                "unknown field answer_include",
            ),
        ):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = valid_case()
                mutation(case)
                write_cases(root, "cases.jsonl", [case])

                with self.assertRaisesRegex(EvalCaseError, expected):
                    load_cases(root)

    def test_rejects_invalid_status_and_layer(self) -> None:
        """状态与执行层必须来自当前版本的固定枚举。"""
        for field, value in (("status", "enabled"), ("layers", ["production"])):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = valid_case()
                case[field] = value
                write_cases(root, "cases.jsonl", [case])

                with self.assertRaises(EvalCaseError):
                    load_cases(root)

    def test_rejects_absolute_parent_and_empty_setup_paths(self) -> None:
        """合成 fixture 也只能写临时 workspace 内的相对文件。"""
        for unsafe in ("/tmp/private", "../outside.txt", "", "."):
            with self.subTest(path=unsafe), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = valid_case()
                case["setup"] = {"files": {unsafe: "synthetic"}}
                write_cases(root, "cases.jsonl", [case])

                with self.assertRaisesRegex(EvalCaseError, "setup file path is unsafe"):
                    load_cases(root)

    def test_rejects_secret_bearing_field_names(self) -> None:
        """场景 Schema 不提供凭据槽位，防止把真实密钥误提交为数据。"""
        for field in ("api_key", "token", "client_secret"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                case = valid_case()
                case[field] = "do-not-store"
                write_cases(root, "cases.jsonl", [case])

                with self.assertRaisesRegex(EvalCaseError, "credential-like field"):
                    load_cases(root)

    def test_json_error_reports_location_without_echoing_case_text(self) -> None:
        """坏 JSON 的错误只包含文件行号，不能回显潜在敏感原文。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret_text = "SHOULD_NOT_BE_ECHOED"
            (root / "broken.jsonl").write_text(
                f'{{"id":"CORE-001","note":"{secret_text}"\n',
                encoding="utf-8",
            )

            with self.assertRaises(EvalCaseError) as captured:
                load_cases(root)

        self.assertIn("broken.jsonl:1", str(captured.exception))
        self.assertNotIn(secret_text, str(captured.exception))

    def test_rejects_non_standard_json_constants(self) -> None:
        """NaN 和 Infinity 不是标准 JSON，不能进入 Tool arguments。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            case = valid_case()
            response = case["offline"]["responses"][0]
            response["tool_calls"] = [
                {
                    "call_id": "call_bad",
                    "name": "system_info",
                    "arguments": {"value": float("nan")},
                }
            ]
            write_cases(root, "cases.jsonl", [case])

            with self.assertRaisesRegex(EvalCaseError, "invalid JSON at cases.jsonl:1"):
                load_cases(root)


class RepositoryEvalSuiteTest(unittest.TestCase):
    """保证随代码提交的首批 Claw-like 场景始终可执行且可追溯。"""

    def test_active_repository_suite_matches_documented_gate(self) -> None:
        """仓库 active 场景数、能力覆盖和 README 门禁数必须一致。"""
        cases = load_cases(PROJECT_ROOT / "evals" / "scenarios")
        active = [case for case in cases if case.status == "active"]
        readme = (PROJECT_ROOT / "evals" / "README.md").read_text(encoding="utf-8")
        match = re.search(r"Active offline gate: (\d+) cases", readme)

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(len(active), int(match.group(1)))
        self.assertEqual(
            {case.capability for case in active},
            {"core", "provider", "tools", "safety", "state", "error"},
        )
        self.assertTrue(all("offline" in case.layers and case.responses for case in active))
        self.assertEqual(len({case.id for case in cases}), len(cases))

    def test_proto_001_is_mapped_to_provider_regression(self) -> None:
        """真实 provider 事故 ID 必须同时存在于场景数据和精确 SSE 单测。"""
        cases = load_cases(PROJECT_ROOT / "evals" / "scenarios")
        provider_tests = (PROJECT_ROOT / "tests" / "test_openai_compatible_provider.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("PROTO-001", {case.id for case in cases})
        self.assertIn("[PROTO-001]", provider_tests)


if __name__ == "__main__":
    unittest.main()
