"""knowledge 子系统的核心数据类型。

定义贯穿整个检索管线的不可变数据结构：
- Document：一份被解析后的源文件。
- Chunk：文档被切分后的最小检索单元。
- RetrievalResult：一次检索的结果（命中的 chunk + 分数）。
- Citation：可供 agent 在回答里标注来源的引用。
- IngestReport：一次索引构建的统计报告。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


def _stable_id(*parts: str) -> str:
    """根据若干字符串生成稳定的短 id（sha1 前 16 位）。"""
    joined = "\x00".join(parts)
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:16]


@dataclass
class Document:
    """一份被解析后的源文件。

    Attributes:
        id: 文档唯一 id（通常由 source_path 派生，稳定可复现）。
        source_path: 文档的源路径（相对索引根目录的 posix 路径）。
        content: 文档的纯文本内容。
        mime: 内容类型标识（如 ``text/markdown`` / ``text/x-python``）。
        metadata: 附加元数据（如 mtime / size / encoding 等）。
    """

    id: str
    source_path: str
    content: str
    mime: str = "text/plain"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make_id(cls, source_path: str) -> str:
        """由源路径生成稳定的文档 id。"""
        return _stable_id(source_path)


@dataclass
class Chunk:
    """文档被切分后的最小检索单元。

    Attributes:
        id: chunk 唯一 id。
        doc_id: 所属文档 id。
        text: chunk 文本内容。
        position: chunk 在文档中的序号（从 0 开始）。
        headings: 该 chunk 所处的标题路径（用于 rerank 加权），如
            ``["架构设计", "数据流"]``。
        metadata: 附加元数据（如 source_path / is_code 等）。
    """

    id: str
    doc_id: str
    text: str
    position: int = 0
    headings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def make_id(cls, doc_id: str, position: int) -> str:
        """由文档 id 与序号生成稳定的 chunk id。"""
        return _stable_id(doc_id, str(position))

    @property
    def source_path(self) -> str:
        """便捷读取所属文档的源路径（若 metadata 中带有）。"""
        return str(self.metadata.get("source_path", ""))


@dataclass
class RetrievalResult:
    """一次检索的结果（单个结果）。

    包含命中的 chunk、相关度分数和来源。
    """

    chunk: Chunk | None = None
    score: float = 0.0
    source: str = "unknown"

    def __bool__(self) -> bool:
        return self.chunk is not None

    @property
    def text(self) -> str:
        """便捷访问 chunk 文本。"""
        return self.chunk.text if self.chunk else ""

    @property
    def source_path(self) -> str:
        """便捷访问 chunk 来源路径。"""
        return self.chunk.source_path if self.chunk else ""

    def to_citation(self, max_excerpt: int = 200) -> Citation | None:
        """转为引用对象。"""
        if not self.chunk:
            return None
        excerpt = self.chunk.text.strip().replace("\n", " ")
        if len(excerpt) > max_excerpt:
            excerpt = excerpt[:max_excerpt] + "…"
        anchor = self.chunk.headings[-1] if self.chunk.headings else ""
        return Citation(
            chunk_id=self.chunk.id,
            source_path=self.chunk.source_path,
            text_excerpt=excerpt,
            score=self.score,
            heading=anchor,
        )


@dataclass
class Citation:
    """可供 agent 在回答里标注来源的引用。

    渲染形态形如 ``[source: docs/auth.md#section]``。
    """

    chunk_id: str
    source_path: str
    text_excerpt: str
    score: float = 0.0
    heading: str = ""

    def label(self) -> str:
        """生成 ``docs/auth.md#section`` 形式的来源标签。"""
        if self.heading:
            return f"{self.source_path}#{self.heading}"
        return self.source_path


@dataclass
class IngestReport:
    """一次索引构建的统计报告。"""

    index_name: str = "default"
    scope: str = "project"
    documents_total: int = 0
    documents_added: int = 0
    documents_updated: int = 0
    documents_removed: int = 0
    documents_skipped: int = 0
    chunks_total: int = 0
    chunks_created: int = 0  # 兼容旧代码
    chunks_failed: int = 0  # 兼容旧代码
    vectors_indexed: int = 0
    total_tokens: int = 0  # 兼容旧代码
    duration_sec: float = 0.0  # 兼容旧代码
    index_path: str = ""
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict，便于打印或 JSON 输出。"""
        return {
            "index_name": self.index_name,
            "scope": self.scope,
            "documents_total": self.documents_total,
            "documents_added": self.documents_added,
            "documents_updated": self.documents_updated,
            "documents_removed": self.documents_removed,
            "documents_skipped": self.documents_skipped,
            "chunks_total": self.chunks_total,
            "vectors_indexed": self.vectors_indexed,
            "index_path": self.index_path,
            "errors": list(self.errors),
        }

    def summary(self) -> str:
        """生成一行人类可读的摘要。"""
        return (
            f"index '{self.index_name}' ({self.scope}): "
            f"{self.documents_total} docs "
            f"(+{self.documents_added} ~{self.documents_updated} "
            f"-{self.documents_removed} skip{self.documents_skipped}), "
            f"{self.chunks_total} chunks"
            + (f", {self.vectors_indexed} vectors" if self.vectors_indexed else "")
        )

    def merge(self, other: "IngestReport") -> None:
        """合并另一个报告到当前报告。"""
        self.documents_total += other.documents_total
        self.documents_added += other.documents_added
        self.documents_updated += other.documents_updated
        self.documents_removed += other.documents_removed
        self.documents_skipped += other.documents_skipped
        self.chunks_total += other.chunks_total
        self.chunks_created += other.chunks_created
        self.chunks_failed += other.chunks_failed
        self.vectors_indexed += other.vectors_indexed
        self.total_tokens += other.total_tokens
        self.duration_sec += other.duration_sec
        self.errors.extend(other.errors)
