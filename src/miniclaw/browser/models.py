"""Browser Worker 协议使用的不可变数据模型。"""

from dataclasses import dataclass

from miniclaw.providers.base import JsonValue


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
