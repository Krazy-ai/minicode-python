"""智能分块器：把 :class:`Document` 切分为便于检索的 :class:`Chunk`。

根据文档类型选择不同策略：
- **Markdown**：按 ``#`` / ``##`` / ``###`` 标题层级切分，并记录每个 chunk 所处
  的标题路径（headings），供后续 rerank 加权。
- **代码**：以 ``def`` / ``class`` / 顶层注释块为边界尽量保持语义单元完整。
- **纯文本**：按段落（双换行）聚合，超长再用滑动窗口（默认 512 字符 / 50 重叠）切。

所有策略都保证「不丢文本」：切分后的 chunk 文本拼接（去重叠后）能覆盖原文有效内容。
"""
from __future__ import annotations

import re

from minicode.knowledge.types import Chunk, Document


# 默认分块参数
DEFAULT_MAX_CHUNK_SIZE = 512
DEFAULT_OVERLAP = 50

# Markdown ATX 标题：# ~ ###### 开头
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")

# 代码中的顶层定义边界（def / class / 装饰器 / export / function 等）
_CODE_DEF_RE = re.compile(
    r"^\s*(?:@|def\s|class\s|async\s+def\s|export\s|function\s|"
    r"public\s|private\s|protected\s|func\s|fn\s)"
)

_MARKDOWN_MIMES = {"text/markdown"}
_CODE_MIMES = {
    "text/x-python",
    "text/x-typescript",
    "text/javascript",
}


def chunk_document(
    doc: Document,
    max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE,
    overlap: int = DEFAULT_OVERLAP,
) -> list[Chunk]:
    """把文档切分为 chunk 列表。

    会根据 ``doc.mime`` 自动选择 markdown / 代码 / 纯文本策略。
    """
    if doc.mime in _MARKDOWN_MIMES:
        pieces = _chunk_markdown(doc.content, max_chunk_size, overlap)
    elif doc.mime in _CODE_MIMES:
        pieces = _chunk_code(doc.content, max_chunk_size, overlap)
    else:
        pieces = [
            (text, [])
            for text in _chunk_plain(doc.content, max_chunk_size, overlap)
        ]

    chunks: list[Chunk] = []
    position = 0
    for text, headings in pieces:
        if not text.strip():
            continue
        chunks.append(
            Chunk(
                id=Chunk.make_id(doc.id, position),
                doc_id=doc.id,
                text=text,
                position=position,
                headings=list(headings),
                metadata={
                    "source_path": doc.source_path,
                    "mime": doc.mime,
                    "is_code": doc.mime in _CODE_MIMES,
                },
            )
        )
        position += 1
    return chunks


# ---------------------------------------------------------------------------
# Markdown 分块
# ---------------------------------------------------------------------------

def _chunk_markdown(
    content: str, max_chunk_size: int, overlap: int
) -> list[tuple[str, list[str]]]:
    """按标题层级切分 markdown，记录每段所属的标题路径。"""
    lines = content.split("\n")
    sections: list[tuple[list[str], list[str]]] = []  # (heading_path, body_lines)
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    current_body: list[str] = []
    current_path: list[str] = []

    def _flush() -> None:
        if current_body:
            sections.append((list(current_path), list(current_body)))

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            # 标题行：先把前一段落 flush，再更新标题栈
            _flush()
            current_body.clear()
            level = len(m.group(1))
            title = m.group(2).strip()
            # 弹出层级 >= 当前的标题
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            current_path = [t for _, t in heading_stack]
            # 标题本身也作为正文的一部分保留
            current_body.append(line)
        else:
            current_body.append(line)
    _flush()

    # 若整篇没有任何标题，sections 可能为空，退化为纯文本切分
    if not sections:
        return [(text, []) for text in _chunk_plain(content, max_chunk_size, overlap)]

    pieces: list[tuple[str, list[str]]] = []
    for heading_path, body_lines in sections:
        body = "\n".join(body_lines).strip()
        if not body:
            continue
        if len(body) <= max_chunk_size:
            pieces.append((body, heading_path))
        else:
            # 段落过大，再用滑动窗口切，标题路径保持不变
            for sub in _chunk_plain(body, max_chunk_size, overlap):
                pieces.append((sub, heading_path))
    return pieces


# ---------------------------------------------------------------------------
# 代码分块
# ---------------------------------------------------------------------------

def _chunk_code(
    content: str, max_chunk_size: int, overlap: int
) -> list[tuple[str, list[str]]]:
    """按顶层 def/class 边界切分代码，尽量保持定义体完整。"""
    lines = content.split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        is_boundary = bool(_CODE_DEF_RE.match(line)) and _indent_of(line) == 0
        if is_boundary and current and any(s.strip() for s in current):
            blocks.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append(current)

    pieces: list[tuple[str, list[str]]] = []
    for block in blocks:
        text = "\n".join(block).strip("\n")
        if not text.strip():
            continue
        # 取该块的首个 def/class 名作为 heading 提示
        heading = _first_def_name(block)
        headings = [heading] if heading else []
        if len(text) <= max_chunk_size:
            pieces.append((text, headings))
        else:
            for sub in _chunk_plain(text, max_chunk_size, overlap):
                pieces.append((sub, headings))
    return pieces


def _indent_of(line: str) -> int:
    """返回一行的前导空白数（tab 记为 4）。"""
    n = 0
    for ch in line:
        if ch == " ":
            n += 1
        elif ch == "\t":
            n += 4
        else:
            break
    return n


def _first_def_name(block: list[str]) -> str:
    """从代码块中提取首个 def/class 名称作为 heading。"""
    name_re = re.compile(
        r"^\s*(?:async\s+def|def|class|function|func|fn)\s+([A-Za-z_][\w]*)"
    )
    for line in block:
        m = name_re.match(line)
        if m:
            return m.group(1)
    return ""


# ---------------------------------------------------------------------------
# 纯文本分块（段落 + 滑动窗口）
# ---------------------------------------------------------------------------

def _chunk_plain(content: str, max_chunk_size: int, overlap: int) -> list[str]:
    """按段落聚合，超长段落用滑动窗口切分。"""
    content = content.strip("\n")
    if not content.strip():
        return []
    if len(content) <= max_chunk_size:
        return [content]

    # 先按空行分段
    paragraphs = re.split(r"\n\s*\n", content)
    chunks: list[str] = []
    buffer = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue
        if len(para) > max_chunk_size:
            # 超长段落先把 buffer flush，再单独滑动窗口
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.extend(_sliding_window(para, max_chunk_size, overlap))
            continue
        if not buffer:
            buffer = para
        elif len(buffer) + len(para) + 2 <= max_chunk_size:
            buffer = f"{buffer}\n\n{para}"
        else:
            chunks.append(buffer)
            buffer = para
    if buffer:
        chunks.append(buffer)
    return chunks


def _sliding_window(text: str, size: int, overlap: int) -> list[str]:
    """对单段长文本做带重叠的滑动窗口切分。"""
    if overlap >= size:
        overlap = size // 4
    step = max(1, size - overlap)
    out: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(n, start + size)
        out.append(text[start:end])
        if end >= n:
            break
        start += step
    return out


# ---------------------------------------------------------------------------
# 分块器类（封装）
# ---------------------------------------------------------------------------

class MarkdownChunker:
    """Markdown 分块器。"""

    def __init__(self, max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP):
        self.max_chunk_size = max_chunk_size
        self.overlap = overlap

    def chunk_document(self, doc: Document) -> list[Chunk]:
        """分块文档。"""
        return chunk_document(doc, self.max_chunk_size, self.overlap)

    def chunk_text(self, text: str, source: str = "manual") -> list[Chunk]:
        """直接分块文本。"""
        from minicode.knowledge.types import Document
        doc = Document(
            id=Document.make_id(source),
            source_path=source,
            content=text,
            mime="text/markdown",
        )
        return self.chunk_document(doc)


class CodeChunker:
    """代码分块器。"""

    def __init__(self, max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP):
        self.max_chunk_size = max_chunk_size
        self.overlap = overlap

    def chunk_document(self, doc: Document) -> list[Chunk]:
        """分块文档。"""
        return chunk_document(doc, self.max_chunk_size, self.overlap)

    def chunk_text(self, text: str, source: str = "manual") -> list[Chunk]:
        """直接分块文本。"""
        from minicode.knowledge.types import Document
        doc = Document(
            id=Document.make_id(source),
            source_path=source,
            content=text,
            mime="text/x-python",
        )
        return self.chunk_document(doc)


class TextChunker:
    """纯文本分块器。"""

    def __init__(self, max_chunk_size: int = DEFAULT_MAX_CHUNK_SIZE, overlap: int = DEFAULT_OVERLAP):
        self.max_chunk_size = max_chunk_size
        self.overlap = overlap

    def chunk_document(self, doc: Document) -> list[Chunk]:
        """分块文档。"""
        return chunk_document(doc, self.max_chunk_size, self.overlap)

    def chunk_text(self, text: str, source: str = "manual") -> list[Chunk]:
        """直接分块文本。"""
        from minicode.knowledge.types import Document
        doc = Document(
            id=Document.make_id(source),
            source_path=source,
            content=text,
            mime="text/plain",
        )
        return self.chunk_document(doc)


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------

__all__ = [
    "chunk_document",
    "MarkdownChunker",
    "CodeChunker",
    "TextChunker",
]
