"""MiniClaw SQLite Schema 的显式单向迁移。"""

import sqlite3
from datetime import UTC, datetime
from importlib import resources

from miniclaw.storage.database import Database

LATEST_SCHEMA_VERSION = 6

_MIGRATION_RESOURCES = {
    1: "schema.sql",
    2: "migrations/0002_feishu_channel.sql",
    3: "migrations/0003_memory_autopilot.sql",
    4: "migrations/0004_memory_maintenance.sql",
    5: "migrations/0005_autonomy.sql",
    6: "migrations/0006_artifacts.sql",
}


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
    applying_version = 0
    applied_now: list[int] = []
    try:
        with database.connect() as connection:
            _ensure_migration_table(connection)
            connection.commit()
            applied = {
                int(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            newest_version = max(applied, default=0)
            if newest_version > LATEST_SCHEMA_VERSION:
                raise MigrationError(
                    f"database uses newer schema version {newest_version}; "
                    f"this MiniClaw supports {LATEST_SCHEMA_VERSION}"
                )
            if applied != set(range(1, newest_version + 1)):
                raise MigrationError("database schema migration history is not contiguous")

            for version in range(1, LATEST_SCHEMA_VERSION + 1):
                if version in applied:
                    continue
                applying_version = version
                statements = _split_statements(_load_migration(version))
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                        (version, datetime.now(UTC).isoformat()),
                    )
                except BaseException:
                    connection.rollback()
                    raise
                else:
                    connection.commit()
                    applied_now.append(version)
    except MigrationError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise MigrationError(
            f"failed to apply MiniClaw schema migration {applying_version}"
        ) from error
    return tuple(applied_now)


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
    return _load_migration(1)


def _load_migration(version: int) -> str:
    """读取指定版本的内置 SQL 资源。"""
    try:
        relative_path = _MIGRATION_RESOURCES[version]
    except KeyError as error:
        raise MigrationError(f"missing MiniClaw schema migration {version}") from error
    resource = resources.files("miniclaw.storage")
    for part in relative_path.split("/"):
        resource = resource.joinpath(part)
    return resource.read_text(encoding="utf-8")


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
