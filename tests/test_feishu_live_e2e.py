"""Feishu Live E2E 的只读取证、进程与报告安全契约。"""

import asyncio
import importlib
import json
import os
import sys
import tempfile
import textwrap
import threading
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, patch

from miniclaw.bootstrap import initialize_state
from miniclaw.doctor import CheckResult, CheckStatus
from miniclaw.gateway_lease import GatewayProvenance
from miniclaw.paths import build_state_paths
from miniclaw.storage.database import Database


class FeishuDatabaseProbeTest(unittest.TestCase):
    """保证 Live 自动证据只看 checkpoint 后的真实 Feishu SQLite 状态。"""

    def setUp(self) -> None:
        """创建真实 schema v2 与最小 Owner。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialize_state(self.paths)
        self.database = Database(self.paths.database)
        self.now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC).isoformat()

    def test_checkpoint_ignores_old_rows_and_detects_new_completed_chain(self) -> None:
        """旧数据或其他 Channel 不能满足本轮 Inbox/Turn/Tool/Delivery/Audit 证据。"""
        api = self._api()
        self._insert_chain(channel="feishu", suffix="old", tool_name="system_info")
        checkpoint = api.capture_checkpoint(self.paths.database)
        self._insert_chain(channel="telegram", suffix="peer", tool_name="system_info")

        before = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            (
                "inbox_completed",
                "turn_completed",
                "delivery_sent",
                "system_info_succeeded",
                "gateway_ready",
                "transport_reconnected",
            ),
        )
        self.assertEqual(before.passed, ())
        self.assertEqual(
            before.failed,
            (
                "inbox_completed",
                "turn_completed",
                "delivery_sent",
                "system_info_succeeded",
                "gateway_ready",
                "transport_reconnected",
            ),
        )

        self._insert_chain(channel="feishu", suffix="new", tool_name="system_info")
        self._insert_audit("channel.supervisor.ready")
        self._insert_audit("channel.transport.reconnecting")
        self._insert_audit("channel.transport.connected")
        after = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            before.failed,
        )

        self.assertEqual(after.passed, before.failed)
        self.assertEqual(after.failed, ())

    def test_multi_turn_approval_and_delivery_evidence_require_complete_shapes(self) -> None:
        """上下文、审批与分片必须满足完整关联，不能用单行或失败状态凑证据。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)
        session_id = self._insert_session("feishu", "context")
        for index in range(3):
            self._insert_turn(session_id, f"om_context_{index}", status="completed")
        approval_turn = self._insert_turn(session_id, "om_approval", status="completed")
        tool_run_id = self._insert_tool_run(
            approval_turn,
            "write_file",
            status="succeeded",
        )
        self._insert_tool_run(approval_turn, "read_file", status="succeeded")
        self._insert_approval(approval_turn, tool_run_id, status="consumed")
        pending_turn = self._insert_turn(session_id, "om_pending", status="waiting_approval")
        pending_tool = self._insert_tool_run(
            pending_turn,
            "write_file",
            status="waiting_approval",
        )
        self._insert_approval(pending_turn, pending_tool, status="pending")
        denied_turn = self._insert_turn(session_id, "om_denied", status="waiting_approval")
        denied_tool = self._insert_tool_run(
            denied_turn,
            "write_file",
            status="denied",
        )
        self._insert_approval(denied_turn, denied_tool, status="denied")
        message_id = self._insert_message(approval_turn, session_id)
        self._insert_delivery(message_id, 0, status="sent")
        self._insert_delivery(message_id, 1, status="sent")
        self._insert_audit("channel.supervisor.ready")
        self._insert_audit("channel.supervisor.ready")

        result = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            (
                "one_session_three_turns",
                "read_file_succeeded",
                "approval_pending",
                "approval_consumed_once",
                "approval_denied",
                "multiple_parts_sent",
                "memory_survived_restart",
            ),
        )

        self.assertEqual(
            result.passed,
            (
                "one_session_three_turns",
                "read_file_succeeded",
                "approval_pending",
                "approval_consumed_once",
                "approval_denied",
                "multiple_parts_sent",
                "memory_survived_restart",
            ),
        )
        self.assertEqual(result.failed, ())

    def test_no_new_turn_is_feishu_scoped_and_flips_after_a_real_turn(self) -> None:
        """静默证据可以忽略 peer Channel，但任何新 Feishu Turn 都必须使其失败。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)
        peer_session = self._insert_session("discord", "peer")
        self._insert_turn(peer_session, "discord-message", status="completed")

        silent = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("no_new_turn",),
        )
        self.assertEqual(silent.passed, ("no_new_turn",))

        session_id = self._insert_session("feishu", "unexpected")
        self._insert_turn(session_id, "om_unexpected", status="completed")
        noisy = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("no_new_turn",),
        )
        self.assertEqual(noisy.failed, ("no_new_turn",))

    def test_delivery_sent_accepts_a_completed_final_card_without_outbox_rows(self) -> None:
        """短回复只更新最终卡片时，completed Inbox/Turn 必须能证明送达完成。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)
        session_id = self._insert_session("feishu", "completed_card")
        external_message_id = "om_completed_card"
        self._insert_turn(session_id, external_message_id, status="completed")
        self._insert_event(
            "feishu",
            "completed_card",
            session_id,
            external_message_id,
        )

        result = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("delivery_sent",),
        )

        self.assertEqual(result.passed, ("delivery_sent",))
        self.assertEqual(result.failed, ())

    def test_delivery_sent_rejects_completed_card_when_outbox_is_not_sent(self) -> None:
        """一旦新建了未发送 Outbox，不能用 completed Inbox/Turn 掩盖发送失败。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)
        session_id = self._insert_session("feishu", "queued_delivery")
        external_message_id = "om_queued_delivery"
        turn_id = self._insert_turn(session_id, external_message_id, status="completed")
        self._insert_event(
            "feishu",
            "queued_delivery",
            session_id,
            external_message_id,
        )
        message_id = self._insert_message(turn_id, session_id)
        self._insert_delivery(message_id, 0, status="queued")

        result = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("delivery_sent",),
        )

        self.assertEqual(result.passed, ())
        self.assertEqual(result.failed, ("delivery_sent",))

    def test_pending_approval_captured_before_action_can_transition_to_consumed(self) -> None:
        """LIVE-007 必须识别上一 case 的 pending 行在本次动作后变成 consumed。"""
        api = self._api()
        session_id = self._insert_session("feishu", "cross_case_approval")
        turn_id = self._insert_turn(session_id, "om_pending_before", status="waiting_approval")
        tool_run_id = self._insert_tool_run(turn_id, "write_file", status="waiting_approval")
        approval_id = self._insert_approval(turn_id, tool_run_id, status="pending")
        checkpoint = api.capture_checkpoint(self.paths.database)

        with self.database.connect() as connection:
            connection.execute(
                "UPDATE approvals SET status = 'consumed', decided_at = ? WHERE id = ?",
                (self.now, approval_id),
            )
            connection.execute(
                "UPDATE tool_runs SET status = 'succeeded', completed_at = ? WHERE id = ?",
                (self.now, tool_run_id),
            )

        result = api.evaluate_local_evidence(
            self.paths.database,
            checkpoint,
            ("approval_consumed_once",),
        )
        self.assertEqual(result.passed, ("approval_consumed_once",))
        self.assertEqual(result.failed, ())

    def test_unknown_key_and_database_failure_return_only_stable_codes(self) -> None:
        """错误不能回显绝对数据库路径、SQL、正文或外部平台标识。"""
        api = self._api()
        checkpoint = api.capture_checkpoint(self.paths.database)

        with self.assertRaises(api.FeishuLiveError) as unknown:
            api.evaluate_local_evidence(
                self.paths.database,
                checkpoint,
                ("not_a_live_fact",),
            )
        self.assertEqual(unknown.exception.code, "unknown_local_evidence")
        self.assertEqual(str(unknown.exception), "unknown_local_evidence")

        missing = Path(self.temporary_directory.name) / "private-owner" / "missing.db"
        with self.assertRaises(api.FeishuLiveError) as unavailable:
            api.capture_checkpoint(missing)
        self.assertEqual(unavailable.exception.code, "evidence_database_unavailable")
        rendered = str(unavailable.exception)
        self.assertNotIn("private-owner", rendered)
        self.assertNotIn("missing.db", rendered)

    def _api(self) -> ModuleType:
        """导入真实生产模块；缺失功能时转成清晰的 RED failure。"""
        try:
            return importlib.import_module("miniclaw.evals.feishu_live")
        except ModuleNotFoundError as error:
            self.fail(f"Feishu Live evidence module is missing: {error.name}")

    def _insert_chain(self, *, channel: str, suffix: str, tool_name: str) -> None:
        """插入一条手工推导的真实 schema 关联链。"""
        session_id = self._insert_session(channel, suffix)
        external_message_id = f"om_{suffix}" if channel == "feishu" else f"msg_{suffix}"
        turn_id = self._insert_turn(session_id, external_message_id, status="completed")
        self._insert_event(channel, suffix, session_id, external_message_id)
        self._insert_tool_run(turn_id, tool_name, status="succeeded")
        message_id = self._insert_message(turn_id, session_id)
        self._insert_delivery(message_id, 0, status="sent", channel=channel)

    def _insert_session(self, channel: str, suffix: str) -> int:
        """插入测试会话并返回内部 ID。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (1, ?, 'default', ?, 'active', ?, ?)
                """,
                (channel, f"conversation_{suffix}", self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_turn(self, session_id: int, inbound: str, *, status: str) -> int:
        """插入指定状态 Turn。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO turns (
                    session_id, inbound_event_id, status, model,
                    started_at, completed_at, runtime_snapshot_json
                ) VALUES (?, ?, ?, 'test-model', ?, ?, '{}')
                """,
                (session_id, inbound, status, self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_event(
        self,
        channel: str,
        suffix: str,
        session_id: int,
        external_message_id: str,
    ) -> None:
        """插入 completed Channel Inbox。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO processed_events (
                    channel, account_id, event_id, external_message_id,
                    session_id, received_at, external_user_id,
                    external_conversation_id, chat_type, message_type, content,
                    reply_to_message_id, status, attempts, updated_at
                ) VALUES (?, 'default', ?, ?, ?, ?, ?, ?, 'p2p', 'text', ?, ?,
                          'completed', 1, ?)
                """,
                (
                    channel,
                    f"event_{suffix}",
                    external_message_id,
                    session_id,
                    self.now,
                    f"user_{suffix}",
                    f"conversation_{suffix}",
                    f"synthetic-content-{suffix}",
                    external_message_id,
                    self.now,
                ),
            )

    def _insert_tool_run(self, turn_id: int, name: str, *, status: str) -> int:
        """插入 ToolRun 并返回内部 ID。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_runs (
                    turn_id, tool_call_id, tool_name, arguments_json,
                    arguments_hash, policy_action, status, created_at, completed_at
                ) VALUES (?, ?, ?, '{}', ?, 'allow', ?, ?, ?)
                """,
                (turn_id, f"call_{turn_id}_{name}", name, "a" * 64, status, self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_approval(self, turn_id: int, tool_run_id: int, *, status: str) -> int:
        """插入与 ToolRun 一对一绑定的审批事实并返回内部 ID。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO approvals (
                    user_id, turn_id, tool_run_id, tool_name, arguments_hash,
                    summary, status, expires_at, decided_at, created_at
                ) VALUES (1, ?, ?, 'write_file', ?, 'safe summary', ?, ?, ?, ?)
                """,
                (turn_id, tool_run_id, "a" * 64, status, self.now, self.now, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_message(self, turn_id: int, session_id: int) -> int:
        """插入内部 Assistant Message。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO messages (session_id, turn_id, role, content, metadata_json, created_at)
                VALUES (?, ?, 'assistant', 'synthetic answer', '{}', ?)
                """,
                (session_id, turn_id, self.now),
            )
            return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

    def _insert_delivery(
        self,
        message_id: int,
        part_index: int,
        *,
        status: str,
        channel: str = "feishu",
    ) -> None:
        """插入真实 v2 Outbox 分片。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO deliveries (
                    message_id, channel, account_id, external_conversation_id,
                    reply_to_message_id, delivery_kind, part_index, content,
                    content_hash, idempotency_key, status, attempts,
                    created_at, updated_at, sent_at
                ) VALUES (?, ?, 'default', 'conversation', 'reply', 'message', ?,
                          'synthetic delivery', ?, ?, ?, 1, ?, ?, ?)
                """,
                (
                    message_id,
                    channel,
                    part_index,
                    f"hash-{message_id}-{part_index}",
                    f"key-{channel}-{message_id}-{part_index}",
                    status,
                    self.now,
                    self.now,
                    self.now,
                ),
            )

    def _insert_audit(self, event_type: str) -> None:
        """插入不含外部 ID 的 Feishu Audit。"""
        with self.database.connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_type, summary, metadata_json, created_at
                ) VALUES (?, ?, '{"channel":"feishu"}', ?)
                """,
                (event_type, event_type.replace(".", " "), self.now),
            )


class ManagedGatewayProcessTest(unittest.IsolatedAsyncioTestCase):
    """保证各平台受管 Gateway 启停有界，且输出永远被持续排空。"""

    def setUp(self) -> None:
        """创建隔离的项目目录与 MiniClaw Home。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.home = self.root / "home"
        self.home.mkdir()
        self.script_index = 0

    async def test_exact_ready_line_and_single_sigterm_exit_cleanly(self) -> None:
        """只有整行 ready marker 才算启动成功，普通 SIGTERM 必须正常退出。"""
        process = await self._start(
            """
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            print("MiniClaw gateway ready: feishu/default", flush=True)
            while True:
                time.sleep(0.05)
            """
        )

        self.assertTrue(process.ready)
        self.assertGreater(process.provenance.pid, 0)
        self.assertEqual(process.provenance.commit, "c" * 40)
        self.assertRegex(
            process.provenance.started_at,
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$",
        )
        self.assertEqual(await process.stop(timeout=1.0), 0)

    async def test_pipe_child_forces_unbuffered_ready_output(self) -> None:
        """无 TTY 且 print 未 flush 时，受管进程仍必须及时交付 ready 行。"""
        api = self._api()
        script = self.root / "buffered_gateway.py"
        script.write_text(
            textwrap.dedent(
                """
                import signal
                import sys
                import time

                signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
                print("MiniClaw gateway ready: feishu/default")
                while True:
                    time.sleep(0.05)
                """
            ),
            encoding="utf-8",
        )

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PYTHONUNBUFFERED", None)
            process = await api.ManagedGateway.start(
                project_root=self.root,
                home=self.home,
                ready_line="MiniClaw gateway ready: feishu/default",
                commit="c" * 40,
                ready_timeout=0.5,
                command=(sys.executable, str(script)),
            )

        self.assertTrue(process.ready)
        self.assertEqual(await process.stop(timeout=1.0), 0)

    async def test_ready_substring_times_out_and_process_is_reaped(self) -> None:
        """日志中碰巧包含 marker 不能误判为 ready，超时后必须回收进程。"""
        api = self._api()
        command = self._command(
            """
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            print("prefix MiniClaw gateway ready: feishu/default suffix", flush=True)
            while True:
                time.sleep(0.05)
            """
        )

        with self.assertRaises(api.ManagedGatewayError) as raised:
            await api.ManagedGateway.start(
                project_root=self.root,
                home=self.home,
                ready_line="MiniClaw gateway ready: feishu/default",
                commit="c" * 40,
                ready_timeout=0.1,
                command=command,
            )

        self.assertEqual(raised.exception.code, "gateway_ready_timeout")

    async def test_exit_before_ready_has_stable_error_code(self) -> None:
        """子进程提前结束只能暴露稳定错误码，不能拼接 stderr。"""
        api = self._api()
        command = self._command(
            """
            import sys

            print("private diagnostic must not escape", file=sys.stderr, flush=True)
            raise SystemExit(17)
            """
        )

        with self.assertRaises(api.ManagedGatewayError) as raised:
            await api.ManagedGateway.start(
                project_root=self.root,
                home=self.home,
                ready_line="MiniClaw gateway ready: feishu/default",
                commit="c" * 40,
                ready_timeout=1.0,
                command=command,
            )

        self.assertEqual(raised.exception.code, "gateway_exited_before_ready")
        self.assertEqual(str(raised.exception), "gateway_exited_before_ready")

    async def test_massive_stderr_is_drained_and_diagnostics_are_bounded(self) -> None:
        """超过 pipe 容量的 stderr 不能死锁，诊断只保留最后 200 条有界行。"""
        process = await self._start(
            """
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            for index in range(300):
                print(f"diagnostic-{index:03d}-" + "x" * 5000, file=sys.stderr)
            sys.stderr.flush()
            print("MiniClaw gateway ready: feishu/default", flush=True)
            while True:
                time.sleep(0.05)
            """,
            ready_timeout=2.0,
        )

        diagnostics = process.bounded_diagnostics
        self.assertEqual(len(diagnostics), 200)
        self.assertTrue(all(len(line) <= 4096 for line in diagnostics))
        self.assertTrue(any(line.startswith("stderr:diagnostic-299-") for line in diagnostics))
        self.assertEqual(await process.stop(timeout=1.0), 0)

    async def test_stop_retries_sigterm_once_without_sigkill(self) -> None:
        """第一次 SIGTERM 被忽略时应再给一次优雅退出机会，绝不自动 SIGKILL。"""
        process = await self._start(
            """
            import signal
            import sys
            import time

            signals = 0

            def stop_after_second(*_):
                global signals
                signals += 1
                if signals >= 2:
                    sys.exit(0)

            signal.signal(signal.SIGTERM, stop_after_second)
            print("MiniClaw gateway ready: feishu/default", flush=True)
            while True:
                time.sleep(0.05)
            """
        )

        self.assertEqual(await process.stop(timeout=0.15), 0)

    async def test_all_output_is_secret_scanned_before_diagnostic_eviction(self) -> None:
        """早期 Secret 即使被 200 行窗口淘汰也必须计数，且不能留在 diagnostics。"""
        api = self._api()
        process = await api.ManagedGateway.start(
            project_root=self.root,
            home=self.home,
            ready_line="MiniClaw gateway ready: feishu/default",
            commit="c" * 40,
            ready_timeout=2.0,
            secret_values=("EARLY_SECRET_SENTINEL",),
            command=self._command(
                """
                import signal
                import sys
                import time

                signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
                print("EARLY_SECRET_SENTINEL", file=sys.stderr, flush=True)
                for index in range(300):
                    print(f"safe-{index}", file=sys.stderr)
                sys.stderr.flush()
                print("MiniClaw gateway ready: feishu/default", flush=True)
                while True:
                    time.sleep(0.05)
                """
            ),
        )

        self.assertEqual(process.secret_match_count, 1)
        self.assertNotIn(
            "EARLY_SECRET_SENTINEL",
            "\n".join(process.bounded_diagnostics),
        )
        self.assertEqual(await process.stop(timeout=1.0), 0)

    async def test_redacted_sdk_placeholders_do_not_count_as_secret_matches(self) -> None:
        """`***` 后接空格、分隔符或行尾时均是安全占位符。"""
        process = await self._start(
            """
            import signal
            import sys
            import time

            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            print(
                "access_key=*** ticket=*** token=***&device_id=*** "
                "Authorization: Bearer ***",
                file=sys.stderr,
                flush=True,
            )
            print("MiniClaw gateway ready: feishu/default", flush=True)
            while True:
                time.sleep(0.05)
            """
        )

        self.assertEqual(process.secret_match_count, 0)
        self.assertEqual(await process.stop(timeout=1.0), 0)

    async def _start(self, source: str, *, ready_timeout: float = 1.0):
        """启动一段隔离 fake Gateway 脚本。"""
        api = self._api()
        return await api.ManagedGateway.start(
            project_root=self.root,
            home=self.home,
            ready_line="MiniClaw gateway ready: feishu/default",
            commit="c" * 40,
            ready_timeout=ready_timeout,
            command=self._command(source),
        )

    def _command(self, source: str) -> tuple[str, ...]:
        """把测试脚本写入临时目录并返回无缓冲 Python 命令。"""
        self.script_index += 1
        script = self.root / f"fake_gateway_{self.script_index}.py"
        script.write_text(textwrap.dedent(source), encoding="utf-8")
        return (sys.executable, "-u", str(script))

    def _api(self) -> ModuleType:
        """导入真实生产模块，并明确要求 ManagedGateway 存在。"""
        api = importlib.import_module("miniclaw.evals.gateway_process")
        if not hasattr(api, "ManagedGateway"):
            self.fail("ManagedGateway is missing")
        return api


class FeishuEvidenceReportTest(unittest.TestCase):
    """保证 Live Evidence 只有封闭事实，不保存 Secret、正文或平台 ID。"""

    def setUp(self) -> None:
        """创建隔离输出目录与稳定时间/commit。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.commit = "a" * 40
        self.started_at = "2026-08-08T12:00:00Z"
        self.finished_at = "2026-08-08T12:15:00Z"
        self.provenance = GatewayProvenance(
            pid=123,
            started_at="2026-08-08T12:00:01.000000Z",
            commit=self.commit,
        )

    def test_verified_report_has_exact_nested_schema_and_derived_counts(self) -> None:
        """15/15、Gateway 正常和零泄露时才能生成严格 VERIFIED 报告。"""
        api = self._api()
        results = tuple(self._passing_result(api, index) for index in range(1, 16))

        report = api.build_evidence_report(
            commit=self.commit,
            started_at=self.started_at,
            finished_at=self.finished_at,
            gateway_ready=True,
            gateway_graceful_exit=True,
            gateway_provenance=self.provenance,
            results=results,
            secret_matches=0,
        )

        self.assertEqual(
            set(report),
            {
                "schema_version",
                "channel",
                "commit",
                "started_at",
                "finished_at",
                "gateway",
                "checks",
                "counts",
                "release_status",
            },
        )
        self.assertEqual(
            set(report["gateway"]),
            {"ready", "graceful_exit", "pid", "started_at", "commit"},
        )
        self.assertEqual(report["gateway"]["pid"], 123)
        self.assertEqual(report["gateway"]["commit"], self.commit)
        self.assertEqual(
            set(report["checks"][0]),
            {"case_id", "status", "local_evidence", "human_evidence", "error_code"},
        )
        self.assertEqual(
            set(report["checks"][0]["local_evidence"][0]),
            {"key", "status"},
        )
        self.assertEqual(
            set(report["counts"]),
            {
                "cases_total",
                "cases_passed",
                "cases_failed",
                "cases_skipped",
                "local_evidence_passed",
                "local_evidence_failed",
                "human_evidence_passed",
                "human_evidence_failed",
                "human_evidence_skipped",
                "secret_matches",
            },
        )
        self.assertEqual(report["counts"]["cases_passed"], 15)
        self.assertEqual(report["release_status"], "FEISHU_E2E_VERIFIED")
        json.dumps(report, allow_nan=False)

    def test_secret_match_forces_case_fifteen_and_release_to_fail(self) -> None:
        """Secret scan 只要命中一次，015 和整个发布都不能维持 PASS。"""
        api = self._api()
        results = tuple(self._passing_result(api, index) for index in range(1, 16))

        report = api.build_evidence_report(
            commit=self.commit,
            started_at=self.started_at,
            finished_at=self.finished_at,
            gateway_ready=True,
            gateway_graceful_exit=True,
            gateway_provenance=self.provenance,
            results=results,
            secret_matches=1,
        )

        case_fifteen = report["checks"][14]
        self.assertEqual(case_fifteen["case_id"], "FEISHU-LIVE-015")
        self.assertEqual(case_fifteen["status"], "fail")
        self.assertIn(
            {"key": "secret_scan_zero", "status": "fail"},
            case_fifteen["local_evidence"],
        )
        self.assertEqual(case_fifteen["error_code"], "secret_scan_match")
        self.assertEqual(report["release_status"], "FEISHU_LIVE_FAILED")

    def test_builder_and_serializer_reject_raw_or_unknown_fields(self) -> None:
        """外部 ID、路径和额外字段不能借 error/evidence/report 进入 JSON。"""
        api = self._api()
        forbidden = (
            "MODEL_SECRET_SENTINEL",
            "CHANNEL_SECRET_SENTINEL",
            "ou_private",
            "oc_private",
            "om_private",
            "full message body",
            str(self.root),
        )
        for value in forbidden:
            with self.subTest(value=value):
                result = api.FeishuCaseResult(
                    case_id="FEISHU-LIVE-001",
                    status="fail",
                    local_passed=(),
                    local_failed=("gateway_ready",),
                    human_statuses=(),
                    error_code=value,
                )
                with self.assertRaises(api.FeishuLiveError) as raised:
                    self._report(api, (result,))
                self.assertEqual(raised.exception.code, "invalid_evidence_report")

        unknown = api.FeishuCaseResult(
            case_id="FEISHU-LIVE-001",
            status="fail",
            local_passed=(),
            local_failed=("raw_private_fact",),
            human_statuses=(),
            error_code="evidence_failed",
        )
        with self.assertRaises(api.FeishuLiveError):
            self._report(api, (unknown,))

        safe = self._report(api, (self._passing_result(api, 1),))
        unsafe = dict(safe)
        unsafe["raw"] = "MODEL_SECRET_SENTINEL"
        with self.assertRaises(api.FeishuLiveError) as raised:
            api.write_evidence(self.root / "unsafe.json", unsafe)
        self.assertEqual(raised.exception.code, "invalid_evidence_report")
        self.assertFalse((self.root / "unsafe.json").exists())

        tampered_counts = json.loads(json.dumps(safe))
        tampered_counts["counts"]["cases_passed"] = 999
        with self.assertRaises(api.FeishuLiveError):
            api.write_evidence(self.root / "tampered-counts.json", tampered_counts)

        tampered_check = json.loads(json.dumps(safe))
        tampered_check["checks"][0]["local_evidence"][0]["key"] = "raw_private_fact"
        with self.assertRaises(api.FeishuLiveError):
            api.write_evidence(self.root / "tampered-check.json", tampered_check)

        with self.assertRaises(api.FeishuLiveError) as mismatch:
            api.build_evidence_report(
                commit=self.commit,
                started_at=self.started_at,
                finished_at=self.finished_at,
                gateway_ready=True,
                gateway_graceful_exit=True,
                gateway_provenance=GatewayProvenance(
                    pid=123,
                    started_at="2026-08-08T12:00:01.000000Z",
                    commit="f" * 40,
                ),
                results=(self._passing_result(api, 1),),
                secret_matches=0,
            )
        self.assertEqual(mismatch.exception.code, "invalid_evidence_report")

    def test_write_evidence_is_exclusive_owner_only_and_redacted(self) -> None:
        """Evidence 使用 0600 原子新建，不能覆盖已有文件或跟随 symlink。"""
        api = self._api()
        report = self._report(api, (self._passing_result(api, 1),))
        target = self.root / "evidence.json"

        api.write_evidence(target, report)

        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        rendered = target.read_text(encoding="utf-8")
        for forbidden in (
            "MODEL_SECRET_SENTINEL",
            "CHANNEL_SECRET_SENTINEL",
            "ou_",
            "oc_",
            "om_",
            str(self.root),
        ):
            self.assertNotIn(forbidden, rendered)
        with self.assertRaises(api.FeishuLiveError) as existing:
            api.write_evidence(target, report)
        self.assertEqual(existing.exception.code, "evidence_already_exists")

        if hasattr(os, "symlink"):
            link = self.root / "linked.json"
            link.symlink_to(target)
            with self.assertRaises(api.FeishuLiveError):
                api.write_evidence(link, report)

    def test_secret_scan_is_bounded_and_returns_only_match_count(self) -> None:
        """扫描跳过 symlink、大文件和第 1001 个文件，只返回匿名命中数。"""
        api = self._api()
        scan_root = self.root / "scan"
        scan_root.mkdir()
        secrets = (
            "MODEL_SECRET_SENTINEL",
            "CHANNEL_SECRET_SENTINEL",
            "ou_private",
            "oc_private",
            "full message body",
            str(self.root),
        )
        (scan_root / "matches.txt").write_text("\n".join(secrets), encoding="utf-8")
        external = self.root / "external.txt"
        external.write_text("MODEL_SECRET_SENTINEL", encoding="utf-8")
        (scan_root / "linked.txt").symlink_to(external)
        (scan_root / "large.txt").write_bytes(
            b"x" * (1024 * 1024 + 1) + b"CHANNEL_SECRET_SENTINEL"
        )

        matches = api.scan_secret_matches((scan_root,), secrets)

        self.assertEqual(matches, len(secrets))
        self.assertIsInstance(matches, int)

        limited = self.root / "limited"
        limited.mkdir()
        for index in range(1001):
            content = "MODEL_SECRET_SENTINEL" if index == 1000 else "safe"
            (limited / f"{index:04d}.txt").write_text(content, encoding="utf-8")
        self.assertEqual(
            api.scan_secret_matches((limited,), ("MODEL_SECRET_SENTINEL",)),
            0,
        )

    def _passing_result(self, api: ModuleType, index: int):
        """构造一个只含 allowlist evidence 的通过结果。"""
        local = "secret_scan_zero" if index == 15 else "gateway_ready"
        return api.FeishuCaseResult(
            case_id=f"FEISHU-LIVE-{index:03d}",
            status="pass",
            local_passed=(local,),
            local_failed=(),
            human_statuses=(("reply_visible", "pass"),),
            error_code=None,
        )

    def _report(self, api: ModuleType, results: tuple[object, ...]):
        """使用稳定元数据调用真实报告 builder。"""
        return api.build_evidence_report(
            commit=self.commit,
            started_at=self.started_at,
            finished_at=self.finished_at,
            gateway_ready=True,
            gateway_graceful_exit=True,
            gateway_provenance=self.provenance,
            results=results,
            secret_matches=0,
        )

    def _api(self) -> ModuleType:
        """导入真实生产模块，并要求报告接口已存在。"""
        api = importlib.import_module("miniclaw.evals.feishu_live")
        required = (
            "FeishuCaseResult",
            "build_evidence_report",
            "write_evidence",
            "scan_secret_matches",
        )
        missing = tuple(name for name in required if not hasattr(api, name))
        if missing:
            self.fail(f"Feishu evidence API is missing: {missing}")
        return api


class FeishuLiveHarnessSafetyTest(unittest.TestCase):
    """保证 Runner 先显式确认、再做静态 preflight，失败时零副作用。"""

    def test_missing_confirmation_reads_nothing_and_creates_nothing(self) -> None:
        """未给 confirm 时不能解析状态、加载 case、启动 Gateway 或建输出目录。"""
        api = self._api("run_feishu_live_harness")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "must-not-exist-home"
            scenarios = root / "must-not-exist-scenarios"
            output = root / "must-not-exist-output"
            with patch.object(api, "_load_preflight") as preflight:
                code = api.run_feishu_live_harness(
                    [
                        "--home",
                        str(home),
                        "--root",
                        str(scenarios),
                        "--output-dir",
                        str(output),
                    ]
                )

            self.assertEqual(code, 2)
            preflight.assert_not_called()
            self.assertFalse(home.exists())
            self.assertFalse(scenarios.exists())
            self.assertFalse(output.exists())

    def test_static_preflight_rejects_each_isolation_or_truth_failure(self) -> None:
        """开关、权限模式、隔离、commit、Doctor、审批和 case 数均失败关闭。"""
        api = self._api("_validate_preflight_state")
        passing_config = self._config(feishu=True)
        passing_checks = (CheckResult("config", CheckStatus.PASS, "ok"),)
        passing_cases = tuple(object() for _ in range(15))
        scenarios = (
            (self._config(feishu=False), passing_checks, 0, "a" * 40, False, passing_cases,
             "feishu_channel_disabled"),
            (self._config(feishu=True, telegram=True), passing_checks, 0, "a" * 40, False,
             passing_cases, "peer_channel_enabled"),
            (self._config(feishu=True, mode="autopilot"), passing_checks, 0, "a" * 40,
             False, passing_cases, "unsafe_permission_mode"),
            (passing_config, passing_checks, 0, "unknown", False, passing_cases,
             "repository_commit_unavailable"),
            (passing_config, passing_checks, 0, "a" * 40, True, passing_cases,
             "repository_dirty"),
            (passing_config, (CheckResult("database", CheckStatus.FAIL, "private"),), 0,
             "a" * 40, False, passing_cases, "doctor_preflight_failed"),
            (passing_config, passing_checks, 1, "a" * 40, False, passing_cases,
             "pending_approval_exists"),
            (passing_config, passing_checks, 0, "a" * 40, False, passing_cases[:-1],
             "live_case_count_invalid"),
        )
        for config, checks, pending, commit, dirty, cases, expected in scenarios:
            with self.subTest(expected=expected), self.assertRaises(api.FeishuLiveError) as raised:
                api._validate_preflight_state(
                    config=config,
                    checks=checks,
                    pending_approvals=pending,
                    commit=commit,
                    dirty=dirty,
                    cases=cases,
                )
            self.assertEqual(raised.exception.code, expected)

    def test_confirmed_preflight_error_does_not_create_evidence(self) -> None:
        """静态失败发生在 Gateway 和 mkdir 之前，只向终端输出稳定错误码。"""
        api = self._api("run_feishu_live_harness")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "evidence"
            with (
                patch.object(
                    api,
                    "_load_preflight",
                    side_effect=api.FeishuLiveError("feishu_channel_disabled"),
                ),
                patch("sys.stderr") as stderr,
            ):
                code = api.run_feishu_live_harness(
                    ["--confirm-live", "--output-dir", str(output)]
                )
        self.assertEqual(code, 2)
        self.assertFalse(output.exists())
        rendered = "".join(str(call.args[0]) for call in stderr.write.call_args_list)
        self.assertIn("feishu_channel_disabled", rendered)

    @staticmethod
    def _config(
        *,
        feishu: bool,
        telegram: bool = False,
        discord: bool = False,
        mode: str = "safe",
    ):
        """构造仅含 Channel 开关与 Tool mode 的静态配置。"""
        return SimpleNamespace(
            channels=SimpleNamespace(
                feishu=SimpleNamespace(enabled=feishu),
                telegram=SimpleNamespace(enabled=telegram),
                discord=SimpleNamespace(enabled=discord),
            ),
            tools=SimpleNamespace(mode=mode),
        )

    def _api(self, required: str) -> ModuleType:
        """导入模块并把缺失入口转换成清晰 RED。"""
        api = importlib.import_module("miniclaw.evals.feishu_live")
        if not hasattr(api, required):
            self.fail(f"Feishu Live harness API is missing: {required}")
        return api


class FeishuCaseOrchestrationTest(unittest.IsolatedAsyncioTestCase):
    """保证每个场景先 checkpoint、自动证据不可人工覆盖，且总能停止 Gateway。"""

    def setUp(self) -> None:
        """创建隔离 Workspace 与固定 checkpoint。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.database = self.root / "miniclaw.db"

    async def test_checkpoint_precedes_action_and_local_failure_skips_human_override(self) -> None:
        """自动失败后不再询问 human pass，避免人工把失败强制改成通过。"""
        api = self._api("_run_case")
        events: list[str] = []
        case = self._case(
            local=("turn_completed",),
            human=("reply_visible",),
        )
        checkpoint = api.DatabaseCheckpoint(0, 0, 0, 0, 0, 0)

        def capture(_: Path):
            events.append("checkpoint")
            return checkpoint

        def answer(_: str) -> str:
            events.append("action")
            return ""

        with (
            patch.object(api, "capture_checkpoint", side_effect=capture),
            patch.object(
                api,
                "_wait_for_local_evidence",
                new=AsyncMock(
                    side_effect=lambda **_: (
                        events.append("evaluate")
                        or api.EvidenceEvaluation((), ("turn_completed",))
                    )
                ),
            ),
        ):
            result = await api._run_case(
                case=case,
                database=self.database,
                workspace=self.workspace,
                gateway=SimpleNamespace(ready=True),
                case_timeout=5.0,
                input_fn=answer,
                output_fn=lambda _: None,
            )

        self.assertEqual(events, ["checkpoint", "action", "evaluate"])
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.local_failed, ("turn_completed",))
        self.assertEqual(result.human_statuses, ())

    async def test_action_prompt_does_not_block_gateway_event_loop(self) -> None:
        """人工输入等待期间事件循环仍须排空 Gateway pipe 与处理 timeout。"""
        api = self._api("_run_case")
        release = threading.Event()
        timed_out: list[bool] = []
        asyncio.get_running_loop().call_later(0.02, release.set)

        def answer(_: str) -> str:
            timed_out.append(not release.wait(0.2))
            return ""

        case = self._case(local=("gateway_ready",), human=())
        checkpoint = api.DatabaseCheckpoint(0, 0, 0, 0, 0, 0)
        with patch.object(api, "capture_checkpoint", return_value=checkpoint):
            result = await api._run_case(
                case=case,
                database=self.database,
                workspace=self.workspace,
                gateway=SimpleNamespace(ready=True),
                case_timeout=5.0,
                input_fn=answer,
                output_fn=lambda _: None,
            )

        self.assertEqual(timed_out, [False])
        self.assertEqual(result.status, "pass")

    async def test_local_and_human_pass_produce_pass_while_skip_stays_nonzero(self) -> None:
        """自动证据通过后才收集 p/f/s，skip 永远保留为 skip。"""
        api = self._api("_run_case")
        case = self._case(
            local=("delivery_sent",),
            human=("reply_visible",),
        )
        checkpoint = api.DatabaseCheckpoint(0, 0, 0, 0, 0, 0)
        with (
            patch.object(api, "capture_checkpoint", return_value=checkpoint),
            patch.object(
                api,
                "_wait_for_local_evidence",
                new=AsyncMock(
                    return_value=api.EvidenceEvaluation(("delivery_sent",), ())
                ),
            ),
        ):
            answers = iter(["", "p"])
            passed = await api._run_case(
                case=case,
                database=self.database,
                workspace=self.workspace,
                gateway=SimpleNamespace(ready=True),
                case_timeout=5.0,
                input_fn=lambda _: next(answers),
                output_fn=lambda _: None,
            )
            skipped = await api._run_case(
                case=case,
                database=self.database,
                workspace=self.workspace,
                gateway=SimpleNamespace(ready=True),
                case_timeout=5.0,
                input_fn=lambda _: "s",
                output_fn=lambda _: None,
            )

        self.assertEqual(passed.status, "pass")
        self.assertEqual(passed.human_statuses, (("reply_visible", "pass"),))
        self.assertEqual(skipped.status, "skip")

    async def test_gateway_is_stopped_when_case_execution_raises(self) -> None:
        """任一 case 异常都必须穿过 finally 关闭同一个 Gateway。"""
        api = self._api("_execute_live_cases")
        provenance = GatewayProvenance(
            pid=321,
            started_at="2026-08-08T12:00:01.000000Z",
            commit="b" * 40,
        )
        gateway = SimpleNamespace(
            ready=True,
            provenance=provenance,
            secret_match_count=0,
            stop=AsyncMock(return_value=0),
        )
        preflight = SimpleNamespace(
            project_root=self.root,
            commit="b" * 40,
            paths=SimpleNamespace(home=self.root / "home", database=self.database),
            config=SimpleNamespace(
                workspace=SimpleNamespace(path=self.workspace),
                channels=SimpleNamespace(
                    feishu=SimpleNamespace(account_id="default")
                ),
            ),
            cases=(self._case(local=("turn_completed",), human=()),),
        )
        with (
            patch.object(
                api,
                "_start_managed_gateway",
                new=AsyncMock(return_value=gateway),
            ),
            patch.object(
                api,
                "_run_case",
                new=AsyncMock(side_effect=api.FeishuLiveError("case_execution_failed")),
            ),
        ):
            with self.assertRaises(api.FeishuLiveError):
                await api._execute_live_cases(
                    preflight,
                    gateway_timeout=5.0,
                    case_timeout=5.0,
                    input_fn=lambda _: "",
                    output_fn=lambda _: None,
                )
        gateway.stop.assert_awaited_once()

    async def test_restart_aggregates_secret_matches_from_every_gateway(self) -> None:
        """重启前后的完整输出都必须进入 Secret 计数，不能只保留末次进程。"""
        api = self._api("_execute_live_cases")
        first = SimpleNamespace(
            ready=True,
            provenance=GatewayProvenance(
                pid=321,
                started_at="2026-08-08T12:00:01.000000Z",
                commit="b" * 40,
            ),
            secret_match_count=1,
            stop=AsyncMock(return_value=0),
        )
        second = SimpleNamespace(
            ready=True,
            provenance=GatewayProvenance(
                pid=654,
                started_at="2026-08-08T12:00:02.000000Z",
                commit="b" * 40,
            ),
            secret_match_count=2,
            stop=AsyncMock(return_value=0),
        )
        preflight = SimpleNamespace(
            project_root=self.root,
            commit="b" * 40,
            paths=SimpleNamespace(home=self.root / "home", database=self.database),
            config=SimpleNamespace(
                workspace=SimpleNamespace(path=self.workspace),
                channels=SimpleNamespace(
                    feishu=SimpleNamespace(account_id="default")
                ),
            ),
            cases=(self._case(local=("turn_completed",), human=(), case_id="FEISHU-LIVE-013"),),
        )

        async def run_restart_case(*, restart_fn, **_):
            await restart_fn()
            return api.FeishuCaseResult(
                "FEISHU-LIVE-013",
                "pass",
                ("turn_completed",),
                (),
                (),
                None,
            )

        with (
            patch.object(
                api,
                "_start_managed_gateway",
                new=AsyncMock(side_effect=(first, second)),
            ),
            patch.object(api, "_run_case", new=AsyncMock(side_effect=run_restart_case)),
        ):
            execution = await api._execute_live_cases(
                preflight,
                gateway_timeout=5.0,
                case_timeout=5.0,
                input_fn=lambda _: "",
                output_fn=lambda _: None,
            )

        self.assertEqual(execution.gateway_secret_matches, 3)
        self.assertEqual(execution.gateway_provenance, second.provenance)
        first.stop.assert_awaited_once()
        second.stop.assert_awaited_once()

    def _case(
        self,
        *,
        local: tuple[str, ...],
        human: tuple[str, ...],
        case_id: str = "FEISHU-LIVE-002",
    ):
        """构造内部 orchestration 所需的最小严格 case。"""
        return SimpleNamespace(
            id=case_id,
            title="synthetic live case",
            query="perform synthetic action",
            turns=(),
            setup_files=(),
            expected=SimpleNamespace(
                live_local_evidence=local,
                live_human_evidence=human,
            ),
        )

    def _api(self, required: str) -> ModuleType:
        """导入模块并把缺失编排接口转换成清晰 RED。"""
        api = importlib.import_module("miniclaw.evals.feishu_live")
        if not hasattr(api, required):
            self.fail(f"Feishu case orchestration API is missing: {required}")
        return api


class FeishuLiveHarnessIntegrationTest(unittest.TestCase):
    """保证 15/15 才返回 0，且运行中 repository 变化会降级 Evidence。"""

    def test_verified_run_and_repository_change_have_truthful_exit_codes(self) -> None:
        """同一组自动结果只有在 HEAD/dirty 不变时才能标为 VERIFIED。"""
        api = self._api()
        results = tuple(
            api.FeishuCaseResult(
                case_id=f"FEISHU-LIVE-{index:03d}",
                status="pass",
                local_passed=("secret_scan_zero" if index == 15 else "gateway_ready",),
                local_failed=(),
                human_statuses=(),
                error_code=None,
            )
            for index in range(1, 16)
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            logs = root / "logs"
            logs.mkdir()
            preflight = SimpleNamespace(
                project_root=root,
                commit="b" * 40,
                paths=SimpleNamespace(
                    home=root / "home",
                    database=root / "miniclaw.db",
                    logs=logs,
                ),
                config=SimpleNamespace(
                    workspace=SimpleNamespace(path=root / "workspace"),
                    channels=SimpleNamespace(
                        feishu=SimpleNamespace(
                            account_id="default",
                            owner_open_id="ou_synthetic",
                            allowed_open_ids=("ou_synthetic",),
                            allowed_chat_ids=("oc_synthetic",),
                        )
                    ),
                ),
                secrets=SimpleNamespace(
                    model_api_key="MODEL_SECRET_SENTINEL",
                    channel_tokens={"feishu": "CHANNEL_SECRET_SENTINEL"},
                    feishu_app_id="cli_synthetic",
                ),
                cases=tuple(SimpleNamespace(query="synthetic", turns=()) for _ in range(15)),
            )
            execution = api.LiveExecution(
                results=results,
                gateway_ready=True,
                gateway_graceful_exit=True,
                gateway_provenance=GatewayProvenance(
                    pid=456,
                    started_at="2026-08-08T12:00:01.000000Z",
                    commit="b" * 40,
                ),
                gateway_secret_matches=0,
            )
            for unchanged, expected_code, expected_release in (
                (True, 0, "FEISHU_E2E_VERIFIED"),
                (False, 1, "FEISHU_LIVE_FAILED"),
            ):
                output = root / f"evidence-{unchanged}"
                with (
                    self.subTest(unchanged=unchanged),
                    patch.object(api, "_load_preflight", return_value=preflight),
                    patch.object(
                        api,
                        "_execute_live_cases",
                        new=AsyncMock(return_value=execution),
                    ),
                    patch.object(api, "scan_secret_matches", return_value=0),
                    patch.object(api, "_repository_unchanged", return_value=unchanged),
                ):
                    code = api.run_feishu_live_harness(
                        ["--confirm-live", "--output-dir", str(output)]
                    )
                files = list(output.glob("*.json"))
                self.assertEqual(code, expected_code)
                self.assertEqual(len(files), 1)
                report = json.loads(files[0].read_text(encoding="utf-8"))
                self.assertEqual(report["release_status"], expected_release)
                if not unchanged:
                    self.assertEqual(report["checks"][14]["error_code"], "repository_changed")

    def _api(self) -> ModuleType:
        """导入模块并要求集成编排数据结构存在。"""
        api = importlib.import_module("miniclaw.evals.feishu_live")
        for required in ("LiveExecution", "run_feishu_live_harness"):
            if not hasattr(api, required):
                self.fail(f"Feishu Live integration API is missing: {required}")
        return api


if __name__ == "__main__":
    unittest.main()
