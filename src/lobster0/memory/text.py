"""Memory Recall 共用的 Unicode/CJK 规范化与有界 n-gram。"""

import re
import unicodedata

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_WORD = re.compile(r"[a-z0-9_]{2,64}")


def normalize_memory_text(value: str) -> str:
    """把 Memory 文本规范成稳定 NFKC/casefold/单空格形式。"""
    if not isinstance(value, str):
        raise TypeError("memory text must be a string")
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def memory_search_tokens(value: str, *, maximum: int = 256) -> tuple[str, ...]:
    """生成去重的英文词与中文 2/3-gram，限制数量避免索引膨胀。"""
    if type(maximum) is not int or not 1 <= maximum <= 2_000:
        raise ValueError("memory token maximum must be between 1 and 2000")
    normalized = normalize_memory_text(value)
    tokens: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        """按首次出现顺序添加非空 token。"""
        if token and token not in seen and len(tokens) < maximum:
            seen.add(token)
            tokens.append(token)

    for word in _WORD.findall(normalized):
        add(word)
    for match in _CJK_RUN.finditer(normalized):
        run = match.group(0)
        if len(run) == 1:
            add(run)
            continue
        for width in (2, 3):
            for index in range(max(0, len(run) - width + 1)):
                add(run[index : index + width])
    return tuple(tokens)


def memory_search_shadow(value: str) -> str:
    """把检索 token 编码为供 FTS5 unicode61 使用的空格文本。"""
    return " ".join(memory_search_tokens(value))
