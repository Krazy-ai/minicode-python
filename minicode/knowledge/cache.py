"""增量索引：文件指纹与变化检测。

用 sha256(content) 作为文档指纹，与 store 中已存的 ``content_hash`` 比对，
把当前文档集合分成三类：

- ``added``：索引里没有的新文档
- ``modified``：hash 变了的文档
- ``deleted``：索引里有、但本次遍历没出现的文档

只有 ``added`` + ``modified`` 需要重新解析/分块，``deleted`` 需要从索引删除。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from minicode.knowledge.types import Document


def content_hash(content: str) -> str:
    """文档内容指纹。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


@dataclass
class ChangeSet:
    """一次增量比对的结果。"""

    added: list[Document] = field(default_factory=list)
    modified: list[Document] = field(default_factory=list)
    unchanged: list[Document] = field(default_factory=list)
    deleted_ids: list[str] = field(default_factory=list)

    @property
    def to_index(self) -> list[Document]:
        """需要（重新）索引的文档 = added + modified。"""
        return self.added + self.modified

    def hash_for(self, doc: Document) -> str:
        return content_hash(doc.content)


def diff_documents(
    documents: list[Document],
    existing_hashes: dict[str, str],
) -> ChangeSet:
    """比对当前文档集合与已索引的 hash，得出变化集。

    参数：
        documents: 本次遍历得到的全部文档
        existing_hashes: store 中已存的 ``{doc_id: content_hash}``

    返回：
        ``ChangeSet``。
    """
    changes = ChangeSet()
    seen_ids: set[str] = set()

    for doc in documents:
        seen_ids.add(doc.id)
        chash = content_hash(doc.content)
        prev = existing_hashes.get(doc.id)
        if prev is None:
            changes.added.append(doc)
        elif prev != chash:
            changes.modified.append(doc)
        else:
            changes.unchanged.append(doc)

    changes.deleted_ids = list(existing_hashes.keys() - seen_ids)
    return changes
