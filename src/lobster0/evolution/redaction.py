"""为 Feedback 目标消息生成脱敏、有界、可复现哈希的上下文快照。"""

import hashlib
import re

from lobster0.channels.sdk_logging import redact_sdk_text

_ABSOLUTE_PATH = re.compile(r"(?:/[A-Za-z0-9_.\-]+){2,}")
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[A-Za-z]{2,}\b")
_MAX_CONTEXT_CHARS = 4_000


def redact_feedback_context(content: str) -> str:
    """依次脱敏 Secret/Token、邮箱和绝对路径，并裁剪到有界长度。

    Args:
        content: 被评价 assistant message 的原始正文。

    Returns:
        不含凭据、邮箱或本机路径，且不超过 4000 字符的脱敏文本。
    """
    redacted = redact_sdk_text(content)
    redacted = _EMAIL.sub("<redacted-email>", redacted)
    redacted = _ABSOLUTE_PATH.sub("<redacted-path>", redacted)
    if len(redacted) > _MAX_CONTEXT_CHARS:
        redacted = redacted[:_MAX_CONTEXT_CHARS] + "<truncated>"
    return redacted


def feedback_context_hash(redacted_content: str) -> str:
    """返回脱敏文本的稳定 sha256，用于事后检测目标消息是否漂移。

    Args:
        redacted_content: ``redact_feedback_context`` 的输出。

    Returns:
        小写 64 位十六进制摘要；不保存、不还原原文。
    """
    return hashlib.sha256(redacted_content.encode("utf-8")).hexdigest()
