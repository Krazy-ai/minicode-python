"""MiniCode 的 Auto 模式。

灵感来自 Claude Code 介于「逐项审批」和 ``--dangerously-skip-permissions`` 之间的 Auto 模式。
它包含：
- 输入层 prompt 注入检测
- 输出层不安全操作分类
- 安全操作自动放行
- 高风险操作直接拦截或引导到安全替代

权限模式：
- default：每个动作都询问（默认行为）
- auto：安全操作自动放行，风险操作仍需确认
- bypass：跳过所有权限检查（危险）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 权限模式
# ---------------------------------------------------------------------------

class PermissionMode(str, Enum):
    """权限模式（参考 Claude Code）。"""
    DEFAULT = "default"           # 每个动作都询问
    AUTO = "auto"                 # 安全操作自动放行
    BYPASS = "bypass"             # 跳过所有权限检查（危险）
    PLAN = "plan"                 # 只读，不允许执行


# ---------------------------------------------------------------------------
# 风险分类
# ---------------------------------------------------------------------------

class RiskLevel(str, Enum):
    """操作风险等级。"""
    SAFE = "safe"                 # 自动放行
    LOW = "low"                   # 自动放行并记录
    MEDIUM = "medium"             # 弹窗询问
    HIGH = "high"                 # 拦截或要求强理由
    DANGEROUS = "dangerous"       # 始终拦截


# ---------------------------------------------------------------------------
# 风险规则
# ---------------------------------------------------------------------------

# 安全工具（auto 模式自动放行）
SAFE_TOOLS = {
    "read_file",
    "list_files",
    "grep_files",
    "load_skill",
}

# 低风险工具（自动放行 + 记录）
LOW_RISK_TOOLS = {
    "run_command",  # 仅适用于只读类命令
}

# 中风险工具（需要审批）
MEDIUM_RISK_TOOLS = {
    "write_file",
    "edit_file",
    "patch_file",
    "modify_file",
}

# 高风险命令（拦截或要求强理由）
HIGH_RISK_COMMANDS = {
    # Unix
    "rm -rf",
    "rm -r",
    "git reset --hard",
    "git clean",
    "git push --force",
    "sudo",
    "chmod -R",
    "chown -R",
    # Windows
    "del /s",
    "del /q",
    "rmdir /s",
    "rd /s",
    "icacls",
    "takeown",
    "net user",
    "net localgroup",
    "reg delete",
    "format",
}

# 危险模式（始终拦截）
DANGEROUS_PATTERNS = [
    # Unix
    r"rm\s+-rf\s+/",           # 删除根目录
    r"chmod\s+777",            # 全员可写
    r"curl.*\|\s*sh",          # curl | sh
    r"wget.*\|\s*sh",
    r"mkfs",                   # 格式化文件系统
    r"dd\s+if=",               # 磁盘镜像
    # Windows
    r"del\s+/[sfq].*[\\]",     # 带路径的递归/强制删除
    r"rmdir\s+/s\s+/q",        # 静默递归删除目录
    r"rd\s+/s\s+/q",
    r"format\s+[a-zA-Z]:",     # 格式化分区
    r"powershell.*\biex\b",    # PowerShell 远程 invoke-expression
    r"powershell.*Invoke-Expression", 
    r"iwr.*\|\s*iex",          # PowerShell 下载并执行
    r"reg\s+delete\s+HKLM",   # 删除全局注册表项
]


@dataclass
class RiskAssessment:
    """风险评估结果。"""
    level: RiskLevel
    tool_name: str
    action: str  # "approve" / "prompt" / "block"
    reason: str
    safe_alternative: str | None = None


# ---------------------------------------------------------------------------
# Auto 模式判定器
# ---------------------------------------------------------------------------

class AutoModeChecker:
    """判定操作是否可以自动放行。

    参考 Claude Code Auto 模式的输入/输出层校验。
    """
    
    def __init__(self, mode: PermissionMode = PermissionMode.DEFAULT):
        self.mode = mode
    
    def set_mode(self, mode: PermissionMode) -> None:
        """切换权限模式。"""
        self.mode = mode
    
    def assess_risk(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> RiskAssessment:
        """评估一次工具调用的风险。

        参数：
            tool_name: 待执行的工具名
            tool_input: 工具输入参数 dict

        返回：
            含动作建议的 RiskAssessment。
        """
        # bypass 模式 —— 全部放行
        if self.mode == PermissionMode.BYPASS:
            return RiskAssessment(
                level=RiskLevel.DANGEROUS,
                tool_name=tool_name,
                action="approve",
                reason="Bypass mode: all permissions skipped",
            )
        
        # plan 模式 —— 仅允许只读
        if self.mode == PermissionMode.PLAN:
            if tool_name in SAFE_TOOLS:
                return RiskAssessment(
                    level=RiskLevel.SAFE,
                    tool_name=tool_name,
                    action="approve",
                    reason="Plan mode: read-only tool",
                )
            else:
                return RiskAssessment(
                    level=RiskLevel.HIGH,
                    tool_name=tool_name,
                    action="block",
                    reason="Plan mode: execution not allowed",
                )
        
        # default 模式 —— 全部询问
        if self.mode == PermissionMode.DEFAULT:
            return RiskAssessment(
                level=RiskLevel.MEDIUM,
                tool_name=tool_name,
                action="prompt",
                reason="Default mode: approval required",
            )
        
        # auto 模式 —— 智能评估
        return self._assess_auto_mode(tool_name, tool_input)
    
    def _assess_auto_mode(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> RiskAssessment:
        """auto 模式下的风险评估。"""
        # 安全工具自动放行
        if tool_name in SAFE_TOOLS:
            return RiskAssessment(
                level=RiskLevel.SAFE,
                tool_name=tool_name,
                action="approve",
                reason=f"Auto mode: {tool_name} is read-only",
            )
        
        # run_command：检查是否为只读命令
        if tool_name == "run_command":
            return self._assess_command(tool_input)
        
        # 文件修改类工具
        if tool_name in MEDIUM_RISK_TOOLS:
            return self._assess_file_edit(tool_name, tool_input)
        
        # 未知工具 —— 询问
        return RiskAssessment(
            level=RiskLevel.MEDIUM,
            tool_name=tool_name,
            action="prompt",
            reason=f"Auto mode: unknown tool '{tool_name}'",
        )
    
    def _assess_command(self, tool_input: dict[str, Any]) -> RiskAssessment:
        """评估 run_command 的风险。"""
        command = tool_input.get("command", "")
        if isinstance(command, list):
            command = " ".join(command)
        
        # 命中危险模式直接拦截
        for pattern in DANGEROUS_PATTERNS:
            if re.search(pattern, command, re.IGNORECASE):
                return RiskAssessment(
                    level=RiskLevel.DANGEROUS,
                    tool_name="run_command",
                    action="block",
                    reason=f"Dangerous pattern detected: {pattern}",
                )
        
        # 命中高风险命令则询问
        for risky_cmd in HIGH_RISK_COMMANDS:
            if risky_cmd in command:
                return RiskAssessment(
                    level=RiskLevel.HIGH,
                    tool_name="run_command",
                    action="prompt",
                    reason=f"High-risk command: '{risky_cmd}'",
                    safe_alternative=f"Consider safer alternative to '{risky_cmd}'",
                )
        
        # 低风险 —— 自动放行 + 记录
        return RiskAssessment(
            level=RiskLevel.LOW,
            tool_name="run_command",
            action="approve",
            reason=f"Auto mode: command appears safe",
        )
    
    def _assess_file_edit(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
    ) -> RiskAssessment:
        """评估文件编辑类工具的风险。"""
        path = tool_input.get("path", "")
        
        # 是否在编辑敏感文件
        # 用 [/\\] 同时兼容 Unix / 与 Windows \
        sensitive_patterns = [
            r"\.env",
            r"\.git[/\\]",
            r"node_modules[/\\]",
            r"__pycache__[/\\]",
            r"\.pyc$",
        ]
        
        for pattern in sensitive_patterns:
            if re.search(pattern, path):
                return RiskAssessment(
                    level=RiskLevel.HIGH,
                    tool_name=tool_name,
                    action="prompt",
                    reason=f"Modifying sensitive file: {path}",
                )
        
        # 普通文件编辑 —— 询问
        return RiskAssessment(
            level=RiskLevel.MEDIUM,
            tool_name=tool_name,
            action="prompt",
            reason=f"Auto mode: file modification requires approval",
        )
    
    # -----------------------------------------------------------------------
    # 输入/输出层校验（参考 Claude Code）
    # -----------------------------------------------------------------------
    
    @staticmethod
    def detect_prompt_injection(user_input: str) -> tuple[bool, str]:
        """检测用户输入中潜在的 prompt 注入攻击。

        返回：
            (is_injection, reason)
        """
        injection_patterns = [
            r"ignore\s+(all\s+)?(previous|prior)\s+(instructions|rules|prompts)",
            r"(system|developer)\s*:\s*",
            r"\[?ignore\s+security\]?",
            r"(bypass|skip|override)\s+(permissions|safety|restrictions)",
            r"(execute|run)\s+(this|following)\s+code\s*:",
            r"ignore\s+(all|your)\s+instructions",
        ]
        
        for pattern in injection_patterns:
            if re.search(pattern, user_input, re.IGNORECASE):
                return True, f"Potential prompt injection: {pattern}"
        
        return False, ""
    
    @staticmethod
    def classify_output_safety(output: str) -> tuple[bool, str]:
        """判断 AI 输出是否包含不安全操作。

        返回：
            (is_unsafe, reason)
        """
        unsafe_patterns = [
            # Unix
            r"rm\s+-rf",
            r"sudo\s+",
            r"chmod\s+777",
            # Windows
            r"del\s+/[sfq]",
            r"rmdir\s+/s",
            r"rd\s+/s",
            r"format\s+[a-zA-Z]:",
            # SQL
            r"DROP\s+TABLE",
            r"DELETE\s+FROM.*WHERE\s+1\s*=\s*1",
        ]
        
        for pattern in unsafe_patterns:
            if re.search(pattern, output, re.IGNORECASE):
                return True, f"Unsafe operation detected: {pattern}"
        
        return False, ""


# ---------------------------------------------------------------------------
# 模式管理
# ---------------------------------------------------------------------------

@dataclass
class ModeState:
    """当前权限模式状态。"""
    mode: PermissionMode = PermissionMode.DEFAULT
    mode_changed_at: float = 0.0
    mode_changed_by: str = "user"
    auto_approve_count: int = 0
    prompt_count: int = 0
    block_count: int = 0
    
    def record_decision(self, action: str) -> None:
        """记录一次权限决策。"""
        import time
        if action == "approve":
            self.auto_approve_count += 1
        elif action == "prompt":
            self.prompt_count += 1
        elif action == "block":
            self.block_count += 1
    
    def format_status(self) -> str:
        """格式化模式状态。"""
        mode_descriptions = {
            PermissionMode.DEFAULT: "Ask for every action",
            PermissionMode.AUTO: "Auto-approve safe operations",
            PermissionMode.BYPASS: "⚠️ Skip all permissions (dangerous!)",
            PermissionMode.PLAN: "Read-only mode",
        }
        
        lines = [
            "Permission Mode",
            "=" * 50,
            f"Current mode: {self.mode.value}",
            f"Description: {mode_descriptions.get(self.mode, 'Unknown')}",
            "",
            "Statistics:",
            f"  Auto-approved: {self.auto_approve_count}",
            f"  Prompted: {self.prompt_count}",
            f"  Blocked: {self.block_count}",
        ]
        
        total = self.auto_approve_count + self.prompt_count + self.block_count
        if total > 0:
            auto_pct = self.auto_approve_count / total * 100
            lines.append(f"  Auto-approval rate: {auto_pct:.0f}%")
        
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 模块级单例
# ---------------------------------------------------------------------------

_checker = AutoModeChecker()
_mode_state = ModeState()


def get_checker() -> AutoModeChecker:
    """获取全局 AutoModeChecker。"""
    return _checker


def get_mode_state() -> ModeState:
    """获取全局模式状态。"""
    return _mode_state


def set_permission_mode(mode: PermissionMode) -> str:
    """设置全局权限模式。"""
    import time
    _checker.set_mode(mode)
    _mode_state.mode = mode
    _mode_state.mode_changed_at = time.time()
    
    mode_messages = {
        PermissionMode.DEFAULT: "✓ Default mode: All actions require approval",
        PermissionMode.AUTO: "⚡ Auto mode: Safe operations auto-approved",
        PermissionMode.BYPASS: "⚠️ BYPASS MODE: All permissions skipped!",
        PermissionMode.PLAN: "📖 Plan mode: Read-only operations allowed",
    }
    
    return mode_messages.get(mode, f"Mode changed to {mode.value}")
