"""模型可见且只经 BrowserClient 执行的固定浏览器动作。"""

import re
from pathlib import Path
from urllib.parse import urlsplit

from lobster0.artifacts.store import ArtifactError, ArtifactStore
from lobster0.browser.client import BrowserClient
from lobster0.browser.models import BrowserAction, BrowserProtocolError
from lobster0.browser.policy import classify_browser_action
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_REF = re.compile(r"^@e[1-9][0-9]{0,5}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_PRESS_KEYS = frozenset(
    {
        "Enter",
        "Space",
        "Escape",
        "Tab",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "PageUp",
        "PageDown",
        "Home",
        "End",
        "Backspace",
        "Delete",
    }
)

_DEFINITIONS = {
    "browser_open": ToolDefinition(
        "browser_open",
        "Open a public HTTPS URL in Lobster0's isolated browser profile.",
        {
            "type": "object",
            "properties": {"url": {"type": "string", "maxLength": 8192}},
            "required": ["url"],
            "additionalProperties": False,
        },
        ToolRisk.LOW,
    ),
    "browser_snapshot": ToolDefinition(
        "browser_snapshot",
        "Read a bounded accessibility snapshot from the current browser tab.",
        {
            "type": "object",
            "properties": {"cursor": {"type": "integer", "minimum": 0}},
            "additionalProperties": False,
        },
        ToolRisk.LOW,
    ),
    "browser_click": ToolDefinition(
        "browser_click",
        "Click a stable ref from the latest browser snapshot after approval.",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "maxLength": 255},
                "generation": {"type": "string", "maxLength": 128},
                "ref": {"type": "string", "maxLength": 16},
                "role": {"type": "string", "maxLength": 64},
            },
            "required": ["origin", "generation", "ref", "role"],
            "additionalProperties": False,
        },
        ToolRisk.HIGH,
    ),
    "browser_type": ToolDefinition(
        "browser_type",
        "Replace a non-sensitive field using a ref from the latest snapshot.",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "maxLength": 255},
                "generation": {"type": "string", "maxLength": 128},
                "ref": {"type": "string", "maxLength": 16},
                "role": {"type": "string", "maxLength": 64},
                "input_kind": {"type": "string", "maxLength": 64},
                "text": {"type": "string", "maxLength": 20000},
            },
            "required": ["origin", "generation", "ref", "role", "input_kind", "text"],
            "additionalProperties": False,
        },
        ToolRisk.LOW,
    ),
    "browser_press": ToolDefinition(
        "browser_press",
        "Press one bounded key on a stable ref from the latest browser snapshot.",
        {
            "type": "object",
            "properties": {
                "origin": {"type": "string", "maxLength": 255},
                "generation": {"type": "string", "maxLength": 128},
                "ref": {"type": "string", "maxLength": 16},
                "role": {"type": "string", "maxLength": 64},
                "key": {"type": "string", "enum": sorted(_PRESS_KEYS)},
            },
            "required": ["origin", "generation", "ref", "role", "key"],
            "additionalProperties": False,
        },
        ToolRisk.HIGH,
    ),
    "browser_scroll": ToolDefinition(
        "browser_scroll",
        "Scroll the current browser tab by a bounded vertical distance.",
        {
            "type": "object",
            "properties": {
                "delta_y": {"type": "integer", "minimum": -10000, "maximum": 10000}
            },
            "required": ["delta_y"],
            "additionalProperties": False,
        },
        ToolRisk.LOW,
    ),
    "browser_screenshot": ToolDefinition(
        "browser_screenshot",
        "Capture the current browser tab as a bounded private artifact.",
        {
            "type": "object",
            "properties": {"full_page": {"type": "boolean"}},
            "additionalProperties": False,
        },
        ToolRisk.LOW,
    ),
    "browser_close": ToolDefinition(
        "browser_close",
        "Close the current Lobster0 browser session.",
        {"type": "object", "properties": {}, "additionalProperties": False},
        ToolRisk.LOW,
    ),
}


class BrowserTool:
    """把一个封闭 Tool Schema 映射到同名 Browser Worker 动作。"""

    def __init__(
        self,
        client: BrowserClient,
        name: str,
        *,
        max_snapshot_chars: int,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        """绑定共享 Client、固定动作名、snapshot 预算与可选 ArtifactStore。"""
        if name not in _DEFINITIONS:
            raise ValueError("browser tool name is invalid")
        if type(max_snapshot_chars) is not int or not 1000 <= max_snapshot_chars <= 100_000:
            raise ValueError("browser snapshot budget is invalid")
        self._client = client
        self.definition = _DEFINITIONS[name]
        self._max_snapshot_chars = max_snapshot_chars
        self._artifact_store = artifact_store

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """拒绝额外字段，并规范当前动作需要的有限参数。"""
        name = self.definition.name
        allowed = set(_DEFINITIONS[name].parameters.get("properties", {}))
        if set(arguments) - allowed:
            raise ToolValidationError(f"{name} received unknown arguments")
        if name == "browser_open":
            return {"url": _bounded_text(arguments.get("url"), "url", 8192)}
        if name == "browser_snapshot":
            cursor = arguments.get("cursor", 0)
            if type(cursor) is not int or cursor < 0:
                raise ToolValidationError("cursor must be a non-negative integer")
            return {"cursor": cursor}
        if name in {"browser_click", "browser_type", "browser_press"}:
            normalized = {
                "origin": _origin(arguments.get("origin")),
                "generation": _matching_text(
                    arguments.get("generation"), "generation", _IDENTIFIER
                ),
                "ref": _matching_text(arguments.get("ref"), "ref", _REF),
                "role": _matching_text(arguments.get("role"), "role", _ROLE),
            }
            if name == "browser_type":
                normalized["input_kind"] = _matching_text(
                    arguments.get("input_kind"), "input_kind", _ROLE
                ).casefold()
                normalized["text"] = _bounded_text(
                    arguments.get("text"), "text", 20_000, allow_empty=True
                )
            if name == "browser_press":
                key = arguments.get("key")
                if not isinstance(key, str) or key not in _PRESS_KEYS:
                    raise ToolValidationError("key is not allowed")
                normalized["key"] = key
            return normalized
        if name == "browser_scroll":
            delta = arguments.get("delta_y")
            if type(delta) is not int or delta == 0 or not -10_000 <= delta <= 10_000:
                raise ToolValidationError("delta_y must be a non-zero integer up to 10000")
            return {"delta_y": delta}
        if name == "browser_screenshot":
            full_page = arguments.get("full_page", False)
            if type(full_page) is not bool:
                raise ToolValidationError("full_page must be a boolean")
            return {"full_page": full_page}
        if arguments:
            raise ToolValidationError("browser_close accepts no arguments")
        return {}

    def effective_risk(self, arguments: dict[str, JsonValue]) -> ToolRisk:
        """返回参数绑定的 Browser 动态风险。"""
        return classify_browser_action(self.definition.name, arguments).risk

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """把已由 Core Policy 放行的动作发送给唯一 Browser Worker。"""
        params = dict(arguments)
        if self.definition.name == "browser_snapshot":
            params["max_chars"] = self._max_snapshot_chars
        try:
            result = await self._client.request(
                BrowserAction(
                    f"u{context.user_id}:s{context.session_id}",
                    self.definition.name.removeprefix("browser_"),
                    params,
                )
            )
        except BrowserProtocolError as error:
            return ToolResult.failure(
                error.code,
                "browser action failed",
                retryable=error.code in {"browser_timeout", "worker_closed"},
            )
        artifact = result.get("artifact")
        if artifact is not None:
            if self._artifact_store is None:
                return ToolResult.failure(
                    "artifact_store_unavailable", "browser artifact import failed"
                )
            if (
                not isinstance(artifact, dict)
                or set(artifact) - {
                    "staging_path",
                    "declared_media_type",
                    "source",
                    "width",
                    "height",
                }
                or not isinstance(artifact.get("staging_path"), str)
                or not isinstance(artifact.get("declared_media_type"), str)
                or not isinstance(artifact.get("source"), str)
            ):
                return ToolResult.failure(
                    "artifact_metadata_invalid", "browser artifact import failed"
                )
            try:
                imported = self._artifact_store.put(
                    Path(artifact["staging_path"]),
                    declared_media_type=artifact["declared_media_type"],
                    source=artifact["source"],
                )
            except ArtifactError as error:
                return ToolResult.failure(error.code, "browser artifact import failed")
            except OSError:
                return ToolResult.failure(
                    "artifact_store_failed", "browser artifact import failed", retryable=True
                )
            result = {**result, "artifact": imported.to_tool_payload()}
        return ToolResult.success(result)


def browser_tools(
    client: BrowserClient,
    *,
    max_snapshot_chars: int = 20_000,
    artifact_store: ArtifactStore | None = None,
) -> tuple[BrowserTool, ...]:
    """创建共享一个 Client 的固定八个 Browser Tool。

    Args:
        client: 当前 Runtime 独占的 Browser Worker 客户端。
        max_snapshot_chars: 单次 snapshot 返回的最大字符数。
        artifact_store: 消费截图/下载 staging 文件的私有 Store。

    Returns:
        按动作名稳定排序的八个 Tool。

    Raises:
        ValueError: snapshot 预算超出 Core 允许范围。
    """
    return tuple(
        BrowserTool(
            client,
            name,
            max_snapshot_chars=max_snapshot_chars,
            artifact_store=artifact_store,
        )
        for name in sorted(_DEFINITIONS)
    )


def _bounded_text(
    value: JsonValue | None,
    name: str,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> str:
    """校验不含 NUL 的有界字符串。"""
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ToolValidationError(f"{name} must be bounded text")
    return value


def _matching_text(value: JsonValue | None, name: str, pattern: re.Pattern[str]) -> str:
    """校验完全匹配有限语法的字符串。"""
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ToolValidationError(f"{name} is invalid")
    return value


def _origin(value: JsonValue | None) -> str:
    """规范只含 HTTPS scheme 与 authority 的展示 origin。"""
    text = _bounded_text(value, "origin", 255)
    try:
        parsed = urlsplit(text)
        port = parsed.port
    except ValueError:
        raise ToolValidationError("origin is invalid") from None
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ToolValidationError("origin must be an HTTPS origin")
    host = parsed.hostname.casefold()
    host_text = f"[{host}]" if ":" in host else host
    return f"https://{host_text}" + (f":{port}" if port not in {None, 443} else "")
