"""Telegram/Discord 的确定性、无网络真实纵向回归 fixtures。"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from miniclaw.agent.turn import TurnResult
from miniclaw.bootstrap import initialize_state
from miniclaw.channels.approvals import (
    ApprovalEnvelope,
    ChannelApprovalController,
    approval_delivery_payload,
    parse_approval_delivery_payload,
    text_approval_prompt,
)
from miniclaw.channels.base import (
    ChannelTransportError,
    IgnoredInbound,
    InboundMessage,
    SendReceipt,
)
from miniclaw.channels.delivery import DeliveryWorker, split_message
from miniclaw.channels.discord import DiscordAdapter
from miniclaw.channels.discord_rendering import render_discord_text
from miniclaw.channels.manager import ChannelManager
from miniclaw.channels.supervisor import ChannelRuntime, GatewaySupervisor
from miniclaw.channels.telegram import TelegramAdapter
from miniclaw.config import DiscordConfig, TelegramConfig
from miniclaw.evals.cases import EvalCase
from miniclaw.paths import build_state_paths
from miniclaw.policy.approvals import ApprovalDecision
from miniclaw.policy.modes import PermissionMode, PermissionState
from miniclaw.storage.channels import (
    ChannelIdentityRepository,
    DeliveryRepository,
    InboundEventRepository,
)
from miniclaw.storage.conversations import (
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from miniclaw.storage.database import Database
from miniclaw.storage.tooling import ApprovalPresentation, StoredApproval
from miniclaw.tools.base import ToolContext
from miniclaw.tools.filesystem import ReadFileTool

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


async def run_multi_channel_fixture(case: EvalCase) -> tuple[str, ...]:
    """按 case ID 平台前缀运行有限真实 fixture。"""
    platform = _platform(case)
    fixture = case.channel_fixture
    if fixture in {
        "dm",
        "group_mention",
        "group_no_mention",
        "group_reply",
        "guild_mention",
        "guild_no_mention",
        "thread",
    }:
        return _adapter_evidence(platform, fixture, case.query)
    if fixture == "dedupe":
        return _dedupe_evidence(platform)
    if fixture == "read_tool":
        return await _read_tool_evidence(case)
    if fixture == "approval_approve":
        return await _approval_evidence(case, platform)
    if fixture == "delivery_retry":
        return await _delivery_evidence(platform)
    if fixture == "restart_queued":
        return await _restart_evidence(platform)
    if fixture == "isolation":
        return await _isolation_evidence(platform)
    if fixture == "compact_reply":
        if platform != "discord":
            raise AssertionError("compact_reply is Discord-only")
        return _discord_compact_reply_evidence(case.query)
    raise AssertionError("unknown multi-channel fixture")


def _platform(case: EvalCase) -> str:
    if case.id.startswith("TELEGRAM-"):
        return "telegram"
    if case.id.startswith("DISCORD-"):
        return "discord"
    raise AssertionError("unknown multi-channel case prefix")


def _discord_compact_reply_evidence(query: str) -> tuple[str, ...]:
    """验证 Discord 生产 renderer 把标题压回正文且完整容纳短回答。"""
    rendered = render_discord_text(query, max_chars=2000)
    if rendered is None or rendered != "**核心能力**\n\n**文件与代码**":
        raise AssertionError("Discord compact reply rendering failed")
    return ("heading_compacted", "single_reply_ready")


@dataclass(frozen=True, slots=True)
class _TelegramMessage:
    update_id: int = 100
    message_id: int = 200
    user_id: int = 300
    chat_id: int = 300
    chat_type: str = "private"
    text: str | None = "hello"
    date: datetime = _NOW
    is_bot: bool = False
    is_service: bool = False
    is_edited: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False
    topic_id: int | None = None
    bot_mention_spans: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True, slots=True)
class _DiscordMessage:
    message_id: int = 700
    author_id: int = 300
    channel_id: int = 400
    guild_id: int | None = None
    thread_id: int | None = None
    content: str = "hello"
    created_at: datetime = _NOW
    author_is_bot: bool = False
    webhook_id: int | None = None
    is_system: bool = False
    mentioned_bot: bool = False
    replied_to_bot: bool = False


def _adapter_evidence(platform: str, fixture: str | None, query: str) -> tuple[str, ...]:
    """通过生产 Adapter 验证 DM、群聊寻址、reply、topic 和 thread。"""
    if platform == "telegram":
        adapter = TelegramAdapter(
            TelegramConfig(
                enabled=True,
                owner_user_id=300,
                allowed_user_ids=(300,),
                allowed_chat_ids=(-100123,),
                allow_group_mentions=True,
            ),
            bot_user_id=999,
        )
        if fixture == "dm":
            result = adapter.normalize(_TelegramMessage(text=query))
        elif fixture == "group_mention":
            result = adapter.normalize(
                _TelegramMessage(
                    chat_id=-100123,
                    chat_type="supergroup",
                    text=query,
                    mentioned_bot=True,
                )
            )
        elif fixture == "group_no_mention":
            result = adapter.normalize(
                _TelegramMessage(
                    chat_id=-100123,
                    chat_type="group",
                    text=query,
                )
            )
        elif fixture == "group_reply":
            result = adapter.normalize(
                _TelegramMessage(
                    chat_id=-100123,
                    chat_type="supergroup",
                    text=query,
                    replied_to_bot=True,
                    topic_id=42,
                )
            )
            if not isinstance(result, InboundMessage):
                raise AssertionError("Telegram reply was not admitted")
            if not result.external_conversation_id.endswith(":topic:42"):
                raise AssertionError("Telegram topic identity was lost")
            return ("reply_admitted", "topic_isolated")
        else:
            raise AssertionError("invalid Telegram adapter fixture")
    else:
        adapter = DiscordAdapter(
            DiscordConfig(
                enabled=True,
                owner_user_id=300,
                allowed_user_ids=(300,),
                allowed_guild_ids=(500,),
                allowed_channel_ids=(400,),
                allow_guild_mentions=True,
            ),
            bot_user_id=999,
        )
        if fixture == "dm":
            result = adapter.normalize(_DiscordMessage(content=query))
        elif fixture == "guild_mention":
            result = adapter.normalize(
                _DiscordMessage(guild_id=500, content=query, mentioned_bot=True)
            )
        elif fixture == "guild_no_mention":
            result = adapter.normalize(_DiscordMessage(guild_id=500, content=query))
        elif fixture == "thread":
            result = adapter.normalize(
                _DiscordMessage(
                    guild_id=500,
                    thread_id=600,
                    content=query,
                    mentioned_bot=True,
                )
            )
            if not isinstance(result, InboundMessage):
                raise AssertionError("Discord thread was not admitted")
            if not result.external_conversation_id.endswith(":thread:600"):
                raise AssertionError("Discord thread identity was lost")
            return ("thread_admitted", "thread_isolated")
        else:
            raise AssertionError("invalid Discord adapter fixture")

    if isinstance(result, InboundMessage):
        return ("inbound_admitted", result.chat_type)
    if isinstance(result, IgnoredInbound):
        return ("inbound_ignored", result.reason)
    raise AssertionError("adapter returned an invalid result")


def _inbound(platform: str, message_id: str = "message-1") -> InboundMessage:
    """构造已通过平台 Adapter 的标准消息供 durable 边界使用。"""
    return InboundMessage(
        channel=platform,
        account_id="default",
        event_id=f"event-{message_id}",
        message_id=message_id,
        external_user_id="owner",
        external_conversation_id="conversation",
        chat_type="p2p",
        message_type="text",
        text="hello",
        reply_to_message_id=message_id,
        received_at=_NOW,
    )


def _dedupe_evidence(platform: str) -> tuple[str, ...]:
    """通过真实 SQLite Inbox 验证 message ID 幂等，而非 event ID。"""
    with TemporaryDirectory(prefix="miniclaw-multi-dedupe-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        initialize_state(paths)
        repository = InboundEventRepository(Database(paths.database))
        first = repository.record(_inbound(platform))
        original = _inbound(platform)
        retried = InboundMessage(
            **{
                name: getattr(original, name)
                for name in InboundMessage.__dataclass_fields__
                if name != "event_id"
            },
            event_id="event-retry",
        )
        second = repository.record(retried)
        if not first.inserted or second.inserted:
            raise AssertionError("multi-channel dedupe failed")
    return ("inbox_inserted_once", "message_id_idempotent")


async def _read_tool_evidence(case: EvalCase) -> tuple[str, ...]:
    """用真实 WorkspaceGuard/Policy 接口读取合成 workspace 文件。"""
    with TemporaryDirectory(prefix="miniclaw-multi-tool-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        for relative, content in case.setup_files:
            target = paths.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        tool = ReadFileTool()
        result = await tool.execute(
            ToolContext(owner.id, 1, 1, paths.home, paths.workspace, ()),
            tool.validate({"path": "hello.txt"}),
        )
        sentinel = case.setup_files[0][1].strip() if case.setup_files else ""
        if not result.ok or not sentinel or sentinel not in str(result.data):
            raise AssertionError("read tool was not grounded")
    return ("read_file_succeeded", "workspace_grounded")


@dataclass(slots=True)
class _ApprovalRepository:
    def presentation(self, user_id: int, approval_id: int) -> ApprovalPresentation:
        now = datetime.now(UTC)
        return ApprovalPresentation(
            StoredApproval(
                approval_id,
                user_id,
                1,
                1,
                "write_file",
                "hash",
                None,
                "write_file note.txt",
                "pending",
                now + timedelta(minutes=5),
                None,
                now,
            ),
            (ApprovalDecision.ONCE,),
        )


@dataclass(slots=True)
class _ApprovalService:
    decisions: list[ApprovalDecision] = field(default_factory=list)

    async def continue_approval(
        self,
        user_id: int,
        approval_id: int,
        *,
        decision: ApprovalDecision,
        on_text=None,
        on_event=None,
    ) -> TurnResult:
        del user_id, approval_id, on_text, on_event
        self.decisions.append(decision)
        return TurnResult(1, 1, "done", 0, 0, None, 1, None)


async def _approval_evidence(case: EvalCase, platform: str) -> tuple[str, ...]:
    """验证 v2 envelope round-trip、文本 renderer 和 Owner-only continuation。"""
    owner = "300"
    service = _ApprovalService()
    controller = ChannelApprovalController(
        owner_external_user_id=owner,
        approvals=_ApprovalRepository(),
        service=service,
    )
    envelope = controller.prompt(user_id=1, approval_id=7)
    parsed = parse_approval_delivery_payload(approval_delivery_payload(envelope))
    if not isinstance(parsed, ApprovalEnvelope) or parsed.version != 2:
        raise AssertionError("approval envelope did not round-trip")
    if "/approve 7 once" not in text_approval_prompt(parsed):
        raise AssertionError("text approval prompt was not rendered")
    denied = await controller.handle_text(
        user_id=1,
        actor_external_user_id=f"non-owner-{platform}",
        text=case.query,
    )
    if denied.result is not None or service.decisions:
        raise AssertionError("non-owner approval was accepted")
    accepted = await controller.handle_text(
        user_id=1,
        actor_external_user_id=owner,
        text=case.query,
    )
    if accepted.result is None or service.decisions != [ApprovalDecision.ONCE]:
        raise AssertionError("owner approval was not continued once")
    return ("v2_envelope_parsed", "owner_once", "non_owner_denied")


@dataclass(slots=True)
class _Clock:
    current: datetime = _NOW

    def __call__(self) -> datetime:
        return self.current


@dataclass(slots=True)
class _RetryTransport:
    outcomes: list[SendReceipt | BaseException]
    keys: list[str] = field(default_factory=list)

    async def send(self, message, *, idempotency_key: str) -> SendReceipt:
        del message
        self.keys.append(idempotency_key)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _assistant_message(database: Database, owner_id: int, channel: str, suffix: str) -> int:
    sessions = SessionRepository(database)
    turns = TurnRepository(database)
    session = sessions.get_or_create(owner_id, channel, "default", f"conversation-{suffix}")
    turn = turns.create_with_user_message(session.id, f"inbound-{suffix}", "eval", "q")
    turns.mark_running(turn.id)
    return turns.complete_with_assistant_message(
        turn.id,
        session.id,
        "answer",
        input_tokens=0,
        output_tokens=0,
        provider_request_id=None,
        iterations=1,
        finish_reason="stop",
    ).id


async def _delivery_evidence(platform: str) -> tuple[str, ...]:
    """验证真实 splitter、durable Outbox、平台退避和幂等键复用。"""
    with TemporaryDirectory(prefix="miniclaw-multi-delivery-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        database = Database(paths.database)
        clock = _Clock()
        repository = DeliveryRepository(database, clock=clock)
        contents = split_message("多渠道回复" * 30, max_chars=40)
        parts = repository.create_parts(
            message_id=_assistant_message(database, owner.id, platform, "delivery"),
            channel=platform,
            account_id="default",
            external_conversation_id="conversation",
            reply_to_message_id="reply",
            kind="message",
            contents=contents,
        )
        if len(parts) < 2 or any(len(part.content) > 40 for part in parts):
            raise AssertionError("message splitter exceeded platform bound")
        transport = _RetryTransport(
            [
                ChannelTransportError(
                    f"{platform}_rate_limited",
                    retryable=True,
                    retry_after=1,
                ),
                SendReceipt("sent"),
            ]
        )
        worker = DeliveryWorker(
            transport=transport,
            repository=repository,
            channel=platform,
            account_id="default",
            clock=clock,
            jitter=lambda: 1.0,
            message_max_chars=40,
        )
        await worker.run_once()
        if repository.get(parts[0].id).status != "retry_wait":
            raise AssertionError("delivery did not enter retry_wait")
        clock.current += timedelta(seconds=1)
        await worker.run_once()
        if repository.get(parts[0].id).status != "sent" or len(set(transport.keys)) != 1:
            raise AssertionError("delivery changed idempotency key")
    return ("multipart_bounded", "retry_wait", "idempotency_preserved")


@dataclass(slots=True)
class _ManagerService:
    sessions: SessionRepository
    messages: MessageRepository
    turns: TurnRepository
    calls: int = 0

    async def handle_inbound(self, **values: Any) -> TurnResult:
        self.calls += 1
        session = self.sessions.get_or_create(
            values["user_id"],
            values["channel"],
            values["account_id"],
            values["external_conversation_id"],
        )
        turn = self.turns.create_with_user_message(
            session.id,
            values["inbound_event_id"],
            "eval",
            values["text"],
        )
        self.turns.mark_running(turn.id)
        assistant = self.turns.complete_with_assistant_message(
            turn.id,
            session.id,
            "recovered",
            input_tokens=0,
            output_tokens=0,
            provider_request_id=None,
            iterations=1,
            finish_reason="stop",
        )
        return TurnResult(turn.id, session.id, "recovered", 0, 0, None, assistant.id, None)


async def _restart_evidence(platform: str) -> tuple[str, ...]:
    """仅从 SQLite queued truth 启动新 Manager，验证一次性恢复。"""
    with TemporaryDirectory(prefix="miniclaw-multi-restart-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        database = Database(paths.database)
        inbound = InboundEventRepository(database)
        inbound.record(_inbound(platform, "restart-message"))
        service = _ManagerService(
            SessionRepository(database),
            MessageRepository(database),
            TurnRepository(database),
        )
        manager = ChannelManager(
            owner_id=owner.id,
            owner_external_user_id="owner",
            permission_state=PermissionState(PermissionMode.SAFE),
            service=service,
            sessions=service.sessions,
            messages=service.messages,
            turns=service.turns,
            identities=ChannelIdentityRepository(database),
            inbound=inbound,
            deliveries=DeliveryRepository(database),
            channel=platform,
            account_id="default",
            queue_size=2,
            worker_count=1,
            feeder_interval=0.01,
        )
        await manager.start()
        try:
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()
        completed = inbound.list_by_status(platform, "default", "completed")
        if service.calls != 1 or len(completed) != 1:
            raise AssertionError("queued event was not recovered exactly once")
    return ("queued_recovered", "service_called_once")


@dataclass(slots=True)
class _Runtime:
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _ManagerComponent:
    async def start(self) -> None:
        return None

    async def stop(self, *, drain_timeout: float = 5.0) -> None:
        del drain_timeout


@dataclass(slots=True)
class _DeliveryComponent:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


@dataclass(slots=True)
class _TransportComponent:
    connection_state: str = "connected"

    async def connect(self) -> None:
        self.connection_state = "connected"

    def stop_receiving(self) -> None:
        return None

    async def disconnect(self) -> None:
        self.connection_state = "disconnected"


def _channel_runtime(channel: str) -> ChannelRuntime:
    return ChannelRuntime(
        channel=channel,
        account_id="default",
        manager=_ManagerComponent(),
        delivery=_DeliveryComponent(),
        transport=_TransportComponent(),
    )


async def _isolation_evidence(platform: str) -> tuple[str, ...]:
    """真实 Supervisor 状态隔离，同时验证 peer reply 已进入 durable Outbox。"""
    peer = "discord" if platform == "telegram" else "telegram"
    runtime = _Runtime()
    failed = _channel_runtime(platform)
    healthy = _channel_runtime(peer)
    supervisor = GatewaySupervisor(runtime=runtime, channels=(failed, healthy))
    await supervisor.start(ready=lambda _: None)
    try:
        supervisor.report_degraded(platform, f"{platform}_connection_lost")
        with TemporaryDirectory(prefix="miniclaw-multi-isolation-") as directory:
            paths = build_state_paths(Path(directory) / "state")
            owner = initialize_state(paths).owner
            database = Database(paths.database)
            deliveries = DeliveryRepository(database)
            rows = deliveries.create_parts(
                message_id=_assistant_message(database, owner.id, peer, "peer"),
                channel=peer,
                account_id="default",
                external_conversation_id="peer-conversation",
                reply_to_message_id="peer-reply",
                kind="message",
                contents=("durable answer",),
            )
            if failed.state != "degraded" or healthy.state != "ready":
                raise AssertionError("supervisor did not isolate platform failure")
            if len(rows) != 1 or rows[0].status != "queued":
                raise AssertionError("peer reply was not durable")
    finally:
        await supervisor.shutdown(force_event=asyncio.Event())
    return ("channel_degraded", "peer_ready", "reply_durable")
