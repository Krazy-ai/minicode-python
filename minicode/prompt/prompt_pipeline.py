"""MiniCode 的 prompt 动态拼装管线。

按段落组织 prompt，通过缓存边界与条件段实现高效复用，
落地了 Learn Claude Code 的最佳实践。

核心概念：
- SYSTEM_PROMPT_DYNAMIC_BOUNDARY：分隔静态前缀（可缓存）和动态后缀（按会话变化），
  让 API 提供商可以做跨会话的 prompt cache。
- PromptSection：以声明式方式注册段落，包含 name / condition / builder。
- 段落级缓存：避免每次都重读 CLAUDE.md / skills 等。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# 区分静态/动态段落的边界标记。
# Anthropic / OpenAI 等 API 会基于该标记做 prompt cache。
SYSTEM_PROMPT_DYNAMIC_BOUNDARY = "__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__"


@dataclass
class PromptSection:
    """声明式的 prompt 段落，支持条件包含与缓存。"""

    name: str
    builder: Callable[[], str]
    condition: Callable[[], bool] | None = None
    cache_ttl: float = 300.0  # 默认 5 分钟
    _cached_value: str | None = field(default=None, repr=False)
    _cached_at: float = field(default=0.0, repr=False)

    def evaluate(self) -> str | None:
        """若满足 condition 则返回段落文本，否则返回 None。"""
        if self.condition is not None and not self.condition():
            return None

        # 命中缓存直接返回
        now = time.monotonic()
        if self._cached_value is not None and (now - self._cached_at) < self.cache_ttl:
            return self._cached_value

        # 重新构建并缓存
        text = self.builder()
        self._cached_value = text
        self._cached_at = now
        return text


class PromptPipeline:
    """以缓存边界为核心的 prompt 段落生命周期管理。

    用法：
        pipeline = PromptPipeline()
        pipeline.register_static("role", "You are an AI assistant...")
        pipeline.register_dynamic(
            "skills",
            lambda: get_skills_text(skills),
            condition=lambda: len(skills) > 0,
        )
        prompt = pipeline.build()
    """

    def __init__(self) -> None:
        self._static_sections: list[PromptSection] = []
        self._dynamic_sections: list[PromptSection] = []

    def register_static(self, name: str, text: str) -> None:
        """注册永不变化的段落（可完全缓存）。"""
        self._static_sections.append(
            PromptSection(
                name=name,
                builder=lambda: text,
                cache_ttl=float("inf"),  # 永不过期
            )
        )

    def register_dynamic(
        self,
        name: str,
        builder: Callable[[], str],
        condition: Callable[[], bool] | None = None,
        cache_ttl: float = 300.0,
    ) -> None:
        """注册可能在不同轮次间变化的段落。"""
        self._dynamic_sections.append(
            PromptSection(
                name=name,
                builder=builder,
                condition=condition,
                cache_ttl=cache_ttl,
            )
        )

    def build(self) -> str:
        """组装出带缓存边界标记的完整 system prompt。"""
        parts: list[str] = []

        # 静态前缀（跨轮次/跨会话可缓存）
        for section in self._static_sections:
            text = section.evaluate()
            if text:
                parts.append(text)

        # 动态边界标记
        parts.append(SYSTEM_PROMPT_DYNAMIC_BOUNDARY)

        # 动态后缀（每轮重建）
        for section in self._dynamic_sections:
            text = section.evaluate()
            if text:
                parts.append(text)

        return "\n\n".join(p for p in parts if p)

    def clear_cache(self) -> None:
        """清空所有段落的缓存（下次 build 时强制重算）。"""
        for section in self._static_sections + self._dynamic_sections:
            section._cached_value = None
            section._cached_at = 0.0


# ---------------------------------------------------------------------------
# 文件级缓存：用于昂贵的段落构建器（如 CLAUDE.md）
# ---------------------------------------------------------------------------

_file_cache: dict[str, tuple[str, float, float]] = {}


def read_file_cached(path: Path, ttl: float = 300.0) -> str | None:
    """基于 mtime 的文件读取缓存。

    文件不存在时返回 None。
    """
    key = str(path.resolve())
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None

    if key in _file_cache:
        cached_text, cached_mtime, cached_at = _file_cache[key]
        if mtime == cached_mtime and (time.monotonic() - cached_at) < ttl:
            return cached_text

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    _file_cache[key] = (text, mtime, time.monotonic())
    return text


def content_hash(text: str) -> str:
    """计算用于缓存失效的简短内容哈希。"""
    return hashlib.sha256(text.encode()).hexdigest()[:12]
