"""定义 Python Core 与外部 UI 共用的 NDJSON protocol v1。"""

import json
import re
from dataclasses import dataclass

from miniclaw.providers.base import JsonValue

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
        "session.new",
        "bridge.shutdown",
    }
)
_APPROVAL_DECISIONS = frozenset({"deny", "once", "session", "always"})
_PERMISSION_MODES = frozenset({"safe", "smart", "autopilot", "yolo"})


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
        if (
            set(payload) != {"session_key", "text"}
            or not _bounded_string(payload.get("session_key"), 1, 128)
            or not _bounded_string(payload.get("text"), 1, MAX_FRAME_BYTES)
        ):
            raise ProtocolError("invalid_turn", "Turn 请求字段不合法")
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
    if request_type == "permissions.set":
        mode = payload.get("mode")
        if set(payload) != {"mode"} or not isinstance(mode, str) or mode not in _PERMISSION_MODES:
            raise ProtocolError("invalid_permission_mode", "权限模式字段不合法")
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
