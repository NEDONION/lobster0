"""生产 Live Gate 共用的最小私有 Evidence 文件边界。"""

import json
import os
import re
import stat
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

_MAX_SCAN_FILES = 1000
_MAX_SCAN_FILE_BYTES = 1024 * 1024
_FORBIDDEN_KEYS = frozenset(
    {
        "chat_id",
        "content",
        "home_path",
        "message",
        "message_content",
        "message_id",
        "open_id",
        "path",
        "prompt",
        "provider_payload",
        "raw",
        "request",
        "response",
        "tool_arguments",
        "user_id",
        "workspace_path",
    }
)


class ProductionEvidenceError(RuntimeError):
    """表示私有 Evidence 边界返回的稳定错误码。"""

    def __init__(self, code: str) -> None:
        """保存不含文件路径或数据正文的错误码。

        Args:
            code: 稳定小写错误码。
        """
        self.code = code
        super().__init__(code)


def validate_commit(value: object) -> str:
    """校验并规范化完整 Git SHA-1。

    Args:
        value: 待校验值。

    Returns:
        小写 40-hex commit。

    Raises:
        ProductionEvidenceError: 输入不是完整 SHA-1。
    """
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{40}", value) is None:
        raise ProductionEvidenceError("invalid_commit")
    return value.lower()


def utc_timestamp() -> str:
    """返回带微秒的 UTC Evidence 时间戳。"""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_private_json(path: Path, payload: Mapping[str, object]) -> None:
    """以 owner-only、exclusive、no-follow 方式写一份标准 JSON。

    Args:
        path: 已存在 owner-only 目录中的新文件目标。
        payload: 已由业务层收窄的 JSON object。

    Raises:
        ProductionEvidenceError: 目录、JSON、目标文件或持久化操作不安全。
    """
    _validate_private_parent(path.parent)
    if not _safe_json_value(payload):
        raise ProductionEvidenceError("invalid_evidence_payload")
    try:
        rendered = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError):
        raise ProductionEvidenceError("invalid_evidence_payload") from None

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        raise ProductionEvidenceError("evidence_already_exists") from None
    except OSError:
        raise ProductionEvidenceError("evidence_write_failed") from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise ProductionEvidenceError("evidence_write_failed") from None


def scan_secret_matches(paths: Sequence[Path], secrets: Sequence[str]) -> int:
    """有界扫描普通小文件并只返回 exact secret 匿名命中次数。

    Args:
        paths: 要扫描的普通文件或目录。
        secrets: 只保存在内存中的 exact needle。

    Returns:
        前 1000 个候选、每个至多 1 MiB 中的总命中数。

    Raises:
        ProductionEvidenceError: needle 不是有界非空字符串。
    """
    if any(not isinstance(secret, str) or not 1 <= len(secret) <= 4096 for secret in secrets):
        raise ProductionEvidenceError("invalid_secret_scan")
    needles = tuple(dict.fromkeys(secret.encode("utf-8") for secret in secrets))
    if not needles:
        return 0

    matches = 0
    for index, candidate in enumerate(_iter_scan_candidates(paths)):
        if index >= _MAX_SCAN_FILES:
            break
        content = _read_bounded_regular_file(candidate)
        if content is not None:
            matches += sum(content.count(needle) for needle in needles)
    return matches


def _validate_private_parent(path: Path) -> None:
    """要求目标父目录为当前用户拥有且不向 group/other 开放。"""
    try:
        metadata = path.lstat()
    except OSError:
        raise ProductionEvidenceError("evidence_directory_unsafe") from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or path.is_symlink()
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise ProductionEvidenceError("evidence_directory_unsafe")


def _safe_json_value(value: object) -> bool:
    """递归拒绝非标准 JSON 类型与可能承载私密原文的字段。"""
    if value is None or type(value) in {bool, int, float, str}:
        return not isinstance(value, str) or "\x00" not in value
    if isinstance(value, list):
        return all(_safe_json_value(item) for item in value)
    if isinstance(value, Mapping):
        return all(
            isinstance(key, str)
            and key.lower().replace("-", "_") not in _FORBIDDEN_KEYS
            and _safe_json_value(item)
            for key, item in value.items()
        )
    return False


def _iter_scan_candidates(paths: Sequence[Path]) -> Iterator[Path]:
    """按稳定顺序枚举文件候选，从不跟随目录或文件 symlink。"""
    for root in sorted(paths, key=lambda item: str(item)):
        try:
            if root.is_symlink():
                continue
            if root.is_file():
                yield root
                continue
            if not root.is_dir():
                continue
        except OSError:
            continue
        for directory, directory_names, file_names in os.walk(root, followlinks=False):
            base = Path(directory)
            directory_names[:] = sorted(
                name for name in directory_names if not (base / name).is_symlink()
            )
            for name in sorted(file_names):
                yield base / name


def _read_bounded_regular_file(path: Path) -> bytes | None:
    """使用 no-follow fd 读取至多 1 MiB，竞态变大时安全跳过。"""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_SCAN_FILE_BYTES:
            return None
        content = os.read(descriptor, _MAX_SCAN_FILE_BYTES + 1)
        return content if len(content) <= _MAX_SCAN_FILE_BYTES else None
    except OSError:
        return None
    finally:
        os.close(descriptor)
