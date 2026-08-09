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
    "approval_actions",
    "setup",
    "offline",
    "expected",
    "introduced_by",
    "tags",
    "channel",
    "memory",
    "automation",
    "browser",
}
_EXPECTATION_FIELDS = {
    "answer_contains",
    "answer_excludes",
    "tool_runs",
    "tool_statuses",
    "audit_events",
    "request_contains",
    "max_tool_runs",
    "approval_statuses",
    "files",
    "absent_files",
    "error_code",
    "channel_evidence",
    "personal_files",
    "absent_personal_files",
    "live_local_evidence",
    "live_human_evidence",
    "memory_evidence",
    "automation_status",
    "delivery_count",
    "automation_evidence",
    "forbidden_automation",
    "browser_evidence",
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
_LAYERS = {
    "offline",
    "live",
    "channel",
    "automation",
    "browser",
    "soak",
    "manual_sensitive",
}
_TOOL_STATUSES = {
    "waiting_approval",
    "succeeded",
    "failed",
    "denied",
    "interrupted",
}
_APPROVAL_ACTIONS = {"approve", "deny", "tamper", "replay"}
_APPROVAL_STATUSES = {"pending", "approved", "denied", "expired", "consumed"}
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
_CHANNEL_FIXTURES = {
    "dm",
    "group_mention",
    "group_no_mention",
    "dedupe",
    "read_tool",
    "approval_approve",
    "approval_deny",
    "restart_queued",
    "restart_running",
    "delivery_retry",
    "card_fallback",
    "reconnect",
    "group_reply",
    "guild_mention",
    "guild_no_mention",
    "thread",
    "isolation",
    "compact_reply",
}
_MEMORY_FIXTURES = frozenset(
    {
        "cross_channel_disclosure",
        "explicit_restart_forget",
        "secret_and_source_rejection",
        "short_term_repeat_promotion",
        "sensitive_and_behavior_review",
        "conflict_correction_supersede",
        "provider_failure_retry",
        "checkpoint_and_lease_recovery",
        "direct_edit_and_legacy_migration",
        "chinese_recall_integrity",
    }
)
_AUTOMATION_FIXTURES = frozenset(
    {
        "scheduler_idempotency",
        "bounded_misfire",
        "durable_estop",
        "secret_prompt_guard",
        "recursive_prompt_guard",
        "immutable_run_snapshot",
        "terminal_and_recovery",
        "waiting_approval",
        "approval_continuation",
        "delivery_idempotency",
        "execution_plan_binding",
        "docker_hardening",
        "checkpoint_quota",
        "rollback_conflict",
        "heartbeat_reconcile",
        "live_one_shot_delivery",
        "live_interval_two_slots",
        "live_gateway_restart",
        "live_interrupted_recovery",
        "live_waiting_approval",
        "live_approval_continuation",
        "live_structured_silence",
        "live_durable_estop",
        "live_budget_stop",
        "live_delivery_unknown_recovery",
    }
)
_BROWSER_FIXTURES = frozenset(
    {
        "navigate",
        "snapshot",
        "click",
        "type",
        "press",
        "scroll",
        "screenshot",
        "download",
        "stale_ref",
        "redirect_ssrf",
        "localhost_denial",
        "injection_page",
        "password_denial",
        "submit_approval",
        "cancel_cleanup",
        "worker_crash",
        "profile_lock",
        "artifact_ttl",
    }
)
_BROWSER_EVIDENCE = frozenset(
    {
        "public_https_only",
        "normalized_origin",
        "bounded_snapshot",
        "opaque_refs",
        "click_high_risk",
        "approval_required",
        "safe_input_allowed",
        "typed_text_redacted",
        "enter_high_risk",
        "scroll_bounded",
        "artifact_id_only",
        "png_dimensions",
        "download_content_hashed",
        "traversal_denied",
        "stable_error_code",
        "client_closed",
        "redirect_revalidated",
        "private_redirect_denied",
        "localhost_denied",
        "untrusted_provenance",
        "prompt_not_authority",
        "password_hard_denied",
        "refs_not_displayed",
        "worker_terminated",
        "no_orphan",
        "crash_redacted",
        "dedicated_profile",
        "owner_only",
        "outside_workspace",
        "expired_deleted",
    }
)
_AUTOMATION_STATUSES = frozenset(
    {"allowed", "denied", "failed", "halted", "queued", "succeeded", "waiting_approval"}
)
_AUTOMATION_EVIDENCE = frozenset(
    {
        "one_slot_only",
        "bounded_misfire",
        "zero_claim",
        "secret_not_persisted",
        "recursive_control_denied",
        "snapshot_immutable",
        "terminal_required",
        "stale_run_interrupted",
        "lease_released",
        "approval_id_bound",
        "continuation_terminal",
        "original_budget_preserved",
        "delivery_once",
        "destination_immutable",
        "plan_hash_bound",
        "exact_argv",
        "network_none",
        "read_only_rootfs",
        "quota_fail_closed",
        "no_side_effect",
        "preview_hash_bound",
        "concurrent_edit_preserved",
        "one_system_task",
        "active_hours_bounded",
        "two_slots_once",
        "task_identity_preserved",
        "gateway_restart_recovered",
        "structured_silence",
        "budget_stopped",
        "idempotency_key_reused",
        "provider_request_observed",
    }
)
_MEMORY_EVIDENCE = frozenset(
    {
        "owner_space_shared",
        "group_denied",
        "non_owner_denied",
        "explicit_persisted",
        "restart_recalled",
        "forget_archived",
        "rebuild_absent",
        "secret_rejected",
        "fabricated_source_rejected",
        "zero_rejected_persistence",
        "first_observation_short_term",
        "independent_repeat_active",
        "duplicate_unit_zero",
        "sensitive_review_required",
        "behavior_review_required",
        "model_cannot_approve",
        "conflict_review_required",
        "old_unit_superseded",
        "correction_source_preserved",
        "provider_failure_sanitized",
        "source_range_retryable",
        "markdown_not_duplicated",
        "projection_resumed",
        "stale_lease_reclaimed",
        "buffer_completed_once",
        "manual_edit_reconciled",
        "invalid_edit_fail_closed",
        "legacy_hash_idempotent",
        "legacy_source_untouched",
        "recall_top5_bounded",
        "chinese_unit_recalled",
        "all_units_sourced",
        "unit_ids_unique",
    }
)
_LIVE_LOCAL_EVIDENCE = frozenset(
    {
        "gateway_ready",
        "inbox_completed",
        "turn_completed",
        "delivery_sent",
        "one_session_three_turns",
        "system_info_succeeded",
        "read_file_succeeded",
        "approval_pending",
        "approval_consumed_once",
        "approval_denied",
        "no_new_turn",
        "multiple_parts_sent",
        "memory_survived_restart",
        "transport_reconnected",
        "secret_scan_zero",
    }
)
_LIVE_HUMAN_EVIDENCE = frozenset(
    {
        "reply_visible",
        "context_answer_correct",
        "system_info_visible",
        "sentinel_visible",
        "approval_prompt_visible",
        "approved_result_visible",
        "denial_visible",
        "bot_silent",
        "group_reply_visible",
        "long_content_intact",
        "restart_answer_correct",
        "reconnect_reply_visible",
    }
)


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
    approval_statuses: tuple[str, ...]
    files: tuple[tuple[str, str], ...]
    absent_files: tuple[str, ...]
    personal_files: tuple[tuple[str, str], ...]
    absent_personal_files: tuple[str, ...]
    error_code: str | None
    channel_evidence: tuple[str, ...]
    live_local_evidence: tuple[str, ...]
    live_human_evidence: tuple[str, ...]
    memory_evidence: tuple[str, ...]
    automation_status: str | None
    delivery_count: int | None
    automation_evidence: tuple[str, ...]
    forbidden_automation: tuple[str, ...]
    browser_evidence: tuple[str, ...]


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
    approval_actions: tuple[str, ...]
    setup_files: tuple[tuple[str, str], ...]
    setup_personal_files: tuple[tuple[str, str], ...]
    setup_executables: tuple[tuple[str, str], ...]
    responses: tuple[ModelResponse, ...]
    expected: EvalExpectation
    introduced_by: str
    tags: tuple[str, ...]
    source: str
    channel_fixture: str | None
    memory_fixture: str | None
    automation_fixture: str | None
    browser_fixture: str | None


def load_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载目录下所有 JSONL 场景并拒绝重复 ID 与不安全输入。

    Args:
        root: 只读场景目录。

    Returns:
        按 case ID 稳定排序的场景。

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
    return tuple(sorted(cases, key=lambda case: case.id))


def load_feishu_live_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载固定十五条 active Feishu Live E2E 场景。

    Args:
        root: 版本化 JSONL 场景目录。

    Returns:
        按 ID 排序的十五条真实飞书场景。

    Raises:
        EvalCaseError: 数据集数量或任一通用场景字段无效。
    """
    cases = tuple(
        case
        for case in load_cases(root)
        if case.status == "active" and case.capability == "feishu_e2e"
    )
    if len(cases) != 15:
        raise EvalCaseError("feishu live suite must contain exactly 15 active cases")
    return cases


def load_automation_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载固定十五条 active Automation v1 场景。

    Args:
        root: 版本化 JSONL 场景目录。

    Returns:
        按 ID 排序的十五条 Automation 场景。

    Raises:
        EvalCaseError: 数据集数量、ID 或 schema 不符合发布契约。
    """
    cases = tuple(
        case
        for case in load_cases(root)
        if case.status == "active" and case.capability == "automation_runtime"
    )
    expected_ids = tuple(f"AUTO-{index:03d}" for index in range(1, 16))
    if tuple(case.id for case in cases) != expected_ids:
        raise EvalCaseError("automation suite must contain exactly AUTO-001..AUTO-015")
    return cases


def load_feishu_automation_live_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载固定十条 active Feishu Automation Live 场景。

    Args:
        root: 版本化 JSONL 场景目录。

    Returns:
        按 ID 排序的十条真实飞书自动化场景。

    Raises:
        EvalCaseError: 数据集数量、ID 或通用场景字段不符合发布契约。
    """
    cases = tuple(
        case
        for case in load_cases(root)
        if case.status == "active" and case.capability == "feishu_automation_e2e"
    )
    expected_ids = tuple(f"FEISHU-AUTO-{index:03d}" for index in range(1, 11))
    if tuple(case.id for case in cases) != expected_ids:
        raise EvalCaseError(
            "feishu automation live suite must contain exactly FEISHU-AUTO-001..010"
        )
    return cases


def load_browser_cases(root: Path) -> tuple[EvalCase, ...]:
    """加载固定十八条 active Browser v1 场景。"""
    cases = tuple(
        case
        for case in load_cases(root)
        if case.status == "active" and case.capability == "browser_agent"
    )
    expected_ids = tuple(f"BROWSER-{index:03d}" for index in range(1, 19))
    if tuple(case.id for case in cases) != expected_ids:
        raise EvalCaseError("browser suite must contain exactly BROWSER-001..BROWSER-018")
    return cases


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
    setup_files, setup_personal_files, setup_executables = _parse_setup(
        value.get("setup", {}),
        source,
    )
    approval_actions = _strings(
        value.get("approval_actions", []),
        source,
        "approval_actions",
    )
    if any(action not in _APPROVAL_ACTIONS for action in approval_actions):
        raise EvalCaseError(f"invalid approval action at {source}")
    responses = _parse_offline(value.get("offline"), source)
    memory_fixture = _parse_memory(value.get("memory"), source)
    automation_fixture = _parse_automation(value.get("automation"), source)
    browser_fixture = _parse_browser(value.get("browser"), source)
    if status == "active" and "offline" in layers and not responses and memory_fixture is None:
        raise EvalCaseError(f"active offline case has no responses at {source}")
    channel_fixture = _parse_channel(value.get("channel"), source)
    if status == "active" and "channel" in layers and channel_fixture is None:
        raise EvalCaseError(f"active channel case has no fixture at {source}")
    capability = _string(value.get("capability"), source, "capability")
    expected = _parse_expectation(value.get("expected", {}), source)
    if memory_fixture is not None:
        if (
            status != "active"
            or layers != ("offline",)
            or capability != "memory_autopilot"
        ):
            raise EvalCaseError(
                f"memory fixture must be active offline memory_autopilot at {source}"
            )
        if responses or channel_fixture is not None or not expected.memory_evidence:
            raise EvalCaseError(f"memory fixture has invalid execution fields at {source}")
    if automation_fixture is not None:
        valid_automation_case = (
            status == "active"
            and (
                (layers == ("automation",) and capability == "automation_runtime")
                or (layers == ("live",) and capability == "feishu_automation_e2e")
            )
        )
        if not valid_automation_case:
            raise EvalCaseError(
                f"automation fixture must be active automation runtime/live at {source}"
            )
        if responses or channel_fixture is not None or memory_fixture is not None:
            raise EvalCaseError(f"automation fixture has invalid execution fields at {source}")
        if expected.automation_status is None or not expected.automation_evidence:
            raise EvalCaseError(f"automation fixture has no expected evidence at {source}")
    if browser_fixture is not None:
        if status != "active" or layers != ("browser",) or capability != "browser_agent":
            raise EvalCaseError(
                f"browser fixture must be active browser browser_agent at {source}"
            )
        if any(
            value is not None
            for value in (channel_fixture, memory_fixture, automation_fixture)
        ) or responses:
            raise EvalCaseError(f"browser fixture has invalid execution fields at {source}")
        if not expected.browser_evidence:
            raise EvalCaseError(f"browser fixture has no expected evidence at {source}")
    if status == "active" and layers == ("live",) and capability not in {
        "feishu_e2e",
        "feishu_automation_e2e",
    }:
        raise EvalCaseError(f"active live case must use feishu_e2e capability at {source}")
    if capability == "feishu_e2e":
        if status == "active" and layers != ("live",):
            raise EvalCaseError(f"active Feishu Live case must use only live layer at {source}")
        if responses or channel_fixture is not None:
            raise EvalCaseError(
                f"Feishu Live case cannot use offline or channel fixture at {source}"
            )
        if status == "active" and not (
            expected.live_local_evidence or expected.live_human_evidence
        ):
            raise EvalCaseError(f"active Feishu Live case has no evidence at {source}")
    if capability == "feishu_automation_e2e":
        if status == "active" and layers != ("live",):
            raise EvalCaseError(
                f"active Feishu Automation Live case must use only live layer at {source}"
            )
        if automation_fixture is None or responses or channel_fixture is not None:
            raise EvalCaseError(
                f"Feishu Automation Live case has invalid execution fields at {source}"
            )
    return EvalCase(
        schema_version=schema_version,
        id=case_id,
        title=_string(value.get("title"), source, "title"),
        status=status,
        layers=layers,
        capability=capability,
        query=_string(value.get("query"), source, "query"),
        turns=_strings(value.get("turns", []), source, "turns"),
        approval_actions=approval_actions,
        setup_files=setup_files,
        setup_personal_files=setup_personal_files,
        setup_executables=setup_executables,
        responses=responses,
        expected=expected,
        introduced_by=_string(value.get("introduced_by"), source, "introduced_by"),
        tags=_strings(value.get("tags", []), source, "tags"),
        source=source,
        channel_fixture=channel_fixture,
        memory_fixture=memory_fixture,
        automation_fixture=automation_fixture,
        browser_fixture=browser_fixture,
    )


def _parse_channel(raw: object, source: str) -> str | None:
    """解析确定性 Channel fixture；不提供任意函数或凭据字段。"""
    if raw is None:
        return None
    value = _object(raw, source, "channel")
    _reject_unknown(value, {"fixture"}, source)
    fixture = _string(value.get("fixture"), source, "channel.fixture")
    if fixture not in _CHANNEL_FIXTURES:
        raise EvalCaseError(f"invalid channel fixture at {source}")
    return fixture


def _parse_memory(raw: object, source: str) -> str | None:
    """解析封闭 Memory fixture，不接受任意脚本或敏感参数。"""
    if raw is None:
        return None
    value = _object(raw, source, "memory")
    _reject_unknown(value, {"fixture"}, source)
    fixture = _string(value.get("fixture"), source, "memory.fixture")
    if fixture not in _MEMORY_FIXTURES:
        raise EvalCaseError(f"invalid memory fixture at {source}")
    return fixture


def _parse_automation(raw: object, source: str) -> str | None:
    """解析封闭 Automation v1 fixture，不接受脚本、环境或凭据。"""
    if raw is None:
        return None
    value = _object(raw, source, "automation")
    _reject_unknown(value, {"schema", "fixture"}, source)
    if _string(value.get("schema"), source, "automation.schema") != "automation.v1":
        raise EvalCaseError(f"unsupported automation schema at {source}")
    fixture = _string(value.get("fixture"), source, "automation.fixture")
    if fixture not in _AUTOMATION_FIXTURES:
        raise EvalCaseError(f"invalid automation fixture at {source}")
    return fixture


def _parse_browser(raw: object, source: str) -> str | None:
    """解析封闭 Browser v1 fixture，不接受脚本、URL、路径或凭据。"""
    if raw is None:
        return None
    value = _object(raw, source, "browser")
    _reject_unknown(value, {"schema", "fixture"}, source)
    if _string(value.get("schema"), source, "browser.schema") != "browser.v1":
        raise EvalCaseError(f"unsupported browser schema at {source}")
    fixture = _string(value.get("fixture"), source, "browser.fixture")
    if fixture not in _BROWSER_FIXTURES:
        raise EvalCaseError(f"invalid browser fixture at {source}")
    return fixture


def _parse_setup(
    raw: object,
    source: str,
) -> tuple[
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
    tuple[tuple[str, str], ...],
]:
    """解析只能写入临时 Workspace、Personal Home 的合成 fixture。"""
    setup = _object(raw, source, "setup")
    _reject_unknown(setup, {"files", "personal_files", "executables"}, source)
    return (
        _parse_relative_text_map(setup.get("files", {}), source, "setup.files"),
        _parse_relative_text_map(
            setup.get("personal_files", {}),
            source,
            "setup.personal_files",
        ),
        _parse_relative_text_map(
            setup.get("executables", {}),
            source,
            "setup.executables",
        ),
    )


def _parse_relative_text_map(
    raw: object,
    source: str,
    field: str,
) -> tuple[tuple[str, str], ...]:
    """解析相对临时 Root 的 UTF-8 文本映射。"""
    files = _object(raw, source, field)
    parsed: list[tuple[str, str]] = []
    for path, content in sorted(files.items()):
        if not isinstance(path, str) or not _safe_relative_path(path):
            raise EvalCaseError(f"setup file path is unsafe at {source}")
        parsed.append((path, _string(content, source, f"{field}.{path}", allow_empty=True)))
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
    approval_statuses = _strings(
        value.get("approval_statuses", []),
        source,
        "approval_statuses",
    )
    if any(status not in _APPROVAL_STATUSES for status in approval_statuses):
        raise EvalCaseError(f"invalid approval status at {source}")
    files = _parse_expected_files(value.get("files", {}), source)
    absent_files = _strings(value.get("absent_files", []), source, "absent_files")
    if any(not _safe_relative_path(path) for path in absent_files):
        raise EvalCaseError(f"expected file path is unsafe at {source}")
    personal_files = _parse_expected_files(
        value.get("personal_files", {}),
        source,
        field="expected.personal_files",
    )
    absent_personal_files = _strings(
        value.get("absent_personal_files", []),
        source,
        "absent_personal_files",
    )
    if any(not _safe_relative_path(path) for path in absent_personal_files):
        raise EvalCaseError(f"expected personal file path is unsafe at {source}")
    live_local_evidence = _parse_live_evidence(
        value.get("live_local_evidence", []),
        source,
        "live local",
        _LIVE_LOCAL_EVIDENCE,
    )
    live_human_evidence = _parse_live_evidence(
        value.get("live_human_evidence", []),
        source,
        "live human",
        _LIVE_HUMAN_EVIDENCE,
    )
    memory_evidence = _parse_live_evidence(
        value.get("memory_evidence", []),
        source,
        "memory",
        _MEMORY_EVIDENCE,
    )
    automation_status = _optional_string(value.get("automation_status"), source)
    if automation_status is not None and automation_status not in _AUTOMATION_STATUSES:
        raise EvalCaseError(f"invalid automation status at {source}")
    delivery_count = value.get("delivery_count")
    if delivery_count is not None:
        delivery_count = _integer(delivery_count, source, "delivery_count")
        if delivery_count < 0:
            raise EvalCaseError(f"delivery_count must be non-negative at {source}")
    automation_evidence = _parse_live_evidence(
        value.get("automation_evidence", []),
        source,
        "automation",
        _AUTOMATION_EVIDENCE,
    )
    forbidden_automation = _strings(
        value.get("forbidden_automation", []),
        source,
        "forbidden_automation",
    )
    browser_evidence = _parse_live_evidence(
        value.get("browser_evidence", []),
        source,
        "browser",
        _BROWSER_EVIDENCE,
    )
    return EvalExpectation(
        answer_contains=_strings(value.get("answer_contains", []), source, "answer_contains"),
        answer_excludes=_strings(value.get("answer_excludes", []), source, "answer_excludes"),
        tool_runs=_strings(value.get("tool_runs", []), source, "tool_runs"),
        tool_statuses=tuple(parsed_statuses),
        audit_events=_strings(value.get("audit_events", []), source, "audit_events"),
        request_contains=_strings(value.get("request_contains", []), source, "request_contains"),
        max_tool_runs=maximum,
        approval_statuses=approval_statuses,
        files=files,
        absent_files=absent_files,
        personal_files=personal_files,
        absent_personal_files=absent_personal_files,
        error_code=_optional_string(value.get("error_code"), source),
        channel_evidence=_strings(
            value.get("channel_evidence", []),
            source,
            "channel_evidence",
        ),
        live_local_evidence=live_local_evidence,
        live_human_evidence=live_human_evidence,
        memory_evidence=memory_evidence,
        automation_status=automation_status,
        delivery_count=delivery_count,
        automation_evidence=automation_evidence,
        forbidden_automation=forbidden_automation,
        browser_evidence=browser_evidence,
    )


def _parse_live_evidence(
    raw: object,
    source: str,
    kind: str,
    allowed: frozenset[str],
) -> tuple[str, ...]:
    """解析封闭且不重复的 Live evidence key。"""
    values = _strings(raw, source, f"{kind} evidence")
    if len(set(values)) != len(values):
        raise EvalCaseError(f"duplicate {kind} evidence at {source}")
    if any(value not in allowed for value in values):
        raise EvalCaseError(f"invalid {kind} evidence at {source}")
    return values


def _parse_expected_files(
    raw: object,
    source: str,
    *,
    field: str = "expected.files",
) -> tuple[tuple[str, str], ...]:
    """解析临时 Root 内的精确文件结果断言。"""
    files = _object(raw, source, field)
    parsed: list[tuple[str, str]] = []
    for path, content in sorted(files.items()):
        if not isinstance(path, str) or not _safe_relative_path(path):
            raise EvalCaseError(f"expected file path is unsafe at {source}")
        parsed.append((path, _string(content, source, f"{field}.{path}", allow_empty=True)))
    return tuple(parsed)


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
