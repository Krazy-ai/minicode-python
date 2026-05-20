"""上下文压缩过程中的工作记忆保护。

借鉴 Learn Claude Code 的最佳实践：
- 在压缩上下文时保留关键的连续性信息
- 防止活跃任务的关键上下文被摘要掉
- 跨压缩边界保持对话流的连续性

提供：
- WorkingMemoryTracker：跟踪并保护关键上下文
- ContinuityMarker：标记重要的对话流转折点
- MemoryBudgetAllocator：为工作记忆分配 token 预算
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from minicode.memory.context_manager import estimate_tokens


@dataclass
class WorkingMemoryEntry:
    """单条工作记忆，压缩时应当被保护。"""

    content: str
    entry_type: str  # active_task / user_intent / key_decision / error_context
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None  # None 表示永不过期
    importance: float = 1.0  # 0.0 - 1.0，越高越重要

    def is_expired(self) -> bool:
        """该记忆是否已过期。"""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def token_count(self) -> int:
        """估算该记忆的 token 数。"""
        return estimate_tokens(self.content)


class WorkingMemoryTracker:
    """跟踪并保护压缩过程中应保留的关键上下文。

    实现 Learn Claude Code 中提到的「工作记忆保护」模式：
    上下文压缩时，本跟踪器中的条目会被保留，
    以维持对话连续性与任务一致性。
    """

    def __init__(
        self,
        max_entries: int = 15,
        max_tokens: int = 4000,
    ) -> None:
        self._entries: list[WorkingMemoryEntry] = []
        self.max_entries = max_entries
        self.max_tokens = max_tokens

    def add(
        self,
        content: str,
        entry_type: str = "active_task",
        ttl_seconds: float | None = None,
        importance: float = 1.0,
    ) -> WorkingMemoryEntry:
        """添加一条受保护的工作记忆。

        参数：
            content: 待保护的内容
            entry_type: 工作记忆类型（active_task / user_intent 等）
            ttl_seconds: 存活时间（秒，None 表示永不过期）
            importance: 重要度 0.0~1.0（越大越受保护）
        """
        expires_at = None
        if ttl_seconds is not None:
            expires_at = time.time() + ttl_seconds

        entry = WorkingMemoryEntry(
            content=content,
            entry_type=entry_type,
            expires_at=expires_at,
            importance=importance,
        )

        self._entries.append(entry)
        self._enforce_limits()
        return entry

    def remove(self, entry: WorkingMemoryEntry) -> None:
        """移除指定的工作记忆。"""
        if entry in self._entries:
            self._entries.remove(entry)

    def clear_expired(self) -> int:
        """清理所有过期条目，返回被清理的数量。"""
        before = len(self._entries)
        self._entries = [e for e in self._entries if not e.is_expired()]
        return before - len(self._entries)

    def get_protected_content(self) -> list[str]:
        """获取所有未过期的受保护内容。"""
        self.clear_expired()
        return [e.content for e in self._entries]

    def get_protected_tokens(self) -> int:
        """统计所有受保护内容的总 token 数。"""
        return sum(e.token_count() for e in self._entries if not e.is_expired())

    def get_stats(self) -> dict[str, Any]:
        """获取工作记忆统计信息。"""
        self.clear_expired()
        return {
            "entries": len(self._entries),
            "max_entries": self.max_entries,
            "protected_tokens": self.get_protected_tokens(),
            "max_tokens": self.max_tokens,
            "utilization": self.get_protected_tokens() / self.max_tokens
            if self.max_tokens > 0
            else 0,
        }

    def _enforce_limits(self) -> None:
        """超出限制时移除优先级最低的条目。"""
        # 先清理过期项
        self.clear_expired()

        # 再按 token 预算裁剪
        while self.get_protected_tokens() > self.max_tokens and self._entries:
            # 删除重要度最低的条目
            self._entries.sort(key=lambda e: e.importance)
            self._entries.pop(0)

        # 最后按条目数量裁剪
        while len(self._entries) > self.max_entries and self._entries:
            self._entries.sort(key=lambda e: e.importance)
            self._entries.pop(0)

    def format_status(self) -> str:
        """格式化工作记忆状态用于展示。"""
        stats = self.get_stats()
        lines = [
            "Working Memory",
            "=" * 50,
            f"Entries: {stats['entries']}/{stats['max_entries']}",
            f"Protected tokens: {stats['protected_tokens']:,}/{stats['max_tokens']:,} ({stats['utilization']*100:.0f}%)",
            "",
        ]

        if self._entries:
            lines.append("Protected Content:")
            for entry in self._entries:
                expires = ""
                if entry.expires_at:
                    remaining = entry.expires_at - time.time()
                    if remaining > 0:
                        expires = f" (expires in {remaining/60:.0f}m)"
                    else:
                        expires = " (EXPIRED)"
                preview = entry.content[:60].replace("\n", " ")
                lines.append(f"  • [{entry.entry_type}] {preview}...{expires}")

        return "\n".join(lines)


@dataclass
class ContinuityMarker:
    """标记对话流中的重要转折点。

    上下文被压缩后，借助这些标记可以
    在消息被摘要后仍然还原对话脉络。
    """

    marker_type: str  # task_start / decision_point / error_recovered / user_redirect
    description: str
    timestamp: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)


class ConversationContinuityManager:
    """跨压缩边界维护对话连续性。

    上下文被压缩时，通过保存关键转折点，
    本管理器帮助恢复对话脉络。
    """

    def __init__(self, max_markers: int = 20) -> None:
        self._markers: list[ContinuityMarker] = []
        self.max_markers = max_markers

    def add_marker(
        self,
        marker_type: str,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> ContinuityMarker:
        """添加一个连续性标记。"""
        marker = ContinuityMarker(
            marker_type=marker_type,
            description=description,
            metadata=metadata or {},
        )
        self._markers.append(marker)

        # 数量上限保护
        if len(self._markers) > self.max_markers:
            self._markers = self._markers[-self.max_markers:]

        return marker

    def get_recent_markers(self, limit: int = 10) -> list[ContinuityMarker]:
        """获取最近的连续性标记。"""
        return self._markers[-limit:]

    def get_markers_since(self, timestamp: float) -> list[ContinuityMarker]:
        """获取指定时间之后添加的标记。"""
        return [m for m in self._markers if m.timestamp > timestamp]

    def format_continuity_summary(self) -> str:
        """格式化对话连续性摘要用于展示。"""
        if not self._markers:
            return "No continuity markers."

        lines = ["Conversation Continuity", "=" * 50, ""]
        for marker in self._markers[-10:]:  # 最近 10 条
            time_str = time.strftime("%H:%M:%S", time.localtime(marker.timestamp))
            lines.append(f"  [{time_str}] [{marker.marker_type}] {marker.description}")

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_working_memory = WorkingMemoryTracker()
_continuity_manager = ConversationContinuityManager()


def get_working_memory() -> WorkingMemoryTracker:
    """获取全局工作记忆跟踪器。"""
    return _working_memory


def get_continuity_manager() -> ConversationContinuityManager:
    """获取全局对话连续性管理器。"""
    return _continuity_manager


def protect_context(
    content: str,
    entry_type: str = "active_task",
    ttl_seconds: float | None = None,
) -> WorkingMemoryEntry:
    """便捷函数：在压缩过程中保护一段上下文。"""
    return _working_memory.add(content, entry_type, ttl_seconds)


def mark_continuity(
    marker_type: str,
    description: str,
    metadata: dict[str, Any] | None = None,
) -> ContinuityMarker:
    """便捷函数：添加一个连续性标记。"""
    return _continuity_manager.add_marker(marker_type, description, metadata)
