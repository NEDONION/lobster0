"""飞书 Approval 卡片、文本命令与 Owner gate 测试。"""

import json
import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from miniclaw.agent.turn import TurnResult
from miniclaw.channels.approvals import (
    ChannelApprovalController,
    approval_delivery_payload,
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
                owner_open_id="ou_owner",
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

        prompt = controller.prompt(user_id=1, approval_id=7)
        rendered = json.dumps(prompt.card, ensure_ascii=False)

        self.assertIn("run_command /usr/bin/open -a Feishu", rendered)
        self.assertIn('"decision": "once"', rendered)
        self.assertIn('"decision": "session"', rendered)
        self.assertNotIn('"decision": "always"', rendered)
        self.assertIn('"decision": "deny"', rendered)
        self.assertNotIn("hash-private", rendered)
        self.assertIn("/approve 7 once", prompt.fallback_text)
        payload = json.loads(approval_delivery_payload(prompt))
        self.assertEqual(payload["fallback_text"], prompt.fallback_text)
        self.assertEqual(payload["card"], prompt.card)

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
                actor_open_id="ou_owner",
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
            actor_open_id="ou_friend",
            text="/approve 7 once",
        )
        malformed = await controller.handle_text(
            user_id=1,
            actor_open_id="ou_owner",
            text="/approve 7 root",
        )
        unrelated = await controller.handle_text(
            user_id=1,
            actor_open_id="ou_owner",
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
            "miniclaw_action": "approval",
            "approval_id": 7,
            "decision": "once",
        }

        outcome = await controller.handle_card_action(
            user_id=1,
            actor_open_id="ou_owner",
            value=value,
        )
        malformed = await controller.handle_card_action(
            user_id=1,
            actor_open_id="ou_owner",
            value={**value, "extra": "not-allowed"},
        )

        self.assertTrue(outcome.handled)
        self.assertIn("已经处理", outcome.notice or "")
        self.assertNotIn("private database detail", outcome.notice or "")
        self.assertTrue(malformed.handled)
        self.assertIn("无法识别", malformed.notice or "")
        self.assertEqual(len(service.calls), 1)


if __name__ == "__main__":
    unittest.main()
