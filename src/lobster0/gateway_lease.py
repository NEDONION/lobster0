"""同一 Lobster0 状态目录的 Gateway 单实例 lease。"""

import fcntl
import json
import os
import re
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


class GatewayLeaseError(RuntimeError):
    """表示 Gateway lease 只能公开的稳定失败码。"""

    def __init__(self, code: str) -> None:
        """保存不包含路径、PID 或底层异常的错误码。

        Args:
            code: 对 CLI 和测试稳定的安全错误码。
        """
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class GatewayProvenance:
    """保存当前本地 Gateway 进程的有限运行来源。"""

    pid: int
    started_at: str
    commit: str


class GatewayLease:
    """持有一个普通私有文件上的进程级 advisory lock。"""

    def __init__(self, descriptor: int, provenance: GatewayProvenance) -> None:
        """保存已成功加锁的 descriptor 和 provenance。

        Args:
            descriptor: 当前进程独占持有的文件描述符。
            provenance: 已写入 lease 文件的本地运行来源。
        """
        self._descriptor: int | None = descriptor
        self.provenance = provenance

    @classmethod
    def acquire(cls, path: Path, *, commit: str) -> "GatewayLease":
        """安全创建或复用 lease 文件并尝试非阻塞独占锁。

        Args:
            path: 状态目录中的绝对 lease 文件路径。
            commit: Live Runner 提供的 40 位源码 commit；其他值记录为 unknown。

        Returns:
            持有独占锁的 lease；调用方必须最终执行 :meth:`close`。

        Raises:
            GatewayLeaseError: 路径不安全、已有实例或本地文件不可用。
        """
        if not isinstance(path, Path) or not path.is_absolute():
            raise GatewayLeaseError("gateway_lease_unsafe")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            code = (
                "gateway_lease_unsafe"
                if error.errno in {getattr(os, "ELOOP", -1), getattr(os, "EISDIR", -1)}
                or path.is_symlink()
                or path.is_dir()
                else "gateway_lease_unavailable"
            )
            raise GatewayLeaseError(code) from None

        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode) != 0o600:
                raise GatewayLeaseError("gateway_lease_unsafe")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise GatewayLeaseError("gateway_already_running") from None
            provenance = GatewayProvenance(
                pid=os.getpid(),
                started_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                commit=commit if _COMMIT.fullmatch(commit) is not None else "unknown",
            )
            _write_payload(descriptor, provenance)
            return cls(descriptor, provenance)
        except GatewayLeaseError:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
            raise
        except OSError:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(descriptor)
            raise GatewayLeaseError("gateway_lease_unavailable") from None

    def close(self) -> None:
        """幂等释放 advisory lock 和文件描述符。"""
        descriptor = self._descriptor
        if descriptor is None:
            return
        self._descriptor = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass


def _write_payload(descriptor: int, provenance: GatewayProvenance) -> None:
    """覆盖写入有限 JSON，并确保磁盘内容与当前持锁者一致。

    Args:
        descriptor: 已验证并持锁的普通文件描述符。
        provenance: 当前进程的有限运行来源。

    Raises:
        OSError: 截断、写入或同步失败。
    """
    payload = (
        json.dumps(
            {
                "schema_version": 1,
                "pid": provenance.pid,
                "started_at": provenance.started_at,
                "commit": provenance.commit,
            },
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    os.ftruncate(descriptor, 0)
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("short lease write")
        remaining = remaining[written:]
    os.fsync(descriptor)
