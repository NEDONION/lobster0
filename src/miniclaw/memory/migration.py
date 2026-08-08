"""把 legacy MEMORY.md/daily 文件只读导入 source-bound Memory Unit。"""

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from miniclaw.memory.markdown_store import MarkdownUnitDocument, MemoryMarkdownStore
from miniclaw.memory.models import SourceRef
from miniclaw.memory.repository import MemoryStateError, MemoryUnitRepository
from miniclaw.memory.store import MemoryError, MemoryStore
from miniclaw.paths import StatePaths
from miniclaw.storage.database import Database

_DAILY_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
_IGNORED_FIELDS = (
    "source_session:",
    "source:",
    "confidence:",
    "content_sha256:",
)


@dataclass(frozen=True, slots=True)
class LegacyMigrationResult:
    """汇总 legacy 文件导入的稳定 Unit IDs 与拒绝计数。"""

    unit_ids: tuple[str, ...]
    imported_files: int
    skipped_files: int
    rejected_chunks: int


class LegacyMemoryImporter:
    """按源文件 hash 幂等导入，不修改或删除任何 legacy 文件。"""

    def __init__(
        self,
        paths: StatePaths,
        database: Database,
        markdown: MemoryMarkdownStore,
        units: MemoryUnitRepository,
        legacy_store: MemoryStore,
    ) -> None:
        """绑定固定路径、数据库、Markdown 真相和 Secret validator。"""
        self._paths = paths
        self._database = database
        self._markdown = markdown
        self._units = units
        self._legacy_store = legacy_store

    def import_all(
        self,
        owner_id: int,
        *,
        now: datetime | None = None,
    ) -> LegacyMigrationResult:
        """扫描固定 long-term/daily 文件并返回跨重启稳定结果。"""
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError("memory owner_id is invalid")
        current = _aware(now or datetime.now(UTC))
        unit_ids: list[str] = []
        imported_files = 0
        skipped_files = 0
        rejected_chunks = 0
        for relative_path, path in self._source_paths():
            payload = _read_source(path)
            if payload is None:
                continue
            content_hash = hashlib.sha256(payload).hexdigest()
            previous = self._existing(owner_id, relative_path, content_hash)
            if previous is not None:
                unit_ids.extend(previous[0])
                rejected_chunks += previous[1]
                if previous[0]:
                    imported_files += 1
                else:
                    skipped_files += 1
                continue
            text = payload.decode("utf-8")
            chunks = _chunks(text)
            accepted: list[str] = []
            rejected = 0
            for chunk in chunks:
                try:
                    fact, _ = self._legacy_store.validate_candidate(
                        chunk,
                        "legacy manual memory",
                    )
                except MemoryError:
                    rejected += 1
                    continue
                accepted.append(fact)
            source = (
                None
                if not accepted
                else self._source_message(owner_id, relative_path, content_hash, current)
            )
            file_units: list[str] = []
            for ordinal, fact in enumerate(accepted):
                existing = self._units.find_by_text(owner_id, fact)
                if existing is not None:
                    file_units.append(existing.id)
                    continue
                assert source is not None
                unit_id = _unit_id(owner_id, content_hash, ordinal, fact)
                document = MarkdownUnitDocument(
                    unit_id=unit_id,
                    owner_id=owner_id,
                    key=f"legacy.{content_hash[:16]}.{ordinal}",
                    text=fact,
                    kind="legacy_manual",
                    scope="private",
                    status="active",
                    confidence=1.0,
                    sensitivity="low",
                    valid_from=current,
                    valid_until=None,
                    sources=(source,),
                )
                write = self._markdown.append(document)
                try:
                    unit = self._units.create(
                        unit_id=unit_id,
                        owner_id=owner_id,
                        key=document.key,
                        text=fact,
                        kind=document.kind,
                        scope=document.scope,
                        status=document.status,
                        confidence=document.confidence,
                        sensitivity=document.sensitivity,
                        valid_from=current,
                        valid_until=None,
                        sources=(source,),
                        markdown_hash=write.block_hash,
                        now=current,
                    )
                except MemoryStateError:
                    recovered = self._units.find(owner_id, unit_id)
                    if recovered is None:
                        raise
                    unit = recovered
                file_units.append(unit.id)
            self._record(
                owner_id,
                relative_path,
                content_hash,
                None if source is None else source.message_id,
                tuple(file_units),
                rejected,
                current,
            )
            unit_ids.extend(file_units)
            rejected_chunks += rejected
            if file_units:
                imported_files += 1
            else:
                skipped_files += 1
        return LegacyMigrationResult(
            tuple(unit_ids),
            imported_files,
            skipped_files,
            rejected_chunks,
        )

    def _source_paths(self) -> tuple[tuple[str, Path], ...]:
        """按稳定顺序返回固定 long-term 与根级 daily 文件。"""
        daily = tuple(
            (f"memory/{path.name}", path)
            for path in sorted(self._paths.memory_dir.glob("*.md"))
            if _DAILY_FILE.fullmatch(path.name) is not None
        )
        return (("MEMORY.md", self._paths.memory_file), *daily)

    def _existing(
        self,
        owner_id: int,
        relative_path: str,
        content_hash: str,
    ) -> tuple[tuple[str, ...], int] | None:
        """读取已完成 import 的稳定 Unit IDs 和拒绝计数。"""
        with self._database.connect_read_only() as connection:
            row = connection.execute(
                """
                SELECT unit_ids_json, rejected_chunks FROM memory_legacy_imports
                WHERE owner_id = ? AND relative_path = ? AND content_hash = ?
                """,
                (owner_id, relative_path, content_hash),
            ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(str(row["unit_ids_json"]))
        except json.JSONDecodeError as error:
            raise MemoryStateError("legacy memory import record is invalid") from error
        if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
            raise MemoryStateError("legacy memory import record is invalid")
        return tuple(raw), int(row["rejected_chunks"])

    def _source_message(
        self,
        owner_id: int,
        relative_path: str,
        content_hash: str,
        now: datetime,
    ) -> SourceRef:
        """幂等创建只含文件标签/hash、不含原文的 synthetic User source。"""
        timestamp = now.isoformat()
        event_id = f"legacy-memory:{content_hash}"
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sessions (
                    user_id, channel, account_id, external_conversation_id,
                    title, status, created_at, updated_at
                ) VALUES (?, 'cli', 'local', 'legacy-memory-import',
                    'Legacy Memory Import', 'active', ?, ?)
                ON CONFLICT(channel, account_id, external_conversation_id) DO NOTHING
                """,
                (owner_id, timestamp, timestamp),
            )
            session = connection.execute(
                """
                SELECT id, user_id FROM sessions
                WHERE channel = 'cli' AND account_id = 'local'
                    AND external_conversation_id = 'legacy-memory-import'
                """
            ).fetchone()
            if session is None or int(session["user_id"]) != owner_id:
                raise MemoryStateError("legacy memory source session is invalid")
            session_id = int(session["id"])
            connection.execute(
                """
                INSERT INTO turns (
                    session_id, inbound_event_id, status, model,
                    started_at, completed_at, runtime_snapshot_json
                ) VALUES (?, ?, 'completed', 'legacy-import', ?, ?, '{}')
                ON CONFLICT(session_id, inbound_event_id) DO NOTHING
                """,
                (session_id, event_id, timestamp, timestamp),
            )
            turn = connection.execute(
                "SELECT id FROM turns WHERE session_id = ? AND inbound_event_id = ?",
                (session_id, event_id),
            ).fetchone()
            assert turn is not None
            turn_id = int(turn["id"])
            connection.execute(
                """
                INSERT INTO messages (
                    session_id, turn_id, role, content, metadata_json, created_at
                )
                SELECT ?, ?, 'user', ?, ?, ?
                WHERE NOT EXISTS (SELECT 1 FROM messages WHERE turn_id = ? AND role = 'user')
                """,
                (
                    session_id,
                    turn_id,
                    f"legacy source sha256:{content_hash}",
                    json.dumps(
                        {
                            "legacy_source_hash": content_hash,
                            "legacy_source_path": relative_path,
                        },
                        separators=(",", ":"),
                    ),
                    timestamp,
                    turn_id,
                ),
            )
            message = connection.execute(
                "SELECT id FROM messages WHERE turn_id = ? AND role = 'user'",
                (turn_id,),
            ).fetchone()
        assert message is not None
        return SourceRef(int(message["id"]), session_id, "cli")

    def _record(
        self,
        owner_id: int,
        relative_path: str,
        content_hash: str,
        source_message_id: int | None,
        unit_ids: tuple[str, ...],
        rejected_chunks: int,
        now: datetime,
    ) -> None:
        """原子记录 hash 级幂等账本和脱敏迁移审计。"""
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO memory_legacy_imports (
                    owner_id, relative_path, content_hash, source_message_id,
                    unit_ids_json, status, rejected_chunks, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(owner_id, relative_path, content_hash) DO NOTHING
                """,
                (
                    owner_id,
                    relative_path,
                    content_hash,
                    source_message_id,
                    json.dumps(unit_ids, separators=(",", ":")),
                    "imported" if unit_ids else "skipped",
                    rejected_chunks,
                    now.isoformat(),
                ),
            )
            connection.execute(
                """
                INSERT INTO memory_audit (
                    owner_id, event_type, reason_code, metadata_json, created_at
                ) VALUES (?, 'legacy_imported', ?, ?, ?)
                """,
                (
                    owner_id,
                    "imported" if unit_ids else "skipped",
                    json.dumps(
                        {
                            "content_hash": content_hash,
                            "rejected_chunks": rejected_chunks,
                            "unit_count": len(unit_ids),
                        },
                        separators=(",", ":"),
                    ),
                    now.isoformat(),
                ),
            )


def _read_source(path: Path) -> bytes | None:
    """安全读取固定 legacy 文件；缺失返回 None。"""
    if path.is_symlink():
        raise MemoryError("unsafe_memory_path", "legacy memory path is not safe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError as error:
        raise MemoryError("unsafe_memory_path", "legacy memory path is not safe") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise MemoryError("invalid_memory", "legacy memory file is invalid")
        payload = os.read(descriptor, metadata.st_size + 1)
        payload.decode("utf-8")
        return payload
    except (OSError, UnicodeError) as error:
        raise MemoryError("memory_read_failed", "legacy memory file could not be read") from error
    finally:
        os.close(descriptor)


def _chunks(text: str) -> tuple[str, ...]:
    """从 legacy Markdown 提取有界事实行/段落，跳过旧元数据。"""
    chunks: list[str] = []
    paragraph: list[str] = []

    def flush() -> None:
        """把当前普通段落合并为一个候选。"""
        if paragraph:
            chunks.append(" ".join(paragraph))
            paragraph.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            continue
        if line.startswith("#"):
            flush()
            continue
        if line.startswith("- fact:"):
            flush()
            value = line.removeprefix("- fact:").strip()
            if value:
                chunks.append(value)
            continue
        if line.startswith("- "):
            flush()
            value = line[2:].strip()
            if value:
                chunks.append(value)
            continue
        if line.startswith(_IGNORED_FIELDS):
            continue
        paragraph.append(line)
    flush()
    return tuple(chunks)


def _unit_id(owner_id: int, content_hash: str, ordinal: int, fact: str) -> str:
    """从 Owner、源文件 hash、序号和事实生成稳定 legacy Unit ID。"""
    digest = hashlib.sha256(
        f"{owner_id}\0{content_hash}\0{ordinal}\0{fact}".encode()
    ).hexdigest()
    return f"legacy-{digest[:24]}"


def _aware(value: datetime) -> datetime:
    """要求 timezone-aware datetime 并统一为 UTC。"""
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("legacy migration time must be timezone-aware")
    return value.astimezone(UTC)
