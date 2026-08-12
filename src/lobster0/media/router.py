"""按模型能力与 Owner 明确请求决定附件是否进入模型请求。

## 两条不可绕过的规则

**规则一：不请求就不发送。** Owner 上传一张图并说"谢谢"，这张图不该被发给模型——那既
产生费用，也把私人图像交给了外部服务。只有 Owner 明确要求处理这张图时才路由。

**规则二：模型没有视觉能力就明确失败。** 当前选中的模型不支持图像时，必须报出可操作的
错误，而**不能**静默改用另一个（往往更贵的）模型。除非配置里明确允许该 fallback 路由，
否则"帮用户做主换模型"就是在替 Owner 花钱。
"""

import re
from dataclasses import dataclass

_IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})
# 只识别明确指向"处理这张图"的意图；泛泛的礼貌用语不算请求。
_VISION_INTENT = re.compile(
    r"(?i)(看|识别|读|认|分析|描述|提取|认出|里面(写|是)的?什么|上面(写|是)的?什么|"
    r"什么内容|是什么|ocr|read|describe|analyz|extract|recogni)"
)


class MediaRouteError(RuntimeError):
    """表示附件无法被安全路由；不包含图像内容或本机路径。"""

    def __init__(self, code: str, message: str) -> None:
        """保存稳定错误码与可操作的安全消息。"""
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ModelCapabilities:
    """描述当前选中模型的输入能力。"""

    model: str
    vision: bool


@dataclass(frozen=True, slots=True)
class MediaRoute:
    """一次路由决策：哪些附件会真正进入模型请求。"""

    capabilities: ModelCapabilities
    image_artifact_ids: tuple[str, ...]

    @property
    def sends_images(self) -> bool:
        """是否有图像会被发送。"""
        return bool(self.image_artifact_ids)


class MediaRouter:
    """按能力与明确请求裁决附件路由。"""

    def __init__(self, capabilities: ModelCapabilities) -> None:
        """绑定当前选中模型的能力声明。"""
        self._capabilities = capabilities

    def resolve(
        self,
        attachments: tuple[tuple[str, str], ...],
        *,
        user_text: str,
    ) -> MediaRoute:
        """决定这一轮哪些附件进入模型请求。

        Args:
            attachments: ``(artifact_id, media_type)`` 序列。
            user_text: Owner 本轮的原始文本，用于判断是否明确请求处理图像。

        Returns:
            只包含获准发送的图像 ID 的路由；未请求时为空。

        Raises:
            MediaRouteError: Owner 明确请求处理图像，但当前模型不支持视觉。
        """
        images = tuple(
            artifact_id
            for artifact_id, media_type in attachments
            if media_type in _IMAGE_MEDIA_TYPES
        )
        if not images:
            return MediaRoute(self._capabilities, ())
        if not _requests_vision(user_text):
            # 规则一：没有明确请求，附件只作为 manifest 里的元数据存在。
            return MediaRoute(self._capabilities, ())
        if not self._capabilities.vision:
            # 规则二：明确失败，不静默改用其他模型。
            raise MediaRouteError(
                "model_lacks_vision",
                f"当前模型 {self._capabilities.model} 不支持图像输入；"
                "请在配置中改用支持视觉的模型后重试。",
            )
        return MediaRoute(self._capabilities, images)


def _requests_vision(user_text: str) -> bool:
    """判断 Owner 是否明确要求处理图像内容。"""
    return bool(user_text) and _VISION_INTENT.search(user_text) is not None
