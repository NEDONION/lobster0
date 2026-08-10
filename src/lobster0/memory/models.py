"""Memory Autopilot 的不可变公共数据契约。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

type ConversationKind = Literal["local", "direct", "group", "unknown"]
type PrivateAccess = Literal["full", "deny"]
type CaptureScope = Literal["private", "public", "none"]

_SOURCE_CHANNELS = frozenset({"cli", "feishu", "telegram", "discord"})


class MemoryScope(StrEnum):
    """表示 Memory Unit 可被披露的封闭作用域。"""

    PRIVATE = "private"
    PUBLIC = "public"
    GROUP = "group"


class MemoryStatus(StrEnum):
    """表示 Memory Unit 在治理状态机中的稳定状态。"""

    OBSERVED = "observed"
    SHORT_TERM = "short_term"
    REVIEW_REQUIRED = "review_required"
    ACTIVE = "active"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"
    ARCHIVED = "archived"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class SourceRef:
    """绑定一条 Memory Unit 可核验的内部消息来源。"""

    message_id: int
    session_id: int
    channel: str

    def __post_init__(self) -> None:
        """拒绝模型可伪造的非法 ID 和未知来源渠道。"""
        if type(self.message_id) is not int or self.message_id <= 0:
            raise ValueError("memory source message_id must be positive")
        if type(self.session_id) is not int or self.session_id <= 0:
            raise ValueError("memory source session_id must be positive")
        if self.channel not in _SOURCE_CHANNELS:
            raise ValueError("memory source channel is unsupported")


@dataclass(frozen=True, slots=True)
class DisclosureContext:
    """保存一次召回不可由模型参数扩大的身份与会话边界。"""

    owner_id: int
    requester_user_id: int | None
    channel: str
    conversation_kind: ConversationKind
    identity_verified: bool


@dataclass(frozen=True, slots=True)
class DisclosureDecision:
    """描述 Core 对私人读取和自动采集给出的稳定决策。"""

    private_access: PrivateAccess
    capture_scope: CaptureScope
    reason_code: str
