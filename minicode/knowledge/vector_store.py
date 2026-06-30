"""
向量存储 (M3 简化版)。

使用 TF-IDF 向量化 + 余弦相似度实现轻量向量检索。
不依赖外部 ML 库，使用 numpy 实现。
"""

from __future__ import annotations

import logging
import math
from collections import Counter
from typing import Any, Optional

import numpy as np

from minicode.knowledge.types import Chunk, RetrievalResult

logger = logging.getLogger(__name__)


class VectorStore:
    """轻量向量存储（基于 TF-IDF 向量化）。"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.dimension = self.config.get("vector_dim", 128)  # 简化版不使用真实维度
        self.chunks: list[Chunk] = []
        self.vectors: list[np.ndarray] = []

    def add_chunks(self, chunks: list[Chunk]) -> None:
        """添加分块到向量存储。"""
        for chunk in chunks:
            vector = self._text_to_vector(chunk.text)
            self.chunks.append(chunk)
            self.vectors.append(vector)

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        threshold: float = 0.0,
    ) -> list[RetrievalResult]:
        """向量检索。

        参数：
            query: 查询字符串
            top_k: 返回结果数量
            threshold: 相似度阈值

        返回：
            RetrievalResult 列表
        """
        if not self.chunks:
            return []

        query_vector = self._text_to_vector(query)
        results = []

        for chunk, vector in zip(self.chunks, self.vectors):
            score = self._cosine_similarity(query_vector, vector)
            if score >= threshold:
                results.append(
                    RetrievalResult(
                        chunk=chunk,
                        score=float(score),
                        source="vector",
                    )
                )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]

    def clear(self) -> None:
        """清空向量存储。"""
        self.chunks.clear()
        self.vectors.clear()

    def _text_to_vector(self, text: str) -> np.ndarray:
        """将文本转换为 TF-IDF 向量（简化版）。"""
        # 分词（使用 BM25 的分词逻辑）
        try:
            from minicode.knowledge.bm25 import _tokenize
            tokens = _tokenize(text)
        except ImportError:
            tokens = text.lower().split()

        # 构建词频向量
        tf = Counter(tokens)
        if not tf:
            return np.zeros(self.dimension)

        # 简化为固定维度向量（取前 dimension 个词的 TF）
        vector = np.zeros(self.dimension)
        for i, (term, count) in enumerate(tf.items()):
            if i >= self.dimension:
                break
            vector[i] = count

        # 归一化
        norm = np.linalg.norm(vector)
        if norm > 0:
            vector = vector / norm

        return vector

    def _cosine_similarity(self, v1: np.ndarray, v2: np.ndarray) -> float:
        """计算余弦相似度。"""
        dot = np.dot(v1, v2)
        norm1 = np.linalg.norm(v1)
        norm2 = np.linalg.norm(v2)
        if norm1 == 0 or norm2 == 0:
            return 0.0
        return float(dot / (norm1 * norm2))


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = ["VectorStore"]
