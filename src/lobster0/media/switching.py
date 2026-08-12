"""按"这一轮有没有图"在主模型与视觉模型之间自动切换的 Provider 包装。

## 为什么做成包装而不是改 AgentRunner

``AgentRunner`` 只知道"有一个 Provider 可以 complete"。把选择逻辑放进包装里，Runner、
Turn、Context 全都不需要知道视觉模型的存在——它们照常构造请求，包装在最后一刻看一眼
请求里有没有图片，再决定发给谁。

这样也避免了在多处重复"要不要切"的判断：只要图片挂在了请求上，切换就一定发生；
反之纯文字轮次永远不会碰到视觉模型，不会多花一分钱。

## 为什么没图时绝不回退

文本模型收到图片不会报错——它只是看不见，然后**照着上下文编一个像样的回答**。
Owner 会以为模型真的看过那张图。因此未配置视觉后端时带图必须直接失败。
"""

from lobster0.media.router import MediaRouteError
from lobster0.providers.base import ModelRequest, ModelResponse, StreamHandler


class VisionSwitchingProvider:
    """在带图请求上自动切到视觉后端的 Provider 包装。

    Args:
        default: 主对话 Provider，承载全部纯文字轮次。
        vision: 视觉 Provider；未配置时为 ``None``。
        vision_model: 视觉模型名；未配置时为 ``None``。
    """

    def __init__(
        self,
        default,
        *,
        vision=None,
        vision_model: str | None = None,
    ) -> None:
        """绑定主后端与可选的视觉后端。"""
        self._default = default
        self._vision = vision
        self._vision_model = vision_model

    async def aclose(self) -> None:
        """关闭两个后端。

        视觉后端持有自己的 HTTP 连接池，只关主后端会把它泄漏到进程退出。
        任一关闭失败都不能阻止另一个被关掉。
        """
        for backend in (self._default, self._vision):
            if backend is None:
                continue
            close = getattr(backend, "aclose", None)
            if close is None:
                continue
            try:
                await close()
            except Exception:  # noqa: BLE001 - 关闭失败不得掩盖另一个后端的清理
                continue

    async def complete(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> ModelResponse:
        """按请求里是否携带图片选择后端并转发。

        Raises:
            MediaRouteError: 请求带图但没有配置可用的视觉后端。
        """
        if not any(message.images for message in request.messages):
            return await self._default.complete(request, on_text)
        if self._vision is None or not self._vision_model:
            raise MediaRouteError(
                "vision_not_configured",
                "this turn contains images but no vision model is configured",
            )
        from dataclasses import replace

        return await self._vision.complete(
            replace(request, model=self._vision_model), on_text
        )
