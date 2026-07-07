"""工具系统的核心契约。

提供整个 minicode 工具体系的三件套：
- ToolDefinition：工具描述（名称、入参 schema、validator、执行函数）
- ToolRegistry：工具注册中心，提供 O(1) 查找与统一执行入口
- ToolContext：工具执行所需的上下文（cwd、permissions、runtime）

此外还实现了 **智能截断**（_smart_truncate_output）：根据工具类型对超长输出
进行 head/tail 保留，避免 token 浪费而又不丢关键信息。

注：本模块中 description 字段会被传给 LLM，刻意保留英文以提升 LLM 工具
调用的准确性，因此 ToolMetadata / Tool Protocol 等向 LLM 暴露的描述维持原样。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Protocol
from abc import abstractmethod


# ---------------------------------------------------------------------------
# 智能截断相关常量
# ---------------------------------------------------------------------------

# 默认输出上限（按字符数计），按工具类型可单独覆盖
_DEFAULT_MAX_OUTPUT = 30_000       # 约 8K token，对 context window 安全
_LARGE_OUTPUT_THRESHOLD = 50_000   # 超过该阈值触发智能截断

# 各工具的输出上限（字符数）
_TOOL_OUTPUT_LIMITS: dict[str, int] = {
    "read_file": 40_000,
    "grep_files": 20_000,
    "run_command": 30_000,
    "run_with_debug": 30_000,
    "web_fetch": 20_000,
    "web_search": 15_000,
    "list_files": 15_000,
    "file_tree": 15_000,
    "code_review": 20_000,
    "diff_viewer": 20_000,
    "db_explorer": 20_000,
    "docker_helper": 20_000,
    "test_runner": 25_000,
    "api_tester": 15_000,
}


def _smart_truncate_output(output: str, tool_name: str, max_chars: int | None = None) -> str:
    """对工具的超长输出做智能截断，尽量保留对 agent 最有价值的部分。

    截断策略：
        1. 长度未超限 → 原样返回
        2. read_file → 保留头部 + 尾部（理解文件结构最重要）
        3. run_command → 保留头部 + 错误行 + 尾部
        4. grep / web_search → 保留前 N 个匹配 + 摘要
        5. 其他 → 通用 head + tail
    """
    if not output:
        return output

    limit = max_chars or _TOOL_OUTPUT_LIMITS.get(tool_name, _DEFAULT_MAX_OUTPUT)

    if len(output) <= limit:
        return output

    lines = output.split("\n")
    total_lines = len(lines)
    total_chars = len(output)

    # 估算可保留的最大行数（按平均行长换算）
    avg_line_len = total_chars / max(1, total_lines)
    max_lines = int(limit / max(40, avg_line_len))

    if tool_name == "read_file":
        # 文件读取 —— 头尾都重要，保留头多一些（结构信息更靠前）
        head_lines = max(1, int(max_lines * 0.6))
        tail_lines = max(1, max_lines - head_lines)
        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - head_lines - tail_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...\n\n"
            f"{tail}"
        )

    if tool_name in ("run_command", "run_with_debug"):
        # 命令执行 —— 同时保留头/尾，并把中段的报错/警告行抽出来单独展示
        head_lines = max(1, int(max_lines * 0.4))
        tail_lines = max(1, int(max_lines * 0.4))

        # 从被省略的中段里抽取 error/warning 行
        error_pattern = re.compile(r'(?i)(error|fail|exception|traceback|warning)', re.IGNORECASE)
        error_lines = [
            (i, line) for i, line in enumerate(lines)
            if error_pattern.search(line) and head_lines <= i < total_lines - tail_lines
        ]
        error_text = ""
        if error_lines:
            error_text = "\n\n[Key errors/warnings from omitted section:]\n" + "\n".join(
                f"L{i+1}: {line[:200]}" for i, line in error_lines[:20]
            )

        head = "\n".join(lines[:head_lines])
        tail = "\n".join(lines[-tail_lines:])
        omitted = total_lines - head_lines - tail_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...{error_text}\n\n"
            f"{tail}"
        )

    if tool_name in ("grep_files", "web_search"):
        # 搜索类 —— 只保留前 N 行匹配 + 总数提示
        head = "\n".join(lines[:max_lines])
        omitted = total_lines - max_lines
        return (
            f"{head}\n"
            f"\n... [{omitted} more lines omitted (output too large: {total_chars:,} chars, {total_lines} total lines)] ..."
        )

    # 通用：head + tail 各一半
    head_lines = max(1, int(max_lines * 0.5))
    tail_lines = max(1, max_lines - head_lines)
    head = "\n".join(lines[:head_lines])
    tail = "\n".join(lines[-tail_lines:])
    omitted = total_lines - head_lines - tail_lines
    return (
        f"{head}\n"
        f"\n... [{omitted} lines omitted (output too large: {total_chars:,} chars)] ...\n\n"
        f"{tail}"
    )


# ---------------------------------------------------------------------------
# 工具元数据（参考 Claude Code 的 Tool 类型）
# ---------------------------------------------------------------------------

class ToolCapability(str, Enum):
    """工具能力标记。"""
    READ_ONLY = "read_only"
    DESTRUCTIVE = "destructive"
    CONCURRENCY_SAFE = "concurrency_safe"
    REQUIRES_PERMISSION = "requires_permission"


@dataclass
class ToolMetadata:
    """Tool metadata for classification and discovery.

    Inspired by Claude Code's Tool type definition.
    """
    name: str
    description: str
    capabilities: set[ToolCapability] = field(default_factory=set)
    input_schema: dict[str, Any] = field(default_factory=dict)
    is_enabled: bool = True
    max_result_size_chars: int = 10_000
    tags: list[str] = field(default_factory=list)

    @property
    def is_read_only(self) -> bool:
        """是否为只读工具。"""
        return ToolCapability.READ_ONLY in self.capabilities

    @property
    def is_destructive(self) -> bool:
        """是否会修改/删除数据。"""
        return ToolCapability.DESTRUCTIVE in self.capabilities

    @property
    def is_concurrency_safe(self) -> bool:
        """是否支持并发执行。"""
        return ToolCapability.CONCURRENCY_SAFE in self.capabilities


# ---------------------------------------------------------------------------
# Tool 协议（参考 Claude Code 的 Tool 接口）
# ---------------------------------------------------------------------------

class Tool(Protocol):
    """Tool protocol defining a complete tool lifecycle.

    Inspired by Claude Code's Tool type which includes:
    - call: Execution logic
    - description: Dynamic description generation
    - validate_input: Input validation
    - check_permissions: Permission checking
    - Metadata: is_read_only, is_destructive, etc.
    """

    @property
    def name(self) -> str: ...

    @property
    def description_template(self) -> str: ...

    def get_description(self, args: dict[str, Any], options: dict[str, Any] | None = None) -> str: ...
    def validate_input(self, args: dict[str, Any]) -> tuple[bool, str]: ...
    def check_permissions(self, args: dict[str, Any], context: ToolContext) -> tuple[bool, str]: ...
    def call(
        self,
        args: dict[str, Any],
        context: ToolContext,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> ToolResult: ...
    def is_enabled(self) -> bool: ...
    def is_read_only(self, args: dict[str, Any]) -> bool: ...
    def is_destructive(self, args: dict[str, Any]) -> bool: ...


@dataclass(slots=True)
class BackgroundTaskResult:
    """后台任务（fork 出去的 shell 进程）的描述。"""
    taskId: str
    type: str
    command: str
    pid: int
    status: str
    startedAt: int


@dataclass(slots=True)
class ToolResult:
    """工具执行结果。

    Attributes:
        ok: 是否成功
        output: 输出文本（会经过智能截断）
        backgroundTask: 若工具启动了后台进程则填入
        awaitUser: 是否需要等待用户介入（如 ask_user 工具）
    """
    ok: bool
    output: str
    backgroundTask: BackgroundTaskResult | None = None
    awaitUser: bool = False


@dataclass(slots=True)
class ToolContext:
    """工具执行上下文。

    cwd 是工作目录；permissions 指向 PermissionManager（可为 None 表示
    headless 等无审批环境）；_runtime 是 runtime 配置字典。
    """
    cwd: str
    permissions: Any | None = None
    _runtime: dict | None = None


Validator = Callable[[Any], Any]
Runner = Callable[[Any, ToolContext], ToolResult]


@dataclass(slots=True)
class ToolDefinition:
    """完整的工具定义。

    description / input_schema 会传给 LLM 用于生成 tool call，因此**保留英文**
    以确保模型理解准确。
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    validator: Validator
    run: Runner
    metadata: ToolMetadata | None = None

    @property
    def is_read_only(self) -> bool:
        """是否为只读工具（可并发执行）。"""
        if self.metadata:
            return self.metadata.is_read_only
        # 兜底：根据工具名判断
        return self.name in _READ_ONLY_TOOL_NAMES

    @property
    def is_concurrency_safe(self) -> bool:
        """是否支持并发执行。"""
        if self.metadata:
            return self.metadata.is_concurrency_safe or self.metadata.is_read_only
        return self.is_read_only


# 经验法则：已知为只读的工具名集合
_READ_ONLY_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "list_files", "grep_files", "file_tree",
    "find_symbols", "find_references", "get_ast_info",
    "code_review", "diff_viewer", "db_explorer",
    "web_fetch", "web_search", "api_tester",
    "ask_user", "todo_write",
    "knowledge_query", "knowledge_status",
})


class ToolRegistry:
    """工具注册中心。

    维护工具列表、关联的 skills、MCP 服务器；提供 O(1) 工具查找
    （内部用 dict 索引）以及统一的 execute() 入口。
    """

    def __init__(
        self,
        tools: list[ToolDefinition],
        skills: list[dict[str, Any]] | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
        disposer: Callable[[], Any] | None = None,
    ) -> None:
        self._tools = tools
        self._skills = skills or []
        self._mcp_servers = mcp_servers or []
        self._disposer = disposer
        # 工具查找缓存 - O(1) 查找代替 O(n) 遍历
        self._tool_index: dict[str, ToolDefinition] = {t.name: t for t in tools}

    def list(self) -> list[ToolDefinition]:
        """列出所有已注册工具。"""
        return list(self._tools)

    def get_skills(self) -> list[dict[str, Any]]:
        """返回关联的 skills 元数据。"""
        return list(self._skills)

    def get_mcp_servers(self) -> list[dict[str, Any]]:
        """返回关联的 MCP 服务器列表。"""
        return list(self._mcp_servers)

    def find(self, name: str) -> ToolDefinition | None:
        """按名查找工具（O(1)）。找不到返回 None。"""
        return self._tool_index.get(name)

    def execute(self, tool_name: str, input_data: Any, context: ToolContext) -> ToolResult:
        """执行工具，并提供完整的异常防护。

        全局异常捕获网会拦截 **所有**异常（除 KeyboardInterrupt / SystemExit），
        把它们转成 error ToolResult，避免单个工具崩溃拖垮整个会话。

        防护层级：
            1. 工具不存在        → error result
            2. 输入校验失败      → error result + 入参摘要
            3. 执行抛异常        → error result + traceback 节选
            4. 输出过大          → 智能截断
            5. 其它意外错误      → error result（绝不向上抛）
        """
        tool = self.find(tool_name)
        if tool is None:
            return ToolResult(ok=False, output=f"Unknown tool: {tool_name}")

        try:
            # 阶段 1：入参校验（带错误上下文）
            try:
                parsed = tool.validator(input_data)
            except (ValueError, TypeError, KeyError) as ve:
                return ToolResult(
                    ok=False,
                    output=f"Input validation error in {tool_name}: {ve}\n"
                           f"Input was: {str(input_data)[:200]}"
                )

            # 阶段 2：执行（带崩溃防护）
            result = tool.run(parsed, context)

            # 阶段 3：输出清洗
            if result.output is None:
                result.output = ""

            # 大输出走智能截断
            if result.output and len(result.output) > _LARGE_OUTPUT_THRESHOLD:
                result.output = _smart_truncate_output(result.output, tool_name)

            return result

        except (KeyboardInterrupt, SystemExit):
            # 这两类异常必须向上传播，不能吞
            raise
        except Exception as error:  # noqa: BLE001
            # 全局兜底：把任何未处理异常转成 error result
            # 防止单个有 bug 的工具搞崩整个会话
            import traceback
            tb_lines = traceback.format_exception(type(error), error, error.__traceback__)
            # 只保留 traceback 最后 5 行用于排查
            tb_excerpt = "".join(tb_lines[-5:]).strip()
            error_type = type(error).__name__

            return ToolResult(
                ok=False,
                output=f"[{error_type}] Tool {tool_name} crashed: {error}\n"
                       f"Traceback (most recent):\n{tb_excerpt}"
            )

    def dispose(self) -> None:
        """释放资源（如关闭 MCP 子进程等）。"""
        if self._disposer is not None:
            self._disposer()
