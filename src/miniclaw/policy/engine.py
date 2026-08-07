"""基于 Tool 风险等级的最小默认 Policy。"""

from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from miniclaw.policy.command import CommandPolicyError, NormalizedCommand, normalize_command
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
    normalized_arguments: dict[str, JsonValue] | None = None


class PolicyEngine:
    """采用安全默认值划分只读、需审批和禁止动作。"""

    def __init__(
        self,
        *,
        security: str = "allowlist",
        ask: str = "on-miss",
        command_rules: tuple[NormalizedCommand, ...] = (),
    ) -> None:
        if security not in {"deny", "allowlist", "full"}:
            raise ValueError("invalid tool security mode")
        if ask not in {"off", "on-miss", "always"}:
            raise ValueError("invalid tool ask mode")
        self._security = security
        self._ask = ask
        self._command_rules = frozenset(command_rules)

    def authorize(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        """只自动放行 low-risk；critical 拒绝；其余要求审批。"""
        if definition.name == "run_command":
            return self._authorize_command(context, arguments)
        normalized_arguments = arguments
        path_argument = _READ_PATH_ARGUMENTS.get(definition.name)
        if path_argument is not None:
            raw_path = cast(str, arguments[path_argument])
            try:
                resolved = WorkspaceGuard().resolve_read(context, raw_path)
            except WorkspaceAccessError as error:
                return PolicyDecision(PolicyAction.DENY, str(error), error.code)
            normalized_arguments = {**arguments, path_argument: str(resolved)}
        write_path_argument = _WRITE_PATH_ARGUMENTS.get(definition.name)
        if write_path_argument is not None:
            raw_path = cast(str, arguments[write_path_argument])
            try:
                resolved = WorkspaceGuard().resolve_write(context, raw_path)
            except WorkspaceAccessError as error:
                return PolicyDecision(PolicyAction.DENY, str(error), error.code)
            normalized_arguments = {**arguments, write_path_argument: str(resolved)}
        if definition.risk is ToolRisk.LOW:
            return PolicyDecision(
                PolicyAction.ALLOW,
                "built_in_read_only",
                normalized_arguments=normalized_arguments,
            )
        if definition.risk is ToolRisk.CRITICAL:
            return PolicyDecision(PolicyAction.DENY, "critical_action")
        return PolicyDecision(
            PolicyAction.REQUIRE_APPROVAL,
            "approval_required",
            normalized_arguments=normalized_arguments,
        )

    def _authorize_command(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        """对解析后的 executable + exact argv 应用 security × ask。"""
        program = cast(str, arguments["program"])
        args = cast(list[str], arguments["args"])
        try:
            normalized = normalize_command(program, tuple(args), context.workspace)
        except CommandPolicyError as error:
            return PolicyDecision(PolicyAction.DENY, str(error), error.code)
        normalized_arguments = {
            **arguments,
            "program": normalized.resolved_program,
            "args": list(normalized.args),
        }
        if self._security == "deny":
            return PolicyDecision(PolicyAction.DENY, "command execution is disabled")
        exact_match = normalized in self._command_rules
        if self._ask == "always":
            action = PolicyAction.REQUIRE_APPROVAL
        elif self._security == "full" or exact_match:
            action = PolicyAction.ALLOW
        elif self._ask == "on-miss":
            action = PolicyAction.REQUIRE_APPROVAL
        else:
            action = PolicyAction.DENY
        return PolicyDecision(
            action,
            "exact_command_rule" if exact_match else "command_policy",
            normalized_arguments=normalized_arguments,
        )
