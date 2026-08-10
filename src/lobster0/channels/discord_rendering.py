"""Discord 专属的正文紧凑 Markdown 与公开进度渲染。"""

import re

from lobster0.channels.progress import AgentProgress

_ATX_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_CLOSING_HASHES = re.compile(r"[ \t]+#+$")
_FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,})")


def render_discord_text(content: str, *, max_chars: int) -> str | None:
    """把完整回答压成 Discord 正文字号；不能完整容纳时返回 None。"""
    if not isinstance(content, str) or not content:
        raise ValueError("Discord content must not be empty")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("Discord max chars must be positive")
    bold = _render_lines(content, bold_headings=True)
    if bold and len(bold) <= max_chars:
        return bold
    plain = _render_lines(content, bold_headings=False)
    return plain if plain and len(plain) <= max_chars else None


def render_discord_progress(progress: AgentProgress, *, max_chars: int) -> str:
    """渲染不含 Trail 与 metrics 的有界 Discord 运行状态。"""
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("Discord max chars must be positive")
    if progress.status == "running":
        text = f"⏳ **Lobster0 正在处理**\n{progress.summary}"
    elif progress.status == "completed":
        text = "✅ **已完成**\n回答较长，正在分段发送。"
    else:
        text = f"⚠️ **未完成**\n{progress.summary}"
    return text[:max_chars]


def _render_lines(content: str, *, bold_headings: bool) -> str:
    """逐行转换代码块外标题并折叠代码块外连续空行。"""
    rendered: list[str] = []
    fence: str | None = None
    previous_blank = False
    for line in content.splitlines():
        fence_match = _FENCE.match(line)
        if fence_match is not None:
            marker = fence_match.group(1)
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            rendered.append(line)
            previous_blank = False
            continue
        if fence is not None:
            rendered.append(line)
            continue
        if not line.strip():
            if rendered and not previous_blank:
                rendered.append("")
            previous_blank = True
            continue
        heading = _ATX_HEADING.match(line)
        if heading is None:
            text = line
        else:
            text = _CLOSING_HASHES.sub("", heading.group(1)).rstrip()
        rendered.append(f"**{text}**" if heading is not None and bold_headings else text)
        previous_blank = False
    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)
