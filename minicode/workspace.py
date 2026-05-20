"""工作区路径解析与权限门卫。

所有需要访问文件系统的工具，都应通过 resolve_tool_path() 把用户输入的路径
转成绝对路径并通过权限校验，避免越权访问工作区之外的文件。
"""
from __future__ import annotations

from pathlib import Path

from minicode.tooling import ToolContext


def resolve_tool_path(context: ToolContext, input_path: str, intent: str) -> Path:
    """把用户/LLM 给的路径解析成绝对路径并做权限检查。

    流程：
        1. 相对路径基于 context.cwd 拼接
        2. 调用 Path.resolve() 消除符号链接和 ..
        3. 若上下文带 PermissionManager，则交由其判定是否允许
        4. 否则做兜底校验：路径必须位于工作区根目录下，否则抛 PermissionError

    Args:
        context: 工具执行上下文（含 cwd 和 permissions）
        input_path: 待解析的原始路径
        intent: 操作意图（"read" / "write" / "execute" 等），传给权限管理器做决策
    """
    candidate = Path(input_path)
    target = candidate if candidate.is_absolute() else Path(context.cwd) / candidate
    normalized = target.resolve()

    if context.permissions is not None:
        context.permissions.ensure_path_access(str(normalized), intent)
    else:
        # 兜底：当没有权限管理器时，禁止访问工作区根目录之外的路径
        workspace_root = Path(context.cwd).resolve()
        try:
            normalized.relative_to(workspace_root)
        except ValueError:
            raise PermissionError(f"Path escapes workspace: {input_path}")

    return normalized
