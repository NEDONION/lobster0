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
_LIST_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.+)$")
_HEADING_PREFIX = re.compile(r"^\s*#{1,6}\s+(.+)$")
_TABLE_SEPARATOR = re.compile(r"^:?-{3,}:?$")
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
    return RenderedProgressCard(best_card, best_visible)


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
        answer_content = _escape_markdown(_answer_as_bullets(answer))
        if answer_trimmed:
            answer_content += "\n- _答案过长，剩余内容将继续发送。_"
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


def _answer_as_bullets(answer: str) -> str:
    """把最终回答转为项目符号；参数是原始答案，返回无表格的 Markdown 列表。"""
    source = answer.splitlines()
    bullets: list[str] = []
    index = 0
    while index < len(source):
        line = source[index]
        cells = _table_cells(line)
        separator = _table_cells(source[index + 1]) if index + 1 < len(source) else None
        if cells is not None and separator is not None and _is_table_separator(separator):
            index += 2
            rows: list[list[str]] = []
            while index < len(source):
                row = _table_cells(source[index])
                if row is None:
                    break
                rows.append(row)
                index += 1
            bullets.extend(_table_bullets(cells, rows))
            continue

        content = line.strip()
        index += 1
        if not content:
            continue
        list_match = _LIST_PREFIX.fullmatch(content)
        heading_match = _HEADING_PREFIX.fullmatch(content)
        if list_match is not None:
            content = list_match.group(1).strip()
        elif heading_match is not None:
            content = f"**{heading_match.group(1).strip()}**"
        elif content.startswith(">"):
            content = content[1:].strip()
        if content:
            bullets.append(f"- {content}")
    return "\n".join(bullets)


def _table_cells(line: str) -> list[str] | None:
    """解析一行原始文本；表格行返回单元格列表，普通文本返回 None。"""
    stripped = line.strip()
    if not stripped.startswith("|") or not stripped.endswith("|"):
        return None
    cells = [cell.strip() for cell in stripped[1:-1].split("|")]
    return cells if cells else None


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
            f"- **{row[0]}**：{row[1]}"
            for row in rows
            if len(row) >= 2 and row[0] and row[1]
        ]

    bullets: list[str] = []
    for row in rows:
        fields = [
            f"**{header}**：{value}"
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
        f"{progress.tool_calls} 个工具",
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
    """转义会改变内联代码语义的 Markdown 字符。"""
    return text.replace("\\", "\\\\").replace("`", "\\`")


def _card_size(card: dict[str, JsonValue]) -> int:
    """按飞书实际 JSON UTF-8 payload 计算保守字节数。"""
    return len(json.dumps(card, ensure_ascii=False).encode("utf-8"))
