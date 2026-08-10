"""once/interval/cron/heartbeat 调度解析、DST 与 misfire 测试。"""

import unittest
from datetime import UTC, datetime

from lobster0.automation.models import ScheduleKind, ScheduleSpec
from lobster0.automation.parser import ScheduleError, next_occurrence, parse_schedule


class ScheduleParserTest(unittest.TestCase):
    """验证 Schedule 结果只由输入、IANA timezone 与固定时钟决定。"""

    def setUp(self) -> None:
        """固定星期五的 UTC 基准时间。"""
        self.now = datetime(2026, 8, 7, 12, tzinfo=UTC)

    def test_weekday_cron_uses_explicit_shanghai_timezone(self) -> None:
        """工作日九点必须按上海墙钟换算，不能使用进程本地时区。"""
        spec = parse_schedule(
            {"kind": "cron", "expression": "0 9 * * 1-5", "timezone": "Asia/Shanghai"},
            now=self.now,
            misfire_grace_seconds=300,
        )

        self.assertEqual(spec.kind, ScheduleKind.CRON)
        self.assertEqual(spec.next_run_at, datetime(2026, 8, 10, 1, tzinfo=UTC))

    def test_dst_fold_wall_clock_slot_occurs_once(self) -> None:
        """纽约秋季 01:30 的两个 fold 只能产生第一个 slot。"""
        spec = ScheduleSpec(
            kind=ScheduleKind.CRON,
            expression="30 1 * * *",
            timezone="America/New_York",
            next_run_at=None,
        )

        first = next_occurrence(spec, after=datetime(2026, 11, 1, 4, tzinfo=UTC))
        second = next_occurrence(spec, after=first)

        self.assertEqual(first, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        self.assertEqual(second, datetime(2026, 11, 2, 6, 30, tzinfo=UTC))

    def test_dst_gap_skips_nonexistent_local_time(self) -> None:
        """纽约春季不存在的 02:30 不能被 croniter 的 03:00 替代。"""
        spec = ScheduleSpec(
            kind=ScheduleKind.CRON,
            expression="30 2 * * *",
            timezone="America/New_York",
            next_run_at=None,
        )

        result = next_occurrence(spec, after=datetime(2026, 3, 8, 5, tzinfo=UTC))

        self.assertEqual(result, datetime(2026, 3, 9, 6, 30, tzinfo=UTC))

    def test_interval_advances_from_anchor_without_worker_drift(self) -> None:
        """Worker 晚到不会把未来 interval 永久平移。"""
        spec = parse_schedule(
            {"kind": "interval", "expression": "3600", "timezone": "UTC"},
            now=datetime(2026, 8, 9, 8, tzinfo=UTC),
            misfire_grace_seconds=300,
        )

        result = next_occurrence(spec, after=datetime(2026, 8, 9, 10, 30, tzinfo=UTC))

        self.assertEqual(spec.next_run_at, datetime(2026, 8, 9, 9, tzinfo=UTC))
        self.assertEqual(result, datetime(2026, 8, 9, 11, tzinfo=UTC))

    def test_once_misfire_respects_grace_and_leap_cron_is_supported(self) -> None:
        """轻微延迟可补做；过期 once 拒绝；闰日由 croniter 正确推进。"""
        inside = parse_schedule(
            {"kind": "once", "expression": "2026-08-09T07:58:00+00:00"},
            now=datetime(2026, 8, 9, 8, tzinfo=UTC),
            misfire_grace_seconds=300,
        )
        self.assertEqual(inside.next_run_at, datetime(2026, 8, 9, 7, 58, tzinfo=UTC))
        with self.assertRaisesRegex(ScheduleError, "schedule_misfire"):
            parse_schedule(
                {"kind": "once", "expression": "2026-08-09T07:00:00+00:00"},
                now=datetime(2026, 8, 9, 8, tzinfo=UTC),
                misfire_grace_seconds=300,
            )
        leap = parse_schedule(
            {"kind": "cron", "expression": "0 9 29 2 *", "timezone": "Asia/Shanghai"},
            now=datetime(2026, 3, 1, tzinfo=UTC),
            misfire_grace_seconds=300,
        )
        self.assertEqual(leap.next_run_at, datetime(2028, 2, 29, 1, tzinfo=UTC))

    def test_invalid_shapes_types_timezone_and_cron_fail_closed(self) -> None:
        """未知字段、bool、短 interval、六字段 cron 和坏时区必须稳定拒绝。"""
        invalid = (
            ({"kind": "interval", "expression": "60", "unknown": 1}, "schedule_fields"),
            ({"kind": True, "expression": "60"}, "schedule_kind"),
            ({"kind": "interval", "expression": True}, "schedule_expression"),
            ({"kind": "interval", "expression": "59"}, "schedule_interval"),
            ({"kind": "interval", "expression": "31536001"}, "schedule_interval"),
            ({"kind": "cron", "expression": "0 0 9 * * *", "timezone": "UTC"},
             "schedule_cron_fields"),
            ({"kind": "cron", "expression": "bad cron value", "timezone": "UTC"},
             "schedule_cron_fields"),
            ({"kind": "cron", "expression": "0 9 * * *"}, "schedule_timezone"),
            ({"kind": "cron", "expression": "0 9 * * *", "timezone": "Mars/Olympus"},
             "schedule_timezone"),
        )
        for raw, code in invalid:
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(ScheduleError, code):
                    parse_schedule(raw, now=self.now, misfire_grace_seconds=300)


if __name__ == "__main__":
    unittest.main()
