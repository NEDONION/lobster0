"""Phase 0 已落地数据的窄 Repository。"""

from dataclasses import dataclass
from datetime import UTC, datetime

from miniclaw.storage.database import Database


@dataclass(frozen=True, slots=True)
class Owner:
    """表示 MiniClaw 单用户实例的唯一所有者。"""

    id: int
    display_name: str
    created_at: datetime


class OwnerRepository:
    """以事务方式读取或创建唯一 Owner。"""

    def __init__(self, database: Database) -> None:
        """绑定一个已经完成迁移的数据库。

        Args:
            database: Owner 数据所在的 SQLite 数据库。
        """
        self._database = database

    def get_or_create(self, display_name: str = "Owner") -> Owner:
        """返回现有 Owner；仅在 users 为空时插入一行。

        Args:
            display_name: 首次创建 Owner 时使用的显示名。

        Returns:
            持久化后的 Owner；重复调用不会改名或新增。
        """
        with self._database.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, display_name, created_at FROM users ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                created_at = datetime.now(UTC)
                cursor = connection.execute(
                    "INSERT INTO users (display_name, created_at) VALUES (?, ?)",
                    (display_name, created_at.isoformat()),
                )
                return Owner(
                    id=cursor.lastrowid,
                    display_name=display_name,
                    created_at=created_at,
                )
            return Owner(
                id=row["id"],
                display_name=row["display_name"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )
