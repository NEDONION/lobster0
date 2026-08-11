"""让模型读取当前会话产物正文的受控 Tool。"""

import re

from lobster0.artifacts.store import ArtifactError, ArtifactStore
from lobster0.providers.base import JsonValue
from lobster0.tools.base import (
    ToolContext,
    ToolDefinition,
    ToolResult,
    ToolRisk,
    ToolValidationError,
)

_ARTIFACT_ID = re.compile(r"art_[0-9a-f]{64}\Z")
# 只有这几类返回正文。图片/PDF/ZIP 的字节塞进上下文既无意义又昂贵，
# D3 也不做 Vision。
_TEXT_MEDIA_TYPES = frozenset({"text/plain", "text/csv", "application/json"})
_DEFAULT_MAX_BYTES = 32_768
_MAX_MAX_BYTES = 131_072


class ReadArtifactTool:
    """按 Artifact id 读取当前会话内产物的有界正文。"""

    definition = ToolDefinition(
        name="read_artifact",
        description=(
            "Read the bounded UTF-8 text of an artifact attached to the current "
            "conversation. Use the artifact_id shown in the [附件] manifest. "
            "Images, PDFs and archives return metadata only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "max_bytes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_MAX_BYTES,
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
        risk=ToolRisk.LOW,
    )

    def __init__(self, store: ArtifactStore) -> None:
        """绑定当前 Owner 的 ArtifactStore。

        「当前会话」不在这里绑定：它来自每次调用的 :class:`ToolContext`，那是
        运行期给定、模型参数伪造不了的边界。
        """
        self._store = store

    def validate(self, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """只接受形状正确的 Artifact id 与有界的 max_bytes。"""
        unexpected = set(arguments) - {"artifact_id", "max_bytes"}
        if unexpected:
            raise ToolValidationError(
                "read_artifact only accepts 'artifact_id' and 'max_bytes'"
            )
        artifact_id = arguments.get("artifact_id")
        # 形状先卡死，任意字符串不该进入 Store 查询。
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise ToolValidationError("artifact_id is not a valid artifact identifier")
        max_bytes = arguments.get("max_bytes", _DEFAULT_MAX_BYTES)
        if (
            type(max_bytes) is not int
            or isinstance(max_bytes, bool)
            or not 1 <= max_bytes <= _MAX_MAX_BYTES
        ):
            raise ToolValidationError("max_bytes is out of range")
        return {"artifact_id": artifact_id, "max_bytes": max_bytes}

    async def execute(
        self,
        context: ToolContext,
        arguments: dict[str, JsonValue],
    ) -> ToolResult:
        """读取当前会话内的 Artifact；越权、过期与非法编码一律拒绝。"""
        artifact_id = arguments["artifact_id"]
        max_bytes = arguments["max_bytes"]
        assert isinstance(artifact_id, str)
        assert isinstance(max_bytes, int)

        # 归属判定走 link 表：只有已关联到当前会话的产物才可读，
        # 同一个 Owner 的其他会话产物也不行。
        linked = {
            item.artifact_id: item
            for item in self._store.list_for_session(context.session_id, limit=500)
        }
        link = linked.get(artifact_id)
        if link is None:
            return ToolResult.failure("artifact_not_found", "artifact is not available")

        try:
            artifact = self._store.read_metadata(artifact_id)
        except ArtifactError as error:
            return ToolResult.failure(error.code, "artifact is not available")

        data: dict[str, JsonValue] = {
            "artifact_id": artifact.artifact_id,
            "filename": link.filename,
            "media_type": artifact.media_type,
            "byte_size": artifact.byte_size,
        }
        if artifact.media_type not in _TEXT_MEDIA_TYPES:
            data["note"] = "binary artifact; ask the user to preview it in the app"
            return ToolResult.success(data)

        try:
            raw = artifact.path.read_bytes()[:max_bytes]
        except OSError:
            return ToolResult.failure("artifact_unreadable", "artifact is not readable")
        try:
            # 不用 errors="replace"：替换字符会让模型把乱码当成真实内容。
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult.failure(
                "artifact_not_text", "artifact is not valid UTF-8 text"
            )
        data["text"] = text
        # 显式声明截断，否则模型会把半截内容当成文件的全部。
        data["truncated"] = artifact.byte_size > max_bytes
        return ToolResult.success(data)
