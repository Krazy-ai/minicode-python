"""Agent 智能层：错误分类、恢复策略、推动消息生成、工具调度。

包含四个核心组件：
    - ErrorCategory / RecoveryStrategy：错误类型枚举与恢复策略枚举
    - ErrorClassifier：基于关键词模式识别错误类型，给出推荐策略
    - NudgeGenerator：根据分类结果生成给 LLM 的"推一把"消息（nudge）
    - ToolScheduler：根据历史成功率与并发安全性，把工具调用分成并发批与串行批

注意：本文件中的 ``TEMPLATES`` 字典里所有英文模板是**直接发送给 LLM 的提示**，
影响 agent 后续行为与措辞，**保留英文不翻译**。中文翻译只覆盖代码注释/docstring。
"""
from enum import Enum, auto
from dataclasses import dataclass
from typing import Any


class ErrorCategory(Enum):
    """工具/API 错误的类别枚举，用于驱动恢复策略选择。"""
    NETWORK = "network"
    PERMISSION = "permission"
    RESOURCE = "resource"
    LOGIC = "logic"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


class RecoveryStrategy(Enum):
    """错误恢复策略枚举。"""
    RETRY_EXPONENTIAL_BACKOFF = "retry_exponential_backoff"
    RETRY_IMMEDIATE = "retry_immediate"
    FALLBACK_ALTERNATIVE = "fallback_alternative"
    REQUEST_PERMISSION = "request_permission"
    WAIT_AND_RETRY = "wait_and_retry"
    SKIP_AND_CONTINUE = "skip_and_continue"
    ABORT = "abort"


@dataclass
class ClassifiedError:
    """ErrorClassifier 的输出。

    Attributes:
        category: 推断出的错误类别
        strategy: 推荐的恢复策略
        confidence: 分类置信度（0.0 - 1.0）
        context: 附加上下文（工具名、错误片段等），方便 NudgeGenerator 定制提示
    """
    category: ErrorCategory
    strategy: RecoveryStrategy
    confidence: float  # 0.0 - 1.0
    context: dict[str, Any]


class ErrorClassifier:
    """根据错误消息文本分类错误并推荐恢复策略。"""

    # 各类错误的关键词模式（用于关键词匹配打分）
    PATTERNS = {
        ErrorCategory.NETWORK: [
            "connection", "timeout", "network", "refused", "unreachable",
            "reset", "closed", "dns", "ssl", "certificate",
        ],
        ErrorCategory.PERMISSION: [
            "permission", "access denied", "unauthorized", "forbidden",
            "privilege", "not allowed", "restricted", "admin",
        ],
        ErrorCategory.RESOURCE: [
            "memory", "disk", "space", "resource", "quota", "limit",
            "exceeded", "out of", "no space", "too large",
        ],
        ErrorCategory.TIMEOUT: [
            "timeout", "timed out", "deadline", "expired", "took too long",
        ],
        ErrorCategory.LOGIC: [
            "invalid", "not found", "does not exist", "already exists",
            "bad request", "syntax", "parse", "format", "type error",
        ],
    }

    # 类别 → 推荐策略的默认映射表
    STRATEGY_MAP = {
        ErrorCategory.NETWORK: RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF,
        ErrorCategory.TIMEOUT: RecoveryStrategy.WAIT_AND_RETRY,
        ErrorCategory.PERMISSION: RecoveryStrategy.REQUEST_PERMISSION,
        ErrorCategory.RESOURCE: RecoveryStrategy.WAIT_AND_RETRY,
        ErrorCategory.LOGIC: RecoveryStrategy.FALLBACK_ALTERNATIVE,
        ErrorCategory.UNKNOWN: RecoveryStrategy.RETRY_IMMEDIATE,
    }

    @classmethod
    def classify(cls, error_message: str, tool_name: str = "") -> ClassifiedError:
        """对一条错误消息分类，并推荐恢复策略。

        算法：把消息小写后与各类别的关键词列表做包含计数；选得分最高的类别。
        无任何匹配时归为 UNKNOWN，置信度 0.3。
        """
        error_lower = error_message.lower()

        scores: dict[ErrorCategory, int] = {}
        for category, patterns in cls.PATTERNS.items():
            score = sum(1 for p in patterns if p in error_lower)
            if score > 0:
                scores[category] = score

        if scores:
            best_category = max(scores, key=scores.get)
            confidence = min(0.95, 0.5 + max(scores.values()) * 0.15)
        else:
            best_category = ErrorCategory.UNKNOWN
            confidence = 0.3

        strategy = cls.STRATEGY_MAP.get(best_category, RecoveryStrategy.RETRY_IMMEDIATE)

        # 工具特化：只读工具遇到 LOGIC 错（如文件不存在）直接跳过即可，无需重试
        if tool_name in ["read_file", "list_files", "grep_files"] and best_category == ErrorCategory.LOGIC:
            strategy = RecoveryStrategy.SKIP_AND_CONTINUE

        return ClassifiedError(
            category=best_category,
            strategy=strategy,
            confidence=confidence,
            context={"tool_name": tool_name, "error_snippet": error_message[:200]},
        )


class NudgeGenerator:
    """根据失败上下文为 LLM 生成"推一把"消息（nudge）。

    nudge 文本会作为 user 消息插入对话，提示模型采取下一步行动。所有模板均为
    英文，是为了与 LLM 训练分布对齐、避免模型行为偏移，**不翻译**。
    """

    # 各 (category, strategy) 组合下发给 LLM 的英文模板（保留英文，原文影响模型行为）
    TEMPLATES = {
        ErrorCategory.NETWORK: {
            RecoveryStrategy.RETRY_EXPONENTIAL_BACKOFF: (
                "Network error detected. The previous attempt failed due to connectivity issues. "
                "Please retry the same operation. If it fails again, consider checking your "
                "network connection or trying an alternative approach."
            ),
            RecoveryStrategy.RETRY_IMMEDIATE: (
                "A transient network issue occurred. Please retry the operation immediately."
            ),
        },
        ErrorCategory.PERMISSION: {
            RecoveryStrategy.REQUEST_PERMISSION: (
                "Permission denied. You don't have sufficient privileges for this operation. "
                "Consider: (1) running with elevated permissions if appropriate, "
                "(2) using a different approach that doesn't require elevated access, or "
                "(3) asking the user for permission to proceed."
            ),
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                "Access was denied. Try an alternative approach that works with current permissions."
            ),
        },
        ErrorCategory.RESOURCE: {
            RecoveryStrategy.WAIT_AND_RETRY: (
                "Resource limit reached (memory/disk/quota). Consider: "
                "(1) freeing up resources before retrying, "
                "(2) processing in smaller batches, or "
                "(3) using a more efficient approach."
            ),
        },
        ErrorCategory.TIMEOUT: {
            RecoveryStrategy.WAIT_AND_RETRY: (
                "The operation timed out. This may be due to heavy load or a long-running process. "
                "Consider: (1) retrying after a brief wait, "
                "(2) breaking the task into smaller steps, or "
                "(3) using a more efficient approach."
            ),
        },
        ErrorCategory.LOGIC: {
            RecoveryStrategy.FALLBACK_ALTERNATIVE: (
                "The previous approach encountered an error. Consider using a different strategy: "
                "try alternative tools, adjust parameters, or break the task into smaller steps."
            ),
            RecoveryStrategy.SKIP_AND_CONTINUE: (
                "This step encountered an issue but it's not critical. "
                "You can skip this and continue with the remaining tasks."
            ),
        },
        ErrorCategory.UNKNOWN: {
            RecoveryStrategy.RETRY_IMMEDIATE: (
                "An unexpected error occurred. Please retry the operation. "
                "If the error persists, try a different approach."
            ),
        },
    }

    @classmethod
    def generate(cls, classified_error: ClassifiedError, retry_count: int = 0) -> str:
        """根据分类结果生成 nudge 消息。

        会在基础模板上追加：
            - 当前是第几次重试
            - 工具特定的额外提示（如 ``run_command`` 遇 PERMISSION 提示 sudo 需用户批准）
        """
        category = classified_error.category
        strategy = classified_error.strategy

        # 查模板（找不到则退到 UNKNOWN/RETRY_IMMEDIATE 的兜底）
        category_templates = cls.TEMPLATES.get(category, cls.TEMPLATES[ErrorCategory.UNKNOWN])
        base_message = category_templates.get(
            strategy,
            category_templates.get(RecoveryStrategy.RETRY_IMMEDIATE, "Please retry."),
        )

        # 追加重试次数
        if retry_count > 0:
            base_message += f" (This is retry attempt {retry_count + 1})"

        # 工具特定提示
        tool_name = classified_error.context.get("tool_name", "")
        if tool_name == "run_command" and category == ErrorCategory.PERMISSION:
            base_message += " For command execution, consider using 'sudo' only if explicitly approved by the user."
        elif tool_name in ["write_file", "edit_file"] and category == ErrorCategory.LOGIC:
            base_message += " For file operations, verify the path exists and you have write permissions."

        return base_message

    @classmethod
    def generate_progress_nudge(cls, tool_results: list[tuple[str, bool]]) -> str | None:
        """当模型在工具执行后返回空/进度消息时，根据成败统计推一把。

        Args:
            tool_results: ``[(tool_name, success), ...]`` 列表

        Returns:
            英文 nudge 文本；列表为空时返回 None。
        """
        if not tool_results:
            return None

        success_count = sum(1 for _, ok in tool_results if ok)
        failure_count = len(tool_results) - success_count

        if failure_count == 0:
            return (
                f"All {success_count} tool(s) executed successfully. "
                "Continue with the next concrete step or provide a <final> answer if complete."
            )
        elif failure_count == len(tool_results):
            return (
                f"All {failure_count} tool(s) failed. "
                "Review the errors, adjust your approach, and try again with corrected parameters."
            )
        else:
            return (
                f"{success_count} tool(s) succeeded, {failure_count} failed. "
                "Address the failures first, then continue with remaining tasks."
            )


class ToolScheduler:
    """根据历史性能与并发安全性，智能编排工具调用顺序。

    核心思路：
        - 不安全的工具串行
        - 历史可靠性高的工具优先并发
        - 已知有过冲突的工具对避免再次同时执行
    """

    def __init__(self, metrics_collector: "AgentMetricsCollector | None" = None):
        self._metrics = metrics_collector
        # 记录工具对的冲突次数：frozenset({tool_a, tool_b}) -> 冲突次数
        self._conflict_history: dict[frozenset[str], int] = {}

    def schedule_calls(self, calls: list[dict], tools: Any) -> tuple[list[dict], list[dict]]:
        """把一批工具调用分成"可并发"与"必须串行"两组。

        Returns:
            ``(concurrent_calls, serial_calls)`` 元组。
        """
        if len(calls) <= 1:
            return calls, []

        # 按历史成功率给每个调用打分
        scored_calls: list[tuple[float, dict]] = []
        for call in calls:
            tool_name = call["toolName"]
            score = self._get_tool_score(tool_name)
            scored_calls.append((score, call))

        # 分数高的优先（更可靠的工具优先并发）
        scored_calls.sort(key=lambda x: x[0], reverse=True)

        # 识别会冲突的工具对
        concurrent_calls: list[dict] = []
        serial_calls: list[dict] = []

        for score, call in scored_calls:
            tool_name = call["toolName"]
            tool_def = tools.find(tool_name)

            if not tool_def or not tool_def.is_concurrency_safe:
                serial_calls.append(call)
                continue

            # 与已选的并发工具是否冲突
            conflicts = self._has_conflicts(tool_name, concurrent_calls)
            if conflicts:
                serial_calls.append(call)
            else:
                concurrent_calls.append(call)

        return concurrent_calls, serial_calls

    def _get_tool_score(self, tool_name: str) -> float:
        """取工具的可靠性分数（0.0 - 1.0）。无 metrics 时默认 1.0。"""
        if self._metrics is None:
            return 1.0
        stats = self._metrics.get_tool_stats(tool_name)
        return stats.success_rate

    def _has_conflicts(self, tool_name: str, concurrent_calls: list[dict]) -> bool:
        """判断 tool_name 是否与已选并发组中的某个工具有过冲突历史。"""
        for other_call in concurrent_calls:
            other_name = other_call["toolName"]
            pair = frozenset({tool_name, other_name})
            conflict_count = self._conflict_history.get(pair, 0)
            if conflict_count >= 2:  # 已知冲突的阈值
                return True
        return False

    def record_conflict(self, tool1: str, tool2: str) -> None:
        """记录两个工具在并发执行时发生了冲突，用于后续调度避让。"""
        pair = frozenset({tool1, tool2})
        self._conflict_history[pair] = self._conflict_history.get(pair, 0) + 1

    def get_recommended_max_workers(self, concurrent_calls: list[dict]) -> int:
        """根据并发批的特征推荐线程池大小（1 ~ 8）。

        - 含写文件类工具时上限收紧到 4
        - 含命令执行类工具时进一步收紧到 3
        """
        if not concurrent_calls:
            return 1

        base = min(len(concurrent_calls), 8)

        # 含文件写入类工具 → 限制并发，避免文件锁/竞争
        write_tools = {"write_file", "edit_file", "patch_file", "modify_file"}
        write_count = sum(1 for c in concurrent_calls if c["toolName"] in write_tools)
        if write_count > 0:
            base = min(base, 4)

        # 含命令执行类工具 → 进一步限制
        command_tools = {"run_command", "execute_command", "bash"}
        cmd_count = sum(1 for c in concurrent_calls if c["toolName"] in command_tools)
        if cmd_count > 0:
            base = min(base, 3)

        return max(1, base)
