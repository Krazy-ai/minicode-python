"""子 agent 的上下文隔离系统。

借鉴 Learn Claude Code 的最佳实践：
- 通过沙箱隔离子 agent 上下文，避免污染主 agent 上下文
- 每个子 agent 拥有独立的工具注册视图
- 为派生 agent 单独管理上下文窗口

提供：
- AgentContext：子 agent 的隔离上下文容器
- ContextSandbox：管理多个隔离上下文
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from minicode.types import ChatMessage


@dataclass
class AgentContext:
    """子 agent 的隔离上下文容器。

    每个子 agent 拥有独立的：
    - 消息历史
    - 工具注册视图（已过滤）
    - 工作目录
    - 权限范围
    """

    agent_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    agent_type: str = "general"  # explore / plan / general
    messages: list[ChatMessage] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    cwd: str = "."
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    token_count: int = 0
    max_tokens: int = 50000  # 该子 agent 上下文窗口上限

    def add_message(self, message: ChatMessage) -> None:
        """向该 agent 的上下文中追加一条消息。"""
        self.messages.append(message)
        self.updated_at = time.time()

    def add_messages(self, messages: list[ChatMessage]) -> None:
        """批量追加消息。"""
        self.messages.extend(messages)
        self.updated_at = time.time()

    def get_recent_messages(self, limit: int = 20) -> list[ChatMessage]:
        """按 token 上限取最近若干条消息。"""
        result = []
        total_tokens = 0
        for msg in reversed(self.messages):
            content = msg.get("content", "")
            msg_tokens = len(content) // 4  # 粗略估算
            if total_tokens + msg_tokens > self.max_tokens:
                break
            result.insert(0, msg)
            total_tokens += msg_tokens
        return result

    def get_context_summary(self) -> dict[str, Any]:
        """汇总该 agent 上下文的概览信息。"""
        return {
            "agent_id": self.agent_id,
            "agent_type": self.agent_type,
            "message_count": len(self.messages),
            "token_count": self.token_count,
            "allowed_tools": self.allowed_tools,
            "cwd": self.cwd,
            "created_at": self.created_at,
        }

    def clear_history(self) -> None:
        """清空消息历史（保留 system 消息）。"""
        system_msgs = [m for m in self.messages if m.get("role") == "system"]
        self.messages = system_msgs
        self.updated_at = time.time()

    def clone(self) -> AgentContext:
        """对当前上下文做深拷贝。"""
        return copy.deepcopy(self)


class ContextSandbox:
    """统一管理多个隔离的 agent 上下文。

    职责：
    - 为子 agent 创建独立上下文
    - agent 完成后清理上下文
    - 跨 agent 的 token 预算管理
    """

    def __init__(self, total_token_budget: int = 150000) -> None:
        self._contexts: dict[str, AgentContext] = {}
        self.total_token_budget = total_token_budget
        self.used_tokens = 0

    def create_context(
        self,
        agent_type: str = "general",
        allowed_tools: list[str] | None = None,
        cwd: str = ".",
        max_tokens: int = 50000,
    ) -> AgentContext:
        """为子 agent 创建一个新的隔离上下文。"""
        # 检查 token 预算
        if self.used_tokens + max_tokens > self.total_token_budget:
            raise ValueError(
                f"Token budget exceeded: {self.used_tokens}/{self.total_token_budget}"
            )

        context = AgentContext(
            agent_type=agent_type,
            allowed_tools=allowed_tools or [],
            cwd=cwd,
            max_tokens=max_tokens,
        )
        self._contexts[context.agent_id] = context
        self.used_tokens += max_tokens
        return context

    def get_context(self, agent_id: str) -> AgentContext | None:
        """按 agent_id 获取上下文。"""
        return self._contexts.get(agent_id)

    def release_context(self, agent_id: str) -> None:
        """释放某个 agent 的上下文，归还 token 预算。"""
        context = self._contexts.pop(agent_id, None)
        if context:
            self.used_tokens -= context.max_tokens

    def release_all(self) -> None:
        """释放全部上下文。"""
        self._contexts.clear()
        self.used_tokens = 0

    def get_active_count(self) -> int:
        """当前活跃上下文数量。"""
        return len(self._contexts)

    def get_sandbox_stats(self) -> dict[str, Any]:
        """获取沙箱统计数据。"""
        return {
            "active_contexts": len(self._contexts),
            "used_tokens": self.used_tokens,
            "total_budget": self.total_token_budget,
            "budget_percentage": (self.used_tokens / self.total_token_budget) * 100
            if self.total_token_budget > 0
            else 0,
        }

    def format_sandbox_status(self) -> str:
        """以可读形式格式化沙箱状态。"""
        stats = self.get_sandbox_stats()
        lines = [
            "Context Sandbox Status",
            "=" * 50,
            f"Active contexts: {stats['active_contexts']}",
            f"Token usage: {stats['used_tokens']:,}/{stats['total_budget']:,} ({stats['budget_percentage']:.0f}%)",
            "",
        ]

        if self._contexts:
            lines.append("Active Agents:")
            for ctx in self._contexts.values():
                lines.append(
                    f"  • [{ctx.agent_type}] {ctx.agent_id} "
                    f"({len(ctx.messages)} msgs, {ctx.cwd})"
                )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_sandbox = ContextSandbox()


def get_sandbox() -> ContextSandbox:
    """获取全局上下文沙箱。"""
    return _sandbox


def create_subagent_context(
    agent_type: str = "general",
    allowed_tools: list[str] | None = None,
    cwd: str = ".",
    max_tokens: int = 50000,
) -> AgentContext:
    """创建子 agent 上下文的便捷函数。"""
    return _sandbox.create_context(agent_type, allowed_tools, cwd, max_tokens)


def release_subagent_context(agent_id: str) -> None:
    """释放子 agent 上下文的便捷函数。"""
    _sandbox.release_context(agent_id)
