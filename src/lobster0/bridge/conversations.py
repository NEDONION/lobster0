"""为 Desktop Bridge 提供 Owner-scoped 会话摘要与可见历史。"""

import re

from lobster0.providers.base import JsonValue
from lobster0.storage.conversations import (
    MessageRepository,
    SessionRepository,
    StoredMessage,
    TurnRepository,
)
from lobster0.storage.database import Database


class ConversationQueryError(RuntimeError):
    """表示可安全返回给 Desktop 的稳定会话查询错误。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码与不含数据库细节的消息。

        Args:
            code: Desktop 可分支处理的稳定错误码。
            message: 不包含内部路径、SQL 或持久数据的公开消息。
        """
        super().__init__(message)
        self.code = code


class ConversationConsole:
    """把持久 Session、Turn 与 Message 投影为有限 Desktop JSON。"""

    def __init__(self, database: Database) -> None:
        """绑定已完成迁移的数据库及现有会话 Repository。

        Args:
            database: Desktop 当前 Runtime 使用的唯一 SQLite 数据库。
        """
        self._sessions = SessionRepository(database)
        self._messages = MessageRepository(database)
        self._turns = TurnRepository(database)

    def list_sessions(self, owner_id: int, *, limit: int) -> dict[str, JsonValue]:
        """列出当前 Owner 最近使用的 CLI Session 安全摘要。

        Args:
            owner_id: Runtime 已绑定的 Owner ID。
            limit: 1 到 50 之间的最大 Session 数量。

        Returns:
            包含 ``sessions`` 数组的 JSON object。

        Raises:
            ValueError: limit 超出 Desktop 查询边界。
        """
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("Session limit must be between 1 and 50")
        summaries: list[JsonValue] = []
        # ponytail: bounded 50-session N+1; use one window query only if profiling requires it.
        for session in self._sessions.list_cli(owner_id, limit):
            turns = self._turns.list_recent(session.id, 1)
            messages = self._messages.list_recent(session.id, 20)
            latest_turn = turns[0] if turns else None
            latest_user = next(
                (message for message in reversed(messages) if message.role == "user"),
                None,
            )
            summaries.append(
                {
                    "session_key": session.external_conversation_id,
                    "title": _title(latest_user),
                    "updated_at": session.updated_at.isoformat(),
                    "status": "idle" if latest_turn is None else latest_turn.status,
                }
            )
        return {"sessions": summaries}

    def history(
        self,
        owner_id: int,
        *,
        session_key: str,
        limit: int,
    ) -> dict[str, JsonValue]:
        """读取当前 Owner 一个 CLI Session 的有限可见历史。

        Args:
            owner_id: Runtime 已绑定的 Owner ID。
            session_key: 由 Desktop 选择的稳定 Session 标识。
            limit: 1 到 200 之间的最大消息与 Turn 数量。

        Returns:
            只含稳定 Turn 状态和 user/assistant 文本的 JSON object。

        Raises:
            ValueError: limit 越界或 Session 标识无效。
            ConversationQueryError: Session 不存在或不属于当前 Owner。
        """
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("History limit must be between 1 and 200")
        session = self._sessions.get_cli(owner_id, session_key)
        if session is None:
            raise ConversationQueryError("session_not_found", "任务不存在")
        turns = tuple(reversed(self._turns.list_recent(session.id, limit)))
        visible = tuple(
            message
            for message in self._messages.list_recent(session.id, limit)
            if message.role in {"user", "assistant"} and message.content
        )[-limit:]
        return {
            "session_key": session.external_conversation_id,
            "updated_at": session.updated_at.isoformat(),
            "turns": [
                {
                    "turn_id": turn.id,
                    "status": turn.status,
                    "error_code": turn.error_code,
                }
                for turn in turns
            ],
            "messages": [
                {
                    "role": message.role,
                    "content": _content(message.content),
                    "turn_id": message.turn_id,
                }
                for message in visible
            ],
        }


def _title(message: StoredMessage | None) -> str:
    """从最近用户消息生成单行、最多 80 字符的任务标题。"""
    if message is None:
        return "未命名任务"
    normalized = re.sub(r"\s+", " ", message.content).strip()
    return normalized[:80] or "未命名任务"


def _content(content: str) -> str:
    """限制单条历史消息大小，确保有限列表不会突破协议帧预算。"""
    return content if len(content) <= 8_000 else content[:7_999] + "…"
