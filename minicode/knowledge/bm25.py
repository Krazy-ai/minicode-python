"""共享的 TF-IDF / BM25 检索基础设施。

本模块是 ``memory`` 与 ``knowledge`` 两个子系统共用的检索内核：分词、词频/逆
文档频率计算、Okapi BM25 评分等。原先这些实现位于 ``memory/memory.py``，现
统一抽取到此处，避免重复实现。

分词同时产出英数词、单个 CJK 字符以及 CJK bigram，以提升中文文本的匹配效果。
"""
from __future__ import annotations

import math
import re
from collections import Counter


# 分词：英文/数字、单个 CJK 字符以及 CJK bigram
_WORD_RE = re.compile(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]')
_CJK_BIGRAM_RE = re.compile(r'[\u4e00-\u9fff]{2}')


def _tokenize(text: str) -> list[str]:
    """将文本分词，用于 TF-IDF / BM25 计分。

    会同时产出英数词、单个 CJK 字符以及 CJK bigram，
    以提升中文文本的语义匹配效果。
    """
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    cjk_bigrams = [match.lower() for match in _CJK_BIGRAM_RE.findall(text)]
    return tokens + cjk_bigrams


# BM25 参数
_BM25_K1 = 1.5  # 词频饱和系数
_BM25_B = 0.75  # 文档长度归一化系数


def _compute_tf(tokens: list[str]) -> dict[str, float]:
    """计算一组 token 的词频（TF）。"""
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    return {term: count / total for term, count in counts.items()}


def _compute_idf(documents: list[list[str]]) -> dict[str, float]:
    """跨文档计算逆文档频率（IDF）。

    使用平滑公式：log((N + 1) / (df + 1)) + 1
    """
    n = len(documents)
    if n == 0:
        return {}
    doc_freq: dict[str, int] = {}
    for doc_tokens in documents:
        seen = set(doc_tokens)
        for term in seen:
            doc_freq[term] = doc_freq.get(term, 0) + 1
    return {
        term: math.log((n + 1) / (df + 1)) + 1
        for term, df in doc_freq.items()
    }


def _compute_avgdl(documents: list[list[str]]) -> float:
    """计算文档平均长度。"""
    if not documents:
        return 0.0
    return sum(len(doc) for doc in documents) / len(documents)


def _bm25_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    idf: dict[str, float],
    avgdl: float,
    *,
    k1: float = _BM25_K1,
    b: float = _BM25_B,
) -> float:
    """计算查询与文档间的 Okapi BM25 得分。

    公式：
        score(q,d) = sum(IDF(qi) * (tf(qi,d) * (k1 + 1)) /
                         (tf(qi,d) + k1 * (1 - b + b * |d|/avgdl)))
    """
    if not query_tokens or not doc_tokens or avgdl == 0:
        return 0.0

    doc_len = len(doc_tokens)
    tf_doc = _compute_tf(doc_tokens)
    total_tokens = doc_len

    score = 0.0
    for term in set(query_tokens):
        if term not in idf:
            continue
        tf = tf_doc.get(term, 0.0)
        if tf == 0:
            continue
        numerator = tf * (k1 + 1)
        denominator = tf + k1 * (1 - b + b * (total_tokens / avgdl))
        score += idf[term] * (numerator / denominator)

    return score


def _tfidf_score(
    query_tokens: list[str],
    doc_tokens: list[str],
    idf: dict[str, float],
    avgdl: float = 0.0,
) -> float:
    """计算查询与文档间的 BM25 得分。

    注：函数名保留为 ``_tfidf_score`` 仅为向后兼容，
    内部已改用 BM25 评分以获得更好的短文本排序效果。
    """
    return _bm25_score(query_tokens, doc_tokens, idf, avgdl)


def get_tfidf_keywords(text: str, top_n: int = 10) -> list[tuple[str, float]]:
    """基于 TF 得分提取文本中最重要的前 N 个词。

    适用于自动归类、理解文本的核心主题等场景。

    参数：
        text: 待分析文本
        top_n: 返回的关键词数量

    返回：
        按重要度倒序的 (term, tf_score) 列表。
    """
    tokens = _tokenize(text)
    if not tokens:
        return []
    tf = _compute_tf(tokens)
    sorted_terms = sorted(tf.items(), key=lambda x: x[1], reverse=True)
    return sorted_terms[:top_n]


class BM25Index:
    """基于内存的轻量 BM25 索引，用于对一批文档/分块做检索。

    典型用法::

        index = BM25Index()
        index.add("doc1", "some text ...")
        index.add("doc2", "another text ...")
        index.finalize()
        results = index.search("query text", top_k=5)  # [(doc_id, score), ...]

    knowledge 子系统在纯 BM25 模式下用它对候选 chunk 召回打分。
    """

    def __init__(self) -> None:
        self._ids: list[str] = []
        self._doc_tokens: list[list[str]] = []
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0
        self._finalized = False

    def add(self, doc_id: str, text: str) -> None:
        """向索引追加一个文档/分块。"""
        self._ids.append(doc_id)
        self._doc_tokens.append(_tokenize(text))
        self._finalized = False

    def add_tokens(self, doc_id: str, tokens: list[str]) -> None:
        """以预分词的 token 列表追加（避免重复分词）。"""
        self._ids.append(doc_id)
        self._doc_tokens.append(list(tokens))
        self._finalized = False

    def finalize(self) -> None:
        """根据已加入的全部文档计算 IDF 与平均文档长度。"""
        self._idf = _compute_idf(self._doc_tokens)
        self._avgdl = _compute_avgdl(self._doc_tokens)
        self._finalized = True

    def search(self, query: str, top_k: int = 10) -> list[tuple[str, float]]:
        """检索：返回按 BM25 得分倒序的 ``(doc_id, score)`` 列表。"""
        if not self._finalized:
            self.finalize()
        if not self._ids:
            return []
        query_tokens = _tokenize(query)
        scored: list[tuple[str, float]] = []
        for doc_id, tokens in zip(self._ids, self._doc_tokens):
            score = _bm25_score(query_tokens, tokens, self._idf, self._avgdl)
            if score > 0:
                scored.append((doc_id, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]
