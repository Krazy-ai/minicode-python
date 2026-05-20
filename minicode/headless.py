"""MiniCode 的无头执行模式 —— 非交互式、单次执行。

灵感来自 Hermes Agent 的 headless 模式，适合 CI/CD 流水线和自动化工作流。

使用方式：
    # 一次性跑一个 prompt 然后退出
    python -m minicode.headless "帮我分析这个项目的结构"

    # 通过管道传入
    echo "解释这段代码" | python -m minicode.headless

    # Docker 内
    docker compose run --rm headless "修复这个 bug"
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def run_headless(prompt: str | None = None) -> str:
    """以无头模式跑一轮 agent 并返回最终回答。

    Args:
        prompt: 要发送的用户消息。传 None 时从 stdin 读取。

    Returns:
        assistant 的最终回答文本。出错则返回 "Error: ..."；没有任何回答时返回 "(no response)"。
    """
    from minicode.agent.agent_loop import run_agent_turn
    from minicode.config import load_runtime_config
    from minicode.memory.memory import MemoryManager
    from minicode.model.model_registry import create_model_adapter
    from minicode.security.permissions import PermissionManager
    from minicode.prompt.prompt import build_system_prompt
    from minicode.tools import create_default_tool_registry
    from minicode.tooling import ToolContext
    from minicode.runtime.logging_config import setup_logging, get_logger

    setup_logging(level=os.environ.get("MINI_CODE_LOG_LEVEL", "WARNING"))
    logger = get_logger("headless")

    # 未传 prompt 时，尝试从 stdin 读取（要求是管道输入而非交互终端）
    if prompt is None:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            print("Usage: python -m minicode.headless <prompt>", file=sys.stderr)
            sys.exit(1)

    if not prompt:
        print("Error: empty prompt", file=sys.stderr)
        sys.exit(1)

    cwd = str(Path.cwd())

    # 加载配置（无配置时直接退出，无法降级——headless 不带 mock 兜底）
    try:
        runtime = load_runtime_config(cwd)
    except Exception as exc:  # noqa: BLE001
        print(f"Config error: {exc}", file=sys.stderr)
        sys.exit(1)

    # 初始化各组件（注意：headless 不带 prompt handler，权限请求会按默认策略处理）
    tools = create_default_tool_registry(cwd, runtime=runtime)
    permissions = PermissionManager(cwd, prompt=None)
    memory_mgr = MemoryManager(project_root=Path(cwd))

    model = create_model_adapter(
        model=runtime.get("model", ""),
        tools=tools,
        runtime=runtime,
    )

    messages = [
        {
            "role": "system",
            "content": build_system_prompt(
                cwd,
                permissions.get_summary(),
                {
                    "skills": tools.get_skills(),
                    "mcpServers": tools.get_mcp_servers(),
                    "memory_context": memory_mgr.get_relevant_context(),
                },
            ),
        },
        {"role": "user", "content": prompt},
    ]

    logger.info("Headless run: %s", prompt[:80])

    try:
        result_messages = run_agent_turn(
            model=model,
            tools=tools,
            messages=messages,
            cwd=cwd,
            permissions=permissions,
        )

        # 取最后一条 assistant 消息作为最终回答
        last_assistant = next(
            (m for m in reversed(result_messages) if m["role"] == "assistant"),
            None,
        )
        return last_assistant["content"] if last_assistant else "(no response)"

    except Exception as exc:  # noqa: BLE001
        logger.error("Headless error: %s", exc)
        return f"Error: {exc}"
    finally:
        # 必须释放工具资源（关闭 MCP 子进程等），否则进程不会干净退出
        try:
            tools.dispose()
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    """无头模式的 CLI 入口（被 console_scripts 中的 ``minicode-headless`` 调用）。"""
    # 从命令行参数或 stdin 拿到 prompt
    prompt = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    response = run_headless(prompt)
    print(response)


if __name__ == "__main__":
    main()
