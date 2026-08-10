# Discord Compact Single-Reply Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Discord render compact body-sized Markdown and replace its progress message with the complete final answer on the normal short-response path.

**Architecture:** Add a Discord-only pure renderer so the shared Agent Runtime remains channel-agnostic. `DiscordTransport` uses that renderer for progress edits and durable outbound parts, while `ChannelExperience(progress_is_final=True)` suppresses the second delivery only when the whole final answer is visible; long or failed edits keep the existing durable Outbox fallback.

**Tech Stack:** Python 3.12, standard-library `dataclasses`/`re`, `discord.py` facade, `unittest`, JSONL channel evals, Ruff.

## Global Constraints

- Only Discord behavior changes; Feishu rich cards and Telegram preview/final delivery remain unchanged.
- Discord uses `channels.discord.message_max_chars`, whose configured maximum remains exactly `2000`.
- Completed short replies contain no `Claw Trail`, step/tool/model counts, or duration metrics.
- Do not use Discord `-#` subtext for the final answer.
- Do not call a model to rewrite formatting and do not alter facts in the final answer.
- Preserve fenced code, lists, quotes, links, inline code, and mention suppression.
- Final text must never be truncated; over-limit or failed edits fall back to the durable Outbox.
- Every new or modified top-level function/class/method has an accurate Chinese docstring and Python 3.12 type annotations.
- Do not read, print, persist, or commit real Discord tokens, account IDs, guild IDs, channel IDs, or message bodies.
- Preserve unrelated workspace changes under `src/lobster0/checkpoints/`, Sandbox/Tool files, tests, and `.pnpm-store/`.

---

## File Map

- Create `src/lobster0/channels/discord_rendering.py`: pure, offline compact Markdown and progress rendering.
- Create `tests/test_discord_rendering.py`: renderer semantics and length-budget regression tests.
- Modify `src/lobster0/channels/discord.py`: use the renderer for send/create/edit and report exact final visibility.
- Modify `tests/test_discord_transport.py`: one-message terminal edit, long-answer fallback, and safe send tests.
- Modify `src/lobster0/gateway.py`: configure only Discord Experience as final-answer-bearing progress.
- Modify `tests/test_gateway.py`: assert Discord wiring passes `progress_is_final=True`.
- Modify `src/lobster0/evals/cases.py`: register the `compact_reply` channel fixture.
- Modify `src/lobster0/evals/multi_channel.py`: run production Discord renderer evidence.
- Modify `evals/scenarios/discord-channel.v1.jsonl`: add `DISCORD-UX-001`.
- Modify `tests/test_multi_channel_evals.py`: lock the 10 Telegram + 11 Discord matrix.
- Modify `README.md`, `docs/engineering/phase-5/20260808_telegram-discord-channels.md`, `docs/getting-started/20260807_本地运行指南.md`, and `docs/progress/index.html`: document verified compact single-reply behavior and fresh gate counts.

---

### Task 1: Pure Discord Compact Renderer

**Files:**
- Create: `src/lobster0/channels/discord_rendering.py`
- Create: `tests/test_discord_rendering.py`

**Interfaces:**
- Consumes: `AgentProgress` from `lobster0.channels.progress`.
- Produces: `render_discord_text(content: str, *, max_chars: int) -> str | None`.
- Produces: `render_discord_progress(progress: AgentProgress, *, max_chars: int) -> str`.

- [ ] **Step 1: Write failing renderer tests**

Create `tests/test_discord_rendering.py` with focused public-result assertions:

```python
"""Discord 紧凑 Markdown 与 progress renderer 测试。"""

import unittest

from lobster0.channels.discord_rendering import (
    render_discord_progress,
    render_discord_text,
)
from lobster0.channels.progress import ProgressProjector


class DiscordRenderingTest(unittest.TestCase):
    """验证 Discord 正文大小、长度和代码块边界。"""

    def test_headings_are_body_sized_and_blank_runs_collapse(self) -> None:
        content = "# 核心能力\n\n\n## 文件与代码\n\n- 读取 `README.md`"

        rendered = render_discord_text(content, max_chars=2000)

        self.assertEqual(
            rendered,
            "**核心能力**\n\n**文件与代码**\n\n- 读取 `README.md`",
        )

    def test_fenced_code_preserves_heading_syntax_and_blank_lines(self) -> None:
        content = "## 示例\n\n```md\n# 代码里的标题\n\n\n- item\n```"

        rendered = render_discord_text(content, max_chars=2000)

        self.assertEqual(
            rendered,
            "**示例**\n\n```md\n# 代码里的标题\n\n\n- item\n```",
        )

    def test_near_limit_heading_falls_back_without_losing_text(self) -> None:
        content = "# 1234567"

        rendered = render_discord_text(content, max_chars=7)

        self.assertEqual(rendered, "1234567")

    def test_over_limit_complete_text_returns_none_instead_of_truncating(self) -> None:
        self.assertIsNone(render_discord_text("12345678", max_chars=7))

    def test_running_progress_is_compact_and_has_no_trail_or_metrics(self) -> None:
        progress = ProgressProjector(clock=lambda: 0.0).snapshot()

        rendered = render_discord_progress(progress, max_chars=2000)

        self.assertEqual(rendered, "⏳ **Lobster0 正在处理**\n正在理解请求")
        self.assertNotIn("Claw Trail", rendered)
        self.assertNotIn("个工具", rendered)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the renderer tests and verify RED**

Run:

```bash
uv run python -m unittest tests.test_discord_rendering -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'lobster0.channels.discord_rendering'`.

- [ ] **Step 3: Implement the minimal pure renderer**

Create `src/lobster0/channels/discord_rendering.py` with these concrete rules:

```python
"""Discord 专属的正文紧凑 Markdown 与公开进度渲染。"""

import re

from lobster0.channels.progress import AgentProgress

_ATX_HEADING = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^[ \t]*(```|~~~)")


def render_discord_text(content: str, *, max_chars: int) -> str | None:
    """把完整回答压成 Discord 正文字号；不能完整容纳时返回 None。"""
    if not isinstance(content, str) or not content:
        raise ValueError("Discord content must not be empty")
    if type(max_chars) is not int or max_chars <= 0:
        raise ValueError("Discord max chars must be positive")
    bold = _render_lines(content, bold_headings=True)
    if len(bold) <= max_chars:
        return bold
    plain = _render_lines(content, bold_headings=False)
    return plain if len(plain) <= max_chars else None


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
            fence = None if fence == marker else marker if fence is None else fence
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
        text = heading.group(1) if heading is not None else line
        rendered.append(f"**{text}**" if heading is not None and bold_headings else text)
        previous_blank = False
    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)
```

Keep the helper private. Do not add a third-party Markdown dependency.

- [ ] **Step 4: Run renderer tests and Ruff**

Run:

```bash
uv run python -m unittest tests.test_discord_rendering -v
uv run ruff check src/lobster0/channels/discord_rendering.py tests/test_discord_rendering.py
```

Expected: all renderer tests PASS and Ruff exits 0.

- [ ] **Step 5: Commit the renderer slice**

```bash
git add src/lobster0/channels/discord_rendering.py tests/test_discord_rendering.py
git commit -m "feat(discord): 增加 compact Markdown renderer"
```

---

### Task 2: Discord Transport Single-Message Terminal Edit

**Files:**
- Modify: `src/lobster0/channels/discord.py:1-25, 314-435, 752-753`
- Modify: `tests/test_discord_transport.py:1-28, 202-228`

**Interfaces:**
- Consumes: `render_discord_text()` and `render_discord_progress()` from Task 1.
- Produces: completed `ProgressReceipt(platform_message_id, visible_answer_chars)` where the count is either the full original final-answer length or `0`.

- [ ] **Step 1: Change the existing progress test to require one compact final edit**

Replace the old “最终内容见下一条消息” assertion and add long-answer/send coverage:

```python
    async def test_typing_context_and_progress_edit_into_compact_final(self) -> None:
        """短回答应原地替换 progress，并准确报告完整可见字符数。"""
        client = FakeDiscordClient(send_outcomes=(700,))
        transport = self._transport(client)
        await transport.connect()
        event = self._event()

        token = await transport.start_typing(event)
        receipt = await transport.create_progress(
            event,
            _progress("第一段"),
            idempotency_key="progress",
        )
        final = "# 完整回答\n\n\n- 第一项"
        completed = await transport.update_progress(
            receipt.platform_message_id,
            _progress(final, completed=True),
        )
        await transport.stop_typing(token)

        self.assertEqual(client.sent[0]["text"], "⏳ **Lobster0 正在处理**\n正在理解请求")
        self.assertEqual(client.edited[0]["text"], "**完整回答**\n\n- 第一项")
        self.assertEqual(completed.visible_answer_chars, len(final))
        self.assertNotIn("Claw Trail", client.edited[0]["text"])
        self.assertTrue(client.edited[0]["suppress_mentions"])
        await transport.disconnect()

    async def test_over_limit_final_keeps_durable_delivery_required(self) -> None:
        """完整回答超限时 progress 只收口状态并返回零可见正文。"""
        client = FakeDiscordClient(send_outcomes=(700,))
        transport = self._transport(client)
        await transport.connect()
        receipt = await transport.create_progress(
            self._event(),
            _progress("partial"),
            idempotency_key="progress",
        )

        completed = await transport.update_progress(
            receipt.platform_message_id,
            _progress("x" * 2001, completed=True),
        )

        self.assertEqual(completed.visible_answer_chars, 0)
        self.assertEqual(client.edited[0]["text"], "✅ **已完成**\n回答较长，正在分段发送。")
        await transport.disconnect()

    async def test_durable_send_compacts_headings_without_enabling_mentions(self) -> None:
        """Outbox 文本也使用紧凑 renderer，且仍永久关闭 mentions。"""
        client = FakeDiscordClient(send_outcomes=(701,))
        transport = self._transport(client)
        await transport.connect()

        await transport.send(
            self._outbound("# 标题\n\n\n@everyone 正文"),
            idempotency_key="local",
        )

        self.assertEqual(client.sent[0]["text"], "**标题**\n\n@everyone 正文")
        self.assertTrue(client.sent[0]["suppress_mentions"])
        await transport.disconnect()
```

- [ ] **Step 2: Run the transport test and verify RED**

Run:

```bash
uv run python -m unittest tests.test_discord_transport -v
```

Expected: FAIL because completed edit still contains `Claw Trail`/“最终内容见下一条消息”, visibility is `0`, and durable send is not normalized.

- [ ] **Step 3: Use the renderer in DiscordTransport**

Modify `src/lobster0/channels/discord.py`:

```python
from dataclasses import dataclass

from lobster0.channels.discord_rendering import (
    render_discord_progress,
    render_discord_text,
)
```

Remove the no-longer-used `replace` and `render_compact_progress` imports. In `send()` render after multipart reply-target handling and before `_send_text()`:

```python
        visible = render_discord_text(
            message.content,
            max_chars=self._config.message_max_chars,
        )
        if visible is None:
            raise ChannelTransportError("discord_format_error")
        return await self._send_text(
            target_id=target_id,
            reply_to_message_id=reply_to,
            text=visible,
        )
```

In `create_progress()` use `render_discord_progress(progress, max_chars=...)`. Replace `update_progress()` terminal rendering with:

```python
        visible_answer_chars = 0
        visible = None
        if progress.status in {"completed", "incomplete"} and progress.final_answer:
            visible = render_discord_text(
                progress.final_answer,
                max_chars=self._config.message_max_chars,
            )
            if visible is not None:
                visible_answer_chars = len(progress.final_answer)
        if visible is None:
            visible = render_discord_progress(
                progress,
                max_chars=self._config.message_max_chars,
            )
```

Keep the existing edit call and error mapping. Return:

```python
        return ProgressReceipt(platform_message_id, visible_answer_chars)
```

Delete `_bounded_discord_text`; all call sites now use the configured renderer budget.

- [ ] **Step 4: Run transport, renderer, Experience, and Ruff tests**

Run:

```bash
uv run python -m unittest tests.test_discord_rendering tests.test_discord_transport tests.test_channel_experience -v
uv run ruff check src/lobster0/channels/discord.py src/lobster0/channels/discord_rendering.py tests/test_discord_transport.py tests/test_discord_rendering.py
```

Expected: all selected tests PASS and Ruff exits 0.

- [ ] **Step 5: Commit the Transport slice**

```bash
git add src/lobster0/channels/discord.py tests/test_discord_transport.py
git commit -m "feat(discord): 原地收口 compact final reply"
```

---

### Task 3: Wire Discord Experience as the Final Reply

**Files:**
- Modify: `src/lobster0/gateway.py:430-444`
- Modify: `tests/test_gateway.py:1-15, 300+`

**Interfaces:**
- Consumes: Task 2 `ProgressReceipt.visible_answer_chars` semantics.
- Produces: Discord `ChannelExperience(..., progress_is_final=True)` wiring; Telegram remains at the default `False`.

- [ ] **Step 1: Add a failing Discord wiring test**

Import `_build_discord_channel`, `GatewaySecrets`, `AsyncMock`, and `Mock`, then add:

```python
    def test_discord_experience_uses_progress_as_final_reply(self) -> None:
        """Discord 短回答必须由同一 progress 消息承载，Telegram 默认值不被改变。"""
        selected = replace(
            self.config.channels.discord,
            enabled=True,
            owner_user_id=300,
            allowed_user_ids=(300,),
        )
        config = replace(
            self.config,
            channels=replace(self.config.channels, discord=selected),
        )
        manager = SimpleNamespace(receive=AsyncMock(), attach_experience=Mock())
        experience = object()
        with (
            patch(
                "lobster0.gateway._channel_common",
                return_value=(object(), object(), manager, object(), object()),
            ),
            patch("lobster0.gateway.DiscordTransport", return_value=object()),
            patch("lobster0.gateway.DeliveryWorker", return_value=object()),
            patch("lobster0.gateway.ChannelExperience", return_value=experience) as factory,
        ):
            _build_discord_channel(
                config,
                self.paths,
                SimpleNamespace(),
                GatewaySecrets("model", {"discord": "configured"}),
            )

        self.assertTrue(factory.call_args.kwargs["progress_is_final"])
        manager.attach_experience.assert_called_once_with(experience)
```

- [ ] **Step 2: Run the gateway test and verify RED**

Run:

```bash
uv run python -m unittest tests.test_gateway.GatewayTest.test_discord_experience_uses_progress_as_final_reply -v
```

Expected: FAIL with `KeyError: 'progress_is_final'` because Discord currently uses the default `False`.

- [ ] **Step 3: Set the Discord-only final-progress flag**

In `_build_discord_channel()` add the explicit argument:

```python
        ChannelExperience(
            transport=transport,
            progress_enabled=True,
            progress_is_final=True,
            update_interval=limits.progress_update_interval,
            max_visible_chars=selected.message_max_chars,
            observer=observer,
        )
```

Do not add the flag to `_build_telegram_channel()`.

- [ ] **Step 4: Run gateway, manager, Experience, and Discord tests**

Run:

```bash
uv run python -m unittest tests.test_gateway tests.test_channel_manager tests.test_channel_experience tests.test_discord_transport -v
```

Expected: all selected tests PASS. Existing final-progress fallback tests continue to prove that failed edits require a durable final delivery.

- [ ] **Step 5: Commit the wiring slice**

```bash
git add src/lobster0/gateway.py tests/test_gateway.py
git commit -m "feat(gateway): 启用 Discord single-message final"
```

---

### Task 4: Versioned UX Gate and User Documentation

**Files:**
- Modify: `src/lobster0/evals/cases.py:78-98`
- Modify: `src/lobster0/evals/multi_channel.py:1-80`
- Modify: `evals/scenarios/discord-channel.v1.jsonl`
- Modify: `tests/test_multi_channel_evals.py:25-70`
- Modify: `README.md`
- Modify: `docs/engineering/phase-5/20260808_telegram-discord-channels.md:1-15, 85-185`
- Modify: `docs/getting-started/20260807_本地运行指南.md:258-290`
- Modify: `docs/progress/index.html`

**Interfaces:**
- Consumes: Task 1 `render_discord_text()`.
- Produces: `compact_reply` fixture evidence `("heading_compacted", "single_reply_ready")`.
- Produces: active versioned case `DISCORD-UX-001` and fresh 33-case/660-check current gate facts.

- [ ] **Step 1: Add a failing exact-matrix test and JSONL case**

Add `"DISCORD-UX-001"` to `DISCORD_IDS`, change the exact matrix length from 20 to 21, and change the suite assertions to 21. Add this single JSONL line:

```json
{"schema_version":1,"id":"DISCORD-UX-001","title":"Discord 紧凑单回复不渲染大标题","status":"active","layers":["channel","live"],"capability":"channel","query":"# 核心能力\n\n\n## 文件与代码","turns":[],"setup":{"files":{}},"channel":{"fixture":"compact_reply"},"expected":{"channel_evidence":["heading_compacted","single_reply_ready"]},"introduced_by":"discord-compact-single-reply","tags":["discord","ux","rendering"]}
```

- [ ] **Step 2: Run validation and matrix tests to verify RED**

Run:

```bash
uv run python -m unittest tests.test_multi_channel_evals -v
uv run lobster0 eval validate --root evals/scenarios
```

Expected: FAIL because `compact_reply` is not registered or implemented.

- [ ] **Step 3: Implement the offline production-renderer fixture**

Add `"compact_reply"` to `_CHANNEL_FIXTURES`. Import `render_discord_text` in `multi_channel.py`, route only Discord to:

```python
def _discord_compact_reply_evidence(query: str) -> tuple[str, ...]:
    """验证 Discord 生产 renderer 把标题压回正文且完整容纳短回答。"""
    rendered = render_discord_text(query, max_chars=2000)
    if rendered is None or rendered != "**核心能力**\n\n**文件与代码**":
        raise AssertionError("Discord compact reply rendering failed")
    return ("heading_compacted", "single_reply_ready")
```

In `run_multi_channel_fixture()` reject Telegram use explicitly:

```python
    if fixture == "compact_reply":
        if platform != "discord":
            raise AssertionError("compact_reply is Discord-only")
        return _discord_compact_reply_evidence(case.query)
```

- [ ] **Step 4: Run the versioned Channel case and targeted tests**

Run:

```bash
uv run python -m unittest tests.test_multi_channel_evals -v
uv run lobster0 eval validate --root evals/scenarios
uv run lobster0 eval run --suite channel --root evals/scenarios
```

Expected: 33/33 Channel cases PASS: Feishu 12, Telegram 10, Discord 11.

- [ ] **Step 5: Update user-facing documentation with verified behavior**

Make these exact semantic updates without rewriting historical release records:

- Discord normal short replies: compact progress is edited into one final answer.
- `#` through `######` are rendered as body-sized bold text outside fenced code.
- blank-line runs collapse; lists/links/code/quotes remain readable.
- completed final replies omit `Claw Trail` and runtime metrics.
- long or failed edits use durable Outbox parts; mention suppression remains enabled.
- Telegram continues using preview plus independent durable final text.
- Current gate count becomes 33/33 and 20-round soak becomes 660/660 only after fresh commands pass.
- Historical v0.5.0/v0.5.3 32/32 and 640/640 claims remain unchanged when explicitly labeled historical.
- Discord strict 15/15 live gate remains pending until the real acceptance in Task 5.

- [ ] **Step 6: Run docs validation and commit the eval/docs slice**

Run:

```bash
uv run python scripts/validate_docs.py
git diff --check
```

Expected: documentation validation PASS and no whitespace errors.

Commit only these files:

```bash
git add src/lobster0/evals/cases.py src/lobster0/evals/multi_channel.py evals/scenarios/discord-channel.v1.jsonl tests/test_multi_channel_evals.py README.md docs/engineering/phase-5/20260808_telegram-discord-channels.md docs/getting-started/20260807_本地运行指南.md docs/progress/index.html
git commit -m "test(discord): 增加 compact reply Channel gate"
```

---

### Task 5: Full Verification, Gateway Restart, and Real Discord Acceptance

**Files:**
- Modify only if fresh verified counts differ: current-fact sections in the Task 4 documents.
- Do not create evidence containing message bodies or Discord identifiers.

**Interfaces:**
- Consumes: all previous tasks.
- Produces: fresh offline gate evidence, restarted local Gateway, and a real compact single-reply smoke result.

- [ ] **Step 1: Run the full Python and Ruff gates**

Run:

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check .
```

Expected: all tests PASS and Ruff exits 0. Record the actual test count from unittest output rather than predicting it.

- [ ] **Step 2: Run the versioned Channel gate and 20-round soak**

Run:

```bash
uv run lobster0 eval run --suite channel --root evals/scenarios
uv run lobster0 eval run --suite channel --repeat 20 --json --root evals/scenarios
```

Expected: 33/33 and 660/660 PASS. If counts differ because concurrent in-scope cases landed, update only current-fact documentation to the fresh values and rerun validation.

- [ ] **Step 3: Run mechanical and documentation gates**

Run:

```bash
uv run python scripts/validate_docs.py
git diff --check
git status --short
```

Expected: docs PASS, no whitespace errors, and no task files remain uncommitted. Unrelated user files may remain and must not be staged.

- [ ] **Step 4: Restart the existing local Gateway service**

Use read-only service inspection first, then restart the already configured Lobster0 Gateway without reading `.env`:

```bash
launchctl print gui/501/io.lobster0.gateway
launchctl kickstart -k gui/501/io.lobster0.gateway
uv run lobster0 doctor
```

Expected: service is running and Doctor reports Discord locally ready. Do not print environment variables or Token values.

- [ ] **Step 5: Perform the real Discord UX smoke**

From the already signed-in owner account, send one mention requesting a two-section answer with Markdown-style headings. Verify visually:

- exactly one normal Bot reply for the short answer;
- headings render at body size, not Discord header size;
- blank spacing is compact;
- no completed `Claw Trail` or runtime metrics;
- reply remains attached to the owner message;
- no unintended mention is triggered.

Record only `LIVE TARGETED PASS` or the stable failure category in the final handoff; do not save the actual prompt/reply, username, guild name, channel ID, message ID, or screenshot in Git.

- [ ] **Step 6: Final diff review and handoff**

Run:

```bash
git log -5 --oneline
git status --short
```

Review every task-owned diff/commit for secrets, debug output, accidental IDs, and unrelated files. Report exact test/eval counts, live targeted result, known Discord unknown-delivery duplication ceiling, and the design/plan/doc links.
