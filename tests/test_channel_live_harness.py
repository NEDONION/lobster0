"""Telegram/Discord live harness 的默认安全与 evidence 契约。"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniclaw.evals import live as live_module
from miniclaw.evals.live import CHECKLIST, LiveHarnessError, run_live_harness
from miniclaw.gateway_lease import GatewayProvenance

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
        paths = SimpleNamespace(
            home=Path("/tmp/unused-home"),
            database=Path("unused.db"),
            logs=Path("unused-logs"),
        )
        for enabled, commit, dirty, peer, expected in (
            (False, "a" * 40, False, False, "channel is disabled"),
            (True, "unknown", False, False, "commit is unavailable"),
            (True, "a" * 40, True, False, "repository is dirty"),
            (True, "a" * 40, False, True, "peer channel is enabled"),
        ):
            with (
                self.subTest(enabled=enabled, commit=commit, dirty=dirty, peer=peer),
                tempfile.TemporaryDirectory() as directory,
                patch("miniclaw.evals.live._load_preflight") as preflight,
                patch(
                    "miniclaw.evals.live._repository_state",
                    return_value=(commit, dirty),
                ),
                patch("miniclaw.evals.live.ManagedGateway.start") as start,
            ):
                config = self._config("telegram", enabled=enabled, peer=peer)
                preflight.return_value = (paths, config, SimpleNamespace())
                output = Path(directory) / "evidence"

                with patch("sys.stderr") as stderr:
                    code = run_live_harness(
                        "telegram",
                        ["--confirm-live", "--output-dir", str(output)],
                    )

                self.assertEqual(code, 2)
                self.assertFalse(output.exists())
                start.assert_not_called()
                self.assertTrue(stderr.write.called)
                rendered = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
                self.assertIn(expected, rendered)

    def test_evidence_schema_is_redacted_and_skip_returns_nonzero(self) -> None:
        """只保存允许字段与匿名计数；任一 skip 必须返回 1。"""
        paths = SimpleNamespace(
            home=Path("/tmp/unused-home"),
            database=Path("unused.db"),
            logs=Path("unused-logs"),
        )
        config = self._config("discord")
        secrets = SimpleNamespace(
            model_api_key="MODEL_SECRET_SENTINEL",
            channel_tokens={"discord": "CHANNEL_SECRET_SENTINEL"},
        )
        gateway = SimpleNamespace(
            ready=True,
            provenance=GatewayProvenance(
                pid=123,
                started_at="2026-08-09T00:00:00.000000Z",
                commit="b" * 40,
            ),
            bounded_diagnostics=(),
            secret_match_count=0,
            stop=AsyncMock(return_value=0),
        )
        answers = iter(["s", *(["p"] * 14)])
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "miniclaw.evals.live._load_preflight",
                return_value=(paths, config, secrets),
            ),
            patch(
                "miniclaw.evals.live._repository_state",
                side_effect=(("b" * 40, False), ("b" * 40, False)),
            ),
            patch(
                "miniclaw.evals.live.ManagedGateway.start",
                new=AsyncMock(return_value=gateway),
            ),
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
            evidence_mode = stat_mode(files[0])

        self.assertEqual(code, 1)
        self.assertEqual(
            set(report),
            {
                "channel",
                "commit",
                "started_at",
                "finished_at",
                "gateway",
                "checks",
                "counts",
            },
        )
        self.assertEqual(report["channel"], "discord")
        self.assertEqual(
            set(report["gateway"]),
            {"ready", "graceful_exit", "pid", "started_at", "commit"},
        )
        self.assertEqual(report["counts"]["pass"], 14)
        self.assertEqual(report["counts"]["skip"], 1)
        self.assertEqual(report["gateway"]["commit"], "b" * 40)
        self.assertEqual(report["gateway"]["pid"], 123)
        self.assertEqual(report["counts"]["secret_matches"], 0)
        self.assertEqual(report["counts"]["repository_changed"], 0)
        self.assertEqual(report["counts"]["gateway_failures"], 0)
        self.assertEqual(report["counts"]["database"], {"inbox": 2})
        self.assertEqual(report["gateway"]["graceful_exit"], True)
        self.assertEqual(report["gateway"]["ready"], True)
        self.assertEqual(report["gateway"]["started_at"], "2026-08-09T00:00:00.000000Z")
        self.assertEqual(report["commit"], "b" * 40)
        self.assertEqual(report["checks"][0]["status"], "skip")
        self.assertEqual(report["checks"][-1]["status"], "pass")
        self.assertEqual(report["counts"]["fail"], 0)
        self.assertEqual(report["counts"]["pass"], 14)
        self.assertEqual(report["counts"]["skip"], 1)
        self.assertEqual(report["started_at"].endswith("+00:00"), True)
        self.assertEqual(report["finished_at"].endswith("+00:00"), True)
        self.assertEqual(evidence_mode, 0o600)
        gateway.stop.assert_awaited_once()
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("MODEL_SECRET_SENTINEL", serialized)
        self.assertNotIn("CHANNEL_SECRET_SENTINEL", serialized)

    def test_harness_owns_exact_ready_gateway_and_releases_it(self) -> None:
        """Discord Harness 必须自己启动绑定 commit 的进程并在结尾优雅释放。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = SimpleNamespace(
                home=root / "home",
                database=root / "miniclaw.db",
                logs=root / "logs",
            )
            config = self._config("discord", account_id="work")
            gateway = SimpleNamespace(
                ready=True,
                provenance=GatewayProvenance(
                    pid=456,
                    started_at="2026-08-09T00:00:00.000000Z",
                    commit="c" * 40,
                ),
                bounded_diagnostics=(),
                secret_match_count=0,
                stop=AsyncMock(return_value=0),
            )
            start = AsyncMock(return_value=gateway)
            with (
                patch(
                    "miniclaw.evals.live._load_preflight",
                    return_value=(paths, config, SimpleNamespace()),
                ),
                patch(
                    "miniclaw.evals.live._repository_state",
                    side_effect=(("c" * 40, False), ("c" * 40, False)),
                ),
                patch("miniclaw.evals.live.ManagedGateway.start", new=start),
                patch("miniclaw.evals.live._secret_match_count", return_value=0),
                patch("miniclaw.evals.live._database_counts", return_value={}),
                patch("builtins.input", return_value="p"),
            ):
                code = run_live_harness(
                    "discord",
                    ["--confirm-live", "--output-dir", str(root / "evidence")],
                )

        self.assertEqual(code, 0)
        self.assertEqual(
            start.await_args.kwargs["ready_line"],
            "MiniClaw gateway ready: discord/work",
        )
        self.assertEqual(start.await_args.kwargs["commit"], "c" * 40)
        gateway.stop.assert_awaited_once()

    def test_streamed_secret_match_overrides_human_pass(self) -> None:
        """完整输出流命中 Secret 时必须强制 secret_scan_zero 失败。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            paths = SimpleNamespace(
                home=root / "home",
                database=root / "miniclaw.db",
                logs=root / "logs",
            )
            gateway = SimpleNamespace(
                ready=True,
                provenance=GatewayProvenance(
                    pid=789,
                    started_at="2026-08-09T00:00:00.000000Z",
                    commit="d" * 40,
                ),
                bounded_diagnostics=(),
                secret_match_count=1,
                stop=AsyncMock(return_value=0),
            )
            with (
                patch(
                    "miniclaw.evals.live._load_preflight",
                    return_value=(paths, self._config("discord"), SimpleNamespace()),
                ),
                patch(
                    "miniclaw.evals.live._repository_state",
                    side_effect=(("d" * 40, False), ("d" * 40, False)),
                ),
                patch(
                    "miniclaw.evals.live.ManagedGateway.start",
                    new=AsyncMock(return_value=gateway),
                ),
                patch("miniclaw.evals.live._secret_match_count", return_value=0),
                patch("miniclaw.evals.live._database_counts", return_value={}),
                patch("builtins.input", return_value="p"),
            ):
                output = root / "evidence"
                code = run_live_harness(
                    "discord",
                    ["--confirm-live", "--output-dir", str(output)],
                )
                report = json.loads(next(output.glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(code, 1)
        self.assertEqual(report["counts"]["secret_matches"], 1)
        secret_check = next(
            item for item in report["checks"] if item["name"] == "secret_scan_zero"
        )
        self.assertEqual(secret_check["status"], "fail")

    def test_evidence_directory_is_owner_only_and_never_follows_symlink(self) -> None:
        """Evidence 最终目录必须为真实 0700 directory，不能跟随 symlink。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            target = root / "evidence"
            live_module._prepare_output_directory(target)
            self.assertEqual(stat_mode(target), 0o700)

            outside = root / "outside"
            outside.mkdir()
            linked = root / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(LiveHarnessError) as raised:
                live_module._prepare_output_directory(linked)

        self.assertEqual(str(raised.exception), "evidence directory is unsafe")

    @staticmethod
    def _config(
        channel: str,
        *,
        enabled: bool = True,
        peer: bool = False,
        account_id: str = "default",
    ) -> SimpleNamespace:
        """构造包含三个平台的最小 typed config 视图。"""
        values = {
            name: SimpleNamespace(
                enabled=(enabled if name == channel else peer and name == "feishu"),
                account_id=account_id if name == channel else "default",
            )
            for name in ("feishu", "telegram", "discord")
        }
        return SimpleNamespace(channels=SimpleNamespace(**values))


def stat_mode(path: Path) -> int:
    """返回测试 evidence 的 Unix permission bits。"""
    return path.stat().st_mode & 0o777


if __name__ == "__main__":
    unittest.main()
