"""基于 Tool 风险等级的最小默认 Policy。"""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import cast

from miniclaw.browser.policy import BROWSER_TOOL_NAMES, classify_browser_action
from miniclaw.policy.approvals import ApprovalDecision, available_approval_decisions
from miniclaw.policy.command import (
    SAFE_EXECUTABLE_PATH,
    CommandPolicyError,
    NormalizedCommand,
    normalize_command,
)
from miniclaw.policy.modes import PermissionMode, PermissionState
from miniclaw.policy.network import (
    NetworkPolicyError,
    NetworkRule,
    Resolver,
    default_resolver,
    validate_https_target,
)
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
    approval_modes: tuple[ApprovalDecision, ...] = ()


class PolicyEngine:
    """采用安全默认值划分只读、需审批和禁止动作。"""

    def __init__(
        self,
        *,
        security: str = "allowlist",
        ask: str = "on-miss",
        command_rules: tuple[NormalizedCommand, ...] = (),
        network_rules: tuple[NetworkRule, ...] = (),
        network_resolver: Resolver | None = None,
        executable_path: str = SAFE_EXECUTABLE_PATH,
        mode: str | PermissionMode = PermissionMode.SAFE,
        permission_state: PermissionState | None = None,
    ) -> None:
        if security not in {"deny", "allowlist", "full"}:
            raise ValueError("invalid tool security mode")
        if ask not in {"off", "on-miss", "always"}:
            raise ValueError("invalid tool ask mode")
        self._security = security
        self._ask = ask
        self._command_rules = set(command_rules)
        self._network_rules = set(network_rules)
        self._network_resolver = network_resolver or default_resolver
        self._executable_path = executable_path
        self._permission_state = permission_state or PermissionState(mode)

    def authorize(
        self,
        definition: ToolDefinition,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        """只自动放行 low-risk；critical 拒绝；其余要求审批。"""
        if definition.name == "run_command":
            return self._authorize_command(context, arguments)
        if definition.name == "http_get":
            return self._authorize_http(context, arguments)
        normalized_arguments = arguments
        if definition.name in BROWSER_TOOL_NAMES:
            classification = classify_browser_action(definition.name, arguments)
            if classification.error_code is not None:
                return PolicyDecision(
                    PolicyAction.DENY,
                    classification.error_code,
                    classification.error_code,
                )
            definition = replace(definition, risk=classification.risk)
            if definition.name == "browser_open":
                url = cast(str, arguments["url"])
                allowed_ports = tuple(
                    sorted({443, *(rule.port for rule in self._network_rules)})
                )
                try:
                    target = validate_https_target(
                        url,
                        self._network_resolver,
                        allowed_ports=allowed_ports,
                    )
                except NetworkPolicyError as error:
                    return PolicyDecision(PolicyAction.DENY, str(error), error.code)
                normalized_arguments = {**arguments, "url": target.url}
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
        if (
            definition.risk is ToolRisk.HIGH
            and self._permission_state.mode is not PermissionMode.YOLO
        ):
            return PolicyDecision(
                PolicyAction.REQUIRE_APPROVAL,
                "high_risk_approval_required",
                normalized_arguments=normalized_arguments,
                approval_modes=available_approval_decisions(
                    definition.name,
                    normalized_arguments,
                ),
            )
        if self._automatic_owner_action(context):
            return PolicyDecision(
                PolicyAction.ALLOW,
                "trusted_owner_automation",
                normalized_arguments=normalized_arguments,
            )
        return PolicyDecision(
            PolicyAction.REQUIRE_APPROVAL,
            "approval_required",
            normalized_arguments=normalized_arguments,
            approval_modes=available_approval_decisions(
                definition.name,
                normalized_arguments,
            ),
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
            normalized = normalize_command(
                program,
                tuple(args),
                context.workspace,
                executable_path=self._executable_path,
            )
        except CommandPolicyError as error:
            return PolicyDecision(PolicyAction.DENY, str(error), error.code)
        normalized_arguments = {
            **arguments,
            "program": normalized.resolved_program,
            "args": list(normalized.args),
        }
        exact_match = normalized in self._command_rules
        action = self._owner_action(context, exact_match, smart_http=False)
        approval_modes = (
            available_approval_decisions("run_command", normalized_arguments)
            if action is PolicyAction.REQUIRE_APPROVAL
            else ()
        )
        return PolicyDecision(
            action,
            "exact_command_rule" if exact_match else "command_policy",
            normalized_arguments=normalized_arguments,
            approval_modes=approval_modes,
        )

    def _authorize_http(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> PolicyDecision:
        """验证 HTTPS/DNS，并按精确 hostname + port 应用监督策略。"""
        url = cast(str, arguments["url"])
        allowed_ports = tuple(
            sorted({443, *(rule.port for rule in self._network_rules)})
        )
        try:
            target = validate_https_target(
                url,
                self._network_resolver,
                allowed_ports=allowed_ports,
            )
        except NetworkPolicyError as error:
            return PolicyDecision(PolicyAction.DENY, str(error), error.code)
        normalized_arguments = {**arguments, "url": target.url}
        exact_match = target.rule in self._network_rules
        action = self._owner_action(context, exact_match, smart_http=True)
        return PolicyDecision(
            action,
            "exact_hostname_rule" if exact_match else "network_policy",
            normalized_arguments=normalized_arguments,
            approval_modes=(
                available_approval_decisions("http_get", normalized_arguments)
                if action is PolicyAction.REQUIRE_APPROVAL
                else ()
            ),
        )

    def add_session_command(self, rule: NormalizedCommand) -> None:
        """在当前 Runtime 内加入一条 exact-argv 规则。"""
        self._command_rules.add(rule)

    def add_session_network(self, rule: NetworkRule) -> None:
        """在当前 Runtime 内加入一条 exact-hostname 规则。"""
        self._network_rules.add(rule)

    def _supervised_action(self, exact_match: bool) -> PolicyAction:
        """实现 command/http 共用的 security × ask 状态表。"""
        if self._security == "deny":
            return PolicyAction.DENY
        if self._ask == "always":
            return PolicyAction.REQUIRE_APPROVAL
        if self._security == "full" or exact_match:
            return PolicyAction.ALLOW
        if self._ask == "on-miss":
            return PolicyAction.REQUIRE_APPROVAL
        return PolicyAction.DENY

    def _automatic_owner_action(self, context: ToolContext) -> bool:
        """只为可信 Owner 的 Autopilot/YOLO 自动放行已校验非关键动作。"""
        return context.trusted_owner and self._permission_state.mode in {
            PermissionMode.AUTOPILOT,
            PermissionMode.YOLO,
        }

    def _owner_action(
        self,
        context: ToolContext | None,
        exact_match: bool,
        *,
        smart_http: bool,
    ) -> PolicyAction:
        """把可信入口与四档模式映射到命令或 HTTPS 的监督动作。"""
        trusted_owner = True if context is None else context.trusted_owner
        if not trusted_owner:
            return PolicyAction.REQUIRE_APPROVAL
        mode = self._permission_state.mode
        if mode in {PermissionMode.AUTOPILOT, PermissionMode.YOLO}:
            return PolicyAction.ALLOW
        if mode is PermissionMode.SMART:
            if smart_http or exact_match:
                return PolicyAction.ALLOW
            return PolicyAction.REQUIRE_APPROVAL
        return self._supervised_action(exact_match)
