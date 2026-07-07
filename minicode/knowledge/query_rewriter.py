"""查询改写：扩展同义词、还原缩写、拆分驼峰、大小写归一化。

目标是提升召回率——把一个查询扩展成若干变体，让 BM25 能匹配到更多相关 chunk。

接口：``rewrite(query) -> list[str]``，返回 ``[原查询, 扩展变体...]``（去重）。
"""

from __future__ import annotations

import re

from minicode.knowledge.bm25 import _CODE_TERM_EXPANSIONS

# 常见缩写还原表
_ABBREVIATIONS: dict[str, list[str]] = {
    "fn": ["function"],
    "func": ["function"],
    "cfg": ["config", "configuration"],
    "config": ["configuration"],
    "msg": ["message"],
    "err": ["error"],
    "auth": ["authentication", "authorization", "login", "oauth", "token"],
    "authn": ["authentication"],
    "authz": ["authorization"],
    "db": ["database", "sql", "sqlite"],
    "api": ["endpoint", "rest", "http", "interface"],
    "ui": ["interface", "frontend"],
    "cli": ["command", "terminal"],
    "repo": ["repository"],
    "dir": ["directory", "folder"],
    "env": ["environment"],
    "var": ["variable"],
    "arg": ["argument", "parameter"],
    "param": ["parameter", "argument"],
    "impl": ["implementation"],
    "init": ["initialize", "initialization"],
    "async": ["asynchronous"],
    "sync": ["synchronous"],
    "ctx": ["context"],
    "req": ["request"],
    "res": ["response", "result"],
    "resp": ["response"],
    "docs": ["documentation", "document"],
    "doc": ["documentation", "document"],
    "pkg": ["package"],
    "mgr": ["manager"],
    "util": ["utility", "utilities"],
    "utils": ["utility", "utilities"],
    "vec": ["vector"],
    "emb": ["embedding"],
    "idx": ["index"],
    "perm": ["permission"],
    "perms": ["permissions"],
    "mem": ["memory"],
    "regex": ["regexp", "regular expression"],
}

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9]*|[\u4e00-\u9fff]+")


def _split_camel(word: str) -> list[str]:
    """拆分驼峰/蛇形命名：``getUserName`` → ``[get, User, Name]``。"""
    # 先按下划线/连字符拆
    parts: list[str] = []
    for piece in re.split(r"[_\-]+", word):
        if not piece:
            continue
        parts.extend(p for p in _CAMEL_RE.split(piece) if p)
    return parts


def rewrite(query: str) -> list[str]:
    """把查询扩展成多个变体。

    步骤：
        1. 原始查询（保底）
        2. 小写归一化版本
        3. 驼峰/蛇形拆分后的版本
        4. 术语/缩写扩展词追加

    参数：
        query: 原始查询字符串

    返回：
        去重后的查询变体列表（第一个始终是原查询）。
    """
    query = (query or "").strip()
    if not query:
        return []

    variants: list[str] = [query]

    lowered = query.lower()
    if lowered != query:
        variants.append(lowered)

    # 驼峰/蛇形拆分
    tokens = _TOKEN_RE.findall(query)
    split_parts: list[str] = []
    for tok in tokens:
        pieces = _split_camel(tok)
        if len(pieces) > 1:
            split_parts.extend(pieces)
    if split_parts:
        variants.append(" ".join(split_parts))

    # 缩写 / 术语扩展
    expansions: list[str] = []
    all_lower_tokens = [t.lower() for t in tokens] + [p.lower() for p in split_parts]
    for tok in all_lower_tokens:
        if tok in _ABBREVIATIONS:
            expansions.extend(_ABBREVIATIONS[tok])
        if tok in _CODE_TERM_EXPANSIONS:
            expansions.extend(_CODE_TERM_EXPANSIONS[tok])
    if expansions:
        variants.append(" ".join(dict.fromkeys(expansions)))

    # 去重，保序
    return list(dict.fromkeys(v for v in variants if v.strip()))
