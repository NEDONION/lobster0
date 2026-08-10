"""Phase 6 macOS + 飞书 production gate 的编排与报告测试。"""

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

from lobster0.evals.feishu_automation_live import (
    AutomationLiveCaseResult,
    build_automation_evidence_report,
)
from lobster0.evals.feishu_live import FeishuCaseResult, build_evidence_report
from lobster0.evals.phase6_production import (
    GateEvidence,
    GatewayLeaseFact,
    ProductionPreflightFacts,
    RecoveryResult,
    build_production_report,
    evaluate_production_preflight,
    load_gate_evidence,
    run_managed_recovery,
    run_phase6_production_gate,
)
from lobster0.evals.phase6_soak import SoakCheckpoint
from lobster0.evals.production_evidence import (
    build_seatbelt_evidence_report,
    write_private_json,
)
from lobster0.gateway_lease import GatewayProvenance
from lobster0.gateway_service import ServiceStatus


class Phase6ProductionPreflightTest(unittest.TestCase):
    """验证 preflight 严格绑定平台、服务和三份同 commit Evidence。"""

    def setUp(self) -> None:
        """创建固定的全部 PASS aggregate。"""
        self.commit = "a" * 40
        self.started = "2026-08-10T00:00:00.000000Z"
        self.finished = "2026-08-10T00:10:00.000000Z"
        self.evidence = (
            GateEvidence("seatbelt", self.commit, "verified", 2, 2, 0, "1" * 64),
            GateEvidence("feishu_channel", self.commit, "verified", 15, 15, 0, "2" * 64),
            GateEvidence("feishu_automation", self.commit, "verified", 10, 10, 0, "3" * 64),
        )
        self.facts = ProductionPreflightFacts(
            platform="darwin",
            managed_python_312=True,
            repository_commit=self.commit,
            repository_clean=True,
            service_owned=True,
            service_status=ServiceStatus(True, True, True),
            gateway_lease_fresh=True,
            owner_only_state=True,
            evidence=self.evidence,
            secret_matches=0,
        )

    def test_healthy_preflight_passes_and_each_failure_has_stable_code(self) -> None:
        """所有前置事实成立才返回空 violation。"""
        self.assertEqual(evaluate_production_preflight(self.facts), ())
        cases = {
            "platform_unsupported": replace(self.facts, platform="linux"),
            "managed_python_invalid": replace(self.facts, managed_python_312=False),
            "repository_dirty": replace(self.facts, repository_clean=False),
            "service_unowned": replace(self.facts, service_owned=False),
            "service_not_running": replace(
                self.facts, service_status=ServiceStatus(True, True, False)
            ),
            "gateway_lease_unhealthy": replace(self.facts, gateway_lease_fresh=False),
            "state_permissions_unsafe": replace(self.facts, owner_only_state=False),
            "secret_match": replace(self.facts, secret_matches=1),
            "feishu_channel_evidence_invalid": replace(
                self.facts,
                evidence=(
                    self.evidence[0],
                    replace(self.evidence[1], status="failed"),
                    self.evidence[2],
                ),
            ),
            "evidence_commit_mismatch": replace(
                self.facts,
                evidence=(replace(self.evidence[0], commit="b" * 40), *self.evidence[1:]),
            ),
        }
        for code, facts in cases.items():
            with self.subTest(code=code):
                violations = evaluate_production_preflight(facts)
                self.assertIn(code, tuple(item.code for item in violations))

    def test_cli_requires_subcommand_and_confirmation_before_confirmed_loader(self) -> None:
        """无 subcommand/confirm 时不读取 Secret、不创建目录也不启动服务。"""
        with mock.patch(
            "lobster0.evals.phase6_production._run_confirmed_command"
        ) as confirmed:
            self.assertEqual(run_phase6_production_gate([]), 2)
            self.assertEqual(
                run_phase6_production_gate(
                    ["preflight", "--evidence-dir", "/private/does-not-exist"]
                ),
                2,
            )
        confirmed.assert_not_called()


class Phase6ProductionEvidenceTest(unittest.TestCase):
    """验证三类 private Evidence 的 schema、权限、hash 与 commit 聚合。"""

    def setUp(self) -> None:
        """创建真实 schema 的 2+15+10 PASS Evidence。"""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.root.chmod(0o700)
        self.commit = "a" * 40
        started = "2026-08-10T00:00:00.000000Z"
        finished = "2026-08-10T00:10:00.000000Z"
        seatbelt = self.root / "seatbelt"
        channel = self.root / "feishu-channel"
        automation = self.root / "feishu-automation"
        for directory in (seatbelt, channel, automation):
            directory.mkdir(mode=0o700)
        for probe in ("python", "node-chain"):
            write_private_json(
                seatbelt / f"{probe}.json",
                build_seatbelt_evidence_report(
                    commit=self.commit,
                    probe=probe,
                    started_at=started,
                    finished_at=finished,
                    contained=True,
                    secret_matches=0,
                ),
            )
        channel_report = build_evidence_report(
            commit=self.commit,
            started_at=started,
            finished_at=finished,
            gateway_ready=True,
            gateway_graceful_exit=True,
            gateway_provenance=GatewayProvenance(123, started, self.commit),
            results=tuple(
                FeishuCaseResult(f"FEISHU-LIVE-{index:03d}", "pass", (), (), (), None)
                for index in range(1, 16)
            ),
            secret_matches=0,
        )
        write_private_json(channel / "channel.json", channel_report)
        automation_report = build_automation_evidence_report(
            commit=self.commit,
            started_at=started,
            finished_at=finished,
            results=tuple(
                AutomationLiveCaseResult(
                    f"FEISHU-AUTO-{index:03d}", "pass", (), (), None, None
                )
                for index in range(1, 11)
            ),
            secret_matches=0,
        )
        write_private_json(automation / "automation.json", automation_report)

    def test_loads_exact_verified_evidence_and_rejects_unsafe_or_tampered_files(self) -> None:
        """只有 owner-only、schema-valid、同 commit 的精确 2/15/10 才通过。"""
        with mock.patch.object(
            Path,
            "read_bytes",
            side_effect=AssertionError("validated evidence must not be reopened by path"),
        ):
            evidence = load_gate_evidence(self.root, expected_commit=self.commit)
        self.assertEqual(tuple((item.kind, item.passed, item.total) for item in evidence), (
            ("seatbelt", 2, 2),
            ("feishu_channel", 15, 15),
            ("feishu_automation", 10, 10),
        ))

        channel_file = self.root / "feishu-channel" / "channel.json"
        channel_file.chmod(0o644)
        with self.assertRaisesRegex(RuntimeError, "evidence_file_unsafe"):
            load_gate_evidence(self.root, expected_commit=self.commit)
        channel_file.chmod(0o600)
        data = json.loads(channel_file.read_text(encoding="utf-8"))
        data["counts"]["cases_passed"] = 14
        channel_file.write_text(json.dumps(data), encoding="utf-8")
        channel_file.chmod(0o600)
        with self.assertRaisesRegex(RuntimeError, "feishu_channel_evidence_invalid"):
            load_gate_evidence(self.root, expected_commit=self.commit)


class Phase6ProductionRecoveryTest(unittest.TestCase):
    """验证 restart 只使用受管 service API 且 exactly-one Delivery。"""

    def test_managed_restart_changes_lease_and_delivers_probe_once(self) -> None:
        """健康恢复必须观察新 lease、保留 Approval 并仅投递一次。"""
        commit = "a" * 40
        leases = iter(
            (
                GatewayLeaseFact("1" * 64, commit, True),
                GatewayLeaseFact("2" * 64, commit, True),
            )
        )
        restarted: list[bool] = []
        result = run_managed_recovery(
            restart=lambda: restarted.append(True),
            read_gateway=lambda: next(leases),
            send_probe=lambda: "probe-token",
            count_probe_deliveries=lambda _token: 1,
            approval_is_stable=lambda: True,
            wait=lambda _seconds: None,
            attempts=1,
        )
        self.assertEqual(result, RecoveryResult("passed", None, 1))
        self.assertEqual(restarted, [True])

    def test_restart_stale_lease_duplicate_delivery_and_approval_loss_fail(self) -> None:
        """恢复不完整时返回固定码，不能继续 soak。"""
        commit = "a" * 40
        old = GatewayLeaseFact("1" * 64, commit, True)
        scenarios = {
            "gateway_not_restarted": (lambda: old, lambda _token: 1, lambda: True),
            "recovery_delivery_duplicate": (
                iter((old, GatewayLeaseFact("2" * 64, commit, True))).__next__,
                lambda _token: 2,
                lambda: True,
            ),
            "approval_recovery_failed": (
                iter((old, GatewayLeaseFact("2" * 64, commit, True))).__next__,
                lambda _token: 1,
                lambda: False,
            ),
        }
        for code, (reader, delivery_count, approval) in scenarios.items():
            with self.subTest(code=code):
                result = run_managed_recovery(
                    restart=lambda: None,
                    read_gateway=reader,
                    send_probe=lambda: "probe-token",
                    count_probe_deliveries=delivery_count,
                    approval_is_stable=approval,
                    wait=lambda _seconds: None,
                    attempts=1,
                )
                self.assertEqual(result.status, "failed")
                self.assertEqual(result.error_code, code)

    def test_active_approval_disappearing_after_restart_fails_recovery(self) -> None:
        """restart 前存在的 active Approval 在新 lease 后丢失必须失败。"""
        commit = "a" * 40
        leases = iter(
            (
                GatewayLeaseFact("1" * 64, commit, True),
                GatewayLeaseFact("2" * 64, commit, True),
            )
        )
        approval_states = iter((True, False))
        sent: list[bool] = []
        result = run_managed_recovery(
            restart=lambda: None,
            read_gateway=lambda: next(leases),
            send_probe=lambda: sent.append(True) or "probe-token",
            count_probe_deliveries=lambda _token: 1,
            approval_is_stable=lambda: next(approval_states),
            wait=lambda _seconds: None,
            attempts=1,
        )
        self.assertEqual(result, RecoveryResult("failed", "approval_recovery_failed", 0))
        self.assertEqual(sent, [])


class Phase6ProductionReportTest(unittest.TestCase):
    """验证 aggregate 只有全部真实 gate 通过才发布 VERIFIED。"""

    def test_report_requires_all_gates_and_keeps_os_reboot_truth(self) -> None:
        """24h、25-case、Seatbelt、恢复和 DeepSeek 必须绑定同一 commit。"""
        commit = "a" * 40
        started = datetime(2026, 8, 10, tzinfo=UTC)
        finished = started + timedelta(days=1)
        evidence = (
            GateEvidence("seatbelt", commit, "verified", 2, 2, 0, "1" * 64),
            GateEvidence("feishu_channel", commit, "verified", 15, 15, 0, "2" * 64),
            GateEvidence("feishu_automation", commit, "verified", 10, 10, 0, "3" * 64),
        )
        checkpoint = SoakCheckpoint(
            1,
            commit,
            "4" * 64,
            "5" * 64,
            started.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            finished.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            finished.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            86_400,
            86_400,
            1_440,
            "passed",
            (),
            "passed",
        )
        report = build_production_report(
            commit=commit,
            started_at=checkpoint.started_at,
            finished_at=checkpoint.last_observed_at,
            evidence=evidence,
            recovery=RecoveryResult("passed", None, 1),
            soak=checkpoint,
            deepseek_checks=("normal", "tool", "approval"),
            os_reboot="not_run",
        )
        self.assertEqual(report["release_status"], "PHASE6_MACOS_FEISHU_PRODUCTION_VERIFIED")
        self.assertEqual(report["os_reboot"], "not_run")
        self.assertNotIn("path", json.dumps(report).lower())
        failed = build_production_report(
            commit=commit,
            started_at=checkpoint.started_at,
            finished_at=checkpoint.last_observed_at,
            evidence=(replace(evidence[0], passed=1, status="failed"), *evidence[1:]),
            recovery=RecoveryResult("passed", None, 1),
            soak=checkpoint,
            deepseek_checks=("normal", "tool", "approval"),
            os_reboot="not_run",
        )
        self.assertEqual(failed["release_status"], "PHASE6_MACOS_FEISHU_PRODUCTION_FAILED")


if __name__ == "__main__":
    unittest.main()
