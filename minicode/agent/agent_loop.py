"""Agent 主循环 ``run_agent_turn``。

这是 MiniCode 的"心脏" —— 实现 ``model → tool → model`` 的多轮工具调用循环。

每一步的处理流程：
    1. 调模型拿到 AgentStep（assistant 文本 或 tool_calls）
    2. 若是 assistant：判定是 progress（推一把继续）/ 空响应（重试）/ final（结束）
    3. 若是 tool_calls：用 ToolScheduler 拆成"并发批"和"串行批"
       - 并发批：只读工具，并行 ThreadPoolExecutor 执行
       - 串行批：写文件/命令等需保序执行
    4. 工具失败时用 ErrorClassifier + NudgeGenerator 自动生成恢复提示
    5. 触发 AGENT_START / PRE_TOOL_USE / POST_TOOL_USE / AGENT_STOP 等 Hook 事件
    6. 集成 ContextManager，在压缩阈值时自动触发 compact
    7. 集成 metrics 收集器，记录每步耗时与工具成败

注意：本文件中的 ``NUDGE_*`` / ``RESUME_*`` 等英文模板是直接喂给 LLM 的提示，
影响模型推理走向，**保留英文不翻译**（中文翻译只覆盖代码注释）。
"""
from __future__ import annotations

import concurrent.futures
import inspect
from typing import Any, Callable

from minicode.memory.context_manager import ContextManager, estimate_message_tokens
from minicode.runtime.logging_config import get_logger
from minicode.security.permissions import PermissionManager
from minicode.runtime.state import Store, AppState, increment_tool_calls, add_cost, record_api_error, update_context_usage, set_busy, set_idle
from minicode.tooling import ToolContext, ToolRegistry, ToolResult
from minicode.types import AgentStep, ChatMessage, ModelAdapter

# Hooks 事件集成
from minicode.runtime.hooks import HookEvent, fire_hook_sync

# Agent 智能层集成（错误分类 / 推动消息 / 工具调度）
from minicode.agent.agent_metrics import AgentMetricsCollector
from minicode.agent.agent_intelligence import ErrorClassifier, NudgeGenerator, RecoveryStrategy, ToolScheduler

logger = get_logger("agent_loop")

# ---------------------------------------------------------------------------
# 给 LLM 的英文 nudge 模板 —— 影响模型行为
# ---------------------------------------------------------------------------

# 当模型返回 progress 文本但还有后续工作时，要求继续推进
NUDGE_CONTINUE = (
    "Continue immediately from your <progress> update with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

# 已经在本回合用过工具，模型却又给纯文本时，提示别把进度当 final
NUDGE_AFTER_TOOL_RESULT = (
    "Continue from your progress update. You have already used tools in this turn, "
    "so treat plain status text as progress, not a final answer. Respond with the "
    "next concrete tool call, code change, or an explicit <final> answer only if "
    "the task is truly complete."
)

# 工具执行后模型回了空，提示先消化 tool result 再继续
NUDGE_AFTER_EMPTY_RESPONSE = (
    "Your last response was empty after recent tool results. Continue immediately "
    "by trying the next concrete step, adapting to any tool errors, or giving an "
    "explicit <final> answer only if the task is complete."
)

# 模型回空且本回合还没用过工具，提示主动出招
NUDGE_AFTER_EMPTY_NO_TOOLS = (
    "Your last response was empty. Continue immediately with concrete tool calls, "
    "code changes, or an explicit <final> answer only if the task is complete."
)

# 模型在 thinking 阶段被中断（pause_turn）后续回合用的恢复提示
RESUME_AFTER_PAUSE = (
    "Resume from the previous pause and continue immediately with the next concrete "
    "tool call, code change, or an explicit <final> answer only if the task is complete."
)

# 模型 thinking 阶段就把 max_tokens 用完时的恢复提示
RESUME_AFTER_MAX_TOKENS = (
    "Your previous response hit max_tokens during thinking before producing the next "
    "actionable step. Resume immediately and continue with the next concrete tool call, "
    "code change, or an explicit <final> answer only if the task is complete."
)


def _is_empty_assistant_response(content: str) -> bool:
    """判断 assistant 回复是否为空（去白后长度 0）。"""
    return len(content.strip()) == 0


def _execute_single_tool(
    call: dict,
    tools: ToolRegistry,
    cwd: str,
    permissions: Any | None,
    runtime: dict | None,
    store: Any | None,
    step: int,
    on_tool_start: Callable[[str, dict], None] | None,
    on_tool_result: Callable[[str, str, bool], None] | None,
) -> ToolResult:
    """执行单个工具调用，附带 hook 触发、状态更新、崩溃防护。

    既用于串行执行，也作为并发执行的 worker 函数。当并发执行时
    （store / on_tool_start / on_tool_result 传 None），hook 与 UI 回调
    会被推迟到结果处理阶段统一触发。

    内置全局异常防护：工具执行流水线（hook、状态更新等）的任何意外崩溃都会
    被捕获并转成 error ToolResult，避免单个工具崩溃拖垮整个 agent loop。
    """
    tool_name = call["toolName"]
    tool_input = call["input"]

    try:
        # 工具开始前的 hook 与 UI 回调（仅串行模式触发）
        if on_tool_start:
            on_tool_start(tool_name, tool_input)

        if store:
            store.set_state(set_busy(tool_name))

        # 真正执行工具（ToolRegistry.execute 自身已带异常防护网）
        result = tools.execute(
            tool_name,
            tool_input,
            ToolContext(cwd=cwd, permissions=permissions, _runtime=runtime),
        )

        # 工具结束后的状态更新（仅串行模式触发）
        if store:
            store.set_state(increment_tool_calls())
            store.set_state(set_idle())

        if on_tool_result:
            on_tool_result(tool_name, result.output, not result.ok)

        return result

    except (KeyboardInterrupt, SystemExit):
        # 这两类必须向上传播
        raise
    except Exception as exc:  # noqa: BLE001
        # 全局兜底：捕获工具执行流水线（hook / 状态更新 / 权限检查等）的任何
        # 意外错误，转成 error result，防止单个工具崩塌整个会话
        import traceback
        tb_excerpt = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)[-3:]).strip()
        error_type = type(exc).__name__

        logger.error("Tool execution pipeline crashed (%s): %s", error_type, exc)

        # 即使崩溃也要把 busy 状态恢复
        if store:
            try:
                store.set_state(set_idle())
            except Exception:
                pass

        return ToolResult(
            ok=False,
            output=f"[{error_type}] Tool execution pipeline crashed: {exc}\n"
                   f"Traceback:\n{tb_excerpt}"
        )


def _format_diagnostics(stop_reason: str | None, block_types: list[str] | None, ignored_block_types: list[str] | None) -> str:
    """把 AgentStep 的诊断信息格式化为人类可读字符串，附在 fallback 文本末尾。"""
    parts: list[str] = []
    if stop_reason:
        parts.append(f"stop_reason={stop_reason}")
    if block_types:
        parts.append(f"blocks={','.join(block_types)}")
    if ignored_block_types:
        parts.append(f"ignored={','.join(ignored_block_types)}")
    return f" Diagnostics: {'; '.join(parts)}." if parts else ""


def _is_recoverable_thinking_stop(*, is_empty: bool, stop_reason: str | None, ignored_block_types: list[str] | None) -> bool:
    """判断这次空响应是否属于"thinking 阶段就被截断"的可恢复情况。

    满足三个条件才算可恢复：
        1. 内容为空
        2. stop_reason 是 pause_turn 或 max_tokens
        3. ignored block 中含 thinking（说明思考链被卡断）
    """
    if not is_empty:
        return False
    if stop_reason not in {"pause_turn", "max_tokens"}:
        return False
    return "thinking" in (ignored_block_types or [])


def _should_treat_assistant_as_progress(*, kind: str | None, content: str, saw_tool_result: bool) -> bool:
    """判断 assistant 文本是否应被视为 progress（而非 final）。

    决策表：
        kind="progress"  → True
        kind="final"     → False
        其他             → 一律 False（保守起见，避免误把 final 当成 progress）
    """
    if kind == "progress":
        return True
    if kind == "final":
        return False
    if not saw_tool_result:
        return False
    return False


def _model_next(
    model: ModelAdapter,
    messages: list[ChatMessage],
    *,
    on_stream_chunk: Callable[[str], None] | None,
    store: Store[AppState] | None,
) -> AgentStep:
    """调 provider adapter 的 next()，向后兼容老的不接受 ``store`` 参数的测试桩。"""
    if store is None:
        return model.next(messages, on_stream_chunk=on_stream_chunk)

    try:
        signature = inspect.signature(model.next)
    except (TypeError, ValueError):
        return model.next(messages, on_stream_chunk=on_stream_chunk, store=store)

    supports_store = any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == "store"
        for parameter in signature.parameters.values()
    )
    if supports_store:
        return model.next(messages, on_stream_chunk=on_stream_chunk, store=store)
    return model.next(messages, on_stream_chunk=on_stream_chunk)


def run_agent_turn(
    *,
    model: ModelAdapter,
    tools: ToolRegistry,
    messages: list[ChatMessage],
    cwd: str,
    permissions: PermissionManager | None = None,
    store: Store[AppState] | None = None,
    max_steps: int = 50,
    on_tool_start: Callable[[str, dict], None] | None = None,
    on_tool_result: Callable[[str, str, bool], None] | None = None,
    on_assistant_message: Callable[[str], None] | None = None,
    on_progress_message: Callable[[str], None] | None = None,
    on_assistant_stream_chunk: Callable[[str], None] | None = None,
    context_manager: ContextManager | None = None,
    runtime: dict | None = None,
    metrics_collector: AgentMetricsCollector | None = None,
) -> list[ChatMessage]:
    """跑一轮完整的 agent turn（模型 → 工具 → 模型 ... 直到 final 或达到 max_steps）。

    Args:
        model: 模型 adapter
        tools: 工具注册表
        messages: 当前消息列表（会被复制后修改）
        cwd: 工作目录
        permissions: 权限管理器（None 时工具调用不会触发审批）
        store: 全局状态 Store（None 时不更新 UI 状态）
        max_steps: 单回合内的最大工具步数，防止无限循环
        on_tool_start / on_tool_result / on_assistant_message / ...: UI 回调
        context_manager: 上下文窗口管理器（接近上限时自动 compact）
        runtime: runtime 配置字典
        metrics_collector: 度量收集器

    Returns:
        累积修改后的完整消息列表（已包含本次 turn 的所有新消息）。
    """
    current_messages = list(messages)
    saw_tool_result = False
    empty_response_retry_count = 0
    recoverable_thinking_retry_count = 0
    tool_error_count = 0
    step = 0

    tool_scheduler = ToolScheduler(metrics_collector=metrics_collector)

    # 检查上下文使用情况
    if context_manager:
        context_manager.messages = current_messages
        stats = context_manager.get_stats()
        logger.info("Context: %d tokens (%.0f%%), %d messages",
                   stats.total_tokens, stats.usage_percentage, stats.messages_count)

        # 接近上限则自动压缩
        if context_manager.should_auto_compact():
            logger.warning("Context near limit, auto-compacting...")
            current_messages = context_manager.compact_messages()
            if on_assistant_message:
                on_assistant_message(context_manager.get_context_summary())

    try:
        while max_steps is None or step < max_steps:
            step += 1

            # Hook：agent turn 开始
            fire_hook_sync(HookEvent.AGENT_START, step=step, cwd=cwd)

            if metrics_collector:
                metrics_collector.start_turn(step)

            next_step: AgentStep
            try:
                next_step = _model_next(
                    model,
                    current_messages,
                    on_stream_chunk=on_assistant_stream_chunk,
                    store=store,
                )
            except KeyboardInterrupt:
                raise  # Ctrl-C 需向上传播
            except ConnectionError as error:
                fallback = f"Network error (connection failed or dropped): {error}"
                logger.error("Model API connection error: %s", error)
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages
            except TimeoutError as error:
                fallback = f"Model API timeout: {error}"
                logger.error("Model API timeout: %s", error)
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages
            except Exception as error:
                # 兜底：rate limit / auth / 5xx 等其他异常
                error_type = type(error).__name__
                fallback = f"Model API error ({error_type}): {error}"
                logger.error("Model API error (%s): %s", error_type, error)
                if on_assistant_message:
                    on_assistant_message(fallback)
                current_messages.append({"role": "assistant", "content": fallback})
                if metrics_collector:
                    metrics_collector.end_turn(total_tokens=0)
                return current_messages

            if next_step.type == "assistant":
                is_empty = _is_empty_assistant_response(next_step.content)
                if not is_empty and _should_treat_assistant_as_progress(
                    kind=getattr(next_step, 'kind', None),
                    content=next_step.content,
                    saw_tool_result=saw_tool_result,
                ):
                    if on_progress_message:
                        on_progress_message(next_step.content)
                    current_messages.append({"role": "assistant_progress", "content": next_step.content})
                    current_messages.append(
                        {
                            "role": "user",
                            "content": (
                                NUDGE_AFTER_TOOL_RESULT
                                if saw_tool_result and getattr(next_step, 'kind', None) != "progress"
                                else NUDGE_CONTINUE
                            ),
                        }
                    )
                    continue

                diagnostics = next_step.diagnostics

                # 可恢复的 thinking 中断：模型在思考阶段就被卡，要求其继续
                if _is_recoverable_thinking_stop(
                    is_empty=is_empty,
                    stop_reason=diagnostics.stopReason if diagnostics else None,
                    ignored_block_types=diagnostics.ignoredBlockTypes if diagnostics else None,
                ) and recoverable_thinking_retry_count < 3:
                    recoverable_thinking_retry_count += 1
                    stop_reason = diagnostics.stopReason if diagnostics else None
                    progress_content = (
                        "Model hit max_tokens during thinking; requesting the next step."
                        if stop_reason == "max_tokens"
                        else "Model returned pause_turn; requesting the next step."
                    )
                    if on_progress_message:
                        on_progress_message(progress_content)
                    current_messages.append({"role": "assistant_progress", "content": progress_content})
                    current_messages.append(
                        {
                            "role": "user",
                            "content": (
                                RESUME_AFTER_PAUSE
                                if stop_reason == "pause_turn"
                                else RESUME_AFTER_MAX_TOKENS
                            ),
                        }
                    )
                    continue

                # 空响应：最多重试 2 次
                if is_empty and empty_response_retry_count < 2:
                    empty_response_retry_count += 1
                    current_messages.append(
                        {
                            "role": "user",
                            "content": (
                                NUDGE_AFTER_EMPTY_RESPONSE
                                if saw_tool_result
                                else NUDGE_AFTER_EMPTY_NO_TOOLS
                            ),
                        }
                    )
                    continue

                # 重试次数也用完了仍是空响应，给一个 fallback 文本并结束
                if is_empty:
                    diagnostics_suffix = _format_diagnostics(
                        diagnostics.stopReason if diagnostics else None,
                        diagnostics.blockTypes if diagnostics else None,
                        diagnostics.ignoredBlockTypes if diagnostics else None,
                    )
                    if saw_tool_result:
                        fallback = (
                            f"Model returned an empty response after tool execution and the turn was stopped. There were {tool_error_count} tool error(s); retry, adjust the command, or choose a different approach.{diagnostics_suffix}"
                            if tool_error_count > 0
                            else f"Model returned an empty response after tool execution and the turn was stopped. Retry or ask the model to continue the remaining steps.{diagnostics_suffix}"
                        )
                    else:
                        fallback = f"Model returned an empty response and the turn was stopped.{diagnostics_suffix}"
                    if on_assistant_message:
                        on_assistant_message(fallback)
                    current_messages.append({"role": "assistant", "content": fallback})
                    return current_messages

                # 正常的 final 回答
                if on_assistant_message:
                    on_assistant_message(next_step.content)
                current_messages.append({"role": "assistant", "content": next_step.content})
                return current_messages

            # —— 走到这里说明 next_step.type == "tool_calls" ——

            # 模型可能在工具调用前还附带了一段 progress/assistant 文本
            if next_step.content:
                role = "assistant_progress" if next_step.contentKind == "progress" else "assistant"
                if role == "assistant_progress":
                    if on_progress_message:
                        on_progress_message(next_step.content)
                    current_messages.append({"role": role, "content": next_step.content})
                    current_messages.append(
                        {
                            "role": "user",
                            "content": NUDGE_CONTINUE,
                        }
                    )
                else:
                    if on_assistant_message:
                        on_assistant_message(next_step.content)
                    current_messages.append({"role": role, "content": next_step.content})

            if not next_step.calls and next_step.content and next_step.contentKind != "progress":
                return current_messages

            # --- 并发工具执行 ---
            # 把调用拆成"并发安全（只读）"和"必须串行（写/命令）"两组
            calls = next_step.calls
            _results: list[tuple[dict, ToolResult]] = []

            if len(calls) <= 1:
                # 单个调用 —— 没必要走线程池
                call = calls[0]
                if metrics_collector:
                    metrics_collector.start_tool(call["toolName"])
                result = _execute_single_tool(
                    call, tools, cwd, permissions, runtime, store, step,
                    on_tool_start, on_tool_result,
                )
                if metrics_collector:
                    metrics_collector.end_tool(
                        success=result.ok,
                        error=result.output if not result.ok else "",
                    )
                _results.append((call, result))
            else:
                # 多个调用 —— 用 ToolScheduler 智能拆分
                concurrent_calls, serial_calls = tool_scheduler.schedule_calls(calls, tools)

                _results: list[tuple[dict, ToolResult]] = []

                # 阶段 1：所有"并发安全"工具并行执行
                if concurrent_calls:
                    max_workers = tool_scheduler.get_recommended_max_workers(concurrent_calls)
                    with concurrent.futures.ThreadPoolExecutor(
                        max_workers=max_workers,
                        thread_name_prefix="mc-tool",
                    ) as pool:
                        future_to_call = {
                            pool.submit(
                                _execute_single_tool,
                                call, tools, cwd, permissions, runtime, None, step,
                                None, None,  # 并发阶段不触发 UI 回调，事后统一处理
                            ): call
                            for call in concurrent_calls
                        }
                        for future in concurrent.futures.as_completed(future_to_call):
                            call = future_to_call[future]
                            try:
                                result = future.result()
                            except Exception as exc:
                                result = ToolResult(ok=False, output=f"Concurrent execution error: {exc}")
                            _results.append((call, result))

                # 阶段 2：串行工具按原顺序逐个执行
                if serial_calls:
                    for call in serial_calls:
                        if metrics_collector:
                            metrics_collector.start_tool(call["toolName"])
                        result = _execute_single_tool(
                            call, tools, cwd, permissions, runtime, store, step,
                            on_tool_start, on_tool_result,
                        )
                        if metrics_collector:
                            metrics_collector.end_tool(
                                success=result.ok,
                                error=result.output if not result.ok else "",
                            )
                        _results.append((call, result))
                        # 若某串行工具要求等待用户介入，立即跳出
                        if result.awaitUser:
                            # 仍需把已有结果写进消息列表
                            break

            # 处理所有结果并写消息（保持原始 call 顺序）
            call_order = {call["id"]: idx for idx, call in enumerate(calls)}
            _results.sort(key=lambda pair: call_order.get(pair[0]["id"], 999))

            for call, result in _results:
                # 并发执行的工具：把推迟的 hook 与 UI 回调补触发
                tool_def = tools.find(call["toolName"])
                is_concurrent = tool_def and tool_def.is_concurrency_safe and len(calls) > 1

                if is_concurrent:
                    # 并发工具的 UI 回调（事后补触发）
                    if on_tool_start:
                        on_tool_start(call["toolName"], call["input"])
                    if store:
                        store.set_state(set_busy(call["toolName"]))
                        store.set_state(increment_tool_calls())
                        store.set_state(set_idle())
                    # Hook：pre-tool-use（并发工具事后补触发）
                    fire_hook_sync(
                        HookEvent.PRE_TOOL_USE,
                        tool_name=call["toolName"],
                        tool_input=call["input"],
                        step=step,
                    )

                # Hook：post-tool-use
                fire_hook_sync(
                    HookEvent.POST_TOOL_USE,
                    tool_name=call["toolName"],
                    tool_output=result.output,
                    is_error=not result.ok,
                    step=step,
                )

                if is_concurrent:
                    if on_tool_result:
                        on_tool_result(call["toolName"], result.output, not result.ok)

                saw_tool_result = True
                if not result.ok:
                    tool_error_count += 1
                    # 用 ErrorClassifier 智能处理错误
                    classified = ErrorClassifier.classify(result.output, tool_name=call["toolName"])
                    nudge = NudgeGenerator.generate(classified, retry_count=tool_error_count)
                    # 把 nudge 附在 tool result 里，供模型上下文参考
                    result_output = result.output + "\n\n[System note: " + nudge + "]"
                else:
                    result_output = result.output

                # 若并发组里有多个工具同时失败，记录冲突供调度器学习
                if not result.ok and len(calls) > 1:
                    for other_call, other_result in _results:
                        if other_call["id"] == call["id"]:
                            continue
                        if not other_result.ok:
                            tool_scheduler.record_conflict(call["toolName"], other_call["toolName"])

                current_messages.append(
                    {
                        "role": "assistant_tool_call",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "input": call["input"],
                    }
                )
                current_messages.append(
                    {
                        "role": "tool_result",
                        "toolUseId": call["id"],
                        "toolName": call["toolName"],
                        "content": result_output,
                        "isError": not result.ok,
                    }
                )
                if result.awaitUser:
                    if on_assistant_message:
                        on_assistant_message(result_output)
                    current_messages.append({"role": "assistant", "content": result_output})
                    if metrics_collector:
                        metrics_collector.end_turn(total_tokens=0)
                    return current_messages

            # 本步工具执行完毕；继续向模型请求下一步，
            # 不要直接 fall through 到 max-step fallback。
            if metrics_collector:
                total_tokens = sum(
                    estimate_message_tokens(m) for m in current_messages
                ) if context_manager else 0
                metrics_collector.end_turn(total_tokens=total_tokens)
            continue

        # 触发 max_steps 上限
        fallback = "Reached the maximum tool step limit for this turn."
        if on_assistant_message:
            on_assistant_message(fallback)
        current_messages.append({"role": "assistant", "content": fallback})
        return current_messages
    finally:
        # Hook：agent turn 结束（即使异常也会触发）
        fire_hook_sync(HookEvent.AGENT_STOP, step=step, tool_errors=tool_error_count)
