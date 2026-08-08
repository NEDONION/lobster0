"""Telegram/Discord live harness 的默认安全与 evidence 契约。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from miniclaw.evals.live import CHECKLIST, run_live_harness

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ChannelLiveHarnessTest(unittest.TestCase):
    """保证真实验收脚本不联网发信、不泄密，也不把跳过写成通过。"""

    def test_scripts_require_confirmation_before_any_state_or_output(self) -> None:
        """未确认时两个脚本都退出 2，且不会创建 home/evidence。"""
        for channel in ("telegram", "discord"):
            with self.subTest(channel=channel), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                home = root / "must-not-exist"
                output = root / "must-not-exist-evidence"
                result = subprocess.run(
                    [
                        sys.executable,
                        str(PROJECT_ROOT / "scripts" / f"{channel}_live_smoke.py"),
                        "--home",
                        str(home),
                        "--output-dir",
                        str(output),
                    ],
                    cwd=PROJECT_ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )

                self.assertEqual(result.returncode, 2)
                self.assertIn("--confirm-live is required", result.stderr)
                self.assertFalse(home.exists())
                self.assertFalse(output.exists())

    def test_harnesses_have_exact_fifteen_stable_checks_and_no_send_calls(self) -> None:
        """清单覆盖设计的 15 项，入口源码不得调用平台发送 API。"""
        self.assertEqual(len(CHECKLIST), 15)
        self.assertEqual(len(set(CHECKLIST)), 15)
        for name in CHECKLIST:
            self.assertRegex(name, r"^[a-z][a-z0-9_]+$")
        sources = "\n".join(
            (PROJECT_ROOT / "scripts" / name).read_text(encoding="utf-8")
            for name in ("telegram_live_smoke.py", "discord_live_smoke.py")
        )
        self.assertNotIn("send_message(", sources)
        self.assertNotIn(".send(", sources)

    def test_confirmed_run_fails_closed_when_channel_disabled_or_commit_missing(self) -> None:
        """enabled config 与 40 位 commit 任一缺失都不能进入人工提示或写 evidence。"""
        paths = SimpleNamespace(database=Path("unused.db"), logs=Path("unused-logs"))
        for enabled, commit, expected in (
            (False, "a" * 40, "channel is disabled"),
            (True, "unknown", "commit is unavailable"),
        ):
            with (
                self.subTest(enabled=enabled, commit=commit),
                tempfile.TemporaryDirectory() as directory,
                patch("miniclaw.evals.live._load_preflight") as preflight,
                patch("miniclaw.evals.live._commit", return_value=commit),
            ):
                config = SimpleNamespace(
                    channels=SimpleNamespace(telegram=SimpleNamespace(enabled=enabled))
                )
                preflight.return_value = (paths, config, SimpleNamespace())
                output = Path(directory) / "evidence"

                with patch("sys.stderr") as stderr:
                    code = run_live_harness(
                        "telegram",
                        ["--confirm-live", "--output-dir", str(output)],
                    )

                self.assertEqual(code, 2)
                self.assertFalse(output.exists())
                self.assertTrue(stderr.write.called)
                rendered = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
                self.assertIn(expected, rendered)

    def test_evidence_schema_is_redacted_and_skip_returns_nonzero(self) -> None:
        """只保存允许字段与匿名计数；任一 skip 必须返回 1。"""
        paths = SimpleNamespace(database=Path("unused.db"), logs=Path("unused-logs"))
        config = SimpleNamespace(
            channels=SimpleNamespace(discord=SimpleNamespace(enabled=True))
        )
        secrets = SimpleNamespace(
            model_api_key="MODEL_SECRET_SENTINEL",
            channel_tokens={"discord": "CHANNEL_SECRET_SENTINEL"},
        )
        answers = iter(["s", *(["p"] * 14)])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "miniclaw.evals.live._load_preflight",
                return_value=(paths, config, secrets),
            ),
            patch("miniclaw.evals.live._commit", return_value="b" * 40),
            patch("miniclaw.evals.live._database_counts", return_value={"inbox": 2}),
            patch("miniclaw.evals.live._secret_match_count", return_value=0),
            patch("builtins.input", side_effect=lambda _: next(answers)),
        ):
            output = Path(directory) / "evidence"
            code = run_live_harness(
                "discord",
                ["--confirm-live", "--output-dir", str(output)],
            )
            files = list(output.glob("*.json"))
            self.assertEqual(len(files), 1)
            report = json.loads(files[0].read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertEqual(
            set(report),
            {"channel", "commit", "started_at", "finished_at", "checks", "counts"},
        )
        self.assertEqual(report["channel"], "discord")
        self.assertEqual(report["counts"]["pass"], 14)
        self.assertEqual(report["counts"]["skip"], 1)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("MODEL_SECRET_SENTINEL", serialized)
        self.assertNotIn("CHANNEL_SECRET_SENTINEL", serialized)


if __name__ == "__main__":
    unittest.main()
