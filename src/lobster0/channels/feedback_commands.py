"""飞书 ``/good``、``/bad`` 反馈命令的严格解析与 Owner 归属路由。"""

import re
from dataclasses import dataclass
from typing import Protocol

from lobster0.evolution.models import Feedback, FeedbackRating
from lobster0.evolution.redaction import feedback_context_hash, redact_feedback_context
from lobster0.evolution.repository import EvolutionError

_GOOD = re.compile(r"/good\Z")
_BAD = re.compile(r"/bad(?:[ \t]+(.+))?\Z", re.DOTALL)
# 提到"更正"不是啰嗦：想改掉一条记错的记忆，措辞必须命中既有的明确纠错意图
# （更正/纠正/改成/更新记忆）。不写出来，这个能力对 Owner 就是隐形的。
_USAGE = (
    "用法：回复一条 Lobster0 的回答，发送 /good 或 /bad <原因>。\n"
    "想改掉记错的事实，请在原因里写「更正：……」，例如 /bad 更正：我的部署机是 mac。"
)
_NOT_A_REPLY = (
    "请先长按并「回复」某一条 Lobster0 的回答，再发送 /good 或 /bad <原因>——"
    "否则无法确定你在评价哪一条。"
)
# 只说"没有找到"是个死胡同：Owner 无从判断该怎么办。把可能的原因列清楚。
_TARGET_NOT_FOUND = (
    "这条消息不能记录反馈。可能的原因："
    "①你回复的是自己发的消息，或是 /reset、/permissions 这类控制提示，"
    "它们不是模型给出的回答；"
    "②这张卡片是更早版本的 Lobster0 发出的，当时没有登记可反查的投递记录。"
    "请改为回复一条 Lobster0 的回答或结果卡片后重试。"
)


class FeedbackLedger(Protocol):
    """收窄 Controller 对 Core FeedbackRepository 的写入能力。"""

    def record(
        self,
        *,
        owner_id: int,
        message_id: int,
        rating: FeedbackRating,
        redacted_reason: str | None,
        context_hash: str,
        reason_message_id: int | None = None,
    ) -> Feedback:
        """持久化一条脱敏后的反馈。"""
        ...


class TargetMessage(Protocol):
    """收窄 Controller 需要的目标消息字段。"""

    id: int
    session_id: int
    role: str
    content: str


class DeliveryLookup(Protocol):
    """收窄 Controller 按平台 receipt 反查内部消息的能力。"""

    def find_sent_by_platform_message_id(
        self,
        *,
        channel: str,
        account_id: str,
        platform_message_id: str,
        kind: str,
    ) -> object | None:
        """返回匹配平台 message ID 的唯一已发送 Delivery，或 None。"""
        ...


class MessageLookup(Protocol):
    """收窄 Controller 按内部 ID 读取消息的能力。"""

    def get(self, message_id: int) -> TargetMessage | None:
        """按内部 ID 读取一条消息；不存在时返回 None。"""
        ...

    def create_feedback_reason(self, session_id: int, content: str) -> TargetMessage:
        """把 Owner 在 ``/bad`` 里说的原话落成一条 user message。"""
        ...


@dataclass(frozen=True, slots=True)
class FeedbackCommandOutcome:
    """描述一条消息是否已作为 /good、/bad 命令消费。"""

    handled: bool
    notice: str | None = None
    feedback_id: int | None = None
    rating: FeedbackRating | None = None
    error_code: str | None = None


class ChannelFeedbackController:
    """把任意 Channel 的 /good、/bad 文本命令路由到 FeedbackRepository。

    Args:
        owner_external_user_id: 唯一允许发出反馈命令的平台身份。
        feedback: 持久化脱敏反馈的 Repository。
        deliveries: 把回复目标的平台 message ID 反查为内部 Delivery 的 Repository。
        messages: 按内部 ID 读取目标消息的 Repository。

    这里刻意不在内部判断 chat_type：群聊白名单和 @mention 已经在 Channel Adapter
    完成，能到达这里的消息本来就只剩 Owner 私聊或已放行群聊。
    """

    def __init__(
        self,
        *,
        owner_external_user_id: str,
        feedback: FeedbackLedger,
        deliveries: DeliveryLookup,
        messages: MessageLookup,
    ) -> None:
        """绑定唯一 Owner 身份与三个协作 Repository。"""
        if not owner_external_user_id:
            raise ValueError("owner_external_user_id must not be empty")
        self._owner_external_user_id = owner_external_user_id
        self._feedback = feedback
        self._deliveries = deliveries
        self._messages = messages

    async def handle_text(
        self,
        *,
        user_id: int,
        actor_external_user_id: str,
        text: str,
        channel: str,
        account_id: str,
        reply_to_platform_message_id: str,
    ) -> FeedbackCommandOutcome:
        """严格识别 /good、/bad 文本命令；普通自然语言返回 handled=False。"""
        normalized = text.strip()
        parsed = _parse_text_command(normalized)
        if parsed is None:
            if normalized.startswith(("/good", "/bad")):
                return FeedbackCommandOutcome(True, notice=_USAGE)
            return FeedbackCommandOutcome(False)
        rating, raw_reason = parsed

        if actor_external_user_id != self._owner_external_user_id:
            return FeedbackCommandOutcome(
                True,
                notice="只有 Owner 可以记录反馈。",
                error_code="not_owner",
            )
        if not reply_to_platform_message_id:
            return FeedbackCommandOutcome(
                True, notice=_NOT_A_REPLY, error_code="not_a_reply"
            )

        # Owner 在 IM 里看到的可能是普通文本，也可能是 Experience 直接发出的卡片；
        # 两种都要能反查回同一条内部 assistant message。
        delivery = None
        for kind in ("message", "card"):
            delivery = self._deliveries.find_sent_by_platform_message_id(
                channel=channel,
                account_id=account_id,
                platform_message_id=reply_to_platform_message_id,
                kind=kind,
            )
            if delivery is not None:
                break
        message_id = getattr(delivery, "message_id", None)
        message = None if message_id is None else self._messages.get(message_id)
        if message is None or message.role != "assistant":
            return FeedbackCommandOutcome(
                True,
                notice=_TARGET_NOT_FOUND,
                error_code="target_not_found",
            )

        redacted_reason = None if raw_reason is None else redact_feedback_context(raw_reason)
        context_hash = feedback_context_hash(redact_feedback_context(message.content))
        # Owner 的原话必须落成一条真实 user message，否则 Memory correction
        # 永远拿不到合法的 SourceRef——记忆的出处不能是编的。
        # 落库失败不能连累反馈本身：反馈仍然记下，只是这条反馈不能再用于改记忆。
        reason_message_id: int | None = None
        if raw_reason is not None:
            try:
                reason_message_id = self._messages.create_feedback_reason(
                    message.session_id, raw_reason
                ).id
            except Exception:  # noqa: BLE001 - 出处缺失不该让 /bad 整体失败
                reason_message_id = None
        try:
            feedback = self._feedback.record(
                owner_id=user_id,
                message_id=message.id,
                rating=rating,
                redacted_reason=redacted_reason,
                context_hash=context_hash,
                reason_message_id=reason_message_id,
            )
        except EvolutionError as error:
            return FeedbackCommandOutcome(
                True,
                notice=_feedback_error_notice(error.code),
                rating=rating,
                error_code=error.code,
            )
        return FeedbackCommandOutcome(
            True,
            notice=_confirmation_notice(feedback),
            feedback_id=feedback.id,
            rating=rating,
        )


def _parse_text_command(text: str) -> tuple[FeedbackRating, str | None] | None:
    """把 exact /good、/bad 文本解析为 rating 和可选原因。"""
    if _GOOD.fullmatch(text) is not None:
        return FeedbackRating.GOOD, None
    match = _BAD.fullmatch(text)
    if match is not None:
        reason = match.group(1)
        return FeedbackRating.BAD, None if reason is None else reason.strip() or None
    return None


def _confirmation_notice(feedback: Feedback) -> str:
    """构建只含反馈编号、rating 和时间的确认卡文本，不展示上下文。"""
    label = "好评" if feedback.rating is FeedbackRating.GOOD else "差评"
    return (
        f"已记录反馈 #{feedback.id}（{label}），时间 {feedback.created_at.isoformat()}。"
        "完整上下文不会展示在这里；如需查看，请使用本机 CLI。"
    )


def _feedback_error_notice(code: str) -> str:
    """把稳定 EvolutionError code 映射为不泄露内部细节的提示。"""
    if code == "feedback_already_recorded":
        return "你已经为这条回答记录过反馈了。"
    return "记录反馈失败，请稍后重试。"
