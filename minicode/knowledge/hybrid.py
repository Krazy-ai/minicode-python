"""【可选】Hybrid 检索：BM25 + 向量的 Reciprocal Rank Fusion (RRF)。

仅在启用向量增强时由 ``pipeline._maybe_hybrid`` 调用。RRF 公式：

    rrf_score(chunk) = sum over methods of 1 / (k + rank_in_method)

用排名而非原始分数融合，天然消除两路分数量纲差异。未装 sqlite-vec 或
向量库不存在时抛异常，调用方会回退到纯 BM25。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from minicode.knowledge.store import KnowledgeStore
from minicode.knowledge.types import Chunk

logger = logging.getLogger(__name__)


def _rrf(
    rankings: list[list[str]],
    k: int = 60,
) -> dict[str, float]:
    """对多路排名列表做 RRF 融合，返回 ``{chunk_id: score}``。"""
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, chunk_id in enumerate(ranking, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return scores


def fuse(
    store: KnowledgeStore,
    emb_cfg: dict[str, Any],
    query_text: str,
    bm25_hits: list[tuple[Chunk, float]],
    retr_cfg: dict[str, Any],
    idx_dir: Path,
) -> list[tuple[Chunk, float]]:
    """融合 BM25 与向量两路召回。

    参数：
        store: KnowledgeStore（用于按 id 取回 chunk）
        emb_cfg: embedding 配置
        query_text: 原始查询
        bm25_hits: BM25 召回 ``[(Chunk, score), ...]``
        retr_cfg: retrieval 配置（rrf_k / vector_top_n_for_rerank）
        idx_dir: 索引目录（内含 vectors.db）

    返回：
        融合后的 ``[(Chunk, rrf_score), ...]``，按分数倒序。

    Raises:
        任何异常都表示应降级到纯 BM25（由调用方捕获）。
    """
    from minicode.knowledge import vector as vector_mod
    from minicode.knowledge.store import VectorStore

    vectors_db = idx_dir / "vectors.db"
    if not vectors_db.exists():
        raise FileNotFoundError("vectors.db not found; vector index not built")

    dimension = int(emb_cfg.get("dimension", 1536))
    vec_top_n = int(retr_cfg.get("vector_top_n_for_rerank", 50))
    rrf_k = int(retr_cfg.get("rrf_k", 60))

    # 向量召回
    query_vec = vector_mod.embed_query(emb_cfg, query_text)
    vstore = VectorStore(vectors_db, dimension=dimension)
    try:
        vec_results = vstore.search_vectors(query_vec, top_k=vec_top_n)
    finally:
        vstore.close()

    bm25_ranking = [c.id for c, _ in bm25_hits]
    vec_ranking = [cid for cid, _ in vec_results]

    fused_scores = _rrf([bm25_ranking, vec_ranking], k=rrf_k)

    # 收集所有涉及的 chunk（BM25 已有对象，向量侧需按 id 取回）
    chunk_map: dict[str, Chunk] = {c.id: c for c, _ in bm25_hits}
    for cid in vec_ranking:
        if cid not in chunk_map:
            chunk = store.get_chunk(cid)
            if chunk is not None:
                chunk_map[cid] = chunk

    combined: list[tuple[Chunk, float]] = []
    for cid, score in fused_scores.items():
        chunk = chunk_map.get(cid)
        if chunk is not None:
            combined.append((chunk, score))

    combined.sort(key=lambda x: x[1], reverse=True)
    return combined
