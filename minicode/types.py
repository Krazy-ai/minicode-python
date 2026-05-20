"""核心类型契约。

定义贯穿整个 minicode 包的基础数据结构：
- ChatMessage：与 LLM 之间往返的消息（含 role / content / tool 调用相关字段）
- ToolCall：单次工具调用请求
- AgentStep：agent 单步输出（assistant 文本 或 tool_calls）
- ModelAdapter：模型适配器协议，所有 LLM provider 必须实现 next() 接口
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Protocol, TypedDict


class ChatMessage(TypedDict, total=False):
    """对话消息。

    role 取值说明：
    - system / user / assistant：标准三类角色
    - assistant_progress：assistant 中间进度文本（非最终回答）
    - assistant_tool_call：assistant 发起的工具调用条目
    - tool_result：工具执行结果回传给 assistant
    """
    role: Literal[
        "system",
        "user",
        "assistant",
        "assistant_progress",
        "assistant_tool_call",
        "tool_result",
    ]
    content: str
    toolUseId: str
    toolName: str
    input: Any
    isError: bool


class ToolCall(TypedDict):
    """单次工具调用请求。"""
    id: str
    toolName: str
    input: Any


@dataclass(slots=True)
class StepDiagnostics:
    """单步执行的诊断信息（用于排查为什么 agent 停下/没产出工具调用）。"""
    stopReason: str | None = None
    blockTypes: list[str] = field(default_factory=list)
    ignoredBlockTypes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AgentStep:
    """agent 单轮模型调用的输出。

    type:
        - "assistant"：纯文本回答（可能是 final 也可能是 progress）
        - "tool_calls"：要求执行一组工具
    kind:
        - "final"：最终回答，循环可结束
        - "progress"：中途汇报，需继续推进
    """
    type: Literal["assistant", "tool_calls"]
    content: str = ""
    kind: Literal["final", "progress"] | None = None
    calls: list[ToolCall] = field(default_factory=list)
    contentKind: Literal["progress"] | None = None
    diagnostics: StepDiagnostics | None = None


class ModelAdapter(Protocol):
    """模型适配器协议。

    所有 LLM provider（Anthropic / OpenAI / OpenRouter / Mock 等）必须实现
    next() 方法：传入消息历史，返回一个 AgentStep。
    """

    def next(
        self,
        messages: list[ChatMessage],
        on_stream_chunk: Callable[[str], None] | None = None,
        store: Any | None = None,
    ) -> AgentStep: ...
