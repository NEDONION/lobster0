"""飞书 Approval 卡片、文本命令与 Owner gate 测试。"""

import json
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from miniclaw.agent.turn import TurnResult
from miniclaw.channels.approvals import (
    ApprovalEnvelope,
    ApprovalPrompt,
    ChannelApprovalController,
    approval_delivery_payload,
    feishu_approval_prompt,
    feishu_approval_status_card,
    parse_approval_delivery_payload,
    text_approval_prompt,
)
from miniclaw.policy.approvals import ApprovalDecision, ApprovalError
from miniclaw.storage.tooling import ApprovalPresentation, StoredApproval


@dataclass(slots=True)
class FakeApprovalRepository:
    """返回 Core 已计算的展示字段。"""

    modes: tuple[ApprovalDecision, ...] = (ApprovalDecision.ONCE,)
    summary: str = "write_file note.txt"

    def presentation(self, user_id: int, approval_id: int) -> ApprovalPresentation:
        """构造不含参数正文的审批视图。"""
        approval = StoredApproval(
            id=approval_id,
            user_id=user_id,
            turn_id=11,
            tool_run_id=12,
            tool_name="write_file",
            arguments_hash="hash-private",
            summary=self.summary,
            status="pending",
            expires_at=datetime.now(UTC) + timedelta(minutes=10),
            decided_at=None,
            created_at=datetime.now(UTC),
        )
        return ApprovalPresentation(approval=approval, grant_modes=self.modes)


@dataclass(slots=True)
class FakeContinuationService:
    """记录 Controller 是否绕过模型直达 continuation。"""

    error: ApprovalError | None = None
    calls: list[tuple[int, int, ApprovalDecision]] = field(default_factory=list)

    async def continue_approval(
        self,
        user_id: int,
        approval_id: int,
        *,
        decision: ApprovalDecision,
        on_text=None,
        on_event=None,
    ) -> TurnResult:
        """返回固定 continuation 结果或稳定 Core 错误。"""
        del on_text, on_event
        self.calls.append((user_id, approval_id, decision))
        if self.error is not None:
            raise self.error
        return TurnResult(
            turn_id=21,
            session_id=3,
            content=f"approval:{approval_id}:{decision.value}",
            input_tokens=1,
            output_tokens=1,
            provider_request_id="req_approval",
            message_id=31,
            approval_id=None,
        )


class ChannelApprovalTest(unittest.IsolatedAsyncioTestCase):
    """验证飞书只是 Core Approval 的薄控制器。"""

    def _controller(
        self,
        repository: FakeApprovalRepository | None = None,
        service: FakeContinuationService | None = None,
    ) -> tuple[ChannelApprovalController, FakeContinuationService]:
        """创建只允许 ou_owner 决策的 Controller。"""
        continuation = service or FakeContinuationService()
        return (
            ChannelApprovalController(
                owner_external_user_id="ou_owner",
                approvals=repository or FakeApprovalRepository(),
                service=continuation,
            ),
            continuation,
        )

    def test_prompt_uses_only_core_modes_and_redacted_summary(self) -> None:
        """卡片按钮只能来自 Core grant_modes，且不得包含哈希或原始参数。"""
        controller, _ = self._controller(
            FakeApprovalRepository(
                modes=(ApprovalDecision.ONCE, ApprovalDecision.SESSION),
                summary="run_command /usr/bin/open -a Feishu",
            )
        )

        envelope = controller.prompt(user_id=1, approval_id=7)
        prompt = feishu_approval_prompt(envelope)
        rendered = json.dumps(prompt.card, ensure_ascii=False)

        self.assertIn("run_command /usr/bin/open -a Feishu", rendered)
        self.assertIn('"decision": "once"', rendered)
        self.assertIn('"decision": "session"', rendered)
        self.assertNotIn('"decision": "always"', rendered)
        self.assertIn('"decision": "deny"', rendered)
        self.assertNotIn("hash-private", rendered)
        self.assertIn("/approve 7 once", prompt.fallback_text)
        payload = json.loads(approval_delivery_payload(envelope))
        self.assertEqual(payload["version"], 2)
        self.assertEqual(payload["approval_id"], 7)
        self.assertEqual(payload["tool_name"], "write_file")
        self.assertEqual(payload["decisions"], ["once", "session", "deny"])
        self.assertNotIn("card", payload)

    async def test_text_commands_bypass_model_and_map_all_decisions(self) -> None:
        """approve once/session/always 与 deny 均严格解析并直达 continuation。"""
        controller, service = self._controller()
        commands = (
            ("/approve 7 once", ApprovalDecision.ONCE),
            ("/approve 8 session", ApprovalDecision.SESSION),
            ("/approve 9 always", ApprovalDecision.ALWAYS),
            ("/deny 10", ApprovalDecision.DENY),
        )
        for text, decision in commands:
            outcome = await controller.handle_text(
                user_id=1,
                actor_external_user_id="ou_owner",
                text=text,
            )
            self.assertTrue(outcome.handled)
            self.assertIsNotNone(outcome.result)
            self.assertEqual(service.calls[-1], (1, int(text.split()[1]), decision))

    async def test_non_owner_and_malformed_commands_never_reach_core(self) -> None:
        """非 Owner 或格式不合法都不能触发 Approval 状态机。"""
        controller, service = self._controller()

        denied = await controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_friend",
            text="/approve 7 once",
        )
        malformed = await controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="/approve 7 root",
        )
        unrelated = await controller.handle_text(
            user_id=1,
            actor_external_user_id="ou_owner",
            text="帮我看看审批",
        )

        self.assertEqual(service.calls, [])
        self.assertIn("只有 Owner", denied.notice or "")
        self.assertIn("用法", malformed.notice or "")
        self.assertFalse(unrelated.handled)

    async def test_card_action_is_strict_and_core_errors_are_idempotent_safe(self) -> None:
        """按钮 payload 必须同形；重复/过期只返回稳定提示，不泄露 Core 原文。"""
        service = FakeContinuationService(
            error=ApprovalError("already_decided", "private database detail")
        )
        controller, service = self._controller(service=service)
        value: dict[str, Any] = {
            "version": 2,
            "miniclaw_action": "approval",
            "approval_id": 7,
            "decision": "once",
        }

        outcome = await controller.handle_card_action(
            user_id=1,
            actor_external_user_id="ou_owner",
            value=value,
        )
        malformed = await controller.handle_card_action(
            user_id=1,
            actor_external_user_id="ou_owner",
            value={**value, "extra": "not-allowed"},
        )

        self.assertTrue(outcome.handled)
        self.assertIn("已经处理", outcome.notice or "")
        self.assertIs(outcome.decision, ApprovalDecision.ONCE)
        self.assertNotIn("private database detail", outcome.notice or "")
        self.assertTrue(malformed.handled)
        self.assertIn("无法识别", malformed.notice or "")
        self.assertEqual(len(service.calls), 1)

    def test_status_cards_are_bounded_terminal_and_have_no_actions(self) -> None:
        """审批状态卡必须有界，终态按结果着色且不能残留可重复点击按钮。"""
        cases = (
            ("processing", "orange", "处理中"),
            ("succeeded", "green", "已完成"),
            ("denied", "red", "已拒绝"),
            ("failed", "red", "处理失败"),
        )
        for state, template, title in cases:
            with self.subTest(state=state):
                card = feishu_approval_status_card(state, "x" * 5_000)
                rendered = json.dumps(card, ensure_ascii=False)
                self.assertEqual(card["header"]["template"], template)
                self.assertIn(title, card["header"]["title"]["content"])
                self.assertNotIn('"tag": "action"', rendered)
                self.assertLessEqual(len(card["elements"][0]["content"]), 2_000)


class ApprovalEnvelopeV2Test(unittest.TestCase):
    """验证 durable Approval v2 是严格、平台中立且兼容旧 v1 的 envelope。"""

    def _envelope(self) -> ApprovalEnvelope:
        """返回稳定的合法 v2 fixture。"""
        return ApprovalEnvelope(
            version=2,
            approval_id=7,
            tool_name="write_file",
            summary="write_file note.txt",
            decisions=(ApprovalDecision.ONCE, ApprovalDecision.DENY),
            expires_at="2026-08-08T09:00:00+00:00",
            fallback_text="卡片不可用时发送：/approve 7 once；/deny 7",
        )

    def test_v2_round_trip_is_neutral_strict_and_redacted(self) -> None:
        """新 writer 不得持久化平台 Card，parser 应恢复 typed envelope。"""
        envelope = self._envelope()

        payload = approval_delivery_payload(envelope)
        decoded = json.loads(payload)
        parsed = parse_approval_delivery_payload(payload)

        self.assertEqual(parsed, envelope)
        self.assertEqual(
            set(decoded),
            {
                "version",
                "approval_id",
                "tool_name",
                "summary",
                "decisions",
                "expires_at",
                "fallback_text",
            },
        )
        self.assertNotIn("card", decoded)
        self.assertNotIn(envelope.summary, repr(envelope))
        self.assertNotIn(envelope.fallback_text, repr(envelope))
        text_prompt = text_approval_prompt(envelope)
        self.assertIn("MiniClaw 审批 #7", text_prompt)
        self.assertIn("/approve 7 once", text_prompt)

    def test_v1_payload_remains_readable_but_is_not_written(self) -> None:
        """升级前 pending Card 仍能恢复；再次编码只接受 v2 envelope。"""
        legacy = json.dumps(
            {
                "version": 1,
                "card": {"header": {"title": "legacy"}},
                "fallback_text": "/deny 7",
            },
            ensure_ascii=False,
        )

        parsed = parse_approval_delivery_payload(legacy)

        self.assertIsInstance(parsed, ApprovalPrompt)
        self.assertEqual(parsed.fallback_text, "/deny 7")
        with self.assertRaises(TypeError):
            approval_delivery_payload(parsed)  # type: ignore[arg-type]

    def test_invalid_v2_values_fail_closed(self) -> None:
        """额外 key、bool ID、未知 decision、坏时间和控制字符全部拒绝。"""
        valid = json.loads(approval_delivery_payload(self._envelope()))
        invalid = (
            {**valid, "extra": True},
            {**valid, "version": True},
            {**valid, "approval_id": True},
            {**valid, "decisions": ["root", "deny"]},
            {**valid, "expires_at": "tomorrow"},
            {**valid, "summary": "unsafe\0summary"},
            {**valid, "fallback_text": ""},
        )
        for value in invalid:
            with self.subTest(keys=sorted(value)):
                with self.assertRaises(ValueError):
                    parse_approval_delivery_payload(json.dumps(value))


if __name__ == "__main__":
    unittest.main()
