"""命令历史持久化。

存储用户在 TUI 里输入过的命令，方便上下方向键回溯。
最多保留最近 200 条，写入 ~/.mini-code/history.json。
"""
from __future__ import annotations

import json

from minicode.config import MINI_CODE_DIR, MINI_CODE_HISTORY_PATH


def load_history_entries() -> list[str]:
    """从磁盘加载历史记录。文件不存在或损坏时返回空列表。"""
    if not MINI_CODE_HISTORY_PATH.exists():
        return []
    try:
        parsed = json.loads(MINI_CODE_HISTORY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    entries = parsed.get("entries", [])
    return [str(entry) for entry in entries] if isinstance(entries, list) else []


def save_history_entries(entries: list[str]) -> None:
    """保存历史记录，仅保留最后 200 条以控制文件大小。"""
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    MINI_CODE_HISTORY_PATH.write_text(
        json.dumps({"entries": entries[-200:]}, indent=2) + "\n",
        encoding="utf-8",
    )
