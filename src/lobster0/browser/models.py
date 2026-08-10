"""Browser Worker 协议使用的不可变数据模型。"""

import json
from dataclasses import dataclass

from lobster0.providers.base import JsonValue, ModelMessage

BROWSER_PROVENANCE = "untrusted_web_content"


class BrowserProtocolError(RuntimeError):
    """表示 Browser Worker 返回的稳定协议或动作错误。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码与不包含敏感诊断的公开消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BrowserAction:
    """描述 Core 发给 Browser Worker 的一个受限动作。"""

    session_id: str
    kind: str
    params: dict[str, JsonValue]


def preserve_browser_provenance(message: ModelMessage) -> ModelMessage:
    """从 Browser Tool JSON 恢复不可由网页移除的 provenance 元数据。

    Args:
        message: 即将进入 Provider Context 的一条历史消息。

    Returns:
        非 Browser 消息保持原值；合法 Browser 结果追加 provenance metadata。

    Raises:
        本函数不传播 JSON 解析异常，无效 Tool 内容按普通不可信结果处理。
    """
    if message.role != "tool":
        return message
    try:
        payload = json.loads(message.content)
    except (json.JSONDecodeError, TypeError):
        return message
    if not isinstance(payload, dict) or not str(payload.get("tool", "")).startswith(
        "browser_"
    ):
        return message
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("provenance") != BROWSER_PROVENANCE:
        return message
    return ModelMessage(
        role=message.role,
        content=message.content,
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
        reasoning_content=message.reasoning_content,
        metadata={**message.metadata, "provenance": BROWSER_PROVENANCE},
    )
