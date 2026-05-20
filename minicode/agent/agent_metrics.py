"""Agent 度量收集器。

记录每个 agent turn 的执行情况：模型调用次数、工具调用记录（含耗时与成败、
错误类别）、token 消耗等，并维护工具的历史成功率统计供 ToolScheduler
做并发决策时参考。可选地把统计写入 JSON 文件做持久化。
"""
from dataclasses import dataclass, field, asdict
from typing import Any
from enum import Enum
import time
import json
from pathlib import Path


class ErrorCategory(Enum):
    """工具错误的简化分类（与 agent_intelligence 中的同名枚举互不依赖）。"""
    NETWORK = "network"          # 连接失败 / timeout
    PERMISSION = "permission"    # access denied / 鉴权失败
    RESOURCE = "resource"        # 内存 / 磁盘 / quota 不足
    LOGIC = "logic"              # 工具逻辑错误 / 入参非法
    UNKNOWN = "unknown"          # 未分类


@dataclass
class ToolExecutionRecord:
    """单次工具执行的记录。"""
    tool_name: str
    start_time: float
    end_time: float = 0.0
    success: bool = False
    error_category: ErrorCategory = ErrorCategory.UNKNOWN
    error_message: str = ""
    tokens_consumed: int = 0

    @property
    def duration_ms(self) -> float:
        """从 start_time 到 end_time 的毫秒数。"""
        return (self.end_time - self.start_time) * 1000


@dataclass
class AgentTurnMetrics:
    """单个 agent turn 的度量数据。"""
    turn_id: int
    start_time: float
    end_time: float = 0.0
    tool_records: list[ToolExecutionRecord] = field(default_factory=list)
    model_calls: int = 0
    total_tokens: int = 0

    @property
    def duration_ms(self) -> float:
        """整个 turn 的耗时（毫秒）。"""
        return (self.end_time - self.start_time) * 1000

    @property
    def tool_success_rate(self) -> float:
        """本 turn 内的工具成功率（无工具调用时返回 1.0）。"""
        if not self.tool_records:
            return 1.0
        successful = sum(1 for r in self.tool_records if r.success)
        return successful / len(self.tool_records)


@dataclass
class ToolHistoricalStats:
    """单个工具的历史累计统计。"""
    tool_name: str
    total_executions: int = 0
    successful_executions: int = 0
    total_duration_ms: float = 0.0
    error_counts: dict[str, int] = field(default_factory=dict)

    @property
    def success_rate(self) -> float:
        """累计成功率（无执行记录时返回 1.0）。"""
        if self.total_executions == 0:
            return 1.0
        return self.successful_executions / self.total_executions

    @property
    def avg_duration_ms(self) -> float:
        """累计平均耗时（毫秒）。"""
        if self.total_executions == 0:
            return 0.0
        return self.total_duration_ms / self.total_executions


class AgentMetricsCollector:
    """收集并持久化 agent 执行度量。

    使用模式：
        collector.start_turn(turn_id)
        for each tool:
            collector.start_tool(name)
            ... 执行 ...
            collector.end_tool(success, error)
        collector.end_turn(total_tokens)
    """

    def __init__(self, storage_path: Path | None = None):
        self._turns: list[AgentTurnMetrics] = []
        self._tool_stats: dict[str, ToolHistoricalStats] = {}
        self._current_turn: AgentTurnMetrics | None = None
        self._current_tool: ToolExecutionRecord | None = None
        self._storage_path = storage_path
        if storage_path and storage_path.exists():
            self._load()

    def start_turn(self, turn_id: int) -> None:
        """开始记录一个新的 agent turn。"""
        self._current_turn = AgentTurnMetrics(turn_id=turn_id, start_time=time.time())

    def end_turn(self, total_tokens: int = 0) -> AgentTurnMetrics:
        """结束当前 turn，更新历史统计并返回 turn 数据。"""
        if self._current_turn is None:
            raise RuntimeError("No turn in progress")
        self._current_turn.end_time = time.time()
        self._current_turn.total_tokens = total_tokens
        self._turns.append(self._current_turn)

        # 把本 turn 的工具记录累计进历史统计
        for record in self._current_turn.tool_records:
            self._update_tool_stats(record)

        result = self._current_turn
        self._current_turn = None
        self._save()
        return result

    def start_tool(self, tool_name: str) -> None:
        """开始记录一次工具执行。"""
        self._current_tool = ToolExecutionRecord(
            tool_name=tool_name,
            start_time=time.time(),
        )

    def end_tool(self, success: bool, error: str = "", tokens: int = 0) -> ToolExecutionRecord:
        """结束当前工具执行，记录到当前 turn 中。"""
        if self._current_tool is None:
            raise RuntimeError("No tool execution in progress")
        self._current_tool.end_time = time.time()
        self._current_tool.success = success
        self._current_tool.error_message = error
        self._current_tool.tokens_consumed = tokens
        self._current_tool.error_category = self._classify_error(error)

        if self._current_turn:
            self._current_turn.tool_records.append(self._current_tool)

        result = self._current_tool
        self._current_tool = None
        return result

    def get_tool_stats(self, tool_name: str) -> ToolHistoricalStats:
        """获取指定工具的历史统计；不存在时返回空白 stats。"""
        return self._tool_stats.get(tool_name, ToolHistoricalStats(tool_name=tool_name))

    def get_all_tool_stats(self) -> dict[str, ToolHistoricalStats]:
        """返回所有工具的历史统计字典副本。"""
        return dict(self._tool_stats)

    def get_recent_turns(self, count: int = 10) -> list[AgentTurnMetrics]:
        """返回最近的 ``count`` 个 turn 度量。"""
        return self._turns[-count:]

    def _classify_error(self, error_message: str) -> ErrorCategory:
        """基于错误消息文本对错误做简单分类。"""
        error_lower = error_message.lower()
        if any(kw in error_lower for kw in ["connection", "timeout", "network", "refused", "unreachable"]):
            return ErrorCategory.NETWORK
        if any(kw in error_lower for kw in ["permission", "access denied", "unauthorized", "forbidden"]):
            return ErrorCategory.PERMISSION
        if any(kw in error_lower for kw in ["memory", "disk", "space", "resource", "quota"]):
            return ErrorCategory.RESOURCE
        if error_message:
            return ErrorCategory.LOGIC
        return ErrorCategory.UNKNOWN

    def _update_tool_stats(self, record: ToolExecutionRecord) -> None:
        """把一条工具记录累加到对应工具的历史统计。"""
        name = record.tool_name
        if name not in self._tool_stats:
            self._tool_stats[name] = ToolHistoricalStats(tool_name=name)

        stats = self._tool_stats[name]
        stats.total_executions += 1
        if record.success:
            stats.successful_executions += 1
        stats.total_duration_ms += record.duration_ms

        cat = record.error_category.value
        stats.error_counts[cat] = stats.error_counts.get(cat, 0) + 1

    def _save(self) -> None:
        """把统计落盘（best-effort，写失败不抛异常）。"""
        if self._storage_path is None:
            return
        try:
            data = {
                "tool_stats": {
                    name: {
                        "tool_name": s.tool_name,
                        "total_executions": s.total_executions,
                        "successful_executions": s.successful_executions,
                        "total_duration_ms": s.total_duration_ms,
                        "error_counts": s.error_counts,
                    }
                    for name, s in self._tool_stats.items()
                },
                "recent_turns": [
                    {
                        "turn_id": t.turn_id,
                        "duration_ms": t.duration_ms,
                        "tool_success_rate": t.tool_success_rate,
                        "total_tokens": t.total_tokens,
                        "tool_count": len(t.tool_records),
                    }
                    for t in self._turns[-50:]  # 仅保留最近 50 个 turn
                ],
            }
            self._storage_path.parent.mkdir(parents=True, exist_ok=True)
            self._storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass  # 度量持久化是 best-effort，写失败不影响主流程

    def _load(self) -> None:
        """从磁盘加载历史统计（损坏或失败时静默忽略）。"""
        try:
            data = json.loads(self._storage_path.read_text(encoding="utf-8"))
            for name, s in data.get("tool_stats", {}).items():
                self._tool_stats[name] = ToolHistoricalStats(
                    tool_name=s["tool_name"],
                    total_executions=s["total_executions"],
                    successful_executions=s["successful_executions"],
                    total_duration_ms=s["total_duration_ms"],
                    error_counts=s.get("error_counts", {}),
                )
        except Exception:
            pass
