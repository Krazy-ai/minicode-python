"""
知识库检索管线 (M1 基础版本)。

串联：解析 → 分块 → 存储 → 检索
为 M2/M3 的改写/精排/向量预留软依赖钩子。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

from minicode.knowledge.types import (
    Chunk,
    Document,
    IngestReport,
    RetrievalResult,
)
from minicode.knowledge.parsers import TextParser
from minicode.knowledge.chunker import MarkdownChunker, CodeChunker, TextChunker
from minicode.knowledge.store import KnowledgeStore
from minicode.knowledge.bm25 import BM25Index

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 查询改写器（软依赖，M2 实现）
# ---------------------------------------------------------------------------

class _PlaceholderRewriter:
    """占位改写器：直接返回原查询（M2 将被真实改写器替换）。"""

    def rewrite(self, query: str, context: Optional[dict] = None) -> list[str]:
        return [query]

    def rewrite_stream(self, query: str, context: Optional[dict] = None):
        """流式改写占位（yield 单次）。"""
        yield [query]


def _create_rewriter(config: dict) -> Any:
    """根据配置创建改写器（软依赖注入）。"""
    backend = config.get("query_rewrite_backend", "placeholder")
    if backend == "llm":
        try:
            from minicode.knowledge.rewriter import QueryRewriter
            return QueryRewriter(config)
        except ImportError:
            logger.warning("QueryRewriter 导入失败，回退到 placeholder")
    return _PlaceholderRewriter()


# ---------------------------------------------------------------------------
# 精排器（软依赖，M3 实现）
# ---------------------------------------------------------------------------

class _PlaceholderReranker:
    """占位精排器：按原始分数排序（M3 将被真实精排器替换）。"""

    def rerank(self, query: str, results: list[RetrievalResult]) -> list[RetrievalResult]:
        return sorted(results, key=lambda r: r.score, reverse=True)

    def rerank_stream(self, query: str, results: list[RetrievalResult]):
        """流式精排占位（yield 单次）。"""
        yield self.rerank(query, results)


def _create_reranker(config: dict) -> Any:
    """根据配置创建精排器（软依赖注入）。"""
    backend = config.get("rerank_backend", "placeholder")
    if backend == "llm":
        try:
            from minicode.knowledge.reranker import LLMReranker
            return LLMReranker(config)
        except ImportError:
            logger.warning("LLMReranker 导入失败，回退到 placeholder")
    return _PlaceholderReranker()


# ---------------------------------------------------------------------------
# 向量检索器（软依赖，M3 实现）
# ---------------------------------------------------------------------------

class _PlaceholderVectorStore:
    """占位向量存储：返回空结果（M3 将被真实向量存储替换）。"""

    def search(self, query: str, top_k: int = 5) -> list[RetrievalResult]:
        return []

    def add_chunks(self, chunks: list[Chunk]) -> None:
        pass


def _create_vector_store(config: dict) -> Any:
    """根据配置创建向量存储（软依赖注入）。"""
    backend = config.get("vector_backend", "placeholder")
    if backend in ("simple", "hnsw"):
        try:
            from minicode.knowledge.vector_store import VectorStore
            return VectorStore(config)
        except ImportError:
            logger.warning("VectorStore 导入失败，回退到 placeholder")
    return _PlaceholderVectorStore()


# ---------------------------------------------------------------------------
# 核心管线
# ---------------------------------------------------------------------------

class KnowledgePipeline:
    """知识库检索管线。

    支持：
    - 文本/文件摄入（ingest）
    - BM25 全文检索（retrieve）
    - 查询改写（M2）
    - 精排（M3）
    - 向量检索（M3）
    """

    def __init__(
        self,
        workspace: str | Path,
        config: Optional[dict] = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.config = config or {}

        # 子模块
        self.store = KnowledgeStore(workspace, self.config)
        self.text_parser = TextParser()

        # 软依赖模块（延迟加载）
        self._rewriter = None
        self._reranker = None
        self._vector_store = None
        self._rewriter_initialized = False
        self._reranker_initialized = False
        self._vector_store_initialized = False

    # ------------------------------------------------------------------
    # 延迟初始化
    # ------------------------------------------------------------------

    @property
    def rewriter(self) -> Any:
        if not self._rewriter_initialized:
            self._rewriter = _create_rewriter(self.config)
            self._rewriter_initialized = True
        return self._rewriter

    @property
    def reranker(self) -> Any:
        if not self._reranker_initialized:
            self._reranker = _create_reranker(self.config)
            self._reranker_initialized = True
        return self._reranker

    @property
    def vector_store(self) -> Any:
        if not self._vector_store_initialized:
            self._vector_store = _create_vector_store(self.config)
            self._vector_store_initialized = True
        return self._vector_store

    # ------------------------------------------------------------------
    # 摄入管线
    # ------------------------------------------------------------------

    def ingest(
        self,
        texts: list[str],
        metadatas: Optional[list[dict]] = None,
        source_name: str = "manual",
        *,
        chunk_strategy: str = "auto",
    ) -> IngestReport:
        """摄入文本列表到知识库。

        参数：
            texts: 文本列表
            metadatas: 每个文本的元数据（可选）
            source_name: 来源名称
            chunk_strategy: 分块策略 ("auto" | "markdown" | "code" | "text")

        返回：
            IngestReport
        """
        t0 = time.time()
        chunks: list[Chunk] = []
        failed = 0

        for i, text in enumerate(texts):
            try:
                # 选择分块策略
                strategy = chunk_strategy
                if strategy == "auto":
                    strategy = self._auto_detect_strategy(text)

                # 分块
                if strategy == "markdown":
                    chunker = MarkdownChunker()
                elif strategy == "code":
                    chunker = CodeChunker()
                else:
                    chunker = TextChunker()

                doc_chunks = chunker.chunk_text(text, source=f"{source_name}_{i}")

                # 附加元数据
                if metadatas and i < len(metadatas):
                    for c in doc_chunks:
                        c.metadata.update(metadatas[i])

                chunks.extend(doc_chunks)

            except Exception as e:
                logger.warning(f"分块失败 (text {i}): {e}")
                failed += 1

        # 存储
        if chunks:
            self.store.add_chunks(chunks)
            # 同步到向量存储（如果可用）
            try:
                self.vector_store.add_chunks(chunks)
            except Exception as e:
                logger.warning(f"向量存储同步失败: {e}")

        return IngestReport(
            chunks_created=len(chunks),
            chunks_failed=failed,
            total_tokens=0,
            duration_sec=time.time() - t0,
        )

    def ingest_file(
        self,
        file_path: str | Path,
        *,
        chunk_strategy: str = "auto",
    ) -> IngestReport:
        """摄入单个文件。

        参数：
            file_path: 文件路径
            chunk_strategy: 分块策略

        返回：
            IngestReport
        """
        path = Path(file_path).resolve()
        text = self.text_parser.parse(path)
        return self.ingest(
            texts=[text],
            metadatas=[{"file_path": str(path)}],
            source_name=path.name,
            chunk_strategy=chunk_strategy,
        )

    def ingest_directory(
        self,
        dir_path: str | Path,
        *,
        glob_patterns: Optional[list[str]] = None,
        max_files: int = 100,
    ) -> IngestReport:
        """批量摄入目录下的文件。

        参数：
            dir_path: 目录路径
            glob_patterns: 文件匹配模式（默认 ["**/*.md", "**/*.txt"]）
            max_files: 最大文件数

        返回：
            IngestReport
        """
        dir_path = Path(dir_path).resolve()
        if not glob_patterns:
            glob_patterns = ["**/*.md", "**/*.txt", "**/*.py", "**/*.js", "**/*.ts"]

        files = []
        for pattern in glob_patterns:
            files.extend(dir_path.glob(pattern))
        files = sorted(set(files))[:max_files]

        logger.info(f"摄入目录 {dir_path}，匹配到 {len(files)} 个文件")

        total_report = IngestReport()
        for file_path in files:
            try:
                report = self.ingest_file(file_path)
                total_report.merge(report)
            except Exception as e:
                logger.warning(f"摄入文件失败 {file_path}: {e}")
                total_report.chunks_failed += 1

        return total_report

    # ------------------------------------------------------------------
    # 检索管线
    # ------------------------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        top_k: int = 5,
        use_rewrite: bool = True,
        use_rerank: bool = True,
        use_vector: bool = True,
    ) -> list[RetrievalResult]:
        """检索相关知识。

        参数：
            query: 查询字符串
            top_k: 返回结果数量
            use_rewrite: 是否使用查询改写
            use_rerank: 是否使用精排
            use_vector: 是否使用向量检索

        返回：
            RetrievalResult 列表
        """
        # 1. 查询改写
        queries = [query]
        if use_rewrite:
            try:
                queries = self.rewriter.rewrite(query)
            except Exception as e:
                logger.warning(f"查询改写失败: {e}")

        # 2. 多路检索
        all_results: dict[str, RetrievalResult] = {}

        # 2.1 BM25 全文检索
        for q in queries:
            bm25_results = self.store.search(q, top_k=top_k * 2)
            for r in bm25_results:
                key = f"{r.chunk.doc_id}:{r.chunk.position}"
                if key not in all_results or r.score > all_results[key].score:
                    all_results[key] = r

        # 2.2 向量检索（如果可用）
        if use_vector:
            try:
                for q in queries:
                    vector_results = self.vector_store.search(q, top_k=top_k)
                    for r in vector_results:
                        key = f"{r.chunk.doc_id}:{r.chunk.chunk_index}"
                        if key not in all_results:
                            all_results[key] = r
            except Exception as e:
                logger.warning(f"向量检索失败: {e}")

        # 3. 精排
        results = list(all_results.values())
        if use_rerank and len(results) > 1:
            try:
                results = self.reranker.rerank(query, results)
            except Exception as e:
                logger.warning(f"精排失败: {e}")
                results = sorted(results, key=lambda r: r.score, reverse=True)

        # 4. 返回 top_k
        return results[:top_k]

    def retrieve_stream(
        self,
        query: str,
        *,
        top_k: int = 5,
    ):
        """流式检索（yield 中间状态）。"""
        yield {"stage": "rewriting", "query": query}
        queries = self.rewriter.rewrite(query)
        yield {"stage": "retrieving", "queries": queries}

        results = self.retrieve(query, top_k=top_k)
        yield {"stage": "done", "results": results}

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _auto_detect_strategy(self, text: str) -> str:
        """自动检测分块策略。"""
        if "# " in text and "## " in text:
            return "markdown"
        if "def " in text or "class " in text or "import " in text:
            return "code"
        return "text"

    def clear(self) -> None:
        """清空知识库。"""
        self.store.clear()
        try:
            self.vector_store.clear()
        except Exception:
            pass

    def get_stats(self) -> dict:
        """获取知识库统计信息。"""
        return self.store.get_stats()


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------

def create_pipeline(workspace: str | Path, config: Optional[dict] = None) -> KnowledgePipeline:
    """创建知识库检索管线。"""
    return KnowledgePipeline(workspace, config)
