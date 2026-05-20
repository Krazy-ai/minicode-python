"""高风险操作的隔离执行器。

借鉴 Learn Claude Code 的最佳实践：
- 用 git worktree 隔离探索性/破坏性操作
- 执行前先做风险评估
- 隔离结束后自动清理

提供：
- RiskAssessor：评估操作风险等级
- IsolationExecutor：在隔离 worktree 中执行命令
- CleanupManager：自动清理隔离环境
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from minicode.tooling import ToolResult


# ---------------------------------------------------------------------------
# 风险评估
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """操作风险等级。"""
    SAFE = "safe"           # 只读操作
    LOW = "low"             # 轻微写入（如配置文件）
    MEDIUM = "medium"       # 修改源代码
    HIGH = "high"           # 数据库/部署等
    CRITICAL = "critical"   # 破坏性操作（rm -rf / drop table 等）


# 命令风险分类
_CRITICAL_COMMANDS = frozenset({
    "rm", "shred", "dd", "mkfs", "fdisk", "format",
    "dropdb", "drop", "truncate",
})

_HIGH_COMMANDS = frozenset({
    "sudo", "su", "chmod", "chown", "mount", "umount",
    "systemctl", "service", "brew", "apt", "yum", "dnf",
})

_MEDIUM_COMMANDS = frozenset({
    "git", "npm", "pip", "cargo", "go", "make", "cmake",
    "docker", "docker-compose", "kubectl",
})


def assess_command_risk(command: str, args: list[str]) -> RiskLevel:
    """评估一次命令执行的风险等级。

    参数：
        command: 待执行的命令
        args: 命令参数

    返回：
        建议的隔离级别 RiskLevel。
    """
    cmd_base = command.lower().split("/")[-1]

    # critical：破坏性操作
    if cmd_base in _CRITICAL_COMMANDS:
        return RiskLevel.CRITICAL

    # 含破坏性 flag 即视为 critical
    destructive_flags = {"-rf", "-fr", "--force", "--recursive", "--no-preserve-root"}
    if any(flag in args for flag in destructive_flags):
        return RiskLevel.CRITICAL

    # high：系统级操作
    if cmd_base in _HIGH_COMMANDS:
        return RiskLevel.HIGH

    # medium：开发工具
    if cmd_base in _MEDIUM_COMMANDS:
        return RiskLevel.MEDIUM

    # low：文件写入类
    if cmd_base in {"echo", "cat", "tee", "cp", "mv", "mkdir", "touch"}:
        return RiskLevel.LOW

    # safe：纯只读
    safe_commands = {
        "ls", "pwd", "cat", "head", "tail", "wc", "grep", "find",
        "which", "whoami", "date", "echo", "df", "du", "uname",
    }
    if cmd_base in safe_commands:
        return RiskLevel.SAFE

    # 未知命令默认 medium
    return RiskLevel.MEDIUM


# ---------------------------------------------------------------------------
# Worktree 隔离
# ---------------------------------------------------------------------------

@dataclass
class IsolationContext:
    """隔离执行的上下文。"""

    worktree_path: Path
    original_path: Path
    branch_name: str
    created_at: float = field(default_factory=time.time)
    cleanup_on_exit: bool = True
    max_age_seconds: float = 3600  # 默认 1 小时

    def is_expired(self) -> bool:
        """该隔离上下文是否已过期。"""
        return (time.time() - self.created_at) > self.max_age_seconds


class WorktreeIsolator:
    """利用 git worktree 隔离高风险操作。

    创建临时 worktree，让探索性/破坏性操作不会影响主工作区。
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        prefix: str = "isolated",
    ) -> None:
        self.base_dir = base_dir or Path(tempfile.gettempdir()) / "minicode-isolation"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.prefix = prefix
        self.active_contexts: dict[str, IsolationContext] = {}

    def create_isolation(
        self,
        source_path: Path,
        task_id: str | None = None,
        max_age_seconds: float = 3600,
    ) -> IsolationContext:
        """从源仓库创建一个新的隔离 worktree。

        参数：
            source_path: 源 git 仓库路径
            task_id: 任务唯一标识（None 时自动生成）
            max_age_seconds: 最大存活时间，超时后自动清理

        返回：
            含 worktree 路径与元信息的 IsolationContext。
        """
        task_id = task_id or str(uuid.uuid4())[:8]
        branch_name = f"{self.prefix}_{task_id}"
        worktree_path = self.base_dir / f"{self.prefix}_{task_id}"

        # Verify source is a git repository
        git_dir = source_path / ".git"
        if not git_dir.exists():
            raise ValueError(f"Source path is not a git repository: {source_path}")

        try:
            # Create worktree
            subprocess.run(
                [
                    "git", "-C", str(source_path),
                    "worktree", "add",
                    "-b", branch_name,
                    str(worktree_path),
                    "HEAD",
                ],
                capture_output=True,
                text=True,
                check=True,
            )

            context = IsolationContext(
                worktree_path=worktree_path,
                original_path=source_path,
                branch_name=branch_name,
                max_age_seconds=max_age_seconds,
            )
            self.active_contexts[task_id] = context
            return context

        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"Failed to create worktree: {e.stderr}") from e

    def execute_in_isolation(
        self,
        task_id: str,
        command: str,
        args: list[str],
        cwd: Path | None = None,
        timeout: int = 300,
    ) -> ToolResult:
        """在隔离 worktree 中执行命令。

        参数：
            task_id: 由 ``create_isolation()`` 返回的任务 ID
            command: 待执行的命令
            args: 命令参数
            cwd: 相对于 worktree 的工作目录（默认 worktree 根目录）
            timeout: 执行超时（秒）

        返回：
            含命令输出的 ToolResult。
        """
        context = self.active_contexts.get(task_id)
        if not context:
            return ToolResult(
                ok=False,
                output=f"Isolation context not found: {task_id}",
            )

        if context.is_expired():
            self.cleanup_isolation(task_id)
            return ToolResult(
                ok=False,
                output="Isolation context expired. Create a new one.",
            )

        exec_cwd = cwd if cwd else context.worktree_path
        if not exec_cwd.exists():
            return ToolResult(
                ok=False,
                output=f"Working directory not found: {exec_cwd}",
            )

        try:
            result = subprocess.run(
                [command, *args],
                cwd=str(exec_cwd),
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )

            output = "\n".join(
                part for part in [result.stdout.strip(), result.stderr.strip()] if part
            )
            return ToolResult(ok=result.returncode == 0, output=output[:10000])

        except subprocess.TimeoutExpired:
            return ToolResult(
                ok=False,
                output=f"Command timed out after {timeout} seconds in isolation.",
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                output=f"Execution failed in isolation: {e}",
            )

    def cleanup_isolation(self, task_id: str) -> bool:
        """清理一个隔离 worktree。

        参数：
            task_id: 待清理的任务 ID

        返回：
            清理是否成功。
        """
        context = self.active_contexts.pop(task_id, None)
        if not context:
            return False

        try:
            # Remove worktree
            subprocess.run(
                [
                    "git", "-C", str(context.original_path),
                    "worktree", "remove", "-f",
                    str(context.worktree_path),
                ],
                capture_output=True,
                text=True,
            )
        except Exception:
            pass  # Best effort cleanup

        # Remove directory if it still exists
        if context.worktree_path.exists():
            try:
                shutil.rmtree(context.worktree_path)
            except Exception:
                pass

        return True

    def cleanup_expired(self) -> list[str]:
        """清理所有已过期的隔离上下文。

        返回：
            被清理的 task_id 列表。
        """
        expired = [
            tid for tid, ctx in self.active_contexts.items()
            if ctx.is_expired()
        ]
        for tid in expired:
            self.cleanup_isolation(tid)
        return expired

    def cleanup_all(self) -> list[str]:
        """清理所有活跃的隔离上下文。

        返回：
            被清理的 task_id 列表。
        """
        all_ids = list(self.active_contexts.keys())
        for tid in all_ids:
            self.cleanup_isolation(tid)
        return all_ids

    def get_active_count(self) -> int:
        """获取活跃隔离上下文数量。"""
        return len(self.active_contexts)

    def get_status(self) -> dict[str, Any]:
        """获取隔离状态信息。"""
        return {
            "active_isolations": len(self.active_contexts),
            "base_dir": str(self.base_dir),
            "isolations": [
                {
                    "task_id": tid,
                    "branch": ctx.branch_name,
                    "age_seconds": time.time() - ctx.created_at,
                    "expired": ctx.is_expired(),
                }
                for tid, ctx in self.active_contexts.items()
            ],
        }


# ---------------------------------------------------------------------------
# 安全执行入口
# ---------------------------------------------------------------------------

_default_isolator = WorktreeIsolator()


def get_isolator() -> WorktreeIsolator:
    """获取全局 WorktreeIsolator。"""
    return _default_isolator


def execute_safely(
    command: str,
    args: list[str],
    source_path: Path,
    task_id: str | None = None,
    timeout: int = 300,
) -> ToolResult:
    """带自动风险评估和隔离的命令执行入口。

    流程：
    1. 评估命令风险等级
    2. medium+ 风险走 worktree 隔离
    3. 执行命令
    4. 完成后自动清理

    参数：
        command: 待执行的命令
        args: 命令参数
        source_path: 用于创建 worktree 的源仓库路径
        task_id: 可选的任务 ID
        timeout: 执行超时（秒）

    返回：
        含执行输出和风险元信息的 ToolResult。
    """
    risk = assess_command_risk(command, args)

    # safe / low 风险直接执行
    if risk in (RiskLevel.SAFE, RiskLevel.LOW):
        try:
            result = subprocess.run(
                [command, *args],
                cwd=str(source_path),
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            output = "\n".join(
                part for part in [result.stdout.strip(), result.stderr.strip()] if part
            )
            return ToolResult(
                ok=result.returncode == 0,
                output=f"[Risk: {risk.value}] {output[:10000]}",
            )
        except Exception as e:
            return ToolResult(
                ok=False,
                output=f"[Risk: {risk.value}] Execution failed: {e}",
            )

    # medium+ 风险走隔离执行
    isolator = get_isolator()
    try:
        context = isolator.create_isolation(
            source_path=source_path,
            task_id=task_id,
            max_age_seconds=600,  # 隔离执行限定 10 分钟
        )

        result = isolator.execute_in_isolation(
            task_id=context.branch_name.split("_")[-1],
            command=command,
            args=args,
            timeout=timeout,
        )

        # 执行后清理
        isolator.cleanup_isolation(context.branch_name.split("_")[-1])

        # 在输出前加上风险等级提示
        if result.ok:
            result.output = f"[Risk: {risk.value}, Isolated] {result.output}"
        else:
            result.output = f"[Risk: {risk.value}, Isolated] {result.output}"

        return result

    except Exception as e:
        return ToolResult(
            ok=False,
            output=f"[Risk: {risk.value}] Isolation failed: {e}",
        )


def format_risk_info(command: str, args: list[str]) -> str:
    """格式化风险评估信息用于展示。

    参数：
        command: 待评估命令
        args: 命令参数

    返回：
        人类可读的风险评估字符串。
    """
    risk = assess_command_risk(command, args)

    risk_descriptions = {
        RiskLevel.SAFE: "Read-only operation, no side effects",
        RiskLevel.LOW: "Minor writes, low risk of data loss",
        RiskLevel.MEDIUM: "Development operation, isolated execution recommended",
        RiskLevel.HIGH: "System operation, requires isolation",
        RiskLevel.CRITICAL: "Destructive operation, requires strict isolation",
    }

    return (
        f"Risk Assessment\n"
        f"{'=' * 50}\n"
        f"Command: {command} {' '.join(args)}\n"
        f"Level: {risk.value.upper()}\n"
        f"Description: {risk_descriptions[risk]}\n"
    )
