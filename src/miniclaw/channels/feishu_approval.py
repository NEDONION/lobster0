"""飞书审批按钮的原卡状态与 durable continuation 投递编排。"""

import asyncio
import hashlib
from typing import Any, Protocol

from miniclaw.channels.approvals import (
    ApprovalCardState,
    ApprovalCommandOutcome,
    ApprovalEnvelope,
    approval_delivery_payload,
    feishu_approval_status_card,
    parse_approval_card_action,
    parse_approval_delivery_payload,
)
from miniclaw.channels.base import DeliveryKind
from miniclaw.channels.delivery import split_message
from miniclaw.policy.approvals import ApprovalDecision


class ApprovalActionController(Protocol):
    """收窄回调编排器对 Approval Controller 的依赖。"""

    async def handle_card_action(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        value: Any,
        expected_approval_id: int,
    ) -> ApprovalCommandOutcome:
        """校验来源绑定后的审批 ID，并执行一个飞书审批动作。"""
        ...

    def prompt(self, *, user_id: int, approval_id: int) -> ApprovalEnvelope:
        """读取下一条审批的中立展示 envelope。"""
        ...


class ApprovalCardTransport(Protocol):
    """收窄编排器对飞书卡片更新能力的依赖。"""

    async def update_card(self, message_id: str, card: dict[str, Any]) -> object:
        """更新一张已经发送的飞书卡片。"""
        ...


class ChannelNotice(Protocol):
    """描述可作为 Delivery 外键来源的 Channel notice。"""

    id: int
    content: str


class ChannelNoticeRepository(Protocol):
    """收窄编排器对 Channel notice 的持久化能力。"""

    def create_channel_notice(self, session_id: int, content: str) -> ChannelNotice:
        """保存一条非模型 Assistant notice。"""
        ...


class ApprovalDeliveryRepository(Protocol):
    """收窄编排器对 durable Outbox 的写入能力。"""

    def create_parts(
        self,
        *,
        message_id: int,
        channel: str,
        account_id: str,
        external_conversation_id: str,
        reply_to_message_id: str,
        kind: str,
        contents: tuple[str, ...],
    ) -> object:
        """幂等保存最终消息或下一条审批卡。"""
        ...

    def find_sent_by_platform_message_id(
        self,
        *,
        channel: str,
        account_id: str,
        platform_message_id: str,
        kind: DeliveryKind,
    ) -> "ApprovalDeliveryReceipt | None":
        """按平台消息 ID 返回唯一 sent Approval receipt，歧义时为空。"""
        ...


class ApprovalDeliveryReceipt(Protocol):
    """暴露来源卡持久化 envelope 所需的最小 Delivery 投影。"""

    content: str


class FeishuApprovalActionHandler:
    """把一次飞书按钮点击编排成原卡状态与 durable 后续投递。"""

    def __init__(
        self,
        *,
        user_id: int,
        owner_external_user_id: str,
        account_id: str,
        message_max_chars: int,
        controller: ApprovalActionController,
        transport: ApprovalCardTransport,
        messages: ChannelNoticeRepository,
        deliveries: ApprovalDeliveryRepository,
    ) -> None:
        """绑定 Owner、飞书账户、可见长度和持久化依赖。

        Args:
            user_id: MiniClaw 内部 Owner ID。
            owner_external_user_id: 唯一允许点击审批卡的飞书 Owner open_id。
            account_id: 当前飞书账户标识。
            message_max_chars: 飞书普通消息每个分片的最大字符数。
            controller: 唯一 Approval 状态机入口。
            transport: 原审批卡的更新能力。
            messages: Channel notice 持久层。
            deliveries: durable Delivery outbox。

        Raises:
            ValueError: ID、账户或消息长度不合法。
        """
        if type(user_id) is not int or user_id <= 0:
            raise ValueError("user_id must be positive")
        if not isinstance(owner_external_user_id, str) or not owner_external_user_id:
            raise ValueError("owner_external_user_id must not be empty")
        if not isinstance(account_id, str) or not account_id:
            raise ValueError("account_id must not be empty")
        if type(message_max_chars) is not int or message_max_chars <= 0:
            raise ValueError("message_max_chars must be positive")
        self._user_id = user_id
        self._owner_external_user_id = owner_external_user_id
        self._account_id = account_id
        self._message_max_chars = message_max_chars
        self._controller = controller
        self._transport = transport
        self._messages = messages
        self._deliveries = deliveries

    async def __call__(
        self,
        actor_open_id: str,
        value: Any,
        chat_id: str,
        message_id: str,
    ) -> None:
        """立即标记处理中，执行审批并在同一张卡片完成或失败收口。

        Args:
            actor_open_id: 点击按钮的飞书用户 open_id。
            value: 飞书按钮 payload。
            chat_id: 后续 durable Delivery 的飞书会话 ID。
            message_id: 被点击的审批卡消息 ID。

        Returns:
            无返回值；所有平台更新与异常都在回调内部安全收口。
        """
        if not all(isinstance(item, str) and item for item in (actor_open_id, chat_id, message_id)):
            return
        parsed = parse_approval_card_action(value)
        if actor_open_id != self._owner_external_user_id or parsed is None:
            return
        approval_id, _ = parsed
        expected_approval_id = self._bound_approval_id(message_id)
        if expected_approval_id is None or approval_id != expected_approval_id:
            return
        await self._update_card(
            message_id,
            "processing",
            "正在验证权限并执行这次操作，请稍候。",
        )
        try:
            outcome = await self._controller.handle_card_action(
                user_id=self._user_id,
                actor_external_user_id=actor_open_id,
                value=value,
                expected_approval_id=expected_approval_id,
            )
        except asyncio.CancelledError:
            await self._update_terminal_card(
                message_id,
                "failed",
                _approval_diagnostics(
                    stage="Gateway 运行期",
                    error_code="approval_callback_interrupted",
                    reason="Gateway 已停止或重启，审批续跑被安全取消。",
                    approval_id=approval_id,
                    message_id=message_id,
                    turn_id=None,
                    tool_status="执行状态未知；系统不会自动重试，请检查 ToolRun。",
                    suggestion="等待 Gateway 恢复 ready 后发送新消息检查当前状态。",
                ),
            )
            raise
        except Exception:
            await self._update_terminal_card(
                message_id,
                "failed",
                _approval_diagnostics(
                    stage="审批续跑",
                    error_code="approval_callback_failed",
                    reason="审批续跑未能完成。",
                    approval_id=approval_id,
                    message_id=message_id,
                    turn_id=None,
                    tool_status="执行状态未知；系统不会自动重试，请检查 ToolRun。",
                    suggestion="请发送一条新消息检查当前状态。",
                ),
            )
            return

        visible = _outcome_visible(outcome)
        try:
            if outcome.result is not None:
                self._persist_result(outcome, chat_id=chat_id, message_id=message_id)
        except Exception:
            await self._update_terminal_card(
                message_id,
                "failed",
                _approval_diagnostics(
                    stage="结果持久化",
                    error_code="approval_delivery_failed",
                    reason="审批已经处理，但结果写入 Outbox 失败。",
                    approval_id=approval_id,
                    message_id=message_id,
                    turn_id=(
                        None if outcome.result is None else outcome.result.turn_id
                    ),
                    tool_status=(
                        "Tool 可能已经执行；系统不会自动重试，请检查 ToolRun。"
                    ),
                    suggestion="请发送一条新消息检查当前状态。",
                ),
            )
            return

        if outcome.error_code is not None:
            state: ApprovalCardState = (
                "succeeded" if outcome.error_code == "already_decided" else "failed"
            )
            visible = _approval_diagnostics(
                stage="审批状态校验",
                error_code=outcome.error_code,
                reason=visible,
                approval_id=approval_id,
                message_id=message_id,
                turn_id=None,
                tool_status="0 个新 Tool；Core 已阻止本次重复或无效执行。",
                suggestion="请发送一条新消息检查当前状态。",
            )
        elif outcome.decision is ApprovalDecision.DENY:
            state = "denied"
        elif outcome.result is not None:
            state = "succeeded"
        else:
            state = "failed"
        await self._update_terminal_card(message_id, state, visible)

    def _bound_approval_id(self, message_id: str) -> int | None:
        """从唯一 sent receipt 恢复来源卡绑定的 Approval ID；异常时 fail closed。

        Args:
            message_id: 飞书 callback 携带的来源卡平台消息 ID。

        Returns:
            v2 durable envelope 中的审批 ID；来源缺失、歧义、损坏或 legacy v1 时为空。
        """
        try:
            receipt = self._deliveries.find_sent_by_platform_message_id(
                channel="feishu",
                account_id=self._account_id,
                platform_message_id=message_id,
                kind="approval",
            )
            if receipt is None:
                return None
            envelope = parse_approval_delivery_payload(receipt.content)
        except Exception:
            return None
        if not isinstance(envelope, ApprovalEnvelope):
            return None
        return envelope.approval_id

    def _persist_result(
        self,
        outcome: ApprovalCommandOutcome,
        *,
        chat_id: str,
        message_id: str,
    ) -> None:
        """把 continuation 的最终消息或下一条审批原子写入 durable Outbox。"""
        result = outcome.result
        if result is None:
            return
        if result.message_id is not None:
            self._deliveries.create_parts(
                message_id=result.message_id,
                channel="feishu",
                account_id=self._account_id,
                external_conversation_id=chat_id,
                reply_to_message_id=message_id,
                kind="message",
                contents=split_message(
                    result.content,
                    max_chars=self._message_max_chars,
                ),
            )
            return
        if result.approval_id is not None:
            envelope = self._controller.prompt(
                user_id=self._user_id,
                approval_id=result.approval_id,
            )
            notice = self._messages.create_channel_notice(
                result.session_id,
                envelope.fallback_text,
            )
            self._deliveries.create_parts(
                message_id=notice.id,
                channel="feishu",
                account_id=self._account_id,
                external_conversation_id=chat_id,
                reply_to_message_id=message_id,
                kind="approval",
                contents=(approval_delivery_payload(envelope),),
            )
            return
        notice = self._messages.create_channel_notice(result.session_id, result.content)
        self._deliveries.create_parts(
            message_id=notice.id,
            channel="feishu",
            account_id=self._account_id,
            external_conversation_id=chat_id,
            reply_to_message_id=message_id,
            kind="message",
            contents=split_message(
                notice.content,
                max_chars=self._message_max_chars,
            ),
        )

    async def _update_card(
        self,
        message_id: str,
        state: ApprovalCardState,
        visible: str,
    ) -> None:
        """尽力更新原卡；飞书瞬时失败不能阻断 Core Approval 状态机。"""
        try:
            card = feishu_approval_status_card(state, visible)
            async with asyncio.timeout(5.0):
                await self._transport.update_card(message_id, card)
        except Exception:
            return

    async def _update_terminal_card(
        self,
        message_id: str,
        state: ApprovalCardState,
        visible: str,
    ) -> None:
        """终态更新被取消时重试同一卡片一次，再继续传播 Gateway 取消。"""
        try:
            await self._update_card(message_id, state, visible)
        except asyncio.CancelledError:
            await self._update_card(message_id, state, visible)
            raise


def _outcome_visible(outcome: ApprovalCommandOutcome) -> str:
    """选择用户可见的有限 continuation 文本，不回显内部异常。"""
    if outcome.result is not None:
        if outcome.result.approval_id is not None:
            return f"当前操作已完成，下一步需要审批 #{outcome.result.approval_id}。"
        if outcome.result.content.strip():
            return outcome.result.content
    if isinstance(outcome.notice, str) and outcome.notice.strip():
        return outcome.notice
    return "审批处理失败，未执行任何新操作。"


def _approval_diagnostics(
    *,
    stage: str,
    error_code: str,
    reason: str,
    approval_id: int,
    message_id: str,
    turn_id: int | None,
    tool_status: str,
    suggestion: str,
) -> str:
    """生成不含异常正文的审批失败 bullet diagnostics。"""
    stable_code = (
        error_code
        if 0 < len(error_code) <= 64
        and all(
            character == "_"
            or "a" <= character <= "z"
            or "0" <= character <= "9"
            for character in error_code
        )
        else "approval_failed"
    )
    references = [f"Approval #{approval_id}"]
    if turn_id is not None:
        references.insert(0, f"Turn #{turn_id}")
    event_reference = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12]
    references.append(f"Event ref #{event_reference}")
    return "\n".join(
        (
            f"- 失败阶段：{stage}",
            f"- 错误码：`{stable_code}`",
            f"- 原因：{reason}",
            f"- 调试编号：{' · '.join(references)}",
            f"- Tool 状态：{tool_status}",
            f"- 下一步：{suggestion}",
        )
    )
