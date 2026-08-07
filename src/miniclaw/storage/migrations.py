"""MiniClaw SQLite Schema 的显式单向迁移。"""

import sqlite3
from datetime import UTC, datetime
from importlib import resources

from miniclaw.storage.database import Database

LATEST_SCHEMA_VERSION = 1


class MigrationError(RuntimeError):
    """表示 Schema 资源无效或数据库迁移失败。"""


def apply_migrations(database: Database) -> tuple[int, ...]:
    """按版本顺序应用尚未执行的数据库迁移。

    Args:
        database: 目标 SQLite 数据库。

    Returns:
        本次实际应用的版本号。

    Raises:
        MigrationError: Schema 无法读取、解析或执行。
    """
    try:
        schema = _load_initial_schema()
        statements = _split_statements(schema)
        with database.connect() as connection:
            _ensure_migration_table(connection)
            applied = {
                row[0] for row in connection.execute("SELECT version FROM schema_migrations")
            }
            newest_version = max(applied, default=0)
            if newest_version > LATEST_SCHEMA_VERSION:
                raise MigrationError(
                    f"database uses newer schema version {newest_version}; "
                    f"this MiniClaw supports {LATEST_SCHEMA_VERSION}"
                )
            if LATEST_SCHEMA_VERSION in applied:
                return ()

            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (LATEST_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )
    except (OSError, sqlite3.Error) as error:
        raise MigrationError("failed to apply MiniClaw schema migration 1") from error
    return (LATEST_SCHEMA_VERSION,)


def current_schema_version(database: Database) -> int:
    """返回数据库已经应用的最高 Schema 版本，未初始化时返回 0。

    Args:
        database: 需要查询的 SQLite 数据库。

    Returns:
        最高迁移版本；迁移表不存在时为 0。
    """
    with database.connect() as connection:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if exists is None:
            return 0
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
    return int(row[0])


def _ensure_migration_table(connection: sqlite3.Connection) -> None:
    """创建迁移账本，使空数据库可以判断待执行版本。"""
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )


def _load_initial_schema() -> str:
    """从安装包资源读取版本 1 Schema 文本。"""
    return resources.files("miniclaw.storage").joinpath("schema.sql").read_text(encoding="utf-8")


def _split_statements(script: str) -> tuple[str, ...]:
    """使用 SQLite 自身的完整语句检测拆分 SQL 脚本。"""
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""
    if buffer.strip():
        raise MigrationError("schema.sql ends with an incomplete SQL statement")
    return tuple(statements)
