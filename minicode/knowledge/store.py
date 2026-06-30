"""基于 SQLite 的知识库索引存储。

负责把文档、分块与倒排索引持久化到单个 ``.db`` 文件，并提供 CRUD 与候选召回
接口。使用 WAL 模式以支持多进程并发读写。

表结构::

    documents(id, source_path, content_hash, mtime, mime, metadata)
    chunks(id, doc_id, text, position, headings, metadata)
    inverted_index(token, chunk_id, tf, PRIMARY KEY(token, chunk_id))
    manifest(key, value)
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

from minicode.knowledge.bm25 import _tokenize
from minicode.knowledge.types import Chunk


class KnowledgeStore:
    """知识库索引的 SQLite 持久层。"""

    def __init__(self, workspace: str | Path, config: dict | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.config = config or {}
        # 数据库路径：<workspace>/.minicode/knowledge.db
        self.db_path = self.workspace / ".minicode" / "knowledge.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- schema ------------------------------------------------------------

    def _init_schema(self) -> None:
        """初始化表结构并开启 WAL。"""
        conn = self._conn
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 某些只读/网络文件系统不支持 WAL，降级为默认模式
            pass
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.executescript(
            """
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
                headings TEXT,
                metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS inverted_index (
                token TEXT,
                chunk_id TEXT,
                tf INTEGER,
                PRIMARY KEY (token, chunk_id)
            );
            CREATE TABLE IF NOT EXISTS manifest (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
            CREATE INDEX IF NOT EXISTS idx_inverted_token ON inverted_index(token);
            CREATE INDEX IF NOT EXISTS idx_inverted_chunk ON inverted_index(chunk_id);
            CREATE INDEX IF NOT EXISTS idx_docs_path ON documents(source_path);
            """
        )
        conn.commit()

    # -- documents ---------------------------------------------------------

    def upsert_document(
        self,
        doc_id: str,
        source_path: str,
        content_hash: str,
        mtime: float,
        mime: str,
        chunks: list[Chunk],
        metadata: dict | None = None,
    ) -> None:
        """写入/更新一个文档及其全部分块与倒排项（先删旧再写新）。"""
        conn = self._conn
        # 先清理该文档的旧分块与倒排项
        self._delete_doc_chunks(doc_id)
        conn.execute(
            "INSERT OR REPLACE INTO documents "
            "(id, source_path, content_hash, mtime, mime, metadata) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                doc_id,
                source_path,
                content_hash,
                mtime,
                mime,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        for chunk in chunks:
            conn.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, doc_id, text, position, headings, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chunk.id,
                    chunk.doc_id,
                    chunk.text,
                    chunk.position,
                    json.dumps(chunk.headings, ensure_ascii=False),
                    json.dumps(chunk.metadata, ensure_ascii=False),
                ),
            )
            # 写倒排索引
            tokens = _tokenize(chunk.text) + _tokenize(" ".join(chunk.headings))
            for token, tf in Counter(tokens).items():
                conn.execute(
                    "INSERT OR REPLACE INTO inverted_index (token, chunk_id, tf) "
                    "VALUES (?, ?, ?)",
                    (token, chunk.id, tf),
                )
        conn.commit()

    def delete_document(self, doc_id: str) -> None:
        """删除一个文档及其分块、倒排项。"""
        self._delete_doc_chunks(doc_id)
        self._conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        self._conn.commit()

    def _delete_doc_chunks(self, doc_id: str) -> None:
        """删除某文档的全部分块及对应倒排项（不提交事务）。"""
        conn = self._conn
        chunk_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT id FROM chunks WHERE doc_id = ?", (doc_id,)
            )
        ]
        for chunk_id in chunk_ids:
            conn.execute(
                "DELETE FROM inverted_index WHERE chunk_id = ?", (chunk_id,)
            )
        conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))

    def list_documents(self) -> list[dict]:
        """列出全部文档的元信息。"""
        rows = self._conn.execute(
            "SELECT id, source_path, content_hash, mtime, mime, metadata "
            "FROM documents ORDER BY source_path"
        ).fetchall()
        out: list[dict] = []
        for row in rows:
            out.append(
                {
                    "id": row["id"],
                    "source_path": row["source_path"],
                    "content_hash": row["content_hash"],
                    "mtime": row["mtime"],
                    "mime": row["mime"],
                    "metadata": json.loads(row["metadata"] or "{}"),
                }
            )
        return out

    def get_document_fingerprints(self) -> dict[str, tuple[str, float]]:
        """返回 ``{source_path: (content_hash, mtime)}``，供增量索引比对。"""
        rows = self._conn.execute(
            "SELECT source_path, content_hash, mtime FROM documents"
        ).fetchall()
        return {
            row["source_path"]: (row["content_hash"], row["mtime"])
            for row in rows
        }

    # -- chunks ------------------------------------------------------------

    def _row_to_chunk(self, row: sqlite3.Row) -> Chunk:
        return Chunk(
            id=row["id"],
            doc_id=row["doc_id"],
            text=row["text"],
            position=row["position"] or 0,
            headings=json.loads(row["headings"] or "[]"),
            metadata=json.loads(row["metadata"] or "{}"),
        )

    def get_chunk(self, chunk_id: str) -> Chunk | None:
        """按 id 取单个 chunk。"""
        row = self._conn.execute(
            "SELECT * FROM chunks WHERE id = ?", (chunk_id,)
        ).fetchone()
        return self._row_to_chunk(row) if row else None

    def get_all_chunks(self) -> list[Chunk]:
        """取出全部 chunk（适用于规模较小的知识库）。"""
        rows = self._conn.execute(
            "SELECT * FROM chunks ORDER BY doc_id, position"
        ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    def get_chunks_for_query(
        self, query: str, candidate_limit: int = 200
    ) -> list[Chunk]:
        """利用倒排索引召回与查询 token 相关的候选 chunk。

        命中的 token 越多、tf 越高的 chunk 排在越前；返回至多 ``candidate_limit``
        个候选，供上层做 BM25 精确打分与 rerank。
        """
        tokens = set(_tokenize(query))
        if not tokens:
            return []
        placeholders = ",".join("?" for _ in tokens)
        rows = self._conn.execute(
            f"SELECT chunk_id, COUNT(DISTINCT token) AS hits, SUM(tf) AS tf_sum "
            f"FROM inverted_index WHERE token IN ({placeholders}) "
            f"GROUP BY chunk_id ORDER BY hits DESC, tf_sum DESC LIMIT ?",
            (*tokens, candidate_limit),
        ).fetchall()
        chunk_ids = [row["chunk_id"] for row in rows]
        return [c for cid in chunk_ids if (c := self.get_chunk(cid)) is not None]

    def count_chunks(self) -> int:
        """统计 chunk 总数。"""
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"]
        )

    def count_documents(self) -> int:
        """统计文档总数。"""
        return int(
            self._conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
        )

    # -- manifest ----------------------------------------------------------

    def set_manifest(self, key: str, value: str) -> None:
        """写入一条 manifest 配置。"""
        self._conn.execute(
            "INSERT OR REPLACE INTO manifest (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get_manifest(self, key: str, default: str | None = None) -> str | None:
        """读取一条 manifest 配置。"""
        row = self._conn.execute(
            "SELECT value FROM manifest WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """关闭数据库连接。"""
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def add_chunks(self, chunks: list[Chunk]) -> None:
        """直接添加 chunks 到存储（按 doc_id 自动分组）。

        参数：
            chunks: Chunk 列表
        """
        # 按 doc_id 分组
        doc_groups: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            if chunk.doc_id not in doc_groups:
                doc_groups[chunk.doc_id] = []
            doc_groups[chunk.doc_id].append(chunk)

        # 对每个文档调用 upsert_document
        for doc_id, doc_chunks in doc_groups.items():
            if not doc_chunks:
                continue

            # 从第一个 chunk 的 metadata 中提取文档信息
            first_chunk = doc_chunks[0]
            source_path = first_chunk.metadata.get("source_path", doc_id)
            mime = first_chunk.metadata.get("mime", "text/plain")
            content_hash = first_chunk.metadata.get("content_hash", "")
            mtime = first_chunk.metadata.get("mtime", 0.0)
            metadata = {
                k: v for k, v in first_chunk.metadata.items()
                if k not in ("source_path", "mime", "content_hash", "mtime")
            }

            self.upsert_document(
                doc_id=doc_id,
                source_path=source_path,
                content_hash=content_hash,
                mtime=mtime,
                mime=mime,
                chunks=doc_chunks,
                metadata=metadata,
            )

    def clear(self) -> None:
        """清空所有数据。"""
        conn = self._conn
        conn.execute("DELETE FROM inverted_index")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM documents")
        conn.execute("DELETE FROM manifest")
        conn.commit()

    def get_stats(self) -> dict:
        """获取存储统计信息。"""
        conn = self._conn
        chunk_count = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        doc_count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

        # 数据库文件大小
        import os
        db_size = 0.0
        if self.db_path.exists():
            db_size = self.db_path.stat().st_size / (1024 * 1024)  # MB

        return {
            "total_chunks": chunk_count,
            "total_docs": doc_count,
            "index_size_mb": round(db_size, 2),
        }

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(self, query: str, *, top_k: int = 5) -> list:
        """BM25 全文检索。

        参数：
            query: 查询字符串
            top_k: 返回结果数量

        返回：
            RetrievalResult 列表
        """
        from minicode.knowledge.bm25 import _tokenize, _compute_idf, _compute_avgdl, _bm25_score
        from minicode.knowledge.types import RetrievalResult

        # 1. 获取候选 chunks
        candidate_chunks = self.get_chunks_for_query(query, candidate_limit=top_k * 10)
        if not candidate_chunks:
            return []

        # 2. 准备 BM25 计算
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []

        # 计算 IDF 和 avgdl
        all_chunk_tokens = [_tokenize(c.text) for c in candidate_chunks]
        idf = _compute_idf(all_chunk_tokens)
        avgdl = _compute_avgdl(all_chunk_tokens)

        # 3. 计算 BM25 分数
        results = []
        for chunk in candidate_chunks:
            chunk_tokens = _tokenize(chunk.text)
            score = _bm25_score(query_tokens, chunk_tokens, idf, avgdl)
            if score > 0:
                results.append(
                    RetrievalResult(
                        chunk=chunk,
                        score=score,
                        source="bm25",
                    )
                )

        # 4. 排序并返回 top_k
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:top_k]
