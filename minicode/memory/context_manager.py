"""LLM 对话的上下文窗口管理。

负责跟踪 token 使用、估算上下文窗口占用，
并在长对话中提供自动压缩以避免上下文溢出。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 各模型默认上下文窗口（token 数）
DEFAULT_CONTEXT_WINDOWS = {
    # Anthropic
    "claude-sonnet-4-20250514": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-20240307": 100_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3-mini": 200_000,
    # OpenRouter 上常见模型
    "openrouter/auto": 200_000,
    "anthropic/claude-sonnet-4": 200_000,
    "anthropic/claude-opus-4": 200_000,
    "openai/gpt-4o": 128_000,
    "openai/gpt-4o-mini": 128_000,
    "google/gemini-2.5-pro": 1_000_000,
    "google/gemini-2.5-flash": 1_000_000,
    "meta-llama/llama-4-maverick": 1_000_000,
    "deepseek/deepseek-r1": 128_000,
    "deepseek/deepseek-chat": 128_000,
    "qwen/qwen3-235b-a22b": 128_000,
    "minimax/minimax-m1": 1_000_000,
    "default": 128_000,  # 兜底
}

# 自动压缩触发阈值（上下文窗口的 95%）
AUTOCOMPACT_THRESHOLD = 0.95

# 每字符对应的 token 数（英文/代码的粗略均值）
CHARS_PER_TOKEN = 4.0

# 压缩后至少保留的消息数
MIN_MESSAGES_TO_KEEP = 10

# system prompt 永远保留（计 1 条）
SYSTEM_PROMPT_RESERVED = 1


# ---------------------------------------------------------------------------
# token 估算
# ---------------------------------------------------------------------------

# 预编译的正则表达式用于快速 CJK 字符检测
import re
_CJK_PATTERN = re.compile(r'[\u4E00-\u9FFF\u3040-\u309F\u30A0-\u30FF\uAC00-\uD7AF]')

# LRU 缓存：token 估算被频繁调用（每条消息、每次上下文检查），
# 相同文本的 token 数是确定性的，缓存可避免重复计算。
_token_cache: dict[str, int] = {}
_TOKEN_CACHE_MAX = 1024


def estimate_tokens(text: str) -> int:
    """改进的 token 估算，支持中英文
    
    - 英文/代码：约 4 字符/token
    - 中文/日文：约 1.5 字符/token
    - 混合文本：使用启发式估算
    
    性能优化：使用正则表达式替代逐字符 ord() 检查，速度快 10-50 倍。
    带 LRU 缓存避免重复计算相同文本。
    """
    if not text:
        return 0
    
    # 缓存查找（短文本优先缓存）
    cache_key = text if len(text) < 256 else hash(text)  # 长文本用 hash 作为 key
    if cache_key in _token_cache:
        return _token_cache[cache_key]
    
    # 使用正则表达式快速统计 CJK 字符数量
    cjk_count = len(_CJK_PATTERN.findall(text))
    
    # CJK 字符约 1.5 字符/token，英文约 4 字符/token
    ascii_chars = len(text) - cjk_count
    
    result = max(1, int(cjk_count / 1.5 + ascii_chars / 4.0))
    
    # 缓存结果（防止无限增长）
    if len(_token_cache) < _TOKEN_CACHE_MAX:
        _token_cache[cache_key] = result
    
    return result


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算单条消息消耗的 token 数。"""
    tokens = 0
    
    # role 元数据开销
    role = message.get("role", "")
    if role == "system":
        tokens += 3  # system prompt 额外开销
    elif role == "user":
        tokens += 4  # user 消息额外开销
    elif role == "assistant":
        tokens += 3  # assistant 额外开销
    elif role == "assistant_tool_call":
        tokens += 7  # 工具调用额外开销
    elif role == "tool_result":
        tokens += 6  # 工具结果额外开销
    elif role == "assistant_progress":
        tokens += 3
    
    # 内容部分
    content = message.get("content", "")
    if isinstance(content, str):
        tokens += estimate_tokens(content)
    
    # 工具调用的 input/output
    if "input" in message:
        input_str = json.dumps(message["input"]) if isinstance(message["input"], dict) else str(message["input"])
        tokens += estimate_tokens(input_str)
    
    return tokens


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算一组消息的总 token 数。"""
    return sum(estimate_message_tokens(msg) for msg in messages)


@dataclass
class _ExtractedInfo:
    """压缩过程中从被移除消息里抽取的关键信息。"""
    user_intents: list[str] = field(default_factory=list)
    file_paths: set[str] = field(default_factory=set)
    key_tool_results: list[str] = field(default_factory=list)
    assistant_conclusions: list[str] = field(default_factory=list)
    tool_names: list[str] = field(default_factory=list)
    code_snippets: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)


# 用于分类的工具集合
_EDIT_TOOLS = frozenset({"edit_file", "write_file", "modify_file", "patch_file", "multi_edit"})
_READ_TOOLS = frozenset({"read_file", "list_files", "grep_files", "file_tree"})
_SEARCH_TOOLS = frozenset({"grep_files", "find_symbols", "find_references", "web_search", "web_fetch"})
_COMMAND_TOOLS = frozenset({"run_command", "execute_command", "bash"})

# 抽取代码块和决策性语句的正则
_CODE_FENCE_RE = re.compile(r'```[\w]*\n(.{20,300}?)```', re.DOTALL)
_DECISION_KEYWORDS = re.compile(
    r'(?:decided|decision|chose|chosen|will use|using|switching to|'
    r'implemented|fixed|resolved|refactored|migrated|upgraded|'
    r'recommend|should|must|need to|going to|plan to|'
    r'approach:|strategy:|solution:|conclusion:)',
    re.IGNORECASE,
)


def _extract_from_messages(messages: list[dict[str, Any]]) -> _ExtractedInfo:
    """从被移除消息里抽取分层结构化信息，用于构建分级摘要。

    这是核心抽取阶段：把不同类型的信息按粒度分别抽出，
    供后续的预算感知摘要构建器按重要性优先填入。
    """
    info = _ExtractedInfo()
    
    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "user" and content.strip():
            # 抽取用户意图：短问题完整保留，
            # 长粘贴/超长输入做截断
            preview = content.strip().replace("\n", " ")
            # < 200 字符整段保留；过长则截前 200 字
            if len(preview) > 200:
                preview = preview[:200] + "..."
            info.user_intents.append(preview)
            
        elif role == "assistant" and content.strip():
            text = content.strip()
            
            # 抽取决策/结论性语句
            sentences = text.replace("\n", " ").split(". ")
            for sentence in sentences:
                if _DECISION_KEYWORDS.search(sentence):
                    decision = sentence.strip()[:180]
                    if decision and decision not in info.decisions:
                        info.decisions.append(decision)
            
            # 从 assistant 回答里抽取代码片段
            for match in _CODE_FENCE_RE.finditer(text):
                snippet = match.group(1).strip()
                if len(snippet) >= 20 and len(info.code_snippets) < 5:
                    info.code_snippets.append(snippet[:300])
            
            # 通用结论预览
            preview = text[:200].replace("\n", " ")
            info.assistant_conclusions.append(preview)
            
        elif role == "assistant_tool_call":
            tool_name = msg.get("toolName", "unknown")
            info.tool_names.append(tool_name)
            
            # 编辑/写入类工具：抽取文件路径
            if tool_name in _EDIT_TOOLS:
                inp = msg.get("input", {})
                path = inp.get("path") or inp.get("filePath", "")
                if path:
                    info.file_paths.add(path)
            
            # 搜索类工具：抽取搜索模式
            if tool_name in _SEARCH_TOOLS:
                inp = msg.get("input", {})
                pattern = inp.get("pattern") or inp.get("query", "")
                if pattern:
                    info.file_paths.add(f"search:{pattern[:80]}")
            
            # 命令类工具：抽取命令名
            if tool_name in _COMMAND_TOOLS:
                inp = msg.get("input", {})
                cmd = inp.get("command", "")
                if cmd:
                    cmd_name = cmd.split()[0] if cmd.split() else ""
                    if cmd_name:
                        info.key_tool_results.append(f"ran: {cmd_name}")
            
        elif role == "tool_result":
            tool_name = msg.get("toolName", "")
            is_error = msg.get("isError", False)
            
            # 错误结果优先保留（最高优先级）
            if is_error:
                error_preview = content.strip()[:150].replace("\n", " ")
                info.key_tool_results.append(f"ERROR({tool_name}): {error_preview}")
            
            # 编辑成功结果保留路径信息
            elif tool_name in _EDIT_TOOLS and content.strip():
                success_preview = content.strip()[:100].replace("\n", " ")
                info.key_tool_results.append(f"{tool_name} ok: {success_preview}")
            
            # read_file 类工具：尝试抽取路径
            elif tool_name in _READ_TOOLS and content.strip():
                # 检查首行是否包含文件路径
                first_line = content.strip().split("\n")[0][:100]
                if "/" in first_line or "\\" in first_line:
                    info.file_paths.add(first_line.strip())
    
    return info


def _build_layered_summary(info: _ExtractedInfo, max_summary_tokens: int = 2000) -> str:
    """根据抽取信息按预算分层构建摘要。

    各层按重要性排序，并分配独立的 token 预算：
    - 第 1 层：用户意图（35% 预算）—— 用户想做什么
    - 第 2 层：决策与文件路径（20% 预算）—— 关键选择
    - 第 3 层：关键工具结果（15% 预算）—— 错误及重要产出
    - 第 4 层：assistant 结论（15% 预算）—— 已得到的结果
    - 第 5 层：代码片段（10% 预算）—— 重要代码模式
    - 第 6 层：工具使用汇总（5% 预算）—— 紧凑活动日志
    """
    lines: list[str] = []
    
    # 各层预算占比
    layer_budgets = [0.35, 0.20, 0.15, 0.15, 0.10, 0.05]
    
    def _remaining_budget() -> int:
        return max(0, max_summary_tokens - estimate_tokens("\n".join(lines)))
    
    # 第 1 层：用户意图（最高优先级）
    if info.user_intents:
        budget = int(max_summary_tokens * layer_budgets[0])
        lines.append("## User requests:")
        for intent in info.user_intents[:12]:
            if estimate_tokens("\n".join(lines)) > budget:
                lines.append(f"  ... and {len(info.user_intents) - info.user_intents.index(intent)} more")
                break
            lines.append(f"- {intent}")
    
    # 第 2 层：决策 + 文件路径
    has_decisions = bool(info.decisions)
    has_files = bool(info.file_paths)
    if has_decisions or has_files:
        budget = int(max_summary_tokens * (layer_budgets[0] + layer_budgets[1]))
        
        if info.decisions:
            lines.append("## Key decisions:")
            for dec in info.decisions[:8]:
                if estimate_tokens("\n".join(lines)) > budget:
                    break
                lines.append(f"- {dec}")
        
        if info.file_paths:
            # 区分真实路径和搜索模式
            real_paths = sorted(p for p in info.file_paths if not p.startswith("search:"))
            search_patterns = sorted(p[8:] for p in info.file_paths if p.startswith("search:"))
            
            path_line = f"## Files: {', '.join(real_paths[:20])}"
            if len(real_paths) > 20:
                path_line += f" (+{len(real_paths)-20} more)"
            if search_patterns:
                path_line += f"\n## Searched: {', '.join(search_patterns[:5])}"
            
            if estimate_tokens("\n".join(lines) + path_line) <= budget:
                lines.append(path_line)
    
    # 第 3 层：关键工具结果（错误 + 编辑）
    if info.key_tool_results:
        budget = int(max_summary_tokens * sum(layer_budgets[:3]))
        lines.append("## Key results:")
        for result in info.key_tool_results[:15]:
            if estimate_tokens("\n".join(lines)) > budget:
                break
            lines.append(f"- {result}")
    
    # 第 4 层：assistant 结论
    if info.assistant_conclusions:
        budget = int(max_summary_tokens * sum(layer_budgets[:4]))
        lines.append("## Conclusions:")
        for conc in info.assistant_conclusions[:8]:
            if estimate_tokens("\n".join(lines)) > budget:
                break
            lines.append(f"- {conc}")
    
    # 第 5 层：代码片段（最克制）
    if info.code_snippets:
        budget = int(max_summary_tokens * sum(layer_budgets[:5]))
        lines.append("## Code patterns:")
        for snippet in info.code_snippets[:3]:
            snippet_line = f"```\n{snippet}\n```"
            if estimate_tokens("\n".join(lines) + snippet_line) > budget:
                break
            lines.append(snippet_line)
    
    # 第 6 层：工具使用汇总（最紧凑）
    if info.tool_names:
        from collections import Counter
        tool_counts = Counter(info.tool_names)
        tool_summary = ", ".join(
            f"{name}×{count}" if count > 1 else name
            for name, count in tool_counts.most_common()
        )
        lines.append(f"## Tools: {tool_summary}")
    
    return "\n".join(lines)


def _summarize_removed_messages(messages: list[dict[str, Any]], max_summary_tokens: int = 2000) -> str:
    """对被移除的消息构造紧凑摘要，便于压缩后保留关键上下文。

    采用两阶段方式：
    1. 抽取：从所有类型消息中按层次抽取结构化信息
    2. 构建：按预算分层组装

    这样能确保最重要的信息（用户意图、关键决策）一定会保留，
    其余信息（工具名、代码片段）按余下预算填入。
    """
    if not messages:
        return ""
    
    info = _extract_from_messages(messages)
    return _build_layered_summary(info, max_summary_tokens)


# ---------------------------------------------------------------------------
# 上下文跟踪
# ---------------------------------------------------------------------------

@dataclass
class ContextStats:
    """当前上下文窗口的统计信息。"""
    total_tokens: int = 0
    context_window: int = 0
    usage_percentage: float = 0.0
    messages_count: int = 0
    system_tokens: int = 0
    conversation_tokens: int = 0
    tool_calls_count: int = 0
    is_near_limit: bool = False
    should_compact: bool = False


@dataclass
class ContextManager:
    """统一管理上下文窗口的跟踪与自动压缩。"""
    model: str = "default"
    context_window: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)
    compaction_history: list[dict[str, Any]] = field(default_factory=list)
    _token_cache: dict[int, int] = field(default_factory=dict, repr=False)  # id(msg) -> tokens
    
    # 多级压缩状态
    _compaction_level: int = field(default_factory=lambda: 0)  # 0=未压缩, 1=轻度, 2=中度, 3=深度
    
    # 多级压缩目标（相对于 context window 的占比）
    _COMPACTION_LEVELS = [0.70, 0.50, 0.30]  # 轻度 / 中度 / 深度
    
    def __post_init__(self):
        if self.context_window == 0:
            self.context_window = DEFAULT_CONTEXT_WINDOWS.get(
                self.model, DEFAULT_CONTEXT_WINDOWS["default"]
            )
    
    def update_model(self, model: str) -> None:
        """切换模型并同步刷新 context window。"""
        self.model = model
        self.context_window = DEFAULT_CONTEXT_WINDOWS.get(
            model, DEFAULT_CONTEXT_WINDOWS["default"]
        )
    
    def add_message(self, message: dict[str, Any]) -> None:
        """新增一条消息并更新 token 跟踪。"""
        self.messages.append(message)
        # 立刻缓存 token 数，避免后续 get_stats() 重复估算
        self._token_cache[id(message)] = estimate_message_tokens(message)
    
    def get_stats(self) -> ContextStats:
        """计算当前上下文统计信息。

        命中缓存的消息为 O(1) 摊销开销
        （即通过 add_message 加入的消息）。
        """
        if not self.messages:
            return ContextStats(
                context_window=self.context_window,
            )
        
        # 优先使用缓存计 token
        system_tokens = 0
        conversation_tokens = 0
        tool_calls = 0
        
        for msg in self.messages:
            msg_tokens = self._token_cache.get(id(msg))
            if msg_tokens is None:
                msg_tokens = estimate_message_tokens(msg)
                self._token_cache[id(msg)] = msg_tokens
            if msg.get("role") == "system":
                system_tokens += msg_tokens
            else:
                conversation_tokens += msg_tokens
            
            if msg.get("role") == "assistant_tool_call":
                tool_calls += 1
        
        total_tokens = system_tokens + conversation_tokens
        usage_pct = (total_tokens / self.context_window * 100) if self.context_window > 0 else 0
        
        is_near_limit = usage_pct >= 80  # 80% 时即开始预警
        should_compact = usage_pct >= (AUTOCOMPACT_THRESHOLD * 100)
        
        return ContextStats(
            total_tokens=total_tokens,
            context_window=self.context_window,
            usage_percentage=usage_pct,
            messages_count=len(self.messages),
            system_tokens=system_tokens,
            conversation_tokens=conversation_tokens,
            tool_calls_count=tool_calls,
            is_near_limit=is_near_limit,
            should_compact=should_compact,
        )
    
    def should_auto_compact(self) -> bool:
        """是否应该触发自动压缩。

        多级触发：
        - Level 0：95% 阈值
        - Level 1：85% 阈值
        - Level 2：75% 阈值
        - Level 3：60% 阈值（最激进）
        """
        stats = self.get_stats()
        # 压缩级别越高，阈值越低（越激进）
        threshold = AUTOCOMPACT_THRESHOLD - (self._compaction_level * 0.10)
        threshold = max(0.60, threshold)  # 最低 60%
        usage_pct = stats.usage_percentage
        return usage_pct >= (threshold * 100)
    
    def compact_messages(self) -> list[dict[str, Any]]:
        """压缩消息以适配 context window。

        多级渐进式压缩：
        - Level 0（首次压缩）：目标 70%
        - Level 1（再次压缩）：目标 50%
        - Level 2+（深度压缩）：目标 30%

        采用语义感知的渐进式压缩策略：
        1. system prompt 始终保留
        2. 删除 assistant_progress 消息（价值最低）
        3. 就地截断超大工具结果（按工具类型自适应）
        4. 把 tool_call+result 对压缩成内联摘要
        5. 仍超限时，按优先级删除剩余消息（tool_result > tool_call > assistant > user）

        相比简单按优先级删除的改进：
        - tool_call+result 对会被压缩而不是直接删除，
          保留「调用了什么 → 结果是什么」的语义关联
        - 工具特化压缩：只读类工具用更短摘要，
          编辑类工具保留路径，错误结果保留错误文本
        - 最近消息会被保护，删除从最旧开始
        - 预算感知：每个阶段都会判断是否已达目标
        """
        stats = self.get_stats()
        if not stats.should_compact:
            return self.messages
        
        # 根据压缩级别决定目标
        target_pct = self._COMPACTION_LEVELS[min(self._compaction_level, 2)]
        target_tokens = int(self.context_window * target_pct)
        
        # 永远保留 system prompt
        system_messages = [m for m in self.messages if m.get("role") == "system"]
        other_messages = [m for m in self.messages if m.get("role") != "system"]
        
        # 阶段 1：删除 progress 消息（最低优先级，可放心删除）
        filtered = [
            m for m in other_messages
            if m.get("role") != "assistant_progress"
        ]
        
        current_tokens = estimate_messages_tokens(filtered)
        if current_tokens <= target_tokens:
            return self._finalize_compaction(
                system_messages, other_messages, filtered, stats, target_tokens
            )
        
        # 阶段 2：按工具类型对超长 tool_result 做自适应截断
        # - 只读工具：可激进截断（再跑一次也能拿到）
        # - 编辑工具：保守截断（结果是副作用确认）
        # - 错误结果：尽量保留（错误难以复现）
        _READ_TOOL_TRUNCATE = 1500   # 只读工具结果保留字符数
        _EDIT_TOOL_TRUNCATE = 3000   # 编辑工具结果保留字符数
        _ERROR_TRUNCATE = 4000       # 错误结果保留字符数
        _DEFAULT_TRUNCATE = 2000     # 默认阈值
        
        for i, m in enumerate(filtered):
            if m.get("role") != "tool_result":
                continue
            content = m.get("content", "")
            if not content or len(content) <= _DEFAULT_TRUNCATE:
                continue
            
            tool_name = m.get("toolName", "")
            is_error = m.get("isError", False)
            
            # 根据工具类别选择阈值
            if is_error:
                threshold = _ERROR_TRUNCATE
            elif tool_name in _EDIT_TOOLS:
                threshold = _EDIT_TOOL_TRUNCATE
            elif tool_name in _READ_TOOLS:
                threshold = _READ_TOOL_TRUNCATE
            else:
                threshold = _DEFAULT_TRUNCATE
            
            if len(content) <= threshold:
                continue
            
            # 头尾保留 + 中间省略
            content_lines = content.split("\n")
            # 根据阈值决定头尾各保留多少行
            keep_chars = threshold
            head_lines: list[str] = []
            tail_lines: list[str] = []
            head_chars = 0
            
            for line in content_lines:
                if head_chars + len(line) + 1 > keep_chars * 0.7:
                    break
                head_lines.append(line)
                head_chars += len(line) + 1
            
            # 尾部保留少量行
            tail_chars = 0
            for line in reversed(content_lines):
                if tail_chars + len(line) + 1 > keep_chars * 0.3:
                    break
                tail_lines.insert(0, line)
                tail_chars += len(line) + 1
            
            omitted = len(content_lines) - len(head_lines) - len(tail_lines)
            truncated_content = "\n".join(head_lines)
            if omitted > 0:
                truncated_content += f"\n... [{omitted} lines truncated for compaction] ...\n"
            truncated_content += "\n".join(tail_lines)
            
            filtered[i] = {**m, "content": truncated_content}
        
        current_tokens = estimate_messages_tokens(filtered)
        if current_tokens <= target_tokens:
            return self._finalize_compaction(
                system_messages, other_messages, filtered, stats, target_tokens
            )
        
        # 阶段 3：把 tool_call + result 对压缩成内联摘要
        # 不直接删除，而是替换成紧凑摘要，
        # 保留「调用了什么 → 得到了什么」的语义。
        # 这对编辑操作尤其重要：知道修改了什么文件比保留具体内容更有价值。
        compressed: list[dict[str, Any]] = []
        i = 0
        while i < len(filtered):
            msg = filtered[i]
            
            # 寻找可压缩的 tool_call + tool_result 对
            if (msg.get("role") == "assistant_tool_call" and
                    i + 1 < len(filtered) and
                    filtered[i + 1].get("role") == "tool_result"):
                
                call_msg = msg
                result_msg = filtered[i + 1]
                tool_name = call_msg.get("toolName", "unknown")
                result_content = result_msg.get("content", "")
                is_error = result_msg.get("isError", False)
                
                # 构造一段紧凑摘要，保留关键信息
                summary = self._compress_tool_pair(call_msg, result_msg)
                
                # 用单条压缩消息替代原本的 call+result
                compressed.append({
                    "role": "assistant",
                    "content": summary,
                })
                i += 2  # 同时跳过两条消息
            else:
                compressed.append(msg)
                i += 1
        
        current_tokens = estimate_messages_tokens(compressed)
        if current_tokens <= target_tokens:
            return self._finalize_compaction(
                system_messages, other_messages, compressed, stats, target_tokens
            )
        
        # 阶段 4：按优先级从旧到新删除（保留高优先级消息）
        # 优先级（数字越大越先被删）：
        #   0 = user 消息（最高 —— 携带意图）
        #   1 = assistant 结论（次高 —— 携带结果与已压缩的工具摘要）
        #   2 = 已压缩的工具调用（中 —— 阶段 3 已处理过）
        PRIORITY = {
            "user": 0,                    # 最高 —— 意图
            "assistant": 1,               # 高 —— 结论 + 压缩后的工具摘要
            "assistant_tool_call": 2,     # 中 —— 应已在阶段 3 被压缩
            "tool_result": 3,             # 低 —— 应已在阶段 3 被压缩
        }
        
        # 最近 6 条消息保护起来，不参与删除
        PROTECTED_RECENT = 6
        
        while estimate_messages_tokens(compressed) > target_tokens and len(compressed) > MIN_MESSAGES_TO_KEEP:
            # 在可删除范围内找优先级最低（数字最大）的消息
            removable_end = max(MIN_MESSAGES_TO_KEEP, len(compressed) - PROTECTED_RECENT)
            best_idx = None
            best_priority = -1
            
            for idx in range(removable_end):
                role = compressed[idx].get("role", "")
                priority = PRIORITY.get(role, 1)
                if priority > best_priority:
                    best_priority = priority
                    best_idx = idx
            
            if best_idx is None:
                break
            
            del compressed[best_idx]
        
        return self._finalize_compaction(
            system_messages, other_messages, compressed, stats, target_tokens
        )
    
    @staticmethod
    def _compress_tool_pair(call_msg: dict[str, Any], result_msg: dict[str, Any]) -> str:
        """将 tool_call + tool_result 对压缩为紧凑的内联摘要。

        按工具类型采用不同压缩策略：
        - 编辑工具：保留文件路径和成功/失败状态
        - 只读工具：仅记录文件被读取（内容可重新读取）
        - 搜索工具：保留模式和结果数量
        - 命令工具：保留命令名和退出状态
        - 错误结果：保留错误信息（debug 必需）
        """
        tool_name = call_msg.get("toolName", "unknown")
        inp = call_msg.get("input", {})
        result_content = result_msg.get("content", "")
        is_error = result_msg.get("isError", False)
        
        # Error results: preserve the error message
        if is_error:
            error_text = result_content.strip()[:200].replace("\n", " ")
            return f"[Tool {tool_name} ERROR: {error_text}]"
        
        # Tool-specific compression
        if tool_name in _EDIT_TOOLS:
            path = inp.get("path") or inp.get("filePath", "unknown")
            # Preserve key edit details
            if tool_name == "multi_edit":
                edits = inp.get("edits", [])
                return f"[Edited {path}: {len(edits)} changes applied]"
            return f"[Edited {path}: ok]"
        
        if tool_name in _READ_TOOLS:
            path = inp.get("path") or inp.get("filePath", "")
            if path:
                # Note: content can be re-read, so just record that it was read
                line_count = result_content.count("\n") + 1
                return f"[Read {path}: {line_count} lines]"
            return f"[{tool_name}: completed]"
        
        if tool_name in _SEARCH_TOOLS:
            pattern = inp.get("pattern") or inp.get("query", "")
            # Count matches from result
            match_lines = [l for l in result_content.split("\n") if l.strip() and not l.startswith("#")]
            return f"[Searched '{pattern[:50]}': {len(match_lines)} results]"
        
        if tool_name in _COMMAND_TOOLS:
            cmd = inp.get("command", "")
            cmd_name = cmd.split()[0] if cmd.split() else "command"
            # Check for success indicators
            exit_info = ""
            if "exit code" in result_content.lower():
                for line in result_content.split("\n"):
                    if "exit code" in line.lower():
                        exit_info = f" ({line.strip()[:50]})"
                        break
            return f"[Ran {cmd_name}{exit_info}]"
        
        # Generic compression: tool name + brief result
        brief = result_content.strip()[:100].replace("\n", " ")
        if brief:
            return f"[{tool_name}: {brief}]"
        return f"[{tool_name}: completed]"
    
    def _finalize_compaction(
        self,
        system_messages: list[dict[str, Any]],
        original_other: list[dict[str, Any]],
        filtered: list[dict[str, Any]],
        stats: ContextStats,
        target_tokens: int,
    ) -> list[dict[str, Any]]:
        """组装最终的压缩后消息列表（含摘要标记）。"""
        # 为被移除的消息构造分层摘要
        removed_set = set(id(m) for m in filtered)
        removed_messages = [m for m in original_other if id(m) not in removed_set]
        summary_text = _summarize_removed_messages(removed_messages)
        
        removed_count = len(original_other) - len(filtered)
        after_pct = estimate_messages_tokens(filtered) / self.context_window * 100 if self.context_window > 0 else 0
        
        # 添加压缩标记 + 内容摘要
        compaction_marker = {
            "role": "system",
            "content": (
                f"[Context compacted at {time.strftime('%H:%M:%S')}. "
                f"{removed_count} messages removed. "
                f"Token usage: {stats.usage_percentage:.0f}% → {after_pct:.0f}%]\n"
                + (f"\nSummary of removed conversation:\n{summary_text}" if summary_text else "")
            ),
        }
        
        # 组装最终消息列表
        compacted = system_messages + [compaction_marker] + filtered
        
        # 记录压缩历史
        self.compaction_history.append({
            "timestamp": time.time(),
            "before_tokens": stats.total_tokens,
            "after_tokens": estimate_messages_tokens(compacted),
            "messages_removed": len(self.messages) - len(compacted),
            "compaction_level": self._compaction_level,
        })
        
        # 提升压缩级别（下次更激进）
        self._compaction_level = min(self._compaction_level + 1, 3)
        
        self.messages = compacted
        # 重建 token 缓存：清掉过期项，仅保留留下来的消息
        self._token_cache = {
            id(m): self._token_cache.get(id(m), estimate_message_tokens(m))
            for m in compacted
        }
        return compacted
    
    def get_context_summary(self) -> str:
        """返回人类可读的上下文使用摘要。"""
        stats = self.get_stats()
        
        if stats.messages_count == 0:
            return "Context: empty"
        
        status = "✓"
        if stats.is_near_limit:
            status = "⚠"
        if stats.should_compact:
            status = "🔴"
        
        return (
            f"Context: {status} {stats.usage_percentage:.0f}% "
            f"({stats.total_tokens:,}/{stats.context_window:,} tokens, "
            f"{stats.messages_count} msgs, {stats.tool_calls_count} tools)"
        )
    
    def format_context_details(self) -> str:
        """为 /context 命令格式化详细信息。"""
        stats = self.get_stats()
        
        lines = [
            "Context Window Usage",
            "=" * 50,
            f"Model: {self.model}",
            f"Context window: {stats.context_window:,} tokens",
            "",
            f"Total tokens: {stats.total_tokens:,}",
            f"Usage: {stats.usage_percentage:.1f}%",
            f"Messages: {stats.messages_count}",
            f"Tool calls: {stats.tool_calls_count}",
            "",
        ]
        
        if stats.should_compact:
            lines.append("⚠️  WARNING: Context is near capacity!")
            lines.append("Auto-compaction will trigger soon.")
            lines.append("")
        
        if self.compaction_history:
            lines.append("Compaction History:")
            for comp in self.compaction_history[-3:]:  # 最近 3 次
                ts = time.strftime("%H:%M:%S", time.localtime(comp["timestamp"]))
                lines.append(
                    f"  {ts}: {comp['messages_removed']} messages removed, "
                    f"{comp['before_tokens']:,} → {comp['after_tokens']:,} tokens"
                )
        
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 持久化
# ---------------------------------------------------------------------------

def save_context_state(manager: ContextManager) -> None:
    """将 ContextManager 状态写入磁盘。"""
    state_path = MINI_CODE_DIR / "context_state.json"
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    
    state = {
        "model": manager.model,
        "context_window": manager.context_window,
        "messages": manager.messages,
        "compaction_history": manager.compaction_history[-10:],  # 仅保留最近 10 条
        "_compaction_level": manager._compaction_level,  # 同时持久化压缩级别
    }
    
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_context_state() -> ContextManager | None:
    """从磁盘恢复 ContextManager 状态。"""
    state_path = MINI_CODE_DIR / "context_state.json"
    if not state_path.exists():
        return None
    
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manager = ContextManager(
            model=state.get("model", "default"),
            context_window=state.get("context_window", 0),
            messages=state.get("messages", []),
            compaction_history=state.get("compaction_history", []),
        )
        # 恢复压缩级别
        if "_compaction_level" in state:
            manager._compaction_level = state["_compaction_level"]
        return manager
    except (json.JSONDecodeError, KeyError):
        return None


def clear_context_state() -> None:
    """清空磁盘上保存的上下文状态。"""
    state_path = MINI_CODE_DIR / "context_state.json"
    if state_path.exists():
        state_path.unlink()
