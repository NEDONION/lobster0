"""飞书审批按钮的原卡收口与 durable Delivery 编排测试。"""

import asyncio
import json
import unittest
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from miniclaw.agent.turn import TurnResult
from miniclaw.channels.approvals import (
    ApprovalCommandOutcome,
    ApprovalEnvelope,
    approval_delivery_payload,
)
from miniclaw.channels.feishu_approval import FeishuApprovalActionHandler
from miniclaw.policy.approvals import ApprovalDecision


def _result(
    *,
    content: str = "操作完成。",
    message_id: int | None = 31,
    approval_id: int | None = None,
) -> TurnResult:
    """构造 callback continuation 的最小结果。"""
    return TurnResult(
        turn_id=21,
        session_id=3,
        content=content,
        input_tokens=1,
        output_tokens=1,
        provider_request_id="req_callback",
        message_id=message_id,
        approval_id=approval_id,
    )


def _approval_payload(approval_id: int) -> str:
    """构造 sent Approval Delivery 中保存的持久 envelope。"""
    return approval_delivery_payload(
        ApprovalEnvelope(
            version=2,
            approval_id=approval_id,
            tool_name="write_file",
            summary="write_file next.txt",
            decisions=(ApprovalDecision.ONCE, ApprovalDecision.DENY),
            expires_at="2026-08-09T10:00:00+00:00",
            fallback_text=f"发送 /approve {approval_id} once 或 /deny {approval_id}",
        )
    )


@dataclass(slots=True)
class FakeController:
    """返回固定审批结果并记录 callback 调用。"""

    outcome: ApprovalCommandOutcome | None = None
    error: Exception | None = None
    gate: asyncio.Event | None = None
    log: list[str] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def handle_card_action(self, **kwargs: Any) -> ApprovalCommandOutcome:
        """记录动作并返回结果或模拟异常。"""
        self.log.append("controller")
        self.calls.append(kwargs)
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        assert self.outcome is not None
        return self.outcome

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalEnvelope:
        """构造下一条 durable 审批 envelope。"""
        del user_id
        return ApprovalEnvelope(
            version=2,
            approval_id=approval_id,
            tool_name="write_file",
            summary="write_file next.txt",
            decisions=(ApprovalDecision.ONCE, ApprovalDecision.DENY),
            expires_at="2026-08-09T10:00:00+00:00",
            fallback_text=f"发送 /approve {approval_id} once 或 /deny {approval_id}",
        )


@dataclass(slots=True)
class FakeTransport:
    """记录同一飞书卡片的全部更新。"""

    log: list[str]
    updates: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    block_on_update: int | None = None
    update_started: asyncio.Event = field(default_factory=asyncio.Event)
    release_update: asyncio.Event = field(default_factory=asyncio.Event)

    async def update_card(self, message_id: str, card: dict[str, Any]) -> SimpleNamespace:
        """保存 update_card 参数。"""
        self.log.append("update")
        self.updates.append((message_id, card))
        if self.block_on_update == len(self.updates):
            self.block_on_update = None
            self.update_started.set()
            await self.release_update.wait()
        return SimpleNamespace(platform_message_id=message_id)


@dataclass(slots=True)
class FakeMessages:
    """为下一条 Approval 创建 durable channel notice。"""

    notices: list[tuple[int, str]] = field(default_factory=list)

    def create_channel_notice(self, session_id: int, content: str) -> SimpleNamespace:
        """返回带稳定 ID 的假消息。"""
        self.notices.append((session_id, content))
        return SimpleNamespace(id=41, content=content)


@dataclass(slots=True)
class FakeDeliveries:
    """记录最终消息或下一条审批的 durable outbox。"""

    calls: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False
    receipt_content: str | None = field(default_factory=lambda: _approval_payload(7))

    def find_sent_by_platform_message_id(self, **kwargs: Any) -> SimpleNamespace | None:
        """只对当前账号的 sent Approval 卡返回持久 envelope。"""
        if (
            kwargs
            == {
                "channel": "feishu",
                "account_id": "default",
                "platform_message_id": "om_card",
                "kind": "approval",
            }
            and self.receipt_content is not None
        ):
            return SimpleNamespace(content=self.receipt_content)
        return None

    def create_parts(self, **kwargs: Any) -> tuple[()]:
        """保存 Delivery 参数。"""
        if self.fail:
            raise RuntimeError("private delivery detail")
        self.calls.append(kwargs)
        return ()


class FeishuApprovalActionHandlerTest(unittest.IsolatedAsyncioTestCase):
    """验证 callback 先更新处理中，再持久化结果并收口原卡。"""

    def _handler(
        self,
        controller: FakeController,
        *,
        log: list[str] | None = None,
        receipt_content: str | None = "default",
    ) -> tuple[FeishuApprovalActionHandler, FakeTransport, FakeMessages, FakeDeliveries]:
        """创建无网络 callback 编排器和全部 fake 依赖。"""
        shared_log = log if log is not None else []
        transport = FakeTransport(shared_log)
        messages = FakeMessages()
        deliveries = FakeDeliveries(
            receipt_content=(
                _approval_payload(7) if receipt_content == "default" else receipt_content
            )
        )
        return (
            FeishuApprovalActionHandler(
                user_id=1,
                owner_external_user_id="ou_owner",
                account_id="default",
                message_max_chars=2_000,
                controller=controller,
                transport=transport,
                messages=messages,
                deliveries=deliveries,
            ),
            transport,
            messages,
            deliveries,
        )

    async def test_success_updates_same_card_and_queues_durable_result(self) -> None:
        """成功点击必须先橙后绿，并把模型最终消息写入 outbox。"""
        log: list[str] = []
        controller = FakeController(
            ApprovalCommandOutcome(
                True,
                result=_result(),
                approval_id=7,
                decision=ApprovalDecision.ONCE,
            ),
            log=log,
        )
        handler, transport, _, deliveries = self._handler(controller, log=log)

        await handler(
            actor_open_id="ou_owner",
            value={
                "version": 2,
                "miniclaw_action": "approval",
                "approval_id": 7,
                "decision": "once",
            },
            chat_id="oc_chat",
            message_id="om_card",
        )

        self.assertEqual(log[:2], ["update", "controller"])
        self.assertEqual(controller.calls[0]["expected_approval_id"], 7)
        self.assertEqual([item[0] for item in transport.updates], ["om_card", "om_card"])
        self.assertEqual(
            [item[1]["header"]["template"] for item in transport.updates],
            ["orange", "green"],
        )
        self.assertEqual(len(deliveries.calls), 1)
        self.assertEqual(deliveries.calls[0]["kind"], "message")
        self.assertEqual(deliveries.calls[0]["message_id"], 31)
        self.assertEqual(deliveries.calls[0]["reply_to_message_id"], "om_card")

    async def test_unbound_or_mismatched_source_card_fails_closed(self) -> None:
        """缺少 sent receipt 或 envelope ID 不匹配时不能更新卡片或调用 Core。"""
        cases = (
            None,
            _approval_payload(8),
            "not-json",
        )
        for receipt_content in cases:
            with self.subTest(receipt_content=receipt_content):
                controller = FakeController(
                    ApprovalCommandOutcome(
                        True,
                        result=_result(),
                        approval_id=7,
                        decision=ApprovalDecision.ONCE,
                    )
                )
                handler, transport, _, deliveries = self._handler(
                    controller,
                    receipt_content=receipt_content,
                )

                await handler(
                    actor_open_id="ou_owner",
                    value={
                        "version": 2,
                        "miniclaw_action": "approval",
                        "approval_id": 7,
                        "decision": "once",
                    },
                    chat_id="oc_chat",
                    message_id="om_card",
                )

                self.assertEqual(controller.calls, [])
                self.assertEqual(transport.updates, [])
                self.assertEqual(deliveries.calls, [])

    async def test_next_approval_is_persisted_and_original_card_completes(self) -> None:
        """续跑再次等待审批时必须创建下一张 durable 卡，不能丢失。"""
        controller = FakeController(
            ApprovalCommandOutcome(
                True,
                result=_result(content="需要下一步审批。", message_id=None, approval_id=8),
                approval_id=7,
                decision=ApprovalDecision.ONCE,
            )
        )
        handler, transport, messages, deliveries = self._handler(controller)

        await handler(
            actor_open_id="ou_owner",
            value={
                "version": 2,
                "miniclaw_action": "approval",
                "approval_id": 7,
                "decision": "once",
            },
            chat_id="oc_chat",
            message_id="om_card",
        )

        self.assertEqual(transport.updates[-1][1]["header"]["template"], "green")
        self.assertEqual(messages.notices, [(3, "发送 /approve 8 once 或 /deny 8")])
        self.assertEqual(deliveries.calls[0]["kind"], "approval")
        payload = json.loads(deliveries.calls[0]["contents"][0])
        self.assertEqual(payload["approval_id"], 8)

    async def test_deny_and_malformed_actions_finish_red_without_reexecution(self) -> None:
        """Owner 拒绝必须红色收口且不能留下按钮。"""
        handler, transport, _, _ = self._handler(
            FakeController(
                ApprovalCommandOutcome(
                    True,
                    result=_result(content="已拒绝。"),
                    approval_id=7,
                    decision=ApprovalDecision.DENY,
                )
            )
        )
        await handler(
            actor_open_id="ou_owner",
            value={
                "version": 2,
                "miniclaw_action": "approval",
                "approval_id": 7,
                "decision": "deny",
            },
            chat_id="oc_chat",
            message_id="om_card",
        )

        final = transport.updates[-1][1]
        self.assertEqual(final["header"]["template"], "red")
        self.assertNotIn('"tag": "action"', json.dumps(final))

    async def test_non_owner_and_malformed_actions_leave_pending_card_untouched(self) -> None:
        """非 Owner 或坏 payload 不能更新原卡、移除按钮或进入 Core。"""
        for actor, value in (
            (
                "ou_friend",
                {
                    "version": 2,
                    "miniclaw_action": "approval",
                    "approval_id": 7,
                    "decision": "once",
                },
            ),
            ("ou_owner", {"decision": "once"}),
        ):
            with self.subTest(actor=actor):
                controller = FakeController(
                    ApprovalCommandOutcome(True, notice="must not be used")
                )
                handler, transport, _, _ = self._handler(controller)

                await handler(
                    actor_open_id=actor,
                    value=value,
                    chat_id="oc_chat",
                    message_id="om_card",
                )

                self.assertEqual(controller.log, [])
                self.assertEqual(transport.updates, [])

    async def test_already_decided_duplicate_keeps_terminal_card_non_failure(self) -> None:
        """重复 callback 只能落到已处理终态，不能把成功卡覆盖成失败。"""
        controller = FakeController(
            ApprovalCommandOutcome(
                True,
                notice="这条审批已经处理，不会重复执行。",
                approval_id=7,
                decision=ApprovalDecision.ONCE,
                error_code="already_decided",
            )
        )
        handler, transport, _, deliveries = self._handler(controller)

        await handler(
            actor_open_id="ou_owner",
            value={
                "version": 2,
                "miniclaw_action": "approval",
                "approval_id": 7,
                "decision": "once",
            },
            chat_id="oc_chat",
            message_id="om_card",
        )

        self.assertEqual(transport.updates[-1][1]["header"]["template"], "green")
        self.assertIn("already_decided", json.dumps(transport.updates[-1][1]))
        self.assertEqual(deliveries.calls, [])

    async def test_unexpected_controller_error_is_redacted_and_swallowed(self) -> None:
        """callback 内部异常不能传回 SDK，也不能把异常正文写进卡片。"""
        controller = FakeController(error=RuntimeError("private callback detail"))
        handler, transport, _, deliveries = self._handler(controller)

        await handler(
            actor_open_id="ou_owner",
            value={
                "version": 2,
                "miniclaw_action": "approval",
                "approval_id": 7,
                "decision": "once",
            },
            chat_id="oc_chat",
            message_id="om_card",
        )

        final = json.dumps(transport.updates[-1][1], ensure_ascii=False)
        self.assertIn("失败阶段", final)
        self.assertIn("approval_callback_failed", final)
        self.assertIn("Approval #7", final)
        self.assertIn("Tool 状态", final)
        self.assertIn("下一步", final)
        self.assertIn("不会自动重试", final)
        self.assertNotIn("未执行任何新操作", final)
        self.assertNotIn("private callback detail", final)
        self.assertEqual(deliveries.calls, [])

    async def test_delivery_failure_reports_possible_side_effects_and_turn_id(self) -> None:
        """续跑后 Outbox 失败必须暴露安全调试编号并提示检查 ToolRun。"""
        controller = FakeController(
            ApprovalCommandOutcome(
                True,
                result=_result(),
                approval_id=7,
                decision=ApprovalDecision.ONCE,
            )
        )
        handler, transport, _, deliveries = self._handler(controller)
        deliveries.fail = True

        await handler(
            actor_open_id="ou_owner",
            value={
                "version": 2,
                "miniclaw_action": "approval",
                "approval_id": 7,
                "decision": "once",
            },
            chat_id="oc_chat",
            message_id="om_card",
        )

        final = json.dumps(transport.updates[-1][1], ensure_ascii=False)
        self.assertIn("approval_delivery_failed", final)
        self.assertIn("Turn #21", final)
        self.assertIn("Approval #7", final)
        self.assertIn("可能已经执行", final)
        self.assertNotIn("private delivery detail", final)

    async def test_gateway_cancellation_finishes_processing_card_with_diagnostics(self) -> None:
        """Gateway 取消审批续跑时必须把处理中卡收口为可调试失败卡。"""
        gate = asyncio.Event()
        controller = FakeController(
            ApprovalCommandOutcome(True, notice="unused"),
            gate=gate,
        )
        handler, transport, _, _ = self._handler(controller)
        task = asyncio.create_task(
            handler(
                actor_open_id="ou_owner",
                value={
                    "version": 2,
                    "miniclaw_action": "approval",
                    "approval_id": 7,
                    "decision": "once",
                },
                chat_id="oc_chat",
                message_id="om_card",
            )
        )
        while controller.log != ["controller"]:
            await asyncio.sleep(0)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        final = json.dumps(transport.updates[-1][1], ensure_ascii=False)
        self.assertIn("approval_callback_interrupted", final)
        self.assertIn("Gateway", final)
        self.assertIn("Approval #7", final)
        self.assertEqual(transport.updates[-1][1]["header"]["template"], "red")

    async def test_cancellation_during_terminal_update_retries_same_final_card(self) -> None:
        """Core 已完成后即使 terminal update 被取消，也必须重试同一终态再传播取消。"""
        controller = FakeController(
            ApprovalCommandOutcome(
                True,
                result=_result(),
                approval_id=7,
                decision=ApprovalDecision.ONCE,
            )
        )
        handler, transport, _, deliveries = self._handler(controller)
        transport.block_on_update = 2
        task = asyncio.create_task(
            handler(
                actor_open_id="ou_owner",
                value={
                    "version": 2,
                    "miniclaw_action": "approval",
                    "approval_id": 7,
                    "decision": "once",
                },
                chat_id="oc_chat",
                message_id="om_card",
            )
        )
        await asyncio.wait_for(transport.update_started.wait(), timeout=1)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(len(deliveries.calls), 1)
        self.assertGreaterEqual(len(transport.updates), 3)
        self.assertEqual(transport.updates[-1][1]["header"]["template"], "green")


if __name__ == "__main__":
    unittest.main()
