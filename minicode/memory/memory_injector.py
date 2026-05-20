from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from minicode.memory.memory import MemoryManager, MemoryScope, MemoryEntry
from minicode.runtime.logging_config import get_logger

logger = get_logger("memory_injector")


@dataclass
class InjectedMemory:
    """已准备好注入到上下文的一条记忆。"""
    content: str
    category: str
    relevance_score: float
    source: str  # "search" / "tag" / "category"


class MemoryInjector:
    """根据当前任务把相关记忆注入 agent 上下文。"""

    def __init__(
        self,
        memory_manager: MemoryManager | None = None,
        max_injected_memories: int = 5,
        min_relevance: float = 0.3,
        max_tokens_per_memory: int = 200,
    ):
        self._memory = memory_manager
        self._max_injected = max_injected_memories
        self._min_relevance = min_relevance
        self._max_tokens = max_tokens_per_memory
        self._last_query: str = ""
        self._last_injection_time: float = 0.0
        self._injection_cooldown: float = 30.0  # 注入间隔下限（秒）

    def inject_for_task(
        self,
        task_description: str,
        current_files: list[str] | None = None,
    ) -> list[InjectedMemory]:
        """根据任务描述检索并准备相关记忆。

        参数：
            task_description: 当前任务的描述
            current_files: 当前正在处理的文件列表

        返回：
            按相关度排序的 InjectedMemory 列表。
        """
        if self._memory is None:
            return []

        # 冷却检查 —— 避免过于频繁地注入
        if time.time() - self._last_injection_time < self._injection_cooldown:
            if task_description == self._last_query:
                return []  # 同样查询，跳过

        self._last_query = task_description
        self._last_injection_time = time.time()

        memories: list[tuple[float, MemoryEntry, str]] = []

        # 跨所有 scope 检索
        for scope in MemoryScope:
            results = self._memory.search(
                task_description,
                scope=scope,
                limit=self._max_injected * 2,
                min_relevance=self._min_relevance,
            )
            for entry in results:
                # 计算综合相关度
                relevance = self._calculate_relevance(entry, task_description, current_files)
                memories.append((relevance, entry, scope.value))

        # 按相关度排序，取 top N
        memories.sort(key=lambda x: x[0], reverse=True)

        injected: list[InjectedMemory] = []
        seen_content: set[str] = set()

        for relevance, entry, scope_name in memories[:self._max_injected]:
            content = entry.content[:self._max_tokens * 4]  # 字符上限粗估
            content_key = content[:100].lower()

            if content_key in seen_content:
                continue
            seen_content.add(content_key)

            injected.append(InjectedMemory(
                content=content,
                category=entry.category,
                relevance_score=relevance,
                source=f"{scope_name}_search",
            ))

        # 若任务含有代码相关关键词，再按 tag 补充
        tag_memories = self._inject_by_tags(task_description)
        for mem in tag_memories:
            content_key = mem.content[:100].lower()
            if content_key not in seen_content and len(injected) < self._max_injected:
                seen_content.add(content_key)
                injected.append(mem)

        logger.info(
            "Injected %d memories for task: %s",
            len(injected),
            task_description[:50],
        )

        return injected

    def inject_on_failure(
        self,
        error_message: str,
        tool_name: str,
    ) -> list[InjectedMemory]:
        """在工具调用失败时检索类似的历史失败和解决方案。

        参数：
            error_message: 工具失败时返回的错误信息
            tool_name: 失败的工具名

        返回：
            可能含有解决方案的相关记忆列表。
        """
        if self._memory is None:
            return []

        # 围绕错误和工具名构造查询
        query = f"{tool_name} {error_message[:100]}"

        memories: list[tuple[float, MemoryEntry, str]] = []

        for scope in MemoryScope:
            results = self._memory.search(
                query,
                scope=scope,
                limit=self._max_injected,
                min_relevance=0.2,  # 失败恢复场景适度放宽阈值
            )
            for entry in results:
                # 给 testing/decision/code-pattern 类记忆加权
                relevance = 0.5  # 失败上下文的基础相关度
                if entry.category in ["testing", "decision", "code-pattern"]:
                    relevance += 0.2
                if tool_name in entry.content.lower():
                    relevance += 0.15
                memories.append((relevance, entry, scope.value))

        memories.sort(key=lambda x: x[0], reverse=True)

        injected: list[InjectedMemory] = []
        for relevance, entry, scope_name in memories[:self._max_injected]:
            injected.append(InjectedMemory(
                content=entry.content[:self._max_tokens * 4],
                category=entry.category,
                relevance_score=relevance,
                source=f"{scope_name}_failure_recovery",
            ))

        if injected:
            logger.info(
                "Injected %d recovery memories for %s failure",
                len(injected),
                tool_name,
            )

        return injected

    def format_for_prompt(self, memories: list[InjectedMemory]) -> str:
        """把已注入的记忆格式化为可拼接到 system prompt 的文本。"""
        if not memories:
            return ""

        lines = ["## Relevant Context from Memory", ""]

        for i, mem in enumerate(memories, 1):
            lines.append(f"{i}. [{mem.category}] {mem.content}")

        lines.append("")
        lines.append("Use the above context to inform your decisions.")

        return "\n".join(lines)

    def _calculate_relevance(
        self,
        entry: MemoryEntry,
        task_description: str,
        current_files: list[str] | None,
    ) -> float:
        """计算一条记忆相对于当前任务的综合相关度。"""
        score = 0.5  # 基础分

        # 类别与任务类型匹配则加权
        task_lower = task_description.lower()
        if entry.category == "architecture" and any(kw in task_lower for kw in ["design", "structure", "api"]):
            score += 0.2
        elif entry.category == "testing" and any(kw in task_lower for kw in ["test", "assert", "verify"]):
            score += 0.2
        elif entry.category == "convention" and any(kw in task_lower for kw in ["style", "naming", "format"]):
            score += 0.2

        # 提及当前文件则加权
        if current_files:
            entry_lower = entry.content.lower()
            for file_path in current_files:
                file_name = file_path.split("/")[-1].split("\\")[-1]
                if file_name.lower() in entry_lower:
                    score += 0.15

        # 时效性加权
        age_hours = (time.time() - entry.updated_at) / 3600
        if age_hours < 24:
            score += 0.1
        elif age_hours < 168:  # 一周内
            score += 0.05

        return min(1.0, score)

    def _inject_by_tags(self, task_description: str) -> list[InjectedMemory]:
        """通过 tag 匹配为当前任务补充记忆。"""
        if self._memory is None:
            return []

        # 从任务描述中抽取潜在 tag
        task_lower = task_description.lower()
        keywords = []

        # 常见代码相关关键词
        code_keywords = [
            "api", "test", "function", "class", "database", "config",
            "security", "performance", "git", "docker", "deploy",
        ]
        for kw in code_keywords:
            if kw in task_lower:
                keywords.append(kw)

        memories: list[InjectedMemory] = []
        seen: set[str] = set()

        for keyword in keywords[:3]:  # 最多取 3 个关键词
            for scope in MemoryScope:
                tagged = self._memory.search_by_tag(scope, keyword)
                for entry in tagged:
                    content_key = entry.content[:100].lower()
                    if content_key not in seen:
                        seen.add(content_key)
                        memories.append(InjectedMemory(
                            content=entry.content[:self._max_tokens * 4],
                            category=entry.category,
                            relevance_score=0.6,  # tag 匹配视为中等相关度
                            source=f"{scope.value}_tag",
                        ))

        return memories[:self._max_injected]
