"""定义 Python Core 与外部 UI 共用的 NDJSON protocol v1。"""

import json
import re
from dataclasses import dataclass

from lobster0.providers.base import JsonValue

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 2 * 1024 * 1024
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_FRAME_TYPE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_REQUEST_TYPES = frozenset(
    {
        "client.hello",
        "turn.start",
        "turn.cancel",
        "approval.resolve",
        "permissions.set",
        "memory.command",
        "session.new",
        "session.list",
        "session.history",
        "automation.list",
        "automation.pause",
        "automation.resume",
        "automation.run",
        "automation.cancel",
        "automation.runs",
        "automation.halt",
        "automation.unhalt",
        "automation.create",
        "providers.list",
        "providers.upsert",
        "providers.remove",
        "providers.select",
        "providers.set_secret",
        "attachment.stage",
        "artifacts.list",
        "artifacts.preview",
        "artifacts.reveal",
        "subagents.list",
        "bridge.shutdown",
    }
)
_APPROVAL_DECISIONS = frozenset({"deny", "once", "session", "always"})
# 只接受 task_id 一个字段的 Automation 写操作。
_AUTOMATION_TASK_ACTIONS = frozenset(
    {"automation.pause", "automation.resume", "automation.run", "automation.cancel"}
)
# 桌面端可创建的调度类型。heartbeat 是系统内部心跳，只能由 Core 自己建，
# 但 automation.list 仍要能显示已存在的 heartbeat 任务。
_CREATABLE_SCHEDULE_KINDS = frozenset({"once", "interval", "cron"})
# interval/heartbeat 的 expression 是秒数。5 分钟下限防止误配置导致高频空转烧 token；
# 这里和界面各校验一次，只在前端做等于没做。
_MIN_INTERVAL_SECONDS = 300
# Provider id 参与密钥环境变量名推导，必须与 config 层同一套字符集。
_PROVIDER_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
# Artifact id 的形状由 Store 决定：art_ + sha256 十六进制。固定住它，
# 免得任意字符串被带进 Store 查询。
_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{64}\Z")
_MAX_ATTACHMENTS = 10
_MAX_ARTIFACT_LIST = 500
_MAX_PREVIEW_BYTES = 1_048_576
_PERMISSION_MODES = frozenset({"safe", "smart", "autopilot", "yolo"})
_MEMORY_ACTIONS = frozenset(
    {
        "status",
        "list",
        "search",
        "why",
        "flush",
        "rebuild",
        "review",
        "forget",
        "approve",
        "reject",
    }
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProtocolError(RuntimeError):
    """表示一个可安全返回给 Bridge 客户端的稳定协议错误。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码和不包含解析器细节的公开消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BridgeRequest:
    """表示经过协议和请求字段验证的一条客户端请求。"""

    version: int
    request_id: str
    type: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class BridgeFrame:
    """表示 Python Bridge 写给 UI 的一条响应或事件。"""

    type: str
    payload: dict[str, JsonValue]
    request_id: str | None = None


def decode_request(raw: bytes) -> BridgeRequest:
    """解码并验证一条客户端 NDJSON 请求。

    Args:
        raw: 包含单个 UTF-8 JSON object 的字节帧，可带末尾换行。

    Returns:
        字段和请求正文均已通过验证的 BridgeRequest。

    Raises:
        ProtocolError: 帧超限、编码、JSON、Envelope 或请求字段不合法。
    """
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", "协议帧超过 2 MiB 上限")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("invalid_encoding", "协议帧必须使用 UTF-8") from None
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise ProtocolError("invalid_json", "协议帧不是有效 JSON") from None
    if (
        not isinstance(value, dict)
        or not {"v", "type", "payload"}.issubset(value)
        or not set(value).issubset({"v", "id", "type", "payload"})
    ):
        raise ProtocolError("invalid_envelope", "协议 Envelope 字段不合法")

    version = value["v"]
    if not isinstance(version, int) or isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise ProtocolError("unsupported_version", "客户端不支持 protocol v1")
    request_id = value.get("id")
    if not isinstance(request_id, str) or _REQUEST_ID.fullmatch(request_id) is None:
        raise ProtocolError("invalid_request_id", "请求 ID 不合法")
    request_type = value["type"]
    if not isinstance(request_type, str) or request_type not in _REQUEST_TYPES:
        raise ProtocolError("unknown_request", "请求类型不受支持")
    payload = value["payload"]
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise ProtocolError("invalid_payload", "请求 payload 必须是 JSON object")
    _validate_payload(request_type, payload)
    return BridgeRequest(version, request_id, request_type, payload)


def encode_frame(frame: BridgeFrame) -> bytes:
    """把响应或事件编码为一条受大小约束的 UTF-8 NDJSON 帧。

    Args:
        frame: 包含安全 JSON payload 的响应或事件。

    Returns:
        以单个换行结尾的紧凑 UTF-8 JSON。

    Raises:
        ProtocolError: 类型、请求 ID、JSON 数值或帧大小不合法。
    """
    if _FRAME_TYPE.fullmatch(frame.type) is None:
        raise ProtocolError("invalid_frame", "服务端帧类型不合法")
    if frame.request_id is not None and _REQUEST_ID.fullmatch(frame.request_id) is None:
        raise ProtocolError("invalid_frame", "服务端请求 ID 不合法")
    envelope: dict[str, JsonValue] = {
        "v": PROTOCOL_VERSION,
        "type": frame.type,
        "payload": frame.payload,
    }
    if frame.request_id is not None:
        envelope["id"] = frame.request_id
    try:
        encoded = json.dumps(
            envelope,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
    except (TypeError, ValueError):
        raise ProtocolError("invalid_frame", "服务端帧不是标准 JSON") from None
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError("frame_too_large", "服务端帧超过 2 MiB 上限")
    return encoded


def _validate_payload(request_type: str, payload: dict[str, JsonValue]) -> None:
    """按请求类型验证 payload 的精确字段和标量边界。"""
    if request_type == "client.hello":
        protocols = payload.get("protocols")
        if (
            set(payload) != {"client_name", "client_version", "protocols"}
            or not _bounded_string(payload.get("client_name"), 1, 64)
            or not _bounded_string(payload.get("client_version"), 1, 64)
            or not isinstance(protocols, list)
            or protocols != [PROTOCOL_VERSION]
        ):
            raise ProtocolError("invalid_hello", "客户端握手字段不合法")
        return
    if request_type == "turn.start":
        # attachment_ids 可选：不带它的旧客户端必须继续可用。
        if (
            set(payload) - {"attachment_ids"} != {"session_key", "text"}
            or not _bounded_string(payload.get("session_key"), 1, 128)
            or not _bounded_string(payload.get("text"), 1, MAX_FRAME_BYTES)
        ):
            raise ProtocolError("invalid_turn", "Turn 请求字段不合法")
        if "attachment_ids" in payload and not _valid_artifact_ids(payload["attachment_ids"]):
            raise ProtocolError("invalid_turn", "Turn 请求字段不合法")
        return
    if request_type == "subagents.list":
        if payload:
            raise ProtocolError("invalid_subagent_query", "子 Agent 查询字段不合法")
        return
    if request_type == "artifacts.list":
        if (
            set(payload) != {"session_key", "limit"}
            or not _bounded_string(payload.get("session_key"), 1, 128)
            or not _integer_between(payload.get("limit"), 1, _MAX_ARTIFACT_LIST)
        ):
            raise ProtocolError("invalid_artifact_query", "产物查询字段不合法")
        return
    if request_type == "artifacts.preview":
        if (
            set(payload) != {"artifact_id", "max_bytes"}
            or not _valid_artifact_ids([payload.get("artifact_id")])
            or not _integer_between(payload.get("max_bytes"), 1, _MAX_PREVIEW_BYTES)
        ):
            raise ProtocolError("invalid_artifact_query", "产物查询字段不合法")
        return
    if request_type == "artifacts.reveal":
        # 只收 id：路径必须由 Core 从 id 解析，接受调用方给路径等于开放任意
        # 本地路径的「在访达中显示」。
        if set(payload) != {"artifact_id"} or not _valid_artifact_ids(
            [payload.get("artifact_id")]
        ):
            raise ProtocolError("invalid_artifact_query", "产物查询字段不合法")
        return
    if request_type == "attachment.stage":
        path = payload.get("path")
        if (
            set(payload) != {"path", "declared_media_type"}
            or not _bounded_string(path, 1, 4096)
            or not str(path).startswith("/")
            or not _bounded_string(payload.get("declared_media_type"), 1, 128)
        ):
            raise ProtocolError("invalid_attachment", "附件请求字段不合法")
        return
    if request_type == "approval.resolve":
        approval_id = payload.get("approval_id")
        decision = payload.get("decision")
        if (
            set(payload) != {"approval_id", "decision"}
            or not isinstance(approval_id, int)
            or isinstance(approval_id, bool)
            or approval_id <= 0
            or not isinstance(decision, str)
            or decision not in _APPROVAL_DECISIONS
        ):
            raise ProtocolError("invalid_approval", "审批决定字段不合法")
        return
    if request_type == "session.new":
        if set(payload) != {"session_key"} or not _bounded_string(
            payload.get("session_key"), 1, 128
        ):
            raise ProtocolError("invalid_session", "Session 字段不合法")
        return
    if request_type == "session.list":
        if set(payload) != {"limit"} or not _integer_between(payload.get("limit"), 1, 50):
            raise ProtocolError("invalid_session_query", "Session 查询字段不合法")
        return
    if request_type == "session.history":
        if (
            set(payload) != {"session_key", "limit"}
            or not _bounded_string(payload.get("session_key"), 1, 256)
            or not _integer_between(payload.get("limit"), 1, 200)
        ):
            raise ProtocolError("invalid_session_query", "Session 查询字段不合法")
        return
    if request_type == "automation.list":
        if set(payload) != {"limit"} or not _integer_between(payload.get("limit"), 1, 100):
            raise ProtocolError("invalid_automation_query", "Automation 查询字段不合法")
        return
    if request_type in _AUTOMATION_TASK_ACTIONS:
        if set(payload) != {"task_id"} or not _integer_between(
            payload.get("task_id"), 1, 2**31 - 1
        ):
            raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
        return
    if request_type == "automation.runs":
        if (
            set(payload) != {"task_id", "limit"}
            or not _integer_between(payload.get("task_id"), 1, 2**31 - 1)
            or not _integer_between(payload.get("limit"), 1, 100)
        ):
            raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
        return
    if request_type == "automation.halt":
        reason = payload.get("reason")
        if (
            set(payload) != {"reason"}
            or not _bounded_string(reason, 1, 500)
            or not str(reason).strip()
        ):
            raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
        return
    if request_type == "automation.unhalt":
        if payload:
            raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
        return
    if request_type == "automation.create":
        _validate_automation_create(payload)
        return
    if request_type == "providers.list":
        if payload:
            raise ProtocolError("invalid_provider_action", "Provider 操作字段不合法")
        return
    if request_type == "providers.upsert":
        # api_key_env 刻意不在字段集里：变量名由 Core 从 id 推导，
        # 接受调用方指定等于开放任意环境变量写入。
        if (
            set(payload) != {"id", "base_url", "timeout_seconds"}
            or not _valid_provider_id(payload.get("id"))
            or not _bounded_string(payload.get("base_url"), 1, 500)
            or not _integer_between(payload.get("timeout_seconds"), 1, 3600)
        ):
            raise ProtocolError("invalid_provider_action", "Provider 操作字段不合法")
        return
    if request_type == "providers.remove":
        if set(payload) != {"id"} or not _valid_provider_id(payload.get("id")):
            raise ProtocolError("invalid_provider_action", "Provider 操作字段不合法")
        return
    if request_type == "providers.select":
        model = payload.get("model")
        if (
            set(payload) != {"id", "model"}
            or not _valid_provider_id(payload.get("id"))
            or not _bounded_string(model, 1, 200)
            or not str(model).strip()
        ):
            raise ProtocolError("invalid_provider_action", "Provider 操作字段不合法")
        return
    if request_type == "providers.set_secret":
        if (
            set(payload) != {"id", "value"}
            or not _valid_provider_id(payload.get("id"))
            or not _bounded_string(payload.get("value"), 1, 4096)
        ):
            raise ProtocolError("invalid_provider_action", "Provider 操作字段不合法")
        return
    if request_type == "permissions.set":
        mode = payload.get("mode")
        if set(payload) != {"mode"} or not isinstance(mode, str) or mode not in _PERMISSION_MODES:
            raise ProtocolError("invalid_permission_mode", "权限模式字段不合法")
        return
    if request_type == "memory.command":
        _validate_memory_command(payload)
        return
    if payload:
        raise ProtocolError("invalid_payload", "该请求不接受 payload 字段")


def _bounded_string(value: JsonValue, minimum: int, maximum: int) -> bool:
    """判断 JSON 值是否为指定字符长度内且不含 NUL 的字符串。"""
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and "\x00" not in value
    )


def _integer_between(value: JsonValue, minimum: int, maximum: int) -> bool:
    """判断 JSON 值是否为指定闭区间内的非 bool 整数。"""
    return type(value) is int and minimum <= value <= maximum


def _valid_artifact_ids(value: JsonValue) -> bool:
    """判断附件 id 列表是否是非空、有界、形状正确的 Artifact id。"""
    return (
        isinstance(value, list)
        and 1 <= len(value) <= _MAX_ATTACHMENTS
        and all(isinstance(item, str) and _ARTIFACT_ID.fullmatch(item) for item in value)
    )


def _valid_provider_id(value: JsonValue) -> bool:
    """判断 Provider id 是否符合与 config 层一致的安全字符集。"""
    return isinstance(value, str) and bool(_PROVIDER_ID.fullmatch(value))


def _validate_automation_create(payload: dict[str, JsonValue]) -> None:
    """校验桌面端创建定时任务的收窄字段集。

    只接受"什么时候、跑什么"：``name``、``prompt`` 与 ``schedule``。Core 支持的
    ``skills``/``delivery``/``budget`` 一律拒绝而不是忽略——静默忽略会让调用方以为
    生效了。``expression`` 的最终合法性仍由 Core 的 ``parse_schedule`` 裁决，这里只挡住
    明显危险与明显非法的输入。

    Args:
        payload: 已解码但未校验的请求负载。

    Raises:
        ProtocolError: 字段集、长度、调度类型或 interval 下限不满足。
    """
    if set(payload) != {"name", "prompt", "schedule"}:
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
    if not _bounded_string(payload.get("name"), 1, 64) or not str(payload["name"]).strip():
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
    if not _bounded_string(payload.get("prompt"), 1, 4000) or not str(payload["prompt"]).strip():
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")

    schedule = payload.get("schedule")
    if not isinstance(schedule, dict) or set(schedule) - {"kind", "expression", "timezone"}:
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
    kind = schedule.get("kind")
    if not isinstance(kind, str) or kind not in _CREATABLE_SCHEDULE_KINDS:
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
    expression = schedule.get("expression")
    if not _bounded_string(expression, 1, 200) or not str(expression).strip():
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
    if "timezone" in schedule and not _bounded_string(schedule.get("timezone"), 1, 64):
        raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")
    if kind == "interval":
        try:
            seconds = int(str(expression).strip())
        except ValueError as error:
            raise ProtocolError(
                "invalid_automation_action", "Automation 操作字段不合法"
            ) from error
        if seconds < _MIN_INTERVAL_SECONDS:
            raise ProtocolError("invalid_automation_action", "Automation 操作字段不合法")


def _validate_memory_command(payload: dict[str, JsonValue]) -> None:
    """按 action 严格限制 Memory Console 参数，身份始终由 Core 绑定。"""
    action = payload.get("action")
    if not isinstance(action, str) or action not in _MEMORY_ACTIONS:
        raise ProtocolError("invalid_memory_command", "Memory 命令字段不合法")
    if action in {"status", "flush", "rebuild"}:
        valid = set(payload) == {"action"}
    elif action in {"list", "review"}:
        valid = set(payload).issubset({"action", "limit"})
    elif action == "search":
        valid = (
            set(payload).issubset({"action", "query", "limit"})
            and _bounded_string(payload.get("query"), 1, 1_000)
        )
    elif action in {"why", "forget"}:
        valid = (
            set(payload) == {"action", "unit_id"}
            and _bounded_string(payload.get("unit_id"), 1, 160)
        )
    else:
        review_id = payload.get("review_id")
        preview_hash = payload.get("preview_hash")
        valid = (
            set(payload) == {"action", "review_id", "preview_hash"}
            and type(review_id) is int
            and review_id > 0
            and isinstance(preview_hash, str)
            and _SHA256.fullmatch(preview_hash) is not None
        )
    limit = payload.get("limit", 10)
    if not valid or type(limit) is not int or not 1 <= limit <= 50:
        raise ProtocolError("invalid_memory_command", "Memory 命令字段不合法")
