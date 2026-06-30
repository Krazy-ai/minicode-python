"""
查询改写器 (M2)。

使用 LLM 将用户原始查询改写为多个子查询，以提升召回率。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class QueryRewriter:
    """查询改写器：使用 LLM 生成子查询。"""

    def __init__(self, config: Optional[dict] = None):
        self.config = config or {}
        self.max_queries = self.config.get("max_rewrite_queries", 3)
        self.model_adapter = None
        self._init_model()

    def _init_model(self) -> None:
        """初始化 LLM 适配器（延迟加载）。"""
        try:
            from minicode.model import create_model_adapter
            self.model_adapter = create_model_adapter(self.config)
        except Exception as e:
            logger.warning(f"LLM 适配器初始化失败: {e}")

    def rewrite(self, query: str, context: Optional[dict] = None) -> list[str]:
        """改写查询为多个子查询。

        参数：
            query: 原始查询
            context: 上下文（可选）

        返回：
            改写后的查询列表（包含原始查询）
        """
        if not self.model_adapter:
            return [query]

        prompt = self._build_rewrite_prompt(query, context)
        try:
            response = self.model_adapter.generate(prompt, max_tokens=200)
            queries = self._parse_rewrite_response(response)
            if not queries:
                return [query]
            # 去重并保留原始查询
            all_queries = [query] + [q for q in queries if q != query]
            return all_queries[:self.max_queries]
        except Exception as e:
            logger.warning(f"查询改写失败: {e}")
            return [query]

    def rewrite_stream(self, query: str, context: Optional[dict] = None):
        """流式改写查询。"""
        if not self.model_adapter:
            yield [query]
            return

        prompt = self._build_rewrite_prompt(query, context)
        try:
            response_chunks = []
            for chunk in self.model_adapter.generate_stream(prompt, max_tokens=200):
                response_chunks.append(chunk)
                yield {"stage": "rewriting", "partial": "".join(response_chunks)}

            response = "".join(response_chunks)
            queries = self._parse_rewrite_response(response)
            if not queries:
                yield [query]
                return

            all_queries = [query] + [q for q in queries if q != query]
            yield all_queries[:self.max_queries]
        except Exception as e:
            logger.warning(f"流式查询改写失败: {e}")
            yield [query]

    def _build_rewrite_prompt(self, query: str, context: Optional[dict] = None) -> str:
        """构建改写提示词。"""
        prompt = f"""请将以下用户查询改写为 2-3 个更具体、更易于检索的子查询。
要求：
1. 保留原始查询的核心意图
2. 从不同角度拆解查询
3. 每行一个子查询，不要编号

原始查询：{query}

子查询："""
        return prompt

    def _parse_rewrite_response(self, response: str) -> list[str]:
        """解析改写响应。"""
        queries = []
        for line in response.strip().split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                # 移除可能的编号（如 "1. "、"1) "）
                import re
                line = re.sub(r"^\d+[\.)]\s*", "", line)
                if line:
                    queries.append(line)
        return queries


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = ["QueryRewriter"]
