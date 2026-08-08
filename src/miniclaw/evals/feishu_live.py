"""真实飞书 E2E 的只读 SQLite 证据与后续编排接口。"""

import asyncio
import json
import os
import signal
import sqlite3
import sys
from collections import deque
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


class GatewayProcess:
    """持续排空输出、按精确 marker 就绪并有界退出的 Gateway 子进程。"""

    _READY_LINE = "MiniClaw gateway ready: feishu/default"
    _DIAGNOSTIC_LINES = 200
    _DIAGNOSTIC_CHARS = 4096

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        """保存子进程；生产调用方应通过 :meth:`start` 创建实例。"""
        self._process = process
        self._ready_event = asyncio.Event()
        self._ready = False
        self._diagnostics: deque[str] = deque(maxlen=self._DIAGNOSTIC_LINES)
        assert process.stdout is not None
        assert process.stderr is not None
        self._drain_tasks = (
            asyncio.create_task(self._drain(process.stdout, "stdout")),
            asyncio.create_task(self._drain(process.stderr, "stderr")),
        )

    @classmethod
    async def start(
        cls,
        *,
        project_root: Path,
        home: Path,
        ready_timeout: float,
        command: tuple[str, ...] | None = None,
    ) -> "GatewayProcess":
        """启动 Gateway，并等待精确的 Feishu ready marker。

        Args:
            project_root: 子进程工作目录。
            home: 传给 MiniClaw CLI 的状态目录。
            ready_timeout: 等待 ready marker 的最长秒数。
            command: 测试专用显式命令；省略时启动当前 Python 的 MiniClaw。

        Raises:
            FeishuLiveError: 子进程提前结束、未按时就绪或无法启动。
        """
        executable = command or (
            sys.executable,
            "-m",
            "miniclaw",
            "--home",
            str(home),
            "gateway",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *executable,
                cwd=project_root,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                limit=8 * 1024 * 1024,
            )
        except (OSError, ValueError):
            raise FeishuLiveError("gateway_start_failed") from None

        gateway = cls(process)
        ready_wait = asyncio.create_task(gateway._ready_event.wait())
        exit_wait = asyncio.create_task(process.wait())
        try:
            done, _ = await asyncio.wait(
                (ready_wait, exit_wait),
                timeout=ready_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if ready_wait in done and process.returncode is None:
                gateway._ready = True
                return gateway
            failure = (
                "gateway_exited_before_ready"
                if exit_wait in done
                else "gateway_ready_timeout"
            )
            await gateway._stop_after_failed_start()
            raise FeishuLiveError(failure)
        finally:
            for waiter in (ready_wait, exit_wait):
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(ready_wait, exit_wait, return_exceptions=True)

    @property
    def ready(self) -> bool:
        """返回当前实例是否见过精确 ready marker。"""
        return self._ready

    @property
    def bounded_diagnostics(self) -> tuple[str, ...]:
        """返回最多 200 行、每行最多 4096 字符的内存诊断快照。"""
        return tuple(self._diagnostics)

    async def stop(self, *, timeout: float = 10.0) -> int:
        """最多发送两次 SIGTERM，并等待子进程和输出管道结束。

        自动化验收刻意不发送 SIGKILL；第二次等待后仍不退出时，由操作者决定。
        """
        if self._process.returncode is None:
            self._send_sigterm()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=timeout)
            except TimeoutError:
                self._send_sigterm()
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=timeout)
                except TimeoutError:
                    raise FeishuLiveError("gateway_shutdown_timeout") from None
        await asyncio.gather(*self._drain_tasks, return_exceptions=True)
        if self._process.returncode is None:
            raise FeishuLiveError("gateway_shutdown_timeout")
        return self._process.returncode

    async def _drain(self, stream: asyncio.StreamReader, source: str) -> None:
        """持续排空一个 pipe，并只保存有界单行诊断。"""
        while line_bytes := await stream.readline():
            line = line_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
            if source == "stdout" and line == self._READY_LINE:
                self._ready_event.set()
            rendered = f"{source}:{line}"[: self._DIAGNOSTIC_CHARS]
            self._diagnostics.append(rendered)

    def _send_sigterm(self) -> None:
        """优先终止整个子进程组，平台不支持时退回单进程 terminate。"""
        if self._process.returncode is not None:
            return
        try:
            os.killpg(self._process.pid, signal.SIGTERM)
        except (AttributeError, ProcessLookupError, PermissionError):
            try:
                self._process.terminate()
            except ProcessLookupError:
                return

    async def _stop_after_failed_start(self) -> None:
        """启动失败时尽力回收子进程，不用内部诊断覆盖稳定错误码。"""
        try:
            await self.stop(timeout=1.0)
        except FeishuLiveError:
            return


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
