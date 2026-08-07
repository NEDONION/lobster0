"""把 MiniClaw 身份文件和会话历史构造成模型请求。"""

from pathlib import Path

from miniclaw.paths import StatePaths
from miniclaw.providers.base import ModelMessage, ModelRequest

_SYSTEM_PREAMBLE = (
    "You are MiniClaw, a private self-hosted personal agent. "
    "Follow the owner's identity instructions, preserve user privacy, and answer clearly."
)


class ContextError(RuntimeError):
    """表示 Agent 身份文件无法安全读取或构造上下文。"""


class ContextBuilder:
    """按固定顺序组合 System、SOUL、USER 和已筛选会话历史。"""

    def __init__(self, paths: StatePaths) -> None:
        """绑定一个已经初始化的 MiniClaw 状态目录。

        Args:
            paths: 提供 ``SOUL.md`` 与 ``USER.md`` 固定位置的路径集合。
        """
        self._paths = paths

    def build(
        self,
        model: str,
        history: tuple[ModelMessage, ...],
    ) -> ModelRequest:
        """构造身份在前、会话历史在后的模型请求。

        Args:
            model: 当前配置选中的 Provider 模型 ID。
            history: Storage 已按时间筛选并排序的最近消息，包含当前用户消息。

        Returns:
            不含工具 Schema 的 Phase 1 模型请求。

        Raises:
            ContextError: SOUL 或 USER 文件无法读取为 UTF-8 文本。
        """
        soul = self._read_identity(self._paths.soul)
        user = self._read_identity(self._paths.user)
        system = ModelMessage(
            role="system",
            content=(
                f"{_SYSTEM_PREAMBLE}\n\n"
                f"## SOUL\n{soul.strip()}\n\n"
                f"## USER\n{user.strip()}"
            ),
        )
        return ModelRequest(model=model, messages=(system, *history))

    @staticmethod
    def _read_identity(path: Path) -> str:
        """读取一个身份文件，并用不含内容的稳定异常收窄 I/O 失败。"""
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ContextError(f"cannot read MiniClaw identity file {path}") from error
