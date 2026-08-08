"""严格解析 Owner Markdown，并以 fail-closed 方式重建 SQLite Projection。"""

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from miniclaw.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from miniclaw.memory.models import SourceRef
from miniclaw.memory.repository import MemoryManifestRepository
from miniclaw.memory.store import contains_sensitive_memory
from miniclaw.memory.text import memory_search_shadow
from miniclaw.storage.database import Database

_PARSER_VERSION = "memory-markdown-v1"
_HEADER = "# MiniClaw Memory\n\n<!-- format: memory-markdown-v1 -->\n"
_BLOCK = re.compile(
    r"<!-- miniclaw:unit (?P<id>[^\s<>]{1,160}) -->\n"
    r"## (?P<key>[^\n]{1,200})\n\n"
    r"(?P<text>.*?)\n\n"
    r"```miniclaw-memory\n(?P<meta>[^\n]+)\n```\n"
    r"<!-- miniclaw:end (?P<end>[^\s<>]{1,160}) -->\n?",
    re.DOTALL,
)
_METADATA_KEYS = frozenset(
    {
        "confidence",
        "kind",
        "scope",
        "sensitivity",
        "sources",
        "status",
        "text_sha256",
        "valid_from",
        "valid_until",
    }
)
_SOURCE_KEYS = frozenset({"channel", "message_id", "session_id"})
_TERMINAL_STATUSES = frozenset({"rejected", "superseded", "archived", "expired"})


@dataclass(frozen=True, slots=True)
class ReconcileIssue:
    """描述不含正文和绝对路径的 Markdown 对账错误。"""

    path: str
    line: int
    code: str


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """汇总一次 Owner Markdown 对账的新增、更新与错误。"""

    added: tuple[str, ...]
    updated: tuple[str, ...]
    errors: tuple[ReconcileIssue, ...]
    content_hash: str | None


@dataclass(frozen=True, slots=True)
class _ParsedUnit:
    """组合已经验证的 Markdown Unit 与原始 block hash。"""

    document: MarkdownUnitDocument
    block_hash: str


class _ParseError(RuntimeError):
    """携带安全行号的内部 Markdown 解析错误。"""

    def __init__(self, line: int) -> None:
        """保存至少为一的行号，不保留原始内容。"""
        super().__init__("memory Markdown is invalid")
        self.line = max(1, line)


class MemoryReconciler:
    """比较 manifest，并只在完整解析成功后原子刷新 Projection。"""

    def __init__(
        self,
        database: Database,
        markdown: MemoryMarkdownStore,
        manifests: MemoryManifestRepository,
    ) -> None:
        """绑定数据库、Markdown Store 和 manifest Repository。"""
        self._database = database
        self._markdown = markdown
        self._manifests = manifests

    def scan(
        self,
        owner_id: int,
        *,
        force: bool = False,
        now: datetime | None = None,
    ) -> ReconcileResult:
        """扫描 Owner 真相文件；非法编辑保留文件和上一版 Projection。"""
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("memory owner_id is invalid")
        if type(force) is not bool:
            raise ValueError("memory reconcile force must be bool")
        current = _aware(now or datetime.now(UTC))
        source = self._markdown.read_for_reconcile(owner_id)
        if source is None:
            return ReconcileResult((), (), (), None)
        manifest = self._manifests.find(owner_id, "memory.md")
        if (
            not force
            and manifest is not None
            and manifest.status == "current"
            and manifest.content_hash == source.content_hash
            and manifest.mtime_ns == source.mtime_ns
        ):
            return ReconcileResult((), (), (), source.content_hash)
        try:
            parsed = _parse(source.payload, owner_id)
            added, updated = self._apply(
                owner_id,
                parsed,
                source.content_hash,
                source.mtime_ns,
                current,
            )
        except (_ParseError, UnicodeError, ValueError, sqlite3.Error) as error:
            line = error.line if isinstance(error, _ParseError) else 1
            self._mark_error(
                owner_id,
                source.content_hash,
                source.mtime_ns,
                manifest.last_valid_hash if manifest is not None else source.content_hash,
                line,
                current,
            )
            return ReconcileResult(
                (),
                (),
                (ReconcileIssue("memory.md", line, "memory_markdown_invalid"),),
                source.content_hash,
            )
        return ReconcileResult(added, updated, (), source.content_hash)

    def _apply(
        self,
        owner_id: int,
        parsed: tuple[_ParsedUnit, ...],
        content_hash: str,
        mtime_ns: int,
        now: datetime,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """在单事务中验证来源、更新 Unit/Source、审计并采用 manifest。"""
        timestamp = now.isoformat()
        added: list[str] = []
        updated: list[str] = []
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT * FROM memory_units WHERE owner_id = ? ORDER BY id",
                (owner_id,),
            ).fetchall()
            existing = {str(row["id"]): row for row in rows}
            parsed_ids = {item.document.unit_id for item in parsed}
            if set(existing) - parsed_ids:
                raise _ParseError(1)
            for item in parsed:
                document = item.document
                _validate_sources(connection, owner_id, document.sources)
                row = existing.get(document.unit_id)
                if row is not None and str(row["status"]) in _TERMINAL_STATUSES:
                    if document.status != str(row["status"]):
                        raise _ParseError(1)
                text_hash = hashlib.sha256(document.text.strip().encode()).hexdigest()
                duplicate = connection.execute(
                    """
                    SELECT id FROM memory_units
                    WHERE owner_id = ? AND text_hash = ? AND id != ?
                    """,
                    (owner_id, text_hash, document.unit_id),
                ).fetchone()
                if duplicate is not None:
                    raise _ParseError(1)
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO memory_units (
                            id, owner_id, memory_key, text, text_hash, kind, scope,
                            status, confidence, sensitivity, valid_from, valid_until,
                            markdown_hash, search_shadow, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            document.unit_id,
                            owner_id,
                            document.key,
                            document.text.strip(),
                            text_hash,
                            document.kind,
                            document.scope,
                            document.status,
                            float(document.confidence),
                            document.sensitivity,
                            document.valid_from.isoformat(),
                            (
                                None
                                if document.valid_until is None
                                else document.valid_until.isoformat()
                            ),
                            item.block_hash,
                            memory_search_shadow(document.text),
                            timestamp,
                            timestamp,
                        ),
                    )
                    added.append(document.unit_id)
                else:
                    changed = _changed(row, document, text_hash, item.block_hash)
                    connection.execute(
                        """
                        UPDATE memory_units SET
                            memory_key = ?, text = ?, text_hash = ?, kind = ?, scope = ?,
                            status = ?, confidence = ?, sensitivity = ?, valid_from = ?,
                            valid_until = ?, markdown_hash = ?, search_shadow = ?, updated_at = ?
                        WHERE owner_id = ? AND id = ?
                        """,
                        (
                            document.key,
                            document.text.strip(),
                            text_hash,
                            document.kind,
                            document.scope,
                            document.status,
                            float(document.confidence),
                            document.sensitivity,
                            document.valid_from.isoformat(),
                            (
                                None
                                if document.valid_until is None
                                else document.valid_until.isoformat()
                            ),
                            item.block_hash,
                            memory_search_shadow(document.text),
                            timestamp,
                            owner_id,
                            document.unit_id,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM memory_sources WHERE unit_id = ?",
                        (document.unit_id,),
                    )
                    if changed:
                        updated.append(document.unit_id)
                connection.executemany(
                    """
                    INSERT INTO memory_sources (
                        unit_id, message_id, session_id, channel, ordinal
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            document.unit_id,
                            source.message_id,
                            source.session_id,
                            source.channel,
                            ordinal,
                        )
                        for ordinal, source in enumerate(document.sources)
                    ),
                )
            for unit_id in (*added, *updated):
                connection.execute(
                    """
                    INSERT INTO memory_audit (
                        owner_id, event_type, unit_id, reason_code,
                        metadata_json, created_at
                    ) VALUES (?, 'manual_edit', ?, 'projection_rebuilt', ?, ?)
                    """,
                    (
                        owner_id,
                        unit_id,
                        json.dumps({"content_hash": content_hash}, separators=(",", ":")),
                        timestamp,
                    ),
                )
            connection.execute(
                """
                INSERT INTO memory_manifests (
                    owner_id, relative_path, content_hash, last_valid_hash,
                    mtime_ns, parser_version, status, last_scanned_at
                ) VALUES (?, 'memory.md', ?, ?, ?, ?, 'current', ?)
                ON CONFLICT(owner_id, relative_path) DO UPDATE SET
                    content_hash = excluded.content_hash,
                    last_valid_hash = excluded.last_valid_hash,
                    mtime_ns = excluded.mtime_ns,
                    parser_version = excluded.parser_version,
                    status = excluded.status,
                    last_scanned_at = excluded.last_scanned_at
                """,
                (
                    owner_id,
                    content_hash,
                    content_hash,
                    mtime_ns,
                    _PARSER_VERSION,
                    timestamp,
                ),
            )
        return tuple(added), tuple(updated)

    def _mark_error(
        self,
        owner_id: int,
        content_hash: str,
        mtime_ns: int,
        last_valid_hash: str,
        line: int,
        now: datetime,
    ) -> None:
        """持久化不含正文的 parser 状态与审计，供 Doctor 只读报告。"""
        self._manifests.upsert(
            owner_id=owner_id,
            relative_path="memory.md",
            content_hash=content_hash,
            last_valid_hash=last_valid_hash,
            mtime_ns=mtime_ns,
            parser_version=_PARSER_VERSION,
            status="error",
            now=now,
        )
        with self._database.connect() as connection:
            connection.execute(
                """
                INSERT INTO memory_audit (
                    owner_id, event_type, reason_code, metadata_json, created_at
                ) VALUES (?, 'reconcile_failed', 'memory_markdown_invalid', ?, ?)
                """,
                (
                    owner_id,
                    json.dumps({"line": line}, separators=(",", ":")),
                    now.isoformat(),
                ),
            )


def _parse(payload: bytes, owner_id: int) -> tuple[_ParsedUnit, ...]:
    """严格解析完整文档，拒绝未知文本、重复 ID 与不合法 metadata。"""
    if len(payload) > 4 * 1024 * 1024:
        raise _ParseError(1)
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise _ParseError(1) from error
    if not text.startswith(_HEADER):
        raise _ParseError(1)
    cursor = len(_HEADER)
    items: list[_ParsedUnit] = []
    seen: set[str] = set()
    for match in _BLOCK.finditer(text, cursor):
        between = text[cursor : match.start()]
        if between.strip():
            raise _ParseError(_line(text, cursor))
        line = _line(text, match.start())
        unit_id = match.group("id")
        if unit_id != match.group("end") or unit_id in seen:
            raise _ParseError(line)
        seen.add(unit_id)
        try:
            metadata = json.loads(match.group("meta"), parse_constant=_reject_constant)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise _ParseError(line) from error
        if not isinstance(metadata, dict) or set(metadata) != _METADATA_KEYS:
            raise _ParseError(line)
        sources = _sources(metadata.get("sources"), line)
        valid_from = _parse_time(metadata.get("valid_from"), line)
        raw_until = metadata.get("valid_until")
        valid_until = None if raw_until is None else _parse_time(raw_until, line)
        digest = metadata.get("text_sha256")
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise _ParseError(line)
        visible_text = match.group("text").strip()
        if contains_sensitive_memory(visible_text):
            raise _ParseError(line)
        try:
            document = MarkdownUnitDocument(
                unit_id=unit_id,
                owner_id=owner_id,
                key=match.group("key").strip(),
                text=visible_text,
                kind=_text(metadata.get("kind"), 120),
                scope=_text(metadata.get("scope"), 20),
                status=_text(metadata.get("status"), 40),
                confidence=_number(metadata.get("confidence")),
                sensitivity=_text(metadata.get("sensitivity"), 20),
                valid_from=valid_from,
                valid_until=valid_until,
                sources=sources,
            )
        except (TypeError, ValueError) as error:
            raise _ParseError(line) from error
        block = match.group(0).encode("utf-8")
        items.append(_ParsedUnit(document, hashlib.sha256(block).hexdigest()))
        cursor = match.end()
    if text[cursor:].strip():
        raise _ParseError(_line(text, cursor))
    return tuple(items)


def _sources(value: object, line: int) -> tuple[SourceRef, ...]:
    """严格解析非空、无额外字段的 SourceRef 列表。"""
    if not isinstance(value, list) or not value:
        raise _ParseError(line)
    result: list[SourceRef] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != _SOURCE_KEYS:
            raise _ParseError(line)
        try:
            result.append(
                SourceRef(
                    item["message_id"],
                    item["session_id"],
                    item["channel"],
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise _ParseError(line) from error
    if len({source.message_id for source in result}) != len(result):
        raise _ParseError(line)
    return tuple(result)


def _validate_sources(
    connection: sqlite3.Connection,
    owner_id: int,
    sources: tuple[SourceRef, ...],
) -> None:
    """在同一对账事务内核验每个 Message/Session/Channel/Owner。"""
    for source in sources:
        row = connection.execute(
            """
            SELECT sessions.user_id, sessions.channel
            FROM messages JOIN sessions ON sessions.id = messages.session_id
            WHERE messages.id = ? AND messages.session_id = ?
            """,
            (source.message_id, source.session_id),
        ).fetchone()
        if row is None or int(row["user_id"]) != owner_id or row["channel"] != source.channel:
            raise _ParseError(1)


def _changed(
    row: sqlite3.Row,
    document: MarkdownUnitDocument,
    text_hash: str,
    block_hash: str,
) -> bool:
    """比较可由 Markdown 控制的 Projection 字段。"""
    return (
        str(row["memory_key"]) != document.key
        or str(row["text"]) != document.text.strip()
        or str(row["text_hash"]) != text_hash
        or str(row["kind"]) != document.kind
        or str(row["scope"]) != document.scope
        or str(row["status"]) != document.status
        or float(row["confidence"]) != float(document.confidence)
        or str(row["sensitivity"]) != document.sensitivity
        or str(row["valid_from"]) != document.valid_from.isoformat()
        or row["valid_until"]
        != (None if document.valid_until is None else document.valid_until.isoformat())
        or row["markdown_hash"] != block_hash
    )


def _parse_time(value: object, line: int) -> datetime:
    """解析带时区 ISO 时间并统一为 UTC。"""
    if not isinstance(value, str):
        raise _ParseError(line)
    try:
        return _aware(datetime.fromisoformat(value))
    except ValueError as error:
        raise _ParseError(line) from error


def _aware(value: datetime) -> datetime:
    """要求 timezone-aware datetime 并统一 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("memory reconcile time must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: object, maximum: int) -> str:
    """收窄 metadata 中的有界非空文本。"""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError("memory metadata text is invalid")
    return value.strip()


def _number(value: object) -> float:
    """收窄 metadata 中 0..1 的非 bool 数字。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError("memory metadata number is invalid")
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError("memory metadata number is invalid")
    return result


def _line(text: str, offset: int) -> int:
    """把字符偏移转换为一开始行号。"""
    return text.count("\n", 0, offset) + 1


def _reject_constant(value: str) -> object:
    """拒绝 NaN/Infinity 等非标准 JSON 常量。"""
    del value
    raise ValueError("non-standard JSON constant")
