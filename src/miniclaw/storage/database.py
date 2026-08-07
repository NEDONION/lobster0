"""SQLite 连接生命周期与每连接安全配置。"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


class DatabaseError(RuntimeError):
    """表示 SQLite 数据库无法打开或初始化连接。"""


@dataclass(frozen=True, slots=True)
class Database:
    """为一个磁盘 SQLite 文件创建短生命周期连接。"""

    path: Path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """打开配置好外键、WAL 和超时的连接，并在退出时关闭。

        Yields:
            启用 ``sqlite3.Row`` 和 Phase 0 PRAGMA 的连接。

        Raises:
            DatabaseError: 数据库文件无法打开或 PRAGMA 初始化失败。
            Exception: 调用方事务中的异常会在回滚后原样抛出。
        """
        try:
            connection = sqlite3.connect(self.path, timeout=5.0)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA busy_timeout = 5000")
        except sqlite3.Error as error:
            if "connection" in locals():
                connection.close()
            raise DatabaseError(f"cannot open MiniClaw database at {self.path}") from error

        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()
