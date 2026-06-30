"""知识库检索工具 (M5)。

允许 agent 主动检索外部知识库（RAG），获取项目文档、README、设计文档等。
"""

from __future__ import annotations

from minicode.tooling import ToolDefinition, ToolResult


def _validate(input_data: dict) -> dict:
    """验证输入参数。"""
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be a non-empty string")

    top_k = int(input_data.get("top_k", 5))
    if top_k < 1 or top_k > 20:
        raise ValueError("top_k must be between 1 and 20")

    return {"query": query.strip(), "top_k": top_k}


def _run(input_data: dict, context: dict) -> ToolResult:
    """执行知识库检索。"""
    query = input_data["query"]
    top_k = input_data["top_k"]
    cwd = context.get("cwd", ".")

    try:
        from pathlib import Path
        from minicode.knowledge.pipeline import create_pipeline

        pipeline = create_pipeline(cwd)
        results = pipeline.retrieve(query, top_k=top_k)

        if not results:
            return ToolResult(
                output=f"知识库中未找到与「{query}」相关的内容。",
                metadata={"query": query, "results_count": 0},
            )

        # 格式化结果
        lines = [f"## 知识库检索结果：{query}\n"]
        for i, r in enumerate(results, 1):
            chunk = r.chunk
            lines.append(f"### 结果 {i} (相似度: {r.score:.3f})")
            lines.append(f"来源: {chunk.source_path}")
            lines.append(f"\n{chunk.text}\n")

        return ToolResult(
            output="\n".join(lines),
            metadata={
                "query": query,
                "results_count": len(results),
                "top_score": results[0].score if results else 0.0,
            },
        )

    except Exception as e:
        return ToolResult(
            output=f"检索失败: {e}",
            error=str(e),
        )


# ---------------------------------------------------------------------------
# ToolDefinition
# ---------------------------------------------------------------------------

retrieve_knowledge_tool = ToolDefinition(
    name="retrieve_knowledge",
    description=(
        "从知识库中检索相关知识。当用户询问项目文档、设计思路、API 用法、"
        "历史决策等内容时，使用此工具从已摄入的知识库中检索相关上下文。"
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "检索查询（用自然语言描述需要查找的内容）",
            },
            "top_k": {
                "type": "integer",
                "description": "返回结果数量（默认 5）",
                "default": 5,
            },
        },
        "required": ["query"],
    },
    validator=_validate,
    run=_run,
)


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = ["retrieve_knowledge_tool"]
