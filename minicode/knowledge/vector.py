"""【可选】向量 embedding 后端。

仅在安装了 ``[rag-vector]`` extras（提供 ``sqlite-vec``）且在 settings 里
``knowledge.embedding.enabled = true`` 时启用。未安装时所有入口透明降级，
调用方（pipeline）会自动回退到纯 BM25。

embedding 通过外部 API（OpenAI / voyage）计算，**不在本地下载模型**。
测试用 ``MockEmbeddingProvider`` 无需网络。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import urllib.request
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from minicode.knowledge.store import KnowledgeStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 依赖探测
# ---------------------------------------------------------------------------

def is_available() -> bool:
    """sqlite-vec 是否可用（决定能否启用向量检索）。"""
    try:
        import sqlite_vec  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------------------
# Embedding Provider 抽象
# ---------------------------------------------------------------------------

class EmbeddingProvider(ABC):
    """embedding 后端抽象基类。"""

    dimension: int = 0
    model: str = ""

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """把一批文本编码成向量。"""
        raise NotImplementedError

    def embed(self, text: str) -> list[float]:
        """编码单条文本。"""
        out = self.embed_batch([text])
        return out[0] if out else []


class MockEmbeddingProvider(EmbeddingProvider):
    """测试用：基于 hash 的确定性伪向量，无需网络。"""

    def __init__(self, dimension: int = 64, model: str = "mock") -> None:
        self.dimension = dimension
        self.model = model

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dimension
            # 基于 token hash 填充，语义相近文本会有相近向量分量
            for tok in text.lower().split():
                h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
                idx = h % self.dimension
                vec[idx] += 1.0
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            vectors.append([v / norm for v in vec])
        return vectors


class OpenAIEmbeddingProvider(EmbeddingProvider):
    """调用 OpenAI ``/v1/embeddings``（text-embedding-3-small 等）。"""

    def __init__(
        self,
        model: str = "text-embedding-3-small",
        dimension: int = 1536,
        api_key_env: str = "OPENAI_API_KEY",
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self.model = model
        self.dimension = dimension
        self._api_key = os.environ.get(api_key_env, "")
        self._base_url = base_url.rstrip("/")
        if not self._api_key:
            raise RuntimeError(
                f"Embedding API key not found in env var {api_key_env}. "
                "Set it or disable knowledge.embedding."
            )

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=payload,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        items = sorted(data.get("data", []), key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in items]


def create_provider(emb_cfg: dict[str, Any]) -> EmbeddingProvider:
    """根据配置创建 embedding provider。"""
    provider = str(emb_cfg.get("provider", "openai")).lower()
    dimension = int(emb_cfg.get("dimension", 1536))
    model = str(emb_cfg.get("model", "text-embedding-3-small"))

    if provider == "mock":
        return MockEmbeddingProvider(dimension=dimension, model=model)
    if provider in {"openai", "custom"}:
        return OpenAIEmbeddingProvider(
            model=model,
            dimension=dimension,
            api_key_env=str(emb_cfg.get("api_key_env", "OPENAI_API_KEY")),
            base_url=str(emb_cfg.get("base_url", "https://api.openai.com/v1")),
        )
    raise RuntimeError(f"Unknown embedding provider: {provider}")


# ---------------------------------------------------------------------------
# 向量索引构建
# ---------------------------------------------------------------------------

def build_index(
    store: KnowledgeStore,
    emb_cfg: dict[str, Any],
    idx_dir: Path,
) -> int:
    """为 store 中所有 chunk 计算向量并写入 sqlite-vec。

    参数：
        store: 已构建 BM25 索引的 KnowledgeStore
        emb_cfg: embedding 配置段
        idx_dir: 索引目录（向量库 vectors.db 放这里）

    返回：
        写入的向量数量。
    """
    from minicode.knowledge.store import VectorStore

    provider = create_provider(emb_cfg)
    chunks = store.all_chunks()
    if not chunks:
        return 0

    vstore = VectorStore(idx_dir / "vectors.db", dimension=provider.dimension)
    try:
        # 批量编码
        batch_size = 64
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            vectors = provider.embed_batch([c.text for c in batch])
            vstore.upsert_vectors(
                [(c.id, vec) for c, vec in zip(batch, vectors)]
            )
        # 记录 meta，避免维度不匹配
        _write_meta(idx_dir, provider)
        return len(chunks)
    finally:
        vstore.close()


def _write_meta(idx_dir: Path, provider: EmbeddingProvider) -> None:
    meta = {
        "provider": provider.__class__.__name__,
        "model": provider.model,
        "dimension": provider.dimension,
    }
    (idx_dir / "meta.json").write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def embed_query(emb_cfg: dict[str, Any], text: str) -> list[float]:
    """编码查询文本（供 hybrid 检索使用）。"""
    provider = create_provider(emb_cfg)
    return provider.embed(text)
