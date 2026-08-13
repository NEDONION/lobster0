"""Phase 4 Feishu Channel 的确定性、无网络场景回归 runner。"""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any

from lobster0.agent.turn import TurnResult
from lobster0.bootstrap import initialize_state
from lobster0.channels.approvals import (
    ApprovalEnvelope,
    ChannelApprovalController,
    approval_delivery_payload,
)
from lobster0.channels.base import (
    ChannelTransportError,
    IgnoredInbound,
    InboundMessage,
    SendReceipt,
)
from lobster0.channels.delivery import DeliveryWorker
from lobster0.channels.feishu import FeishuAdapter, FeishuTransport
from lobster0.channels.manager import ChannelManager
from lobster0.config import FeishuConfig
from lobster0.evals.cases import EvalCase
from lobster0.paths import build_state_paths
from lobster0.policy.approvals import ApprovalDecision
from lobster0.policy.modes import PermissionMode, PermissionState
from lobster0.storage.channels import (
    ChannelIdentityRepository,
    DeliveryRepository,
    InboundEventRepository,
)
from lobster0.storage.conversations import (
    MessageRepository,
    SessionRepository,
    TurnRepository,
)
from lobster0.storage.database import Database
from lobster0.storage.tooling import ApprovalPresentation, StoredApproval
from lobster0.tools.base import ToolContext
from lobster0.tools.filesystem import ReadFileTool


@dataclass(frozen=True, slots=True)
class ChannelEvalCaseResult:
    """保存单条 Channel case 的稳定证据和短失败码。"""

    case_id: str
    passed: bool
    duration_ms: int
    failures: tuple[str, ...]
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ChannelEvalSuiteResult:
    """汇总十二条 Channel 回归结果。"""

    total: int
    passed: int
    failed: int
    duration_ms: int
    cases: tuple[ChannelEvalCaseResult, ...]


async def run_channel_case(case: EvalCase) -> ChannelEvalCaseResult:
    """运行一个显式 fixture，并与 JSONL 的稳定证据精确比较。"""
    started = time.monotonic()
    failures: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    if "channel" not in case.layers or case.channel_fixture is None:
        failures = ("not_channel_case",)
    else:
        try:
            evidence = await _run_fixture(case)
        except Exception:  # noqa: BLE001 - 回归输出只允许稳定短码
            failures = ("execution_error",)
        if not failures and evidence != case.expected.channel_evidence:
            failures = ("evidence_mismatch",)
    return ChannelEvalCaseResult(
        case_id=case.id,
        passed=not failures,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        failures=failures,
        evidence=evidence,
    )


async def run_channel_suite(cases: tuple[EvalCase, ...]) -> ChannelEvalSuiteResult:
    """顺序执行 Channel cases，使 SQLite 和输出顺序完全确定。"""
    started = time.monotonic()
    results = tuple([await run_channel_case(case) for case in cases])
    passed = sum(result.passed for result in results)
    return ChannelEvalSuiteResult(
        total=len(results),
        passed=passed,
        failed=len(results) - passed,
        duration_ms=max(0, round((time.monotonic() - started) * 1000)),
        cases=results,
    )


async def _run_fixture(case: EvalCase) -> tuple[str, ...]:
    """把有限 fixture 名映射到真实 Adapter/SQLite/Worker 纵切。"""
    if case.id.startswith(("TELEGRAM-", "DISCORD-")):
        from lobster0.evals.multi_channel import run_multi_channel_fixture

        return await run_multi_channel_fixture(case)
    fixture = case.channel_fixture
    if fixture == "dm":
        result = _adapter().normalize(_MessageView(body_text=case.query))
        if not isinstance(result, InboundMessage):
            raise AssertionError("DM was not admitted")
        return ("inbound_admitted", result.chat_type)
    if fixture == "group_mention":
        result = _adapter().normalize(
            _MessageView(chat_type="group", mentioned_bot=True, body_text=case.query)
        )
        if not isinstance(result, InboundMessage):
            raise AssertionError("mentioned group message was not admitted")
        return ("inbound_admitted", result.chat_type)
    if fixture == "group_no_mention":
        result = _adapter().normalize(
            _MessageView(chat_type="group", mentioned_bot=False, body_text=case.query)
        )
        if not isinstance(result, IgnoredInbound):
            raise AssertionError("unmentioned group message was admitted")
        return ("inbound_ignored", result.reason)
    if fixture == "dedupe":
        return _dedupe_evidence()
    if fixture == "read_tool":
        return await _read_tool_evidence(case)
    if fixture in {"approval_approve", "approval_deny"}:
        return await _approval_evidence(case, approve=fixture == "approval_approve")
    if fixture == "restart_queued":
        return await _restart_queued_evidence()
    if fixture == "restart_running":
        return await _restart_running_evidence()
    if fixture == "delivery_retry":
        return await _delivery_retry_evidence()
    if fixture == "card_fallback":
        return await _card_fallback_evidence()
    if fixture == "reconnect":
        return await _reconnect_evidence()
    raise AssertionError("unknown channel fixture")


@dataclass(frozen=True, slots=True)
class _MessageView:
    """FeishuAdapter fixture 使用的有限官方消息视图。"""

    event_id: str = "evt_eval"
    message_id: str = "om_eval"
    chat_id: str = "oc_eval"
    chat_type: str = "p2p"
    sender_id: str = "ou_owner"
    sender_type: str | None = "user"
    sender_is_bot: bool = False
    mentioned_bot: bool = False
    body_text: str = "hello"
    raw_content_type: str = "text"
    parent_message_id: str = ""
    # 与 _OfficialMessageView 同名：只带图片描述符，本地路径由 Transport 后补。
    image_descriptors: tuple = ()
    create_time: datetime = datetime(2026, 8, 8, tzinfo=UTC)


def _feishu_config() -> FeishuConfig:
    """返回所有 Channel fixture 共用的严格配置。"""
    return FeishuConfig(
        enabled=True,
        owner_open_id="ou_owner",
        allowed_open_ids=("ou_owner",),
        allowed_chat_ids=("oc_eval",),
        allow_group_mentions=True,
        message_max_chars=1000,
    )


def _adapter() -> FeishuAdapter:
    """构造严格 Adapter。"""
    return FeishuAdapter(_feishu_config())


def _inbound(message_id: str = "om_eval", text: str = "hello") -> InboundMessage:
    """构造已经通过 Adapter 的标准入站消息。"""
    return InboundMessage(
        channel="feishu",
        account_id="default",
        event_id=f"evt_{message_id}",
        message_id=message_id,
        external_user_id="ou_owner",
        external_conversation_id="oc_eval",
        chat_type="p2p",
        message_type="text",
        text=text,
        reply_to_message_id=message_id,
        received_at=datetime(2026, 8, 8, tzinfo=UTC),
    )


def _dedupe_evidence() -> tuple[str, ...]:
    """验证 message_id 是事实幂等键，event_id 变化不重复插入。"""
    with TemporaryDirectory(prefix="lobster0-channel-eval-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        initialize_state(paths)
        repository = InboundEventRepository(Database(paths.database))
        first = repository.record(_inbound())
        retried = InboundMessage(
            **{
                field: getattr(_inbound(), field)
                for field in InboundMessage.__dataclass_fields__
                if field != "event_id"
            },
            event_id="evt_retry",
        )
        second = repository.record(retried)
        if not first.inserted or second.inserted:
            raise AssertionError("message dedupe failed")
    return ("inbox_inserted_once", "message_id_idempotent")


async def _read_tool_evidence(case: EvalCase) -> tuple[str, ...]:
    """通过真实 WorkspaceGuard 和 ReadFileTool 读取合成文件。"""
    with TemporaryDirectory(prefix="lobster0-channel-tool-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        initialized = initialize_state(paths)
        for relative, content in case.setup_files:
            target = paths.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        tool = ReadFileTool()
        arguments = tool.validate({"path": "hello.txt"})
        result = await tool.execute(
            ToolContext(
                initialized.owner.id,
                1,
                1,
                paths.home,
                paths.workspace,
                (),
            ),
            arguments,
        )
        if not result.ok or "LOBSTER0-FEISHU-GROUNDED" not in str(result.data):
            raise AssertionError("read tool was not grounded")
    return ("read_file_succeeded", "workspace_grounded")


@dataclass(slots=True)
class _ApprovalRepository:
    """提供 Core 已允许的 once 模式展示。"""

    def presentation(self, user_id: int, approval_id: int) -> ApprovalPresentation:
        approval = StoredApproval(
            approval_id,
            user_id,
            1,
            1,
            "write_file",
            "hash",
            None,
            "write_file note.txt",
            "pending",
            datetime.now(UTC) + timedelta(minutes=5),
            None,
            datetime.now(UTC),
        )
        return ApprovalPresentation(approval, (ApprovalDecision.ONCE,))


@dataclass(slots=True)
class _ApprovalService:
    """记录 Controller 解析后交给 Core 的 decision，不调用模型。"""

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


async def _approval_evidence(case: EvalCase, *, approve: bool) -> tuple[str, ...]:
    """验证文本控制命令绕过模型并保留 Core decision 枚举。"""
    service = _ApprovalService()
    controller = ChannelApprovalController(
        owner_external_user_id="ou_owner",
        approvals=_ApprovalRepository(),
        service=service,
    )
    outcome = await controller.handle_text(
        user_id=1,
        actor_external_user_id="ou_owner",
        text=case.query,
    )
    expected = ApprovalDecision.ONCE if approve else ApprovalDecision.DENY
    if not outcome.handled or service.decisions != [expected]:
        raise AssertionError("approval command did not bypass model")
    return ("command_bypassed_model", f"decision_{expected.value}")


@dataclass(slots=True)
class _ManagerService:
    """为 restart fixture 持久化一个最小真实 Turn。"""

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
            "eval-model",
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


def _manager(
    database: Database,
    owner_id: int,
    service: _ManagerService,
) -> ChannelManager:
    """构造使用真实 SQLite Repository 的单 Worker Manager。"""
    return ChannelManager(
        owner_id=owner_id,
        owner_external_user_id="ou_owner",
        permission_state=PermissionState(PermissionMode.SAFE),
        service=service,
        sessions=service.sessions,
        messages=service.messages,
        turns=service.turns,
        identities=ChannelIdentityRepository(database),
        inbound=InboundEventRepository(database),
        deliveries=DeliveryRepository(database),
        channel="feishu",
        account_id="default",
        queue_size=2,
        worker_count=1,
        feeder_interval=0.01,
    )


async def _restart_queued_evidence() -> tuple[str, ...]:
    """验证仅存在 SQLite 的 queued event 会被新 Manager feeder 恢复。"""
    with TemporaryDirectory(prefix="lobster0-channel-restart-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        database = Database(paths.database)
        inbound = InboundEventRepository(database)
        inbound.record(_inbound("om_restart_queued"))
        service = _ManagerService(
            SessionRepository(database),
            MessageRepository(database),
            TurnRepository(database),
        )
        manager = _manager(database, owner.id, service)
        await manager.start()
        try:
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()
        completed = inbound.list_by_status("feishu", "default", "completed")
        if service.calls != 1 or len(completed) != 1:
            raise AssertionError("queued event was not recovered exactly once")
    return ("queued_recovered", "service_called_once")


async def _restart_running_evidence() -> tuple[str, ...]:
    """验证已绑定 running Turn 只中断，不重新调用 Service。"""
    with TemporaryDirectory(prefix="lobster0-channel-running-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        database = Database(paths.database)
        inbound = InboundEventRepository(database)
        event = inbound.record(_inbound("om_restart_running")).event
        claimed = inbound.claim(event.key)
        sessions = SessionRepository(database)
        messages = MessageRepository(database)
        turns = TurnRepository(database)
        session = sessions.get_or_create(owner.id, "feishu", "default", "oc_eval")
        assert claimed is not None
        inbound.bind_session(claimed.key, session.id)
        turn = turns.create_with_user_message(
            session.id,
            "om_restart_running",
            "eval-model",
            "do not replay",
        )
        turns.mark_running(turn.id)
        service = _ManagerService(sessions, messages, turns)
        manager = _manager(database, owner.id, service)
        await manager.start()
        try:
            await manager.wait_idle(timeout=2)
        finally:
            await manager.stop()
        if service.calls or turns.get(turn.id).status != "failed":
            raise AssertionError("running Turn was replayed")
    return ("running_not_replayed", "turn_interrupted")


@dataclass(slots=True)
class _MutableClock:
    """Delivery fixtures 使用的 UTC 时钟。"""

    current: datetime = datetime(2026, 8, 8, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.current


@dataclass(slots=True)
class _DeliveryTransport:
    """分别预设 Markdown 与 Card 发送结果。"""

    outcomes: list[SendReceipt | BaseException]
    card_outcomes: list[SendReceipt | BaseException] = field(default_factory=list)
    keys: list[str] = field(default_factory=list)

    async def connect(self) -> None:
        return None

    async def disconnect(self) -> None:
        return None

    async def send(self, message, *, idempotency_key: str) -> SendReceipt:
        del message
        self.keys.append(idempotency_key)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def send_card(self, **values: Any) -> SendReceipt:
        self.keys.append(values["idempotency_key"])
        outcome = self.card_outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _assistant_message(database: Database, owner_id: int, suffix: str) -> int:
    """创建 Delivery 外键需要的已完成 Assistant Message。"""
    sessions = SessionRepository(database)
    turns = TurnRepository(database)
    session = sessions.get_or_create(owner_id, "feishu", "default", f"oc_{suffix}")
    turn = turns.create_with_user_message(session.id, f"om_{suffix}", "eval", "q")
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


async def _delivery_retry_evidence() -> tuple[str, ...]:
    """验证 retry_wait 到期后复用相同幂等键。"""
    with TemporaryDirectory(prefix="lobster0-channel-delivery-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        database = Database(paths.database)
        clock = _MutableClock()
        repository = DeliveryRepository(database, clock=clock)
        delivery = repository.create_parts(
            message_id=_assistant_message(database, owner.id, "retry"),
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_eval",
            reply_to_message_id="om_retry",
            kind="message",
            contents=("answer",),
        )[0]
        transport = _DeliveryTransport(
            [
                ChannelTransportError("feishu_rate_limited", retryable=True),
                SendReceipt("om_sent"),
            ]
        )
        worker = DeliveryWorker(
            transport=transport,
            repository=repository,
            channel="feishu",
            account_id="default",
            base_delay=1,
            jitter=lambda: 1.0,
            clock=clock,
        )
        await worker.run_once()
        if repository.get(delivery.id).status != "retry_wait":
            raise AssertionError("delivery did not wait")
        clock.current += timedelta(seconds=1)
        await worker.run_once()
        if len(set(transport.keys)) != 1:
            raise AssertionError("delivery changed idempotency key")
    return ("retry_wait", "idempotency_preserved")


async def _card_fallback_evidence() -> tuple[str, ...]:
    """验证 durable Approval card 失败后 supersede 并发送 Markdown。"""
    with TemporaryDirectory(prefix="lobster0-channel-card-") as directory:
        paths = build_state_paths(Path(directory) / "state")
        owner = initialize_state(paths).owner
        database = Database(paths.database)
        repository = DeliveryRepository(database)
        message_id = _assistant_message(database, owner.id, "card")
        payload = approval_delivery_payload(
            ApprovalEnvelope(
                version=2,
                approval_id=7,
                tool_name="write_file",
                summary="write_file note.txt",
                decisions=(ApprovalDecision.ONCE, ApprovalDecision.DENY),
                expires_at="2026-08-08T09:00:00+00:00",
                fallback_text="/deny 7",
            )
        )
        card = repository.create_parts(
            message_id=message_id,
            channel="feishu",
            account_id="default",
            external_conversation_id="oc_eval",
            reply_to_message_id="om_card",
            kind="approval",
            contents=(payload,),
        )[0]
        transport = _DeliveryTransport(
            [SendReceipt("om_fallback")],
            [ChannelTransportError("feishu_permission_denied")],
        )
        worker = DeliveryWorker(
            transport=transport,
            repository=repository,
            channel="feishu",
            account_id="default",
        )
        await worker.run_once()
        await worker.run_once()
        if repository.get(card.id).status != "superseded":
            raise AssertionError("card was not superseded")
        with database.connect_read_only() as connection:
            status = connection.execute(
                "SELECT status FROM deliveries WHERE message_id = ? "
                "AND delivery_kind = 'message'",
                (message_id,),
            ).fetchone()[0]
        if status != "sent":
            raise AssertionError("fallback was not sent")
    return ("card_superseded", "markdown_sent")


class _FakeSdkChannel:
    """FeishuTransport reconnect fixture 的最小 official facade。"""

    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}
        self.connect_count = 0

    def on(self, name: str, handler: Any):
        self.handlers[name] = handler
        return lambda: self.handlers.pop(name, None)

    async def connect(self) -> None:
        self.connect_count += 1

    async def disconnect(self) -> None:
        return None


class _FakeSdk:
    """接受 Transport config 并复用同一个 facade。"""

    FEISHU_DOMAIN = "https://open.feishu.cn"
    LARK_DOMAIN = "https://open.larksuite.com"

    def __init__(self) -> None:
        self.channel = _FakeSdkChannel()

    def __getattr__(self, name: str):
        if name in {
            "SecurityConfig",
            "PolicyConfig",
            "InboundConfig",
            "TransportConfig",
            "MediaCacheConfig",
            # media_cache 只能经由 ChannelConfig 传给真实 SDK；
            # 见 tests/test_feishu_sdk_contract.py。
            "ChannelConfig",
        }:
            return lambda **values: SimpleNamespace(**values)
        if name == "FeishuChannel":
            return lambda **values: self.channel
        if name == "SendOpts":
            return lambda **values: SimpleNamespace(**values)
        raise AttributeError(name)


async def _reconnect_evidence() -> tuple[str, ...]:
    """验证同一 Transport 断开再连接时重新注册消息 handler。"""
    sdk = _FakeSdk()

    async def receive(message: InboundMessage) -> None:
        del message

    transport = FeishuTransport(
        _feishu_config(),
        app_id="cli_eval",
        app_secret="synthetic",
        on_inbound=receive,
        sdk=sdk,
    )
    await transport.connect()
    await transport.disconnect()
    await transport.connect()
    restored = "message" in sdk.channel.handlers
    await transport.disconnect()
    if sdk.channel.connect_count != 2 or not restored:
        raise AssertionError("transport handlers were not restored")
    return ("connected_twice", "handlers_restored")
