"""纯文本文件解析器。

把磁盘上的源文件读成统一的 :class:`Document`：
- 仅处理纯文本类后缀（markdown / 代码 / 配置等），不涉及二进制或多模态。
- 编码自动探测：utf-8 → utf-8-sig → gbk，最后退化为 errors="replace"。
- 大文件保护：超过 :data:`MAX_FILE_SIZE` 直接跳过并告警，避免索引爆内存。
"""
from __future__ import annotations

import logging
from pathlib import Path

from minicode.knowledge.types import Document

logger = logging.getLogger(__name__)


# 后缀 → MIME 类型映射（决定后续分块策略）
SUPPORTED_EXTENSIONS: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/x-rst",
    ".py": "text/x-python",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".js": "text/javascript",
    ".jsx": "text/javascript",
    ".json": "application/json",
    ".yaml": "application/x-yaml",
    ".yml": "application/x-yaml",
    ".toml": "application/toml",
}

# 编码探测的尝试顺序
_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")

# 单文件大小上限（10 MB）；超过则跳过
MAX_FILE_SIZE = 10 * 1024 * 1024


class ParseError(Exception):
    """解析失败的基类异常。"""


class UnsupportedFileError(ParseError):
    """文件后缀不在支持列表内。"""


class FileTooLargeError(ParseError):
    """文件超过大小上限。"""


def is_supported(path: str | Path) -> bool:
    """判断给定路径的后缀是否被支持。"""
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def detect_mime(path: str | Path) -> str:
    """根据后缀返回 MIME 类型，未知后缀返回 ``text/plain``。"""
    return SUPPORTED_EXTENSIONS.get(Path(path).suffix.lower(), "text/plain")


def _read_text(path: Path) -> str:
    """按多种编码尝试读取文本，全部失败则用 replace 兜底。"""
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    # 最终兜底：永不抛编码异常
    return raw.decode("utf-8", errors="replace")


def parse(path: str | Path, root: str | Path | None = None) -> Document:
    """把单个文件解析为 :class:`Document`。

    参数：
        path: 待解析文件路径。
        root: 索引根目录；若提供，``source_path`` 取相对该根目录的 posix 路径，
            否则使用绝对 posix 路径。

    异常：
        UnsupportedFileError: 后缀不被支持。
        FileTooLargeError: 文件超过 :data:`MAX_FILE_SIZE`。
        ParseError: 其他不可读情况（权限/IO 等）。
    """
    p = Path(path)
    if not is_supported(p):
        raise UnsupportedFileError(f"Unsupported file type: {p.suffix}")

    try:
        size = p.stat().st_size
        mtime = p.stat().st_mtime
    except OSError as e:
        raise ParseError(f"Cannot stat file {p}: {e}") from e

    if size > MAX_FILE_SIZE:
        logger.warning("Skipping large file (%d bytes > %d): %s", size, MAX_FILE_SIZE, p)
        raise FileTooLargeError(f"File too large ({size} bytes): {p}")

    try:
        content = _read_text(p)
    except OSError as e:
        raise ParseError(f"Cannot read file {p}: {e}") from e

    if root is not None:
        try:
            source_path = p.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            source_path = p.as_posix()
    else:
        source_path = p.as_posix()

    return Document(
        id=Document.make_id(source_path),
        source_path=source_path,
        content=content,
        mime=detect_mime(p),
        metadata={
            "size": size,
            "mtime": mtime,
            "suffix": p.suffix.lower(),
        },
    )


class TextParser:
    """纯文本文件解析器封装。

    提供 ``parse()`` 方法将文件解析为 :class:`Document`。
    """

    def __init__(self):
        self.supported_extensions = SUPPORTED_EXTENSIONS
        self.max_file_size = MAX_FILE_SIZE

    def parse(self, path: str | Path, root: str | Path | None = None) -> Document:
        """解析文件为 Document。

        参数：
            path: 文件路径
            root: 根目录（用于计算相对路径）

        返回：
            Document 对象
        """
        return parse(path, root)

    def is_supported(self, path: str | Path) -> bool:
        """判断文件是否支持。"""
        return is_supported(path)


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "TextParser",
    "parse",
    "is_supported",
    "detect_mime",
    "ParseError",
    "UnsupportedFileError",
    "FileTooLargeError",
]
