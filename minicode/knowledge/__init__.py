"""knowledge 子包：为 agent 提供外部文档知识库（RAG）。

与 ``minicode/memory/`` 的定位区别：
- ``memory/``：agent 自己的内省记忆，自动注入 system prompt。
- ``knowledge/``：外部文档知识库（README / 设计文档 / 代码注释等），
  通过工具或 ``/ask`` 命令按需检索。

设计原则：**默认零依赖**（纯 BM25 + 启发式 rerank），向量增强作为
可选 extras（``pip install minicode-py[rag-vector]``），未安装时透明降级。
"""

from minicode.knowledge.types import (
    Chunk,
    Citation,
    Document,
    IngestReport,
    RetrievalResult,
)

__all__ = [
    "Chunk",
    "Citation",
    "Document",
    "IngestReport",
    "RetrievalResult",
]
