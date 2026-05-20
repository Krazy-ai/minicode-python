"""会话持久化与恢复模块。

提供会话数据结构、自动保存机制以及 resume 能力，
让 MiniCode 可以在重启之间持久化并恢复对话状态。

为减少序列化开销，采用增量 delta 保存策略：
- 自上次保存以来仅追加新增/变更的消息
- 每 N 次 delta 保存做一次完整保存以保证一致性
- 字段级 dirty 跟踪避免重复序列化
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from minicode.config import MINI_CODE_DIR


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

SESSIONS_DIR = MINI_CODE_DIR / "sessions"
AUTOSAVE_INTERVAL_SECONDS = 30  # 两次自动保存之间的最小间隔（秒）

# 增量保存配置
DELTA_DIR_NAME = "deltas"        # 存放 delta 文件的子目录
FULL_SAVE_INTERVAL = 10          # 每 N 次 delta 保存做一次完整保存
MAX_DELTA_FILES = 50             # delta 文件数上限，超过则强制合并


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SessionMetadata:
    """用于会话列表展示的轻量元信息。"""
    session_id: str
    created_at: float  # Unix 时间戳
    updated_at: float  # Unix 时间戳
    first_message: str = ""  # 截断后的首条 user 消息
    last_message: str = ""   # 截断后的最末一条消息
    message_count: int = 0
    workspace: str = ""      # 创建会话时的工作目录


@dataclass
class SessionData:
    """完整的会话状态，可被持久化与恢复。"""
    session_id: str
    created_at: float
    updated_at: float
    workspace: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    transcript_entries: list[dict[str, Any]] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    permissions_summary: dict[str, Any] = field(default_factory=dict)
    skills: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    metadata: SessionMetadata = field(default=None)
    
    # 增量保存跟踪
    _last_saved_msg_count: int = field(default=0, repr=False)
    _last_saved_transcript_count: int = field(default=0, repr=False)
    _delta_save_count: int = field(default=0, repr=False)
    _last_full_save_hash: str = field(default="", repr=False)

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = SessionMetadata(
                session_id=self.session_id,
                created_at=self.created_at,
                updated_at=self.updated_at,
                message_count=len(self.messages),
                workspace=self.workspace,
            )

    def update_metadata(self) -> None:
        """根据当前状态刷新元信息。"""
        self.updated_at = time.time()
        self.metadata.updated_at = self.updated_at
        self.metadata.message_count = len(self.messages)

        # 抽取首条 user 消息（截断）
        for msg in self.messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                self.metadata.first_message = content[:100]
                break

        # 抽取最末一条消息（截断）
        for msg in reversed(self.messages):
            if msg.get("role") in ("user", "assistant"):
                content = msg.get("content", "")
                self.metadata.last_message = content[:100]
                break
    
    @property
    def has_delta(self) -> bool:
        """是否存在尚未保存的修改。"""
        return (
            len(self.messages) != self._last_saved_msg_count
            or len(self.transcript_entries) != self._last_saved_transcript_count
        )
    
    def _compute_content_hash(self) -> str:
        """对消息内容做快速哈希，用于检测变化。"""
        h = hashlib.md5(usedforsecurity=False)
        for msg in self.messages[-20:]:  # 仅哈希最近 20 条以提速
            h.update(msg.get("role", "").encode())
            content = msg.get("content", "")
            if isinstance(content, str):
                h.update(content[:500].encode())
        return h.hexdigest()


# ---------------------------------------------------------------------------
# 会话文件操作
# ---------------------------------------------------------------------------

def _session_file(session_id: str) -> Path:
    """返回会话主 JSON 文件路径。"""
    return SESSIONS_DIR / f"{session_id}.json"


def _session_delta_dir(session_id: str) -> Path:
    """返回会话 delta 目录路径。"""
    return SESSIONS_DIR / DELTA_DIR_NAME / session_id


def _session_index_file() -> Path:
    """返回会话索引文件路径。"""
    return MINI_CODE_DIR / "sessions_index.json"


def _load_session_index() -> dict[str, SessionMetadata]:
    """加载会话索引（所有会话的轻量元信息）。"""
    index_path = _session_index_file()
    if not index_path.exists():
        return {}
    try:
        raw = index_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return {
            sid: SessionMetadata(**meta)
            for sid, meta in data.items()
        }
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}


def _save_session_index(index: dict[str, SessionMetadata]) -> None:
    """保存会话索引。"""
    MINI_CODE_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    serializable = {
        sid: {
            "session_id": meta.session_id,
            "created_at": meta.created_at,
            "updated_at": meta.updated_at,
            "first_message": meta.first_message,
            "last_message": meta.last_message,
            "message_count": meta.message_count,
            "workspace": meta.workspace,
        }
        for sid, meta in index.items()
    }
    _session_index_file().write_text(
        json.dumps(serializable, indent=2) + "\n",
        encoding="utf-8",
    )


def _save_delta(session: SessionData) -> None:
    """仅保存自上次保存以来的增量变更。

    delta 文件包含上次保存以来新增的消息与 transcript，
    比每次都序列化完整会话要轻量得多。
    """
    delta_dir = _session_delta_dir(session.session_id)
    delta_dir.mkdir(parents=True, exist_ok=True)
    
    # Collect new messages since last save
    new_messages = session.messages[session._last_saved_msg_count:]
    new_transcripts = session.transcript_entries[session._last_saved_transcript_count:]
    
    if not new_messages and not new_transcripts:
        return
    
    # Create delta entry
    delta_data: dict[str, Any] = {
        "ts": time.time(),
        "msg_offset": session._last_saved_msg_count,
        "transcript_offset": session._last_saved_transcript_count,
    }
    if new_messages:
        delta_data["messages"] = new_messages
    if new_transcripts:
        delta_data["transcripts"] = new_transcripts
    
    # Write delta file with sequential numbering
    delta_num = session._delta_save_count
    delta_path = delta_dir / f"delta_{delta_num:04d}.json"
    delta_path.write_text(
        json.dumps(delta_data, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    
    # Update tracking
    session._last_saved_msg_count = len(session.messages)
    session._last_saved_transcript_count = len(session.transcript_entries)
    session._delta_save_count += 1


def _consolidate_deltas(session: SessionData) -> None:
    """将所有 delta 文件合并到主会话文件中并清理。

    定期调用以防止 delta 文件无限增长，
    同时保证主会话文件保持一致。
    """
    delta_dir = _session_delta_dir(session.session_id)
    if not delta_dir.exists():
        return
    
    # Deltas are already applied during load_session, so just clean up
    for delta_file in sorted(delta_dir.glob("delta_*.json")):
        try:
            delta_file.unlink()
        except OSError:
            pass
    
    # Try to remove empty delta directory
    try:
        delta_dir.rmdir()
        # Also try to remove parent if empty
        parent = delta_dir.parent
        if parent.name == DELTA_DIR_NAME and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    
    session._delta_save_count = 0


def save_session(session: SessionData, force_full: bool = False) -> None:
    """将会话持久化到磁盘，支持增量保存。

    采用混合策略：
    - delta 保存：仅追加新增的消息/transcript（速度快、I/O 小）
    - 完整保存：序列化整个会话（较慢，但保证一致）
    - 合并：定期把 delta 合并回主文件

    参数：
        session: 待保存的会话
        force_full: 是否强制完整保存（如执行显式保存命令时）
    """
    session.update_metadata()
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Decide whether to do a full save or delta save
    should_full_save = (
        force_full
        or session._delta_save_count == 0  # First save is always full
        or session._delta_save_count >= FULL_SAVE_INTERVAL
        or session._delta_save_count >= MAX_DELTA_FILES  # Safety cap
    )
    
    if should_full_save:
        # Full save: serialize everything
        session_path = _session_file(session.session_id)
        serializable = {
            "session_id": session.session_id,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "workspace": session.workspace,
            "messages": session.messages,
            "transcript_entries": session.transcript_entries,
            "history": session.history,
            "permissions_summary": session.permissions_summary,
            "skills": session.skills,
            "mcp_servers": session.mcp_servers,
            "metadata": {
                "session_id": session.metadata.session_id,
                "created_at": session.metadata.created_at,
                "updated_at": session.metadata.updated_at,
                "first_message": session.metadata.first_message,
                "last_message": session.metadata.last_message,
                "message_count": session.metadata.message_count,
                "workspace": session.metadata.workspace,
            },
        }
        session_path.write_text(
            json.dumps(serializable, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        
        # Reset delta tracking
        session._last_saved_msg_count = len(session.messages)
        session._last_saved_transcript_count = len(session.transcript_entries)
        session._last_full_save_hash = session._compute_content_hash()
        
        # Consolidate and clean up delta files
        _consolidate_deltas(session)
    else:
        # Delta save: only append new data
        _save_delta(session)
    
    # Update index (always lightweight)
    index = _load_session_index()
    index[session.session_id] = session.metadata
    _save_session_index(index)


def load_session(session_id: str) -> SessionData | None:
    """从磁盘加载会话，并应用所有挂起的 delta。

    加载流程：
    1. 加载主会话文件
    2. 扫描 delta 文件
    3. 按顺序应用 delta（追加新消息/transcript）
    4. 更新跟踪计数器
    """
    session_path = _session_file(session_id)
    if not session_path.exists():
        return None

    try:
        raw = session_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        metadata = SessionMetadata(**data.get("metadata", {}))
        session = SessionData(
            session_id=data["session_id"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            workspace=data["workspace"],
            messages=data.get("messages", []),
            transcript_entries=data.get("transcript_entries", []),
            history=data.get("history", []),
            permissions_summary=data.get("permissions_summary", {}),
            skills=data.get("skills", []),
            mcp_servers=data.get("mcp_servers", []),
            metadata=metadata,
        )
        
        # Apply any pending deltas
        delta_dir = _session_delta_dir(session_id)
        if delta_dir.exists():
            delta_files = sorted(delta_dir.glob("delta_*.json"))
            for delta_path in delta_files:
                try:
                    delta_raw = delta_path.read_text(encoding="utf-8")
                    delta = json.loads(delta_raw)
                    
                    # Append delta messages at the correct offset
                    if "messages" in delta:
                        offset = delta.get("msg_offset", len(session.messages))
                        # Ensure we don't duplicate messages
                        if offset >= len(session.messages):
                            session.messages.extend(delta["messages"])
                        elif offset + len(delta["messages"]) > len(session.messages):
                            # Partial overlap — append only the new part
                            overlap = len(session.messages) - offset
                            session.messages.extend(delta["messages"][overlap:])
                    
                    # Append delta transcripts
                    if "transcripts" in delta:
                        t_offset = delta.get("transcript_offset", len(session.transcript_entries))
                        if t_offset >= len(session.transcript_entries):
                            session.transcript_entries.extend(delta["transcripts"])
                        elif t_offset + len(delta["transcripts"]) > len(session.transcript_entries):
                            overlap = len(session.transcript_entries) - t_offset
                            session.transcript_entries.extend(delta["transcripts"][overlap:])
                    
                    session._delta_save_count += 1
                except (json.JSONDecodeError, KeyError, TypeError):
                    # Skip corrupt delta files
                    continue
        
        # Update tracking counters
        session._last_saved_msg_count = len(session.messages)
        session._last_saved_transcript_count = len(session.transcript_entries)
        session._last_full_save_hash = session._compute_content_hash()
        
        return session
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def list_sessions() -> list[SessionMetadata]:
    """List all available sessions, newest first."""
    index = _load_session_index()
    sessions = list(index.values())
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


def delete_session(session_id: str) -> bool:
    """Delete a session from disk. Returns True if deleted."""
    session_path = _session_file(session_id)
    if not session_path.exists():
        return False

    try:
        session_path.unlink()
        index = _load_session_index()
        index.pop(session_id, None)
        _save_session_index(index)
        return True
    except OSError:
        return False


def cleanup_old_sessions(max_sessions: int = 50) -> int:
    """Remove oldest sessions beyond max_sessions limit. Returns count deleted."""
    sessions = list_sessions()
    if len(sessions) <= max_sessions:
        return 0

    to_delete = sessions[max_sessions:]
    deleted = 0
    for meta in to_delete:
        if delete_session(meta.session_id):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Session creation helpers
# ---------------------------------------------------------------------------

def create_new_session(workspace: str) -> SessionData:
    """Create a new empty session."""
    now = time.time()
    session_id = uuid.uuid4().hex[:12]
    return SessionData(
        session_id=session_id,
        created_at=now,
        updated_at=now,
        workspace=workspace,
    )


def get_latest_session(workspace: str | None = None) -> SessionData | None:
    """Get the most recent session, optionally filtered by workspace."""
    sessions = list_sessions()
    for meta in sessions:
        if workspace is None or meta.workspace == workspace:
            return load_session(meta.session_id)
    return None


# ---------------------------------------------------------------------------
# Autosave manager
# ---------------------------------------------------------------------------

class AutosaveManager:
    """带速率限制和 delta 支持的自动保存管理器。

    自动保存使用增量 delta（速度快），
    显式保存命令使用完整保存（保证一致）。
    """

    def __init__(self, session: SessionData, interval: int = AUTOSAVE_INTERVAL_SECONDS):
        self.session = session
        self.interval = interval
        self._last_save_time = time.time()  # Initialize to current time
        self._dirty = False
        self._full_save_counter = 0

    def mark_dirty(self) -> None:
        """Mark session as needing save."""
        self._dirty = True

    def should_save(self) -> bool:
        """Check if autosave should trigger."""
        if not self._dirty:
            return False
        elapsed = time.time() - self._last_save_time
        return elapsed >= self.interval

    def save_if_needed(self) -> bool:
        """Save if dirty and interval elapsed. Uses delta saves for speed.
        
        Returns True if saved.
        """
        if self.should_save():
            # Use incremental delta save for autosave (fast)
            save_session(self.session, force_full=False)
            self._last_save_time = time.time()
            self._dirty = False
            self._full_save_counter += 1
            return True
        return False

    def force_save(self) -> None:
        """Force immediate full save regardless of interval."""
        save_session(self.session, force_full=True)
        self._last_save_time = time.time()
        self._dirty = False
        self._full_save_counter = 0


# ---------------------------------------------------------------------------
# Session formatting for display
# ---------------------------------------------------------------------------

def format_session_list(sessions: list[SessionMetadata]) -> str:
    """Format sessions as a human-readable list."""
    if not sessions:
        return "No saved sessions found."

    lines = ["Saved sessions:", ""]
    for i, meta in enumerate(sessions, 1):
        created = time.strftime(
            "%Y-%m-%d %H:%M",
            time.localtime(meta.created_at),
        )
        workspace = meta.workspace or "unknown"
        first_msg = meta.first_message or "(empty)"
        count = meta.message_count

        lines.append(
            f"  {i}. [{meta.session_id[:8]}] {created} - {workspace}"
        )
        lines.append(f"     Messages: {count} | First: {first_msg}")
        lines.append("")

    lines.append(f"Total: {len(sessions)} session(s)")
    return "\n".join(lines)


def format_session_resume(session: SessionData) -> str:
    """Format session info for resume confirmation."""
    created = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(session.created_at),
    )
    updated = time.strftime(
        "%Y-%m-%d %H:%M:%S",
        time.localtime(session.updated_at),
    )
    return (
        f"Resuming session {session.session_id[:8]}\n"
        f"  Created: {created}\n"
        f"  Updated: {updated}\n"
        f"  Messages: {len(session.messages)}\n"
        f"  Workspace: {session.workspace}"
    )
