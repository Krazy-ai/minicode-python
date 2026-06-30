"""
精排器 (M2)。

使用 LLM 对召回结果重新打分排序，提升准确率。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from minicode.knowledge.types import RetrievalResult

logger = logging.getLogger(__name__)


class LLMReranker:
    """LLM 精排器：使用 LLM 重新打分排序。"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.model_adapter = None
        self._init_model()

    def _init_model(self) -> None:
        """初始化 LLM 适配器（延迟加载）。"""
        try:
            from minicode.model import create_model_adapter
            self.model_adapter = create_model_adapter(self.config)
        except Exception as e:
            logger.warning(f"LLM 适配器初始化失败: {e}")

    def rerank(
        self,
        query: str,
        results: list[RetrievalResult],
        *,
        batch_size: int = 5,
    ) -> list[RetrievalResult]:
        """精排召回结果。

        参数：
            query: 查询字符串
            results: 召回结果列表
            batch_size: 批量打分大小

        返回：
            重新排序后的结果列表
        """
        if not self.model_adapter or len(results) <= 1:
            return sorted(results, key=lambda r: r.score, reverse=True)

        # 批量打分
        try:
            scores = self._batch_score(query, results, batch_size)
            for r, s in zip(results, scores):
                r.score = float(s)
        except Exception as e:
            logger.warning(f"LLM 精排失败: {e}")

        return sorted(results, key=lambda r: r.score, reverse=True)

    def rerank_stream(
        self,
        query: str,
        results: list[RetrievalResult],
    ):
        """流式精排（yield 进度）。"""
        yield {"stage": "reranking", "total": len(results)}

        if not self.model_adapter or len(results) <= 1:
            yield results
            return

        try:
            scores = self._batch_score(query, results, 5)
            for r, s in zip(results, scores):
                r.score = float(s)
        except Exception as e:
            logger.warning(f"流式精排失败: {e}")

        sorted_results = sorted(results, key=lambda r: r.score, reverse=True)
        yield sorted_results

    def _batch_score(
        self,
        query: str,
        results: list[RetrievalResult],
        batch_size: int,
    ) -> list[float]:
        """批量打分。"""
        scores = []
        for i in range(0, len(results), batch_size):
            batch = results[i:i + batch_size]
            batch_scores = self._score_batch(query, batch)
            scores.extend(batch_scores)
        return scores

    def _score_batch(
        self,
        query: str,
        batch: list[RetrievalResult],
    ) -> list[float]:
        """对一批结果打分。"""
        prompt = self._build_rerank_prompt(query, batch)
        try:
            response = self.model_adapter.generate(prompt, max_tokens=100)
            scores = self._parse_rerank_response(response, len(batch))
            return scores
        except Exception as e:
            logger.warning(f"批量打分失败: {e}")
            return [r.score for r in batch]

    def _build_rerank_prompt(
        self,
        query: str,
        batch: list[RetrievalResult],
    ) -> str:
        """构建精排提示词。"""
        context = "\n\n".join(
            f"[片段 {i + 1}]\n{r.chunk.text[:200]}"
            for i, r in enumerate(batch)
        )
        prompt = f"""请评估以下片段与查询的相关性，给出 0-10 的分数。
要求：
1. 分数越高表示越相关
2. 每行一个分数，格式：<数字>
3. 不要解释

查询：{query}

{context}

分数："""
        return prompt

    def _parse_rerank_response(self, response: str, expected_count: int) -> list[float]:
        """解析精排响应。"""
        import re
        scores = []
        for line in response.strip().split("\n"):
            line = line.strip()
            match = re.search(r"(\d+(?:\.\d+)?)", line)
            if match:
                try:
                    score = float(match.group(1))
                    scores.append(min(max(score, 0.0), 10.0))
                except ValueError:
                    pass
        # 补齐或截断
        while len(scores) < expected_count:
            scores.append(5.0)
        return scores[:expected_count]


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = ["LLMReranker"]
