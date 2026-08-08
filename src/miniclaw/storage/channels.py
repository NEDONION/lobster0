"""Channel identity、持久化 Inbox 与 Delivery Outbox Repository。"""

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from miniclaw.channels.base import DeliveryKind, InboundMessage
from miniclaw.storage.database import Database

type InboundStatus = Literal["queued", "running", "completed", "failed", "ignored"]
type DeliveryStatus = Literal[
    "queued",
    "sending",
    "retry_wait",
    "sent",
    "failed",
    "unknown",
    "superseded",
]


class ChannelStateError(RuntimeError):
    """表示 Channel 身份、Inbox 或 Delivery 状态冲突。"""


@dataclass(frozen=True, slots=True, repr=False)
class ChannelIdentity:
    """保存一个外部平台身份到本地 Owner 的稳定映射。"""

    id: int
    user_id: int
    channel: str
    account_id: str
    external_user_id: str
    created_at: datetime

    def __repr__(self) -> str:
        """隐藏完整外部用户标识。"""
        return (
            "ChannelIdentity("
            f"id={self.id}, user_id={self.user_id}, channel={self.channel!r}, "
            f"account_id={self.account_id!r})"
        )


@dataclass(frozen=True, slots=True)
class InboundEventKey:
    """唯一定位一条已持久化平台消息。"""

    channel: str
    account_id: str
    external_message_id: str


@dataclass(frozen=True, slots=True, repr=False)
class StoredInboundEvent:
    """保存一条可恢复的标准化 Channel 入站消息。"""

    key: InboundEventKey
    event_id: str
    external_user_id: str
    external_conversation_id: str
    chat_type: str
    message_type: str
    content: str
    reply_to_message_id: str
    session_id: int | None
    status: InboundStatus
    attempts: int
    last_error_code: str | None
    received_at: datetime
    updated_at: datetime

    @property
    def external_message_id(self) -> str:
        """返回用于幂等和 Turn inbound ID 的平台消息标识。"""
        return self.key.external_message_id

    def __repr__(self) -> str:
        """隐藏正文和完整平台标识。"""
        return (
            "StoredInboundEvent("
            f"channel={self.key.channel!r}, account_id={self.key.account_id!r}, "
            f"status={self.status!r}, attempts={self.attempts})"
        )


@dataclass(frozen=True, slots=True)
class InboundRecordResult:
    """区分首次持久化和平台重复投递。"""

    event: StoredInboundEvent
    inserted: bool


@dataclass(frozen=True, slots=True, repr=False)
class StoredDelivery:
    """保存一个可单独恢复和重试的出站分片。"""

    id: int
    message_id: int | None
    channel: str
    account_id: str
    external_conversation_id: str
    reply_to_message_id: str
    delivery_kind: str
    part_index: int
    content: str
    content_hash: str
    idempotency_key: str
    platform_message_id: str | None
    status: DeliveryStatus
    attempts: int
    last_error_code: str | None
    last_error_detail: str | None
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime | None
    sent_at: datetime | None

    def __repr__(self) -> str:
        """隐藏正文、目标和平台标识。"""
        return (
            "StoredDelivery("
            f"id={self.id}, channel={self.channel!r}, account_id={self.account_id!r}, "
            f"kind={self.delivery_kind!r}, part_index={self.part_index}, "
            f"status={self.status!r}, attempts={self.attempts})"
        )


class ChannelIdentityRepository:
    """管理外部 Channel 身份到单 Owner 的不可变映射。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or _utc_now

    def get_or_create(
        self,
        user_id: int,
        channel: str,
        account_id: str,
        external_user_id: str,
    ) -> ChannelIdentity:
        """创建或返回平台身份；已绑定其他用户时失败关闭。"""
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, user_id, channel, account_id, external_user_id, created_at
                FROM channel_identities
                WHERE channel = ? AND account_id = ? AND external_user_id = ?
                """,
                (channel, account_id, external_user_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO channel_identities (
                        user_id, channel, account_id, external_user_id, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        channel,
                        account_id,
                        external_user_id,
                        self._clock().isoformat(),
                    ),
                )
                identity_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])
                row = connection.execute(
                    """
                    SELECT id, user_id, channel, account_id, external_user_id, created_at
                    FROM channel_identities WHERE id = ?
                    """,
                    (identity_id,),
                ).fetchone()
            if int(row["user_id"]) != user_id:
                raise ChannelStateError("identity_owner_conflict")
        return _identity_from_row(row)


class InboundEventRepository:
    """持久化、幂等 claim 并结算标准化入站消息。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or _utc_now

    def record(self, message: InboundMessage) -> InboundRecordResult:
        """先按 message ID 幂等落库，不用 event ID 代替消息幂等。"""
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = self._find_by_message(
                connection,
                message.channel,
                message.account_id,
                message.message_id,
            )
            if duplicate is not None:
                return InboundRecordResult(_inbound_from_row(duplicate), False)
            event_conflict = connection.execute(
                """
                SELECT external_message_id FROM processed_events
                WHERE channel = ? AND account_id = ? AND event_id = ?
                """,
                (message.channel, message.account_id, message.event_id),
            ).fetchone()
            if event_conflict is not None:
                raise ChannelStateError("event_id_conflict")
            now = self._clock().isoformat()
            connection.execute(
                """
                INSERT INTO processed_events (
                    channel, account_id, event_id, external_message_id,
                    session_id, received_at, external_user_id,
                    external_conversation_id, chat_type, message_type, content,
                    reply_to_message_id, status, attempts, last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, NULL, ?)
                """,
                (
                    message.channel,
                    message.account_id,
                    message.event_id,
                    message.message_id,
                    message.received_at.isoformat(),
                    message.external_user_id,
                    message.external_conversation_id,
                    message.chat_type,
                    message.message_type,
                    message.text,
                    message.reply_to_message_id,
                    now,
                ),
            )
            row = self._find_by_message(
                connection,
                message.channel,
                message.account_id,
                message.message_id,
            )
        if row is None:
            raise ChannelStateError("inbound_insert_failed")
        return InboundRecordResult(_inbound_from_row(row), True)

    def claim_next(self, channel: str, account_id: str) -> StoredInboundEvent | None:
        """原子 claim 最早 queued 事件，并增加一次处理尝试。"""
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT rowid AS storage_rowid, * FROM processed_events
                WHERE channel = ? AND account_id = ? AND status = 'queued'
                ORDER BY received_at, storage_rowid
                LIMIT 1
                """,
                (channel, account_id),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE processed_events
                SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE channel = ? AND account_id = ? AND external_message_id = ?
                  AND status = 'queued'
                """,
                (
                    self._clock().isoformat(),
                    channel,
                    account_id,
                    row["external_message_id"],
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = self._find_by_message(
                connection,
                channel,
                account_id,
                str(row["external_message_id"]),
            )
        return None if claimed is None else _inbound_from_row(claimed)

    def claim(self, key: InboundEventKey) -> StoredInboundEvent | None:
        """只在指定事件仍为 queued 时原子 claim，重复 wake-up 返回 None。"""
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE processed_events
                SET status = 'running', attempts = attempts + 1, updated_at = ?
                WHERE channel = ? AND account_id = ? AND external_message_id = ?
                  AND status = 'queued'
                """,
                (
                    self._clock().isoformat(),
                    key.channel,
                    key.account_id,
                    key.external_message_id,
                ),
            )
            if updated.rowcount != 1:
                return None
            claimed = self._find_by_message(
                connection,
                key.channel,
                key.account_id,
                key.external_message_id,
            )
        return None if claimed is None else _inbound_from_row(claimed)

    def get(self, key: InboundEventKey) -> StoredInboundEvent:
        """按稳定消息键读取事件，不暴露复合 SQL 给 Worker。"""
        with self._database.connect_read_only() as connection:
            row = self._find_by_message(
                connection,
                key.channel,
                key.account_id,
                key.external_message_id,
            )
        if row is None:
            raise ChannelStateError("inbound_not_found")
        return _inbound_from_row(row)

    def list_by_status(
        self,
        channel: str,
        account_id: str,
        status: InboundStatus,
    ) -> tuple[StoredInboundEvent, ...]:
        """按接收顺序列出指定状态，供 feeder 和恢复流程使用。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT rowid AS storage_rowid, * FROM processed_events
                WHERE channel = ? AND account_id = ? AND status = ?
                ORDER BY received_at, storage_rowid
                """,
                (channel, account_id, status),
            ).fetchall()
        return tuple(_inbound_from_row(row) for row in rows)

    def bind_session(self, key: InboundEventKey, session_id: int) -> StoredInboundEvent:
        """把 running 事件绑定到已解析 Session，供崩溃恢复判断。"""
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE processed_events SET session_id = ?, updated_at = ?
                WHERE channel = ? AND account_id = ? AND external_message_id = ?
                  AND status = 'running' AND (session_id IS NULL OR session_id = ?)
                """,
                (
                    session_id,
                    self._clock().isoformat(),
                    key.channel,
                    key.account_id,
                    key.external_message_id,
                    session_id,
                ),
            )
            if updated.rowcount != 1:
                raise ChannelStateError("invalid_inbound_transition")
        return self.get(key)

    def mark_completed(self, key: InboundEventKey) -> StoredInboundEvent:
        """把 running 事件结算为 completed。"""
        return self._finish(key, "completed", None)

    def mark_failed(
        self,
        key: InboundEventKey,
        error_code: str,
    ) -> StoredInboundEvent:
        """把 running 事件结算为 failed，只保存稳定错误码。"""
        return self._finish(key, "failed", error_code)

    def recover_running(
        self,
        key: InboundEventKey,
        status: Literal["queued", "completed", "failed"],
        error_code: str | None,
    ) -> StoredInboundEvent:
        """启动恢复时结算遗留 running，不允许从其他状态任意跳转。"""
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE processed_events
                SET status = ?, last_error_code = ?, updated_at = ?
                WHERE channel = ? AND account_id = ? AND external_message_id = ?
                  AND status = 'running'
                """,
                (
                    status,
                    error_code,
                    self._clock().isoformat(),
                    key.channel,
                    key.account_id,
                    key.external_message_id,
                ),
            )
            if updated.rowcount != 1:
                raise ChannelStateError("invalid_inbound_transition")
        return self.get(key)

    def _finish(
        self,
        key: InboundEventKey,
        status: Literal["completed", "failed"],
        error_code: str | None,
    ) -> StoredInboundEvent:
        """实现两个终态共享的条件状态更新。"""
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE processed_events
                SET status = ?, last_error_code = ?, updated_at = ?
                WHERE channel = ? AND account_id = ? AND external_message_id = ?
                  AND status = 'running'
                """,
                (
                    status,
                    error_code,
                    self._clock().isoformat(),
                    key.channel,
                    key.account_id,
                    key.external_message_id,
                ),
            )
            if updated.rowcount != 1:
                raise ChannelStateError("invalid_inbound_transition")
        return self.get(key)

    @staticmethod
    def _find_by_message(
        connection: sqlite3.Connection,
        channel: str,
        account_id: str,
        external_message_id: str,
    ) -> sqlite3.Row | None:
        """读取一条消息幂等记录。"""
        return connection.execute(
            """
            SELECT rowid AS storage_rowid, * FROM processed_events
            WHERE channel = ? AND account_id = ? AND external_message_id = ?
            """,
            (channel, account_id, external_message_id),
        ).fetchone()


class DeliveryRepository:
    """管理全部出站分片及其可恢复发送状态。"""

    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or _utc_now

    def create_parts(
        self,
        *,
        message_id: int,
        channel: str,
        account_id: str,
        external_conversation_id: str,
        reply_to_message_id: str,
        kind: DeliveryKind,
        contents: Sequence[str],
    ) -> tuple[StoredDelivery, ...]:
        """在首次发送前原子保存所有分片，重复调用返回同一组记录。"""
        if not contents or any(not isinstance(content, str) or not content for content in contents):
            raise ChannelStateError("invalid_delivery_content")
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._list_parts(connection, message_id, channel, kind)
            if existing:
                if not _delivery_parts_match(
                    existing,
                    account_id,
                    external_conversation_id,
                    reply_to_message_id,
                    contents,
                ):
                    raise ChannelStateError("delivery_content_conflict")
                return tuple(_delivery_from_row(row) for row in existing)
            now = self._clock().isoformat()
            for part_index, content in enumerate(contents):
                content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                idempotency_key = _idempotency_key(
                    message_id,
                    channel,
                    account_id,
                    kind,
                    part_index,
                )
                connection.execute(
                    """
                    INSERT INTO deliveries (
                        message_id, channel, account_id, external_conversation_id,
                        reply_to_message_id, delivery_kind, part_index, content,
                        content_hash, idempotency_key, status, attempts,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                    """,
                    (
                        message_id,
                        channel,
                        account_id,
                        external_conversation_id,
                        reply_to_message_id,
                        kind,
                        part_index,
                        content,
                        content_hash,
                        idempotency_key,
                        now,
                        now,
                    ),
                )
            rows = self._list_parts(connection, message_id, channel, kind)
        return tuple(_delivery_from_row(row) for row in rows)

    def claim_next(self, channel: str, account_id: str) -> StoredDelivery | None:
        """原子 claim 当前可发送且没有未完成前序 part 的最早 Delivery。"""
        now = self._clock().isoformat()
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT candidate.* FROM deliveries AS candidate
                WHERE candidate.channel = ? AND candidate.account_id = ?
                  AND (
                    candidate.status = 'queued'
                    OR (
                        candidate.status = 'retry_wait'
                        AND candidate.next_attempt_at IS NOT NULL
                        AND candidate.next_attempt_at <= ?
                    )
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM deliveries AS prior
                    WHERE prior.message_id = candidate.message_id
                      AND prior.channel = candidate.channel
                      AND prior.delivery_kind = candidate.delivery_kind
                      AND prior.part_index < candidate.part_index
                      AND prior.status != 'sent'
                  )
                ORDER BY candidate.created_at, candidate.id
                LIMIT 1
                """,
                (channel, account_id, now),
            ).fetchone()
            if row is None:
                return None
            updated = connection.execute(
                """
                UPDATE deliveries
                SET status = 'sending', attempts = attempts + 1,
                    next_attempt_at = NULL, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now, row["id"], row["status"]),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?",
                (row["id"],),
            ).fetchone()
        return None if claimed is None else _delivery_from_row(claimed)

    def get(self, delivery_id: int) -> StoredDelivery:
        """按内部 ID 读取一条 Delivery。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise ChannelStateError("delivery_not_found")
        return _delivery_from_row(row)

    def mark_sent(self, delivery_id: int, platform_message_id: str) -> StoredDelivery:
        """记录平台确认，并允许 unknown 经核实后结算为 sent。"""
        now = self._clock().isoformat()
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE deliveries
                SET status = 'sent', platform_message_id = ?, sent_at = ?,
                    updated_at = ?, last_error_code = NULL, last_error_detail = NULL
                WHERE id = ? AND status IN ('sending', 'unknown')
                """,
                (platform_message_id, now, now, delivery_id),
            )
            if updated.rowcount != 1:
                raise ChannelStateError("invalid_delivery_transition")
        return self.get(delivery_id)

    def mark_retry_wait(
        self,
        delivery_id: int,
        error_code: str,
        next_attempt_at: datetime,
        error_detail: str | None = None,
    ) -> StoredDelivery:
        """把临时发送错误安排到一个明确的未来时间。"""
        return self._transition_from_sending(
            delivery_id,
            "retry_wait",
            error_code,
            error_detail,
            next_attempt_at,
        )

    def mark_failed(
        self,
        delivery_id: int,
        error_code: str,
        error_detail: str | None = None,
    ) -> StoredDelivery:
        """把永久错误结算为 failed。"""
        return self._transition_from_sending(
            delivery_id,
            "failed",
            error_code,
            error_detail,
            None,
        )

    def mark_unknown(
        self,
        delivery_id: int,
        error_code: str,
        error_detail: str | None = None,
    ) -> StoredDelivery:
        """记录平台是否收到请求无法确定的发送结果。"""
        return self._transition_from_sending(
            delivery_id,
            "unknown",
            error_code,
            error_detail,
            None,
        )

    def recover_sending(self, channel: str, account_id: str) -> int:
        """重启时把遗留 sending 改为 unknown，避免生成新幂等键盲发。"""
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE deliveries
                SET status = 'unknown', last_error_code = 'feishu_delivery_unknown',
                    updated_at = ?
                WHERE channel = ? AND account_id = ? AND status = 'sending'
                """,
                (self._clock().isoformat(), channel, account_id),
            )
        return int(updated.rowcount)

    def recover_unknown(
        self,
        channel: str,
        account_id: str,
        *,
        max_attempts: int,
    ) -> int:
        """用相同 UUID 重新排队未达上限的 unknown，并终止耗尽项。"""
        now = self._clock().isoformat()
        with self._database.connect() as connection:
            requeued = connection.execute(
                """
                UPDATE deliveries
                SET status = 'queued', next_attempt_at = NULL, updated_at = ?
                WHERE channel = ? AND account_id = ? AND status = 'unknown'
                  AND attempts < ?
                """,
                (now, channel, account_id, max_attempts),
            )
            connection.execute(
                """
                UPDATE deliveries
                SET status = 'failed', last_error_code = 'feishu_send_failed',
                    updated_at = ?
                WHERE channel = ? AND account_id = ? AND status = 'unknown'
                  AND attempts >= ?
                """,
                (now, channel, account_id, max_attempts),
            )
        return int(requeued.rowcount)

    def _transition_from_sending(
        self,
        delivery_id: int,
        status: Literal["retry_wait", "failed", "unknown"],
        error_code: str,
        error_detail: str | None,
        next_attempt_at: datetime | None,
    ) -> StoredDelivery:
        """实现发送失败分支共享的条件状态更新。"""
        safe_detail = None if error_detail is None else error_detail[:500]
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE deliveries
                SET status = ?, last_error_code = ?, last_error_detail = ?,
                    next_attempt_at = ?, updated_at = ?
                WHERE id = ? AND status = 'sending'
                """,
                (
                    status,
                    error_code,
                    safe_detail,
                    None if next_attempt_at is None else next_attempt_at.isoformat(),
                    self._clock().isoformat(),
                    delivery_id,
                ),
            )
            if updated.rowcount != 1:
                raise ChannelStateError("invalid_delivery_transition")
        return self.get(delivery_id)

    @staticmethod
    def _list_parts(
        connection: sqlite3.Connection,
        message_id: int,
        channel: str,
        kind: DeliveryKind,
    ) -> list[sqlite3.Row]:
        """按分片顺序读取一条内部消息的同类 Delivery。"""
        return connection.execute(
            """
            SELECT * FROM deliveries
            WHERE message_id = ? AND channel = ? AND delivery_kind = ?
            ORDER BY part_index
            """,
            (message_id, channel, kind),
        ).fetchall()


def _delivery_parts_match(
    rows: Sequence[sqlite3.Row],
    account_id: str,
    external_conversation_id: str,
    reply_to_message_id: str,
    contents: Sequence[str],
) -> bool:
    """确认幂等创建没有试图改变既有目标或正文。"""
    return len(rows) == len(contents) and all(
        row["account_id"] == account_id
        and row["external_conversation_id"] == external_conversation_id
        and row["reply_to_message_id"] == reply_to_message_id
        and row["content"] == contents[index]
        for index, row in enumerate(rows)
    )


def _idempotency_key(
    message_id: int,
    channel: str,
    account_id: str,
    kind: DeliveryKind,
    part_index: int,
) -> str:
    """生成不含正文、固定 32 字符且跨进程稳定的发送 UUID。"""
    source = f"{message_id}:{channel}:{account_id}:{kind}:{part_index}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def _identity_from_row(row: sqlite3.Row) -> ChannelIdentity:
    """把 SQLite 行转换为身份对象。"""
    return ChannelIdentity(
        id=int(row["id"]),
        user_id=int(row["user_id"]),
        channel=str(row["channel"]),
        account_id=str(row["account_id"]),
        external_user_id=str(row["external_user_id"]),
        created_at=_parse_time(str(row["created_at"])),
    )


def _inbound_from_row(row: sqlite3.Row) -> StoredInboundEvent:
    """把 SQLite 行转换为可恢复入站对象。"""
    return StoredInboundEvent(
        key=InboundEventKey(
            channel=str(row["channel"]),
            account_id=str(row["account_id"]),
            external_message_id=str(row["external_message_id"]),
        ),
        event_id=str(row["event_id"]),
        external_user_id=str(row["external_user_id"]),
        external_conversation_id=str(row["external_conversation_id"]),
        chat_type=str(row["chat_type"]),
        message_type=str(row["message_type"]),
        content=str(row["content"]),
        reply_to_message_id=str(row["reply_to_message_id"]),
        session_id=None if row["session_id"] is None else int(row["session_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error_code=(
            None if row["last_error_code"] is None else str(row["last_error_code"])
        ),
        received_at=_parse_time(str(row["received_at"])),
        updated_at=_parse_time(str(row["updated_at"])),
    )


def _delivery_from_row(row: sqlite3.Row) -> StoredDelivery:
    """把 SQLite 行转换为出站对象。"""
    return StoredDelivery(
        id=int(row["id"]),
        message_id=None if row["message_id"] is None else int(row["message_id"]),
        channel=str(row["channel"]),
        account_id=str(row["account_id"]),
        external_conversation_id=str(row["external_conversation_id"]),
        reply_to_message_id=str(row["reply_to_message_id"]),
        delivery_kind=str(row["delivery_kind"]),
        part_index=int(row["part_index"]),
        content=str(row["content"]),
        content_hash=str(row["content_hash"]),
        idempotency_key=str(row["idempotency_key"]),
        platform_message_id=(
            None
            if row["platform_message_id"] is None
            else str(row["platform_message_id"])
        ),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error_code=(
            None if row["last_error_code"] is None else str(row["last_error_code"])
        ),
        last_error_detail=(
            None if row["last_error_detail"] is None else str(row["last_error_detail"])
        ),
        created_at=_parse_time(str(row["created_at"])),
        updated_at=_parse_time(str(row["updated_at"])),
        next_attempt_at=(
            None if row["next_attempt_at"] is None else _parse_time(str(row["next_attempt_at"]))
        ),
        sent_at=None if row["sent_at"] is None else _parse_time(str(row["sent_at"])),
    )


def _parse_time(value: str) -> datetime:
    """解析 Repository 自己写入的 ISO 时间。"""
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _utc_now() -> datetime:
    """返回带 UTC 时区的当前时间。"""
    return datetime.now(UTC)
