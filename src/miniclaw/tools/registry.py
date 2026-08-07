"""按唯一名称管理当前运行期可用 Tool。"""

from collections.abc import Iterable

from miniclaw.providers.base import JsonValue
from miniclaw.tools.base import Tool


class ToolRegistry:
    """注册 Tool，并按稳定顺序生成模型可见 Schema。"""

    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        """注册给定 Tool，并拒绝可能静默覆盖行为的重复名称。"""
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            name = tool.definition.name
            if name in self._tools:
                raise ValueError(f"duplicate tool name: {name}")
            self._tools[name] = tool

    def get(self, name: str) -> Tool | None:
        """按精确名称读取 Tool；未注册时返回 ``None``。"""
        return self._tools.get(name)

    @property
    def schemas(self) -> tuple[dict[str, JsonValue], ...]:
        """按 Tool 名排序返回可直接放入 ModelRequest 的 Schema。"""
        return tuple(
            self._tools[name].definition.to_model_schema() for name in sorted(self._tools)
        )
