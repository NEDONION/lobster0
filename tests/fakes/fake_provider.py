"""按预设顺序返回完整响应或异常的确定性模型 Fake。"""

from miniclaw.providers.base import ModelRequest, ModelResponse, StreamHandler


class FakeProvider:
    """记录真实 Agent 请求，并逐项返回测试提供的模型结果。"""

    def __init__(self, outcomes: tuple[ModelResponse | BaseException, ...]) -> None:
        """保存有限结果序列，任何多余调用都作为测试失败。"""
        self._outcomes = outcomes
        self.requests: list[ModelRequest] = []

    async def complete(
        self,
        request: ModelRequest,
        on_text: StreamHandler | None = None,
    ) -> ModelResponse:
        """记录请求并返回同索引结果；异常结果按原类型抛出。"""
        index = len(self.requests)
        self.requests.append(request)
        if index >= len(self._outcomes):
            raise AssertionError("FakeProvider received more requests than configured")
        outcome = self._outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        if on_text is not None and outcome.content:
            await on_text(outcome.content)
        return outcome
