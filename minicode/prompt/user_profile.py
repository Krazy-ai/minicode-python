"""USER.md 用户画像系统：用于持久化用户偏好。

支持两种作用域：
- 全局：``~/.mini-code/USER.md``（在所有项目中生效）
- 项目：``.mini-code/USER.md``（项目内的覆盖）

画像分节：
- preferences：通用偏好（语言、详略、回答风格等）
- coding_style：代码格式与风格偏好
- common_patterns：常用模式与约定
- project_context：项目相关备忘
- custom_instructions: 自由格式的助手指令
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class UserPreferences:
    """通用用户偏好。"""
    language: str = ""           # 例如 "zh-CN" / "en-US"
    verbosity: str = ""          # "concise" / "normal" / "detailed"
    response_style: str = ""    # "formal" / "casual" / "technical"
    preferred_framework: str = ""  # 如 "react" / "vue" / "svelte"
    preferred_test_framework: str = ""  # 如 "pytest" / "jest"
    auto_format: bool = False    # 编辑后自动格式化代码


@dataclass
class CodingStyle:
    """代码风格偏好。"""
    indent_style: str = ""       # "spaces" / "tabs"
    indent_size: int = 0         # 2 / 4 ...
    quote_style: str = ""        # "single" / "double"
    semicolons: bool = False     # JS/TS 是否需要分号
    trailing_comma: bool = False
    max_line_length: int = 0
    naming_convention: str = ""  # camelCase / snake_case / PascalCase


@dataclass
class UserProfile:
    """从 USER.md 加载得到的完整画像。"""
    preferences: UserPreferences = field(default_factory=UserPreferences)
    coding_style: CodingStyle = field(default_factory=CodingStyle)
    common_patterns: list[str] = field(default_factory=list)
    project_context: str = ""
    custom_instructions: str = ""
    # 元信息
    source_path: str = ""        # 该画像加载自哪个文件
    raw_content: str = ""        # 原始 Markdown


# ---------------------------------------------------------------------------
# Markdown 解析
# ---------------------------------------------------------------------------

_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)
_KV_RE = re.compile(r"^-\s+\*\*(.+?)\*\*:\s*(.+)$")
_LIST_ITEM_RE = re.compile(r"^-\s+(.+)$", re.MULTILINE)


def _parse_section_body(body: str) -> dict[str, str]:
    """从「- **key**: value」形式的小节正文里解析键值对。"""
    result: dict[str, str] = {}
    for line in body.strip().splitlines():
        m = _KV_RE.match(line.strip())
        if m:
            result[m.group(1).strip().lower().replace(" ", "_")] = m.group(2).strip()
    return result


def _parse_list_items(body: str) -> list[str]:
    """从「- item」形式的小节正文里解析列表项。"""
    items: list[str] = []
    for line in body.strip().splitlines():
        m = _LIST_ITEM_RE.match(line.strip())
        if m:
            items.append(m.group(1).strip())
    return items


def parse_user_md(content: str) -> UserProfile:
    """把 USER.md Markdown 文本解析为 UserProfile。"""
    profile = UserProfile(raw_content=content)

    # 按 ## 分小节
    sections: dict[str, str] = {}
    parts = _SECTION_RE.split(content)

    # parts[0] 是首个 heading 之前的内容；之后按 (heading, body) 交替
    for i in range(1, len(parts) - 1, 2):
        heading = parts[i].strip().lower().replace(" ", "_")
        body = parts[i + 1]
        sections[heading] = body

    # 解析 preferences
    if "preferences" in sections:
        kv = _parse_section_body(sections["preferences"])
        p = profile.preferences
        p.language = kv.get("language", "")
        p.verbosity = kv.get("verbosity", "")
        p.response_style = kv.get("response_style", "")
        p.preferred_framework = kv.get("preferred_framework", "")
        p.preferred_test_framework = kv.get("preferred_test_framework", "")
        p.auto_format = kv.get("auto_format", "").lower() in ("true", "yes", "1")

    # 解析 coding_style
    if "coding_style" in sections:
        kv = _parse_section_body(sections["coding_style"])
        cs = profile.coding_style
        cs.indent_style = kv.get("indent_style", "")
        try:
            cs.indent_size = int(kv.get("indent_size", "0"))
        except ValueError:
            cs.indent_size = 0
        cs.quote_style = kv.get("quote_style", "")
        cs.semicolons = kv.get("semicolons", "").lower() in ("true", "yes", "1")
        cs.trailing_comma = kv.get("trailing_comma", "").lower() in ("true", "yes", "1")
        try:
            cs.max_line_length = int(kv.get("max_line_length", "0"))
        except ValueError:
            cs.max_line_length = 0
        cs.naming_convention = kv.get("naming_convention", "")

    # 解析 common_patterns
    if "common_patterns" in sections:
        profile.common_patterns = _parse_list_items(sections["common_patterns"])

    # 解析 project_context（标题之后的自由文本）
    if "project_context" in sections:
        profile.project_context = sections["project_context"].strip()

    # 解析 custom_instructions（标题之后的自由文本）
    if "custom_instructions" in sections:
        profile.custom_instructions = sections["custom_instructions"].strip()

    return profile


# ---------------------------------------------------------------------------
# Markdown 序列化
# ---------------------------------------------------------------------------

def serialize_user_md(profile: UserProfile) -> str:
    """把 UserProfile 序列化回 USER.md Markdown。"""
    lines: list[str] = ["# User Profile", ""]

    # Preferences
    p = profile.preferences
    if any([p.language, p.verbosity, p.response_style, p.preferred_framework,
            p.preferred_test_framework, p.auto_format]):
        lines.append("## Preferences")
        if p.language:
            lines.append(f"- **Language**: {p.language}")
        if p.verbosity:
            lines.append(f"- **Verbosity**: {p.verbosity}")
        if p.response_style:
            lines.append(f"- **Response Style**: {p.response_style}")
        if p.preferred_framework:
            lines.append(f"- **Preferred Framework**: {p.preferred_framework}")
        if p.preferred_test_framework:
            lines.append(f"- **Preferred Test Framework**: {p.preferred_test_framework}")
        if p.auto_format:
            lines.append("- **Auto Format**: true")
        lines.append("")

    # Coding Style
    cs = profile.coding_style
    if any([cs.indent_style, cs.indent_size, cs.quote_style, cs.naming_convention,
            cs.semicolons, cs.trailing_comma, cs.max_line_length]):
        lines.append("## Coding Style")
        if cs.indent_style:
            lines.append(f"- **Indent Style**: {cs.indent_style}")
        if cs.indent_size:
            lines.append(f"- **Indent Size**: {cs.indent_size}")
        if cs.quote_style:
            lines.append(f"- **Quote Style**: {cs.quote_style}")
        if cs.semicolons:
            lines.append("- **Semicolons**: true")
        if cs.trailing_comma:
            lines.append("- **Trailing Comma**: true")
        if cs.max_line_length:
            lines.append(f"- **Max Line Length**: {cs.max_line_length}")
        if cs.naming_convention:
            lines.append(f"- **Naming Convention**: {cs.naming_convention}")
        lines.append("")

    # Common Patterns
    if profile.common_patterns:
        lines.append("## Common Patterns")
        for pattern in profile.common_patterns:
            lines.append(f"- {pattern}")
        lines.append("")

    # Project Context
    if profile.project_context:
        lines.append("## Project Context")
        lines.append(profile.project_context)
        lines.append("")

    # Custom Instructions
    if profile.custom_instructions:
        lines.append("## Custom Instructions")
        lines.append(profile.custom_instructions)
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 画像管理器
# ---------------------------------------------------------------------------

class UserProfileManager:
    """统一管理 USER.md 画像（合并 global + project 作用域）。"""

    def __init__(self, cwd: str | Path | None = None):
        from minicode.config import MINI_CODE_DIR
        self._global_path = MINI_CODE_DIR / "USER.md"
        self._project_path = Path(cwd or Path.cwd()) / ".mini-code" / "USER.md"

    @property
    def global_path(self) -> Path:
        return self._global_path

    @property
    def project_path(self) -> Path:
        return self._project_path

    def load_global(self) -> Optional[UserProfile]:
        """从 ``~/.mini-code/USER.md`` 加载全局画像。"""
        return self._load_from(self._global_path)

    def load_project(self) -> Optional[UserProfile]:
        """从 ``.mini-code/USER.md`` 加载项目画像。"""
        return self._load_from(self._project_path)

    def load_merged(self) -> UserProfile:
        """加载并合并 global + project 画像，project 覆盖 global。"""
        global_profile = self.load_global()
        project_profile = self.load_project()

        if global_profile is None and project_profile is None:
            return UserProfile()
        if global_profile is None:
            return project_profile  # type: ignore[return-value]
        if project_profile is None:
            return global_profile

        return self._merge_profiles(global_profile, project_profile)

    def save_global(self, profile: UserProfile) -> None:
        """保存到全局路径。"""
        self._save_to(self._global_path, profile)

    def save_project(self, profile: UserProfile) -> None:
        """保存到项目路径。"""
        self._save_to(self._project_path, profile)

    def to_prompt_section(self, profile: UserProfile) -> str:
        """把 profile 转换为可注入到 system prompt 的段落。"""
        parts: list[str] = ["## User Profile", ""]

        p = profile.preferences
        prefs = []
        if p.language:
            prefs.append(f"Language: {p.language}")
        if p.verbosity:
            prefs.append(f"Verbosity: {p.verbosity}")
        if p.response_style:
            prefs.append(f"Response style: {p.response_style}")
        if p.preferred_framework:
            prefs.append(f"Preferred framework: {p.preferred_framework}")
        if p.preferred_test_framework:
            prefs.append(f"Preferred test framework: {p.preferred_test_framework}")
        if p.auto_format:
            prefs.append("Auto-format on edit: yes")
        if prefs:
            parts.append("Preferences: " + ", ".join(prefs))

        cs = profile.coding_style
        style = []
        if cs.indent_style:
            style.append(f"indent: {cs.indent_style}" + (f" ({cs.indent_size})" if cs.indent_size else ""))
        if cs.quote_style:
            style.append(f"quotes: {cs.quote_style}")
        if cs.naming_convention:
            style.append(f"naming: {cs.naming_convention}")
        if cs.max_line_length:
            style.append(f"max line: {cs.max_line_length}")
        if style:
            parts.append("Coding style: " + ", ".join(style))

        if profile.common_patterns:
            parts.append("Common patterns: " + "; ".join(profile.common_patterns[:5]))

        if profile.project_context:
            parts.append(f"Project context: {profile.project_context[:200]}")

        if profile.custom_instructions:
            parts.append(f"Custom instructions: {profile.custom_instructions[:300]}")

        if len(parts) <= 2:
            return ""  # No meaningful content

        return "\n".join(parts)

    def search_preferences(self, profile: UserProfile, query: str) -> list[str]:
        """在 profile 中搜索包含 query 的偏好项。"""
        query_lower = query.lower()
        matches: list[str] = []

        # 检查 preferences
        for attr in ["language", "verbosity", "response_style",
                     "preferred_framework", "preferred_test_framework"]:
            val = getattr(profile.preferences, attr, "")
            if val and query_lower in val.lower():
                matches.append(f"preference.{attr} = {val}")

        # 检查 coding style
        for attr in ["indent_style", "quote_style", "naming_convention"]:
            val = getattr(profile.coding_style, attr, "")
            if val and query_lower in val.lower():
                matches.append(f"coding_style.{attr} = {val}")

        # 检查 patterns
        for pattern in profile.common_patterns:
            if query_lower in pattern.lower():
                matches.append(f"pattern: {pattern}")

        # 检查自由文本字段
        for text, label in [
            (profile.project_context, "project_context"),
            (profile.custom_instructions, "custom_instructions"),
        ]:
            if text and query_lower in text.lower():
                matches.append(f"{label}: (matched)")

        return matches

    # -----------------------------------------------------------------------
    # 内部辅助方法
    # -----------------------------------------------------------------------

    @staticmethod
    def _load_from(path: Path) -> Optional[UserProfile]:
        """从指定路径加载 profile。"""
        if not path.exists() or not path.is_file():
            return None
        try:
            content = path.read_text(encoding="utf-8")
            profile = parse_user_md(content)
            profile.source_path = str(path)
            return profile
        except Exception:
            return None

    @staticmethod
    def _save_to(path: Path, profile: UserProfile) -> None:
        """保存 profile 到指定路径。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        content = serialize_user_md(profile)
        path.write_text(content, encoding="utf-8")

    @staticmethod
    def _merge_profiles(global_p: UserProfile, project_p: UserProfile) -> UserProfile:
        """合并 global 与 project 画像（project 覆盖 global）。"""
        merged = UserProfile()

        # 合并 preferences（project 非空值优先）
        gp, pp, mp = global_p.preferences, project_p.preferences, merged.preferences
        for attr in ["language", "verbosity", "response_style",
                     "preferred_framework", "preferred_test_framework"]:
            setattr(mp, attr, getattr(pp, attr, "") or getattr(gp, attr, ""))
        mp.auto_format = pp.auto_format or gp.auto_format

        # 合并 coding style
        gcs, pcs, mcs = global_p.coding_style, project_p.coding_style, merged.coding_style
        for attr in ["indent_style", "quote_style", "naming_convention"]:
            setattr(mcs, attr, getattr(pcs, attr, "") or getattr(gcs, attr, ""))
        for attr in ["indent_size", "max_line_length"]:
            setattr(mcs, attr, getattr(pcs, attr, 0) or getattr(gcs, attr, 0))
        mcs.semicolons = pcs.semicolons or gcs.semicolons
        mcs.trailing_comma = pcs.trailing_comma or gcs.trailing_comma

        # 合并列表（去重）
        seen: set[str] = set()
        for pattern in global_p.common_patterns + project_p.common_patterns:
            if pattern not in seen:
                merged.common_patterns.append(pattern)
                seen.add(pattern)

        # 自由文本：project 覆盖 global
        merged.project_context = project_p.project_context or global_p.project_context
        merged.custom_instructions = project_p.custom_instructions or global_p.custom_instructions

        # 来源信息
        merged.source_path = f"{global_p.source_path} + {project_p.source_path}"

        return merged


# ---------------------------------------------------------------------------
# CLI 命令处理
# ---------------------------------------------------------------------------

def handle_user_command(args: str, cwd: str | Path | None = None) -> str:
    """处理 ``/user`` 子命令。

    支持的子命令：
        /user           — 展示合并后的画像摘要
        /user global    — 展示全局画像
        /user project   — 展示项目画像
        /user paths     — 展示画像文件路径
        /user reset     — 重置（删除）项目画像
        /user reset-global — 重置（删除）全局画像
        /user set <key> <value> — 设置某项偏好（点路径，如 preferences.language）
        /user search <query> — 搜索画像中匹配的偏好
    """
    manager = UserProfileManager(cwd)
    parts = args.strip().split(maxsplit=1)
    subcmd = parts[0] if parts else ""
    subcmd_args = parts[1] if len(parts) > 1 else ""

    if not subcmd or subcmd == "show":
        # Show merged profile
        profile = manager.load_merged()
        prompt_section = manager.to_prompt_section(profile)
        if not prompt_section:
            return "No user profile configured. Create ~/.mini-code/USER.md or .mini-code/USER.md"
        source = profile.source_path or "none"
        return f"{prompt_section}\n\nSource: {source}"

    if subcmd == "global":
        profile = manager.load_global()
        if profile is None:
            return f"No global profile found at {manager.global_path}"
        return f"Global Profile ({manager.global_path})\n\n{manager.to_prompt_section(profile)}"

    if subcmd == "project":
        profile = manager.load_project()
        if profile is None:
            return f"No project profile found at {manager.project_path}"
        return f"Project Profile ({manager.project_path})\n\n{manager.to_prompt_section(profile)}"

    if subcmd == "paths":
        return "\n".join([
            f"Global:  {manager.global_path} ({'exists' if manager.global_path.exists() else 'not found'})",
            f"Project: {manager.project_path} ({'exists' if manager.project_path.exists() else 'not found'})",
        ])

    if subcmd == "reset":
        if not manager.project_path.exists():
            return f"No project profile to reset at {manager.project_path}"
        manager.project_path.unlink()
        return f"Deleted project profile: {manager.project_path}"

    if subcmd == "reset-global":
        if not manager.global_path.exists():
            return f"No global profile to reset at {manager.global_path}"
        manager.global_path.unlink()
        return f"Deleted global profile: {manager.global_path}"

    if subcmd == "set":
        return _handle_user_set(subcmd_args, manager)

    if subcmd == "search":
        profile = manager.load_merged()
        results = manager.search_preferences(profile, subcmd_args)
        if not results:
            return f"No preferences matching '{subcmd_args}'"
        return "\n".join(f"  - {r}" for r in results)

    return (
        f"Unknown /user subcommand: {subcmd}\n"
        "Available: show, global, project, paths, reset, reset-global, set, search"
    )


def _handle_user_set(args: str, manager: UserProfileManager) -> str:
    """处理 ``/user set <key> <value>``。"""
    parts = args.strip().split(maxsplit=1)
    if len(parts) < 2:
        return "Usage: /user set <key> <value>\nKeys: preferences.language, preferences.verbosity, etc."
    key, value = parts[0].strip(), parts[1].strip()

    # 决定作用域：``project.`` 前缀写入项目画像，否则写入全局
    scope = "global"
    if key.startswith("project."):
        key = key[len("project."):]
        scope = "project"

    # 加载已有画像
    if scope == "project":
        profile = manager.load_project() or UserProfile()
    else:
        profile = manager.load_global() or UserProfile()

    # 应用本次设置
    changed = _apply_setting(profile, key, value)
    if not changed:
        return f"Unknown profile key: {key}\nValid keys: preferences.*, coding_style.*, project_context, custom_instructions"

    # 保存
    if scope == "project":
        manager.save_project(profile)
        return f"Set {key} = {value} in project profile ({manager.project_path})"
    else:
        manager.save_global(profile)
        return f"Set {key} = {value} in global profile ({manager.global_path})"


def _apply_setting(profile: UserProfile, key: str, value: str) -> bool:
    """将单个设置写入 profile，返回是否找到合法 key。"""
    # Preferences
    pref_keys = {
        "preferences.language": "language",
        "preferences.verbosity": "verbosity",
        "preferences.response_style": "response_style",
        "preferences.preferred_framework": "preferred_framework",
        "preferences.preferred_test_framework": "preferred_test_framework",
    }
    if key in pref_keys:
        setattr(profile.preferences, pref_keys[key], value)
        return True
    if key == "preferences.auto_format":
        profile.preferences.auto_format = value.lower() in ("true", "yes", "1")
        return True

    # Coding style
    style_keys = {
        "coding_style.indent_style": "indent_style",
        "coding_style.quote_style": "quote_style",
        "coding_style.naming_convention": "naming_convention",
    }
    if key in style_keys:
        setattr(profile.coding_style, style_keys[key], value)
        return True

    int_keys = {
        "coding_style.indent_size": "indent_size",
        "coding_style.max_line_length": "max_line_length",
    }
    if key in int_keys:
        try:
            setattr(profile.coding_style, int_keys[key], int(value))
            return True
        except ValueError:
            return False

    bool_keys = {
        "coding_style.semicolons": "semicolons",
        "coding_style.trailing_comma": "trailing_comma",
    }
    if key in bool_keys:
        setattr(profile.coding_style, bool_keys[key], value.lower() in ("true", "yes", "1"))
        return True

    # Free text
    if key == "project_context":
        profile.project_context = value
        return True
    if key == "custom_instructions":
        profile.custom_instructions = value
        return True

    return False
