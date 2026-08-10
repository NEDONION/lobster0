"""确定性解析 Automation schedule，并统一处理 timezone、DST 与 misfire。"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

from lobster0.automation.models import ScheduleKind, ScheduleSpec

_SCHEDULE_FIELDS = frozenset({"kind", "expression", "timezone"})
_MIN_INTERVAL_SECONDS = 60
_MAX_INTERVAL_SECONDS = 365 * 24 * 60 * 60
_MAX_CRON_CANDIDATES = 10_000


class ScheduleError(ValueError):
    """表示调用者可稳定识别且不泄露原始表达式的调度错误。"""


def parse_schedule(
    raw: Mapping[str, object],
    *,
    now: datetime,
    misfire_grace_seconds: int,
) -> ScheduleSpec:
    """把不可信调度输入规范化为 UTC ``ScheduleSpec``。

    参数：
        raw: 仅允许 kind、expression 和 timezone 的输入映射。
        now: 创建调度时的 aware 时间，内部会转成 UTC。
        misfire_grace_seconds: once 调度允许补做的秒数。

    返回：
        已计算 ``next_run_at`` 的不可变调度规格。

    异常：
        ScheduleError: 输入形状、时间、时区或表达式无效。
    """
    if not isinstance(raw, Mapping) or set(raw) - _SCHEDULE_FIELDS:
        raise ScheduleError("schedule_fields")
    current = _as_utc(now, code="schedule_now")
    if type(misfire_grace_seconds) is not int or misfire_grace_seconds < 0:
        raise ScheduleError("schedule_misfire_grace")

    kind_value = raw.get("kind")
    if not isinstance(kind_value, str):
        raise ScheduleError("schedule_kind")
    try:
        kind = ScheduleKind(kind_value)
    except ValueError as exc:
        raise ScheduleError("schedule_kind") from exc

    expression = raw.get("expression")
    if not isinstance(expression, str) or not expression.strip():
        raise ScheduleError("schedule_expression")
    expression = expression.strip()

    timezone_value = raw.get("timezone", "UTC")
    timezone_name = _parse_timezone_name(timezone_value)

    if kind is ScheduleKind.ONCE:
        slot = _parse_once(expression)
        if current - slot > timedelta(seconds=misfire_grace_seconds):
            raise ScheduleError("schedule_misfire")
        return ScheduleSpec(kind, expression, timezone_name, slot)

    if kind in {ScheduleKind.INTERVAL, ScheduleKind.HEARTBEAT}:
        seconds = _parse_interval(expression)
        return ScheduleSpec(
            kind,
            str(seconds),
            timezone_name,
            current + timedelta(seconds=seconds),
        )

    if "timezone" not in raw:
        raise ScheduleError("schedule_timezone")
    _validate_cron(expression)
    provisional = ScheduleSpec(kind, expression, timezone_name, None)
    return ScheduleSpec(
        kind,
        expression,
        timezone_name,
        next_occurrence(provisional, after=current),
    )


def next_occurrence(spec: ScheduleSpec, *, after: datetime) -> datetime | None:
    """计算严格晚于 ``after`` 的下一个 UTC slot，且不产生 interval 漂移。

    参数：
        spec: 已规范化的调度规格。
        after: 严格下界；允许任意 aware 时区。

    返回：
        下一个 UTC 时间；一次性任务耗尽时返回 ``None``。

    异常：
        ScheduleError: 规格中的表达式、时区或时间无效。
    """
    boundary = _as_utc(after, code="schedule_after")
    if spec.kind is ScheduleKind.ONCE:
        if spec.next_run_at is None:
            return None
        return spec.next_run_at if spec.next_run_at > boundary else None

    if spec.kind in {ScheduleKind.INTERVAL, ScheduleKind.HEARTBEAT}:
        seconds = _parse_interval(spec.expression)
        anchor = spec.next_run_at
        if anchor is None:
            raise ScheduleError("schedule_anchor")
        if boundary < anchor:
            return anchor
        elapsed_seconds = (boundary - anchor).total_seconds()
        periods = int(elapsed_seconds // seconds) + 1
        return anchor + timedelta(seconds=periods * seconds)

    if spec.kind is not ScheduleKind.CRON:
        raise ScheduleError("schedule_kind")
    _validate_cron(spec.expression)
    timezone = _load_timezone(spec.timezone)
    local_boundary = boundary.astimezone(timezone)
    iterator = croniter(spec.expression, local_boundary, ret_type=datetime)
    for _ in range(_MAX_CRON_CANDIDATES):
        try:
            candidate = iterator.get_next(datetime)
        except CroniterError as exc:
            raise ScheduleError("schedule_no_occurrence") from exc
        normalized = candidate.astimezone(UTC).astimezone(timezone)
        candidate_utc = normalized.astimezone(UTC)
        if candidate_utc <= boundary:
            continue
        # croniter 对 aware DST gap 候选可能把 02:30 归一成 03:00 后仍返回 True；
        # 用相同墙钟的 naive 值复核，确保表达式实际匹配用户看见的本地时间。
        if not croniter.match(spec.expression, normalized.replace(tzinfo=None)):
            continue
        if _is_second_fold(normalized):
            continue
        return candidate_utc
    raise ScheduleError("schedule_no_occurrence")


def _as_utc(value: object, *, code: str) -> datetime:
    """把 aware datetime 转为 UTC，并用稳定错误码拒绝 naive 值。"""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError(code)
    return value.astimezone(UTC)


def _parse_timezone_name(value: object) -> str:
    """校验并返回 IANA timezone 名称。"""
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError("schedule_timezone")
    name = value.strip()
    _load_timezone(name)
    return name


def _load_timezone(name: str) -> ZoneInfo:
    """加载 IANA timezone，同时隐藏平台异常细节。"""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ScheduleError("schedule_timezone") from exc


def _parse_once(expression: str) -> datetime:
    """解析含显式 offset 的 RFC 3339 once 时间。"""
    normalized = expression[:-1] + "+00:00" if expression.endswith(("Z", "z")) else expression
    try:
        value = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ScheduleError("schedule_once") from exc
    return _as_utc(value, code="schedule_once")


def _parse_interval(expression: str) -> int:
    """解析边界内的十进制整秒 interval。"""
    if not expression.isascii() or not expression.isdecimal():
        raise ScheduleError("schedule_interval")
    seconds = int(expression)
    if not _MIN_INTERVAL_SECONDS <= seconds <= _MAX_INTERVAL_SECONDS:
        raise ScheduleError("schedule_interval")
    return seconds


def _validate_cron(expression: str) -> None:
    """只接受标准五字段 cron，并隐藏第三方解析异常。"""
    if len(expression.split()) != 5:
        raise ScheduleError("schedule_cron_fields")
    try:
        valid = croniter.is_valid(expression)
    except (CroniterError, ValueError, TypeError) as exc:
        raise ScheduleError("schedule_cron") from exc
    if not valid:
        raise ScheduleError("schedule_cron")


def _is_second_fold(value: datetime) -> bool:
    """判断候选是否为同一墙钟时间的第二个 DST fold。"""
    if value.fold != 1:
        return False
    first = value.replace(fold=0)
    return first.utcoffset() != value.utcoffset()
