"""基于 Tool 风险等级的最小默认 Policy。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from miniclaw.policy.workspace import WorkspaceAccessError, WorkspaceGuard
from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import ToolContext, ToolDefinition, ToolRisk

_READ_PATH_ARGUMENTS = {"read_file": "path", "glob": "root", "grep": "root"}
_WRITE_PATH_ARGUMENTS = {"write_file": "path", "edit_file": "path"}


class PolicyAction(StrEnum):
    """Policy 对一次 Tool Call 的执行决定。"""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """包含稳定动作和可审计原因的 Policy 结果。"""

    action: PolicyAction
    reason: str
    error_code: str = "denied"


class PolicyEngine:
    """采用安全默认值划分只读、需审批和禁止动作。"""

    def authorize(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        """只自动放行 low-risk；critical 拒绝；其余要求审批。"""
        path_argument = _READ_PATH_ARGUMENTS.get(definition.name)
        if path_argument is not None:
            raw_path = cast(str, arguments[path_argument])
            try:
                WorkspaceGuard().resolve_read(context, raw_path)
            except WorkspaceAccessError as error:
                return PolicyDecision(PolicyAction.DENY, str(error), error.code)
        write_path_argument = _WRITE_PATH_ARGUMENTS.get(definition.name)
        if write_path_argument is not None:
            raw_path = cast(str, arguments[write_path_argument])
            try:
                WorkspaceGuard().resolve_write(context, raw_path)
            except WorkspaceAccessError as error:
                return PolicyDecision(PolicyAction.DENY, str(error), error.code)
        if definition.risk is ToolRisk.LOW:
            return PolicyDecision(PolicyAction.ALLOW, "built_in_read_only")
        if definition.risk is ToolRisk.CRITICAL:
            return PolicyDecision(PolicyAction.DENY, "critical_action")
        return PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required")
