"""knowledge 管线入口：``ingest`` / ``query`` / ``status``。

串联 parsers → chunker → store → 检索，并对外提供三个稳定 API：

- ``ingest(docs_dir, index_name, scope)`` → ``IngestReport``
- ``query(text, index_name, top_k)`` → ``RetrievalResult``
- ``status(index_name, scope)`` → ``dict``

默认零依赖（纯 BM25）。若安装了 ``[rag-vector]`` 且在 settings 里启用 embedding，
``query`` 会自动走 hybrid 检索（BM25 + 向量 RRF 融合），否则透明降级为纯 BM25。
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR, load_effective_settings
from minicode.knowledge import parsers
from minicode.knowledge.bm25 import _expand_query_terms, _tokenize
from minicode.knowledge.cache import diff_documents
from minicode.knowledge.chunker import (
    DEFAULT_MAX_CHUNK_SIZE,
    DEFAULT_OVERLAP,
    chunk_document,
)
from minicode.knowledge.store import KnowledgeStore
from minicode.knowledge.types import IngestReport, RetrievalResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, Any] = {
    "default_index": "default",
    "default_scope": "project",
    "chunker": {"max_chunk_size": DEFAULT_MAX_CHUNK_SIZE, "overlap": DEFAULT_OVERLAP},
    "retrieval": {
        "default_top_k": 5,
        "bm25_top_n_for_rerank": 50,
        "vector_top_n_for_rerank": 50,
        "rrf_k": 60,
    },
    "embedding": {"enabled": False},
}


def load_knowledge_config(cwd: str | Path | None = None) -> dict[str, Any]:
    """读取生效的 ``knowledge`` 配置段（带默认值兜底）。"""
    try:
        effective = load_effective_settings(cwd)
    except Exception:  # noqa: BLE001 - 配置读取失败不应阻塞检索
        effective = {}
    user_cfg = effective.get("knowledge", {}) if isinstance(effective, dict) else {}

    cfg = {**_DEFAULTS}
    for key, value in (user_cfg or {}).items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key] = {**cfg[key], **value}
        else:
            cfg[key] = value
    return cfg


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def index_dir(index_name: str, scope: str, cwd: str | Path | None = None) -> Path:
    """返回某个索引的存储目录。

    - ``scope="project"``：``<cwd>/.mini-code/knowledge/<index_name>/``
    - ``scope="user"``：``~/.mini-code/knowledge/<index_name>/``
    """
    if scope == "user":
        base = MINI_CODE_DIR / "knowledge"
    else:
        base = Path(cwd or Path.cwd()) / ".mini-code" / "knowledge"
    return base / index_name


def index_db_path(index_name: str, scope: str, cwd: str | Path | None = None) -> Path:
    return index_dir(index_name, scope, cwd) / "index.db"


def find_existing_index(
    index_name: str,
    cwd: str | Path | None = None,
) -> tuple[str, Path] | None:
    """在 project / user 两个 scope 中查找已存在的索引。

    返回 ``(scope, db_path)``，找不到返回 ``None``。project 优先。
    """
    for scope in ("project", "user"):
        db = index_db_path(index_name, scope, cwd)
        if db.exists():
            return scope, db
    return None


def list_indexes(cwd: str | Path | None = None) -> list[dict[str, Any]]:
    """列出 project / user 两个 scope 下所有可用索引。"""
    found: list[dict[str, Any]] = []
    for scope in ("project", "user"):
        base = (
            MINI_CODE_DIR / "knowledge"
            if scope == "user"
            else Path(cwd or Path.cwd()) / ".mini-code" / "knowledge"
        )
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "index.db").exists():
                found.append({"name": child.name, "scope": scope, "path": str(child / "index.db")})
    return found


def has_any_index(cwd: str | Path | None = None) -> bool:
    """工作区或用户级是否存在任何知识库索引。"""
    return bool(list_indexes(cwd))


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# ingest
# ---------------------------------------------------------------------------

def ingest(
    docs_dir: str | Path,
    index_name: str = "default",
    scope: str = "project",
    *,
    cwd: str | Path | None = None,
    incremental: bool = True,
) -> IngestReport:
    """把某个目录的文档索引进指定索引。

    参数：
        docs_dir: 待索引的目录或单文件
        index_name: 索引名（同一 workspace 可有多个）
        scope: ``"project"``（默认）或 ``"user"``
        cwd: 项目根（scope=project 时用于定位 .mini-code 目录）
        incremental: 是否增量（仅重建变化的文档），默认 True

    返回：
        ``IngestReport`` 统计报告。
    """
    cfg = load_knowledge_config(cwd)
    chunk_cfg = cfg.get("chunker", {})
    max_chunk_size = int(chunk_cfg.get("max_chunk_size", DEFAULT_MAX_CHUNK_SIZE))
    overlap = int(chunk_cfg.get("overlap", DEFAULT_OVERLAP))

    db = index_db_path(index_name, scope, cwd)
    report = IngestReport(index_name=index_name, scope=scope, index_path=str(db))

    documents = parsers.iter_documents(docs_dir)
    report.documents_total = len(documents)

    store = KnowledgeStore(db)
    try:
        existing_hashes = store.get_document_hashes() if incremental else {}
        changes = diff_documents(documents, existing_hashes)

        report.documents_skipped = len(changes.unchanged)

        for doc in changes.to_index:
            chash = _content_hash(doc.content)
            try:
                chunks = chunk_document(
                    doc, max_chunk_size=max_chunk_size, overlap=overlap
                )
                store.upsert_document(doc, chunks, chash)
                report.documents_indexed += 1
                report.chunks_total += len(chunks)
            except Exception as e:  # noqa: BLE001
                logger.warning("Failed to index %s: %s", doc.source_path, e)
                report.errors.append(f"{doc.source_path}: {e}")

        # 删除已不存在的文档（仅在遍历整个目录时才安全）
        if incremental and Path(docs_dir).is_dir():
            for old_id in changes.deleted_ids:
                store.delete_document(old_id)
                report.documents_deleted += 1

        # 记录 manifest
        store.set_manifest("chunker", {"max_chunk_size": max_chunk_size, "overlap": overlap})
        store.set_manifest("scope", scope)
        store.set_manifest("docs_dir", str(docs_dir))

        # 可选：向量索引（安装 extras 且启用 embedding 时）
        _maybe_build_vectors(store, cfg, cwd, index_name, scope, report)

    finally:
        store.close()

    return report


def _maybe_build_vectors(
    store: KnowledgeStore,
    cfg: dict[str, Any],
    cwd: str | Path | None,
    index_name: str,
    scope: str,
    report: IngestReport,
) -> None:
    """尝试构建向量索引；未安装 extras 或未启用则静默跳过。"""
    emb_cfg = cfg.get("embedding", {})
    if not emb_cfg.get("enabled"):
        return
    try:
        from minicode.knowledge import vector as vector_mod
    except Exception:  # noqa: BLE001
        return
    if not vector_mod.is_available():
        report.errors.append(
            "embedding.enabled=true but sqlite-vec not installed; "
            "run `pip install minicode-py[rag-vector]`"
        )
        return
    try:
        vector_mod.build_index(store, emb_cfg, index_dir(index_name, scope, cwd))
    except Exception as e:  # noqa: BLE001
        logger.warning("Vector index build failed: %s", e)
        report.errors.append(f"vector: {e}")


# ---------------------------------------------------------------------------
# query
# ---------------------------------------------------------------------------

def query(
    text: str,
    index_name: str = "default",
    top_k: int = 5,
    *,
    scope: str | None = None,
    cwd: str | Path | None = None,
) -> RetrievalResult:
    """检索知识库，返回 top_k 个最相关 chunk。

    检索流程：查询改写 → BM25 召回（可选向量融合）→ rerank 精排。

    参数：
        text: 查询文本
        index_name: 索引名
        top_k: 返回结果数
        scope: 指定 scope；不指定则自动在 project/user 中查找
        cwd: 项目根

    返回：
        ``RetrievalResult``（未找到索引或无命中时返回空结果）。
    """
    cfg = load_knowledge_config(cwd)
    retr_cfg = cfg.get("retrieval", {})
    bm25_top_n = int(retr_cfg.get("bm25_top_n_for_rerank", 50))

    # 定位索引
    if scope is not None:
        db = index_db_path(index_name, scope, cwd)
        resolved_scope = scope
    else:
        found = find_existing_index(index_name, cwd)
        if found is None:
            return RetrievalResult(query=text)
        resolved_scope, db = found

    if not db.exists():
        return RetrievalResult(query=text)

    # 查询改写（术语扩展）
    from minicode.knowledge.query_rewriter import rewrite

    query_variants = rewrite(text)
    query_tokens: list[str] = []
    for variant in query_variants:
        query_tokens.extend(_tokenize(variant))
    query_tokens = _expand_query_terms(query_tokens)

    store = KnowledgeStore(db)
    try:
        bm25_hits = store.bm25_search(query_tokens, top_n=bm25_top_n)

        # 可选 hybrid（向量融合）
        combined = _maybe_hybrid(
            store, cfg, text, bm25_hits, retr_cfg, index_name, resolved_scope, cwd
        )

        # rerank 精排
        from minicode.knowledge.reranker import rerank

        reranked = rerank(text, combined, top_k=top_k)
    finally:
        store.close()

    result = RetrievalResult(query=text)
    for chunk, score in reranked:
        result.chunks.append(chunk)
        result.scores.append(score)
    return result


def _maybe_hybrid(
    store: KnowledgeStore,
    cfg: dict[str, Any],
    text: str,
    bm25_hits: list,
    retr_cfg: dict[str, Any],
    index_name: str,
    scope: str,
    cwd: str | Path | None,
) -> list:
    """若启用向量则做 RRF 融合，否则原样返回 BM25 结果。"""
    emb_cfg = cfg.get("embedding", {})
    if not emb_cfg.get("enabled"):
        return bm25_hits
    try:
        from minicode.knowledge import hybrid as hybrid_mod
    except Exception:  # noqa: BLE001
        return bm25_hits
    try:
        return hybrid_mod.fuse(
            store, emb_cfg, text, bm25_hits, retr_cfg,
            index_dir(index_name, scope, cwd),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Hybrid fusion failed, falling back to BM25: %s", e)
        return bm25_hits


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def status(
    index_name: str = "default",
    *,
    scope: str | None = None,
    cwd: str | Path | None = None,
) -> dict[str, Any]:
    """返回某个索引的状态信息。"""
    if scope is not None:
        db = index_db_path(index_name, scope, cwd)
        resolved_scope = scope
    else:
        found = find_existing_index(index_name, cwd)
        if found is None:
            return {
                "exists": False,
                "index_name": index_name,
                "indexes": list_indexes(cwd),
            }
        resolved_scope, db = found

    if not db.exists():
        return {"exists": False, "index_name": index_name, "indexes": list_indexes(cwd)}

    store = KnowledgeStore(db)
    try:
        info = {
            "exists": True,
            "index_name": index_name,
            "scope": resolved_scope,
            "path": str(db),
            "documents": store.count_documents(),
            "chunks": store.count_chunks(),
            "chunker": store.get_manifest("chunker"),
            "docs_dir": store.get_manifest("docs_dir"),
            "size_bytes": db.stat().st_size if db.exists() else 0,
        }
    finally:
        store.close()
    return info
