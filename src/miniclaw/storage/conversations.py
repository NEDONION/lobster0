"""CLI Session、Message 与 Turn 的参数化 SQLite Repository。"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from miniclaw.providers.base import JsonValue, ModelMessage
from miniclaw.storage.database import Database


class ConversationStateError(RuntimeError):
    """表示调用方尝试执行不符合当前状态的 Turn 变更。"""


class ConversationDataError(RuntimeError):
    """表示数据库中的 JSON 或枚举数据不符合当前程序契约。"""


@dataclass(frozen=True, slots=True)
class Session:
    """表示一个 Owner 在固定 Channel 对话中的持久会话。"""

    id: int
    user_id: int
    channel: str
    account_id: str
    external_conversation_id: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredMessage:
    """表示按会话顺序持久化的用户或助手消息。"""

    id: int
    session_id: int
    turn_id: int | None
    role: str
    content: str
    provider_message_id: str | None
    tool_call_id: str | None
    metadata: dict[str, JsonValue]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class StoredTurn:
    """表示一次输入从 queued 到终态的持久执行记录。"""

    id: int
    session_id: int
    parent_turn_id: int | None
    inbound_event_id: str
    status: str
    model: str
    started_at: datetime | None
    completed_at: datetime | None
    input_tokens: int
    output_tokens: int
    runtime_snapshot: dict[str, JsonValue]
    error_code: str | None
    error_message: str | None


class SessionRepository:
    """创建或读取单 Owner 的本地 CLI Session。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的 MiniClaw 数据库。

        Args:
            database: Session 表所在的 SQLite 连接工厂。
        """
        self._database = database

    def get_or_create_cli(self, user_id: int, conversation_id: str) -> Session:
        """幂等读取或创建一个本机 CLI 会话。

        Args:
            user_id: Phase 0 唯一 Owner 的数据库 ID。
            conversation_id: CLI 选择的稳定会话标识。

        Returns:
            channel 为 ``cli``、account 为 ``local`` 的持久 Session。

        Raises:
            ValueError: 会话标识为空。
        """
        normalized = conversation_id.strip()
        if not normalized:
            raise ValueError("conversation_id must not be empty")
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', ?, 'active', ?, ?)
                ON CONFLICT(channel, account_id, external_conversation_id) DO NOTHING
                """,
                (user_id, normalized, now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE channel = 'cli' AND account_id = 'local'
                  AND external_conversation_id = ?
                """,
                (normalized,),
            ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise ConversationStateError("CLI session is owned by a different user")
        return _session_from_row(row)


class MessageRepository:
    """按稳定 ID 顺序读取一个 Session 的最近消息。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的 MiniClaw 数据库。

        Args:
            database: Message 表所在的 SQLite 连接工厂。
        """
        self._database = database

    def list_recent(self, session_id: int, limit: int = 20) -> tuple[StoredMessage, ...]:
        """选择最新消息并按从旧到新的 Context 顺序返回。

        Args:
            session_id: 要读取的内部 Session ID。
            limit: 最多返回的严格正整数条数。

        Returns:
            按递增消息 ID 排列的不可变记录；为补齐最早 Turn 时可略多于 ``limit``。

        Raises:
            ValueError: limit 不是正整数。
            ConversationDataError: metadata JSON 已损坏。
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("message limit must be a positive integer")
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
            ordered = list(reversed(rows))
            if ordered and ordered[0]["role"] != "user":
                boundary = connection.execute(
                    "SELECT id FROM messages "
                    "WHERE session_id = ? AND role = 'user' AND id < ? "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, ordered[0]["id"]),
                ).fetchone()
            else:
                boundary = None
            if boundary is not None:
                prefix = connection.execute(
                    "SELECT * FROM messages "
                    "WHERE session_id = ? AND id >= ? AND id < ? ORDER BY id",
                    (session_id, boundary["id"], ordered[0]["id"]),
                ).fetchall()
                ordered = [*prefix, *ordered]
        return tuple(_message_from_row(row) for row in ordered)


class TurnRepository:
    """以事务方式创建 Turn/User Message 并写入终态。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的 MiniClaw 数据库。

        Args:
            database: Turn、Message 与 Session 表所在的 SQLite 连接工厂。
        """
        self._database = database

    def create_with_user_message(
        self,
        session_id: int,
        event_id: str,
        model: str,
        content: str,
    ) -> StoredTurn:
        """在一个事务中创建 queued Turn 和对应 User Message。

        Args:
            session_id: 当前输入所属的内部 Session ID。
            event_id: 当前 Channel 内稳定且幂等的入站事件 ID。
            model: 该 Turn 固定记录的模型 ID。
            content: 需要原样持久化的用户输入。

        Returns:
            状态为 ``queued`` 的新 Turn。

        Raises:
            sqlite3.IntegrityError: Session 不存在或事件 ID 在该 Session 内重复。
        """
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO turns (
                    session_id, inbound_event_id, status, model, runtime_snapshot_json
                ) VALUES (?, ?, 'queued', ?, '{}')
                """,
                (session_id, event_id, model),
            )
            turn_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO messages (session_id, turn_id, role, content, created_at)
                VALUES (?, ?, 'user', ?, ?)
                """,
                (session_id, turn_id, content, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn_from_row(row)

    def mark_running(self, turn_id: int) -> StoredTurn:
        """把一个 queued Turn 原子转为 running 并记录开始时间。

        Args:
            turn_id: 必须处于 ``queued`` 的内部 Turn ID。

        Returns:
            已刷新且状态为 ``running`` 的 Turn。

        Raises:
            ConversationStateError: Turn 不存在或不再处于 queued。
        """
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE turns SET status = 'running', started_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now, turn_id),
            )
            if cursor.rowcount != 1:
                raise ConversationStateError("Turn is not queued")
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn_from_row(row)

    def create_continuation(
        self,
        session_id: int,
        approval_id: int,
        parent_turn_id: int,
        model: str,
    ) -> StoredTurn:
        """为 waiting Turn 创建没有伪造 User Message 的 queued child Turn。"""
        with self._database.connect() as connection:
            parent = connection.execute(
                "SELECT status FROM turns WHERE id = ? AND session_id = ?",
                (parent_turn_id, session_id),
            ).fetchone()
            if parent is None or parent["status"] != "waiting_approval":
                raise ConversationStateError("Parent Turn is not waiting for approval")
            cursor = connection.execute(
                """
                INSERT INTO turns (
                    session_id, parent_turn_id, inbound_event_id, status,
                    model, runtime_snapshot_json
                ) VALUES (?, ?, ?, 'queued', ?, '{}')
                """,
                (session_id, parent_turn_id, f"approval:{approval_id}", model),
            )
            row = connection.execute(
                "SELECT * FROM turns WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return _turn_from_row(row)

    def append_intermediate_messages(
        self,
        turn_id: int,
        session_id: int,
        messages: tuple[ModelMessage, ...],
    ) -> None:
        """原子保存一个已完成的 Assistant Tool Call/Result 批次。"""
        if not messages:
            return
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            running = connection.execute(
                """
                SELECT 1 FROM turns
                WHERE id = ? AND session_id = ? AND status = 'running'
                """,
                (turn_id, session_id),
            ).fetchone()
            if running is None:
                raise ConversationStateError("Turn is not running")
            _insert_intermediate_messages(
                connection,
                turn_id,
                session_id,
                messages,
                now,
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )

    def complete_with_assistant_message(
        self,
        turn_id: int,
        session_id: int,
        content: str,
        *,
        intermediate_messages: tuple[ModelMessage, ...] = (),
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None,
        iterations: int,
        finish_reason: str,
    ) -> StoredMessage:
        """在一个事务中插入 Assistant、Token、快照和 completed 状态。

        Args:
            turn_id: 必须处于 running 的 Turn ID。
            session_id: 同时约束 Turn 和新 Message 的 Session ID。
            content: 最终可见 Assistant 回答。
            intermediate_messages: 本轮按顺序产生的 Assistant Tool Call 与 Tool Result。
            input_tokens: 当前 Agent Loop 累计输入 Token。
            output_tokens: 当前 Agent Loop 累计输出 Token。
            provider_request_id: 最后一个可用的服务商诊断请求 ID。
            iterations: 实际模型调用轮数。
            finish_reason: 最终模型响应的结束原因。

        Returns:
            与 completed Turn 同事务写入的 Assistant Message。

        Raises:
            ConversationStateError: Turn 不存在、不属于 Session 或不处于 running。
            sqlite3.IntegrityError: Message 或 Token 数据违反 Schema，整个事务回滚。
        """
        now = _utc_now().isoformat()
        snapshot = _json_text(
            {
                "provider_request_id": provider_request_id,
                "iterations": iterations,
                "finish_reason": finish_reason,
            }
        )
        metadata = _json_text({"provider_request_id": provider_request_id})
        with self._database.connect() as connection:
            _insert_intermediate_messages(
                connection,
                turn_id,
                session_id,
                intermediate_messages,
                now,
            )
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    session_id, turn_id, role, content, provider_message_id,
                    metadata_json, created_at
                ) VALUES (?, ?, 'assistant', ?, ?, ?, ?)
                """,
                (session_id, turn_id, content, provider_request_id, metadata, now),
            )
            message_id = int(cursor.lastrowid)
            updated = connection.execute(
                """
                UPDATE turns SET
                    status = 'completed', completed_at = ?, input_tokens = ?,
                    output_tokens = ?, runtime_snapshot_json = ?
                WHERE id = ? AND session_id = ? AND status = 'running'
                """,
                (now, input_tokens, output_tokens, snapshot, turn_id, session_id),
            )
            if updated.rowcount != 1:
                raise ConversationStateError("Turn is not running")
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return _message_from_row(row)

    def wait_for_approval(
        self,
        turn_id: int,
        session_id: int,
        approval_id: int,
        *,
        input_tokens: int,
        output_tokens: int,
        provider_request_id: str | None,
        iterations: int,
    ) -> StoredTurn:
        """保存 waiting_approval 状态和恢复所需的最小运行快照。"""
        snapshot = _json_text(
            {
                "approval_id": approval_id,
                "provider_request_id": provider_request_id,
                "iterations": iterations,
                "finish_reason": "approval_required",
            }
        )
        with self._database.connect() as connection:
            updated = connection.execute(
                """
                UPDATE turns SET
                    status = 'waiting_approval', input_tokens = ?, output_tokens = ?,
                    runtime_snapshot_json = ?
                WHERE id = ? AND session_id = ? AND status = 'running'
                """,
                (input_tokens, output_tokens, snapshot, turn_id, session_id),
            )
            if updated.rowcount != 1:
                raise ConversationStateError("Turn is not running")
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn_from_row(row)

    def fail(self, turn_id: int, error_code: str, error_message: str) -> StoredTurn:
        """把 queued/running Turn 标记为 failed 并保存安全错误分类。

        Args:
            turn_id: 尚未进入终态的 Turn ID。
            error_code: 由 TurnService 映射的稳定机器错误码。
            error_message: 已由模块边界收窄且不含凭据的用户可诊断消息。

        Returns:
            状态为 ``failed`` 的刷新后 Turn。
        """
        return self._terminal(
            turn_id,
            "failed",
            error_code=error_code,
            error_message=error_message,
        )

    def cancel(self, turn_id: int) -> StoredTurn:
        """把 queued/running Turn 标记为 cancelled，不伪造错误原因。

        Args:
            turn_id: 尚未进入终态的 Turn ID。

        Returns:
            状态为 ``cancelled`` 且没有 error_code 的 Turn。
        """
        return self._terminal(turn_id, "cancelled", error_code=None, error_message=None)

    def get(self, turn_id: int) -> StoredTurn:
        """按 ID 读取一个 Turn；不存在时抛出明确状态错误。

        Args:
            turn_id: 需要读取的内部 Turn ID。

        Returns:
            当前持久状态的不可变 Turn。

        Raises:
            ConversationStateError: ID 不存在。
        """
        with self._database.connect_read_only() as connection:
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        if row is None:
            raise ConversationStateError(f"Turn not found: {turn_id}")
        return _turn_from_row(row)

    def list_recent(self, session_id: int, limit: int = 20) -> tuple[StoredTurn, ...]:
        """按从新到旧顺序返回一个 Session 的有限 Turn。

        Args:
            session_id: 需要读取的内部 Session ID。
            limit: 最多返回的严格正整数条数。

        Returns:
            从最新到最旧排列的 Turn 元组。

        Raises:
            ValueError: limit 不是正整数。
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("Turn limit must be a positive integer")
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT * FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT ?",
                (session_id, limit),
            ).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def _terminal(
        self,
        turn_id: int,
        status: str,
        *,
        error_code: str | None,
        error_message: str | None,
    ) -> StoredTurn:
        """实现 failed/cancelled 共用的受限终态更新。"""
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE turns SET status = ?, completed_at = ?, error_code = ?, error_message = ?
                WHERE id = ? AND status IN ('queued', 'running')
                """,
                (status, now, error_code, error_message, turn_id),
            )
            if cursor.rowcount != 1:
                raise ConversationStateError("Turn cannot enter terminal state")
            row = connection.execute("SELECT * FROM turns WHERE id = ?", (turn_id,)).fetchone()
        return _turn_from_row(row)


def _insert_intermediate_messages(
    connection: sqlite3.Connection,
    turn_id: int,
    session_id: int,
    messages: tuple[ModelMessage, ...],
    created_at: str,
) -> None:
    """在调用方事务内保存合法的 Assistant/Tool 中间消息。"""
    for message in messages:
        if message.role == "assistant":
            metadata = _json_text(
                {
                    "tool_calls": [
                        {
                            "call_id": call.call_id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in message.tool_calls
                    ],
                    "reasoning_content": message.reasoning_content,
                }
            )
        elif message.role == "tool":
            metadata = "{}"
        else:
            raise ConversationStateError("intermediate message must be assistant or tool")
        connection.execute(
            """
            INSERT INTO messages (
                session_id, turn_id, role, content, tool_call_id,
                metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                turn_id,
                message.role,
                message.content,
                message.tool_call_id,
                metadata,
                created_at,
            ),
        )


def _session_from_row(row: sqlite3.Row) -> Session:
    """把 SQLite Row 转换为强类型 Session。"""
    return Session(
        id=row["id"],
        user_id=row["user_id"],
        channel=row["channel"],
        account_id=row["account_id"],
        external_conversation_id=row["external_conversation_id"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _message_from_row(row: sqlite3.Row) -> StoredMessage:
    """把 SQLite Row 转换为带已校验 metadata 的消息。"""
    return StoredMessage(
        id=row["id"],
        session_id=row["session_id"],
        turn_id=row["turn_id"],
        role=row["role"],
        content=row["content"],
        provider_message_id=row["provider_message_id"],
        tool_call_id=row["tool_call_id"],
        metadata=_json_object(row["metadata_json"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _turn_from_row(row: sqlite3.Row) -> StoredTurn:
    """把 SQLite Row 转换为带已校验快照的 Turn。"""
    return StoredTurn(
        id=row["id"],
        session_id=row["session_id"],
        parent_turn_id=row["parent_turn_id"],
        inbound_event_id=row["inbound_event_id"],
        status=row["status"],
        model=row["model"],
        started_at=_optional_datetime(row["started_at"]),
        completed_at=_optional_datetime(row["completed_at"]),
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        runtime_snapshot=_json_object(row["runtime_snapshot_json"]),
        error_code=row["error_code"],
        error_message=row["error_message"],
    )


def _json_text(value: dict[str, JsonValue]) -> str:
    """把内部 JSON 对象编码为稳定紧凑文本。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: str) -> dict[str, JsonValue]:
    """把数据库 JSON 文本解码并拒绝非 object 或非字符串键。"""
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise ConversationDataError("conversation JSON is invalid") from error
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise ConversationDataError("conversation JSON must be an object")
    return parsed


def _optional_datetime(value: str | None) -> datetime | None:
    """把可空 ISO 时间转换为带时区 datetime。"""
    return None if value is None else datetime.fromisoformat(value)


def _utc_now() -> datetime:
    """返回便于测试识别时区的当前 UTC 时间。"""
    return datetime.now(UTC)
