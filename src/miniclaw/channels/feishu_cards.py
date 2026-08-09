"""从脱敏 AgentProgress 渲染飞书 Card 2.0 与紧凑文本。"""

import json
import re
from dataclasses import dataclass

from miniclaw.channels.progress import AgentProgress, ProgressStatus, ProgressStep
from miniclaw.providers.base import JsonValue

_MAX_CARD_BYTES = 20 * 1024
_STATUS_HEADER: dict[ProgressStatus, tuple[str, str]] = {
    "running": ("blue", "MiniClaw · 执行中"),
    "completed": ("green", "MiniClaw · 已完成"),
    "incomplete": ("red", "MiniClaw · 未完成"),
    "waiting": ("orange", "MiniClaw · 等待中"),
}
_STEP_ICON = {
    "pending": "○",
    "running": "◉",
    "succeeded": "✓",
    "failed": "✕",
    "waiting": "◷",
    "incomplete": "!",
}
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
_FENCE_OPEN = re.compile(r"^\s*(`{3,}|~{3,}).*$")
_HTML_TAG = re.compile(r"<!--.*?-->|</?[A-Za-z][^>\n]*>")
_KEY_VALUE_FIRST_HEADERS = frozenset({"项目", "字段", "属性", "名称"})
_KEY_VALUE_SECOND_HEADERS = frozenset({"内容", "值", "信息", "详情"})


@dataclass(frozen=True, slots=True)
class RenderedProgressCard:
    """保存飞书 Card JSON 与卡片内已覆盖的最终答案字符数。"""

    card: dict[str, JsonValue]
    visible_answer_chars: int


def render_agent_progress_card(progress: AgentProgress) -> RenderedProgressCard:
    """把公开进度渲染为不超过 20 KiB 的单张飞书 Agent 卡片。"""
    answer = progress.final_answer if progress.status != "running" else ""
    detail_indexes = set(range(len(progress.steps)))
    card = _build_card(progress, answer, detail_indexes, answer_trimmed=False)
    if _card_size(card) <= _MAX_CARD_BYTES:
        return RenderedProgressCard(card, len(answer))

    for index in range(len(progress.steps)):
        detail_indexes.discard(index)
        card = _build_card(progress, answer, detail_indexes, answer_trimmed=False)
        if _card_size(card) <= _MAX_CARD_BYTES:
            return RenderedProgressCard(card, len(answer))

    low = 0
    high = len(answer)
    best_card = _build_card(progress, "", detail_indexes, answer_trimmed=bool(answer))
    best_visible = 0
    while low <= high:
        middle = (low + high) // 2
        candidate = _build_card(
            progress,
            answer[:middle],
            detail_indexes,
            answer_trimmed=middle < len(answer),
        )
        if _card_size(candidate) <= _MAX_CARD_BYTES:
            best_card = candidate
            best_visible = middle
            low = middle + 1
        else:
            high = middle - 1
    visible = _safe_markdown_prefix_length(answer, best_visible)
    if visible != best_visible:
        best_card = _build_card(
            progress,
            answer[:visible],
            detail_indexes,
            answer_trimmed=visible < len(answer),
        )
    return RenderedProgressCard(best_card, visible)


def render_compact_progress(progress: AgentProgress) -> str:
    """把同一公开快照压缩为 Telegram/Discord 可编辑的纯文本。"""
    lines = [f"MiniClaw · {_status_label(progress.status)}", progress.summary, "", "Claw Trail"]
    for step in progress.steps:
        icon = _STEP_ICON[step.status]
        duration = _duration_label(step.duration_ms)
        line = f"{step.index}. {icon} {step.title}{duration}"
        if step.detail:
            line += f" — {step.detail}"
        lines.append(line)
    if progress.public_text and progress.status == "running":
        lines.extend(["", "过程摘要", progress.public_text])
    if progress.final_answer:
        lines.extend(["", "最终回答", progress.final_answer])
    lines.extend(["", _metrics(progress)])
    return "\n".join(lines)


def _build_card(
    progress: AgentProgress,
    answer: str,
    detail_indexes: set[int],
    *,
    answer_trimmed: bool,
) -> dict[str, JsonValue]:
    """按给定详情集合和答案前缀构造一帧 Card JSON。"""
    template, title = _STATUS_HEADER[progress.status]
    trail = _trail_markdown(progress.steps, detail_indexes)
    elements: list[JsonValue] = [
        {
            "tag": "markdown",
            "content": f"**{_escape_markdown(progress.summary)}**",
            "text_size": "small",
        },
        {"tag": "hr"},
        {
            "tag": "markdown",
            "content": f"**Claw Trail**\n{trail}",
            "text_size": "small",
        },
    ]
    if progress.public_text and progress.status == "running":
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "**过程摘要**\n" + _escape_markdown(progress.public_text),
                    "text_size": "small",
                },
            ]
        )
    if answer or (answer_trimmed and progress.final_answer):
        answer_content = _render_answer_markdown(answer)
        if answer_trimmed:
            answer_content += "\n\n> _答案过长，剩余内容将继续发送。_"
        elements.extend(
            [
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "**最终回答**\n" + answer_content,
                    "text_size": "small",
                },
            ]
        )
    elements.append(
        {
            "tag": "markdown",
            "content": _metrics(progress),
            "text_size": "small",
        }
    )
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": template,
        },
        "body": {"elements": elements},
    }


def _trail_markdown(steps: tuple[ProgressStep, ...], detail_indexes: set[int]) -> str:
    """把步骤渲染为视觉稳定的编号轨迹。"""
    lines: list[str] = []
    for position, step in enumerate(steps):
        icon = _STEP_ICON[step.status]
        duration = _duration_label(step.duration_ms)
        lines.append(f"**{step.index}. {icon} {_escape_markdown(step.title)}**{duration}")
        if position in detail_indexes and step.detail:
            lines.append(f"<font color='grey'>{_escape_markdown(step.detail)}</font>")
    return "\n".join(lines)


def _render_answer_markdown(answer: str) -> str:
    """规范化最终回答 Markdown，保留结构并仅降级 fence 外的表格。

    参数：
        answer：模型返回的原始最终回答。

    返回：
        适合飞书 Card Markdown 组件的内容；代码 fence 内文本不作变换。
    """
    source = answer.split("\n")
    rendered: list[str] = []
    index = 0
    active_fence: str | None = None
    while index < len(source):
        line = source[index]
        if active_fence is not None:
            rendered.append(line)
            if _is_fence_close(line, active_fence):
                active_fence = None
            index += 1
            continue

        opening_fence = _fence_marker(line)
        if opening_fence is not None:
            rendered.append(line)
            active_fence = opening_fence
            index += 1
            continue

        cells = _table_cells(line)
        separator = _table_cells(source[index + 1]) if index + 1 < len(source) else None
        if (
            cells is not None
            and separator is not None
            and len(cells) == len(separator)
            and _is_table_separator(separator)
        ):
            next_index = index + 2
            rows: list[list[str]] = []
            while next_index < len(source):
                row = _table_cells(source[next_index])
                if row is None or len(row) != len(cells) or not any(row):
                    break
                rows.append(row)
                next_index += 1
            if rows:
                rendered.extend(_table_bullets(cells, rows))
                index = next_index
                continue

        rendered.append(_escape_raw_html(line))
        index += 1

    if active_fence is not None:
        rendered.append(active_fence)
    return "\n".join(rendered)


def _safe_markdown_prefix_length(answer: str, limit: int) -> int:
    """返回不超过字符上限、优先落在 Markdown 结构边界的原始偏移。

    参数：
        answer：未规范化的原始最终回答。
        limit：已由 Card 字节预算确认安全的最大字符偏移。

    返回：
        原始 `answer` 的精确前缀长度；单行文本不会因缺少边界而变成空字符串。
    """
    visible = min(max(limit, 0), len(answer))
    if visible == 0 or visible == len(answer) or answer[:visible].endswith("\n"):
        return visible

    newline = answer.rfind("\n", 0, visible)
    return newline + 1 if newline >= 0 else visible


def _fence_marker(line: str) -> str | None:
    """识别 Markdown fence 起始行，返回用于匹配闭合行的原始 marker。"""
    match = _FENCE_OPEN.match(line)
    return match.group(1) if match is not None else None


def _is_fence_close(line: str, marker: str) -> bool:
    """判断给定行是否闭合指定 Markdown fence。"""
    stripped = line.lstrip()
    if not stripped.startswith(marker[0] * len(marker)):
        return False
    closing = len(stripped) - len(stripped.lstrip(marker[0]))
    return closing >= len(marker) and not stripped[closing:].strip()


def _escape_raw_html(line: str) -> str:
    """把代码 fence 外的原始 HTML 标签转为可见文本，避免触发飞书专用标签。"""
    def escape_tag(match: re.Match[str]) -> str:
        """转义单个已识别的 HTML 标签。"""
        return match.group(0).replace("<", "&lt;").replace(">", "&gt;")

    return "".join(
        segment if is_code else _HTML_TAG.sub(escape_tag, segment)
        for segment, is_code in _inline_code_segments(line)
    )


def _table_cells(line: str) -> list[str] | None:
    """解析可选左右外框管道的表格行，普通文本返回 None。"""
    stripped = line.strip()
    delimiters = _table_delimiter_indexes(stripped)
    if not delimiters:
        return None
    leading = delimiters[0] == 0
    trailing = delimiters[-1] == len(stripped) - 1
    body_start = 1 if leading else 0
    body_end = len(stripped) - 1 if trailing else len(stripped)
    body = stripped[body_start:body_end]
    cells: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "`" and not _is_escaped(body, index):
            code_end = _inline_code_span_end(body, index)
            if code_end is not None:
                current.append(body[index:code_end])
                index = code_end
                continue
        if char == "|" and not _is_escaped(body, index):
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        index += 1
    cells.append("".join(current).strip())
    if len(cells) >= 2 or ((leading or trailing) and len(cells) == 1):
        return cells
    return None


def _table_delimiter_indexes(text: str) -> list[int]:
    """返回 code span 外且未转义的管道位置，用于识别表格列边界。"""
    delimiters: list[int] = []
    index = 0
    while index < len(text):
        if text[index] == "`" and not _is_escaped(text, index):
            code_end = _inline_code_span_end(text, index)
            if code_end is not None:
                index = code_end
                continue
        if text[index] == "|" and not _is_escaped(text, index):
            delimiters.append(index)
        index += 1
    return delimiters


def _inline_code_segments(text: str) -> list[tuple[str, bool]]:
    """把文本分为普通段和匹配的行内 code span，返回段内容及其代码标记。"""
    segments: list[tuple[str, bool]] = []
    plain_start = 0
    index = 0
    while index < len(text):
        if text[index] == "`" and not _is_escaped(text, index):
            code_end = _inline_code_span_end(text, index)
            if code_end is not None:
                if plain_start < index:
                    segments.append((text[plain_start:index], False))
                segments.append((text[index:code_end], True))
                index = code_end
                plain_start = index
                continue
        index += 1
    if plain_start < len(text):
        segments.append((text[plain_start:], False))
    return segments


def _inline_code_span_end(text: str, start: int) -> int | None:
    """返回从未转义反引号开始的同长度行内 code span 结束偏移。"""
    marker_end = start
    while marker_end < len(text) and text[marker_end] == "`":
        marker_end += 1
    marker_length = marker_end - start
    index = marker_end
    while index < len(text):
        if text[index] != "`" or _is_escaped(text, index):
            index += 1
            continue
        candidate_end = index
        while candidate_end < len(text) and text[candidate_end] == "`":
            candidate_end += 1
        if candidate_end - index == marker_length:
            return candidate_end
        index = candidate_end
    return None


def _is_escaped(text: str, index: int) -> bool:
    """判断指定位置是否被奇数个连续反斜线转义。"""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _is_table_separator(cells: list[str]) -> bool:
    """检查给定单元格列表，返回它是否为标准 Markdown 表头分隔行。"""
    return bool(cells) and all(_TABLE_SEPARATOR.fullmatch(cell.replace(" ", "")) for cell in cells)


def _table_bullets(headers: list[str], rows: list[list[str]]) -> list[str]:
    """按给定表头转换数据行，返回忽略空单元格的键值 bullet 列表。"""
    if (
        len(headers) == 2
        and headers[0] in _KEY_VALUE_FIRST_HEADERS
        and headers[1] in _KEY_VALUE_SECOND_HEADERS
    ):
        return [
            f"- **{_escape_raw_html(row[0])}**：{_escape_raw_html(row[1])}"
            for row in rows
            if len(row) >= 2 and row[0] and row[1]
        ]

    bullets: list[str] = []
    for row in rows:
        fields = [
            f"**{_escape_raw_html(header)}**：{_escape_raw_html(value)}"
            for header, value in zip(headers, row, strict=False)
            if header and value
        ]
        if fields:
            bullets.append("- " + "；".join(fields))
    return bullets


def _metrics(progress: AgentProgress) -> str:
    """生成不含供应商标识或请求 ID 的公开运行指标。"""
    parts = [
        f"{len(progress.steps)} 步",
        f"{progress.tool_calls} 次工具请求",
        f"{progress.iterations} 轮模型",
    ]
    if progress.duration_ms is not None:
        parts.append(_human_duration(progress.duration_ms))
    return " · ".join(parts)


def _duration_label(duration_ms: int | None) -> str:
    """把可选毫秒耗时压缩为步骤尾注。"""
    return "" if duration_ms is None else f" · {_human_duration(duration_ms)}"


def _human_duration(duration_ms: int) -> str:
    """用毫秒或一位小数秒展示非负耗时。"""
    if duration_ms < 1_000:
        return f"{duration_ms} ms"
    return f"{duration_ms / 1_000:.1f} s"


def _status_label(status: ProgressStatus) -> str:
    """返回紧凑文本使用的中文状态。"""
    return {
        "running": "执行中",
        "completed": "已完成",
        "incomplete": "未完成",
        "waiting": "等待中",
    }[status]


def _escape_markdown(text: str) -> str:
    """把内部公开字段编码为 Markdown 中不可执行的严格纯文本。"""
    encoded = (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|>~])", r"\\\1", encoded)


def _card_size(card: dict[str, JsonValue]) -> int:
    """按飞书实际 JSON UTF-8 payload 计算保守字节数。"""
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
