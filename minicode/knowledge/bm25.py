"""共享的分词与 BM25 / TF-IDF 检索原语。

这些函数原先内嵌在 ``minicode/memory/memory.py``，为了让 ``memory`` 与新增的
``knowledge`` 子系统复用同一套检索逻辑（避免重复实现），统一抽取到本模块。

``memory/memory.py`` 通过 ``from minicode.knowledge.bm25 import ...`` 复用，
因此这些符号仍可从 ``minicode.memory.memory`` 访问（re-export，非垫片包装）。

包含：
- 分词：英文/数字词、单个 CJK 字符、CJK bigram
- 中英术语双向扩展表 ``_CODE_TERM_EXPANSIONS``
- BM25 打分（``_bm25_score``）及其依赖（TF / IDF / avgdl）
"""

from __future__ import annotations

import math
import re
from collections import Counter


# ---------------------------------------------------------------------------
# 分词
# ---------------------------------------------------------------------------

# 分词：英文/数字、单个 CJK 字符以及 CJK bigram
_WORD_RE = re.compile(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]')
_CJK_BIGRAM_RE = re.compile(r'[\u4e00-\u9fff]{2}')


# 中英术语互译映射表（双向扩展）
_CODE_TERM_EXPANSIONS: dict[str, list[str]] = {
    "函数": ["function", "func", "method"],
    "function": ["函数", "func", "method"],
    "func": ["函数", "function", "method"],
    "method": ["函数", "function", "func"],
    "类": ["class", "type"],
    "class": ["类", "type"],
    "type": ["类", "class"],
    "变量": ["variable", "var"],
    "variable": ["变量", "var"],
    "var": ["变量", "variable"],
    "参数": ["parameter", "param", "argument", "arg"],
    "parameter": ["参数", "param", "argument"],
    "param": ["参数", "parameter", "arg"],
    "argument": ["参数", "parameter", "arg"],
    "属性": ["attribute", "attr", "property", "prop"],
    "attribute": ["属性", "attr", "property"],
    "property": ["属性", "attr", "prop"],
    "接口": ["interface"],
    "interface": ["接口"],
    "模块": ["module"],
    "module": ["模块"],
    "包": ["package"],
    "package": ["包"],
    "方法": ["method", "function"],
    "对象": ["object", "obj"],
    "object": ["对象", "obj"],
    "继承": ["inherit", "inheritance", "extends"],
    "inherit": ["继承"],
    "多态": ["polymorphism"],
    "封装": ["encapsulation", "encapsulate"],
    "异常": ["exception", "error"],
    "exception": ["异常"],
    "error": ["错误", "异常"],
    "错误": ["error", "bug"],
    "bug": ["错误", "bug", "缺陷"],
    "循环": ["loop", "iteration", "iterate"],
    "loop": ["循环"],
    "条件": ["condition"],
    "condition": ["条件"],
    "数组": ["array"],
    "array": ["数组"],
    "列表": ["list"],
    "list": ["列表"],
    "字典": ["dict", "dictionary", "map"],
    "dict": ["字典", "dictionary"],
    "dictionary": ["字典", "dict"],
    "map": ["字典", "映射"],
    "映射": ["map"],
    "集合": ["set"],
    "set": ["集合"],
    "字符串": ["string", "str"],
    "string": ["字符串"],
    "整数": ["int", "integer"],
    "integer": ["整数"],
    "浮点": ["float"],
    "float": ["浮点"],
    "布尔": ["bool", "boolean"],
    "boolean": ["布尔"],
    "同步": ["sync", "synchronous"],
    "异步": ["async", "asynchronous"],
    "async": ["异步"],
    "回调": ["callback"],
    "callback": ["回调"],
    "事件": ["event"],
    "event": ["事件"],
    "装饰器": ["decorator"],
    "decorator": ["装饰器"],
    "生成器": ["generator"],
    "generator": ["生成器"],
    "迭代器": ["iterator"],
    "iterator": ["迭代器"],
    "测试": ["test", "testing"],
    "test": ["测试"],
    "调试": ["debug", "debugging"],
    "debug": ["调试"],
    "配置": ["config", "configuration"],
    "config": ["配置"],
    "数据库": ["database", "db"],
    "database": ["数据库", "db"],
    "缓存": ["cache"],
    "cache": ["缓存"],
    "队列": ["queue"],
    "queue": ["队列"],
    "栈": ["stack"],
    "stack": ["栈"],
    "树": ["tree"],
    "tree": ["树"],
    "图": ["graph"],
    "graph": ["图"],
    "搜索": ["search"],
    "search": ["搜索"],
    "排序": ["sort", "sorting"],
    "sort": ["排序"],
    "文件": ["file"],
    "file": ["文件"],
    "路径": ["path"],
    "path": ["路径"],
    "网络": ["network"],
    "network": ["网络"],
    "请求": ["request"],
    "request": ["请求"],
    "响应": ["response"],
    "response": ["响应"],
}


def _expand_query_terms(terms: list[str]) -> list[str]:
    """利用术语映射表扩展查询词。"""
    expanded = list(terms)
    for term in terms:
        if term in _CODE_TERM_EXPANSIONS:
            expanded.extend(_CODE_TERM_EXPANSIONS[term])
    return expanded


def _tokenize(text: str) -> list[str]:
    """将文本分词，用于 TF-IDF / BM25 计分。

    会同时产出英数词、单个 CJK 字符以及 CJK bigram，
    以提升中文文本的语义匹配效果。
    """
    tokens = [w.lower() for w in _WORD_RE.findall(text)]
    cjk_bigrams = [match.lower() for match in _CJK_BIGRAM_RE.findall(text)]
    return tokens + cjk_bigrams


# ---------------------------------------------------------------------------
# BM25 / TF-IDF
# ---------------------------------------------------------------------------

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
