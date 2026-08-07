"""Workspace 文件读写边界。"""

import os
from pathlib import Path

from miniclaw.tools.base import ToolContext

_SENSITIVE_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg", ".kube"})
_PRIVATE_KEY_NAMES = frozenset(
    {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "private_key", "private.key"}
)
_CREDENTIAL_NAMES = frozenset(
    {
        ".netrc",
        ".npmrc",
        ".git-credentials",
        ".pypirc",
        "credentials",
        "credentials.json",
        "application_default_credentials.json",
        "service_account.json",
        "token.json",
        "secrets.json",
        "secrets.yaml",
    }
)
_KEYSTORE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
_CONTAINER_SOCKET_NAMES = frozenset(
    {"docker.sock", "containerd.sock", "crio.sock", "podman.sock"}
)
_SENSITIVE_PATH_PAIRS = frozenset({(".config", "gcloud"), (".docker", "config.json")})
_STATE_FILE_NAMES = frozenset(
    {"config.toml", "miniclaw.db", "miniclaw.db-wal", "miniclaw.db-shm", "miniclaw.db-journal"}
)


class WorkspaceAccessError(ValueError):
    """表示路径违反 Workspace 读取边界。"""

    def __init__(self, code: str, message: str) -> None:
        """保存可安全返回给模型的稳定错误码与消息。"""
        super().__init__(message)
        self.code = code


class WorkspaceGuard:
    """统一解析模型提供的路径，并把结果限制在配置允许根内。"""

    def resolve_read(self, context: ToolContext, raw_path: str) -> Path:
        """返回允许读取的规范路径；逃逸或敏感路径时抛出稳定异常。"""
        supplied = Path(raw_path)
        candidate = supplied if supplied.is_absolute() else context.workspace / supplied
        state_home = _resolve(context.state_home)
        if _is_sensitive(candidate, state_home):
            raise WorkspaceAccessError(
                "sensitive_path",
                "path is sensitive and cannot be read",
            )
        resolved = _resolve(candidate)
        if _is_sensitive(resolved, state_home):
            raise WorkspaceAccessError(
                "sensitive_path",
                "path is sensitive and cannot be read",
            )
        roots = (context.workspace, *context.read_only_roots)
        if not any(_contains(_resolve(root), resolved) for root in roots):
            raise WorkspaceAccessError(
                "workspace_escape",
                "path is outside the configured workspace",
            )
        return resolved

    def resolve_write(self, context: ToolContext, raw_path: str) -> Path:
        """返回允许写入的 Workspace 路径；只读根和 symlink 一律拒绝。"""
        supplied = Path(raw_path)
        candidate = supplied if supplied.is_absolute() else context.workspace / supplied
        state_home = _resolve(context.state_home)
        if _is_sensitive(candidate, state_home):
            raise WorkspaceAccessError(
                "sensitive_path",
                "path is sensitive and cannot be written",
            )

        workspace = _resolve(context.workspace)
        lexical = _absolute_without_symlink_resolution(candidate)
        for read_only_root in context.read_only_roots:
            if _contains(_resolve(read_only_root), _resolve(candidate)):
                raise WorkspaceAccessError(
                    "read_only_path",
                    "path belongs to a read-only root",
                )
        if not _contains(workspace, lexical):
            raise WorkspaceAccessError(
                "workspace_escape",
                "path is outside the configured workspace",
            )

        relative = lexical.relative_to(workspace)
        current = workspace
        try:
            for part in relative.parts:
                current /= part
                if current.is_symlink():
                    raise WorkspaceAccessError(
                        "symlink_path",
                        "symbolic links cannot be used for writes",
                    )
        except OSError:
            raise WorkspaceAccessError(
                "workspace_escape",
                "path could not be resolved safely",
            ) from None

        if not lexical.parent.is_dir():
            raise WorkspaceAccessError(
                "parent_not_found",
                "target parent directory does not exist",
            )
        resolved = _resolve(lexical)
        if _is_sensitive(resolved, state_home):
            raise WorkspaceAccessError(
                "sensitive_path",
                "path is sensitive and cannot be written",
            )
        if not _contains(workspace, resolved):
            raise WorkspaceAccessError(
                "workspace_escape",
                "path is outside the configured workspace",
            )
        return resolved

    def display(self, context: ToolContext, path: Path, *, root: Path | None = None) -> str:
        """返回相对允许根的路径，不把本机 Home 目录暴露给模型。"""
        base = _resolve(root or context.workspace)
        return _resolve(path).relative_to(base).as_posix() or "."


def _contains(root: Path, path: Path) -> bool:
    """判断规范路径是否位于规范根目录内。"""
    return path.is_relative_to(root)


def _resolve(path: Path) -> Path:
    """解析路径并把底层异常转换成不含本机细节的稳定错误。"""
    try:
        try:
            return path.resolve(strict=True)
        except FileNotFoundError:
            return path.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        raise WorkspaceAccessError(
            "workspace_escape",
            "path could not be resolved safely",
        ) from None


def _absolute_without_symlink_resolution(path: Path) -> Path:
    """规范化 `.`/`..`，但保留 symlink 组件供写边界逐段拒绝。"""
    try:
        return Path(os.path.abspath(path))
    except (OSError, ValueError):
        raise WorkspaceAccessError(
            "workspace_escape",
            "path could not be resolved safely",
        ) from None


def _is_sensitive(path: Path, state_home: Path) -> bool:
    """判断逻辑或规范路径是否指向凭据、状态或系统敏感文件。"""
    parts = tuple(part.casefold() for part in path.parts)
    leaf = parts[-1] if parts else ""
    if any(part.startswith(".env") for part in parts):
        return True
    if any(part in _SENSITIVE_DIRECTORIES for part in parts):
        return True
    if any(
        (left, right) in _SENSITIVE_PATH_PAIRS
        for left, right in zip(parts, parts[1:], strict=False)
    ):
        return True
    if (
        leaf in _PRIVATE_KEY_NAMES
        or leaf in _CREDENTIAL_NAMES
        or leaf.endswith(_KEYSTORE_SUFFIXES)
    ):
        return True

    normalized = Path(str(path).casefold())
    normalized_state = Path(str(state_home).casefold())
    if normalized.parent == normalized_state and normalized.name in _STATE_FILE_NAMES:
        return True
    if normalized.is_relative_to(normalized_state / "logs"):
        return True

    posix = normalized.as_posix()
    if posix in ("/etc/shadow", "/etc/gshadow", "/etc/sudoers") or posix.startswith(
        "/etc/sudoers.d/"
    ):
        return True
    return leaf in _CONTAINER_SOCKET_NAMES
