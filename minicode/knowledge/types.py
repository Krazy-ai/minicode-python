"""knowledge 子系统的核心数据类型。

这些是在整个 RAG 管线（parsers → chunker → store → 检索）中传递的
不可变/半结构化数据契约：

- ``Document``：一份被解析出来的源文件
- ``Chunk``：文档切分后的最小检索单元
- ``RetrievalResult``：一次检索的结果集（chunks + 分数 + 引用）
- ``Citation``：单条可展示的引用（供 agent 在回答里标注来源）
- ``IngestReport``：一次索引构建的统计报告
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Document:
    """一份被解析出来的源文件。

    Attributes:
        id: 文档唯一 ID（通常是 source_path 的稳定 hash）
        source_path: 源文件路径（相对或绝对，展示用）
        content: 文件纯文本内容
        mime: 简单的类型标记（如 ``text/markdown`` / ``text/x-python``）
        metadata: 额外元数据（编码、大小、mtime 等）
    """

    id: str
    source_path: str
    content: str
    mime: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_id(source_path: str) -> str:
        """基于源路径生成稳定的文档 ID。"""
        return hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_path": self.source_path,
            "content": self.content,
            "mime": self.mime,
            "metadata": self.metadata,
        }


@dataclass
class Chunk:
    """文档切分后的最小检索单元。

    Attributes:
        id: chunk 唯一 ID（``{doc_id}#{position}``）
        doc_id: 所属文档 ID
        text: chunk 文本
        position: 在文档中的序号（从 0 开始）
        source_path: 冗余存一份源路径，方便检索结果直接引用
        headings: 该 chunk 所处的标题路径（markdown）/ 符号名（代码）
        metadata: 额外元数据
    """

    id: str
    doc_id: str
    text: str
    position: int = 0
    source_path: str = ""
    headings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def make_id(doc_id: str, position: int) -> str:
        """生成 chunk ID。"""
        return f"{doc_id}#{position}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "doc_id": self.doc_id,
            "text": self.text,
            "position": self.position,
            "source_path": self.source_path,
            "headings": self.headings,
            "metadata": self.metadata,
        }

    @property
    def heading_path(self) -> str:
        """把标题路径拼成 ``a > b > c`` 形式，展示用。"""
        return " > ".join(h for h in self.headings if h)


@dataclass
class Citation:
    """单条可展示的引用。

    供 agent 在回答里标注来源，形如 ``[source: docs/auth.md#section]``。
    """

    chunk_id: str
    source_path: str
    text_excerpt: str
    score: float = 0.0
    heading_path: str = ""

    def format_ref(self, max_excerpt: int = 160) -> str:
        """格式化为一条人类可读的引用行。"""
        loc = self.source_path
        if self.heading_path:
            loc = f"{loc}#{self.heading_path}"
        excerpt = self.text_excerpt.strip().replace("\n", " ")
        if len(excerpt) > max_excerpt:
            excerpt = excerpt[:max_excerpt].rstrip() + "…"
        return f"[source: {loc}] {excerpt}"


@dataclass
class RetrievalResult:
    """一次检索的结果集。"""

    query: str
    chunks: list[Chunk] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.chunks)

    def __bool__(self) -> bool:
        return bool(self.chunks)

    def citations(self, max_excerpt: int = 160) -> list[Citation]:
        """把结果转成 Citation 列表。"""
        cites: list[Citation] = []
        for i, chunk in enumerate(self.chunks):
            score = self.scores[i] if i < len(self.scores) else 0.0
            excerpt = chunk.text.strip()
            if len(excerpt) > max_excerpt:
                excerpt = excerpt[:max_excerpt].rstrip() + "…"
            cites.append(
                Citation(
                    chunk_id=chunk.id,
                    source_path=chunk.source_path or chunk.doc_id,
                    text_excerpt=excerpt,
                    score=score,
                    heading_path=chunk.heading_path,
                )
            )
        return cites

    def format_context(self, max_chars_per_chunk: int = 1200) -> str:
        """把检索到的 chunks 拼成可喂给 LLM 的 context 文本。"""
        parts: list[str] = []
        for i, chunk in enumerate(self.chunks, start=1):
            loc = chunk.source_path or chunk.doc_id
            if chunk.heading_path:
                loc = f"{loc}#{chunk.heading_path}"
            text = chunk.text.strip()
            if len(text) > max_chars_per_chunk:
                text = text[:max_chars_per_chunk].rstrip() + "…"
            parts.append(f"[{i}] ({loc})\n{text}")
        return "\n\n".join(parts)


@dataclass
class IngestReport:
    """一次索引构建的统计报告。"""

    index_name: str
    scope: str
    documents_total: int = 0
    documents_indexed: int = 0
    documents_skipped: int = 0
    documents_deleted: int = 0
    chunks_total: int = 0
    errors: list[str] = field(default_factory=list)
    index_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_name": self.index_name,
            "scope": self.scope,
            "documents_total": self.documents_total,
            "documents_indexed": self.documents_indexed,
            "documents_skipped": self.documents_skipped,
            "documents_deleted": self.documents_deleted,
            "chunks_total": self.chunks_total,
            "errors": self.errors,
            "index_path": self.index_path,
        }

    def summary(self) -> str:
        """一行人类可读摘要。"""
        return (
            f"index '{self.index_name}' ({self.scope}): "
            f"{self.documents_indexed} indexed, "
            f"{self.documents_skipped} skipped, "
            f"{self.documents_deleted} deleted, "
            f"{self.chunks_total} chunks"
            + (f", {len(self.errors)} errors" if self.errors else "")
        )
