"""飞书 Approval 的脱敏展示、严格命令解析与 Core continuation 路由。"""

import json
import re
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Protocol

from miniclaw.agent.events import RunEventHandler
from miniclaw.agent.turn import TurnResult
from miniclaw.policy.approvals import ApprovalDecision, ApprovalError
from miniclaw.storage.tooling import ApprovalPresentation

_APPROVE = re.compile(r"/approve[ \t]+([1-9][0-9]*)[ \t]+(once|session|always)\Z")
_DENY = re.compile(r"/deny[ \t]+([1-9][0-9]*)\Z")
_ACTION_KEYS = frozenset({"miniclaw_action", "approval_id", "decision"})
_USAGE = "用法：/approve <编号> once|session|always，或 /deny <编号>。"


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
    """保存卡片和始终可用的文本降级指令。"""

    card: dict[str, Any]
    fallback_text: str


@dataclass(frozen=True, slots=True)
class ApprovalCommandOutcome:
    """描述一条消息是否已作为控制命令消费。"""

    handled: bool
    result: TurnResult | None = None
    notice: str | None = None
    approval_id: int | None = None


class ChannelApprovalController:
    """把飞书文本/按钮命令直接路由到唯一 Core Approval 状态机。"""

    def __init__(
        self,
        *,
        owner_open_id: str,
        approvals: ApprovalPresentationRepository,
        service: ApprovalContinuationService,
    ) -> None:
        if not owner_open_id:
            raise ValueError("owner_open_id must not be empty")
        self._owner_open_id = owner_open_id
        self._approvals = approvals
        self._service = service

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalPrompt:
        """从 Core presentation 构建有限按钮和文本降级说明。"""
        presentation = self._approvals.presentation(user_id, approval_id)
        fallback = _fallback_text(presentation)
        return ApprovalPrompt(
            card=_approval_card(presentation, fallback),
            fallback_text=fallback,
        )

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_open_id: str,
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
            actor_open_id=actor_open_id,
            approval_id=approval_id,
            decision=decision,
            on_event=on_event,
        )

    async def handle_card_action(
        self,
        *,
        user_id: int,
        actor_open_id: str,
        value: Any,
        on_event: RunEventHandler | None = None,
    ) -> ApprovalCommandOutcome:
        """只接受 MiniClaw 自己生成且键集合完全一致的按钮 payload。"""
        parsed = _parse_card_action(value)
        if parsed is None:
            return ApprovalCommandOutcome(True, notice="无法识别这次审批操作。")
        approval_id, decision = parsed
        return await self._decide(
            user_id=user_id,
            actor_open_id=actor_open_id,
            approval_id=approval_id,
            decision=decision,
            on_event=on_event,
        )

    async def _decide(
        self,
        *,
        user_id: int,
        actor_open_id: str,
        approval_id: int,
        decision: ApprovalDecision,
        on_event: RunEventHandler | None,
    ) -> ApprovalCommandOutcome:
        """执行 Owner gate，并把稳定 Core 错误映射为短提示。"""
        if actor_open_id != self._owner_open_id:
            return ApprovalCommandOutcome(
                True,
                notice="只有 Owner 可以处理这条审批。",
                approval_id=approval_id,
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
            )
        return ApprovalCommandOutcome(
            True,
            result=result,
            approval_id=approval_id,
        )


def approval_delivery_payload(prompt: ApprovalPrompt) -> str:
    """把卡片与文本 fallback 编码为 DeliveryWorker 可验证的 JSON。"""
    return json.dumps(
        {
            "version": 1,
            "card": prompt.card,
            "fallback_text": prompt.fallback_text,
        },
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def parse_approval_delivery_payload(content: str) -> ApprovalPrompt:
    """严格解码持久化审批 payload，损坏数据失败关闭。"""
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        raise ValueError("invalid approval delivery payload") from None
    if (
        not isinstance(value, dict)
        or set(value) != {"version", "card", "fallback_text"}
        or value.get("version") != 1
        or not isinstance(value.get("card"), dict)
        or not isinstance(value.get("fallback_text"), str)
        or not value["fallback_text"]
    ):
        raise ValueError("invalid approval delivery payload")
    return ApprovalPrompt(value["card"], value["fallback_text"])


def _parse_text_command(text: str) -> tuple[int, ApprovalDecision] | None:
    """解析完整文本命令，不接受尾随参数或 Unicode 数字。"""
    approved = _APPROVE.fullmatch(text)
    if approved is not None:
        return int(approved.group(1)), ApprovalDecision(approved.group(2))
    denied = _DENY.fullmatch(text)
    if denied is not None:
        return int(denied.group(1)), ApprovalDecision.DENY
    return None


def _parse_card_action(value: Any) -> tuple[int, ApprovalDecision] | None:
    """解析卡片 callback 的固定三字段 payload。"""
    if not isinstance(value, dict) or set(value) != _ACTION_KEYS:
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
    presentation: ApprovalPresentation,
    fallback_text: str,
) -> dict[str, Any]:
    """构造只含脱敏 summary、TTL 和 Core grant modes 的交互卡片。"""
    approval = presentation.approval
    actions = [
        _button(
            approval.id,
            mode,
            {
                ApprovalDecision.ONCE: "仅本次",
                ApprovalDecision.SESSION: "本会话",
                ApprovalDecision.ALWAYS: "始终允许",
            }[mode],
            primary=mode is ApprovalDecision.ONCE,
        )
        for mode in presentation.grant_modes
    ]
    actions.append(_button(approval.id, ApprovalDecision.DENY, "拒绝", danger=True))
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {"tag": "plain_text", "content": f"MiniClaw 审批 #{approval.id}"},
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"**工具**：`{approval.tool_name}`\n"
                    f"**操作**：{approval.summary}\n"
                    f"**过期时间**：{approval.expires_at.astimezone(UTC).isoformat()}"
                ),
            },
            {"tag": "action", "actions": actions},
            {"tag": "note", "elements": [{"tag": "plain_text", "content": fallback_text}]},
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
