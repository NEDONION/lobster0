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


# 与 protocol._MAX_ATTACHMENTS 同一个上限：一条消息最多能带这么多附件，投影时
# 再夹一次，防止历史里某条被写坏的 metadata 撑爆一帧。
_MAX_ATTACHMENTS = 10


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
        session = self._sessions.get_local(owner_id, session_key)
        if session is None:
            raise ConversationQueryError("session_not_found", "任务不存在")
        turns = tuple(reversed(self._turns.list_recent(session.id, limit)))
        # 过程也要能回放：此前只留 user/assistant 且 content 非空，于是工具
        # 调用被整个滤掉，只调工具没写正文的那一轮也被丢掉。重新打开会话只剩
        # 问答两行，定时任务尤其致命——它没有实时事件流可看。
        visible = tuple(
            message
            for message in self._messages.list_recent(session.id, limit)
            if message.role in {"user", "assistant", "tool"}
            and (message.content or _tool_call_names(message) or _reasoning(message))
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
            "messages": _message_summaries(visible),
        }


def _title(message: StoredMessage | None) -> str:
    """从最近用户消息生成单行、最多 80 字符的任务标题。"""
    if message is None:
        return "未命名任务"
    normalized = re.sub(r"\s+", " ", message.content).strip()
    return normalized[:80] or "未命名任务"


def _message_summaries(messages: tuple[object, ...]) -> list[JsonValue]:
    """按顺序投影历史消息，并把工具结果关联回它的工具名。

    结果消息自身只带 tool_call_id，名字在发起它的那条 Assistant 里，所以边走
    边建映射。
    """
    names_by_call: dict[str, str] = {}
    summaries: list[JsonValue] = []
    for message in messages:
        names_by_call.update(_tool_call_ids(message))
        summaries.append(_message_summary(message, names_by_call))
    return summaries


def _tool_call_ids(message: object) -> dict[str, str]:
    """从一条 Assistant 里取出 call_id → 工具名的映射。"""
    calls = message.metadata.get("tool_calls")
    if not isinstance(calls, list):
        return {}
    mapping: dict[str, str] = {}
    for call in calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("call_id")
        name = call.get("name")
        if isinstance(call_id, str) and isinstance(name, str):
            mapping[call_id] = name
    return mapping


def _message_summary(
    message: object, names_by_call: dict[str, str]
) -> dict[str, JsonValue]:
    """把一条历史消息投影成界面可回放的形状。

    思考与工具调用都从 metadata 里取——它们本来就存着，只是此前没有下发。
    工具**参数不下发**：里面可能有 URL、路径这类没必要进列表的细节，界面只需要
    知道调用了什么。
    """
    summary: dict[str, JsonValue] = {
        "role": message.role,
        "content": _content(message.content),
        "turn_id": message.turn_id,
    }
    reasoning = _reasoning(message)
    if reasoning:
        summary["reasoning"] = _content(reasoning)
    attachments = _attachments(message)
    if attachments:
        summary["attachments"] = attachments
    names = _tool_call_names(message)
    if names:
        summary["tool_calls"] = list(names)
    if message.role == "tool":
        summary["tool_name"] = names_by_call.get(message.tool_call_id or "")
    return summary


def _attachments(message: object) -> list[JsonValue]:
    """读取该条消息携带的附件摘要。

    附件早就写在 ``metadata_json`` 里，只是此前从没下发，于是渲染层无从知道
    一条消息带了什么——上传的图片在界面上完全看不见。

    **只投影可安全展示的四个字段，且不含文件字节。** 历史可能有几十条消息，
    每条都内联 data URI 会让一次 session.load 涨到几十兆；缩略图由界面按需
    通过 ``artifacts.preview`` 单独取，那条路径已经带归属校验。

    逐字段校验类型而不是整块透传：``metadata_json`` 是历史数据，早期写入的
    形状不一定与今天一致，坏掉的一条不该让整个会话打不开。
    """
    raw = message.metadata.get("attachments")
    if not isinstance(raw, list):
        return []
    attachments: list[JsonValue] = []
    for item in raw[:_MAX_ATTACHMENTS]:
        if not isinstance(item, dict):
            continue
        artifact_id = item.get("artifact_id")
        filename = item.get("filename")
        media_type = item.get("media_type")
        byte_size = item.get("byte_size")
        if not isinstance(artifact_id, str) or not isinstance(filename, str):
            continue
        if not isinstance(media_type, str) or not isinstance(byte_size, int):
            continue
        attachments.append(
            {
                "artifact_id": artifact_id,
                "filename": filename,
                "media_type": media_type,
                # 存储里叫 byte_size，但线上早已由 attachment.stage 定为
                # size_bytes。投影时对齐线上约定，免得 Desktop 为同一个概念
                # 维护两个字段名。
                "size_bytes": byte_size,
            }
        )
    return attachments


def _reasoning(message: object) -> str:
    """读取 Assistant 的思考正文；缺失或类型不对时按空处理。"""
    value = message.metadata.get("reasoning_content")
    return value if isinstance(value, str) else ""


def _tool_call_names(message: object) -> tuple[str, ...]:
    """读取该条 Assistant 发起的工具名，顺序与模型返回一致。"""
    calls = message.metadata.get("tool_calls")
    if not isinstance(calls, list):
        return ()
    return tuple(
        call["name"]
        for call in calls
        if isinstance(call, dict) and isinstance(call.get("name"), str)
    )


def _content(content: str) -> str:
    """限制单条历史消息大小，确保有限列表不会突破协议帧预算。"""
    return content if len(content) <= 8_000 else content[:7_999] + "…"
