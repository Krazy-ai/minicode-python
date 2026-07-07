"""M1 最小 e2e：ingest + query 基础闭环（纯 BM25，零依赖）。"""

from __future__ import annotations

from pathlib import Path

from minicode.knowledge.pipeline import (
    index_db_path,
    ingest,
    query,
    status,
)


def _make_corpus(root: Path) -> None:
    (root / "auth.md").write_text(
        "# Authentication\n\n"
        "The login flow uses OAuth tokens. Call authenticate() to verify "
        "credentials before granting access.\n\n"
        "## Token Refresh\n\nRefresh tokens expire after 30 days.\n",
        encoding="utf-8",
    )
    (root / "database.md").write_text(
        "# Database\n\nWe use SQLite for local storage. The schema has "
        "documents and chunks tables.\n",
        encoding="utf-8",
    )
    (root / "memory.md").write_text(
        "# Memory System\n\nThe memory subsystem uses BM25 retrieval with three "
        "scopes: user, project and local.\n",
        encoding="utf-8",
    )


def test_ingest_creates_sqlite_index(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_corpus(docs)

    report = ingest(docs, index_name="basic", scope="project", cwd=str(ws))

    assert report.documents_indexed == 3
    assert report.chunks_total >= 3
    db = index_db_path("basic", "project", str(ws))
    assert db.exists()


def test_query_hits_correct_file(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_corpus(docs)
    ingest(docs, index_name="basic", scope="project", cwd=str(ws))

    result = query("how does login authentication work", index_name="basic", cwd=str(ws))
    assert len(result) >= 1
    assert "auth.md" in result.chunks[0].source_path

    result2 = query("SQLite storage schema", index_name="basic", cwd=str(ws))
    assert result2
    assert "database.md" in result2.chunks[0].source_path


def test_status_reports_counts(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    _make_corpus(docs)
    ingest(docs, index_name="basic", scope="project", cwd=str(ws))

    info = status("basic", cwd=str(ws))
    assert info["exists"] is True
    assert info["documents"] == 3
    assert info["chunks"] >= 3


def test_query_missing_index_returns_empty(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    result = query("anything", index_name="nope", cwd=str(ws))
    assert len(result) == 0
    assert not result


def test_chinese_query(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    ws = tmp_path / "ws"
    ws.mkdir()
    (docs / "zh.md").write_text(
        "# 记忆系统\n\n记忆子系统使用 BM25 检索，支持三种作用域：用户、项目、本地。\n",
        encoding="utf-8",
    )
    ingest(docs, index_name="zh", scope="project", cwd=str(ws))
    result = query("记忆系统的作用域", index_name="zh", cwd=str(ws))
    assert result
    assert "zh.md" in result.chunks[0].source_path
