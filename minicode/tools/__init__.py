"""tools 子包：所有内置工具的实现。

每个工具暴露 description（发给 LLM）、input_schema 与执行函数，
通过 ToolRegistry 注册并被 agent loop 调度。
"""

from dataclasses import asdict
import os

from minicode.mcp import create_mcp_backed_tools
from minicode.prompt.skills import discover_skills
from minicode.tooling import ToolRegistry
from minicode.tools.ask_user import ask_user_tool
from minicode.tools.batch_ops import batch_copy_tool, batch_move_tool, batch_delete_tool
from minicode.tools.code_nav import find_symbols_tool, find_references_tool, get_ast_info_tool
from minicode.tools.code_review import code_review_tool
from minicode.tools.diff_viewer import diff_viewer_tool
from minicode.tools.edit_file import edit_file_tool
from minicode.tools.file_tree import file_tree_tool
from minicode.tools.git import git_tool
from minicode.tools.grep_files import grep_files_tool
from minicode.tools.list_files import list_files_tool
from minicode.tools.load_skill import create_load_skill_tool
from minicode.tools.modify_file import modify_file_tool
from minicode.tools.patch_file import patch_file_tool
from minicode.tools.read_file import read_file_tool
from minicode.tools.run_command import run_command_tool
from minicode.tools.test_runner import test_runner_tool
from minicode.tools.todo_write import todo_write_tool
from minicode.tools.web_fetch import web_fetch_tool
from minicode.tools.web_search import web_search_tool
from minicode.tools.write_file import write_file_tool
from minicode.tools.task import task_tool


_CORE_TOOLS = [
    # 用户交互
    ask_user_tool,
    # 文件操作
    list_files_tool,
    grep_files_tool,
    read_file_tool,
    write_file_tool,
    modify_file_tool,
    edit_file_tool,
    patch_file_tool,
    # 批量操作
    batch_copy_tool,
    batch_move_tool,
    batch_delete_tool,
    # 命令执行
    run_command_tool,
    # 网络工具
    web_fetch_tool,
    web_search_tool,
    # 任务管理
    todo_write_tool,
    # 子 agent
    task_tool,
    # Git 工作流
    git_tool,
    # 代码智能
    find_symbols_tool,
    find_references_tool,
    get_ast_info_tool,
    code_review_tool,
    # 可视化
    file_tree_tool,
    diff_viewer_tool,
    # 测试
    test_runner_tool,
]

def _resolve_tool_profile(runtime: dict | None) -> str:
    """从环境变量或 runtime 中解析工具集类型。"""
    configured = (
        os.environ.get("MINI_CODE_TOOL_PROFILE")
        or (runtime or {}).get("toolProfile")
        or "core"
    )
    return str(configured).strip().lower()


def _is_full_tool_profile(profile: str) -> bool:
    """判断是否启用完整工具集。"""
    return profile in {"full", "utility", "utilities", "all"}


def _load_utility_wrapper_tools():
    """惰性加载工具集：避免日常 coding 场景为不常用工具支付导入开销，
    同时保持默认暴露给模型的工具数量较少。"""
    from minicode.tools.archive_utils import (
        gzip_compress_tool, gzip_decompress_tool, tar_create_tool, tar_extract_tool,
        zip_create_tool, zip_extract_tool,
    )
    from minicode.tools.crypto_utils import current_time_tool, timestamp_tool, hash_tool, hmac_tool
    from minicode.tools.csv_utils import csv_parse_tool, csv_create_tool
    from minicode.tools.encoding_utils import base64_encode_tool, base64_decode_tool, url_encode_tool, url_decode_tool
    from minicode.tools.http_utils import http_request_tool
    from minicode.tools.json_utils import json_format_tool, json_parse_tool
    from minicode.tools.regex_utils import regex_test_tool, regex_replace_tool
    from minicode.tools.text_utils import (
        uuid_generate_tool, text_sort_tool, text_dedupe_tool, text_join_tool,
        line_count_tool, random_string_tool,
    )

    return [
        http_request_tool,
        json_format_tool,
        json_parse_tool,
        regex_test_tool,
        regex_replace_tool,
        base64_encode_tool,
        base64_decode_tool,
        url_encode_tool,
        url_decode_tool,
        current_time_tool,
        timestamp_tool,
        hash_tool,
        hmac_tool,
        gzip_compress_tool,
        gzip_decompress_tool,
        tar_create_tool,
        tar_extract_tool,
        zip_create_tool,
        zip_extract_tool,
        csv_parse_tool,
        csv_create_tool,
        uuid_generate_tool,
        text_sort_tool,
        text_dedupe_tool,
        text_join_tool,
        line_count_tool,
        random_string_tool,
    ]


def create_default_tool_registry(cwd: str, runtime: dict | None = None) -> ToolRegistry:
    """根据 runtime 配置创建默认的 ToolRegistry。

    会装载内置核心工具、按需的扩展工具集、MCP 工具
    以及 ``load_skill`` 工具。
    """
    skills = [asdict(skill) for skill in discover_skills(cwd)]
    mcp = create_mcp_backed_tools(cwd=cwd, mcp_servers=dict(runtime.get("mcpServers", {})) if runtime else {})
    profile = _resolve_tool_profile(runtime)
    tools = list(_CORE_TOOLS)
    if _is_full_tool_profile(profile):
        tools.extend(_load_utility_wrapper_tools())
    tools.extend(
        [
            create_load_skill_tool(cwd),
            *mcp["tools"],
        ]
    )
    return ToolRegistry(
        tools,
        skills=skills,
        mcp_servers=mcp["servers"],
        disposer=mcp["dispose"],
    )
