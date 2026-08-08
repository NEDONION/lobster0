"""平台中立 Approval envelope、严格命令解析与 Core continuation 路由。"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from miniclaw.agent.events import RunEventHandler
from miniclaw.agent.turn import TurnResult
from miniclaw.policy.approvals import ApprovalDecision, ApprovalError
from miniclaw.storage.tooling import ApprovalPresentation

_APPROVE = re.compile(r"/approve[ \t]+([1-9][0-9]*)[ \t]+(once|session|always)\Z")
_DENY = re.compile(r"/deny[ \t]+([1-9][0-9]*)\Z")
_ACTION_V1_KEYS = frozenset({"miniclaw_action", "approval_id", "decision"})
_ACTION_V2_KEYS = frozenset({"version", "miniclaw_action", "approval_id", "decision"})
_USAGE = "用法：/approve <编号> once|session|always，或 /deny <编号>。"
_V2_KEYS = frozenset(
    {
        "version",
        "approval_id",
        "tool_name",
        "summary",
        "decisions",
        "expires_at",
        "fallback_text",
    }
)
ApprovalCardState = Literal["processing", "succeeded", "denied", "failed"]


class ApprovalPresentationRepository(Protocol):
    """收窄 Controller 对 Core Repository 的读取能力。"""

    def presentation(self, user_id: int, approval_id: int) -> ApprovalPresentation:
        """返回 Core 已校验的展示字段。"""
        ...


class ApprovalContinuationService(Protocol):
    """收窄 Controller 对共享 TurnService 的审批入口。"""

    async def continue_approval(
        self,
        user_id: int,
        approval_id: int,
        *,
        decision: ApprovalDecision,
        on_text=None,
        on_event: RunEventHandler | None = None,
    ) -> TurnResult:
        """执行既有 Core approval continuation。"""
        ...


@dataclass(frozen=True, slots=True)
class ApprovalPrompt:
    """保存 legacy/平台渲染后的 Card 和始终可用的文本降级指令。"""

    card: dict[str, Any]
    fallback_text: str


@dataclass(frozen=True, slots=True, repr=False)
class ApprovalEnvelope:
    """保存可跨平台持久化的 v2 审批展示语义。"""

    version: Literal[2]
    approval_id: int
    tool_name: str
    summary: str
    decisions: tuple[ApprovalDecision, ...]
    expires_at: str
    fallback_text: str

    def __post_init__(self) -> None:
        """拒绝松散 JSON、控制字符、重复决定和无时区时间。"""
        if type(self.version) is not int or self.version != 2:
            raise ValueError("invalid approval envelope version")
        if type(self.approval_id) is not int or self.approval_id <= 0:
            raise ValueError("invalid approval envelope id")
        _bounded_visible(self.tool_name, "tool_name", maximum=100)
        _bounded_visible(self.summary, "summary", maximum=500)
        _bounded_visible(self.fallback_text, "fallback_text", maximum=2000)
        if (
            not isinstance(self.decisions, tuple)
            or not self.decisions
            or any(not isinstance(item, ApprovalDecision) for item in self.decisions)
            or len(set(self.decisions)) != len(self.decisions)
            or ApprovalDecision.DENY not in self.decisions
        ):
            raise ValueError("invalid approval envelope decisions")
        try:
            expires = datetime.fromisoformat(self.expires_at)
        except (TypeError, ValueError):
            raise ValueError("invalid approval envelope expiry") from None
        if expires.tzinfo is None or expires.utcoffset() is None:
            raise ValueError("invalid approval envelope expiry")

    def __repr__(self) -> str:
        """只显示版本、内部审批编号和 Tool 名，不显示操作摘要。"""
        return (
            "ApprovalEnvelope("
            f"version={self.version}, approval_id={self.approval_id}, "
            f"tool_name={self.tool_name!r})"
        )


@dataclass(frozen=True, slots=True)
class ApprovalCommandOutcome:
    """描述一条消息是否已作为控制命令消费。"""

    handled: bool
    result: TurnResult | None = None
    notice: str | None = None
    approval_id: int | None = None
    decision: ApprovalDecision | None = None
    error_code: str | None = None


class ChannelApprovalController:
    """把任意 Channel 的文本/按钮命令路由到唯一 Core Approval 状态机。"""

    def __init__(
        self,
        *,
        owner_external_user_id: str,
        approvals: ApprovalPresentationRepository,
        service: ApprovalContinuationService,
    ) -> None:
        if not owner_external_user_id:
            raise ValueError("owner_external_user_id must not be empty")
        self._owner_external_user_id = owner_external_user_id
        self._approvals = approvals
        self._service = service

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalEnvelope:
        """从 Core presentation 构建平台中立、可持久化的 v2 envelope。"""
        presentation = self._approvals.presentation(user_id, approval_id)
        fallback = _fallback_text(presentation)
        approval = presentation.approval
        decisions = tuple(dict.fromkeys((*presentation.grant_modes, ApprovalDecision.DENY)))
        return ApprovalEnvelope(
            version=2,
            approval_id=approval.id,
            tool_name=approval.tool_name,
            summary=approval.summary,
            decisions=decisions,
            expires_at=approval.expires_at.astimezone(UTC).isoformat(),
            fallback_text=fallback,
        )

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        text: str,
        on_event: RunEventHandler | None = None,
    ) -> ApprovalCommandOutcome:
        """严格识别审批文本命令；普通自然语言返回 handled=False。"""
        normalized = text.strip()
        parsed = _parse_text_command(normalized)
        if parsed is None:
            if normalized.startswith(("/approve", "/deny")):
                return ApprovalCommandOutcome(True, notice=_USAGE)
            return ApprovalCommandOutcome(False)
        approval_id, decision = parsed
        return await self._decide(
            user_id=user_id,
            actor_external_user_id=actor_external_user_id,
            approval_id=approval_id,
            decision=decision,
            on_event=on_event,
        )

    async def handle_card_action(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        value: Any,
        on_event: RunEventHandler | None = None,
    ) -> ApprovalCommandOutcome:
        """只接受 MiniClaw 自己生成且键集合完全一致的按钮 payload。"""
        parsed = parse_approval_card_action(value)
        if parsed is None:
            return ApprovalCommandOutcome(True, notice="无法识别这次审批操作。")
        approval_id, decision = parsed
        return await self._decide(
            user_id=user_id,
            actor_external_user_id=actor_external_user_id,
            approval_id=approval_id,
            decision=decision,
            on_event=on_event,
        )

    async def _decide(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        approval_id: int,
        decision: ApprovalDecision,
        on_event: RunEventHandler | None,
    ) -> ApprovalCommandOutcome:
        """执行 Owner gate，并把稳定 Core 错误映射为短提示。"""
        if actor_external_user_id != self._owner_external_user_id:
            return ApprovalCommandOutcome(
                True,
                notice="只有 Owner 可以处理这条审批。",
                approval_id=approval_id,
                decision=decision,
                error_code="not_owner",
            )
        try:
            result = await self._service.continue_approval(
                user_id,
                approval_id,
                decision=decision,
                on_event=on_event,
            )
        except ApprovalError as error:
            return ApprovalCommandOutcome(
                True,
                notice=_approval_error_notice(error.code),
                approval_id=approval_id,
                decision=decision,
                error_code=error.code,
            )
        return ApprovalCommandOutcome(
            True,
            result=result,
            approval_id=approval_id,
            decision=decision,
        )


def approval_delivery_payload(envelope: ApprovalEnvelope) -> str:
    """把平台中立 v2 envelope 编码为 DeliveryWorker 可验证的 JSON。"""
    if not isinstance(envelope, ApprovalEnvelope):
        raise TypeError("approval_delivery_payload requires ApprovalEnvelope")
    return json.dumps(
        {
            "version": envelope.version,
            "approval_id": envelope.approval_id,
            "tool_name": envelope.tool_name,
            "summary": envelope.summary,
            "decisions": [decision.value for decision in envelope.decisions],
            "expires_at": envelope.expires_at,
            "fallback_text": envelope.fallback_text,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_approval_delivery_payload(content: str) -> ApprovalEnvelope | ApprovalPrompt:
    """严格解码 v2 envelope，并只读兼容升级前的 v1 Feishu Card。"""
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("invalid approval delivery payload") from None
    if not isinstance(value, dict) or type(value.get("version")) is not int:
        raise ValueError("invalid approval delivery payload")
    if value["version"] == 1:
        if (
            set(value) != {"version", "card", "fallback_text"}
            or not isinstance(value.get("card"), dict)
            or not _is_bounded_visible(value.get("fallback_text"), maximum=2000)
        ):
            raise ValueError("invalid approval delivery payload")
        return ApprovalPrompt(value["card"], value["fallback_text"])
    if value["version"] != 2 or set(value) != _V2_KEYS:
        raise ValueError("invalid approval delivery payload")
    decisions = value.get("decisions")
    if not isinstance(decisions, list) or any(not isinstance(item, str) for item in decisions):
        raise ValueError("invalid approval delivery payload")
    try:
        parsed_decisions = tuple(ApprovalDecision(item) for item in decisions)
        return ApprovalEnvelope(
            version=value["version"],
            approval_id=value.get("approval_id"),
            tool_name=value.get("tool_name"),
            summary=value.get("summary"),
            decisions=parsed_decisions,
            expires_at=value.get("expires_at"),
            fallback_text=value.get("fallback_text"),
        )
    except (TypeError, ValueError):
        raise ValueError("invalid approval delivery payload") from None


def feishu_approval_prompt(envelope: ApprovalEnvelope) -> ApprovalPrompt:
    """把中立 envelope 渲染为 Feishu Card；持久层不保存该平台 payload。"""
    if not isinstance(envelope, ApprovalEnvelope):
        raise TypeError("feishu renderer requires ApprovalEnvelope")
    return ApprovalPrompt(
        card=_approval_card(envelope),
        fallback_text=envelope.fallback_text,
    )


def feishu_approval_status_card(
    state: ApprovalCardState,
    visible: str,
) -> dict[str, Any]:
    """渲染无按钮的飞书审批处理中或终态卡片，并限制可见正文长度。"""
    presentations = {
        "processing": ("orange", "MiniClaw 审批 · 处理中", "正在验证并执行这次操作。"),
        "succeeded": ("green", "MiniClaw 审批 · 已完成", "操作已完成。"),
        "denied": ("red", "MiniClaw 审批 · 已拒绝", "操作已拒绝，未执行。"),
        "failed": ("red", "MiniClaw 审批 · 处理失败", "操作未完成。"),
    }
    try:
        template, title, fallback = presentations[state]
    except (KeyError, TypeError):
        raise ValueError("invalid approval card state") from None
    if not isinstance(visible, str):
        raise TypeError("approval card visible text must be a string")
    content = visible.strip() or fallback
    content = "".join(
        character
        for character in content
        if ord(character) >= 32 or character in "\n\t"
    )[:2000]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "markdown", "content": content}],
    }


def text_approval_prompt(envelope: ApprovalEnvelope) -> str:
    """把中立 envelope 渲染为 Telegram/Discord 都可发送的有限纯文本。"""
    if not isinstance(envelope, ApprovalEnvelope):
        raise TypeError("text renderer requires ApprovalEnvelope")
    return (
        f"MiniClaw 审批 #{envelope.approval_id}\n"
        f"工具：{envelope.tool_name}\n"
        f"操作：{envelope.summary}\n"
        f"过期时间：{envelope.expires_at}\n"
        f"{envelope.fallback_text}"
    )


def _parse_text_command(text: str) -> tuple[int, ApprovalDecision] | None:
    """解析完整文本命令，不接受尾随参数或 Unicode 数字。"""
    approved = _APPROVE.fullmatch(text)
    if approved is not None:
        return int(approved.group(1)), ApprovalDecision(approved.group(2))
    denied = _DENY.fullmatch(text)
    if denied is not None:
        return int(denied.group(1)), ApprovalDecision.DENY
    return None


def parse_approval_card_action(value: Any) -> tuple[int, ApprovalDecision] | None:
    """解析 v2 callback，并兼容升级前的固定三字段 v1 payload。"""
    if not isinstance(value, dict) or set(value) not in {_ACTION_V1_KEYS, _ACTION_V2_KEYS}:
        return None
    if "version" in value and (type(value["version"]) is not int or value["version"] != 2):
        return None
    if value.get("miniclaw_action") != "approval":
        return None
    approval_id = value.get("approval_id")
    decision = value.get("decision")
    if type(approval_id) is not int or approval_id <= 0 or not isinstance(decision, str):
        return None
    try:
        parsed_decision = ApprovalDecision(decision)
    except ValueError:
        return None
    return approval_id, parsed_decision


def _approval_card(
    envelope: ApprovalEnvelope,
) -> dict[str, Any]:
    """构造只含中立 envelope 字段的 Feishu 交互卡片。"""
    actions = [
        _button(
            envelope.approval_id,
            mode,
            {
                ApprovalDecision.ONCE: "仅本次",
                ApprovalDecision.SESSION: "本会话",
                ApprovalDecision.ALWAYS: "始终允许",
                ApprovalDecision.DENY: "拒绝",
            }[mode],
            primary=mode is ApprovalDecision.ONCE,
            danger=mode is ApprovalDecision.DENY,
        )
        for mode in envelope.decisions
    ]
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {
                "tag": "plain_text",
                "content": f"MiniClaw 审批 #{envelope.approval_id}",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**工具**：`{envelope.tool_name}`\n"
                    f"**操作**：{envelope.summary}\n"
                    f"**过期时间**：{envelope.expires_at}"
                ),
            },
            {"tag": "action", "actions": actions},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": envelope.fallback_text}
                ],
            },
        ],
    }


def _button(
    approval_id: int,
    decision: ApprovalDecision,
    label: str,
    *,
    primary: bool = False,
    danger: bool = False,
) -> dict[str, Any]:
    """构造固定 callback payload 的按钮。"""
    kind = "danger" if danger else "primary" if primary else "default"
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": kind,
        "value": {
            "version": 2,
            "miniclaw_action": "approval",
            "approval_id": approval_id,
            "decision": decision.value,
        },
    }


def _fallback_text(presentation: ApprovalPresentation) -> str:
    """生成卡片不可用时的完整文本命令。"""
    approval_id = presentation.approval.id
    commands = [
        f"/approve {approval_id} {mode.value}" for mode in presentation.grant_modes
    ]
    commands.append(f"/deny {approval_id}")
    return "卡片不可用时发送：" + "；".join(commands)


def _is_bounded_visible(value: object, *, maximum: int) -> bool:
    """判断文本非空、有界且不含危险控制字符。"""
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
        and not any(ord(character) < 32 and character not in "\n\t" for character in value)
    )


def _bounded_visible(value: object, name: str, *, maximum: int) -> str:
    """校验 envelope 的有限可见文本，不在异常中回显原值。"""
    if not _is_bounded_visible(value, maximum=maximum):
        raise ValueError(f"invalid approval envelope {name}")
    return value


def _approval_error_notice(code: str) -> str:
    """把 Core 稳定码映射为不含内部详情的用户提示。"""
    notices = {
        "already_decided": "这条审批已经处理，不会重复执行。",
        "expired": "这条审批已过期，不会执行。",
        "not_owner": "只有 Owner 可以处理这条审批。",
        "not_found": "没有找到这条审批。",
        "scope_forbidden": "该授权范围不被允许，请选择更窄的范围。",
        "hash_mismatch": "审批参数已变化，已安全停止执行。",
    }
    return notices.get(code, "审批处理失败，未执行任何操作。")
