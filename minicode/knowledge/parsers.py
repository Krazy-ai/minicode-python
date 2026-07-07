"""纯文本解析器：把源文件读成 ``Document``。

范围（非目标）：只处理纯文本类文件，**不做** PDF / 图片 / OCR / VLM。

特性：
- 支持常见文本/代码后缀
- 编码自动探测（utf-8 → utf-8-sig → gbk fallback）
- 大文件保护（> ``MAX_FILE_SIZE`` 跳过并告警）
"""

from __future__ import annotations

import logging
from pathlib import Path

from minicode.knowledge.types import Document

logger = logging.getLogger(__name__)


# 支持的文本后缀 → mime 简单标记
_SUFFIX_MIME: dict[str, str] = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".rst": "text/x-rst",
    ".py": "text/x-python",
    ".pyi": "text/x-python",
    ".ts": "text/x-typescript",
    ".tsx": "text/x-typescript",
    ".js": "text/x-javascript",
    ".jsx": "text/x-javascript",
    ".json": "application/json",
    ".yaml": "text/x-yaml",
    ".yml": "text/x-yaml",
    ".toml": "text/x-toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".cff": "text/plain",
    ".go": "text/x-go",
    ".rs": "text/x-rust",
    ".java": "text/x-java",
    ".c": "text/x-c",
    ".h": "text/x-c",
    ".cpp": "text/x-c++",
    ".hpp": "text/x-c++",
    ".sh": "text/x-shellscript",
    ".sql": "text/x-sql",
    ".html": "text/html",
    ".css": "text/css",
}

# 大文件保护上限（字节）：超过则跳过
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# 编码探测顺序
_ENCODINGS = ("utf-8", "utf-8-sig", "gbk")


def is_supported(path: Path | str) -> bool:
    """判断某个后缀是否受支持。"""
    return Path(path).suffix.lower() in _SUFFIX_MIME


def supported_suffixes() -> set[str]:
    """返回受支持的后缀集合。"""
    return set(_SUFFIX_MIME.keys())


def _detect_and_read(path: Path) -> str:
    """按 ``_ENCODINGS`` 顺序尝试读取文件文本。

    全部失败时用 ``errors="replace"`` 兜底，保证不抛异常。
    """
    raw = path.read_bytes()
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 最终兜底：替换非法字节
    logger.warning("Falling back to utf-8/replace for %s", path)
    return raw.decode("utf-8", errors="replace")


def parse(path: Path | str) -> Document | None:
    """把一个文件解析成 ``Document``。

    返回 ``None`` 表示应跳过（不支持的后缀 / 文件过大 / 读取失败）。

    参数：
        path: 文件路径

    返回：
        ``Document`` 或 ``None``。
    """
    p = Path(path)

    suffix = p.suffix.lower()
    if suffix not in _SUFFIX_MIME:
        return None

    try:
        stat = p.stat()
    except OSError as e:
        logger.warning("Cannot stat %s: %s", p, e)
        return None

    if not p.is_file():
        return None

    if stat.st_size > MAX_FILE_SIZE:
        logger.warning(
            "Skipping large file %s (%.1f MB > %.1f MB limit)",
            p,
            stat.st_size / 1024 / 1024,
            MAX_FILE_SIZE / 1024 / 1024,
        )
        return None

    try:
        content = _detect_and_read(p)
    except OSError as e:
        logger.warning("Cannot read %s: %s", p, e)
        return None

    source_path = str(p)
    return Document(
        id=Document.make_id(source_path),
        source_path=source_path,
        content=content,
        mime=_SUFFIX_MIME[suffix],
        metadata={
            "size": stat.st_size,
            "mtime": stat.st_mtime,
            "suffix": suffix,
        },
    )


# 索引时默认跳过的目录
SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    "dist", "build", ".hg", ".svn", ".next", ".nuxt", "target",
    "vendor", "Pods", ".dart_tool", ".gradle", ".idea", ".vscode",
    "coverage", ".coverage", "htmlcov", ".mypy_cache", ".pytest_cache",
    ".ruff_cache", ".pytype", ".mini-code", ".mini-code-memory",
    ".mini-code-memory-local",
})


def iter_documents(docs_dir: Path | str) -> list[Document]:
    """递归遍历目录，解析所有受支持的文件。

    会跳过 ``SKIP_DIRS`` 中的目录、隐藏目录以及不支持/过大的文件。

    参数：
        docs_dir: 待索引的目录（也可以是单个文件）

    返回：
        ``Document`` 列表。
    """
    root = Path(docs_dir)
    documents: list[Document] = []

    if root.is_file():
        doc = parse(root)
        if doc is not None:
            documents.append(doc)
        return documents

    if not root.is_dir():
        return documents

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        # 跳过位于被忽略目录 / 隐藏目录下的文件
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            rel_parts = path.parts
        if any(part in SKIP_DIRS or part.startswith(".") for part in rel_parts[:-1]):
            continue
        if not is_supported(path):
            continue
        doc = parse(path)
        if doc is not None:
            documents.append(doc)

    return documents
