"""knowledge 子包：为 agent 提供外部文档知识库（RAG）。

与 ``minicode/memory/`` 的区别：
- memory：agent 自己的内省记忆，自动注入 system prompt。
- knowledge：外部文档知识库（README / 设计文档 / 代码注释 / 项目手册等），
  通过工具按需检索，或用户通过 ``/ask`` 命令直接提问。

设计原则：
- **默认零依赖**：纯 BM25 检索 + 启发式 rerank，不引入任何第三方库。
- **可选向量增强**：安装 ``[rag-vector]`` extras 后启用 hybrid（BM25 + 向量）检索；
  未安装时自动降级到纯 BM25，调用方无感知。

主要入口在 ``pipeline.py``：``ingest`` / ``query`` / ``status``。
"""

from minicode.knowledge.types import (
    Chunk,
    Citation,
    Document,
    IngestReport,
    RetrievalResult,
)
from minicode.knowledge.parsers import TextParser
from minicode.knowledge.chunker import MarkdownChunker, CodeChunker, TextChunker
from minicode.knowledge.store import KnowledgeStore
from minicode.knowledge.pipeline import KnowledgePipeline, create_pipeline
from minicode.knowledge.rewriter import QueryRewriter
from minicode.knowledge.reranker import LLMReranker
from minicode.knowledge.vector_store import VectorStore

__all__ = [
    # 数据类型
    "Document",
    "Chunk",
    "RetrievalResult",
    "Citation",
    "IngestReport",
    # 解析器
    "TextParser",
    # 分块器
    "MarkdownChunker",
    "CodeChunker",
    "TextChunker",
    # 存储
    "KnowledgeStore",
    # 管线
    "KnowledgePipeline",
    "create_pipeline",
    # M2: 查询改写 + 精排
    "QueryRewriter",
    "LLMReranker",
    # M3: 向量存储
    "VectorStore",
]
