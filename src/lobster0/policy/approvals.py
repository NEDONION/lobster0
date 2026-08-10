"""Approval 参数绑定使用的标准 JSON 与稳定业务错误。"""

import hashlib
import json
from enum import StrEnum
from pathlib import Path

from lobster0.policy.command import NormalizedCommand, command_rule_is_persistable
from lobster0.providers.base import JsonValue


class ApprovalDecision(StrEnum):
    """表示 Owner 对一条参数绑定 Approval 的决定。"""

    DENY = "deny"
    ONCE = "once"
    SESSION = "session"
    ALWAYS = "always"


def available_approval_decisions(
    tool_name: str,
    arguments: dict[str, JsonValue],
) -> tuple[ApprovalDecision, ...]:
    """根据已归一化参数返回 Core 允许展示的审批决定。"""
    if tool_name == "run_command":
        program = arguments.get("program")
        args = arguments.get("args")
        if (
            not isinstance(program, str)
            or not Path(program).is_absolute()
            or not isinstance(args, list)
            or any(not isinstance(argument, str) for argument in args)
        ):
            return (ApprovalDecision.ONCE,)
        decisions = (ApprovalDecision.ONCE, ApprovalDecision.SESSION)
        command = NormalizedCommand(program, tuple(args))
        return (
            (*decisions, ApprovalDecision.ALWAYS)
            if command_rule_is_persistable(command)
            else decisions
        )
    if tool_name == "http_get":
        return (
            ApprovalDecision.ONCE,
            ApprovalDecision.SESSION,
            ApprovalDecision.ALWAYS,
        )
    return (ApprovalDecision.ONCE,)


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
