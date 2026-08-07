"""严格加载版本化 JSONL Agent 回归场景。"""

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from miniclaw.providers.base import ModelResponse, ToolCall

_CASE_FIELDS = {
    "schema_version",
    "id",
    "title",
    "status",
    "layers",
    "capability",
    "query",
    "turns",
    "setup",
    "offline",
    "expected",
    "introduced_by",
    "tags",
}
_EXPECTATION_FIELDS = {
    "answer_contains",
    "answer_excludes",
    "tool_runs",
    "tool_statuses",
    "audit_events",
    "request_contains",
    "max_tool_runs",
}
_RESPONSE_FIELDS = {
    "content",
    "tool_calls",
    "reasoning_content",
    "finish_reason",
    "input_tokens",
    "output_tokens",
    "provider_request_id",
}
_TOOL_CALL_FIELDS = {"call_id", "name", "arguments"}
_STATUSES = {"active", "planned", "retired"}
_LAYERS = {"offline", "live", "channel", "soak", "manual_sensitive"}
_TOOL_STATUSES = {"succeeded", "failed", "interrupted"}
_CREDENTIAL_FIELDS = {
    "api_key",
    "apikey",
    "token",
    "access_token",
    "refresh_token",
    "client_secret",
    "secret",
    "password",
    "authorization",
}
_CASE_ID = re.compile(r"^[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{3}$")


class EvalCaseError(ValueError):
    """表示场景文件不符合安全且版本化的输入契约。"""


@dataclass(frozen=True, slots=True)
class EvalExpectation:
    """保存离线 runner 可确定性检查的公开结果。"""

    answer_contains: tuple[str, ...]
    answer_excludes: tuple[str, ...]
    tool_runs: tuple[str, ...]
    tool_statuses: tuple[tuple[str, str], ...]
    audit_events: tuple[str, ...]
    request_contains: tuple[str, ...]
    max_tool_runs: int | None


@dataclass(frozen=True, slots=True)
class EvalCase:
    """保存一条已通过边界校验的 Agent 回归场景。"""

    schema_version: int
    id: str
    title: str
    status: str
    layers: tuple[str, ...]
    capability: str
    query: str
    turns: tuple[str, ...]
    setup_files: tuple[tuple[str, str], ...]
    responses: tuple[ModelResponse, ...]
    expected: EvalExpectation
    introduced_by: str
    tags: tuple[str, ...]
    source: str


def load_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载目录下所有 JSONL 场景并拒绝重复 ID 与不安全输入。

    Args:
        root: 只读场景目录。

    Returns:
        按文件名和行号稳定排序的场景。

    Raises:
        EvalCaseError: 目录、编码、JSON 或任一场景字段无效。
    """
    if not root.is_dir():
        raise EvalCaseError(f"eval root is not a directory: {root.name or '.'}")
    cases: list[EvalCase] = []
    seen: dict[str, str] = {}
    for path in sorted(root.glob("*.jsonl"), key=lambda item: item.name):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise EvalCaseError(f"cannot read eval file {path.name}") from error
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            source = f"{path.name}:{line_number}"
            try:
                raw = json.loads(line, parse_constant=_reject_json_constant)
            except (json.JSONDecodeError, ValueError) as error:
                raise EvalCaseError(f"invalid JSON at {source}") from error
            case = _parse_case(raw, source)
            previous = seen.get(case.id)
            if previous is not None:
                raise EvalCaseError(
                    f"duplicate case id {case.id} at {source}; first declared at {previous}"
                )
            seen[case.id] = source
            cases.append(case)
    return tuple(cases)


def _reject_json_constant(value: str) -> None:
    """拒绝 Python JSON 扩展支持的 NaN 与 Infinity。"""
    del value
    raise ValueError("non-standard JSON constant")


def _parse_case(raw: object, source: str) -> EvalCase:
    """把单行 JSON object 收窄为不可变场景。"""
    value = _object(raw, source, "case")
    _reject_credential_fields(value, source)
    _reject_unknown(value, _CASE_FIELDS, source)
    schema_version = _integer(value.get("schema_version"), source, "schema_version")
    if schema_version != 1:
        raise EvalCaseError(f"unsupported schema_version at {source}")
    case_id = _string(value.get("id"), source, "id")
    if _CASE_ID.fullmatch(case_id) is None:
        raise EvalCaseError(f"invalid case id at {source}")
    status = _string(value.get("status"), source, "status")
    if status not in _STATUSES:
        raise EvalCaseError(f"invalid status at {source}")
    layers = _strings(value.get("layers"), source, "layers")
    if not layers or any(layer not in _LAYERS for layer in layers):
        raise EvalCaseError(f"invalid layers at {source}")
    setup_files = _parse_setup(value.get("setup", {}), source)
    responses = _parse_offline(value.get("offline"), source)
    if status == "active" and "offline" in layers and not responses:
        raise EvalCaseError(f"active offline case has no responses at {source}")
    return EvalCase(
        schema_version=schema_version,
        id=case_id,
        title=_string(value.get("title"), source, "title"),
        status=status,
        layers=layers,
        capability=_string(value.get("capability"), source, "capability"),
        query=_string(value.get("query"), source, "query"),
        turns=_strings(value.get("turns", []), source, "turns"),
        setup_files=setup_files,
        responses=responses,
        expected=_parse_expectation(value.get("expected", {}), source),
        introduced_by=_string(value.get("introduced_by"), source, "introduced_by"),
        tags=_strings(value.get("tags", []), source, "tags"),
        source=source,
    )


def _parse_setup(raw: object, source: str) -> tuple[tuple[str, str], ...]:
    """解析只能写入临时 workspace 的合成文本文件。"""
    setup = _object(raw, source, "setup")
    _reject_unknown(setup, {"files"}, source)
    files = _object(setup.get("files", {}), source, "setup.files")
    parsed: list[tuple[str, str]] = []
    for path, content in sorted(files.items()):
        if not isinstance(path, str) or not _safe_relative_path(path):
            raise EvalCaseError(f"setup file path is unsafe at {source}")
        parsed.append((path, _string(content, source, f"setup.files.{path}", allow_empty=True)))
    return tuple(parsed)


def _parse_offline(raw: object, source: str) -> tuple[ModelResponse, ...]:
    """解析 Fake Provider 将按顺序返回的完整响应。"""
    if raw is None:
        return ()
    offline = _object(raw, source, "offline")
    _reject_unknown(offline, {"responses"}, source)
    responses = _list(offline.get("responses"), source, "offline.responses")
    return tuple(_parse_response(response, source) for response in responses)


def _parse_response(raw: object, source: str) -> ModelResponse:
    """把单个脚本响应映射到现有 Provider 契约。"""
    value = _object(raw, source, "response")
    _reject_unknown(value, _RESPONSE_FIELDS, source)
    tool_calls = tuple(
        _parse_tool_call(call, source)
        for call in _list(value.get("tool_calls", []), source, "tool_calls")
    )
    return ModelResponse(
        content=_string(value.get("content", ""), source, "content", allow_empty=True),
        tool_calls=tool_calls,
        reasoning_content=_optional_string(value.get("reasoning_content"), source),
        finish_reason=_string(value.get("finish_reason"), source, "finish_reason"),
        input_tokens=_optional_non_negative_integer(value.get("input_tokens"), source),
        output_tokens=_optional_non_negative_integer(value.get("output_tokens"), source),
        provider_request_id=_optional_string(value.get("provider_request_id"), source),
    )


def _parse_tool_call(raw: object, source: str) -> ToolCall:
    """解析场景中的结构化 Tool Call。"""
    value = _object(raw, source, "tool_call")
    _reject_unknown(value, _TOOL_CALL_FIELDS, source)
    arguments = _object(value.get("arguments"), source, "tool_call.arguments")
    if not _is_json_value(arguments):
        raise EvalCaseError(f"invalid tool arguments at {source}")
    return ToolCall(
        call_id=_string(value.get("call_id"), source, "call_id"),
        name=_string(value.get("name"), source, "name"),
        arguments=arguments,
    )


def _parse_expectation(raw: object, source: str) -> EvalExpectation:
    """解析 runner 支持的确定性结果断言。"""
    value = _object(raw, source, "expected")
    _reject_unknown(value, _EXPECTATION_FIELDS, source)
    statuses = _object(value.get("tool_statuses", {}), source, "tool_statuses")
    parsed_statuses: list[tuple[str, str]] = []
    for name, status in sorted(statuses.items()):
        tool_name = _string(name, source, "tool_statuses name")
        tool_status = _string(status, source, f"tool_statuses.{tool_name}")
        if tool_status not in _TOOL_STATUSES:
            raise EvalCaseError(f"invalid tool status at {source}")
        parsed_statuses.append((tool_name, tool_status))
    maximum = value.get("max_tool_runs")
    if maximum is not None:
        maximum = _integer(maximum, source, "max_tool_runs")
        if maximum < 0:
            raise EvalCaseError(f"max_tool_runs must be non-negative at {source}")
    return EvalExpectation(
        answer_contains=_strings(value.get("answer_contains", []), source, "answer_contains"),
        answer_excludes=_strings(value.get("answer_excludes", []), source, "answer_excludes"),
        tool_runs=_strings(value.get("tool_runs", []), source, "tool_runs"),
        tool_statuses=tuple(parsed_statuses),
        audit_events=_strings(value.get("audit_events", []), source, "audit_events"),
        request_contains=_strings(value.get("request_contains", []), source, "request_contains"),
        max_tool_runs=maximum,
    )


def _reject_unknown(value: dict[str, object], allowed: set[str], source: str) -> None:
    """拒绝首个未知字段，避免拼写错误被静默吞掉。"""
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise EvalCaseError(f"unknown field {unknown[0]} at {source}")


def _reject_credential_fields(raw: object, source: str) -> None:
    """递归拒绝明确的凭据字段名，但允许 synthetic secret 断言文本。"""
    if isinstance(raw, dict):
        for key, value in raw.items():
            if isinstance(key, str) and key.lower().replace("-", "_") in _CREDENTIAL_FIELDS:
                raise EvalCaseError(f"credential-like field at {source}")
            _reject_credential_fields(value, source)
    elif isinstance(raw, list):
        for value in raw:
            _reject_credential_fields(value, source)


def _safe_relative_path(value: str) -> bool:
    """判断 POSIX 场景路径是否停留在临时 workspace 内。"""
    if not value or "\\" in value:
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and ".." not in path.parts and value != "."


def _object(raw: object, source: str, field: str) -> dict[str, object]:
    """要求输入是字符串键 JSON object。"""
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise EvalCaseError(f"{field} must be an object at {source}")
    return raw


def _list(raw: object, source: str, field: str) -> list[object]:
    """要求输入是 JSON array。"""
    if not isinstance(raw, list):
        raise EvalCaseError(f"{field} must be an array at {source}")
    return raw


def _string(raw: object, source: str, field: str, *, allow_empty: bool = False) -> str:
    """要求输入是非空字符串，或在显式允许时接受空文本。"""
    if not isinstance(raw, str) or (not allow_empty and not raw.strip()):
        raise EvalCaseError(f"{field} must be a string at {source}")
    return raw


def _optional_string(raw: object, source: str) -> str | None:
    """解析可空字符串。"""
    if raw is None:
        return None
    return _string(raw, source, "optional string", allow_empty=True)


def _strings(raw: object, source: str, field: str) -> tuple[str, ...]:
    """解析非空字符串数组并保持输入顺序。"""
    return tuple(_string(item, source, field) for item in _list(raw, source, field))


def _integer(raw: object, source: str, field: str) -> int:
    """要求输入是非 bool 整数。"""
    if type(raw) is not int:
        raise EvalCaseError(f"{field} must be an integer at {source}")
    return raw


def _optional_non_negative_integer(raw: object, source: str) -> int | None:
    """解析可空的非负 Token 数。"""
    if raw is None:
        return None
    value = _integer(raw, source, "token count")
    if value < 0:
        raise EvalCaseError(f"token count must be non-negative at {source}")
    return value


def _is_json_value(raw: object) -> bool:
    """递归确认 Tool arguments 只包含稳定 JSON 值。"""
    if raw is None or isinstance(raw, str | bool) or type(raw) in {int, float}:
        return True
    if isinstance(raw, list):
        return all(_is_json_value(item) for item in raw)
    if isinstance(raw, dict):
        return all(isinstance(key, str) and _is_json_value(value) for key, value in raw.items())
    return False
