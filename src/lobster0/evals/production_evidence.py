"""生产 Live Gate 共用的最小私有 Evidence 文件边界。"""

import json
import os
import re
import stat
import subprocess
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
_SEATBELT_PROBES = frozenset({"python", "node-chain"})


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


def clean_repository_commit(root: Path) -> str:
    """返回 clean、非 detached 仓库的完整 commit。

    Args:
        root: 当前 release candidate 仓库根。

    Returns:
        lowercase 40-hex commit。

    Raises:
        ProductionEvidenceError: Git 不可用、HEAD 非法、detached 或工作树不干净。
    """
    try:
        resolved = root.resolve(strict=True)
        head = subprocess.run(
            ("/usr/bin/git", "rev-parse", "--verify", "HEAD"),
            cwd=resolved,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        branch = subprocess.run(
            ("/usr/bin/git", "symbolic-ref", "--quiet", "HEAD"),
            cwd=resolved,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ("/usr/bin/git", "status", "--porcelain", "--untracked-files=normal"),
            cwd=resolved,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        raise ProductionEvidenceError("repository_state_invalid") from None
    commit = head.stdout.strip().lower()
    if (
        head.returncode != 0
        or branch.returncode != 0
        or status.returncode != 0
        or status.stdout
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        raise ProductionEvidenceError("repository_state_invalid")
    return commit


def prepare_private_directory(path: Path) -> None:
    """创建或验证当前用户 0700、非 symlink 的 Evidence 目录。

    Args:
        path: Evidence 最终目录。

    Raises:
        ProductionEvidenceError: 创建失败、owner/mode/type 不安全。
    """
    try:
        if not isinstance(path, Path) or not path.is_absolute() or path.is_symlink():
            raise ProductionEvidenceError("evidence_directory_unsafe")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
        ):
            raise ProductionEvidenceError("evidence_directory_unsafe")
    except ProductionEvidenceError:
        raise
    except OSError:
        raise ProductionEvidenceError("evidence_directory_unsafe") from None


def build_seatbelt_evidence_report(
    *,
    commit: str,
    probe: str,
    started_at: str,
    finished_at: str,
    contained: bool,
    secret_matches: int,
) -> dict[str, object]:
    """构造单个 Seatbelt executable-chain probe 的封闭 Evidence。

    Args:
        commit: 当前 clean release candidate commit。
        probe: ``python`` 或 ``node-chain``。
        started_at: 带微秒的 UTC 起始时间。
        finished_at: 带微秒的 UTC 结束时间。
        contained: Workspace allow、外部 Secret deny 与 network deny 是否全部成立。
        secret_matches: 输出/Evidence 的 exact Secret 匿名命中数。

    Returns:
        可交给 :func:`write_private_json` 的固定 schema。

    Raises:
        ProductionEvidenceError: 任一字段不符合封闭契约。
    """
    normalized = validate_commit(commit)
    if (
        probe not in _SEATBELT_PROBES
        or not _is_timestamp(started_at)
        or not _is_timestamp(finished_at)
        or type(contained) is not bool
        or type(secret_matches) is not int
        or secret_matches < 0
    ):
        raise ProductionEvidenceError("invalid_seatbelt_evidence")
    verified = contained and secret_matches == 0
    return {
        "schema_version": 1,
        "suite": "seatbelt-containment",
        "commit": normalized,
        "probe": probe,
        "started_at": started_at,
        "finished_at": finished_at,
        "contained": contained,
        "secret_matches": secret_matches,
        "release_status": (
            "SEATBELT_CONTAINMENT_VERIFIED"
            if verified
            else "SEATBELT_CONTAINMENT_FAILED"
        ),
    }


def validate_seatbelt_evidence_report(report: Mapping[str, object]) -> bool:
    """重新构造并验证一个 Seatbelt private Evidence report。"""
    expected = {
        "schema_version",
        "suite",
        "commit",
        "probe",
        "started_at",
        "finished_at",
        "contained",
        "secret_matches",
        "release_status",
    }
    if not isinstance(report, Mapping) or set(report) != expected:
        return False
    try:
        rebuilt = build_seatbelt_evidence_report(
            commit=report["commit"],
            probe=report["probe"],
            started_at=report["started_at"],
            finished_at=report["finished_at"],
            contained=report["contained"],
            secret_matches=report["secret_matches"],
        )
    except (ProductionEvidenceError, TypeError):
        return False
    return rebuilt == dict(report)


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


def _is_timestamp(value: object) -> bool:
    """验证带微秒的 UTC Evidence timestamp。"""
    if not isinstance(value, str):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return False
    return True
