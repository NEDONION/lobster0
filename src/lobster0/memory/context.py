"""把完整 Recall Unit 选入有界模型 Context。"""

from dataclasses import dataclass

from lobster0.memory.retrieval import MemorySearchResult


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """描述本轮 Recall 文本、选中 Unit 和实际预算。"""

    text: str
    unit_ids: tuple[str, ...]
    budget_tokens: int


class MemoryContextSelector:
    """使用 8%/2200 token 上限，按命中顺序保留完整 Unit。"""

    def select(
        self,
        result: MemorySearchResult,
        *,
        provider_window: int,
    ) -> MemoryContext:
        """选择完整 Unit；预算不足时丢弃尾项而不截断事实。"""
        if type(provider_window) is not int or provider_window <= 0:
            raise ValueError("provider_window must be a positive integer")
        budget = min(max(1, int(provider_window * 0.08)), 2_200)
        selected: list[str] = []
        identifiers: list[str] = []
        used = 0
        for hit in result.items:
            sources = ",".join(str(source.message_id) for source in hit.unit.sources)
            block = (
                f"- [{hit.unit.id}] {hit.unit.text}\n"
                f"  key={hit.unit.key}; sources={sources}"
            )
            cost = max(1, (len(block) + 3) // 4)
            if used + cost > budget:
                continue
            selected.append(block)
            identifiers.append(hit.unit.id)
            used += cost
        return MemoryContext("\n".join(selected), tuple(identifiers), budget)
