"""把已上传的图片附件读成可发给视觉模型的内容分片。

## 为什么读取发生在这里而不是上传时

上传时只做存储与校验，**不读正文进内存**；图片字节只在真的要发给视觉模型的那一刻
才被读出来，用完即弃，不落日志也不进持久化。这样一次纯文字对话不会因为历史里有图
而白白读盘、白白花钱。

非图片附件（PDF、压缩包等）在这里被跳过：它们的内容对视觉模型没有意义，硬塞进去
只会烧 token。它们仍然以文字摘要的形式出现在上下文里，模型需要时可以用
``read_artifact`` 工具按需读取。
"""

import hashlib
from dataclasses import replace
from typing import Protocol

from lobster0.providers.base import ImagePart, ModelRequest

# 与 ImagePart 的白名单保持一致；这里再列一次是为了在读盘之前就跳过，
# 避免为一个注定被拒绝的类型白白读一遍文件。
_VISION_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})
_MAX_IMAGE_BYTES = 8 * 1024 * 1024


class ArtifactMetadata(Protocol):
    """收窄本模块需要的 Artifact 字段。"""

    media_type: str
    content_hash: str

    @property
    def path(self): ...


class ArtifactReader(Protocol):
    """收窄本模块对 ArtifactStore 的读取能力。"""

    def read_metadata(self, artifact_id: str) -> ArtifactMetadata:
        """按 id 读取已校验的 Artifact 元数据与本地路径。"""
        ...


def build_image_parts(
    store: ArtifactReader,
    summaries: tuple[dict[str, object], ...],
) -> tuple[ImagePart, ...]:
    """把附件摘要里的图片读成 ImagePart；非图片一律跳过。

    Args:
        store: 提供 Artifact 元数据与本地路径的 Store。
        summaries: ``handle`` 已经解析过的附件摘要。

    Returns:
        按摘要顺序排列的图片分片；没有图片时为空元组。

    Raises:
        ValueError: 磁盘上的字节与 Store 记录的内容哈希不一致。
    """
    parts: list[ImagePart] = []
    for summary in summaries:
        media_type = summary.get("media_type")
        if not isinstance(media_type, str) or media_type not in _VISION_MEDIA_TYPES:
            continue
        artifact_id = summary.get("artifact_id")
        if not isinstance(artifact_id, str):
            continue
        artifact = store.read_metadata(artifact_id)
        data = artifact.path.read_bytes()[: _MAX_IMAGE_BYTES + 1]
        if len(data) > _MAX_IMAGE_BYTES:
            # 超限不是错误，只是不发：模型侧本来也会拒收，跳过比整轮失败温和。
            continue
        # 重新校验内容哈希：元数据可能是很久以前写的，磁盘上的文件可能已被替换。
        if hashlib.sha256(data).hexdigest() != artifact.content_hash:
            raise ValueError("artifact bytes no longer match the recorded content hash")
        parts.append(
            ImagePart(
                media_type=media_type,
                content_hash=artifact.content_hash,
                data=data,
            )
        )
    return tuple(parts)


def attach_images_to_request(
    request: ModelRequest, images: tuple[ImagePart, ...]
) -> ModelRequest:
    """把图片挂到请求里**最后一条用户消息**上。

    挂在最后一条用户消息，而不是散落到历史里：视觉模型按消息顺序理解"这句话配这张图"，
    挂错位置会让它把旧问题和新图片对应起来。没有用户消息时原样返回——宁可不发，
    也不能把图挂到 system 上改变它的语义。

    Args:
        request: 已经构建好的模型请求。
        images: 本轮要发送的图片分片；为空时原样返回。

    Returns:
        挂好图片的新请求；无需改动时返回原对象。
    """
    if not images:
        return request
    messages = list(request.messages)
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "user":
            messages[index] = replace(messages[index], images=images)
            return replace(request, messages=tuple(messages))
    return request
