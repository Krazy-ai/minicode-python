"""SQLite 索引存储：文档 / chunk / 倒排索引 / manifest。

设计：
- 纯 stdlib ``sqlite3``，零外部依赖。
- WAL 模式，支持多进程/多 agent 并发读写。
- 倒排索引（``inverted_index``）用于快速召回候选 chunk，避免全表 BM25。

向量能力（``VectorStore``）作为可选子模块，仅在安装 ``[rag-vector]`` 且
配置了 embedding 时使用；本模块的 BM25 检索完全不依赖它。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any

from minicode.knowledge.bm25 import _tokenize
from minicode.knowledge.types import Chunk, Document

logger = logging.getLogger(__name__)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    content_hash TEXT,
    mtime REAL,
    mime TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    text TEXT NOT NULL,
    position INTEGER,
    source_path TEXT,
    headings TEXT,
    metadata TEXT,
    length INTEGER
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS inverted_index (
    token TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    tf INTEGER NOT NULL,
    PRIMARY KEY (token, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_inverted_token ON inverted_index(token);

CREATE TABLE IF NOT EXISTS manifest (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


class KnowledgeStore:
    """一个索引对应的 SQLite 存储。"""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        try:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error as e:  # pragma: no cover - platform dependent
            logger.warning("Failed to set PRAGMA for %s: %s", self.db_path, e)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- manifest -----------------------------------------------------------

    def set_manifest(self, key: str, value: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO manifest(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )
            self._conn.commit()

    def get_manifest(self, key: str, default: Any = None) -> Any:
        row = self._conn.execute(
            "SELECT value FROM manifest WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            return default

    # -- documents ----------------------------------------------------------

    def upsert_document(
        self,
        doc: Document,
        chunks: list[Chunk],
        content_hash: str,
    ) -> None:
        """写入/更新一个文档及其全部 chunk（含倒排索引）。

        会先删除该文档已有的 chunk / 倒排项，再重建，保证一致性。
        """
        with self._lock:
            cur = self._conn.cursor()
            # 清理旧数据
            self._delete_document_rows(cur, doc.id)

            cur.execute(
                "INSERT INTO documents(id, source_path, content_hash, mtime, mime, metadata) "
                "VALUES(?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "source_path=excluded.source_path, content_hash=excluded.content_hash, "
                "mtime=excluded.mtime, mime=excluded.mime, metadata=excluded.metadata",
                (
                    doc.id,
                    doc.source_path,
                    content_hash,
                    float(doc.metadata.get("mtime", 0.0)),
                    doc.mime,
                    json.dumps(doc.metadata, ensure_ascii=False),
                ),
            )

            for chunk in chunks:
                tokens = _tokenize(chunk.text)
                cur.execute(
                    "INSERT INTO chunks(id, doc_id, text, position, source_path, headings, metadata, length) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.id,
                        chunk.doc_id,
                        chunk.text,
                        chunk.position,
                        chunk.source_path,
                        json.dumps(chunk.headings, ensure_ascii=False),
                        json.dumps(chunk.metadata, ensure_ascii=False),
                        len(tokens),
                    ),
                )
                tf: dict[str, int] = {}
                for tok in tokens:
                    tf[tok] = tf.get(tok, 0) + 1
                cur.executemany(
                    "INSERT INTO inverted_index(token, chunk_id, tf) VALUES(?, ?, ?) "
                    "ON CONFLICT(token, chunk_id) DO UPDATE SET tf=excluded.tf",
                    [(tok, chunk.id, count) for tok, count in tf.items()],
                )

            self._conn.commit()

    @staticmethod
    def _delete_document_rows(cur: sqlite3.Cursor, doc_id: str) -> None:
        chunk_ids = [
            r["id"] for r in cur.execute(
                "SELECT id FROM chunks WHERE doc_id=?", (doc_id,)
            ).fetchall()
        ]
        if chunk_ids:
            cur.executemany(
                "DELETE FROM inverted_index WHERE chunk_id=?",
                [(cid,) for cid in chunk_ids],
            )
        cur.execute("DELETE FROM chunks WHERE doc_id=?", (doc_id,))
        cur.execute("DELETE FROM documents WHERE id=?", (doc_id,))

    def delete_document(self, doc_id: str) -> None:
        """删除一个文档及其全部 chunk / 倒排项。"""
        with self._lock:
            cur = self._conn.cursor()
            self._delete_document_rows(cur, doc_id)
            self._conn.commit()

    def list_documents(self) -> list[dict[str, Any]]:
        """列出所有文档的元信息。"""
        rows = self._conn.execute(
            "SELECT id, source_path, content_hash, mtime, mime FROM documents"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_document_hashes(self) -> dict[str, str]:
        """返回 ``{doc_id: content_hash}``，用于增量索引比对。"""
        rows = self._conn.execute(
            "SELECT id, content_hash FROM documents"
        ).fetchall()
        return {r["id"]: r["content_hash"] for r in rows}

    def get_document_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT id FROM documents").fetchall()
        return {r["id"] for r in rows}

    # -- chunks -------------------------------------------------------------

    def count_chunks(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
        return int(row["n"]) if row else 0

    def count_documents(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()
        return int(row["n"]) if row else 0

    def _row_to_chunk(self, row: sqlite3.Row) -> Chunk:
        try:
            headings = json.loads(row["headings"]) if row["headings"] else []
        except (json.JSONDecodeError, TypeError):
            headings = []
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except (json.JSONDecodeError, TypeError):
            metadata = {}
        return Chunk(
            id=row["id"],
            doc_id=row["doc_id"],
            text=row["text"],
            position=row["position"] or 0,
            source_path=row["source_path"] or "",
            headings=headings,
            metadata=metadata,
        )

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        row = self._conn.execute(
            "SELECT * FROM chunks WHERE id=?", (chunk_id,)
        ).fetchone()
        return self._row_to_chunk(row) if row else None

    def all_chunks(self) -> list[Chunk]:
        rows = self._conn.execute(
            "SELECT * FROM chunks ORDER BY doc_id, position"
        ).fetchall()
        return [self._row_to_chunk(r) for r in rows]

    # -- retrieval ----------------------------------------------------------

    def _candidate_chunk_ids(self, query_tokens: list[str]) -> set[str]:
        """用倒排索引召回包含任一 query token 的候选 chunk。"""
        candidates: set[str] = set()
        if not query_tokens:
            return candidates
        unique = list(dict.fromkeys(query_tokens))
        # 分批 IN 查询，避免 SQL 变量上限
        batch = 400
        for i in range(0, len(unique), batch):
            part = unique[i:i + batch]
            placeholders = ",".join("?" * len(part))
            rows = self._conn.execute(
                f"SELECT DISTINCT chunk_id FROM inverted_index WHERE token IN ({placeholders})",
                part,
            ).fetchall()
            candidates.update(r["chunk_id"] for r in rows)
        return candidates

    def bm25_search(
        self,
        query_tokens: list[str],
        top_n: int = 50,
    ) -> list[tuple[Chunk, float]]:
        """基于倒排索引 + BM25 的召回。

        参数：
            query_tokens: 已分词（且已扩展）的查询 token
            top_n: 返回候选数上限

        返回：
            ``[(Chunk, score), ...]`` 按分数倒序。
        """
        if not query_tokens:
            return []

        candidate_ids = self._candidate_chunk_ids(query_tokens)
        if not candidate_ids:
            return []

        # 全库统计 avgdl 与 idf（基于 chunk 语料）
        all_lengths = [
            r["length"] for r in self._conn.execute(
                "SELECT length FROM chunks"
            ).fetchall()
        ]
        n_docs = len(all_lengths)
        avgdl = (sum(all_lengths) / n_docs) if n_docs else 0.0
        if avgdl == 0:
            return []

        # idf：对每个 query token 统计 df（有多少 chunk 含该 token）
        unique_tokens = list(dict.fromkeys(query_tokens))
        idf: dict[str, float] = {}
        import math
        for tok in unique_tokens:
            row = self._conn.execute(
                "SELECT COUNT(*) AS df FROM inverted_index WHERE token=?", (tok,)
            ).fetchone()
            df = int(row["df"]) if row else 0
            idf[tok] = math.log((n_docs + 1) / (df + 1)) + 1

        # 拉取候选 chunk 的 token → tf，逐个算 BM25
        scored: list[tuple[Chunk, float]] = []
        cand_list = list(candidate_ids)
        batch = 400
        chunk_rows: dict[str, sqlite3.Row] = {}
        for i in range(0, len(cand_list), batch):
            part = cand_list[i:i + batch]
            placeholders = ",".join("?" * len(part))
            for r in self._conn.execute(
                f"SELECT * FROM chunks WHERE id IN ({placeholders})", part
            ).fetchall():
                chunk_rows[r["id"]] = r

        k1, b = 1.5, 0.75
        for cid in cand_list:
            row = chunk_rows.get(cid)
            if row is None:
                continue
            doc_len = row["length"] or 0
            if doc_len == 0:
                continue
            # 取该 chunk 中 query token 的 tf
            placeholders = ",".join("?" * len(unique_tokens))
            tf_rows = self._conn.execute(
                f"SELECT token, tf FROM inverted_index "
                f"WHERE chunk_id=? AND token IN ({placeholders})",
                [cid, *unique_tokens],
            ).fetchall()
            score = 0.0
            for tr in tf_rows:
                tok = tr["token"]
                raw_tf = tr["tf"]
                tf_norm = raw_tf / doc_len
                numerator = tf_norm * (k1 + 1)
                denominator = tf_norm + k1 * (1 - b + b * (doc_len / avgdl))
                if denominator:
                    score += idf.get(tok, 0.0) * (numerator / denominator)
            if score > 0:
                scored.append((self._row_to_chunk(row), score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]


def get_chunks_for_query(
    store: KnowledgeStore,
    query_tokens: list[str],
    top_n: int = 50,
) -> list[tuple[Chunk, float]]:
    """便捷函数：等价于 ``store.bm25_search``。

    保留为模块级函数以对齐执行计划中的 store API 命名。
    """
    return store.bm25_search(query_tokens, top_n=top_n)


# ---------------------------------------------------------------------------
# 【可选】向量存储（sqlite-vec）
# ---------------------------------------------------------------------------

class VectorStore:
    """基于 sqlite-vec 的向量存储。

    仅在安装了 ``sqlite-vec`` 时可用；未安装时构造会抛 RuntimeError，
    调用方（vector/hybrid）应捕获并降级。
    """

    def __init__(self, db_path: Path | str, dimension: int) -> None:
        try:
            import sqlite_vec
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(
                "sqlite-vec not installed; run `pip install minicode-py[rag-vector]`"
            ) from e

        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.dimension = dimension
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.enable_load_extension(True)
        sqlite_vec.load(self._conn)
        self._conn.enable_load_extension(False)
        self._conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vectors "
            f"USING vec0(chunk_id TEXT PRIMARY KEY, embedding float[{dimension}])"
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    @staticmethod
    def _serialize(vec: list[float]) -> bytes:
        import struct
        return struct.pack(f"{len(vec)}f", *vec)

    def upsert_vectors(self, items: list[tuple[str, list[float]]]) -> None:
        """写入 ``[(chunk_id, embedding), ...]``。"""
        cur = self._conn.cursor()
        for chunk_id, vec in items:
            cur.execute(
                "DELETE FROM chunk_vectors WHERE chunk_id=?", (chunk_id,)
            )
            cur.execute(
                "INSERT INTO chunk_vectors(chunk_id, embedding) VALUES(?, ?)",
                (chunk_id, self._serialize(vec)),
            )
        self._conn.commit()

    def search_vectors(
        self,
        query_vec: list[float],
        top_k: int = 50,
    ) -> list[tuple[str, float]]:
        """KNN 检索，返回 ``[(chunk_id, distance), ...]``（距离升序）。"""
        rows = self._conn.execute(
            "SELECT chunk_id, distance FROM chunk_vectors "
            "WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (self._serialize(query_vec), top_k),
        ).fetchall()
        return [(r["chunk_id"], float(r["distance"])) for r in rows]
