"""SQLite 数据库安全更新：备份、变更守卫与原子回滚。

Task 13 交付 `UpdateCoordinator`：在停止旧 service 后备份数据库，趁旧
service 停止期间运行新 Runtime 的 schema migration，再原子切换到新
Runtime 并刷新/健康检查新 service。任一阶段失败都先停止新 service，
再用 `DatabaseChangeGuard`（基于 SQLite ``PRAGMA data_version``）判断
migration 之后是否已经发生过外部写入：

- 没有外部写入 —— 销毁性恢复数据库备份、旧 Runtime 与旧 service，
  然后重新抛出触发失败的原始异常；
- 已经发生外部写入 —— 绝不覆盖这些数据，保留备份与当前（新）状态，
  转而抛出 ``rollback_conflict``，交给运维人工判断。

本模块只依赖调用方注入的路径与副作用回调，不了解 `InstallLayout`、
`RuntimeReceipt` 或 service manager 的具体实现；因此绝不会读取或写入
Memory、Skills、Workspace 或任何 `state_home` 下除数据库与其备份文件
之外的路径。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lobster0.install.models import InstallError

_CONNECT_TIMEOUT = 5.0


@dataclass(slots=True)
class DatabaseChangeGuard:
    """用一个持续只读连接检测预期 migration 之后发生的新 commit。

    Args:
        connection: 事务全程持有的只读 SQLite 连接。
        expected_data_version: 当前判定基线使用的 ``PRAGMA data_version``。
    """

    connection: sqlite3.Connection
    expected_data_version: int

    @classmethod
    def open(cls, database: Path) -> DatabaseChangeGuard:
        """打开覆盖整个 update 事务生命周期的只读监控连接。

        Args:
            database: 必须已经存在的当前存活 SQLite 数据库文件。

        Returns:
            以迁移前 ``data_version`` 为初始基线的守卫。

        Raises:
            InstallError: 路径类型不对、数据库缺失或连接/查询失败。
        """
        if not isinstance(database, Path):
            raise InstallError("request_invalid", "manifest")
        if not database.is_file():
            raise InstallError("request_invalid", "manifest")
        uri = f"{database.resolve(strict=True).as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(uri, timeout=_CONNECT_TIMEOUT, uri=True)
        except sqlite3.Error as error:
            raise InstallError("installer_error", "manifest") from error
        try:
            version = _read_data_version(connection)
        except BaseException:
            connection.close()
            raise
        return cls(connection=connection, expected_data_version=version)

    def refresh_after_expected_migration(self) -> None:
        """把基线更新为 migration 刚完成后的 ``data_version``。

        Raises:
            InstallError: 监控连接已经不可用。
        """
        self.expected_data_version = _read_data_version(self.connection)

    def has_external_commit(self) -> bool:
        """判断当前 ``data_version`` 是否已经偏离基线，即出现新写入。

        Returns:
            自上次 refresh/open 以来发生过至少一次新提交时为 True。

        Raises:
            InstallError: 监控连接已经不可用。
        """
        return _read_data_version(self.connection) != self.expected_data_version

    def close(self) -> None:
        """关闭监控连接；事务结束（无论成败）后必须调用一次。"""
        try:
            self.connection.close()
        except sqlite3.Error:
            pass


def _read_data_version(connection: sqlite3.Connection) -> int:
    """读取当前连接可见的 SQLite ``data_version``。"""
    try:
        row = connection.execute("PRAGMA data_version").fetchone()
        return int(row[0])
    except sqlite3.Error as error:
        raise InstallError("installer_error", "manifest") from error


def backup_database(source: Path, destination: Path) -> None:
    """用 ``sqlite3.Connection.backup`` 创建 owner-only 备份文件并 fsync。

    这个函数总是被无条件调用 —— 即便目标 schema 版本与当前完全相同，
    也绝不允许跳过备份；调用方不得为“兼容 schema”添加短路优化。

    Args:
        source: 当前存活、必须已经存在的 SQLite 数据库文件。
        destination: 尚不存在的 owner-only 备份文件路径。

    Raises:
        InstallError: source 缺失、destination 已存在或备份/刷盘失败。
    """
    if not isinstance(source, Path) or not isinstance(destination, Path):
        raise InstallError("request_invalid", "manifest")
    if not source.is_file():
        raise InstallError("request_invalid", "manifest")
    if destination.is_symlink() or destination.exists():
        raise InstallError("request_invalid", "manifest")
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as error:
        raise InstallError("installer_error", "manifest") from error
    os.close(descriptor)
    try:
        source_connection = sqlite3.connect(str(source), timeout=_CONNECT_TIMEOUT)
        try:
            destination_connection = sqlite3.connect(str(destination), timeout=_CONNECT_TIMEOUT)
            try:
                source_connection.backup(destination_connection)
            finally:
                destination_connection.close()
        finally:
            source_connection.close()
        _fsync_file(destination)
        _fsync_directory(destination.parent)
    except BaseException:
        _unlink_if_exists(destination)
        raise InstallError("installer_error", "manifest") from None


def restore_database(backup: Path, destination: Path) -> None:
    """把 owner-only 备份原子恢复到 destination：临时文件 + fsync + replace。

    `backup_database` 用 ``sqlite3.Connection.backup`` 生成备份，产出的是
    一个已经把 WAL 内容合并进主文件、自包含的单文件快照。恢复时如果
    `destination` 旁边还留有失败 migration 遗留的 ``-wal``/``-shm``
    sidecar，它们描述的是被替换掉的旧主文件内容；一旦主文件被替换而
    sidecar 未清理，下一次打开会用不匹配的 WAL 帧去恢复一个它们从未
    见过的主文件，存在损坏风险。因此替换主文件之后必须立即清理这两个
    sidecar，让恢复后的数据库回到一个自洽的冷启动状态。

    Args:
        backup: 已确认存在的 owner-only 备份文件。
        destination: 待恢复覆盖的当前数据库路径。

    Raises:
        InstallError: backup 缺失或恢复/刷盘失败。
    """
    if not isinstance(backup, Path) or not isinstance(destination, Path):
        raise InstallError("request_invalid", "manifest")
    if not backup.is_file():
        raise InstallError("request_invalid", "manifest")
    temporary = destination.with_name(f".{destination.name}.restore.{os.getpid()}")
    _unlink_if_exists(temporary)
    try:
        payload = backup.read_bytes()
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, destination)
        _unlink_if_exists(destination.with_name(f"{destination.name}-wal"))
        _unlink_if_exists(destination.with_name(f"{destination.name}-shm"))
        _fsync_directory(destination.parent)
    except BaseException:
        _unlink_if_exists(temporary)
        raise InstallError("installer_error", "manifest") from None


def _fsync_file(path: Path) -> None:
    """刷盘一个既有普通文件的内容。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    """刷盘一个目录的元数据，使其中的 rename/replace 持久化。"""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_exists(path: Path) -> None:
    """删除一个可能不存在的普通文件。"""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@dataclass(frozen=True, slots=True)
class UpdateRequest:
    """绑定一次 update 事务需要的全部路径与受控副作用回调。

    本类型刻意不知道 `InstallLayout`、`RuntimeReceipt` 或具体 service
    manager；调用方（`lobster0.install.orchestrator`）负责把每个回调
    绑定成真实的 Task 8/10 原语。除 `database`/`backup_path` 外，任何
    回调都绝不能读取或写入 `state_home` 下的 Memory、Skills、Workspace
    或其他用户数据路径。

    Args:
        database: 当前存活的 SQLite 数据库文件。
        backup_path: 本次事务专用、尚不存在的 owner-only 备份路径。
        stop_old_service: 停止旧 Runtime service 的回调；旧 service 未
            安装时必须是安全 no-op。
        run_migration: 在旧 service 已停止期间运行新 Runtime schema
            migration 的回调。
        activate_new_runtime: 原子切换 `current` 到新 Runtime 的回调，
            不涉及 receipt。
        commit_receipt: 在 `activate_new_runtime` 成功之后，写入反映新
            current/previous 的 install receipt 的回调；与 Task 8/11
            fresh-install 的 activate→commit 顺序保持一致。
        revert_to_old_runtime: 只在 `activate_new_runtime` 已经成功后
            才会被调用（无论 `commit_receipt` 是否也成功），把 `current`
            与 receipt 都恢复到旧 Runtime；必须使用「仍指向本次 target
            才恢复」式的安全检查，并且必须是幂等的 —— `commit_receipt`
            从未运行时，重写同一份旧 receipt 仍然要安全。
        start_new_service: 启动/刷新新 Runtime service 并等待 bounded
            health 的回调；超时应当返回 False 而不是抛出，但也允许
            为其他基础设施失败直接抛出 `InstallError`。
        stop_new_service: 停止新 Runtime service 的回调；新 service 从未
            成功启动时也必须是安全 no-op。
        restart_old_service: 恢复路径里重新拉起旧 Runtime service 的
            回调；旧 service 未安装时必须是安全 no-op。
        retain_runtimes: 仅在整个事务成功后调用一次，只保留 current 与
            N-1 Runtime。
        health_timeout: 等待新 service health 的建议最长秒数；具体
            manager 若使用固定超时，可以安全忽略该值。

    Raises:
        InstallError: 路径类型、缺失回调或超时不可信。
    """

    database: Path
    backup_path: Path
    stop_old_service: Callable[[], None]
    run_migration: Callable[[], None]
    activate_new_runtime: Callable[[], None]
    commit_receipt: Callable[[], None]
    revert_to_old_runtime: Callable[[], None]
    start_new_service: Callable[[float], bool]
    stop_new_service: Callable[[], None]
    restart_old_service: Callable[[], None]
    retain_runtimes: Callable[[], None]
    health_timeout: float

    def __post_init__(self) -> None:
        """拒绝错误类型的路径、回调或超时。"""
        callables = (
            self.stop_old_service,
            self.run_migration,
            self.activate_new_runtime,
            self.commit_receipt,
            self.revert_to_old_runtime,
            self.start_new_service,
            self.stop_new_service,
            self.restart_old_service,
            self.retain_runtimes,
        )
        if (
            not isinstance(self.database, Path)
            or not isinstance(self.backup_path, Path)
            or any(not callable(value) for value in callables)
            or type(self.health_timeout) not in (int, float)
            or self.health_timeout <= 0
        ):
            raise InstallError("request_invalid", "manifest")


class UpdateCoordinator:
    """在数据库备份/守卫保护下协调一次 update 的 activation 与 rollback。"""

    def update(self, request: UpdateRequest) -> None:
        """执行一次 DB-guarded 原子 update，失败时安全回滚或返回 conflict。

        Args:
            request: 已绑定全部路径与副作用回调的 update 请求。

        Raises:
            InstallError: 任一阶段失败。安全（无外部写入）时重新抛出
                导致失败的原始异常，并已经完成销毁性数据库恢复与旧
                Runtime/service 回滚；检测到 migration 之后的外部写入
                时改为抛出 `rollback_conflict`，且不会做任何销毁性
                恢复 —— 备份与新状态都原样保留供人工处理。
        """
        if type(request) is not UpdateRequest:
            raise InstallError("request_invalid", "manifest")
        request.stop_old_service()
        try:
            guard = DatabaseChangeGuard.open(request.database)
        except BaseException as error:
            _best_effort(request.restart_old_service)
            if isinstance(error, InstallError):
                raise
            raise InstallError("installer_error", "manifest") from error
        stage = "stopped"
        try:
            try:
                backup_database(request.database, request.backup_path)
                stage = "backed_up"
                request.run_migration()
                stage = "migrated"
                guard.refresh_after_expected_migration()
                request.activate_new_runtime()
                stage = "activated"
                request.commit_receipt()
                stage = "committed"
                healthy = request.start_new_service(request.health_timeout)
                if type(healthy) is not bool or not healthy:
                    raise InstallError("activation_failed", "service")
                stage = "service_started"
            except BaseException as error:
                self._recover(request, guard, stage, error)
        finally:
            guard.close()
        request.retain_runtimes()

    def _recover(
        self,
        request: UpdateRequest,
        guard: DatabaseChangeGuard,
        stage: str,
        error: BaseException,
    ) -> None:
        """按失败阶段决定安全销毁性恢复，或保留 conflict 状态。

        总是重新抛出：清理路径下重新抛出触发失败的原始异常；一旦守卫
        检测到 migration 之后的外部写入，则改为抛出 `rollback_conflict`。
        """
        _best_effort(request.stop_new_service)
        if stage == "stopped":
            _best_effort(request.restart_old_service)
            self._raise_original(error)
            return
        if stage != "backed_up" and guard.has_external_commit():
            raise InstallError("rollback_conflict", "manifest") from error
        restore_database(request.backup_path, request.database)
        if stage in ("activated", "committed", "service_started"):
            _best_effort(request.revert_to_old_runtime)
        _best_effort(request.restart_old_service)
        self._raise_original(error)

    @staticmethod
    def _raise_original(error: BaseException) -> None:
        """原样重新抛出已捕获的原始异常。"""
        if isinstance(error, InstallError):
            raise error
        raise InstallError("installer_error", "manifest") from error


def _best_effort(action: Callable[[], None]) -> None:
    """执行 recovery-path 副作用，吞掉异常以保留原始失败信息。"""
    try:
        action()
    except BaseException:
        pass
