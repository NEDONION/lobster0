"""第三方 Channel SDK 的进程内日志脱敏边界。"""

import copy
import logging
import re
import traceback

_MAX_LOG_CHARS = 16_384
_URL_QUERY = re.compile(
    r"(?i)\b((?:wss?|https?)://[^\s?'\"<>]+)\?[^\s'\"<>]*"
)
_SENSITIVE_VALUE = re.compile(
    r"(?i)\b(access_key|ticket|token|device_id)\b(\s*[:=]\s*)([^&,;\s]+)"
)
_BEARER = re.compile(
    r"(?i)\b(authorization\s*[:=]\s*bearer\s+)([^,;\s]+)"
)


def redact_sdk_text(value: object) -> str:
    """把 SDK 日志值转换为有限、稳定且不含凭据的文本。

    Args:
        value: SDK 交给 logging 的任意消息、异常或 stack 文本。

    Returns:
        已遮蔽连接 query、敏感键值和 Bearer Token 的有限字符串。

    该函数不会抛出第三方对象的格式化异常。
    """
    try:
        rendered = str(value)
    except Exception:
        rendered = f"<unprintable:{type(value).__name__}>"
    rendered = _URL_QUERY.sub(r"\1?<redacted>", rendered)
    rendered = _SENSITIVE_VALUE.sub(r"\1\2***", rendered)
    rendered = _BEARER.sub(r"\1***", rendered)
    if len(rendered) > _MAX_LOG_CHARS:
        return rendered[:_MAX_LOG_CHARS] + "<truncated>"
    return rendered


class SafeSdkLogFilter(logging.Filter):
    """为单个 handler 返回已脱敏的 ``LogRecord`` 副本。"""

    def filter(self, record: logging.LogRecord) -> logging.LogRecord:
        """复制并清洗最终 message、异常和 stack，不修改 SDK 原记录。

        Args:
            record: 上游 logger 创建的原始记录。

        Returns:
            可安全交给当前 handler formatter 的记录副本。
        """
        clone = copy.copy(record)
        try:
            message = record.getMessage()
        except Exception:
            message = f"<unprintable:{type(record.msg).__name__}>"
        if record.exc_info is not None:
            try:
                message += "\n" + "".join(
                    traceback.format_exception(*record.exc_info)
                )
            except Exception:
                message += "\n<unprintable:exception>"
        clone.msg = redact_sdk_text(message)
        clone.args = ()
        clone.exc_info = None
        clone.exc_text = None
        clone.stack_info = (
            redact_sdk_text(record.stack_info) if record.stack_info else None
        )
        return clone


def install_feishu_sdk_log_filter(logger: logging.Logger | None = None) -> int:
    """为当前 ``Lark`` handlers 幂等安装安全 Filter。

    Args:
        logger: 测试可注入的 logger；默认使用上游 SDK 的 ``Lark`` logger。

    Returns:
        本次新安装 Filter 的 handler 数量。
    """
    target = logger or logging.getLogger("Lark")
    installed = 0
    for handler in target.handlers:
        if any(isinstance(item, SafeSdkLogFilter) for item in handler.filters):
            continue
        handler.addFilter(SafeSdkLogFilter())
        installed += 1
    return installed
