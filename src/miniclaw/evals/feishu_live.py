"""真实飞书 E2E 的只读 SQLite 证据与后续编排接口。"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from miniclaw.storage.database import Database, DatabaseError


class FeishuLiveError(RuntimeError):
    """表示 Live E2E 只能向操作者公开的稳定错误码。"""

    def __init__(self, code: str) -> None:
        """保存不含路径、SQL、正文或平台标识的错误码。"""
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class DatabaseCheckpoint:
    """保存一次人工动作前六张事实表的最大内部 ID。"""

    processed_event_rowid: int
    turn_id: int
    tool_run_id: int
    approval_id: int
    delivery_id: int
    audit_event_id: int


@dataclass(frozen=True, slots=True)
class EvidenceEvaluation:
    """按请求顺序保存已经满足与尚未满足的 Live evidence key。"""

    passed: tuple[str, ...]
    failed: tuple[str, ...]


type _EvidenceCheck = Callable[[sqlite3.Connection, DatabaseCheckpoint], bool]


def capture_checkpoint(database: Path) -> DatabaseCheckpoint:
    """只读捕获当前最大内部 ID，旧运行不能满足新案例。

    Args:
        database: 已初始化的 MiniClaw SQLite 文件。

    Returns:
        六张事实表的最大内部 ID。

    Raises:
        FeishuLiveError: 数据库不存在、损坏或无法只读查询。
    """
    try:
        with Database(database).connect_read_only() as connection:
            return DatabaseCheckpoint(
                processed_event_rowid=_maximum(connection, "processed_events", "rowid"),
                turn_id=_maximum(connection, "turns", "id"),
                tool_run_id=_maximum(connection, "tool_runs", "id"),
                approval_id=_maximum(connection, "approvals", "id"),
                delivery_id=_maximum(connection, "deliveries", "id"),
                audit_event_id=_maximum(connection, "audit_events", "id"),
            )
    except (DatabaseError, OSError, sqlite3.Error):
        raise FeishuLiveError("evidence_database_unavailable") from None


def evaluate_local_evidence(
    database: Path,
    checkpoint: DatabaseCheckpoint,
    requirements: tuple[str, ...],
) -> EvidenceEvaluation:
    """只读判断 checkpoint 后的 Feishu 状态是否满足封闭证据集合。

    Args:
        database: MiniClaw SQLite 文件。
        checkpoint: 人工动作前捕获的内部 ID。
        requirements: 需要按原顺序判断的证据 key。

    Returns:
        已满足与未满足 key，均保持输入顺序。

    Raises:
        FeishuLiveError: key 未注册或数据库无法只读查询。
    """
    if any(requirement not in _EVIDENCE_CHECKS for requirement in requirements):
        raise FeishuLiveError("unknown_local_evidence")
    passed: list[str] = []
    failed: list[str] = []
    try:
        with Database(database).connect_read_only() as connection:
            for requirement in requirements:
                target = passed if _EVIDENCE_CHECKS[requirement](connection, checkpoint) else failed
                target.append(requirement)
    except (DatabaseError, OSError, sqlite3.Error, ValueError):
        raise FeishuLiveError("evidence_database_unavailable") from None
    return EvidenceEvaluation(tuple(passed), tuple(failed))


def _maximum(connection: sqlite3.Connection, table: str, column: str) -> int:
    """读取固定事实表的最大内部整数，不接受外部输入。"""
    allowed = {
        ("processed_events", "rowid"),
        ("turns", "id"),
        ("tool_runs", "id"),
        ("approvals", "id"),
        ("deliveries", "id"),
        ("audit_events", "id"),
    }
    if (table, column) not in allowed:
        raise ValueError("unsupported checkpoint table")
    row = connection.execute(f"SELECT COALESCE(MAX({column}), 0) FROM {table}").fetchone()
    return int(row[0])


def _has_completed_inbox(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断是否出现新的 completed Feishu Inbox。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM processed_events
        WHERE rowid > ? AND channel = 'feishu' AND status = 'completed'
        LIMIT 1
        """,
        (checkpoint.processed_event_rowid,),
    )


def _has_completed_turn(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断是否出现绑定 Feishu Session 的 completed Turn。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu' AND t.status = 'completed'
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _has_sent_delivery(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断是否出现新的 sent Feishu Delivery。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM deliveries
        WHERE id > ? AND channel = 'feishu' AND status = 'sent'
        LIMIT 1
        """,
        (checkpoint.delivery_id,),
    )


def _has_succeeded_tool(tool_name: str) -> _EvidenceCheck:
    """构造只匹配一个固定 Tool 名的成功检查。"""

    def check(connection: sqlite3.Connection, checkpoint: DatabaseCheckpoint) -> bool:
        return _exists(
            connection,
            """
            SELECT 1 FROM tool_runs AS r
            JOIN turns AS t ON t.id = r.turn_id
            JOIN sessions AS s ON s.id = t.session_id
            WHERE r.id > ? AND s.channel = 'feishu'
              AND r.tool_name = ? AND r.status = 'succeeded'
            LIMIT 1
            """,
            (checkpoint.tool_run_id, tool_name),
        )

    return check


def _has_three_completed_turns_in_one_session(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断同一 Feishu Session 是否完成至少三个新 Turn。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu' AND t.status = 'completed'
        GROUP BY t.session_id HAVING COUNT(*) >= 3
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _has_approval(status: str, tool_status: str) -> _EvidenceCheck:
    """构造审批和绑定 ToolRun 必须同时满足的检查。"""

    def check(connection: sqlite3.Connection, checkpoint: DatabaseCheckpoint) -> bool:
        return _exists(
            connection,
            """
            SELECT 1 FROM approvals AS a
            JOIN tool_runs AS r ON r.id = a.tool_run_id
            JOIN turns AS t ON t.id = a.turn_id
            JOIN sessions AS s ON s.id = t.session_id
            WHERE a.id > ? AND s.channel = 'feishu'
              AND a.status = ? AND r.status = ?
            LIMIT 1
            """,
            (checkpoint.approval_id, status, tool_status),
        )

    return check


def _has_no_new_turn(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断 checkpoint 后没有任何 Feishu Turn。"""
    return not _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu'
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _has_multiple_sent_parts(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断一个新 Feishu Message 是否有连续且全部 sent 的多分片。"""
    return _exists(
        connection,
        """
        SELECT 1 FROM deliveries
        WHERE id > ? AND channel = 'feishu'
        GROUP BY message_id
        HAVING COUNT(*) >= 2
           AND SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) = COUNT(*)
           AND MIN(part_index) = 0
           AND MAX(part_index) = COUNT(*) - 1
        LIMIT 1
        """,
        (checkpoint.delivery_id,),
    )


def _has_gateway_ready(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断 checkpoint 后是否记录 Feishu supervisor ready。"""
    return _audit_count(
        connection,
        checkpoint,
        "channel.supervisor.ready",
    ) >= 1


def _has_transport_reconnected(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断真实连接是否先 reconnecting 后再次 connected。"""
    return (
        _audit_count(connection, checkpoint, "channel.transport.reconnecting") >= 1
        and _audit_count(connection, checkpoint, "channel.transport.connected") >= 1
    )


def _has_memory_restart_shape(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """判断两次 ready 之间同一 Session 至少完成两个新 Turn。"""
    if _audit_count(connection, checkpoint, "channel.supervisor.ready") < 2:
        return False
    return _exists(
        connection,
        """
        SELECT 1 FROM turns AS t
        JOIN sessions AS s ON s.id = t.session_id
        WHERE t.id > ? AND s.channel = 'feishu' AND t.status = 'completed'
        GROUP BY t.session_id HAVING COUNT(*) >= 2
        LIMIT 1
        """,
        (checkpoint.turn_id,),
    )


def _audit_count(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
    event_type: str,
) -> int:
    """按解析后的安全 metadata 统计 Feishu Audit，不做字符串猜测。"""
    rows = connection.execute(
        """
        SELECT metadata_json FROM audit_events
        WHERE id > ? AND event_type = ? ORDER BY id
        """,
        (checkpoint.audit_event_id, event_type),
    ).fetchall()
    count = 0
    for row in rows:
        metadata = json.loads(str(row["metadata_json"]))
        if isinstance(metadata, dict) and metadata.get("channel") == "feishu":
            count += 1
    return count


def _exists(
    connection: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> bool:
    """执行固定只读查询并判断是否至少有一行。"""
    return connection.execute(statement, parameters).fetchone() is not None


def _unsupported_until_secret_scan(
    connection: sqlite3.Connection,
    checkpoint: DatabaseCheckpoint,
) -> bool:
    """Secret scan 由 Evidence 阶段执行，数据库本身不能证明无泄露。"""
    del connection, checkpoint
    return False


_EVIDENCE_CHECKS: dict[str, _EvidenceCheck] = {
    "gateway_ready": _has_gateway_ready,
    "inbox_completed": _has_completed_inbox,
    "turn_completed": _has_completed_turn,
    "delivery_sent": _has_sent_delivery,
    "one_session_three_turns": _has_three_completed_turns_in_one_session,
    "system_info_succeeded": _has_succeeded_tool("system_info"),
    "read_file_succeeded": _has_succeeded_tool("read_file"),
    "approval_pending": _has_approval("pending", "waiting_approval"),
    "approval_consumed_once": _has_approval("consumed", "succeeded"),
    "approval_denied": _has_approval("denied", "denied"),
    "no_new_turn": _has_no_new_turn,
    "multiple_parts_sent": _has_multiple_sent_parts,
    "memory_survived_restart": _has_memory_restart_shape,
    "transport_reconnected": _has_transport_reconnected,
    "secret_scan_zero": _unsupported_until_secret_scan,
}
