"""把 Agent Runtime 事件投影为可公开展示的脱敏进度。"""

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import PurePath
from typing import Literal
from urllib.parse import urlsplit

from miniclaw.agent.events import RunEvent
from miniclaw.providers.base import JsonValue

type ProgressStatus = Literal["running", "completed", "incomplete", "waiting"]
type StepStatus = Literal[
    "pending", "running", "succeeded", "failed", "waiting", "incomplete"
]

_MAX_STEPS = 16
_MAX_FIELD_CHARS = 240
_MAX_METADATA_BYTES = 16 * 1024
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class ProgressStep:
    """保存一条已经脱敏且可跨平台展示的 Agent 步骤。"""

    index: int
    call_id: str | None
    title: str
    detail: str
    status: StepStatus
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True, repr=False)
class AgentProgress:
    """保存一次 Turn 的有界公开进度快照。"""

    status: ProgressStatus
    summary: str
    steps: tuple[ProgressStep, ...]
    public_text: str
    final_answer: str
    iterations: int
    tool_calls: int
    input_tokens: int | None
    output_tokens: int | None
    duration_ms: int | None

    def __repr__(self) -> str:
        """返回不包含正文、目标或答案的安全诊断表示。"""
        return (
            "AgentProgress("
            f"status={self.status!r}, steps={len(self.steps)}, "
            f"iterations={self.iterations}, tool_calls={self.tool_calls})"
        )


class ProgressProjector:
    """按事件顺序生成有界且不含原始敏感字段的公开快照。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        public_text_limit: int = 20_000,
    ) -> None:
        """绑定 monotonic 时钟、公开文本预算并初始化理解请求步骤。"""
        if type(public_text_limit) is not int or public_text_limit <= 0:
            raise ValueError("public text limit must be positive")
        self._clock = clock or time.monotonic
        self._public_text_limit = public_text_limit
        self._started_at = self._clock()
        self._steps: list[ProgressStep] = [
            ProgressStep(1, None, "理解请求", "正在分析任务并规划执行步骤", "running")
        ]
        self._call_positions: dict[str, int] = {}
        self._public_text = ""
        self._iterations = 0
        self._tool_calls = 0
        self._input_tokens: int | None = None
        self._output_tokens: int | None = None
        self._finished: AgentProgress | None = None

    def apply(self, event: RunEvent) -> bool:
        """消费一个 Runtime 事件；返回公开快照是否发生可见变化。"""
        if self._finished is not None or event.kind == "model_reasoning":
            return False
        if event.kind == "model_text_delta":
            text = event.data.get("text")
            if isinstance(text, str) and text:
                self._public_text = _clean(
                    text=self._public_text + text,
                    limit=self._public_text_limit,
                )
                return True
            return False
        if event.kind == "model_usage":
            self._iterations = _non_negative_int(event.data.get("iteration"), self._iterations)
            self._tool_calls = _non_negative_int(event.data.get("tool_calls"), self._tool_calls)
            self._input_tokens = _optional_non_negative_int(event.data.get("input_tokens"))
            self._output_tokens = _optional_non_negative_int(event.data.get("output_tokens"))
            return True
        if event.kind == "tool_requested":
            return self._request_tool(event.data)
        if event.kind == "tool_started":
            return self._set_tool_status(event.data, "running")
        if event.kind == "tool_finished":
            raw_status = event.data.get("status")
            status: StepStatus = "succeeded" if raw_status == "succeeded" else "failed"
            return self._set_tool_status(event.data, status)
        if event.kind == "approval_required":
            return self._set_latest_waiting()
        return event.kind in {"turn_started", "turn_finished", "turn_failed", "turn_cancelled"}

    def snapshot(self) -> AgentProgress:
        """返回当前运行中进度，不冻结后续事件。"""
        if self._finished is not None:
            return self._finished
        return self._build(status="running", summary=_running_summary(self._steps))

    def finish(self, content: str | None, *, failed: bool) -> AgentProgress:
        """幂等冻结终态；失败时保留已完成步骤并标记未完成步骤。"""
        if self._finished is not None:
            return self._finished
        final_steps: list[ProgressStep] = []
        for step in self._steps:
            if step.status in {"running", "pending"}:
                status: StepStatus = "incomplete" if failed else "succeeded"
                final_steps.append(replace(step, status=status))
            else:
                final_steps.append(step)
        self._steps = final_steps
        status: ProgressStatus = "incomplete" if failed else "completed"
        summary = "任务未完整完成" if failed else "任务已完成"
        self._finished = self._build(
            status=status,
            summary=summary,
            final_answer=content if isinstance(content, str) else "",
        )
        return self._finished

    def _request_tool(self, data: dict[str, JsonValue]) -> bool:
        """从 Tool 请求中只提取精确白名单字段并新建公开步骤。"""
        call_id = data.get("call_id")
        tool_name = data.get("tool_name")
        arguments = data.get("arguments")
        if not isinstance(call_id, str) or not call_id or not isinstance(tool_name, str):
            return False
        if call_id in self._call_positions:
            return False
        safe_arguments = arguments if isinstance(arguments, dict) else {}
        title, detail = _tool_summary(tool_name, safe_arguments)
        self._complete_understanding()
        step = ProgressStep(
            index=len(self._steps) + 1,
            call_id=_clean(text=call_id, limit=80),
            title=title,
            detail=detail,
            status="pending",
        )
        self._steps.append(step)
        self._call_positions[call_id] = len(self._steps) - 1
        self._tool_calls = max(self._tool_calls, len(self._call_positions))
        self._bound_steps()
        return True

    def _set_tool_status(self, data: dict[str, JsonValue], status: StepStatus) -> bool:
        """按 call_id 更新步骤状态，忽略 preview 等原始 Tool 输出。"""
        call_id = data.get("call_id")
        if not isinstance(call_id, str):
            return False
        position = self._call_positions.get(call_id)
        if position is None or position >= len(self._steps):
            return False
        current = self._steps[position]
        duration = _optional_non_negative_int(data.get("duration_ms"))
        self._steps[position] = replace(current, status=status, duration_ms=duration)
        return True

    def _set_latest_waiting(self) -> bool:
        """把最近一条未完成 Tool 步骤标为等待授权。"""
        for position in range(len(self._steps) - 1, -1, -1):
            if self._steps[position].status in {"pending", "running"}:
                self._steps[position] = replace(self._steps[position], status="waiting")
                return True
        return False

    def _complete_understanding(self) -> None:
        """首次实际动作前完成理解请求步骤。"""
        if self._steps[0].status == "running":
            self._steps[0] = replace(self._steps[0], status="succeeded")

    def _bound_steps(self) -> None:
        """把超过预算的最早步骤折叠成一个确定性摘要。"""
        if len(self._steps) <= _MAX_STEPS:
            return
        removed = len(self._steps) - (_MAX_STEPS - 1)
        kept = [self._steps[0], *self._steps[-(_MAX_STEPS - 2) :]]
        folded = ProgressStep(
            2,
            None,
            f"较早步骤（{removed}）",
            "已折叠以保持卡片简洁",
            "succeeded",
        )
        self._steps = [kept[0], folded, *kept[1:]]
        self._steps = [replace(step, index=index + 1) for index, step in enumerate(self._steps)]
        self._call_positions = {
            step.call_id: index
            for index, step in enumerate(self._steps)
            if step.call_id is not None
        }

    def _build(
        self,
        *,
        status: ProgressStatus,
        summary: str,
        final_answer: str = "",
    ) -> AgentProgress:
        """从当前私有状态复制一个不可变公开快照。"""
        duration_ms = max(0, round((self._clock() - self._started_at) * 1000))
        return AgentProgress(
            status=status,
            summary=_clean(text=summary, limit=_MAX_FIELD_CHARS),
            steps=tuple(self._steps),
            public_text=_clean(text=self._public_text, limit=self._public_text_limit),
            final_answer=_clean(text=final_answer, limit=100_000),
            iterations=self._iterations,
            tool_calls=self._tool_calls,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            duration_ms=duration_ms,
        )


def progress_to_metadata(progress: AgentProgress) -> dict[str, JsonValue]:
    """序列化最终脱敏 trace；正文和最终答案永不写入 trace。"""
    value: dict[str, JsonValue] = {
        "version": 1,
        "status": progress.status,
        "summary": progress.summary,
        "steps": [
            {
                "index": step.index,
                "call_id": step.call_id,
                "title": step.title,
                "detail": step.detail,
                "status": step.status,
                "duration_ms": step.duration_ms,
            }
            for step in progress.steps
        ],
        "iterations": progress.iterations,
        "tool_calls": progress.tool_calls,
        "input_tokens": progress.input_tokens,
        "output_tokens": progress.output_tokens,
        "duration_ms": progress.duration_ms,
    }
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_METADATA_BYTES:
        raise ValueError("experience trace exceeds 16 KiB")
    return value


def progress_from_metadata(
    value: dict[str, JsonValue],
    final_answer: str,
) -> AgentProgress | None:
    """从严格的脱敏 trace 恢复卡片状态；损坏或未知版本返回 None。"""
    if value.get("version") != 1:
        return None
    status = value.get("status")
    summary = value.get("summary")
    raw_steps = value.get("steps")
    if status not in {"running", "completed", "incomplete", "waiting"}:
        return None
    if not isinstance(summary, str) or not isinstance(raw_steps, list):
        return None
    steps: list[ProgressStep] = []
    for raw in raw_steps[:_MAX_STEPS]:
        if not isinstance(raw, dict):
            return None
        index = raw.get("index")
        call_id = raw.get("call_id")
        title = raw.get("title")
        detail = raw.get("detail")
        step_status = raw.get("status")
        duration = raw.get("duration_ms")
        if (
            type(index) is not int
            or index < 1
            or (call_id is not None and not isinstance(call_id, str))
            or not isinstance(title, str)
            or not isinstance(detail, str)
            or step_status
            not in {"pending", "running", "succeeded", "failed", "waiting", "incomplete"}
            or (duration is not None and (type(duration) is not int or duration < 0))
        ):
            return None
        steps.append(
            ProgressStep(
                index=index,
                call_id=call_id,
                title=_clean(text=title, limit=_MAX_FIELD_CHARS),
                detail=_clean(text=detail, limit=_MAX_FIELD_CHARS),
                status=step_status,
                duration_ms=duration,
            )
        )
    iterations = _metadata_int(value.get("iterations"))
    tool_calls = _metadata_int(value.get("tool_calls"))
    if iterations is None or tool_calls is None:
        return None
    return AgentProgress(
        status=status,
        summary=_clean(text=summary, limit=_MAX_FIELD_CHARS),
        steps=tuple(steps),
        public_text="",
        final_answer=_clean(text=final_answer, limit=100_000),
        iterations=iterations,
        tool_calls=tool_calls,
        input_tokens=_metadata_optional_int(value.get("input_tokens")),
        output_tokens=_metadata_optional_int(value.get("output_tokens")),
        duration_ms=_metadata_optional_int(value.get("duration_ms")),
    )


def _tool_summary(tool_name: str, arguments: dict[str, JsonValue]) -> tuple[str, str]:
    """按精确 Tool 名称提取允许公开的标题与目标。"""
    safe_name = _clean(text=tool_name, limit=80) or "未知工具"
    if tool_name == "run_command":
        return _command_summary(arguments)
    if tool_name == "read_file":
        return "查看文件", _safe_scalar(arguments.get("path"), "受限文件")
    if tool_name == "write_file":
        return "写入文件", _safe_scalar(arguments.get("path"), "受限文件")
    if tool_name == "edit_file":
        return "编辑文件", _safe_scalar(arguments.get("path"), "受限文件")
    if tool_name == "glob":
        pattern = _safe_scalar(arguments.get("pattern"), "文件")
        root = _safe_scalar(arguments.get("root"), ".")
        return "查找文件", _clean(text=f"{root} · {pattern}", limit=_MAX_FIELD_CHARS)
    if tool_name == "grep":
        root = _safe_scalar(arguments.get("root"), ".")
        pattern = _safe_scalar(arguments.get("pattern"), "关键词")
        return "搜索文件内容", _clean(text=f"{root} · {pattern}", limit=_MAX_FIELD_CHARS)
    if tool_name == "http_get":
        url = arguments.get("url")
        hostname = urlsplit(url).hostname if isinstance(url, str) else None
        return "访问公开网页", _clean(text=hostname or "HTTPS 资源", limit=_MAX_FIELD_CHARS)
    if tool_name == "read_memory":
        return "查看记忆", _safe_scalar(arguments.get("scope"), "固定范围")
    if tool_name == "propose_memory":
        return "更新记忆", "仅记录经安全校验的事实"
    if tool_name == "memory_remember":
        return "记住事实", "仅记录 Owner 明确要求的事实"
    if tool_name == "system_info":
        return "查看系统信息", _safe_scalar(arguments.get("kind"), "本机公开信息")
    return "调用工具", safe_name


def _command_summary(arguments: dict[str, JsonValue]) -> tuple[str, str]:
    """仅公开命令名；lark-cli 额外公开固定业务域与动作。"""
    program = arguments.get("program")
    raw_args = arguments.get("args")
    executable = PurePath(program).name if isinstance(program, str) and program else "命令"
    args = raw_args if isinstance(raw_args, list) else []
    lark_action = (
        [args[0], args[1]]
        if len(args) >= 2
        and all(isinstance(value, str) for value in args[:2])
        and executable in {"lark-cli", "run.js"}
        else []
    )
    detail = _clean(text=" ".join([executable, *lark_action]), limit=_MAX_FIELD_CHARS)
    if lark_action == ["drive", "+search"]:
        title = "查询飞书云空间"
    elif executable in {"lark-cli", "run.js"}:
        title = "调用飞书工具"
    else:
        title = "执行命令"
    return title, detail


def _safe_scalar(value: JsonValue, fallback: str) -> str:
    """把白名单中的单个字符串规范为有界展示值。"""
    return _clean(text=value if isinstance(value, str) else fallback, limit=_MAX_FIELD_CHARS)


def _clean(*, text: str, limit: int) -> str:
    """移除控制字符、合并空白并按 Unicode 字符数截断。"""
    normalized = _SPACE.sub(" ", _CONTROL_CHARACTERS.sub("", text)).strip()
    return normalized[:limit]


def _running_summary(steps: list[ProgressStep]) -> str:
    """从步骤状态生成无需模型调用的公开阶段摘要。"""
    latest = steps[-1]
    if latest.status == "waiting":
        return "等待授权后继续"
    if latest.call_id is not None:
        return f"正在执行：{latest.title}"
    return "正在理解请求"


def _non_negative_int(value: JsonValue, fallback: int) -> int:
    """读取非负整数，否则保留已有计数。"""
    return value if type(value) is int and value >= 0 else fallback


def _optional_non_negative_int(value: JsonValue) -> int | None:
    """读取可选非负整数，非法值按未知处理。"""
    return value if type(value) is int and value >= 0 else None


def _metadata_int(value: JsonValue) -> int | None:
    """严格读取 metadata 必需非负整数。"""
    return value if type(value) is int and value >= 0 else None


def _metadata_optional_int(value: JsonValue) -> int | None:
    """严格读取 metadata 可选非负整数。"""
    if value is None:
        return None
    return value if type(value) is int and value >= 0 else None
