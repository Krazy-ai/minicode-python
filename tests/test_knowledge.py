"""knowledge 子系统完整测试套件。

覆盖：parsers / chunker / bm25 / query_rewriter / reranker / cache /
store / pipeline / hybrid(mock) / eval / agent tools。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minicode.knowledge import cache, chunker, parsers, pipeline, query_rewriter, reranker
from minicode.knowledge.bm25 import _bm25_score, _compute_idf, _tokenize
from minicode.knowledge.store import KnowledgeStore
from minicode.knowledge.types import Chunk, Document, RetrievalResult
from minicode.tooling import ToolContext
from minicode.tools.knowledge_ingest import knowledge_ingest_tool
from minicode.tools.knowledge_query import knowledge_query_tool
from minicode.tools.knowledge_status import knowledge_status_tool

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "knowledge_corpus"


# ---------------------------------------------------------------------------
# parsers
# ---------------------------------------------------------------------------

class TestParsers:
    def test_parse_markdown(self):
        doc = parsers.parse(FIXTURE_DIR / "authentication.md")
        assert doc is not None
        assert doc.mime == "text/markdown"
        assert "OAuth" in doc.content

    def test_parse_python(self):
        doc = parsers.parse(FIXTURE_DIR / "utils.py")
        assert doc is not None
        assert doc.mime == "text/x-python"

    def test_parse_text(self):
        doc = parsers.parse(FIXTURE_DIR / "notes.txt")
        assert doc is not None
        assert doc.mime == "text/plain"

    def test_unsupported_suffix_returns_none(self, tmp_path):
        p = tmp_path / "image.png"
        p.write_bytes(b"\x89PNG\r\n")
        assert parsers.parse(p) is None

    def test_encoding_fallback_gbk(self, tmp_path):
        p = tmp_path / "gbk.txt"
        p.write_bytes("你好世界，这是中文内容".encode("gbk"))
        doc = parsers.parse(p)
        assert doc is not None
        assert "你好" in doc.content

    def test_large_file_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(parsers, "MAX_FILE_SIZE", 10)
        p = tmp_path / "big.txt"
        p.write_text("x" * 100, encoding="utf-8")
        assert parsers.parse(p) is None

    def test_iter_documents_skips_hidden_dirs(self, tmp_path):
        (tmp_path / "a.md").write_text("# A", encoding="utf-8")
        hidden = tmp_path / ".git"
        hidden.mkdir()
        (hidden / "config.md").write_text("# nope", encoding="utf-8")
        docs = parsers.iter_documents(tmp_path)
        names = {Path(d.source_path).name for d in docs}
        assert "a.md" in names
        assert "config.md" not in names


# ---------------------------------------------------------------------------
# chunker
# ---------------------------------------------------------------------------

class TestChunker:
    def test_markdown_splits_by_heading(self):
        doc = parsers.parse(FIXTURE_DIR / "authentication.md")
        chunks = chunker.chunk_document(doc)
        assert len(chunks) >= 2
        # 标题路径被记录
        all_headings = {h for c in chunks for h in c.headings}
        assert "Authentication" in all_headings

    def test_code_keeps_definitions(self):
        doc = parsers.parse(FIXTURE_DIR / "utils.py")
        chunks = chunker.chunk_document(doc)
        # RateLimiter 类应整体在某个 chunk 内
        joined = {c.text for c in chunks}
        assert any("class RateLimiter" in t and "def allow" in t for t in joined)

    def test_plain_text_chunking(self):
        doc = parsers.parse(FIXTURE_DIR / "notes.txt")
        chunks = chunker.chunk_document(doc)
        assert len(chunks) >= 1
        assert all(c.text.strip() for c in chunks)

    def test_no_text_lost(self):
        doc = Document(
            id="d1", source_path="x.txt",
            content="alpha beta gamma\n\ndelta epsilon\n\nzeta", mime="text/plain",
        )
        chunks = chunker.chunk_document(doc)
        combined = " ".join(c.text for c in chunks)
        for word in ("alpha", "beta", "gamma", "delta", "epsilon", "zeta"):
            assert word in combined

    def test_long_paragraph_uses_sliding_window(self):
        long_text = "word " * 400  # ~2000 chars
        doc = Document(id="d2", source_path="y.txt", content=long_text, mime="text/plain")
        chunks = chunker.chunk_document(doc, max_chunk_size=200, overlap=20)
        assert len(chunks) >= 2

    def test_empty_document(self):
        doc = Document(id="d3", source_path="z.txt", content="   \n  ", mime="text/plain")
        assert chunker.chunk_document(doc) == []


# ---------------------------------------------------------------------------
# bm25
# ---------------------------------------------------------------------------

class TestBM25:
    def test_tokenize_mixed(self):
        tokens = _tokenize("hello 世界 function_name")
        assert "hello" in tokens
        assert "function" in tokens or "function_name" in "".join(tokens)

    def test_bm25_ranks_relevant_higher(self):
        docs = [
            _tokenize("the cat sat on the mat"),
            _tokenize("python programming language tutorial"),
        ]
        idf = _compute_idf(docs)
        avgdl = sum(len(d) for d in docs) / len(docs)
        q = _tokenize("python tutorial")
        s0 = _bm25_score(q, docs[0], idf, avgdl)
        s1 = _bm25_score(q, docs[1], idf, avgdl)
        assert s1 > s0


# ---------------------------------------------------------------------------
# query_rewriter
# ---------------------------------------------------------------------------

class TestQueryRewriter:
    def test_original_query_preserved(self):
        variants = query_rewriter.rewrite("auth flow")
        assert variants[0] == "auth flow"

    def test_abbreviation_expansion(self):
        variants = query_rewriter.rewrite("auth")
        joined = " ".join(variants)
        assert "authentication" in joined

    def test_camel_case_split(self):
        variants = query_rewriter.rewrite("getUserName")
        joined = " ".join(variants)
        assert "get" in joined.lower() and "user" in joined.lower()

    def test_snake_case_split(self):
        variants = query_rewriter.rewrite("get_user_name")
        joined = " ".join(variants).lower()
        assert "user" in joined

    def test_config_abbreviation(self):
        variants = query_rewriter.rewrite("cfg")
        assert any("config" in v for v in variants)

    def test_empty_query(self):
        assert query_rewriter.rewrite("") == []

    def test_no_duplicate_variants(self):
        variants = query_rewriter.rewrite("database")
        assert len(variants) == len(set(variants))


# ---------------------------------------------------------------------------
# reranker
# ---------------------------------------------------------------------------

class TestReranker:
    def _chunk(self, text, headings=None, position=0, mime="text/markdown"):
        return Chunk(
            id=f"c{position}", doc_id="d", text=text, position=position,
            source_path="f.md", headings=headings or [], metadata={"mime": mime},
        )

    def test_returns_top_k(self):
        hits = [(self._chunk(f"text {i}", position=i), 1.0) for i in range(10)]
        out = reranker.rerank("text", hits, top_k=3)
        assert len(out) == 3

    def test_heading_hit_boost(self):
        c_no = self._chunk("some content about widgets", headings=["Misc"])
        c_yes = self._chunk("some content about widgets", headings=["Authentication"])
        hits = [(c_no, 1.0), (c_yes, 1.0)]
        out = reranker.rerank("authentication", hits, top_k=2)
        assert out[0][0] is c_yes

    def test_coverage_boost(self):
        c_low = self._chunk("apples oranges bananas")
        c_high = self._chunk("python testing framework pytest")
        hits = [(c_low, 1.0), (c_high, 1.0)]
        out = reranker.rerank("python pytest testing", hits, top_k=2)
        assert out[0][0] is c_high

    def test_code_query_prefers_code_chunk(self):
        c_md = self._chunk("class definition explanation", mime="text/markdown")
        c_code = self._chunk("class definition explanation", mime="text/x-python")
        hits = [(c_md, 1.0), (c_code, 1.0)]
        out = reranker.rerank("class def function", hits, top_k=2)
        assert out[0][0] is c_code

    def test_empty_hits(self):
        assert reranker.rerank("q", [], top_k=5) == []


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------

class TestCache:
    def _doc(self, doc_id, content):
        return Document(id=doc_id, source_path=f"{doc_id}.md", content=content)

    def test_detects_added(self):
        docs = [self._doc("a", "hello")]
        changes = cache.diff_documents(docs, {})
        assert len(changes.added) == 1
        assert not changes.modified

    def test_detects_modified(self):
        d = self._doc("a", "new content")
        old = {"a": cache.content_hash("old content")}
        changes = cache.diff_documents([d], old)
        assert len(changes.modified) == 1

    def test_detects_unchanged(self):
        d = self._doc("a", "same")
        old = {"a": cache.content_hash("same")}
        changes = cache.diff_documents([d], old)
        assert len(changes.unchanged) == 1
        assert not changes.to_index

    def test_detects_deleted(self):
        changes = cache.diff_documents([], {"gone": "hash"})
        assert changes.deleted_ids == ["gone"]


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

class TestStore:
    def test_crud_and_search(self, tmp_path):
        store = KnowledgeStore(tmp_path / "index.db")
        try:
            doc = Document(id="d1", source_path="a.md", content="python testing guide")
            chunks = [
                Chunk(id="d1#0", doc_id="d1", text="python testing guide with pytest",
                      position=0, source_path="a.md"),
            ]
            store.upsert_document(doc, chunks, "hash1")
            assert store.count_documents() == 1
            assert store.count_chunks() == 1

            hits = store.bm25_search(_tokenize("pytest testing"), top_n=5)
            assert hits
            assert hits[0][0].id == "d1#0"

            store.delete_document("d1")
            assert store.count_documents() == 0
            assert store.count_chunks() == 0
        finally:
            store.close()

    def test_inverted_index_consistency(self, tmp_path):
        store = KnowledgeStore(tmp_path / "index.db")
        try:
            doc = Document(id="d1", source_path="a.md", content="alpha")
            store.upsert_document(
                doc, [Chunk(id="d1#0", doc_id="d1", text="alpha beta", position=0)], "h",
            )
            # 覆盖更新（同 doc_id 重新 upsert 应清理旧倒排项）
            store.upsert_document(
                doc, [Chunk(id="d1#0", doc_id="d1", text="gamma delta", position=0)], "h2",
            )
            hits_old = store.bm25_search(_tokenize("alpha"), top_n=5)
            hits_new = store.bm25_search(_tokenize("gamma"), top_n=5)
            assert not hits_old
            assert hits_new
        finally:
            store.close()

    def test_manifest(self, tmp_path):
        store = KnowledgeStore(tmp_path / "index.db")
        try:
            store.set_manifest("k", {"a": 1})
            assert store.get_manifest("k") == {"a": 1}
            assert store.get_manifest("missing", "default") == "default"
        finally:
            store.close()


# ---------------------------------------------------------------------------
# pipeline end-to-end
# ---------------------------------------------------------------------------

class TestPipeline:
    def test_ingest_query_status(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        report = pipeline.ingest(FIXTURE_DIR, index_name="corpus", scope="project", cwd=str(ws))
        assert report.documents_indexed >= 4
        assert report.chunks_total >= 4

        result = pipeline.query("oauth token authentication login", index_name="corpus", cwd=str(ws))
        assert result
        assert "authentication" in result.chunks[0].source_path.lower()

        info = pipeline.status("corpus", cwd=str(ws))
        assert info["exists"]
        assert info["documents"] >= 4

    def test_incremental_reindex(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        (docs / "a.md").write_text("# A\n\nalpha content", encoding="utf-8")
        (docs / "b.md").write_text("# B\n\nbeta content", encoding="utf-8")

        r1 = pipeline.ingest(docs, index_name="inc", scope="project", cwd=str(ws))
        assert r1.documents_indexed == 2

        # 第二次不改动 → 全部跳过
        r2 = pipeline.ingest(docs, index_name="inc", scope="project", cwd=str(ws))
        assert r2.documents_indexed == 0
        assert r2.documents_skipped == 2

        # 改一个文件 → 只重建该文件
        (docs / "a.md").write_text("# A\n\nalpha content changed", encoding="utf-8")
        r3 = pipeline.ingest(docs, index_name="inc", scope="project", cwd=str(ws))
        assert r3.documents_indexed == 1
        assert r3.documents_skipped == 1

    def test_incremental_delete(self, tmp_path):
        docs = tmp_path / "docs"
        docs.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        (docs / "a.md").write_text("# A\n\nalpha", encoding="utf-8")
        (docs / "b.md").write_text("# B\n\nbeta", encoding="utf-8")
        pipeline.ingest(docs, index_name="del", scope="project", cwd=str(ws))

        (docs / "b.md").unlink()
        r = pipeline.ingest(docs, index_name="del", scope="project", cwd=str(ws))
        assert r.documents_deleted == 1
        info = pipeline.status("del", cwd=str(ws))
        assert info["documents"] == 1


# ---------------------------------------------------------------------------
# hybrid (mock provider)
# ---------------------------------------------------------------------------

class TestHybrid:
    def test_rrf_fusion(self):
        from minicode.knowledge.hybrid import _rrf

        bm25 = ["a", "b", "c"]
        vec = ["c", "d", "a"]
        scores = _rrf([bm25, vec], k=60)
        # a 与 c 同时出现在两路，应比只出现一次的高
        assert scores["a"] > scores["b"]
        assert scores["c"] > scores["d"]

    def test_mock_embedding_provider(self):
        from minicode.knowledge.vector import MockEmbeddingProvider

        provider = MockEmbeddingProvider(dimension=32)
        vecs = provider.embed_batch(["hello world", "hello world"])
        assert len(vecs) == 2
        assert len(vecs[0]) == 32
        assert vecs[0] == vecs[1]  # 确定性

    def test_vector_unavailable_degrades(self, tmp_path):
        # 未装 sqlite-vec 时 pipeline.query 仍应工作（纯 BM25）
        ws = tmp_path / "ws"
        ws.mkdir()
        pipeline.ingest(FIXTURE_DIR, index_name="deg", scope="project", cwd=str(ws))
        result = pipeline.query("database sqlite", index_name="deg", cwd=str(ws))
        assert result


# ---------------------------------------------------------------------------
# eval
# ---------------------------------------------------------------------------

class TestEval:
    def test_evaluate_metrics(self, tmp_path):
        from minicode.knowledge.eval import EvalSample, evaluate

        ws = tmp_path / "ws"
        ws.mkdir()
        pipeline.ingest(FIXTURE_DIR, index_name="ev", scope="project", cwd=str(ws))

        # 找到 authentication 相关 chunk id 作为 ground truth
        result = pipeline.query("oauth authentication login token", index_name="ev", cwd=str(ws))
        assert result
        relevant_id = result.chunks[0].id

        dataset = [
            EvalSample(query="oauth authentication login token", relevant_chunks={relevant_id}),
        ]
        report = evaluate(dataset, index_name="ev", cwd=str(ws))
        assert report.num_samples == 1
        assert report.mrr > 0
        assert report.hit_at[1] == 1.0

    def test_empty_dataset(self):
        from minicode.knowledge.eval import evaluate

        report = evaluate([], index_name="x")
        assert report.num_samples == 0


# ---------------------------------------------------------------------------
# agent tools
# ---------------------------------------------------------------------------

class TestAgentTools:
    def test_ingest_query_status_tools(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        # 把 fixture 拷进工作区，保证在权限允许范围内
        docs = ws / "docs"
        docs.mkdir()
        for f in FIXTURE_DIR.iterdir():
            (docs / f.name).write_text(f.read_text(encoding="utf-8"), encoding="utf-8")

        ctx = ToolContext(cwd=str(ws), permissions=None)

        r_ingest = knowledge_ingest_tool.run(
            {"docs_dir": "docs", "index_name": "default"}, ctx
        )
        assert r_ingest.ok
        assert "indexed" in r_ingest.output

        r_query = knowledge_query_tool.run(
            {"query": "how does authentication work", "top_k": 3}, ctx
        )
        assert r_query.ok
        assert "source:" in r_query.output

        r_status = knowledge_status_tool.run({}, ctx)
        assert r_status.ok
        assert "documents" in r_status.output

    def test_query_without_index(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        ctx = ToolContext(cwd=str(ws), permissions=None)
        r = knowledge_query_tool.run({"query": "anything"}, ctx)
        assert r.ok
        assert "No knowledge base" in r.output or "No results" in r.output

    def test_ingest_validates_scope(self):
        with pytest.raises(ValueError):
            knowledge_ingest_tool.validator({"docs_dir": "x", "scope": "bad"})

    def test_query_validates_empty(self):
        with pytest.raises(ValueError):
            knowledge_query_tool.validator({"query": ""})


# ---------------------------------------------------------------------------
# types
# ---------------------------------------------------------------------------

class TestTypes:
    def test_retrieval_result_citations(self):
        chunks = [
            Chunk(id="c0", doc_id="d", text="some text here", position=0,
                  source_path="a.md", headings=["Sec"]),
        ]
        result = RetrievalResult(query="q", chunks=chunks, scores=[1.5])
        cites = result.citations()
        assert len(cites) == 1
        assert cites[0].source_path == "a.md"
        assert "a.md#Sec" in cites[0].format_ref()

    def test_document_stable_id(self):
        assert Document.make_id("a/b.md") == Document.make_id("a/b.md")
        assert Document.make_id("a.md") != Document.make_id("b.md")
