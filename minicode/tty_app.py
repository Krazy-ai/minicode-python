"""MiniCode 的 TTY 全屏应用。

实现完整的终端 UI 主循环，包括：
    - 实时 transcript 渲染（含工具输出折叠展示）
    - 交互式权限审批弹窗
    - 后台 agent 线程管理
    - 键盘事件解析与命令路由
    - 会话持久化与自动保存

入口函数 ``run_tty_app`` 由 main.py 在 stdin 是 TTY 时调用。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Callable

from minicode.security.permissions import PermissionManager
from minicode.tooling import ToolRegistry
from minicode.tui.chrome import _cached_terminal_size
from minicode.tui.input_parser import (
    KeyEvent,
    ParsedInputEvent,
    TextEvent,
    parse_input_chunk,
)
from minicode.tui.types import TranscriptEntry
from minicode.types import ChatMessage, ModelAdapter

# ---------------------------------------------------------------------------
from minicode.tui.state import TtyAppArgs, ScreenState
from minicode.tui.tool_helpers import _summarize_collapsed_tool_body, _summarize_tool_input, _apply_tool_result_visual_state as _shared_apply_tool_result_visual_state, _mark_unfinished_tools as _shared_mark_unfinished_tools, _save_transcript as _shared_save_transcript
from minicode.tui.event_flow import _handle_event as _handle_tty_event
from minicode.tui.runtime_control import _ThrottledRenderer, enter_tty_runtime, exit_tty_runtime, install_sigwinch_rerender
from minicode.tui.session_flow import handle_session_listing, load_or_create_session, build_tty_runtime_state, install_permission_prompt, finalize_tty_session
from minicode.tui.renderer import _render_screen
from minicode.tui.input_handler import _RawModeContext, _handle_input

# 终端尺寸 —— 复用 chrome 模块的统一缓存
# ---------------------------------------------------------------------------

# 给 chrome.py 中规范实现起一个别名
_get_terminal_size = _cached_terminal_size


# ---------------------------------------------------------------------------
# 主事件驱动 TTY 应用
# ---------------------------------------------------------------------------


def run_tty_app(
    *,
    runtime: dict | None,
    tools: ToolRegistry,
    model: ModelAdapter,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager,
    resume_session: str | None = None,
    list_sessions_only: bool = False,
    memory_manager: Any | None = None,
    context_manager: Any | None = None,
) -> list[ChatMessage]:
    """事件驱动的全屏 TTY 应用，从 TypeScript 版本移植。

    Args:
        resume_session: 要恢复的 session ID；传 "latest" 表示恢复最近一次
        list_sessions_only: 若为 True，仅打印会话列表后返回，不进入 UI
    """

    if handle_session_listing(cwd, list_sessions_only):
        return messages

    session = load_or_create_session(cwd, resume_session)
    args, state = build_tty_runtime_state(
        runtime,
        tools,
        model,
        messages,
        cwd,
        permissions,
        session,
        memory_manager,
        context_manager,
    )

    # 节流渲染器：合并短时间内多次的 rerender() 请求，降低闪烁与 CPU 占用
    throttled = _ThrottledRenderer(lambda: _render_screen(args, state), min_interval=0.016)

    def rerender() -> None:
        throttled.request()

    approval_event, approval_result, _ = install_permission_prompt(args, state, rerender)

    input_remainder = ""
    should_exit = False
    # 自动保存的节流：每 ~2 秒检查一次（而不是每 20ms）
    _autosave_counter = 0
    _AUTOSAVE_CHECK_INTERVAL = 100  # 迭代次数（按 20ms 轮询折算约 2 秒）

    enter_tty_runtime()

    # Unix 上监听 SIGWINCH，使终端尺寸变化能立即反映，
    # 而不必等到 0.5 秒缓存 TTL 失效。
    # signal.signal() 只能在主线程调用。
    _prev_sigwinch = install_sigwinch_rerender(throttled)

    try:
        _render_screen(args, state)

        with _RawModeContext():
            while not should_exit:
                # 节流式自动保存
                _autosave_counter += 1
                if state.autosave and _autosave_counter >= _AUTOSAVE_CHECK_INTERVAL:
                    _autosave_counter = 0
                    state.autosave.save_if_needed()

                # 检查后台 agent 线程是否完成
                agent_result_data = state.agent_result
                lock = getattr(state, "agent_lock", None)
                if agent_result_data is not None and lock is not None and agent_result_data.get("done"):
                    with lock:
                        if agent_result_data.get("messages"):
                            args.messages = agent_result_data["messages"]
                        agent_result_data["done"] = False  # 重置标志位

                # 读取原始输入
                if sys.platform == "win32":
                    import msvcrt

                    if not msvcrt.kbhit():
                        # 空闲时把延迟的渲染冲掉
                        throttled.flush()
                        time.sleep(0.05)  # 从 0.02 增加到 0.05 以降低 CPU 使用率
                        continue
                    # 用 _win_read_one_key 翻译特殊按键
                    chunk = ""
                    while True:
                        ch = _win_read_one_key()
                        if not ch:
                            break
                        chunk += ch
                else:
                    import select

                    _fd = sys.stdin.fileno()
                    ready, _, _ = select.select([_fd], [], [], 0.05)
                    if not ready:
                        # 空闲时把延迟的渲染冲掉
                        throttled.flush()
                        continue
                    # 用 os.read() 绕过 Python 的 TextIOWrapper / BufferedReader：
                    # raw 模式下它们可能因为 UTF-8 序列被截断而阻塞。
                    _raw = os.read(_fd, 4096)
                    if not _raw:
                        should_exit = True
                        continue
                    # 非阻塞地把剩余字节都吸干净
                    while True:
                        ready2, _, _ = select.select([_fd], [], [], 0)
                        if not ready2:
                            break
                        _more = os.read(_fd, 4096)
                        if not _more:
                            break
                        _raw += _more
                    chunk = _raw.decode("utf-8", errors="replace")

                if not chunk:
                    continue

                parsed = parse_input_chunk(input_remainder + chunk)
                input_remainder = parsed.rest

                for event in parsed.events:
                    try:
                        _handle_tty_event(args, state, event, rerender, approval_event, approval_result, _handle_input)
                        if state.input == "/exit" or (
                            isinstance(event, KeyEvent)
                            and event.name == "c"
                            and event.ctrl
                        ):
                            raise SystemExit(0)
                    except SystemExit:
                        should_exit = True
                        break
                    except Exception as e:
                        # 记录事件处理错误，但不中断主循环
                        logging.debug("Event handling error: %s", e, exc_info=True)

                # 处理完一批事件后保证最终状态被画出来
                throttled.flush()

    finally:
        # Unix 上还原原 SIGWINCH 处理器
        exit_tty_runtime(_prev_sigwinch)

        finalize_tty_session(args, state)

    return args.messages


# ---------------------------------------------------------------------------
# 公开 API / 兼容旧测试的导出
# ---------------------------------------------------------------------------


def summarize_tool_input(tool_name: str, tool_input: Any) -> str:
    """生成工具入参的可读摘要（外部调用方用）。

    本函数是 _summarize_tool_input 的公开包装。

    Args:
        tool_name: 被调用的工具名
        tool_input: 传给工具的入参字典

    Returns:
        可在 transcript 中展示的可读摘要字符串
    """
    return _summarize_tool_input(tool_name, tool_input)


def summarize_tool_output(tool_name: str, output: str) -> str:
    """对工具输出做折叠态摘要。

    取首个有意义的行并截断到 140 字符。

    Args:
        tool_name: 工具名（当前未用，保留是为了 API 一致性）
        output: 工具完整输出字符串

    Returns:
        适合在折叠态展示的截断摘要
    """
    return _summarize_collapsed_tool_body(output)


def _format_history(entries: list[str], limit: int = 20) -> str:
    """格式化最近的历史条目，编号从 1 开始。"""
    start = max(0, len(entries) - limit)
    return "\n".join(
        f"{start + i + 1}. {entry}" for i, entry in enumerate(entries[start:])
    )


def _save_transcript(state_obj: Any, cwd: str, permissions: PermissionManager, output_path: str) -> str:
    """把 transcript 条目保存到文件，返回最终落盘的绝对路径。"""
    return _shared_save_transcript(state_obj, cwd, permissions, output_path)


def _apply_tool_result_visual_state(
    entry: TranscriptEntry,
    tool_name: str,
    output: str,
    is_error: bool,
) -> None:
    """把工具执行结果的视觉状态应用到 transcript 条目上。"""
    _shared_apply_tool_result_visual_state(entry, tool_name, output, is_error)


def _mark_unfinished_tools(state_obj: Any) -> int:
    """把仍处于运行中的工具条目标记为 error 并清理状态，返回受影响条目数。"""
    return _shared_mark_unfinished_tools(state_obj)


def _handle_feedback_mode_event(
    state: ScreenState,
    event: ParsedInputEvent,
    rerender: Callable[[], None],
    approval_event: threading.Event,
    approval_result: dict[str, Any],
) -> None:
    """处理反馈模式下的事件（用户在拒绝权限时输入说明）。"""
    pending = state.pending_approval
    if not pending:
        return

    if isinstance(event, KeyEvent):
        if event.name == "escape":
            pending.feedback_mode = False
            pending.feedback_input = ""
            rerender()
            return
        if event.name == "return":
            approval_result.clear()
            approval_result["decision"] = "deny_with_feedback"
            approval_result["feedback"] = pending.feedback_input
            approval_event.set()
            rerender()
            return
        if event.name == "backspace":
            if pending.feedback_input:
                pending.feedback_input = pending.feedback_input[:-1]
                rerender()
            return

    if isinstance(event, TextEvent) and not event.ctrl:
        pending.feedback_input += event.text
        rerender()
