"""参数绑定 Approval 的 SQLite 生命周期与并发消费测试。"""

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from lobster0.bootstrap import initialize_state
from lobster0.paths import build_state_paths
from lobster0.policy.approvals import (
    ApprovalDecision,
    ApprovalError,
    canonical_arguments_hash,
)
from lobster0.policy.engine import PolicyAction, PolicyDecision
from lobster0.policy.network import NetworkRule
from lobster0.providers.base import JsonValue, ToolCall
from lobster0.storage.conversations import SessionRepository, TurnRepository
from lobster0.storage.database import Database
from lobster0.storage.tooling import (
    ApprovalRepository,
    PolicyRuleRepository,
    ToolRunRepository,
)
from lobster0.tools.base import ToolContext


class ApprovalRepositoryTest(unittest.TestCase):
    """验证 Approval 跨进程可恢复、绑定参数并且只消费一次。"""

    def setUp(self) -> None:
        """创建带真实 Owner、Session 和 running Turn 的隔离数据库。"""
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.paths = build_state_paths(Path(self.temporary_directory.name).resolve())
        initialized = initialize_state(self.paths)
        self.owner_id = initialized.owner.id
        self.database = Database(self.paths.database)
        self.session = SessionRepository(self.database).get_or_create_cli(
            self.owner_id,
            "approval-test",
        )
        turns = TurnRepository(self.database)
        self.turn = turns.create_with_user_message(
            self.session.id,
            "approval-event",
            "test-model",
            "write a note",
        )
        turns.mark_running(self.turn.id)
        self.context = ToolContext(
            user_id=self.owner_id,
            session_id=self.session.id,
            turn_id=self.turn.id,
            state_home=self.paths.home,
            workspace=self.paths.workspace,
            read_only_roots=(),
        )
        self.now = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)
        self.repository = ApprovalRepository(self.database, clock=lambda: self.now)

    def create(
        self,
        *,
        call_id: str = "write-1",
        arguments: dict[str, JsonValue] | None = None,
        ttl_seconds: int = 600,
    ) -> int:
        """创建一条默认 write_file waiting Approval 并返回 ID。"""
        approval = self.repository.create_waiting(
            self.context,
            ToolCall(
                call_id,
                "write_file",
                arguments
                or {
                    "path": str(self.paths.workspace / "note.txt"),
                    "content": "private-content",
                    "overwrite": False,
                },
            ),
            arguments
            or {
                "path": str(self.paths.workspace / "note.txt"),
                "content": "private-content",
                "overwrite": False,
            },
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=ttl_seconds,
            summary="write_file note.txt",
        )
        return approval.id

    def test_canonical_hash_is_order_independent_tool_bound_and_standard_json(self) -> None:
        """键顺序不能改变 hash，Tool 名或参数变化必须改变，NaN 必须拒绝。"""
        left = canonical_arguments_hash("write_file", {"path": "x", "content": "a"})
        right = canonical_arguments_hash("write_file", {"content": "a", "path": "x"})

        self.assertEqual(left, right)
        self.assertNotEqual(
            left,
            canonical_arguments_hash("edit_file", {"path": "x", "content": "a"}),
        )
        self.assertNotEqual(
            left,
            canonical_arguments_hash("write_file", {"path": "x", "content": "b"}),
        )
        with self.assertRaises(ValueError):
            canonical_arguments_hash("write_file", {"value": float("nan")})

    def test_create_waiting_persists_bound_run_approval_and_redacted_audit(self) -> None:
        """创建操作必须原子保存 waiting ToolRun、pending Approval 和脱敏审计。"""
        approval_id = self.create()

        with self.database.connect_read_only() as connection:
            approval = connection.execute("SELECT * FROM approvals").fetchone()
            run = connection.execute("SELECT * FROM tool_runs").fetchone()
            audit = connection.execute("SELECT * FROM audit_events").fetchone()
        self.assertEqual(approval["id"], approval_id)
        self.assertEqual(approval["status"], "pending")
        self.assertEqual(run["status"], "waiting_approval")
        self.assertEqual(run["policy_action"], "require_approval")
        self.assertEqual(approval["arguments_hash"], run["arguments_hash"])
        self.assertEqual(audit["event_type"], "approval.created")
        self.assertNotIn("private-content", audit["summary"] + audit["metadata_json"])
        self.assertEqual(
            json.loads(run["arguments_json"])["path"],
            str(self.paths.workspace / "note.txt"),
        )

    def test_pending_survives_repository_restart_and_lists_only_owner_records(self) -> None:
        """新 Repository 实例必须从 SQLite 恢复 pending，不依赖内存单例。"""
        approval_id = self.create()

        restarted = ApprovalRepository(self.database, clock=lambda: self.now)
        stored = restarted.get(self.owner_id, approval_id)

        self.assertEqual(stored.status, "pending")
        self.assertEqual([item.id for item in restarted.list(self.owner_id)], [approval_id])
        self.assertEqual(restarted.list(self.owner_id + 1), ())

    def test_non_owner_and_expired_approval_cannot_be_approved(self) -> None:
        """Owner 不匹配和 TTL 到期都必须失败，过期 ToolRun 同时终止。"""
        approval_id = self.create(ttl_seconds=10)

        with self.assertRaises(ApprovalError) as wrong_owner:
            self.repository.approve(self.owner_id + 1, approval_id)
        self.assertEqual(wrong_owner.exception.code, "not_owner")

        self.now += timedelta(seconds=11)
        with self.assertRaises(ApprovalError) as expired:
            self.repository.approve(self.owner_id, approval_id)
        self.assertEqual(expired.exception.code, "expired")

        with self.database.connect_read_only() as connection:
            approval_status = connection.execute(
                "SELECT status FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()[0]
            run_status = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
            events = [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM audit_events ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual((approval_status, run_status), ("expired", "denied"))
        self.assertEqual(events, ["approval.created", "approval.expired"])

    def test_disallowed_scope_is_rejected_before_approval_state_changes(self) -> None:
        """write_file 的 Always 必须在批准和执行前失败关闭。"""
        approval_id = self.create()

        with self.assertRaises(ApprovalError) as rejected:
            self.repository.validate_decision(
                self.owner_id,
                approval_id,
                ApprovalDecision.ALWAYS,
            )

        self.assertEqual(rejected.exception.code, "scope_forbidden")
        self.assertEqual(
            self.repository.get(self.owner_id, approval_id).status,
            "pending",
        )

    def test_presentation_exposes_only_core_modes_and_safe_summary(self) -> None:
        """Channel 展示只能读取 Core 计算的模式，不能读取完整参数。"""
        approval_id = self.create()

        presentation = self.repository.presentation(self.owner_id, approval_id)

        self.assertEqual(presentation.approval.id, approval_id)
        self.assertEqual(presentation.approval.summary, "write_file note.txt")
        self.assertEqual(
            presentation.grant_modes,
            (ApprovalDecision.ONCE,),
        )
        self.assertFalse(hasattr(presentation, "arguments"))

    def test_list_and_get_lazily_expire_without_executing_waiting_tool(self) -> None:
        """只读查询会结算过期状态，但绝不能消费或执行 ToolRun。"""
        approval_id = self.create(ttl_seconds=10)
        self.now += timedelta(seconds=11)

        stored = self.repository.get(self.owner_id, approval_id)

        self.assertEqual(stored.status, "expired")
        self.assertEqual(self.repository.list(self.owner_id, status="pending"), ())
        self.assertEqual(
            [item.id for item in self.repository.list(self.owner_id, status="expired")],
            [approval_id],
        )
        with self.database.connect_read_only() as connection:
            run = connection.execute("SELECT status FROM tool_runs").fetchone()[0]
        self.assertEqual(run, "denied")

    def test_changed_stored_arguments_fail_hash_check_without_consuming(self) -> None:
        """批准后任何参数 JSON 变化都不能消费原签名。"""
        approval_id = self.create()
        self.repository.approve(self.owner_id, approval_id)
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tool_runs SET arguments_json = ? WHERE tool_call_id = 'write-1'",
                ('{"content":"changed","overwrite":false,"path":"note.txt"}',),
            )

        with self.assertRaises(ApprovalError) as changed:
            self.repository.consume(self.owner_id, approval_id)

        self.assertEqual(changed.exception.code, "hash_mismatch")
        with self.database.connect_read_only() as connection:
            statuses = connection.execute(
                "SELECT a.status, tr.status FROM approvals a "
                "JOIN tool_runs tr ON tr.id = a.tool_run_id WHERE a.id = ?",
                (approval_id,),
            ).fetchone()
        self.assertEqual(tuple(statuses), ("approved", "waiting_approval"))

    def test_two_concurrent_consumers_have_exactly_one_winner(self) -> None:
        """两个并发批准进程只能让一个 claim 获得 running ToolRun。"""
        approval_id = self.create()
        self.repository.approve(self.owner_id, approval_id)

        def consume(_: int) -> str:
            try:
                ApprovalRepository(self.database, clock=lambda: self.now).consume(
                    self.owner_id,
                    approval_id,
                )
            except ApprovalError as error:
                return error.code
            return "consumed"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(consume, range(2)))

        self.assertEqual(outcomes.count("consumed"), 1)
        self.assertEqual(outcomes.count("already_decided"), 1)
        with self.database.connect_read_only() as connection:
            statuses = connection.execute(
                "SELECT a.status, tr.status FROM approvals a "
                "JOIN tool_runs tr ON tr.id = a.tool_run_id WHERE a.id = ?",
                (approval_id,),
            ).fetchone()
            events = [
                row[0]
                for row in connection.execute(
                    "SELECT event_type FROM audit_events ORDER BY id"
                ).fetchall()
            ]
        self.assertEqual(tuple(statuses), ("consumed", "running"))
        self.assertEqual(
            events,
            ["approval.created", "approval.approved", "approval.consumed"],
        )

    def test_successful_http_approval_creates_exact_hostname_rule_without_path(self) -> None:
        """HTTP always 规则只能保存小写 hostname + port，不能保存 query/path。"""
        arguments: dict[str, JsonValue] = {
            "url": "https://example.com/private/path?token=secret",
            "timeout_seconds": 20,
        }
        approval = self.repository.create_waiting(
            self.context,
            ToolCall("http-1", "http_get", arguments),
            arguments,
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="http_get https://example.com:443",
        )
        self.repository.approve(self.owner_id, approval.id)
        run = self.repository.consume(self.owner_id, approval.id)
        ToolRunRepository(self.database).succeed(run.id, "{}", 1)
        rules = PolicyRuleRepository(self.database)

        first = rules.add_network_from_approval(self.owner_id, approval.id)
        second = rules.add_network_from_approval(self.owner_id, approval.id)

        self.assertEqual(first, second)
        self.assertEqual(rules.network_rules(self.owner_id), (NetworkRule("example.com"),))
        with self.database.connect_read_only() as connection:
            stored = connection.execute(
                "SELECT rule_json FROM policy_rules WHERE id = ?", (first,)
            ).fetchone()[0]
        self.assertEqual(
            json.loads(stored),
            {"hostname": "example.com", "port": 443, "type": "exact_hostname"},
        )
        self.assertNotIn("secret", stored)

    def test_inline_osascript_cannot_become_persistent_rule(self) -> None:
        """即使绕过 TUI，Repository 也必须拒绝持久化 inline AppleScript。"""
        program = self.paths.workspace / "osascript"
        program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        program.chmod(0o700)
        arguments: dict[str, JsonValue] = {
            "program": str(program),
            "args": ["-e", 'display dialog "secret"'],
            "timeout_seconds": 30,
        }
        approval = self.repository.create_waiting(
            self.context,
            ToolCall("osascript-1", "run_command", arguments),
            arguments,
            PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required"),
            ttl_seconds=600,
            summary="run_command osascript",
        )
        self.repository.approve(self.owner_id, approval.id)
        run = self.repository.consume(self.owner_id, approval.id)
        ToolRunRepository(self.database).succeed(run.id, "{}", 1)

        with self.assertRaises(ApprovalError) as rejected:
            PolicyRuleRepository(self.database).add_command_from_approval(
                self.owner_id,
                approval.id,
            )

        self.assertEqual(rejected.exception.code, "scope_forbidden")
        with self.database.connect_read_only() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM policy_rules").fetchone()[0],
                0,
            )

    def test_stale_running_tool_is_interrupted_once_and_never_replayed(self) -> None:
        """崩溃遗留的旧 running 记录只转 interrupted，不执行原动作。"""
        runs = ToolRunRepository(self.database)
        run_id = runs.start(
            self.context,
            ToolCall("stale-1", "write_file", {"path": "never-created.txt"}),
            {"path": "never-created.txt"},
            PolicyDecision(PolicyAction.ALLOW, "test"),
        )
        with self.database.connect() as connection:
            connection.execute(
                "UPDATE tool_runs SET created_at = ? WHERE id = ?",
                ((self.now - timedelta(hours=1)).isoformat(), run_id),
            )

        recovered = runs.interrupt_stale_runs(stale_before=self.now)
        repeated = runs.interrupt_stale_runs(stale_before=self.now)

        self.assertEqual(recovered, (run_id,))
        self.assertEqual(repeated, ())
        self.assertFalse((self.paths.workspace / "never-created.txt").exists())
        with self.database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT status, completed_at FROM tool_runs WHERE id = ?", (run_id,)
            ).fetchone()
            events = connection.execute(
                "SELECT event_type FROM audit_events WHERE event_type = 'tool.interrupted'"
            ).fetchall()
        self.assertEqual(row["status"], "interrupted")
        self.assertIsNotNone(row["completed_at"])
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
