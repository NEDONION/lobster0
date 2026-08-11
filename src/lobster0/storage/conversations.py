"""CLI Session、Message 与 Turn 的参数化 SQLite Repository。"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from lobster0.providers.base import JsonValue, ModelMessage
from lobster0.storage.database import Database

CONTEXT_RESET_SUMMARY = (
    "【会话上下文已由 Owner 重置】\n"
    "这条摘要之前的对话历史已归档，不再作为上下文。当前没有待办任务、没有待续的代码改动、"
    "也没有待处理的审批。\n"
    "此摘要之后的每一条消息都按全新请求处理：不要接续任何早先的工作，不要主动修改文件，"
    "除非用户在新消息里明确提出要求。用户只是打招呼或提问时，直接回答就好。"
)


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


@dataclass(frozen=True, slots=True)
class StoredCompaction:
    """表示引用原消息范围的持久会话摘要。"""

    message_id: int
    session_id: int
    first_message_id: int
    last_message_id: int
    summary: str
    model: str
    content_hash: str
    created_at: datetime


class SessionRepository:
    """创建或读取单 Owner 在任意受支持 Channel 的 Session。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的 Lobster0 数据库。

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
        return self.get_or_create(
            user_id,
            "cli",
            "local",
            conversation_id,
        )

    def get_cli(self, user_id: int, conversation_id: str) -> Session | None:
        """按 Owner 与 CLI 会话标识读取 Session，但绝不隐式创建。

        Args:
            user_id: 当前 Owner 的数据库 ID。
            conversation_id: Desktop 或 CLI 使用的稳定会话标识。

        Returns:
            属于该 Owner 的 Session；不存在或属于其他 Owner 时返回 None。

        Raises:
            ValueError: 会话标识为空、超长或包含 NUL。
        """
        normalized = _session_key(
            conversation_id,
            "external_conversation_id",
            maximum=256,
        )
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = ? AND channel = 'cli' AND account_id = 'local'
                  AND external_conversation_id = ?
                """,
                (user_id, normalized),
            ).fetchone()
        return None if row is None else _session_from_row(row)

    def list_cli(self, user_id: int, limit: int = 50) -> tuple[Session, ...]:
        """按更新时间从新到旧列出一个 Owner 的有限 CLI Session。

        Args:
            user_id: 当前 Owner 的数据库 ID。
            limit: 最多返回的严格正整数条数。

        Returns:
            仅含 channel=cli、account=local 的不可变 Session 元组。

        Raises:
            ValueError: limit 不是严格正整数。
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("Session limit must be a positive integer")
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE user_id = ? AND channel = 'cli' AND account_id = 'local'
                ORDER BY updated_at DESC, id DESC LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
        return tuple(_session_from_row(row) for row in rows)

    def get_or_create(
        self,
        user_id: int,
        channel: str,
        account_id: str,
        external_conversation_id: str,
    ) -> Session:
        """幂等读取或创建一个 Channel/account/conversation Session。"""
        normalized_channel = _session_key(channel, "channel", maximum=32)
        normalized_account = _session_key(account_id, "account_id", maximum=64)
        normalized_conversation = _session_key(
            external_conversation_id,
            "external_conversation_id",
            maximum=256,
        )
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(channel, account_id, external_conversation_id) DO NOTHING
                """,
                (
                    user_id,
                    normalized_channel,
                    normalized_account,
                    normalized_conversation,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE channel = ? AND account_id = ?
                  AND external_conversation_id = ?
                """,
                (
                    normalized_channel,
                    normalized_account,
                    normalized_conversation,
                ),
            ).fetchone()
        if row is None or row["user_id"] != user_id:
            raise ConversationStateError("Channel session is owned by a different user")
        return _session_from_row(row)


class MessageRepository:
    """按稳定 ID 顺序读取一个 Session 的最近消息。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的 Lobster0 数据库。

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

    def get(self, message_id: int) -> StoredMessage | None:
        """按内部 ID 读取一条消息；不存在时返回 None。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
        return None if row is None else _message_from_row(row)

    def final_assistant_for_turn(self, turn_id: int) -> StoredMessage:
        """读取 completed Turn 最后一条 Assistant Message。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT * FROM messages
                WHERE turn_id = ? AND role = 'assistant'
                ORDER BY id DESC LIMIT 1
                """,
                (turn_id,),
            ).fetchone()
        if row is None:
            raise ConversationStateError("completed Turn has no assistant message")
        return _message_from_row(row)

    def save_experience_trace(
        self,
        message_id: int,
        trace: dict[str, JsonValue],
    ) -> None:
        """把最多 16 KiB 的脱敏 Experience trace 合并到 Assistant metadata。"""
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("message id must be positive")
        if not isinstance(trace, dict):
            raise ValueError("experience trace must be an object")
        encoded_trace = _json_text(trace).encode("utf-8")
        if len(encoded_trace) > 16 * 1024:
            raise ValueError("experience trace exceeds 16 KiB")
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT role, metadata_json FROM messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if row is None or row["role"] != "assistant":
                raise ConversationStateError("experience trace requires an assistant message")
            metadata = _json_object(row["metadata_json"])
            metadata["experience_trace"] = trace
            connection.execute(
                "UPDATE messages SET metadata_json = ? WHERE id = ?",
                (_json_text(metadata), message_id),
            )

    def experience_trace(self, message_id: int) -> dict[str, JsonValue] | None:
        """读取 Assistant 的脱敏 Experience trace；缺失或非对象值返回 None。"""
        if type(message_id) is not int or message_id <= 0:
            raise ValueError("message id must be positive")
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT metadata_json FROM messages WHERE id = ? AND role = 'assistant'",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        trace = _json_object(row["metadata_json"]).get("experience_trace")
        return trace if isinstance(trace, dict) else None

    def create_channel_notice(self, session_id: int, content: str) -> StoredMessage:
        """保存一条由 Channel 生成而非模型生成的安全 Assistant 提示。"""
        if not isinstance(content, str) or not content.strip():
            raise ValueError("channel notice must not be empty")
        now = _utc_now().isoformat()
        metadata = _json_text({"channel_notice": True})
        with self._database.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (
                    session_id, role, content, metadata_json, created_at
                ) VALUES (?, 'assistant', ?, ?, ?)
                """,
                (session_id, content, metadata, now),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?",
                (now, session_id),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return _message_from_row(row)

    def latest_compaction(self, session_id: int) -> StoredCompaction | None:
        """读取一个 Session 最新且结构有效的 compaction summary。"""
        with self._database.connect_read_only() as connection:
            rows = connection.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND role = 'system' ORDER BY id DESC",
                (session_id,),
            ).fetchall()
        for row in rows:
            metadata = _json_object(row["metadata_json"])
            if metadata.get("kind") == "compaction":
                return _compaction_from_row(row, metadata)
        return None

    def list_context(self, session_id: int, limit: int = 200) -> tuple[StoredMessage, ...]:
        """返回最新摘要和其覆盖范围之后的 Provider-safe 原始消息。

        Args:
            session_id: 当前会话 ID。
            limit: 摘要后最多恢复的最近原始消息数。

        Returns:
            摘要在前、未压缩原消息按 ID 正序排列的上下文。

        窗口首条不是 User 消息时，会在摘要覆盖范围之后往前补齐到最近一条 User 消息，
        避免把 Assistant Tool Call 切在窗口外、只留下裸 Tool Result（真实事故见
        docs/engineering/phase-4/20260811_approval-continuation-context-window-400-incident.md）。
        补齐后的条数因此可以略多于 ``limit``，与 :meth:`list_recent` 的契约一致。
        """
        if type(limit) is not int or limit <= 0:
            raise ValueError("message limit must be a positive integer")
        compaction = self.latest_compaction(session_id)
        if compaction is None:
            return _provider_safe_context(self.list_recent(session_id, limit=limit))
        with self._database.connect_read_only() as connection:
            summary_row = connection.execute(
                "SELECT * FROM messages WHERE id = ? AND session_id = ?",
                (compaction.message_id, session_id),
            ).fetchone()
            rows = connection.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id > ? AND role != 'system' "
                "ORDER BY id DESC LIMIT ?",
                (session_id, compaction.last_message_id, limit),
            ).fetchall()
            ordered = list(reversed(rows))
            if ordered and ordered[0]["role"] != "user":
                boundary = connection.execute(
                    "SELECT id FROM messages "
                    "WHERE session_id = ? AND role = 'user' AND id > ? AND id < ? "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id, compaction.last_message_id, ordered[0]["id"]),
                ).fetchone()
                if boundary is not None:
                    prefix = connection.execute(
                        "SELECT * FROM messages "
                        "WHERE session_id = ? AND role != 'system' "
                        "AND id >= ? AND id < ? ORDER BY id",
                        (session_id, boundary["id"], ordered[0]["id"]),
                    ).fetchall()
                    ordered = [*prefix, *ordered]
        if summary_row is None:
            raise ConversationDataError("compaction summary message is missing")
        return _provider_safe_context(
            (_message_from_row(summary_row), *(_message_from_row(row) for row in ordered))
        )

    def reset_context(self, session_id: int) -> StoredCompaction | None:
        """写入覆盖全部历史的重置摘要，让后续 Turn 从干净上下文重新开始。

        原始消息一条都不删除，只是不再进入发给 Provider 的上下文——:meth:`list_context`
        只返回最新摘要及其覆盖范围之后的消息。Owner 因此可以自助脱离一个被半完成任务
        污染的会话（例如审批中断留下"我正在改某个文件"的痕迹，之后随便说一句话都会被
        当成催进度），不必去动数据库。

        Args:
            session_id: 要重置上下文的会话 ID。

        Returns:
            新写入的摘要；已经没有可压缩消息时返回 None。

        Raises:
            ConversationStateError: 摘要范围不属于当前 Session 或与已有摘要冲突。
        """
        previous = self.latest_compaction(session_id)
        covered_until = 0 if previous is None else previous.last_message_id
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                "SELECT MIN(id) AS first, MAX(id) AS last FROM messages "
                "WHERE session_id = ? AND id > ? AND role != 'system'",
                (session_id, covered_until),
            ).fetchone()
        if row["first"] is None:
            return None
        return self.save_compaction(
            session_id,
            int(row["first"]),
            int(row["last"]),
            CONTEXT_RESET_SUMMARY,
            "reset",
            hashlib.sha256(CONTEXT_RESET_SUMMARY.encode()).hexdigest(),
        )

    def compaction_candidates(self, session_id: int) -> tuple[StoredMessage, ...]:
        """选择摘要覆盖范围后的最旧连续且可压缩消息前缀。

        最近两个 Turn 和任意 waiting approval Turn 都是保护边界；遇到第一个保护 Turn
        后立即停止，不能跨过它继续覆盖后面的消息。
        """
        latest = self.latest_compaction(session_id)
        covered_until = 0 if latest is None else latest.last_message_id
        with self._database.connect_read_only() as connection:
            recent = connection.execute(
                "SELECT id FROM turns WHERE session_id = ? ORDER BY id DESC LIMIT 2",
                (session_id,),
            ).fetchall()
            waiting = connection.execute(
                "SELECT id FROM turns WHERE session_id = ? AND status = 'waiting_approval'",
                (session_id,),
            ).fetchall()
            protected = {row[0] for row in (*recent, *waiting)}
            rows = connection.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id > ? AND role != 'system' ORDER BY id",
                (session_id, covered_until),
            ).fetchall()
        candidates: list[StoredMessage] = []
        for row in rows:
            if row["turn_id"] in protected:
                break
            candidates.append(_message_from_row(row))
        return tuple(candidates)

    def save_compaction(
        self,
        session_id: int,
        first_message_id: int,
        last_message_id: int,
        summary: str,
        model: str,
        content_hash: str,
    ) -> StoredCompaction:
        """原子插入一个引用原始消息范围的 system summary。

        Raises:
            ValueError: 覆盖范围、摘要、模型或哈希无效。
            ConversationStateError: 范围不属于当前 Session 或与已有摘要倒退。
        """
        if (
            type(first_message_id) is not int
            or type(last_message_id) is not int
            or first_message_id <= 0
            or last_message_id < first_message_id
            or not summary.strip()
            or len(summary) > 20_000
            or not model.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
        ):
            raise ValueError("invalid compaction summary")
        now = _utc_now().isoformat()
        metadata = _json_text(
            {
                "kind": "compaction",
                "first_message_id": first_message_id,
                "last_message_id": last_message_id,
                "model": model,
                "content_hash": content_hash,
            }
        )
        with self._database.connect() as connection:
            endpoints = connection.execute(
                "SELECT id, role FROM messages "
                "WHERE session_id = ? AND id IN (?, ?) ORDER BY id",
                (session_id, first_message_id, last_message_id),
            ).fetchall()
            if (
                len(endpoints) != (1 if first_message_id == last_message_id else 2)
                or any(row["role"] == "system" for row in endpoints)
            ):
                raise ConversationStateError("compaction range is not valid")
            rows = connection.execute(
                "SELECT metadata_json FROM messages "
                "WHERE session_id = ? AND role = 'system' ORDER BY id DESC",
                (session_id,),
            ).fetchall()
            for row in rows:
                previous = _json_object(row["metadata_json"])
                if previous.get("kind") != "compaction":
                    continue
                previous_last = previous.get("last_message_id")
                if type(previous_last) is not int:
                    raise ConversationStateError("compaction range is not continuous")
                if last_message_id <= previous_last:
                    raise ConversationStateError("compaction range does not advance")
                # 只要 previous_last 与新范围之间没有落下未压缩的真实消息就算连续。
                # 不能用 first_message_id == previous_last + 1 判定：summary 自身也是
                # messages 表里的一行（role='system'），会占掉紧随其后的那个 id，
                # 于是第二次压缩永远被误判为跳过了消息。
                skipped = connection.execute(
                    "SELECT 1 FROM messages "
                    "WHERE session_id = ? AND id > ? AND id < ? AND role != 'system' "
                    "LIMIT 1",
                    (session_id, previous_last, first_message_id),
                ).fetchone()
                if skipped is not None:
                    raise ConversationStateError("compaction range is not continuous")
                break
            cursor = connection.execute(
                "INSERT INTO messages ("
                "session_id, role, content, metadata_json, created_at"
                ") VALUES (?, 'system', ?, ?, ?)",
                (session_id, summary.strip(), metadata, now),
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?",
                (int(cursor.lastrowid),),
            ).fetchone()
        return _compaction_from_row(row, _json_object(row["metadata_json"]))


class TurnRepository:
    """以事务方式创建 Turn/User Message 并写入终态。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的 Lobster0 数据库。

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
        attachments: tuple[dict[str, JsonValue], ...] = (),
    ) -> StoredTurn:
        """在一个事务中创建 queued Turn 和对应 User Message。

        Args:
            session_id: 当前输入所属的内部 Session ID。
            event_id: 当前 Channel 内稳定且幂等的入站事件 ID。
            model: 该 Turn 固定记录的模型 ID。
            content: 需要原样持久化的用户输入。
            attachments: 可选的附件摘要，写入该 User Message 的 ``metadata_json``；
                与 Turn 在同一个事务里落盘，不会出现"有 Turn 没附件记录"的中间态。

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
            metadata = {"attachments": list(attachments)} if attachments else {}
            connection.execute(
                """
                INSERT INTO messages
                    (session_id, turn_id, role, content, metadata_json, created_at)
                VALUES (?, ?, 'user', ?, ?, ?)
                """,
                (session_id, turn_id, content, _json_text(metadata), now),
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
        runtime_snapshot: dict[str, JsonValue] | None = None,
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
                **(runtime_snapshot or {}),
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
        runtime_snapshot: dict[str, JsonValue] | None = None,
    ) -> StoredTurn:
        """保存 waiting_approval 状态和恢复所需的最小运行快照。"""
        snapshot = _json_text(
            {
                **(runtime_snapshot or {}),
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

    def get_by_inbound(self, session_id: int, inbound_event_id: str) -> StoredTurn:
        """按 Session 内稳定入站 ID 读取 Turn，供第二道幂等恢复使用。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT * FROM turns
                WHERE session_id = ? AND inbound_event_id = ?
                """,
                (session_id, inbound_event_id),
            ).fetchone()
        if row is None:
            raise ConversationStateError("inbound Turn not found")
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

    def interrupt_stale(self) -> int:
        """把上次进程遗留的 queued/running Turn 收敛为安全失败终态。

        Returns:
            本次被标记为 ``runtime_interrupted`` 的 Turn 数量。

        Notes:
            ``waiting_approval`` 可由持久审批继续，因此不会被修改。
        """
        now = _utc_now().isoformat()
        with self._database.connect() as connection:
            connection.execute(
                """
                UPDATE sessions SET updated_at = ?
                WHERE id IN (
                    SELECT DISTINCT session_id FROM turns
                    WHERE status IN ('queued', 'running')
                )
                """,
                (now,),
            )
            cursor = connection.execute(
                """
                UPDATE turns SET
                    status = 'failed', completed_at = ?,
                    error_code = 'runtime_interrupted',
                    error_message = 'Lobster0 Core 在上次运行期间退出'
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
        return cursor.rowcount

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


def _provider_safe_context(
    messages: tuple[StoredMessage, ...],
) -> tuple[StoredMessage, ...]:
    """逐条剔除无法形成 Assistant Tool Call → Tool Result 配对的消息。

    按单条消息 ID 而不是 Turn ID 判定失效范围：一个 Tool Call 批次的执行结果可能因为
    审批 continuation 被落在和触发它的 Assistant 消息不同的 Turn 里，按 Turn 整体剔除
    会误删同一 Turn 里其他仍然完整、无关的 Tool Call/Tool Result 配对（真实事故见
    docs/engineering/phase-4/20260810_feishu-provider-protocol-400-incident.md）。
    Orphan Tool Call 只连带它紧邻的前一条 User 消息一起剔除——那条消息是它唯一的触发
    者；再往前的消息属于更早、已经配对完整的交互，不受影响。

    判定按 Assistant 批次结算，并且两个方向都要收口（真实事故见
    docs/engineering/phase-4/20260811_approval-continuation-context-window-400-incident.md）：
    一个批次只拿到部分结果时，除了那条 Assistant 消息，**它已经配对成功的 Tool Result
    也必须一起剔除**，否则会留下没有 Tool Call 的裸 Tool Result；反过来，Tool Result 的
    Assistant 消息被上下文窗口或 compaction 切在范围之外时，这条结果同样必须剔除。

    Args:
        messages: 按消息 ID 递增排列的持久上下文。

    Returns:
        保留完整审批 continuation，并删除任何无法双向配对的 Tool Call/Tool Result。

    Raises:
        ConversationDataError: Tool Call metadata 或 Tool Message 已损坏。
    """
    provider_messages = tuple(
        message
        for message in messages
        if message.metadata.get("channel_notice") is not True
    )
    invalid_message_ids: set[int] = set()
    unanswered_calls: set[str] = set()
    answered_results: dict[str, int] = {}
    batch_assistant_id: int | None = None
    batch_trigger_id: int | None = None
    previous_message: StoredMessage | None = None

    def _settle_batch() -> None:
        """结算当前 Assistant 批次：完整则保留，缺结果则整批剔除。"""
        nonlocal batch_assistant_id, batch_trigger_id
        if batch_assistant_id is not None and unanswered_calls:
            invalid_message_ids.add(batch_assistant_id)
            invalid_message_ids.update(answered_results.values())
            if batch_trigger_id is not None:
                invalid_message_ids.add(batch_trigger_id)
        unanswered_calls.clear()
        answered_results.clear()
        batch_assistant_id = None
        batch_trigger_id = None

    for message in provider_messages:
        call_ids = _stored_tool_call_ids(message)
        if call_ids:
            _settle_batch()
            batch_assistant_id = message.id
            unanswered_calls.update(call_ids)
            if previous_message is not None and previous_message.role == "user":
                batch_trigger_id = previous_message.id
            previous_message = message
            continue
        if message.role == "tool":
            if message.tool_call_id is None:
                raise ConversationDataError("tool message has no tool_call_id")
            if message.tool_call_id in unanswered_calls:
                unanswered_calls.discard(message.tool_call_id)
                answered_results[message.tool_call_id] = message.id
            else:
                # 裸 Tool Result：Assistant Tool Call 被上下文窗口或 compaction 切掉、
                # 已被判定失效，或同一个 call_id 被重复回答。
                invalid_message_ids.add(message.id)
            previous_message = message
            continue
        _settle_batch()
        previous_message = message
    _settle_batch()
    if not invalid_message_ids:
        return provider_messages
    return tuple(
        message
        for message in provider_messages
        if message.id not in invalid_message_ids
    )


def _stored_tool_call_ids(message: StoredMessage) -> tuple[str, ...]:
    """从持久 Assistant metadata 中读取非空 Tool Call ID。

    Args:
        message: 已解码 metadata 的持久消息。

    Returns:
        没有 Tool Call 时为空，否则保持模型返回顺序。

    Raises:
        ConversationDataError: Tool Call 容器或任一 ID 不符合持久契约。
    """
    value = message.metadata.get("tool_calls", [])
    if not isinstance(value, list):
        raise ConversationDataError("tool call metadata is invalid")
    call_ids: list[str] = []
    for call in value:
        if not isinstance(call, dict):
            raise ConversationDataError("tool call metadata is invalid")
        call_id = call.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            raise ConversationDataError("tool call metadata is invalid")
        call_ids.append(call_id)
    return tuple(call_ids)


def _compaction_from_row(
    row: sqlite3.Row,
    metadata: dict[str, JsonValue],
) -> StoredCompaction:
    """把带 compaction metadata 的 system Message 收窄为强类型记录。"""
    first = metadata.get("first_message_id")
    last = metadata.get("last_message_id")
    model = metadata.get("model")
    content_hash = metadata.get("content_hash")
    if (
        type(first) is not int
        or type(last) is not int
        or first <= 0
        or last < first
        or not isinstance(model, str)
        or not model
        or not isinstance(content_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_hash) is None
    ):
        raise ConversationDataError("compaction metadata is invalid")
    return StoredCompaction(
        message_id=row["id"],
        session_id=row["session_id"],
        first_message_id=first,
        last_message_id=last,
        summary=row["content"],
        model=model,
        content_hash=content_hash,
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


def _session_key(value: str, name: str, *, maximum: int) -> str:
    """校验 Session 复合键非空、有界且不含控制字符。"""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > maximum
        or any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in normalized)
    ):
        raise ValueError(f"{name} is invalid")
    return normalized


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
