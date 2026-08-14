"""Channel 链路的脱敏结构化日志与 durable Audit。"""

import hashlib
import json
import logging
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from lobster0.storage.database import Database, DatabaseError

type TransportState = Literal[
    "connecting",
    "connected",
    "reconnecting",
    "stopping",
    "disconnected",
    "failed",
]
type SupervisorState = Literal["ready", "degraded", "stopping"]
type InboundState = Literal["accepted", "duplicate", "ignored"]
type TurnState = Literal[
    "started",
    "completed",
    "waiting_approval",
    "failed",
    "interrupted",
]
type DeliveryState = Literal[
    "sending",
    "sent",
    "retry_wait",
    "unknown",
    "failed",
    "superseded",
]

_SAFE_DIMENSION = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")
_SAFE_CODE = re.compile(r"[a-z0-9_.-]{1,96}\Z")
_TRANSPORT_STATES = frozenset(
    {"connecting", "connected", "reconnecting", "stopping", "disconnected", "failed"}
)
_SUPERVISOR_STATES = frozenset({"ready", "degraded", "stopping"})
_INBOUND_STATES = frozenset({"accepted", "duplicate", "ignored"})
_TURN_STATES = frozenset(
    {"started", "completed", "waiting_approval", "failed", "interrupted"}
)
_DELIVERY_STATES = frozenset(
    {"sending", "sent", "retry_wait", "unknown", "failed", "superseded"}
)
_RETRY_DECISIONS = frozenset({"none", "retry", "unknown", "terminal", "fallback"})
_APPROVAL_STATES = frozenset({"none", "waiting", "approved", "denied", "expired"})
_CAPABILITIES = frozenset(
    {
        "typing_add",
        "typing_remove",
        "progress_card_create",
        "progress_card_update",
        "typing_start",
        "typing_stop",
        "progress_create",
        "progress_update",
        # 入站图片的下载与缓存。它和 Typing/进度卡同类：失败只让这一轮少一项能力，
        # 不改变权威回复。但它此前完全没有痕迹——图片没进模型时，日志里一片空白，
        # 只有 Owner 在 IM 里看到"我看不到图片"，无从判断断在哪一环。
        "media_resolve",
    }
)


class ChannelObserver:
    """把同一 Channel 链路写成安全 JSON 日志和 SQLite Audit。"""

    def __init__(
        self,
        database: Database,
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定已有数据库；Observer 不接收正文、Secret 或 SDK 原始事件。"""
        self._database = database
        self._logger = logger or logging.getLogger("lobster0.channel")
        self._clock = clock or (lambda: datetime.now(UTC))

    def transport_state(
        self,
        *,
        channel: str,
        account_id: str,
        state: TransportState,
        error_code: str | None = None,
    ) -> None:
        """记录 WebSocket 生命周期和稳定错误码。"""
        if state not in _TRANSPORT_STATES:
            raise ValueError("invalid transport state")
        metadata: dict[str, Any] = {
            "channel": _dimension(channel),
            "account_id": _dimension(account_id),
            "connection_state": state,
        }
        if error_code is not None:
            metadata["error_code"] = _error_code(error_code)
        self._record(f"channel.transport.{state}", metadata)

    def supervisor(
        self,
        *,
        channel: str,
        account_id: str,
        state: SupervisorState,
        error_code: str | None = None,
    ) -> None:
        """记录 pipeline 编排状态；不接收任何外部平台标识。"""
        if state not in _SUPERVISOR_STATES:
            raise ValueError("invalid supervisor state")
        metadata: dict[str, Any] = {
            "channel": _dimension(channel),
            "account_id": _dimension(account_id),
            "supervisor_state": state,
        }
        if error_code is not None:
            metadata["error_code"] = _error_code(error_code)
        self._record(f"channel.supervisor.{state}", metadata)

    def inbound(
        self,
        *,
        channel: str,
        account_id: str,
        external_message_id: str,
        status: InboundState,
        external_conversation_id: str | None = None,
        event_row_id: int | None = None,
        enqueued: bool | None = None,
        reason: str | None = None,
        user_id: int | None = None,
    ) -> None:
        """记录入站 admission/去重；外部标识只生成短哈希。"""
        if status not in _INBOUND_STATES:
            raise ValueError("invalid inbound state")
        metadata = self._chain_metadata(channel, account_id, external_message_id)
        if external_conversation_id:
            metadata["conversation_id_hash"] = _short_hash(external_conversation_id)
        if event_row_id is not None:
            metadata["event_row_id"] = _positive_int(event_row_id, "event_row_id")
        if enqueued is not None:
            metadata["enqueued"] = bool(enqueued)
        if reason is not None:
            metadata["reason"] = _error_code(reason)
        self._record(f"channel.inbound.{status}", metadata, user_id=user_id)

    def turn(
        self,
        *,
        channel: str,
        account_id: str,
        external_message_id: str,
        status: TurnState,
        event_row_id: int | None = None,
        user_id: int | None = None,
        session_id: int | None = None,
        turn_id: int | None = None,
        internal_message_id: int | None = None,
        queue_wait_ms: int | None = None,
        agent_duration_ms: int | None = None,
        tool_count: int | None = None,
        approval_state: str | None = None,
        error_code: str | None = None,
    ) -> None:
        """记录队列到 Agent 终态的内部 ID、耗时、Tool 数和审批状态。"""
        if status not in _TURN_STATES:
            raise ValueError("invalid turn state")
        metadata = self._chain_metadata(channel, account_id, external_message_id)
        for name, value in (
            ("event_row_id", event_row_id),
            ("session_id", session_id),
            ("turn_id", turn_id),
            ("internal_message_id", internal_message_id),
            ("queue_wait_ms", queue_wait_ms),
            ("agent_duration_ms", agent_duration_ms),
            ("tool_count", tool_count),
        ):
            if value is not None:
                metadata[name] = _non_negative_int(value, name)
        if approval_state is not None:
            metadata["approval_state"] = _choice(
                approval_state,
                _APPROVAL_STATES,
                "approval_state",
            )
        if error_code is not None:
            metadata["error_code"] = _error_code(error_code)
        self._record(
            f"channel.turn.{status}",
            metadata,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
        )

    def delivery(
        self,
        *,
        channel: str,
        account_id: str,
        external_message_id: str,
        delivery_id: int,
        status: DeliveryState,
        internal_message_id: int | None = None,
        delivery_duration_ms: int | None = None,
        attempts: int | None = None,
        error_code: str | None = None,
        retry_decision: str | None = None,
        user_id: int | None = None,
        session_id: int | None = None,
        turn_id: int | None = None,
    ) -> None:
        """记录 Outbox claim、尝试数、耗时和稳定恢复决定。"""
        if status not in _DELIVERY_STATES:
            raise ValueError("invalid delivery state")
        metadata = self._chain_metadata(channel, account_id, external_message_id)
        metadata["delivery_id"] = _positive_int(delivery_id, "delivery_id")
        for name, value in (
            ("internal_message_id", internal_message_id),
            ("delivery_duration_ms", delivery_duration_ms),
            ("delivery_attempts", attempts),
        ):
            if value is not None:
                metadata[name] = _non_negative_int(value, name)
        if error_code is not None:
            metadata["error_code"] = _error_code(error_code)
        if retry_decision is not None:
            metadata["retry_decision"] = _choice(
                retry_decision,
                _RETRY_DECISIONS,
                "retry_decision",
            )
        self._record(
            f"channel.delivery.{status}",
            metadata,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
        )

    def capability(
        self,
        *,
        channel: str,
        account_id: str,
        external_message_id: str,
        capability: str,
        error_code: str,
        event_row_id: int | None = None,
        user_id: int | None = None,
        session_id: int | None = None,
        turn_id: int | None = None,
    ) -> None:
        """记录 Typing/进度卡 best-effort 失败，不改变权威回复状态。"""
        metadata = self._chain_metadata(channel, account_id, external_message_id)
        metadata["capability"] = _choice(capability, _CAPABILITIES, "capability")
        metadata["error_code"] = _error_code(error_code)
        if event_row_id is not None:
            metadata["event_row_id"] = _positive_int(event_row_id, "event_row_id")
        self._record(
            "channel.capability.failed",
            metadata,
            user_id=user_id,
            session_id=session_id,
            turn_id=turn_id,
        )

    def media(
        self,
        *,
        channel: str,
        account_id: str,
        external_message_id: str,
        descriptor_count: int,
        resolved_count: int,
        outcome: str,
        error_code: str | None = None,
        user_id: int | None = None,
    ) -> None:
        """记录入站图片从"描述符"到"本地文件"的每一次转换，成功也记。

        ## 为什么成功也要记

        这条链路的失败模式全都是**静默**的：描述符为空、下载被拒、MIME 不符、
        文件超限——每一种的结果都一样，就是"这一轮没有图"，日志里什么都不留。
        2026-08-13 排查"飞书看不到图"时，正是因为只有失败分支没有埋点，无法区分
        "SDK 没给描述符"和"下载失败了"，只能靠反复重启和手工复现去猜。

        记下 ``descriptor_count`` 与 ``resolved_count`` 两个数，就能一眼分辨：

        - ``0 → 0``：SDK 根本没给图片描述符，问题在通道或消息类型；
        - ``N → 0``：拿到了描述符但一张都没落地，问题在下载或过滤；
        - ``N → M``（M < N）：部分被过滤，``error_code`` 说明原因；
        - ``N → N``：正常。

        Args:
            descriptor_count: SDK 给出的图片描述符数量。
            resolved_count: 最终可用的本地图片数量。
            outcome: ``resolved`` / ``empty`` / ``failed``。
            error_code: 稳定错误码；``outcome`` 为 ``resolved`` 时可省略。
        """
        metadata = self._chain_metadata(channel, account_id, external_message_id)
        metadata["descriptor_count"] = _non_negative_int(
            descriptor_count, "descriptor_count"
        )
        metadata["resolved_count"] = _non_negative_int(resolved_count, "resolved_count")
        metadata["outcome"] = _choice(
            outcome, frozenset({"resolved", "empty", "failed"}), "outcome"
        )
        if error_code is not None:
            metadata["error_code"] = _error_code(error_code)
        self._record("channel.media.resolved", metadata, user_id=user_id)

    def tool_count(self, turn_id: int | None) -> int:
        """返回某个内部 Turn 的 ToolRun 数量；未知 Turn 按零处理。"""
        if turn_id is None:
            return 0
        _positive_int(turn_id, "turn_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM tool_runs WHERE turn_id = ?",
                (turn_id,),
            ).fetchone()
        return int(row[0])

    def message_context(self, message_id: int | None) -> tuple[int | None, int | None, int | None]:
        """由内部 Message 反查 user/session/turn，供 Delivery Audit 关联。"""
        if message_id is None:
            return (None, None, None)
        _positive_int(message_id, "message_id")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT s.user_id, m.session_id, m.turn_id
                FROM messages AS m
                JOIN sessions AS s ON s.id = m.session_id
                WHERE m.id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            return (None, None, None)
        return (
            int(row["user_id"]),
            int(row["session_id"]),
            None if row["turn_id"] is None else int(row["turn_id"]),
        )

    @staticmethod
    def correlation_id(channel: str, account_id: str, external_message_id: str) -> str:
        """生成跨重启稳定但不包含外部标识的本地 correlation id。"""
        source = f"lobster0-channel-v1\0{channel}\0{account_id}\0{external_message_id}"
        return f"ch_{hashlib.sha256(source.encode('utf-8')).hexdigest()[:16]}"

    def _chain_metadata(
        self,
        channel: str,
        account_id: str,
        external_message_id: str,
    ) -> dict[str, Any]:
        """建立不含原始外部标识的公共链路字段。"""
        if not external_message_id:
            raise ValueError("external_message_id must not be empty")
        return {
            "channel": _dimension(channel),
            "account_id": _dimension(account_id),
            "correlation_id": self.correlation_id(channel, account_id, external_message_id),
            "message_id_hash": _short_hash(external_message_id),
        }

    def _record(
        self,
        event_type: str,
        metadata: dict[str, Any],
        *,
        user_id: int | None = None,
        session_id: int | None = None,
        turn_id: int | None = None,
    ) -> None:
        """先写 durable Audit，再输出同一份 canonical JSON 运维日志。"""
        timestamp = self._clock().astimezone(UTC).isoformat()
        metadata_json = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        persisted = True
        try:
            with self._database.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_type, user_id, session_id, turn_id,
                        summary, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        user_id,
                        session_id,
                        turn_id,
                        event_type.replace(".", " "),
                        metadata_json,
                        timestamp,
                    ),
                )
        except (DatabaseError, OSError, sqlite3.Error):
            persisted = False
        payload = {
            "timestamp": timestamp,
            "level": "info" if persisted else "error",
            "source": "lobster0.channel",
            "event_type": event_type,
            "audit_persisted": persisted,
            **metadata,
        }
        self._logger.info(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        )


def _short_hash(value: str) -> str:
    """生成只用于诊断关联的 12 字符短哈希。"""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _dimension(value: str) -> str:
    """限制 channel/account 维度，避免把用户输入当日志字段。"""
    if not isinstance(value, str) or _SAFE_DIMENSION.fullmatch(value) is None:
        raise ValueError("invalid channel dimension")
    return value


def _error_code(value: str) -> str:
    """只允许稳定机器码进入日志和 Audit。"""
    if not isinstance(value, str) or _SAFE_CODE.fullmatch(value) is None:
        raise ValueError("invalid stable error code")
    return value


def _choice(value: str, choices: frozenset[str], name: str) -> str:
    """校验有限枚举观测字段。"""
    if value not in choices:
        raise ValueError(f"invalid {name}")
    return value


def _positive_int(value: int, name: str) -> int:
    """校验内部数据库 ID。"""
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value: int, name: str) -> int:
    """校验耗时与计数字段。"""
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value
