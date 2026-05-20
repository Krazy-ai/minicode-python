"""多 Agent 协作协议（实验性）。

参考 "Learn Claude Code" 的最佳实践：
    - 命名持久化的 teammate，使用标准化通信协议
    - 安全的自主任务认领（带角色与能力校验）
    - 通过 git worktree 实现并行操作的执行隔离

提供四个核心抽象：
    - AgentIdentity：带能力清单和状态的命名 agent
    - CollaborationMessage：agent 间通信的统一消息格式
    - TeamRegistry：可用 agent 的注册中心，支持任务发布与认领
    - MessageRouter：在 agent 之间安全转发消息

注意：这是为多 agent 编排预留的基础设施，目前主要由 ``task`` 工具的子 agent
机制使用。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from minicode.memory.context_isolation import AgentContext, ContextSandbox, get_sandbox


# ---------------------------------------------------------------------------
# Agent 身份
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    """Agent 生命周期状态。"""
    IDLE = "idle"
    BUSY = "busy"
    AWAY = "away"
    OFFLINE = "offline"


class AgentRole(str, Enum):
    """Agent 角色类型。"""
    EXPLORER = "explorer"      # 代码库探索
    PLANNER = "planner"        # 任务规划与拆解
    IMPLEMENTER = "implementer"  # 代码实现
    REVIEWER = "reviewer"      # Code review 与质量检查
    GENERAL = "general"        # 通用


@dataclass
class AgentIdentity:
    """命名持久化 agent 身份。

    每个 agent 拥有唯一身份，包含能力清单、状态和当前任务信息。
    """

    agent_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    role: AgentRole = AgentRole.GENERAL
    status: AgentStatus = AgentStatus.IDLE
    capabilities: list[str] = field(default_factory=list)
    current_task: str | None = None
    task_started_at: float | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def start_task(self, task_id: str) -> None:
        """把 agent 标记为 BUSY，关联到指定任务。"""
        self.status = AgentStatus.BUSY
        self.current_task = task_id
        self.task_started_at = time.time()
        self.last_active = time.time()

    def complete_task(self) -> None:
        """标记任务完成，回到 IDLE。"""
        self.status = AgentStatus.IDLE
        self.current_task = None
        self.task_started_at = None
        self.last_active = time.time()

    def go_away(self) -> None:
        """临时不可用（AWAY）。"""
        self.status = AgentStatus.AWAY
        self.last_active = time.time()

    def go_offline(self) -> None:
        """下线（OFFLINE）。"""
        self.status = AgentStatus.OFFLINE
        self.last_active = time.time()

    def is_available(self) -> bool:
        """是否可接收新任务（仅 IDLE 状态时为 True）。"""
        return self.status == AgentStatus.IDLE

    def get_active_duration(self) -> float:
        """当前任务已执行的秒数。"""
        if self.task_started_at is None:
            return 0.0
        return time.time() - self.task_started_at


# ---------------------------------------------------------------------------
# 协作消息
# ---------------------------------------------------------------------------

class MessageType(str, Enum):
    """agent 间通信的标准化消息类型。"""
    TASK_ASSIGN = "task_assign"         # 给指定 agent 派活
    TASK_CLAIM = "task_claim"           # agent 主动认领可用任务
    TASK_COMPLETE = "task_complete"     # 任务完成通知
    TASK_FAILED = "task_failed"         # 任务失败通知
    HELP_REQUEST = "help_request"       # 求助请求
    HELP_RESPONSE = "help_response"     # 对求助的回复
    STATUS_UPDATE = "status_update"     # agent 状态变化广播
    CONTEXT_SHARE = "context_share"     # 共享上下文信息
    REVIEW_REQUEST = "review_request"   # 请求 code review
    REVIEW_RESPONSE = "review_response" # code review 反馈


@dataclass
class CollaborationMessage:
    """agent 间通信的标准消息格式。"""

    msg_type: MessageType
    sender_id: str
    receiver_id: str | None = None  # None 表示广播
    task_id: str | None = None
    content: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    msg_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict[str, Any]:
        """序列化为 dict（便于跨进程传递）。"""
        return {
            "msg_id": self.msg_id,
            "msg_type": self.msg_type.value,
            "sender_id": self.sender_id,
            "receiver_id": self.receiver_id,
            "task_id": self.task_id,
            "content": self.content,
            "metadata": self.metadata,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CollaborationMessage:
        """从 dict 反序列化。"""
        return cls(
            msg_type=MessageType(data["msg_type"]),
            sender_id=data["sender_id"],
            receiver_id=data.get("receiver_id"),
            task_id=data.get("task_id"),
            content=data.get("content", ""),
            metadata=data.get("metadata", {}),
            timestamp=data.get("timestamp", time.time()),
            msg_id=data.get("msg_id", str(uuid.uuid4())[:8]),
        )


# ---------------------------------------------------------------------------
# 团队注册中心
# ---------------------------------------------------------------------------

@dataclass
class TaskPosting:
    """一条可被 agent 认领的任务广告。"""

    task_id: str
    description: str
    required_role: AgentRole | None = None
    required_capabilities: list[str] = field(default_factory=list)
    priority: str = "normal"  # low / normal / high / critical
    posted_at: float = field(default_factory=time.time)
    claimed_by: str | None = None
    status: str = "open"  # open / claimed / completed / failed


class TeamRegistry:
    """可用 agent 的注册中心，支持标准化的任务认领流程。

    管理：
        - agent 注册与发现
        - 任务发布与认领
        - 安全的自主任务派发（含角色 + 能力校验）
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentIdentity] = {}
        self._tasks: dict[str, TaskPosting] = {}
        self._message_handlers: dict[MessageType, list[Callable]] = {}

    # --- agent 管理 ---
    def register_agent(self, agent: AgentIdentity) -> None:
        """把 agent 注册到团队。"""
        self._agents[agent.agent_id] = agent

    def unregister_agent(self, agent_id: str) -> None:
        """从团队中移除 agent。"""
        self._agents.pop(agent_id, None)

    def get_agent(self, agent_id: str) -> AgentIdentity | None:
        """按 ID 取 agent，找不到返回 None。"""
        return self._agents.get(agent_id)

    def get_available_agents(
        self,
        role: AgentRole | None = None,
        capability: str | None = None,
    ) -> list[AgentIdentity]:
        """按条件筛选可用 agent（IDLE 状态 + 角色匹配 + 能力匹配）。"""
        available = [a for a in self._agents.values() if a.is_available()]

        if role:
            available = [a for a in available if a.role == role]

        if capability:
            available = [
                a for a in available
                if capability in a.capabilities
            ]

        return available

    # --- 任务管理 ---
    def post_task(
        self,
        description: str,
        required_role: AgentRole | None = None,
        required_capabilities: list[str] | None = None,
        priority: str = "normal",
    ) -> TaskPosting:
        """发布一条等待认领的任务。"""
        task = TaskPosting(
            task_id=str(uuid.uuid4())[:8],
            description=description,
            required_role=required_role,
            required_capabilities=required_capabilities or [],
            priority=priority,
        )
        self._tasks[task.task_id] = task
        return task

    def claim_task(
        self,
        task_id: str,
        agent_id: str,
    ) -> bool:
        """agent 认领任务。校验通过且认领成功返回 True。"""
        task = self._tasks.get(task_id)
        agent = self._agents.get(agent_id)

        if not task or not agent:
            return False

        if task.status != "open":
            return False

        if not agent.is_available():
            return False

        # 校验角色 / 能力是否满足要求
        if task.required_role and agent.role != task.required_role:
            return False

        for cap in task.required_capabilities:
            if cap not in agent.capabilities:
                return False

        # 认领任务
        task.claimed_by = agent_id
        task.status = "claimed"
        agent.start_task(task_id)

        return True

    def complete_task(self, task_id: str, agent_id: str) -> bool:
        """标记任务完成。仅认领者本人可调用。"""
        task = self._tasks.get(task_id)
        agent = self._agents.get(agent_id)

        if not task or not agent:
            return False

        if task.claimed_by != agent_id:
            return False

        task.status = "completed"
        agent.complete_task()

        return True

    def fail_task(self, task_id: str, agent_id: str, reason: str = "") -> bool:
        """标记任务失败。"""
        task = self._tasks.get(task_id)
        agent = self._agents.get(agent_id)

        if not task or not agent:
            return False

        task.status = "failed"
        task.metadata["failure_reason"] = reason
        agent.complete_task()  # 失败也回到 IDLE，方便接新任务

        return True

    def get_open_tasks(self) -> list[TaskPosting]:
        """返回所有 open 状态的任务。"""
        return [t for t in self._tasks.values() if t.status == "open"]

    # --- 消息路由 ---
    def register_handler(
        self,
        msg_type: MessageType,
        handler: Callable[[CollaborationMessage], None],
    ) -> None:
        """为某种消息类型注册处理函数。"""
        if msg_type not in self._message_handlers:
            self._message_handlers[msg_type] = []
        self._message_handlers[msg_type].append(handler)

    def send_message(self, message: CollaborationMessage) -> list[Any]:
        """把消息分发给所有已注册的 handler，返回它们各自的返回值列表。"""
        handlers = self._message_handlers.get(message.msg_type, [])
        results = []
        for handler in handlers:
            try:
                results.append(handler(message))
            except Exception:
                results.append(None)
        return results

    # --- 状态查询 ---
    def get_team_status(self) -> dict[str, Any]:
        """返回结构化的团队状态摘要。"""
        return {
            "agents": {
                aid: {
                    "name": a.name,
                    "role": a.role.value,
                    "status": a.status.value,
                    "current_task": a.current_task,
                }
                for aid, a in self._agents.items()
            },
            "tasks": {
                "open": sum(1 for t in self._tasks.values() if t.status == "open"),
                "claimed": sum(1 for t in self._tasks.values() if t.status == "claimed"),
                "completed": sum(1 for t in self._tasks.values() if t.status == "completed"),
                "failed": sum(1 for t in self._tasks.values() if t.status == "failed"),
            },
        }

    def format_team_status(self) -> str:
        """把团队状态格式化为可读文本（供 UI / 命令行展示）。"""
        status = self.get_team_status()
        lines = [
            "Team Status",
            "=" * 50,
            f"Agents: {len(status['agents'])}",
            f"Tasks: {status['tasks']['open']} open, "
            f"{status['tasks']['claimed']} claimed, "
            f"{status['tasks']['completed']} done",
            "",
        ]

        if status["agents"]:
            lines.append("Agents:")
            for aid, info in status["agents"].items():
                task_info = ""
                if info["current_task"]:
                    task_info = f" (task: {info['current_task'][:8]})"
                lines.append(
                    f"  • [{info['status']}] {info['name']} "
                    f"({info['role']}){task_info}"
                )

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模块级单例与便捷函数
# ---------------------------------------------------------------------------

_team_registry = TeamRegistry()


def get_team_registry() -> TeamRegistry:
    """获取全局团队注册中心单例。"""
    return _team_registry


def register_agent(agent: AgentIdentity) -> None:
    """便捷函数：注册一个 agent。"""
    _team_registry.register_agent(agent)


def post_task(
    description: str,
    required_role: AgentRole | None = None,
    required_capabilities: list[str] | None = None,
    priority: str = "normal",
) -> TaskPosting:
    """便捷函数：发布一条任务。"""
    return _team_registry.post_task(
        description, required_role, required_capabilities, priority
    )


def claim_task(task_id: str, agent_id: str) -> bool:
    """便捷函数：让 agent 认领任务。"""
    return _team_registry.claim_task(task_id, agent_id)


def get_available_agents(
    role: AgentRole | None = None,
    capability: str | None = None,
) -> list[AgentIdentity]:
    """便捷函数：获取可用 agent 列表。"""
    return _team_registry.get_available_agents(role, capability)


def format_team_status() -> str:
    """便捷函数：格式化团队状态文本。"""
    return _team_registry.format_team_status()
