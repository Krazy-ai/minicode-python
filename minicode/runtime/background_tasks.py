"""后台任务注册表与槽位管理。

跟踪后台启动的子进程（如 long-running shell 命令），
提供存活状态刷新、并发槽位限制、完成回调等能力。
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from typing import Any, Callable

from minicode.tooling import BackgroundTaskResult

# 内存中的后台任务注册表
_background_tasks: dict[str, dict[str, Any]] = {}

# 任务槽位管理
_max_slots: int = 5  # 最大并发后台任务数
_slot_callbacks: dict[str, Callable] = {}  # 完成回调


def _is_process_alive(pid: int) -> bool | None:
    """跨平台检查进程是否存活。

    返回：
        True  — 仍在运行
        False — 进程已经结束
        None  — 无法判断（视为「失败」）
    """
    if sys.platform == "win32":
        # Windows 上 os.kill(pid, 0) 在所有情况下都会抛 OSError
        # （包括进程仍然存在），因此改用 ctypes
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259

            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False  # 打不开句柄即视为已结束

            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return None
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return None
    else:
        # Unix：发送 0 号信号检查存在性，不会真的发信号
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # EPERM —— 进程存在但我们没权限发信号，仍视为存活
            return True
        except OSError:
            return None


def _refresh_record(record: dict[str, Any]) -> dict[str, Any]:
    """检查 running 任务是否仍存活并更新状态。"""
    if record.get("status") != "running":
        return record
    pid = record.get("pid")
    if pid is None:
        return record

    alive = _is_process_alive(pid)
    if alive is True:
        return record
    elif alive is False:
        record["status"] = "completed"
    else:
        record["status"] = "failed"
    return record


def register_background_shell_task(command: str, pid: int, cwd: str) -> BackgroundTaskResult:
    """注册一个后台 shell 任务。"""
    del cwd
    result = BackgroundTaskResult(
        taskId=f"task_{uuid.uuid4().hex[:8]}",
        type="local_bash",
        command=command,
        pid=pid,
        status="running",
        startedAt=int(time.time() * 1000),
    )
    _background_tasks[result.taskId] = {
        "taskId": result.taskId,
        "type": result.type,
        "command": result.command,
        "pid": result.pid,
        "status": result.status,
        "startedAt": result.startedAt,
        "label": command[:60],
    }
    return result


def list_background_tasks() -> list[dict[str, Any]]:
    """返回当前跟踪的所有后台任务（状态已刷新）。"""
    return [_refresh_record(record) for record in _background_tasks.values()]


def get_background_task(task_id: str) -> dict[str, Any] | None:
    """按 ID 获取单个后台任务（状态已刷新）。"""
    record = _background_tasks.get(task_id)
    if record is None:
        return None
    return _refresh_record(record)


# ---------------------------------------------------------------------------
# 槽位管理
# ---------------------------------------------------------------------------

def get_slot_stats() -> dict[str, Any]:
    """获取当前槽位使用统计。"""
    running = sum(1 for r in _background_tasks.values() if r.get("status") == "running")
    return {
        "used_slots": running,
        "max_slots": _max_slots,
        "available_slots": _max_slots - running,
        "total_tracked": len(_background_tasks),
    }


def can_start_new_task() -> bool:
    """是否还有空闲槽位可用于启动新任务。"""
    stats = get_slot_stats()
    return stats["available_slots"] > 0


def set_max_slots(max_slots: int) -> None:
    """设置最大并发后台任务数。"""
    global _max_slots
    _max_slots = max(1, max_slots)  # 至少保留 1 个槽位


def register_completion_callback(task_id: str, callback: Callable) -> None:
    """为指定任务注册完成回调。"""
    _slot_callbacks[task_id] = callback


def check_completed_tasks() -> list[str]:
    """扫描已完成任务并触发回调。

    返回已完成的任务 ID 列表。
    """
    completed = []
    for task_id, record in list(_background_tasks.items()):
        if record.get("status") == "running":
            refreshed = _refresh_record(record)
            if refreshed["status"] != "running":
                completed.append(task_id)
                # 触发已注册的回调
                callback = _slot_callbacks.pop(task_id, None)
                if callback:
                    try:
                        callback(task_id, refreshed)
                    except Exception:
                        pass  # 回调异常不应影响主循环
    return completed


def format_slot_status() -> str:
    """格式化槽位状态用于展示。"""
    stats = get_slot_stats()
    running_tasks = [
        r for r in _background_tasks.values() if r.get("status") == "running"
    ]

    lines = [
        "Background Task Slots",
        "=" * 50,
        f"Slots: {stats['used_slots']}/{stats['max_slots']} used",
        f"Available: {stats['available_slots']}",
        f"Total tracked: {stats['total_tracked']}",
        "",
    ]

    if running_tasks:
        lines.append("Running Tasks:")
        for task in running_tasks:
            lines.append(
                f"  • [{task.get('taskId', '?')}] {task.get('label', task.get('command', 'unknown'))}"
            )
        lines.append("")

    return "\n".join(lines)
