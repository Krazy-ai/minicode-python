"""agent 工具：查看知识库索引状态（knowledge_status）。只读。"""

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
    return {"index_name": str(input_data.get("index_name") or "default")}


def _run(input_data: dict, context: ToolContext) -> ToolResult:
    cwd = context.cwd
    index_name = str(input_data.get("index_name") or "default")
    info = pipeline.status(index_name, cwd=cwd)

    if not info.get("exists"):
        indexes = info.get("indexes", [])
        if not indexes:
            return ToolResult(
                ok=True,
                output="No knowledge base indexes found. Use knowledge_ingest to create one.",
            )
        lines = ["Available indexes:"]
        lines.extend(
            f"  - {i['name']} (scope={i['scope']}): {i['path']}" for i in indexes
        )
        return ToolResult(ok=True, output="\n".join(lines))

    lines = [
        f"index: {info['index_name']} (scope={info['scope']})",
        f"path: {info['path']}",
        f"documents: {info['documents']}",
        f"chunks: {info['chunks']}",
        f"size: {info['size_bytes'] / 1024:.1f} KB",
    ]
    if info.get("docs_dir"):
        lines.append(f"source dir: {info['docs_dir']}")
    if info.get("chunker"):
        lines.append(f"chunker: {info['chunker']}")

    other = pipeline.list_indexes(cwd)
    if len(other) > 1:
        lines.append("")
        lines.append("Other indexes:")
        lines.extend(
            f"  - {i['name']} (scope={i['scope']})"
            for i in other
            if i["name"] != info["index_name"]
        )
    return ToolResult(ok=True, output="\n".join(lines))


knowledge_status_tool = ToolDefinition(
    name="knowledge_status",
    description=(
        "Show the status of the project knowledge base: which indexes exist, how "
        "many documents/chunks they contain, and their storage location. Read-only."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "index_name": {
                "type": "string",
                "description": "Index name to inspect (default: 'default')",
            },
        },
    },
    validator=_validate,
    run=_run,
    metadata=ToolMetadata(
        name="knowledge_status",
        description="Show knowledge base status.",
        capabilities={ToolCapability.READ_ONLY, ToolCapability.CONCURRENCY_SAFE},
        tags=["knowledge", "rag", "status"],
    ),
)
