"""TUI 内部的斜杠命令处理。

提供两类能力：
    1. 命令元数据（``SLASH_COMMANDS`` 列表）—— 用于自动补全与帮助菜单
    2. 即时执行（``try_handle_local_command``）—— 不进 agent loop，直接返回
       一段字符串展示给用户（如 ``/help`` ``/status`` ``/mcp`` 等）

这里只处理 **本地** 命令；与"本地工具快捷调用"（``/grep`` ``/cmd`` 等）
分别由 ``local_tool_shortcuts.py`` 处理。

注：所有面向用户/LLM 的 ``description`` 字符串保留英文，以便 LLM 在工具
推理与命令补全展示中保持一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from minicode.config import (
    CLAUDE_SETTINGS_PATH,
    MINI_CODE_MCP_PATH,
    MINI_CODE_PERMISSIONS_PATH,
    MINI_CODE_SETTINGS_PATH,
    load_runtime_config,
    save_mini_code_settings,
)


@dataclass(frozen=True, slots=True)
class SlashCommand:
    """单条斜杠命令的元数据：命令名、用法、英文描述（用于补全菜单）。"""
    name: str
    usage: str
    description: str


SLASH_COMMANDS = [
    SlashCommand("/help", "/help", "Show available slash commands."),
    SlashCommand("/tools", "/tools", "List tools available to the coding agent and tool shortcuts."),
    SlashCommand("/state", "/state", "Show detailed application state and Store summary."),
    SlashCommand("/status", "/status", "Show application state summary and current model."),
    SlashCommand("/cost", "/cost [--detailed]", "Show API cost and usage report."),
    SlashCommand("/context", "/context", "Show context window usage."),
    SlashCommand("/tasks", "/tasks", "Show current task list."),
    SlashCommand("/memory", "/memory", "Show memory system status."),
    SlashCommand("/ask", "/ask <question>", "Ask the knowledge base a question (RAG, does not run tools)."),
    SlashCommand("/knowledge", "/knowledge", "Show knowledge base indexes and status."),
    SlashCommand("/config", "/config", "Show configuration diagnostics and validation."),
    SlashCommand("/history", "/history", "Show recent prompt history from ~/.mini-code/history.json."),
    SlashCommand("/clear", "/clear", "Clear the current transcript view."),
    SlashCommand("/retry", "/retry", "Retry the last natural-language prompt in this session."),
    SlashCommand("/transcript-save", "/transcript-save <path>", "Save the current session transcript to a text file."),
    SlashCommand("/model", "/model", "Show the current model."),
    SlashCommand("/model", "/model <model-name>", "Persist a model override into ~/.mini-code/settings.json."),
    SlashCommand("/config-paths", "/config-paths", "Show mini-code and Claude fallback settings paths."),
    SlashCommand("/skills", "/skills", "List discovered SKILL.md workflows."),
    SlashCommand("/mcp", "/mcp", "Show configured MCP servers and connection state."),
    SlashCommand("/permissions", "/permissions", "Show mini-code permission storage path."),
    SlashCommand("/exit", "/exit", "Exit mini-code."),
    SlashCommand("/debug", "/debug", "Show scroll and terminal diagnostics."),
    SlashCommand("/user", "/user", "Show or manage user profile (preferences, coding style)."),
    SlashCommand("/ls", "/ls [path]", "List files in a directory."),
    SlashCommand("/grep", "/grep <pattern>::[path]", "Search text in files."),
    SlashCommand("/read", "/read <path>", "Read a file directly."),
    SlashCommand("/write", "/write <path>::<content>", "Write a file directly."),
    SlashCommand("/modify", "/modify <path>::<content>", "Replace a file, showing a reviewable diff before applying it."),
    SlashCommand("/edit", "/edit <path>::<search>::<replace>", "Edit a file by exact replacement."),
    SlashCommand("/patch", "/patch <path>::<search1>::<replace1>::<search2>::<replace2>...", "Apply multiple replacements to one file in one command."),
    SlashCommand("/cmd", "/cmd [cwd::]<command> [args...]", "Run an allowed development command directly."),
]


def format_slash_commands() -> str:
    """生成 ``/help`` 输出的命令大全（带分组与边框，UI 直接展示）。"""
    lines = [
        "╔══════════════════════════════════════════════════════════╗",
        "║  📚 Available Commands                                  ║",
        "╠══════════════════════════════════════════════════════════╣",
    ]
    
    command_groups = {
        "🔧 Core Commands": [
            ("/help", "Show this help message"),
            ("/exit", "Exit mini-code"),
            ("/clear", "Clear the current transcript view"),
            ("/history", "Show recent prompt history"),
        ],
        "🛠️ Tool Commands": [
            ("/tools", "List all available tools"),
            ("/skills", "List discovered SKILL.md workflows"),
            ("/mcp", "Show MCP servers and connection state"),
            ("/cmd", "Run development commands directly"),
        ],
        "📊 Status & Info": [
            ("/status", "Show application state summary"),
            ("/model", "Show or change current model"),
            ("/user", "Show or manage user profile"),
            ("/cost", "Show API cost and usage report"),
            ("/context", "Show context window usage"),
            ("/tasks", "Show current task list"),
            ("/memory", "Show memory system status"),
        ],
        "✏️ File Operations": [
            ("/ls [path]", "List files in directory"),
            ("/grep <pattern>", "Search text in files"),
            ("/read <path>", "Read a file directly"),
            ("/write <path>", "Write content to file"),
            ("/edit <path>", "Edit file by exact replacement"),
            ("/patch <path>", "Apply multiple replacements in one go"),
            ("/modify <path>", "Replace file with reviewable diff"),
        ],
        "💾 Session Management": [
            ("/transcript-save <path>", "Save transcript to text file"),
            ("/retry", "Retry the last prompt"),
            ("/permissions", "Show permission storage path"),
            ("/config-paths", "Show settings file paths"),
        ],
    }
    
    for group_name, commands in command_groups.items():
        lines.append(f"║  {group_name:<54}║")
        for cmd, desc in commands:
            cmd_display = f"    {cmd}"
            lines.append(f"║  {cmd_display:<20} {desc:<33} ║")
        lines.append("╠══════════════════════════════════════════════════════════╣")
    
    lines.extend([
        "║  💡 Tips:                                              ║",
        "║  - Use Tab to autocomplete commands                    ║",
        "║  - Prefix with / to access any command                 ║",
        "║  - Type naturally - I'll understand Chinese & English  ║",
        "╚══════════════════════════════════════════════════════════╝",
    ])
    
    return "\n".join(lines)


def find_matching_slash_commands(user_input: str) -> list[str]:
    """返回所有以 ``user_input`` 为前缀的命令用法（用于命令前缀过滤）。"""
    return [command.usage for command in SLASH_COMMANDS if command.usage.startswith(user_input)]


def complete_slash_command(line: str) -> tuple[list[str], str]:
    """命令补全：返回 ``(候选用法列表, 当前输入)``。无匹配时回退到全部命令。"""
    hits = [command.usage for command in SLASH_COMMANDS if command.usage.startswith(line)]
    return (hits if hits else [command.usage for command in SLASH_COMMANDS], line)


def try_handle_local_command(user_input: str, tools=None, cwd: str | None = None) -> str | None:
    """尝试把输入当作本地斜杠命令处理。

    Args:
        user_input: 用户输入的整行（含前导斜杠）
        tools: ToolRegistry，部分命令（``/skills`` ``/mcp``）需要从中读取信息
        cwd: 当前工作目录，部分命令（``/memory``）需要

    Returns:
        命中且执行成功 → 返回字符串结果（直接展示给用户）
        未命中 → 返回 ``None``（调用方应继续尝试其他处理路径）
    """
    if user_input in {"/", "/help"}:
        return format_slash_commands()

    if user_input == "/config-paths":
        return "\n".join(
            [
                f"mini-code settings: {MINI_CODE_SETTINGS_PATH}",
                f"mini-code permissions: {MINI_CODE_PERMISSIONS_PATH}",
                f"mini-code mcp: {MINI_CODE_MCP_PATH}",
                f"compat fallback: {CLAUDE_SETTINGS_PATH}",
            ]
        )

    if user_input == "/permissions":
        return f"permission store: {MINI_CODE_PERMISSIONS_PATH}"

    if user_input == "/skills":
        skills = tools.get_skills() if tools else []
        if not skills:
            return "No skills discovered. Add skills under ~/.mini-code/skills/<name>/SKILL.md, .mini-code/skills/<name>/SKILL.md, .claude/skills/<name>/SKILL.md, or ~/.claude/skills/<name>/SKILL.md."
        return "\n".join(
            f"{skill['name']}  {skill['description']}  [{skill['source']}]"
            for skill in skills
        )

    if user_input == "/config":
        from minicode.config import format_config_diagnostic
        return format_config_diagnostic()

    if user_input == "/state":
        try:
            from minicode.runtime.state import handle_state_command
            return handle_state_command()
        except ImportError:
            return "State system not available. Please ensure state.py exists."

    if user_input == "/memory":
        # 展示三层记忆系统的统计信息
        try:
            from minicode.memory.memory import MemoryManager
            from pathlib import Path
            memory_mgr = MemoryManager(project_root=Path(cwd) if cwd else Path.cwd())
            return memory_mgr.format_stats()
        except Exception as e:
            return f"Error loading memory: {e}"

    if user_input == "/context":
        # 展示 context window 占用情况
        try:
            from minicode.memory.context_manager import load_context_state
            ctx_mgr = load_context_state()
            if ctx_mgr:
                return ctx_mgr.format_context_details()
            else:
                return "No context state available. Context tracking starts after first turn."
        except Exception as e:
            return f"Error loading context: {e}"

    if user_input == "/mcp":
        servers = tools.get_mcp_servers() if tools else []
        if not servers:
            return "No MCP servers configured. Add mcpServers to ~/.mini-code/settings.json, ~/.mini-code/mcp.json, or project .mcp.json."
        lines = []
        for server in servers:
            suffix = f"  error={server['error']}" if server.get("error") else ""
            protocol = f"  protocol={server['protocol']}" if server.get("protocol") else ""
            resources = f"  resources={server['resourceCount']}" if server.get("resourceCount") is not None else ""
            prompts = f"  prompts={server['promptCount']}" if server.get("promptCount") is not None else ""
            lines.append(
                f"{server['name']}  status={server['status']}  tools={server['toolCount']}{resources}{prompts}{protocol}{suffix}"
            )
        return "\n".join(lines)

    if user_input == "/status":
        try:
            runtime = load_runtime_config()
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"
        from minicode.model.model_registry import detect_provider
        provider = detect_provider(runtime["model"], runtime)
        auth_methods = []
        if runtime.get("authToken"):
            auth_methods.append("ANTHROPIC_AUTH_TOKEN")
        if runtime.get("apiKey"):
            auth_methods.append("ANTHROPIC_API_KEY")
        if runtime.get("openaiApiKey"):
            auth_methods.append("OPENAI_API_KEY")
        if runtime.get("openrouterApiKey"):
            auth_methods.append("OPENROUTER_API_KEY")
        if runtime.get("customApiKey"):
            auth_methods.append("CUSTOM_API_KEY")
        return "\n".join(
            [
                f"model: {runtime['model']}",
                f"provider: {provider.value}",
                f"baseUrl: {runtime['baseUrl']}",
                f"auth: {', '.join(auth_methods) or 'none'}",
                f"mcp servers: {len(runtime.get('mcpServers', {}))}",
                runtime["sourceSummary"],
            ]
        )

    if user_input == "/model":
        try:
            runtime = load_runtime_config()
            from minicode.model.model_registry import format_model_status
            return format_model_status(runtime["model"], runtime)
        except Exception as error:  # noqa: BLE001
            return f"runtime not configured: {error}"

    if user_input.startswith("/model "):
        arg = user_input[len("/model "):].strip()
        if not arg:
            from minicode.model.model_registry import format_model_list
            return format_model_list()
        # 子命令
        if arg in ("status", "info"):
            try:
                runtime = load_runtime_config()
                from minicode.model.model_registry import format_model_status
                return format_model_status(runtime["model"], runtime)
            except Exception as error:  # noqa: BLE001
                return f"runtime not configured: {error}"
        if arg in ("list", "ls"):
            from minicode.model.model_registry import format_model_list
            return format_model_list()
        # 按 provider 过滤：/model anthropic, /model openrouter ...
        from minicode.model.model_registry import Provider, format_model_list
        for p in Provider:
            if arg.lower() == p.value:
                return format_model_list(provider=p)
        # 否则视为新模型名并保存
        save_mini_code_settings({"model": arg})
        return f"saved model={arg} to {MINI_CODE_SETTINGS_PATH}\nRestart MiniCode for the change to take effect."

    if user_input == "/user" or user_input.startswith("/user "):
        from minicode.prompt.user_profile import handle_user_command
        args = user_input[len("/user"):].strip()
        return handle_user_command(args)

    if user_input == "/knowledge" or user_input.startswith("/knowledge "):
        return _handle_knowledge_command(user_input, cwd=cwd)

    if user_input == "/ask" or user_input.startswith("/ask "):
        question = user_input[len("/ask"):].strip()
        return handle_ask_command(question, cwd=cwd)

    return None


def _handle_knowledge_command(user_input: str, cwd: str | None = None) -> str:
    """展示知识库索引清单与默认索引状态。"""
    try:
        from pathlib import Path

        from minicode.knowledge import pipeline

        base_cwd = cwd or str(Path.cwd())
        indexes = pipeline.list_indexes(base_cwd)
        if not indexes:
            return (
                "No knowledge base indexes found.\n"
                "Build one with the knowledge_ingest tool, e.g. ask me to "
                "\"index the ./docs directory\"."
            )
        lines = ["Knowledge base indexes:"]
        for i in indexes:
            info = pipeline.status(i["name"], scope=i["scope"], cwd=base_cwd)
            lines.append(
                f"  - {i['name']} (scope={i['scope']}): "
                f"{info.get('documents', 0)} docs, {info.get('chunks', 0)} chunks"
            )
        lines.append("")
        lines.append("Use /ask <question> to query, or the knowledge_query tool.")
        return "\n".join(lines)
    except Exception as e:  # noqa: BLE001
        return f"Error reading knowledge base: {e}"


def handle_ask_command(question: str, cwd: str | None = None) -> str:
    """处理 ``/ask <question>``：检索知识库并（若可用）让模型基于检索结果作答。

    流程：
        1. ``pipeline.query`` 拿到最相关 chunks
        2. 拼装 ``<context>...</context>`` prompt
        3. 透传给当前 model adapter 一次性生成（不走 agent loop、不调工具）
        4. 输出答案 + 折叠的引用清单

    模型不可用（离线/未配置）时优雅降级：直接返回检索到的片段与引用。
    """
    from pathlib import Path

    from minicode.knowledge import pipeline

    question = (question or "").strip()
    if not question:
        return "Usage: /ask <question>"

    base_cwd = cwd or str(Path.cwd())

    if not pipeline.has_any_index(base_cwd):
        return (
            "No knowledge base index found. Build one first with the "
            "knowledge_ingest tool (e.g. ask me to index the ./docs directory)."
        )

    result = pipeline.query(question, top_k=5, cwd=base_cwd)
    if not result:
        return f"No relevant information found in the knowledge base for: {question}"

    citations = result.citations()
    citation_block = "\n".join(
        f"  [{i}] {c.format_ref()}" for i, c in enumerate(citations, start=1)
    )

    context_text = result.format_context()
    answer = _try_model_answer(question, context_text, base_cwd)

    if answer is None:
        # 降级：直接返回检索内容
        return (
            f"Q: {question}\n\n"
            f"(model unavailable — showing retrieved context)\n\n"
            f"{context_text}\n\n"
            f"Sources:\n{citation_block}"
        )

    return f"{answer}\n\nSources:\n{citation_block}"


def _try_model_answer(question: str, context_text: str, cwd: str) -> str | None:
    """尝试用当前模型基于检索上下文作答；失败返回 None（触发降级）。"""
    try:
        from minicode.model.model_registry import create_model_adapter

        runtime = load_runtime_config(cwd)
        adapter = create_model_adapter(runtime["model"], tools=None, runtime=runtime)

        system_prompt = (
            "You are a helpful assistant answering questions strictly based on the "
            "provided knowledge base context. If the context does not contain the "
            "answer, say so honestly. Cite the numbered sources like [1], [2] when "
            "relevant. Do not use tools."
        )
        user_prompt = (
            f"<context>\n{context_text}\n</context>\n\n"
            f"Answer this question based only on the context above:\n{question}"
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        step = adapter.next(messages)
        content = getattr(step, "content", None)
        if not content or not str(content).strip():
            return None
        text = str(content).strip()
        # 去掉可能的 <progress>/<final> 协议前缀
        for marker in ("<final>", "</final>", "<progress>", "</progress>"):
            text = text.replace(marker, "")
        return text.strip()
    except Exception:  # noqa: BLE001 - 离线/未配置模型时降级
        return None
