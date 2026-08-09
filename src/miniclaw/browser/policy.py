"""Browser 动作的 Core 风险分级。"""

from dataclasses import dataclass

from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import ToolRisk

BROWSER_TOOL_NAMES = frozenset(
    {
        "browser_open",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press",
        "browser_scroll",
        "browser_screenshot",
        "browser_close",
    }
)
_SENSITIVE_INPUT_KINDS = frozenset(
    {"password", "one-time-code", "otp", "current-password", "new-password"}
)


@dataclass(frozen=True, slots=True)
class BrowserPolicyResult:
    """保存动作风险和不可由 Approval 绕过的拒绝码。"""

    risk: ToolRisk
    error_code: str | None = None


def classify_browser_action(
    tool_name: str,
    arguments: dict[str, JsonValue],
) -> BrowserPolicyResult:
    """按浏览器动作语义返回动态风险与硬拒绝结果。

    Args:
        tool_name: 已注册的 Browser Tool 名称。
        arguments: Tool 完成基本类型校验后的参数。

    Returns:
        可交给 Core Policy 的风险和可选硬拒绝码。

    Raises:
        本函数不抛出业务异常；未知动作按 critical fail closed。
    """
    if tool_name not in BROWSER_TOOL_NAMES:
        return BrowserPolicyResult(ToolRisk.CRITICAL, "browser_action_unknown")
    if tool_name == "browser_type":
        input_kind = arguments.get("input_kind")
        if isinstance(input_kind, str) and input_kind.casefold() in _SENSITIVE_INPUT_KINDS:
            return BrowserPolicyResult(ToolRisk.CRITICAL, "browser_sensitive_input")
    if tool_name == "browser_click":
        return BrowserPolicyResult(ToolRisk.HIGH)
    if tool_name == "browser_press" and arguments.get("key") in {"Enter", "Space"}:
        return BrowserPolicyResult(ToolRisk.HIGH)
    return BrowserPolicyResult(ToolRisk.LOW)
