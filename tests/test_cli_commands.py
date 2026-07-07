from minicode.cli_commands import find_matching_slash_commands, format_slash_commands, try_handle_local_command
from minicode.local_tool_shortcuts import parse_local_tool_shortcut


def test_find_matching_slash_commands_returns_help_variants() -> None:
    matches = find_matching_slash_commands("/mo")
    assert "/model" in matches
    assert "/model <model-name>" in matches


def test_parse_local_tool_shortcut_parses_cmd() -> None:
    shortcut = parse_local_tool_shortcut("/cmd src::git status")
    assert shortcut == {
        "toolName": "run_command",
        "input": {"command": "git status", "cwd": "src"},
    }


def test_parse_local_tool_shortcut_parses_patch_pairs() -> None:
    shortcut = parse_local_tool_shortcut("/patch demo.txt::hello::hi::world::earth")
    assert shortcut == {
        "toolName": "patch_file",
        "input": {
            "path": "demo.txt",
            "replacements": [
                {"search": "hello", "replace": "hi"},
                {"search": "world", "replace": "earth"},
            ],
        },
    }


def test_format_slash_commands_includes_permissions() -> None:
    assert "/permissions" in format_slash_commands()


def test_format_slash_commands_describes_patch_replacements() -> None:
    commands = format_slash_commands()
    # 检查格式化后的帮助信息包含关键命令
    assert "/patch" in commands
    assert "replacements" in commands or "multiple" in commands


def test_format_slash_commands_includes_history_and_retry() -> None:
    commands = format_slash_commands()
    assert "/history" in commands
    assert "/retry" in commands


def test_memory_command_uses_current_workspace(tmp_path) -> None:
    result = try_handle_local_command("/memory", cwd=str(tmp_path))

    assert result is not None
    assert "Memory System Status" in result


def test_ask_without_index_reports_missing(tmp_path) -> None:
    result = try_handle_local_command("/ask what is auth", cwd=str(tmp_path))
    assert result is not None
    assert "No knowledge base index" in result


def test_ask_empty_question_shows_usage(tmp_path) -> None:
    result = try_handle_local_command("/ask", cwd=str(tmp_path))
    assert result is not None
    assert "Usage: /ask" in result


def test_ask_returns_context_when_model_unavailable(tmp_path, monkeypatch) -> None:
    # 建一个索引
    from minicode.knowledge.pipeline import ingest

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "auth.md").write_text(
        "# Authentication\n\nLogin uses OAuth tokens to verify credentials.\n",
        encoding="utf-8",
    )
    ingest(docs, index_name="default", scope="project", cwd=str(tmp_path))

    # 强制模型不可用 → 降级返回检索内容
    import minicode.cli_commands as cli

    monkeypatch.setattr(cli, "_try_model_answer", lambda q, c, cwd: None)
    result = try_handle_local_command("/ask how does login work", cwd=str(tmp_path))
    assert result is not None
    assert "Sources:" in result
    assert "auth.md" in result


def test_knowledge_command_lists_indexes(tmp_path) -> None:
    from minicode.knowledge.pipeline import ingest

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("# A\n\nalpha content here", encoding="utf-8")
    ingest(docs, index_name="default", scope="project", cwd=str(tmp_path))

    result = try_handle_local_command("/knowledge", cwd=str(tmp_path))
    assert result is not None
    assert "Knowledge base indexes" in result
    assert "default" in result


def test_knowledge_command_empty(tmp_path) -> None:
    result = try_handle_local_command("/knowledge", cwd=str(tmp_path))
    assert result is not None
    assert "No knowledge base indexes" in result
