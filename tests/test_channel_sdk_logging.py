"""上游 Channel SDK 日志脱敏边界测试。"""

import io
import logging
import sys
import unittest

from miniclaw.channels.sdk_logging import (
    SafeSdkLogFilter,
    install_feishu_sdk_log_filter,
    redact_sdk_text,
)


class _Unprintable:
    """模拟 ``str`` 自身失败的第三方 SDK 对象。"""

    def __str__(self) -> str:
        """抛出测试用异常。"""
        raise RuntimeError("PRIVATE_UNPRINTABLE_SENTINEL")


class ChannelSdkLoggingTest(unittest.TestCase):
    """验证 SDK 日志保留诊断意义但不暴露连接凭据。"""

    def test_websocket_query_is_redacted_without_mutating_source_record(self) -> None:
        """handler 只能看到脱敏副本，SDK 原始 LogRecord 仍可交给其他逻辑。"""
        logger = logging.Logger("Lark.test.websocket")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(handler)
        self.assertEqual(install_feishu_sdk_log_filter(logger), 1)
        source = logger.makeRecord(
            logger.name,
            logging.INFO,
            __file__,
            1,
            "connecting %s",
            (
                "wss://example.invalid/ws/v2?access_key=ACCESS_SENTINEL"
                "&ticket=TICKET_SENTINEL&device_id=DEVICE_SENTINEL",
            ),
            None,
        )

        logger.handle(source)

        rendered = stream.getvalue()
        self.assertIn("INFO connecting wss://example.invalid/ws/v2?<redacted>", rendered)
        for sentinel in (
            "ACCESS_SENTINEL",
            "TICKET_SENTINEL",
            "DEVICE_SENTINEL",
        ):
            self.assertNotIn(sentinel, rendered)
        self.assertIn("ACCESS_SENTINEL", source.getMessage())

    def test_key_values_bearer_exception_and_stack_info_are_redacted(self) -> None:
        """URL 之外的敏感键、Bearer、异常和 stack_info 也不能绕过 Filter。"""
        logger = logging.Logger("Lark.test.exception")
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s%(stack_info)s"))
        logger.addHandler(handler)
        install_feishu_sdk_log_filter(logger)
        try:
            raise RuntimeError(
                "ticket=EXCEPTION_TICKET_SENTINEL token=EXCEPTION_TOKEN_SENTINEL"
            )
        except RuntimeError:
            exception = sys.exc_info()
        source = logger.makeRecord(
            logger.name,
            logging.ERROR,
            __file__,
            1,
            (
                "access_key=ACCESS_SENTINEL device_id=DEVICE_SENTINEL "
                "Authorization: Bearer BEARER_SENTINEL"
            ),
            (),
            exception,
        )
        source.stack_info = "token=STACK_TOKEN_SENTINEL"

        logger.handle(source)

        rendered = stream.getvalue()
        self.assertIn("access_key=***", rendered)
        self.assertIn("device_id=***", rendered)
        self.assertIn("Authorization: Bearer ***", rendered)
        for sentinel in (
            "ACCESS_SENTINEL",
            "DEVICE_SENTINEL",
            "BEARER_SENTINEL",
            "EXCEPTION_TICKET_SENTINEL",
            "EXCEPTION_TOKEN_SENTINEL",
            "STACK_TOKEN_SENTINEL",
        ):
            self.assertNotIn(sentinel, rendered)

    def test_install_is_idempotent_per_handler(self) -> None:
        """重复启动 Gateway 不能叠加 Filter 或重复日志。"""
        logger = logging.Logger("Lark.test.idempotent")
        first = logging.StreamHandler(io.StringIO())
        second = logging.StreamHandler(io.StringIO())
        logger.handlers[:] = [first, second]

        self.assertEqual(install_feishu_sdk_log_filter(logger), 2)
        self.assertEqual(install_feishu_sdk_log_filter(logger), 0)
        for handler in logger.handlers:
            filters = [item for item in handler.filters if isinstance(item, SafeSdkLogFilter)]
            self.assertEqual(len(filters), 1)

    def test_unprintable_objects_return_stable_placeholder(self) -> None:
        """第三方对象格式化失败时不能抛异常或泄露原异常文本。"""
        rendered = redact_sdk_text(_Unprintable())

        self.assertEqual(rendered, "<unprintable:_Unprintable>")
        self.assertNotIn("PRIVATE_UNPRINTABLE_SENTINEL", rendered)


if __name__ == "__main__":
    unittest.main()
