"""启发式 rerank：对 BM25 召回结果做精排。

不依赖任何外部模型，纯启发式信号叠加在原始召回分数之上：

- **标题命中加权**：query token 命中 chunk 的 headings → 加权
- **关键词覆盖率**：query token 有多少比例出现在 chunk 中
- **长度偏好**：偏好 200-800 字符的 chunk，过短/过长惩罚
- **位置偏置**：靠近文档开头的 chunk 略微加权
- **代码块加分**：query 含代码迹象时，代码 chunk 加权

接口：``rerank(query, hits, top_k) -> list[(Chunk, float)]``。
"""

from __future__ import annotations

import re

from minicode.knowledge.bm25 import _expand_query_terms, _tokenize
from minicode.knowledge.types import Chunk

# query 中代码迹象的信号词/符号
_CODE_HINT_RE = re.compile(
    r"(def |class |function|import |->|::|\(\)|\{\}|=>|api|method|函数|类|方法|接口)",
    re.IGNORECASE,
)


def _coverage(query_tokens: set[str], chunk_tokens: set[str]) -> float:
    if not query_tokens:
        return 0.0
    hit = len(query_tokens & chunk_tokens)
    return hit / len(query_tokens)


def _length_factor(length: int) -> float:
    """长度偏好：200-800 字符最优，之外线性衰减。"""
    if length < 60:
        return 0.5
    if length < 200:
        return 0.8
    if length <= 800:
        return 1.0
    if length <= 1500:
        return 0.85
    return 0.7


def _heading_hits(query_tokens: set[str], headings: list[str]) -> int:
    if not headings:
        return 0
    heading_tokens: set[str] = set()
    for h in headings:
        heading_tokens.update(_tokenize(h))
    return len(query_tokens & heading_tokens)


def rerank(
    query: str,
    hits: list[tuple[Chunk, float]],
    top_k: int = 5,
) -> list[tuple[Chunk, float]]:
    """对召回结果做启发式精排。

    参数：
        query: 原始查询
        hits: ``[(Chunk, base_score), ...]`` 召回结果
        top_k: 返回结果数

    返回：
        精排后的 ``[(Chunk, final_score), ...]``，按分数倒序，长度 ≤ top_k。
    """
    if not hits:
        return []

    q_tokens = set(_expand_query_terms(_tokenize(query)))
    query_is_code = bool(_CODE_HINT_RE.search(query))

    # 归一化 base score 到 0-1，避免量纲压过启发式信号
    max_base = max((s for _, s in hits), default=0.0) or 1.0

    scored: list[tuple[Chunk, float]] = []
    for chunk, base in hits:
        chunk_tokens = set(_tokenize(chunk.text))

        base_norm = base / max_base  # 0-1

        coverage = _coverage(q_tokens, chunk_tokens)
        length_factor = _length_factor(len(chunk.text))
        heading_hits = _heading_hits(q_tokens, chunk.headings)
        position_bonus = 0.1 / (1.0 + chunk.position)  # 越靠前越高

        code_bonus = 0.0
        if query_is_code:
            mime = str(chunk.metadata.get("mime", ""))
            if mime.startswith("text/x-") and mime != "text/x-rst":
                code_bonus = 0.3

        final = (
            base_norm * 1.0
            + coverage * 0.8
            + heading_hits * 0.5
            + (length_factor - 1.0) * 0.3
            + position_bonus
            + code_bonus
        )
        scored.append((chunk, final))

    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
