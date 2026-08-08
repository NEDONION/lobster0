"""``miniclaw eval`` 离线场景门禁的 CLI 测试。"""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCENARIO_ROOT = PROJECT_ROOT / "evals" / "scenarios"
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from miniclaw.cli import main  # noqa: E402


def run_cli(arguments: list[str]) -> tuple[int, str, str]:
    """调用 CLI，并把 argparse 退出也收窄为可断言的退出码。"""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = main(arguments)
        except SystemExit as error:
            code = int(error.code)
    return code, stdout.getvalue(), stderr.getvalue()


class CliEvalTest(unittest.TestCase):
    """验证 eval 子命令无需用户状态或模型凭据即可稳定运行。"""

    def test_list_prints_stable_case_metadata(self) -> None:
        """list 应展示 ID、状态、能力和标题，不执行场景。"""
        code, output, error = run_cli(["eval", "list", "--root", str(SCENARIO_ROOT)])

        self.assertEqual((code, error), (0, ""))
        lines = output.splitlines()
        self.assertEqual(len(lines), 36)
        self.assertTrue(lines[0].startswith("CORE-001 active core "))
        self.assertTrue(any(line.startswith("PROTO-001 active provider ") for line in lines))

    def test_validate_reports_case_count_without_initializing_state(self) -> None:
        """validate 只读场景目录，不创建或要求 MiniClaw home。"""
        with tempfile.TemporaryDirectory() as directory:
            missing_home = Path(directory) / "must-not-exist"
            code, output, error = run_cli(
                ["eval", "validate", "--root", str(SCENARIO_ROOT)]
            )

        self.assertEqual((code, output, error), (0, "Validated 36 eval cases.\n", ""))
        self.assertFalse(missing_home.exists())

    def test_run_offline_prints_pass_rows_and_summary(self) -> None:
        """offline suite 应运行全部 active case，并以 100% PASS 返回 0。"""
        code, output, error = run_cli(
            ["eval", "run", "--suite", "offline", "--root", str(SCENARIO_ROOT)]
        )

        self.assertEqual((code, error), (0, ""))
        self.assertIn("PASS CORE-001", output)
        self.assertIn("PASS SAFE-001", output)
        self.assertIn("Offline eval: 24/24 passed, 0 failed", output)

    def test_run_returns_one_and_only_short_codes_when_case_fails(self) -> None:
        """任一场景失败应返回 1，只打印 ID 和稳定短码。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_line = (SCENARIO_ROOT / "core.v1.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()[0]
            row = json.loads(first_line)
            row["expected"]["answer_contains"] = ["NEVER_PRESENT"]
            (root / "failed.jsonl").write_text(
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            code, output, error = run_cli(
                ["eval", "run", "--suite", "offline", "--root", str(root)]
            )

        self.assertEqual((code, error), (1, ""))
        self.assertIn("FAIL CORE-001 answer_missing", output)
        self.assertNotIn("NEVER_PRESENT", output)

    def test_run_channel_and_all_print_independent_gate_summaries(self) -> None:
        """Channel 12-case gate 可单跑，all 必须同时报告 Agent 与 Channel。"""
        channel_code, channel_output, channel_error = run_cli(
            ["eval", "run", "--suite", "channel", "--root", str(SCENARIO_ROOT)]
        )
        all_code, all_output, all_error = run_cli(
            ["eval", "run", "--suite", "all", "--root", str(SCENARIO_ROOT)]
        )

        self.assertEqual((channel_code, channel_error), (0, ""))
        self.assertIn("PASS FEISHU-DM-001", channel_output)
        self.assertIn("Channel eval: 12/12 passed, 0 failed", channel_output)
        self.assertEqual((all_code, all_error), (0, ""))
        self.assertIn("Offline eval: 24/24 passed, 0 failed", all_output)
        self.assertIn("Channel eval: 12/12 passed, 0 failed", all_output)

    def test_run_channel_repeat_reports_local_soak_evidence(self) -> None:
        """repeat 应重复真实 Channel 纵切，并只输出聚合的本地 soak 证据。"""
        code, output, error = run_cli(
            [
                "eval",
                "run",
                "--suite",
                "channel",
                "--repeat",
                "2",
                "--root",
                str(SCENARIO_ROOT),
            ]
        )

        self.assertEqual((code, error), (0, ""))
        self.assertIn("Channel local soak: 24/24 checks passed across 2/2 runs", output)
        self.assertNotIn("PASS FEISHU-DM-001", output)

    def test_run_rejects_repeat_outside_safe_bound(self) -> None:
        """repeat 必须是 1..1000，避免误输入制造无界本地任务。"""
        for value in ("0", "1001", "not-a-number"):
            with self.subTest(value=value):
                code, output, error = run_cli(
                    [
                        "eval",
                        "run",
                        "--suite",
                        "channel",
                        "--repeat",
                        value,
                        "--root",
                        str(SCENARIO_ROOT),
                    ]
                )

                self.assertEqual((code, output), (2, ""))
                self.assertIn("--repeat", error)

    def test_invalid_root_returns_configuration_exit_two(self) -> None:
        """无效场景目录必须给稳定错误和退出码 2。"""
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing"
            code, output, error = run_cli(
                ["eval", "validate", "--root", str(missing)]
            )

        self.assertEqual((code, output), (2, ""))
        self.assertIn("error: eval root is not a directory", error)

    def test_run_rejects_empty_offline_gate(self) -> None:
        """没有 active offline case 时不能用 0/0 制造伪通过。"""
        with tempfile.TemporaryDirectory() as directory:
            code, output, error = run_cli(
                ["eval", "run", "--suite", "offline", "--root", directory]
            )

        self.assertEqual((code, output), (2, ""))
        self.assertEqual(error, "error: no active offline eval cases\n")


if __name__ == "__main__":
    unittest.main()
