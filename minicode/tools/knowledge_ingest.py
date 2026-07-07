"""agent 工具：构建/更新知识库索引（knowledge_ingest）。

把某个目录（如 ``./docs``）的文档解析、分块并写入 SQLite 索引。
增量：只重建自上次索引以来发生变化的文档。
"""

from __future__ import annotations

from pathlib import Path

from minicode.knowledge import pipeline
from minicode.tooling import ToolContext, ToolDefinition, ToolResult
from minicode.workspace import resolve_tool_path


def _validate(input_data: dict) -> dict:
    docs_dir = input_data.get("docs_dir")
    if not isinstance(docs_dir, str) or not docs_dir.strip():
        raise ValueError("docs_dir is required and must be a non-empty string")
    scope = str(input_data.get("scope") or "project").lower()
    if scope not in {"project", "user"}:
        raise ValueError("scope must be 'project' or 'user'")
    return {
        "docs_dir": docs_dir.strip(),
        "index_name": str(input_data.get("index_name") or "default"),
        "scope": scope,
        "incremental": bool(input_data.get("incremental", True)),
    }


def _run(input_data: dict, context: ToolContext) -> ToolResult:
    docs_dir = input_data.get("docs_dir")
    if not docs_dir:
        return ToolResult(ok=False, output="docs_dir is required")
    scope = str(input_data.get("scope") or "project").lower()
    if scope not in {"project", "user"}:
        scope = "project"
    index_name = str(input_data.get("index_name") or "default")
    incremental = bool(input_data.get("incremental", True))

    # 通过权限门卫解析路径（读意图）
    resolved = resolve_tool_path(context, docs_dir, "read")
    if not Path(resolved).exists():
        return ToolResult(ok=False, output=f"Path does not exist: {docs_dir}")

    report = pipeline.ingest(
        resolved,
        index_name=index_name,
        scope=scope,
        cwd=context.cwd,
        incremental=incremental,
    )

    lines = [report.summary(), f"index db: {report.index_path}"]
    if report.errors:
        lines.append("")
        lines.append("Errors:")
        lines.extend(f"  - {e}" for e in report.errors[:20])
    return ToolResult(ok=True, output="\n".join(lines))


knowledge_ingest_tool = ToolDefinition(
    name="knowledge_ingest",
    description=(
        "Build or update a knowledge base index from a directory of documents "
        "(markdown, text, source code, etc.). Parses, chunks, and stores them in a "
        "local SQLite index for later retrieval via knowledge_query. Incremental: "
        "only changed files are re-indexed."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "docs_dir": {
                "type": "string",
                "description": "Directory (or single file) to index, e.g. './docs'",
            },
            "index_name": {
                "type": "string",
                "description": "Index name to create/update (default: 'default')",
            },
            "scope": {
                "type": "string",
                "enum": ["project", "user"],
                "description": "'project' (<cwd>/.mini-code) or 'user' (~/.mini-code). Default: project",
            },
            "incremental": {
                "type": "boolean",
                "description": "Only re-index changed files (default: true)",
            },
        },
        "required": ["docs_dir"],
    },
    validator=_validate,
    run=_run,
)
