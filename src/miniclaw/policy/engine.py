"""基于 Tool 风险等级的最小默认 Policy。"""

from dataclasses import dataclass
from enum import StrEnum

from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import ToolContext, ToolDefinition, ToolRisk


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


class PolicyEngine:
    """采用安全默认值划分只读、需审批和禁止动作。"""

    def authorize(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        """只自动放行 low-risk；critical 拒绝；其余要求审批。"""
        del context, arguments
        if definition.risk is ToolRisk.LOW:
            return PolicyDecision(PolicyAction.ALLOW, "built_in_read_only")
        if definition.risk is ToolRisk.CRITICAL:
            return PolicyDecision(PolicyAction.DENY, "critical_action")
        return PolicyDecision(PolicyAction.REQUIRE_APPROVAL, "approval_required")
