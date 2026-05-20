"""MiniCode 的交互式安装向导。

负责配置模型名 / API 凭据，并把 launcher 脚本安装到平台专属的 bin 目录。
被 ``minicode-py --install`` 调用。
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from minicode.config import (
    MINI_CODE_DIR,
    MINI_CODE_SETTINGS_PATH,
    load_effective_settings,
    save_mini_code_settings,
)


def _read_input(prompt: str, default: str | None = None) -> str:
    """从 stdin 读取一行输入，可指定默认值。Ctrl+C / EOF 时优雅退出。"""
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{prompt}{suffix}: ").strip()
        return value or default or ""
    except (EOFError, KeyboardInterrupt):
        print("\n\nInstallation cancelled.")
        sys.exit(0)


def _require_input(prompt: str, default: str | None = None) -> str:
    """要求非空输入，否则反复询问。"""
    while True:
        value = _read_input(prompt, default)
        if value:
            return value
        print("该项不能为空，请重新输入。")


def _mask_secret(secret: str | None) -> str:
    """以脱敏形式显示密钥状态。"""
    if not secret:
        return "[not set]"
    return "[saved]"


def _install_launcher_script() -> str | None:
    """把 launcher 脚本安装到平台对应的 bin 目录。

    Returns:
        成功时返回 ``(launcher_path, launcher_command, target_bin_dir)`` 元组；
        失败或被用户跳过时返回 ``None``。
    """
    home = Path.home()

    # 根据平台决定目标 bin 目录与脚本格式
    if sys.platform == "win32":
        # Windows：装到 ~/.mini-code/bin，使用 .bat 脚本
        target_bin_dir = MINI_CODE_DIR / "bin"
        launcher_path = target_bin_dir / "minicode.bat"
        python_exe = sys.executable.replace("/", "\\")
        launcher_script = "\r\n".join([
            "@echo off",
            "REM MiniCode Python Launcher for Windows",
            f'"{python_exe}" -m minicode.main %*',
            "",
        ])
        launcher_command = "minicode.bat"
    elif sys.platform == "darwin":
        # macOS：装到 ~/.local/bin，使用 bash 脚本（zsh 也兼容）
        target_bin_dir = home / ".local" / "bin"
        launcher_path = target_bin_dir / "minicode-py"
        python_exe = sys.executable
        launcher_script = "\n".join([
            "#!/usr/bin/env bash",
            "# MiniCode Python Launcher for macOS",
            "# Works with bash, zsh, and other shells",
            "set -euo pipefail",
            f'exec "{python_exe}" -m minicode.main "$@"',
            "",
        ])
        launcher_command = "minicode-py"
    else:
        # Linux：装到 ~/.local/bin，使用 bash 脚本
        target_bin_dir = home / ".local" / "bin"
        launcher_path = target_bin_dir / "minicode-py"
        python_exe = sys.executable
        launcher_script = "\n".join([
            "#!/usr/bin/env bash",
            "# MiniCode Python Launcher for Linux",
            "set -euo pipefail",
            f'exec "{python_exe}" -m minicode.main "$@"',
            "",
        ])
        launcher_command = "minicode-py"

    # 路径安全检查
    resolved = str(target_bin_dir.resolve())
    if '..' in str(target_bin_dir) or '~' in str(target_bin_dir):
        print("⚠️  安装路径包含不安全字符，跳过安装。")
        return None

    if launcher_path.exists():
        answer = _read_input(f"启动器 {launcher_path} 已存在，是否覆盖？(y/N)", "N")
        if answer.lower() != "y":
            print("跳过启动器安装。")
            return str(launcher_path), launcher_command, str(target_bin_dir)

    try:
        target_bin_dir.mkdir(parents=True, exist_ok=True)

        # 原子写入：先写入临时文件再 rename，避免半成品脚本被执行
        fd, tmp_path = tempfile.mkstemp(dir=str(target_bin_dir), suffix=".tmp")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(launcher_script)
            os.replace(tmp_path, str(launcher_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        # 在类 Unix 系统上加上可执行权限
        if sys.platform != "win32":
            current_permissions = launcher_path.stat().st_mode
            launcher_path.chmod(current_permissions | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

        return str(launcher_path), launcher_command, str(target_bin_dir)
    except OSError as e:
        print(f"\n⚠️  无法安装启动器脚本: {e}")
        print("你可以手动创建启动器脚本来调用 minicode。")
        return None


def _check_path_entry(target_dir: str) -> bool:
    """检查目标目录是否已在当前 PATH 中。"""
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    return target_dir in path_entries


def main() -> None:
    """运行交互式安装向导。被 ``minicode-py --install`` 调用。"""
    print("=" * 60)
    print("  MiniCode Python 安装向导")
    print("=" * 60)
    print()
    print(f"配置会写入: {MINI_CODE_SETTINGS_PATH}")
    print("配置保存在独立目录中，不会影响其它本地工具配置。")
    print()

    # 加载已有配置作为默认值
    try:
        settings = load_effective_settings()
    except Exception:
        settings = {}

    current_env = settings.get("env", {})

    # 收集配置项
    print("📋 请输入配置信息：")
    print()
    
    model = _require_input(
        "Model name",
        settings.get("model") or current_env.get("ANTHROPIC_MODEL", ""),
    )
    
    base_url = _require_input(
        "ANTHROPIC_BASE_URL",
        current_env.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
    )
    
    saved_auth_token = current_env.get("ANTHROPIC_AUTH_TOKEN", "")
    token_status = _mask_secret(saved_auth_token)
    token_input = _read_input(
        f"ANTHROPIC_AUTH_TOKEN {token_status}",
        None,
    )
    auth_token = token_input or saved_auth_token
    
    if not auth_token and not saved_auth_token:
        print("\n❌ ANTHROPIC_AUTH_TOKEN 不能为空。")
        sys.exit(1)
    
    auth_token = auth_token or saved_auth_token
    
    # 保存配置
    print("\n💾 保存配置...")
    try:
        save_mini_code_settings({
            "model": model,
            "env": {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": auth_token,
                "ANTHROPIC_MODEL": model,
            },
        })
        print(f"✅ 配置已保存到: {MINI_CODE_SETTINGS_PATH}")
    except OSError as e:
        print(f"\n❌ 保存配置失败: {e}")
        sys.exit(1)
    
    # 安装 launcher 脚本
    print("\n🚀 安装启动器...")
    launcher_result = _install_launcher_script()

    if launcher_result:
        launcher_path, launcher_command, target_bin_dir = launcher_result
        print(f"✅ 启动器已安装: {launcher_path}")

        # 检查 PATH 并按平台给出对应的添加方法
        if not _check_path_entry(target_bin_dir):
            print()
            print("⚠️  你的 PATH 里还没有", target_bin_dir)
            print()
            if sys.platform == "win32":
                print("📋 请将以下路径添加到系统 PATH:")
                print(f"  {target_bin_dir}")
                print()
                print("Windows 添加 PATH 方法:")
                print("  1. 按 Win+R 输入 sysdm.cpl")
                print("  2. 高级 → 环境变量")
                print("  3. 在用户变量中找到 Path")
                print("  4. 添加:", target_bin_dir)
            elif sys.platform == "darwin":
                print("📋 可以把下面这行加入到 ~/.zshrc (macOS 默认 zsh):")
                print(f'  export PATH="{target_bin_dir}:$PATH"')
                print()
                print("macOS 快速添加:")
                print(f'  echo \'export PATH="{target_bin_dir}:$PATH"\' >> ~/.zshrc')
                print("  source ~/.zshrc")
            else:
                print("📋 可以把下面这行加入到 ~/.bashrc 或 ~/.zshrc:")
                print(f'  export PATH="{target_bin_dir}:$PATH"')
                print()
                print("Linux 快速添加 (bash):")
                print(f'  echo \'export PATH="{target_bin_dir}:$PATH"\' >> ~/.bashrc')
                print("  source ~/.bashrc")
        else:
            print()
            print(f"✅ 现在你可以在任意终端输入 `{launcher_command}` 启动。")

    # 最终摘要
    print()
    print("=" * 60)
    print("  安装完成！")
    print("=" * 60)
    print()
    print("📁 配置文件:", MINI_CODE_SETTINGS_PATH)
    if launcher_result:
        launcher_path, launcher_command, _ = launcher_result
        print("🚀 启动命令:", launcher_command)
    print()
    print("📋 各平台启动方式:")
    print()
    print("  Windows:")
    print("    minicode.bat               (如果已添加 PATH)")
    print("    python -m minicode.main    (通用方式)")
    print()
    print("  macOS:")
    print("    minicode-py                (如果已添加 PATH)")
    print("    python3 -m minicode.main   (通用方式)")
    print()
    print("  Linux:")
    print("    minicode-py                (如果已添加 PATH)")
    print("    python3 -m minicode.main   (通用方式)")
    print()
    print("感谢使用 MiniCode Python！🎉")
    print()


if __name__ == "__main__":
    main()
