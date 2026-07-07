"""agent 工具：查询知识库（knowledge_query）。

只读工具。基于 BM25（+ 可选向量）检索知识库，返回带引用的 chunk 片段，
让 agent 在回答里能标注来源 ``[source: docs/auth.md#section]``。
"""

from __future__ import annotations

from minicode.knowledge import pipeline
from minicode.tooling import (
    ToolCapability,
    ToolContext,
    ToolDefinition,
    ToolMetadata,
    ToolResult,
)


def _validate(input_data: dict) -> dict:
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")
    top_k = input_data.get("top_k", 5)
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(top_k, 20))
    return {
        "query": query.strip(),
        "index_name": str(input_data.get("index_name") or "default"),
        "top_k": top_k,
    }


def _run(input_data: dict, context: ToolContext) -> ToolResult:
    cwd = context.cwd
    index_name = str(input_data.get("index_name") or "default")
    try:
        top_k = int(input_data.get("top_k", 5))
    except (TypeError, ValueError):
        top_k = 5
    top_k = max(1, min(top_k, 20))
    result = pipeline.query(
        input_data["query"],
        index_name=index_name,
        top_k=top_k,
        cwd=cwd,
    )

    if not result:
        indexes = pipeline.list_indexes(cwd)
        if not indexes:
            return ToolResult(
                ok=True,
                output=(
                    "No knowledge base index found. "
                    "Use knowledge_ingest to build one first, "
                    "e.g. index the ./docs directory."
                ),
            )
        available = ", ".join(f"{i['name']}({i['scope']})" for i in indexes)
        return ToolResult(
            ok=True,
            output=(
                f"No results for query in index '{index_name}'. "
                f"Available indexes: {available}"
            ),
        )

    lines = [f"Found {len(result)} relevant chunk(s) for: {input_data['query']}", ""]
    for i, chunk in enumerate(result.chunks, start=1):
        score = result.scores[i - 1] if i - 1 < len(result.scores) else 0.0
        loc = chunk.source_path or chunk.doc_id
        if chunk.heading_path:
            loc = f"{loc}#{chunk.heading_path}"
        lines.append(f"[{i}] (score={score:.3f}) [source: {loc}]")
        lines.append(chunk.text.strip())
        lines.append("")

    lines.append(
        "Cite sources in your answer using the [source: path#section] markers above."
    )
    return ToolResult(ok=True, output="\n".join(lines))


knowledge_query_tool = ToolDefinition(
    name="knowledge_query",
    description=(
        "Search the project knowledge base (indexed documentation, design docs, "
        "code) and return the most relevant text chunks with source citations. "
        "Prefer this over manually reading files when the user asks about project "
        "documentation, design decisions, or unfamiliar concepts. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language question or keywords to search for",
            },
            "index_name": {
                "type": "string",
                "description": "Knowledge base index name (default: 'default')",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of chunks to return (1-20, default: 5)",
                "minimum": 1,
                "maximum": 20,
            },
        },
        "required": ["query"],
    },
    validator=_validate,
    run=_run,
    metadata=ToolMetadata(
        name="knowledge_query",
        description="Search the knowledge base.",
        capabilities={ToolCapability.READ_ONLY, ToolCapability.CONCURRENCY_SAFE},
        tags=["knowledge", "rag", "search"],
    ),
)
