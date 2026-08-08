"""Typing 与 streaming card 的 best-effort 能力测试。"""

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from miniclaw.agent.events import RunEvent
from miniclaw.channels.base import ChannelTransportError, SendReceipt
from miniclaw.channels.capabilities import ChannelCapabilities
from miniclaw.storage.channels import InboundEventKey, StoredInboundEvent


@dataclass(slots=True)
class MutableMonotonic:
    """可手动推进的单调时钟。"""

    value: float = 0.0

    def __call__(self) -> float:
        """返回当前测试时间。"""
        return self.value


@dataclass(slots=True)
class FakeCapabilityTransport:
    """记录 Typing/Card 调用并可注入卡片失败。"""

    fail_typing: bool = False
    fail_card_create: bool = False
    fail_card_update: bool = False
    typing_added: list[str] = field(default_factory=list)
    typing_removed: list[tuple[str, str]] = field(default_factory=list)
    cards_sent: list[tuple[str, str, dict[str, Any], str]] = field(default_factory=list)
    cards_updated: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def add_typing(self, message_id: str) -> str | None:
        """添加 Typing 或模拟平台拒绝。"""
        self.typing_added.append(message_id)
        if self.fail_typing:
            raise ChannelTransportError("feishu_permission_denied")
        return "reaction_1"

    async def remove_typing(self, message_id: str, reaction_id: str | None) -> bool:
        """记录 Typing 清理。"""
        if reaction_id is not None:
            self.typing_removed.append((message_id, reaction_id))
        return True

    async def send_card(
        self,
        *,
        conversation_id: str,
        reply_to_message_id: str,
        card: dict[str, Any],
        idempotency_key: str,
    ) -> SendReceipt:
        """创建进度卡片或模拟失败。"""
        self.cards_sent.append(
            (conversation_id, reply_to_message_id, card, idempotency_key)
        )
        if self.fail_card_create:
            raise ChannelTransportError("feishu_permission_denied")
        return SendReceipt("om_progress_card")

    async def update_card(
        self,
        platform_message_id: str,
        card: dict[str, Any],
    ) -> SendReceipt:
        """更新进度卡片或模拟失败。"""
        self.cards_updated.append((platform_message_id, card))
        if self.fail_card_update:
            raise ChannelTransportError("feishu_rate_limited", retryable=True)
        return SendReceipt(platform_message_id)


@dataclass(slots=True)
class RecordingObserver:
    """记录能力层失败，不接收正文或原始异常。"""

    events: list[dict[str, Any]] = field(default_factory=list)

    def capability(self, **event: Any) -> None:
        """保存单条安全 capability 事件。"""
        self.events.append(event)


class ChannelCapabilitiesTest(unittest.IsolatedAsyncioTestCase):
    """验证进度能力不会改变 Agent 或 durable final delivery。"""

    def setUp(self) -> None:
        """创建固定入站事件和单调时钟。"""
        self.clock = MutableMonotonic()
        self.event = StoredInboundEvent(
            key=InboundEventKey("feishu", "default", "om_inbound"),
            event_id="evt_inbound",
            external_user_id="ou_owner",
            external_conversation_id="oc_chat",
            chat_type="p2p",
            message_type="text",
            content="question",
            reply_to_message_id="om_inbound",
            session_id=1,
            status="running",
            attempts=1,
            last_error_code=None,
            received_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def _activity(
        self,
        transport: FakeCapabilityTransport,
        *,
        enabled: bool = True,
        observer: RecordingObserver | None = None,
    ):
        """创建单条消息的能力会话。"""
        capabilities = ChannelCapabilities(
            transport=transport,
            streaming_card=enabled,
            update_interval=0.5,
            clock=self.clock,
            observer=observer,
        )
        return capabilities.activity(self.event)

    async def test_typing_starts_and_always_ends_best_effort(self) -> None:
        """Typing 失败不能中断 Turn，成功 token 必须在完成时移除。"""
        failing = FakeCapabilityTransport(fail_typing=True)
        observer = RecordingObserver()
        activity = self._activity(failing, observer=observer)
        await activity.start()
        await activity.finish(content="answer", failed=False)
        self.assertEqual(failing.typing_added, ["om_inbound"])
        self.assertEqual(failing.typing_removed, [])
        self.assertEqual(observer.events[0]["capability"], "typing_add")
        self.assertEqual(observer.events[0]["error_code"], "feishu_permission_denied")

        successful = FakeCapabilityTransport()
        activity = self._activity(successful)
        await activity.start()
        await activity.finish(content="answer", failed=False)
        self.assertEqual(
            successful.typing_removed,
            [("om_inbound", "reaction_1")],
        )

    async def test_tool_activity_creates_safe_claw_trail_and_updates_same_card(self) -> None:
        """Turn 开始时建轨迹卡，Tool 只更新原卡且敏感参数不能进入卡片。"""
        transport = FakeCapabilityTransport()
        activity = self._activity(transport)
        await activity.start()

        self.assertEqual(len(transport.cards_sent), 1)
        self.assertIn("MiniClaw · 执行中", repr(transport.cards_sent[0][2]))

        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "你"}))
        await activity.on_event(
            RunEvent("model_reasoning", 1, {"text": "secret reasoning"})
        )
        await activity.on_event(
            RunEvent(
                "tool_requested",
                1,
                {
                    "call_id": "call_1",
                    "tool_name": "run_command",
                    "arguments": {
                        "program": "lark-cli",
                        "args": ["drive", "+search", "--token", "secret-token"],
                    },
                },
            )
        )
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "好"}))
        self.assertEqual(len(transport.cards_sent), 1)
        self.assertEqual(len(transport.cards_updated), 0)

        self.clock.value = 0.6
        await activity.on_event(
            RunEvent(
                "tool_started",
                1,
                {"call_id": "call_1", "tool_name": "run_command"},
            )
        )
        self.assertEqual(len(transport.cards_sent), 1)
        self.assertEqual(len(transport.cards_updated), 1)
        self.assertEqual(transport.cards_updated[0][0], "om_progress_card")

        self.clock.value = 1.2
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "！"}))
        await activity.finish(content="你好！", failed=False)

        self.assertEqual(len(transport.cards_updated), 3)
        self.assertTrue(
            all(message_id == "om_progress_card" for message_id, _ in transport.cards_updated)
        )
        self.assertIn("MiniClaw · 已完成", repr(transport.cards_updated[-1][1]))
        self.assertIn("Claw Trail", repr(transport.cards_updated[-1][1]))
        self.assertEqual(
            transport.cards_updated[-1][1]["body"]["elements"][0]["text_size"],
            "small",
        )
        rendered = repr([transport.cards_sent, transport.cards_updated])
        self.assertIn("你好！", rendered)
        self.assertNotIn("secret reasoning", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertEqual(
            transport.cards_sent[0][3],
            activity.idempotency_key,
        )

    async def test_card_failures_are_contained_and_final_can_fall_back(self) -> None:
        """卡片创建/更新失败只关闭进度能力，让 Manager 继续 durable Markdown。"""
        for transport in (
            FakeCapabilityTransport(fail_card_create=True),
            FakeCapabilityTransport(fail_card_update=True),
        ):
            with self.subTest(transport=transport):
                activity = self._activity(transport)
                await activity.start()
                await activity.on_event(
                    RunEvent("model_text_delta", 1, {"text": "partial"})
                )
                await activity.on_event(
                    RunEvent(
                        "tool_requested",
                        1,
                        {
                            "call_id": "call_1",
                            "tool_name": "read_file",
                            "arguments": {"path": "README.md"},
                        },
                    )
                )
                await activity.on_event(
                    RunEvent(
                        "tool_started",
                        1,
                        {"call_id": "call_1", "tool_name": "read_file"},
                    )
                )
                outcome = await activity.finish(content="final answer", failed=False)
                self.assertTrue(outcome.card_failed)
                self.assertTrue(outcome.final_markdown_required)

    async def test_disabled_streaming_uses_no_card(self) -> None:
        """关闭 streaming_card 时只保留 Typing 和最终 durable Markdown。"""
        transport = FakeCapabilityTransport()
        activity = self._activity(transport, enabled=False)
        await activity.start()
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "answer"}))
        outcome = await activity.finish(content="answer", failed=False)

        self.assertEqual(transport.cards_sent, [])
        self.assertEqual(transport.cards_updated, [])
        self.assertTrue(outcome.final_markdown_required)

    async def test_partial_provider_failure_finishes_card_without_buffered_text(self) -> None:
        """终态前失败必须把原卡转红，并丢弃未确认的模型正文。"""
        transport = FakeCapabilityTransport()
        activity = self._activity(transport)
        await activity.start()
        await activity.on_event(
            RunEvent("model_text_delta", 1, {"text": "partial answer"})
        )
        await activity.finish(content=None, failed=True)

        self.assertEqual(len(transport.cards_sent), 1)
        self.assertEqual(len(transport.cards_updated), 1)
        self.assertEqual(transport.cards_updated[0][0], "om_progress_card")
        self.assertIn("MiniClaw · 未完成", repr(transport.cards_updated[0][1]))
        self.assertNotIn("partial answer", repr(transport.cards_updated[0][1]))


if __name__ == "__main__":
    unittest.main()
