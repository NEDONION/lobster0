"""MiniClaw 进程级权限模式与脱敏变更通知。"""

from collections.abc import Callable
from enum import StrEnum

type PermissionAudit = Callable[[int, "PermissionMode", "PermissionMode", str], None]

_SOURCES = frozenset({"cli", "feishu", "telegram", "discord"})


class PermissionMode(StrEnum):
    """表示 Owner 为当前 MiniClaw 进程选择的监督级别。"""

    SAFE = "safe"
    SMART = "smart"
    AUTOPILOT = "autopilot"
    YOLO = "yolo"


class PermissionState:
    """保存所有入口共享且只能通过受控方法修改的当前权限模式。"""

    def __init__(
        self,
        mode: str | PermissionMode = PermissionMode.SAFE,
        *,
        audit: PermissionAudit | None = None,
    ) -> None:
        """创建权限状态，并可绑定一个先审计后生效的同步回调。"""
        self._mode = PermissionMode(mode)
        self._audit = audit

    @property
    def mode(self) -> PermissionMode:
        """返回当前进程对后续 Turn 生效的权限模式。"""
        return self._mode

    def set_mode(
        self,
        mode: str | PermissionMode,
        *,
        user_id: int,
        source: str,
    ) -> PermissionMode:
        """校验、审计并原子替换模式；相同模式保持幂等。"""
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("permission mode user_id is invalid")
        if source not in _SOURCES:
            raise ValueError("permission mode source is invalid")
        selected = PermissionMode(mode)
        previous = self._mode
        if selected is previous:
            return selected
        if self._audit is not None:
            self._audit(user_id, previous, selected, source)
        self._mode = selected
        return selected
