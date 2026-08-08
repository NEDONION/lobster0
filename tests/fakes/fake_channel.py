"""Channel 契约测试使用的纯内存对象。"""

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class FakeFeishuMessage:
    """模拟官方 SDK 归一化后的最小消息视图。"""

    event_id: str = "evt_test"
    message_id: str = "om_test"
    chat_id: str = "oc_allowed"
    chat_type: str = "p2p"
    sender_id: str = "ou_owner"
    sender_type: str | None = "user"
    sender_is_bot: bool = False
    mentioned_bot: bool = False
    body_text: str = "你好"
    raw_content_type: str = "text"
    create_time: datetime = datetime(2026, 8, 8, tzinfo=UTC)
