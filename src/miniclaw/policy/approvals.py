"""Approval 参数绑定使用的标准 JSON 与稳定业务错误。"""

import hashlib
import json

from miniclaw.providers.base import JsonValue


class ApprovalError(RuntimeError):
    """表示可安全交给 CLI 的 Approval 状态冲突。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical_arguments_json(arguments: dict[str, JsonValue]) -> str:
    """使用标准、紧凑、键排序的 UTF-8 JSON 绑定完整 Tool 参数。"""
    try:
        return json.dumps(
            arguments,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ValueError("tool arguments must be standard JSON") from None


def canonical_arguments_hash(tool_name: str, arguments: dict[str, JsonValue]) -> str:
    """返回包含 Tool 名的 SHA-256，防止同参数跨 Tool 重放。"""
    canonical = canonical_arguments_json(arguments)
    return hashlib.sha256(f"{tool_name}\n{canonical}".encode()).hexdigest()
