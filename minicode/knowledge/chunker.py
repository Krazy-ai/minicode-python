"""智能分块器：把 ``Document`` 切成 ``Chunk`` 列表。

按文件类型选择切分策略：

- **Markdown**：按 ``#`` / ``##`` / ``###`` 标题层级切分，记录标题路径。
- **代码**：按 ``def`` / ``class`` 顶层定义边界切分，保持定义整体不拆。
- **纯文本**：按段落（双换行）聚合，超长时用滑动窗口再切。

每个 ``Chunk`` 会附带 ``headings``（标题路径 / 符号名），供后续 rerank 加权。
"""

from __future__ import annotations

import re

from minicode.knowledge.types import Chunk, Document

# 默认分块参数
DEFAULT_MAX_CHUNK_SIZE = 512   # 目标 chunk 字符数
DEFAULT_OVERLAP = 50           # 滑动窗口重叠字符数
_MIN_CHUNK_SIZE = 24           # 太短的碎片直接并入上一块

_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_CODE_DEF_RE = re.compile(r"^(?:async\s+)?(?:def|class)\s+([A-Za-z_][\w]*)")


class _ChunkBuilder:
    """辅助累积 chunk，统一分配 position 与 id。"""

    def __init__(self, doc: Document) -> None:
        self.doc = doc
        self.chunks: list[Chunk] = []

    def add(self, text: str, headings: list[str]) -> None:
        text = text.strip("\n")
        if not text.strip():
            return
        position = len(self.chunks)
        self.chunks.append(
            Chunk(
                id=Chunk.make_id(self.doc.id, position),
                doc_id=self.doc.id,
                text=text,
                position=position,
                source_path=self.doc.source_path,
                headings=[h for h in headings if h],
                metadata={"mime": self.doc.mime},
            )
        )


def _sliding_window(
    text: str,
    max_size: int,
    overlap: int,
) -> list[str]:
    """对超长文本做滑动窗口切分（尽量在空白处断开）。"""
    text = text.strip()
    if len(text) <= max_size:
        return [text] if text else []

    windows: list[str] = []
    start = 0
    n = len(text)
    step = max(1, max_size - overlap)
    while start < n:
        end = min(n, start + max_size)
        # 尝试在窗口末尾附近的空白处断开，避免切碎单词
        if end < n:
            window = text[start:end]
            break_pos = max(window.rfind("\n"), window.rfind(" "))
            if break_pos > max_size // 2:
                end = start + break_pos
        chunk = text[start:end].strip()
        if chunk:
            windows.append(chunk)
        if end >= n:
            break
        start = max(end - overlap, start + step) if overlap else end
        if start <= 0:
            start = end
    return windows


def _chunk_markdown(
    doc: Document,
    max_size: int,
    overlap: int,
) -> list[Chunk]:
    """按标题层级切分 markdown。"""
    builder = _ChunkBuilder(doc)
    lines = doc.content.split("\n")

    # heading_stack: list of (level, title)，维护当前标题路径
    heading_stack: list[tuple[int, str]] = []
    buffer: list[str] = []

    def current_headings() -> list[str]:
        return [title for _, title in heading_stack]

    def flush() -> None:
        text = "\n".join(buffer).strip()
        buffer.clear()
        if not text:
            return
        if len(text) <= max_size:
            builder.add(text, current_headings())
        else:
            for window in _sliding_window(text, max_size, overlap):
                builder.add(window, current_headings())

    for line in lines:
        m = _MD_HEADING_RE.match(line)
        if m:
            # 遇到新标题：先把已积累内容切出去
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            # 弹出层级 >= 当前的标题
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
        else:
            buffer.append(line)

    flush()

    # 完全没有标题也没有内容 → 兜底整篇
    if not builder.chunks and doc.content.strip():
        for window in _sliding_window(doc.content, max_size, overlap):
            builder.add(window, [])

    return builder.chunks


def _chunk_code(
    doc: Document,
    max_size: int,
    overlap: int,
) -> list[Chunk]:
    """按顶层 def/class 边界切分代码，保持定义整体。"""
    builder = _ChunkBuilder(doc)
    lines = doc.content.split("\n")

    segments: list[tuple[str, list[str]]] = []  # (text, symbol names)
    current: list[str] = []
    current_symbols: list[str] = []

    def is_top_level_def(line: str) -> str | None:
        if line[:1] in (" ", "\t"):
            return None
        m = _CODE_DEF_RE.match(line)
        return m.group(1) if m else None

    for line in lines:
        symbol = is_top_level_def(line)
        if symbol is not None and current and any(s.strip() for s in current):
            segments.append(("\n".join(current), current_symbols))
            current = []
            current_symbols = []
        if symbol is not None:
            current_symbols.append(symbol)
        current.append(line)

    if current and any(s.strip() for s in current):
        segments.append(("\n".join(current), current_symbols))

    if not segments and doc.content.strip():
        segments.append((doc.content, []))

    for text, symbols in segments:
        text = text.strip("\n")
        if not text.strip():
            continue
        if len(text) <= max_size:
            builder.add(text, symbols)
        else:
            for window in _sliding_window(text, max_size, overlap):
                builder.add(window, symbols)

    return builder.chunks


def _chunk_plain(
    doc: Document,
    max_size: int,
    overlap: int,
) -> list[Chunk]:
    """按段落聚合 + 滑动窗口切分纯文本。"""
    builder = _ChunkBuilder(doc)
    paragraphs = re.split(r"\n\s*\n", doc.content)

    buffer = ""
    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_size:
            # 先把 buffer 冲掉
            if buffer.strip():
                builder.add(buffer, [])
                buffer = ""
            for window in _sliding_window(para, max_size, overlap):
                builder.add(window, [])
            continue
        candidate = f"{buffer}\n\n{para}" if buffer else para
        if len(candidate) > max_size:
            if buffer.strip():
                builder.add(buffer, [])
            buffer = para
        else:
            buffer = candidate

    if buffer.strip():
        builder.add(buffer, [])

    return builder.chunks


def _merge_tiny_chunks(chunks: list[Chunk]) -> list[Chunk]:
    """把过短的碎片并入上一块，避免噪声 chunk。"""
    if len(chunks) <= 1:
        return chunks
    merged: list[Chunk] = []
    for chunk in chunks:
        if (
            merged
            and len(chunk.text) < _MIN_CHUNK_SIZE
            and merged[-1].headings == chunk.headings
        ):
            merged[-1].text = f"{merged[-1].text}\n{chunk.text}"
        else:
            merged.append(chunk)
    # 重新分配 position / id 保证连续
    for i, chunk in enumerate(merged):
        chunk.position = i
        chunk.id = Chunk.make_id(chunk.doc_id, i)
    return merged


def chunk_document(
    doc: Document,
    *,
    max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """把一个 ``Document`` 切成 ``Chunk`` 列表。

    参数：
        doc: 待切分文档
        max_chunk_size: 目标 chunk 字符数上限
        overlap: 滑动窗口重叠字符数

    返回：
        ``Chunk`` 列表（保证覆盖全文，不丢文本）。
    """
    if not doc.content.strip():
        return []

    mime = doc.mime
    if mime == "text/markdown":
        chunks = _chunk_markdown(doc, max_chunk_size, overlap)
    elif mime in {
        "text/x-python", "text/x-typescript", "text/x-javascript",
        "text/x-go", "text/x-rust", "text/x-java", "text/x-c",
        "text/x-c++", "text/x-shellscript",
    }:
        chunks = _chunk_code(doc, max_chunk_size, overlap)
    else:
        chunks = _chunk_plain(doc, max_chunk_size, overlap)

    return _merge_tiny_chunks(chunks)
