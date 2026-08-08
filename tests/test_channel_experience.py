"""平台无关 Typing 与 progress preview 的 best-effort 测试。"""

import unittest
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from miniclaw.agent.events import RunEvent
from miniclaw.channels.base import ChannelTransportError
from miniclaw.channels.experience import (
    ChannelExperience,
    ChannelExperienceTransport,
    ProgressReceipt,
)
from miniclaw.channels.progress import AgentProgress
from miniclaw.storage.channels import InboundEventKey, StoredInboundEvent


@dataclass(slots=True)
class Clock:
    """可推进单调时钟。"""

    value: float = 0.0

    def __call__(self) -> float:
        return self.value


@dataclass(slots=True)
class FakeExperienceTransport:
    """记录平台无关体验调用，并可注入安全失败。"""

    fail_typing: bool = False
    fail_progress: bool = False
    visible_limit: int = 20
    typing_started: list[StoredInboundEvent] = field(default_factory=list)
    typing_stopped: list[str | None] = field(default_factory=list)
    created: list[tuple[StoredInboundEvent, AgentProgress, str]] = field(default_factory=list)
    updated: list[tuple[str, AgentProgress]] = field(default_factory=list)

    async def start_typing(self, event: StoredInboundEvent) -> str | None:
        self.typing_started.append(event)
        if self.fail_typing:
            raise ChannelTransportError("telegram_typing_failed")
        return "opaque-typing-token"

    async def stop_typing(self, token: str | None) -> None:
        self.typing_stopped.append(token)

    async def create_progress(
        self,
        event: StoredInboundEvent,
        progress: AgentProgress,
        *,
        idempotency_key: str,
    ) -> ProgressReceipt:
        self.created.append((event, progress, idempotency_key))
        if self.fail_progress:
            raise ChannelTransportError("telegram_progress_failed")
        return ProgressReceipt(
            "progress-message",
            min(len(progress.final_answer), self.visible_limit),
        )

    async def update_progress(
        self,
        platform_message_id: str,
        progress: AgentProgress,
    ) -> ProgressReceipt:
        self.updated.append((platform_message_id, progress))
        if self.fail_progress:
            raise ChannelTransportError("telegram_progress_failed")
        return ProgressReceipt(
            platform_message_id,
            min(len(progress.final_answer), self.visible_limit),
        )


@dataclass(slots=True)
class Observer:
    """记录脱敏 capability failure。"""

    events: list[dict[str, Any]] = field(default_factory=list)

    def capability(self, **values: Any) -> None:
        self.events.append(values)


class ChannelExperienceTest(unittest.IsolatedAsyncioTestCase):
    """验证通用 Experience 不依赖 Card/Reaction 等平台术语。"""

    def setUp(self) -> None:
        self.clock = Clock()
        self.event = StoredInboundEvent(
            key=InboundEventKey("telegram", "default", "chat:1:message:2"),
            event_id="update:3",
            external_user_id="1",
            external_conversation_id="chat:1",
            chat_type="p2p",
            message_type="text",
            content="private question",
            reply_to_message_id="chat:1:message:2",
            session_id=1,
            status="running",
            attempts=1,
            last_error_code=None,
            received_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )

    def _activity(
        self,
        transport: FakeExperienceTransport,
        *,
        observer: Observer | None = None,
        progress_is_final: bool = False,
    ):
        experience = ChannelExperience(
            transport=transport,
            progress_enabled=True,
            progress_is_final=progress_is_final,
            update_interval=0.5,
            max_visible_chars=20,
            clock=self.clock,
            observer=observer,
        )
        return experience.activity(self.event)

    async def test_protocol_and_full_success_lifecycle(self) -> None:
        """Typing、公开 delta、限频 preview 和 completed finish 应按意图调用。"""
        transport = FakeExperienceTransport()
        self.assertIsInstance(transport, ChannelExperienceTransport)
        activity = self._activity(transport)

        await activity.start()
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "你"}))
        await activity.on_event(RunEvent("model_reasoning", 1, {"text": "hidden"}))
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "好"}))
        self.clock.value = 0.6
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "！"}))
        outcome = await activity.finish(content="你好！", failed=False)

        self.assertEqual(transport.typing_started, [self.event])
        self.assertEqual(transport.typing_stopped, ["opaque-typing-token"])
        self.assertEqual(len(transport.created), 1)
        self.assertNotIn("hidden", repr((transport.created, transport.updated)))
        self.assertEqual(transport.updated[-1][0], "progress-message")
        self.assertEqual(transport.updated[-1][1].final_answer, "你好！")
        self.assertEqual(transport.updated[-1][1].status, "completed")
        self.assertTrue(outcome.progress_created)
        self.assertFalse(outcome.progress_failed)
        self.assertTrue(outcome.final_delivery_required)

    async def test_completed_final_progress_replaces_text_delivery(self) -> None:
        """平台把 progress 作为终态时，成功完成后不再要求重复文本。"""
        transport = FakeExperienceTransport()
        activity = self._activity(transport, progress_is_final=True)

        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "partial"}))
        outcome = await activity.finish(content="final answer", failed=False)

        self.assertFalse(outcome.final_delivery_required)
        self.assertEqual(transport.created[-1][1].final_answer, "final answer")
        self.assertEqual(transport.created[-1][1].status, "completed")

    async def test_final_card_starts_on_tool_started_and_shows_safe_trace(self) -> None:
        """飞书式终态卡在 Tool 真正执行时建立，并在完成后更新同一卡片。"""
        transport = FakeExperienceTransport(visible_limit=100)
        activity = self._activity(transport, progress_is_final=True)
        await activity.on_event(
            RunEvent(
                "tool_requested",
                1,
                {
                    "call_id": "call_1",
                    "tool_name": "read_file",
                    "arguments": {"path": "README.md", "content": "secret"},
                },
            )
        )
        self.assertEqual(transport.created, [])

        await activity.on_event(
            RunEvent("tool_started", 1, {"call_id": "call_1", "tool_name": "read_file"})
        )
        await activity.on_event(
            RunEvent(
                "tool_finished",
                1,
                {
                    "call_id": "call_1",
                    "tool_name": "read_file",
                    "status": "succeeded",
                    "preview": "private file content",
                },
            )
        )
        progress = activity.finalize(content="done", failed=False)
        outcome = await activity.finish(content="done", failed=False, progress=progress)

        self.assertEqual(len(transport.created), 1)
        self.assertEqual(transport.created[0][1].steps[-1].status, "running")
        self.assertEqual(transport.updated[-1][1].steps[-1].status, "succeeded")
        self.assertNotIn("secret", repr((transport.created, transport.updated)))
        self.assertNotIn("private file content", repr((transport.created, transport.updated)))
        self.assertFalse(outcome.final_delivery_required)

    async def test_final_progress_at_visible_limit_needs_no_tail_reply(self) -> None:
        """完整正文刚好填满卡片时仍由单张卡片承载，不产生空的后续回复。"""
        transport = FakeExperienceTransport()
        activity = self._activity(transport, progress_is_final=True)
        content = "12345678901234567890"

        outcome = await activity.finish(content=content, failed=False)

        self.assertFalse(outcome.final_delivery_required)
        self.assertEqual(outcome.final_delivery_offset, 20)
        self.assertIsNone(outcome.final_reply_to_message_id)
        self.assertEqual(transport.created[-1][1].final_answer, content)

    async def test_final_progress_overflow_requires_only_tail_reply(self) -> None:
        """超出卡片上限的正文必须返回精确后缀偏移和卡片回复目标。"""
        transport = FakeExperienceTransport()
        activity = self._activity(transport, progress_is_final=True)
        content = "12345678901234567890TAIL"

        outcome = await activity.finish(content=content, failed=False)

        self.assertTrue(outcome.final_delivery_required)
        self.assertEqual(outcome.final_delivery_offset, 20)
        self.assertEqual(outcome.final_reply_to_message_id, "progress-message")
        self.assertEqual(transport.created[-1][1].final_answer, content)

    async def test_final_progress_failure_still_requires_text_fallback(self) -> None:
        """终态卡片失败时必须保留普通文本 Outbox 兜底。"""
        transport = FakeExperienceTransport()
        activity = self._activity(transport, progress_is_final=True)
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "partial"}))
        transport.fail_progress = True

        outcome = await activity.finish(content="final answer", failed=False)

        self.assertTrue(outcome.progress_failed)
        self.assertTrue(outcome.final_delivery_required)

    async def test_final_progress_is_created_at_finish_without_stream_delta(self) -> None:
        """Provider 不发送 delta 时也应创建 completed card，而不是退回双路径。"""
        transport = FakeExperienceTransport()
        activity = self._activity(transport, progress_is_final=True)

        outcome = await activity.finish(content="final answer", failed=False)

        self.assertFalse(outcome.final_delivery_required)
        self.assertEqual(transport.created[0][1].final_answer, "final answer")
        self.assertEqual(transport.updated, [])

    async def test_final_progress_waits_for_terminal_result_before_card_creation(self) -> None:
        """终态卡必须先确认不是 waiting Approval，避免 preview 与审批形成双卡。"""
        transport = FakeExperienceTransport()
        activity = self._activity(transport, progress_is_final=True)

        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "checking"}))
        self.assertEqual(transport.created, [])

        outcome = await activity.finish(content=None, failed=True)

        self.assertFalse(outcome.progress_created)
        self.assertEqual(transport.created, [])
        self.assertEqual(transport.updated, [])

    async def test_failures_and_finish_are_contained_and_idempotent(self) -> None:
        """体验失败只产生稳定短码，重复 finish 不重复清理或改变最终 Delivery。"""
        observer = Observer()
        transport = FakeExperienceTransport(fail_typing=True, fail_progress=True)
        activity = self._activity(transport, observer=observer)

        await activity.start()
        await activity.on_event(RunEvent("model_text_delta", 1, {"text": "partial"}))
        first = await activity.finish(content=None, failed=True)
        second = await activity.finish(content="ignored", failed=False)

        self.assertEqual(first, second)
        self.assertTrue(first.progress_failed)
        self.assertEqual(transport.typing_stopped, [None])
        self.assertEqual(
            [event["error_code"] for event in observer.events],
            ["telegram_typing_failed", "telegram_progress_failed"],
        )
        self.assertNotIn("private question", repr(observer.events))

    async def test_each_activity_has_private_bounded_state(self) -> None:
        """两个 Turn 不共享 preview text，单条可见缓存不能超过配置上限。"""
        transport = FakeExperienceTransport()
        experience = ChannelExperience(
            transport=transport,
            progress_enabled=True,
            update_interval=0.5,
            max_visible_chars=5,
            clock=self.clock,
        )
        first = experience.activity(self.event)
        second = experience.activity(self.event)

        await first.on_event(RunEvent("model_text_delta", 1, {"text": "abcdefgh"}))
        await second.on_event(RunEvent("model_text_delta", 2, {"text": "xy"}))

        self.assertEqual(transport.created[0][1].public_text, "abcde")
        self.assertEqual(transport.created[1][1].public_text, "xy")
        self.assertNotEqual(first.idempotency_key, "chat:1:message:2")


if __name__ == "__main__":
    unittest.main()
