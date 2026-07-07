# MiniCode 知识库（RAG）融合执行计划

> 版本：v1（方案 B：BM25 默认 + 可选向量增强）
> 范围：把多模态 RAG 项目的工程亮点融合进 MiniCode，作为 agent 的外部知识库子系统
> 原则：**默认零依赖**，向量能力作为 opt-in 可选 extras，不破坏现有运行体验

---

## 1. 背景与目标

### 1.1 现状

- MiniCode 已有的"记忆"是 **`minicode/memory/`**：BM25 检索 + 三层 scope 的短笔记，定位是 **agent 自己的内省记忆**。
- 想要新增的 **`minicode/knowledge/`**：为 agent 提供外部文档知识库（README / 设计文档 / 代码注释 / 项目手册等）。
- 两个子系统**互补不替换**：memory 自动注入 prompt，knowledge 通过工具按需调用。

### 1.2 核心目标

1. **服务 agent 自用**：编码时遇到不熟悉的代码/文档自动调工具查
2. **服务用户直问**：通过 `/ask` 命令直接问知识库
3. **零依赖默认能跑**：BM25 + 启发式 rerank
4. **可选增强**：装了 `[rag-vector]` extras 后启用 hybrid 检索（BM25 + 向量）

### 1.3 非目标

- ❌ 多模态（PDF/图片/OCR/VLM）
- ❌ 本地 embedding 模型（sentence-transformers 等）
- ❌ Chroma / FAISS（用更轻的 sqlite-vec 替代）
- ❌ 替换或修改现有 memory 子系统的功能

---

## 2. 架构设计

### 2.1 模块结构

```
minicode/knowledge/                  ← 新增子包
├── __init__.py
├── types.py                         # Document / Chunk / RetrievalResult / Citation
├── parsers.py                       # 纯文本解析（.md / .txt / .py / .ts / .json …）
├── chunker.py                       # 智能分块
├── bm25.py                          # BM25（从 memory 抽出来共享）
├── query_rewriter.py                # 查询改写
├── reranker.py                      # 启发式精排
├── store.py                         # SQLite 索引存储
├── pipeline.py                      # 入口：ingest / query / status
├── cache.py                         # 文件 hash 增量索引
├── eval.py                          # MRR / Hit@K / Recall@K 评测
├── vector.py                        # 【可选】向量 embedding 后端
└── hybrid.py                        # 【可选】BM25 + 向量 RRF 融合

minicode/tools/
├── knowledge_ingest.py              # agent 工具：建/更新索引
├── knowledge_query.py               # agent 工具：查询
└── knowledge_status.py              # agent 工具：查看状态

修改：
├── minicode/cli_commands.py         # 新增 /ask 斜杠命令
├── minicode/prompt/prompt.py        # 检测知识库存在时注入工具用法
└── pyproject.toml                   # 新增 [rag-vector] optional-dependencies
```

### 2.2 数据流

**离线索引**（`ingest`）：
```
docs/ 目录
  → parsers.py 读文件 → Document
  → chunker.py 切分    → List[Chunk]
  → store.py 写 SQLite（chunk 表 + 倒排表 + manifest 表）
  → cache.py 记录文件 hash（下次只重建变化的）
  → 【可选】vector.py 算向量并存 sqlite-vec 表
```

**在线检索**（`query`）：
```
用户/agent 查询
  → query_rewriter.py 扩展同义词 / 还原缩写
  → bm25.py 一路召回（top-50）
  → 【可选】vector.py 一路召回（top-50）
  → 【可选】hybrid.py RRF 融合
  → reranker.py 精排（top-K）
  → 返回 RetrievalResult（chunks + citations）
```

### 2.3 存储布局

```
.mini-code/knowledge/<index_name>/
├── index.db                  # SQLite：chunks / inverted_index / manifest
├── vectors.db                # 【可选】sqlite-vec 向量表
└── meta.json                 # index 配置：embedder/dimension/chunker 参数
```

- **项目级**（默认）：`<workspace>/.mini-code/knowledge/`
- **用户级**：`~/.mini-code/knowledge/`（跨项目复用）
- 由用户在 `ingest` 时通过参数选 scope

### 2.4 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| `/ask` 回答风格 | 透传给 LLM 基于 chunks 自答 | agent 助手定位最合理 |
| 被动 prefetch | 默认关，可在 settings 启用 | 性能与灵活性兼顾 |
| 索引作用域 | 都支持，默认项目级 | 跟代码走，团队可共享 |
| BM25 共享 | 硬移动到 `knowledge/bm25.py`，`memory/memory.py` 改 import | 模块级复用，避免重复实现 |
| 向量后端 | sqlite-vec（小） + embedding API（OpenAI/Anthropic） | 不下大模型、无重依赖 |
| 默认行为 | 零依赖纯 BM25 | 兼容现有零依赖定位 |

---

## 3. Milestone 与 TODO 列表

总计 **5 个 Milestone、22 个任务**。每个 Milestone 结束必须跑全量 pytest 验证 313 passed 不退化。

---

### M1：能跑通的最小骨架（7 任务）

> **目标**：能 `ingest("./docs")` + `query("xxx")` 用 BM25 出结果，纯零依赖。

#### TODO-1：建子包骨架 + 数据类型
- 文件：`minicode/knowledge/__init__.py`、`types.py`
- 内容：
  - `Document(id, source_path, content, mime, metadata)`
  - `Chunk(id, doc_id, text, position, headings, metadata)`
  - `RetrievalResult(chunks: list[Chunk], scores: list[float], query: str)`
  - `Citation(chunk_id, source_path, text_excerpt, score)`
- 验收：`python -c "from minicode.knowledge.types import Chunk; print(Chunk)"` 通过

#### TODO-2：纯文本解析器
- 文件：`minicode/knowledge/parsers.py`
- 内容：
  - 统一接口 `parse(path: Path) -> Document`
  - 支持后缀：`.md / .txt / .rst / .py / .ts / .js / .json / .yaml / .yml / .toml`
  - 编码自动探测（utf-8 / utf-8-sig / gbk fallback）
  - 大文件保护（>10MB skip + warn）
- 验收：单元测试覆盖 4 种后缀 + 编码异常 + 大文件

#### TODO-3：智能分块器
- 文件：`minicode/knowledge/chunker.py`
- 内容：
  - **Markdown 模式**：按 `#`/`##`/`###` 标题层级切，记录标题路径
  - **代码模式**：保留 `def` / `class` / 注释块整体不拆
  - **纯文本模式**：按段落（双换行）+ 滑动窗口（默认 512 字符 / 50 重叠）
  - 输出 `Chunk` 时附带 `headings: list[str]` 用于后续 rerank 加权
- 验收：单元测试 ≥ 4 个用例，验证切分正确 + 不丢文本

#### TODO-4：BM25 抽取共享
- 操作：把 `minicode/memory/memory.py` 中 `_BM25_K1`、`_BM25_B`、`_compute_idf`、`_bm25_score`、`_tokenize` 等抽到 `minicode/knowledge/bm25.py`
- 修改：`memory.py` 改成 `from minicode.knowledge.bm25 import ...`（硬移动，不留垫片）
- 验收：
  - `pytest tests/test_memory_e2e.py` 通过
  - 全量 pytest 313 passed
  - `grep -rn "_bm25_score\|_compute_idf" minicode/` 只剩 knowledge/bm25.py 一处定义

#### TODO-5：SQLite 索引存储
- 文件：`minicode/knowledge/store.py`
- 表结构：
  ```sql
  documents(id TEXT PRIMARY KEY, source_path TEXT, content_hash TEXT, mtime REAL, metadata TEXT)
  chunks(id TEXT PRIMARY KEY, doc_id TEXT, text TEXT, position INT, headings TEXT, metadata TEXT)
  inverted_index(token TEXT, chunk_id TEXT, tf INT, PRIMARY KEY(token, chunk_id))
  manifest(key TEXT PRIMARY KEY, value TEXT)
  ```
- 提供：`upsert_document / delete_document / get_chunks_for_query / list_documents`
- 验收：单元测试覆盖 CRUD + 倒排索引一致性

#### TODO-6：入口管线
- 文件：`minicode/knowledge/pipeline.py`
- 提供 3 个 API：
  - `ingest(docs_dir, index_name="default", scope="project") -> IngestReport`
  - `query(text, index_name="default", top_k=5) -> RetrievalResult`
  - `status(index_name="default") -> dict`
- 内部串联：parsers → chunker → store → bm25 检索
- 验收：手动 `python -c "from minicode.knowledge.pipeline import ingest, query; ingest('./docs'); print(query('memory system'))"` 能输出结果

#### TODO-7：M1 验收
- 写最小 e2e 测试 `tests/test_knowledge_basic.py`：
  - 用临时目录建 3 个 markdown 文件
  - 调 `ingest()`
  - 调 `query()` 验证能命中正确文件
  - 验证 SQLite 文件实际生成
- 全量 `pytest -q` 必须 ≥ 313 passed

---

### M2：亮点工程化（4 任务）

> **目标**：增量索引 + 查询改写 + Rerank。仍纯零依赖，但召回质量明显提升。

#### TODO-8：增量索引
- 文件：`minicode/knowledge/cache.py` + 修改 `pipeline.py`
- 内容：
  - 文件 hash（sha256）+ mtime 双重指纹
  - 三种变化检测：新增 / 修改 / 删除
  - 只重算变化的文档，未变化的跳过解析+分块
- 验收：单元测试模拟改一个文件 → 只该文件被重建

#### TODO-9：查询改写
- 文件：`minicode/knowledge/query_rewriter.py`
- 内容：
  - **代码术语字典**（约 50-100 条）：`auth → authentication, login, oauth, token`、`db → database, sql, sqlite`、`api → endpoint, rest, http`…
  - **缩写还原**：`fn → function, cfg → config, msg → message`
  - **大小写归一化** + **驼峰拆分**
  - 接口：`rewrite(query: str) -> list[str]`（返回原查询 + 扩展查询）
- 验收：单元测试 ≥ 6 个用例，覆盖术语扩展 + 缩写 + 驼峰

#### TODO-10：Rerank 精排
- 文件：`minicode/knowledge/reranker.py`
- 启发式信号：
  - **标题命中加权**：query token 命中 chunk 的 headings → +0.5 per hit
  - **长度惩罚**：偏好 200-800 字符的 chunk
  - **关键词覆盖率**：query 中有多少比例的 token 在 chunk 出现
  - **位置偏置**：靠近文档开头的 chunk 略加权
  - **代码块加分**：query 含代码迹象时加权代码 chunk
- 接口：`rerank(query, chunks, scores, top_k=5) -> list[(Chunk, float)]`
- 验收：单元测试 ≥ 5 个用例

#### TODO-11：M2 验收
- 单元测试覆盖 chunker / rewriter / reranker / cache
- 增量索引验证：build → 改 1 个文件 → rebuild → 只重建该文件
- 全量 pytest ≥ 313 passed

---

### M3：可选向量增强（4 任务）

> **目标**：装了 `[rag-vector]` extras 时自动启用 hybrid 检索；未装则透明降级。

#### TODO-12：向量后端抽象
- 文件：`minicode/knowledge/vector.py`
- 内容：
  - `EmbeddingProvider` 抽象基类
  - `OpenAIEmbeddingProvider`：调 `text-embedding-3-small`
  - `AnthropicEmbeddingProvider`：占位（Anthropic 暂无原生 embedding，可调 voyage-ai）
  - `MockEmbeddingProvider`：测试用
  - 统一 `embed_batch(texts: list[str]) -> list[list[float]]`
  - **依赖处理**：`try: import sqlite_vec except ImportError: SUPPORT = False`，未装时调用方收到清晰的错误信息

#### TODO-13：sqlite-vec 存储
- 修改：`minicode/knowledge/store.py` 加 `VectorStore` 子模块
- 内容：
  - 加载 sqlite-vec 扩展
  - 表 `chunk_vectors(chunk_id TEXT, embedding BLOB)`
  - `upsert_vectors / search_vectors(query_vec, top_k)`
- 配置：`meta.json` 记录维度 / provider / model name，避免维度不匹配

#### TODO-14：Hybrid 融合
- 文件：`minicode/knowledge/hybrid.py`
- 算法：**Reciprocal Rank Fusion (RRF)**
  ```
  rrf_score(chunk) = sum(1 / (k + rank_in_each_method))
  ```
- 接口：`hybrid_query(query, top_k=5) -> RetrievalResult`
- 启用条件：
  1. `pip install "minicode-py[rag-vector]"` 已装
  2. `settings.json` 配置 `embedding.provider`
  3. 索引时已写入向量
- 否则自动降级到纯 BM25
- 验收：用 mock provider 写测试，验证 hybrid 结果包含两路召回

#### TODO-15：M3 验收
- 单元测试覆盖 vector / hybrid（用 MockEmbeddingProvider）
- 集成测试：未装 sqlite-vec 时确认降级且无报错
- 全量 pytest ≥ 313 passed

---

### M4：Agent 集成与用户入口（3 任务）

> **目标**：让 agent 能调工具、用户能 `/ask`。

#### TODO-16：Agent 工具实现
- 文件：
  - `minicode/tools/knowledge_ingest.py` - 参数：`docs_dir`、`index_name`、`scope`
  - `minicode/tools/knowledge_query.py` - 参数：`query`、`index_name`、`top_k`
  - `minicode/tools/knowledge_status.py` - 参数：`index_name`
- 注册到 `minicode/tools/__init__.py` 的 `_CORE_TOOLS`
- 输出格式带 citation 引用，让 agent 能在回答里标 `[source: docs/auth.md#section]`
- 验收：单元测试 + 通过 ToolRegistry 调用

#### TODO-17：`/ask` 斜杠命令
- 修改：`minicode/cli_commands.py`
- 行为：
  1. 解析 `/ask <question>`
  2. 调 `pipeline.query(question, top_k=5)` 拿 chunks
  3. 拼装 prompt：`<context>\n{chunks}\n</context>\n\n回答这个问题：{question}`
  4. 透传给当前 model adapter，**不走 agent loop**（不调工具，纯生成）
  5. 输出答案 + 折叠的引用清单
- 验收：单元测试 + 手动验证

#### TODO-18：系统提示集成
- 修改：`minicode/prompt/prompt.py`
- 行为：检测到 `.mini-code/knowledge/<any>/index.db` 存在时，在系统提示里追加：
  ```
  This workspace has a knowledge base. When the user asks about project
  documentation, design decisions, or unfamiliar code, prefer using
  knowledge_query before reading files manually.
  ```
- 不存在则不追加，保持简洁
- 验收：snapshot 测试

---

### M5：测评、测试、文档（4 任务）

> **目标**：可量化质量、可信赖、可上手。

#### TODO-19：评测框架
- 文件：`minicode/knowledge/eval.py`
- 指标：
  - **MRR**（Mean Reciprocal Rank）
  - **Hit@K**（K=1,3,5,10）
  - **Recall@K**
- 数据集格式：JSONL，每行 `{"query": "...", "relevant_chunks": ["chunk_id_1", ...]}`
- 命令行入口：`python -m minicode.knowledge.eval --index <name> --dataset <file>`
- 验收：单元测试用 fixture 数据集跑一次，输出报告

#### TODO-20：测试套件
- 文件：`tests/test_knowledge.py`
- 覆盖（≥ 15 用例）：
  - parsers：4 种文件类型 + 编码异常
  - chunker：markdown / 代码 / 纯文本
  - bm25：算法正确性
  - query_rewriter：扩展逻辑
  - reranker：各信号生效
  - cache：增量检测
  - store：CRUD + 倒排
  - pipeline：端到端 ingest → query
  - hybrid：mock provider 下的融合
- 测试 fixtures：`tests/fixtures/knowledge_corpus/`（5-10 个 markdown 测试文档）

#### TODO-21：文档
- 文件：`docs/knowledge.md`
- 章节：
  1. 概述（vs memory 区别）
  2. 快速开始（agent 视角 + 用户 `/ask` 视角）
  3. 索引管理（项目级 / 用户级 / 多个 index）
  4. 配置项（settings.json schema）
  5. 启用向量增强（pip extras + provider 配置）
  6. 评测使用
  7. 扩展点（未来如何加新 embedder / 自定义 chunker）
  8. FAQ

#### TODO-22：全局验收
- ✅ 全量 `pytest -q` ≥ 313 passed（基线无退化）
- ✅ 4 个 console entry point 仍可正常 `python -c "import minicode.main"` 等
- ✅ `/ask` 命令实测一次（用 mock model 也行）
- ✅ Agent 工具 demo：手动启动 agent，让它用 knowledge_query
- ✅ 评测脚本能跑出数字
- ✅ 未装 sqlite-vec 时全部功能仍正常（降级路径）

---

## 4. pyproject.toml 改动

```toml
[project.optional-dependencies]
dev = ["pytest>=8.0.0"]

# 新增：可选的向量增强
rag-vector = [
    "sqlite-vec>=0.1.0",     # 几百 KB 的 SQLite 扩展
    # 注意：embedding 用 OpenAI/Anthropic API，不本地下模型
]
```

用户安装：

```bash
# 默认（零依赖，纯 BM25）
pip install -e .

# 启用向量增强
pip install -e ".[rag-vector]"
```

---

## 5. settings.json 新增配置

```json
{
  "model": "claude-sonnet-4-20250514",
  "knowledge": {
    "default_index": "default",
    "default_scope": "project",
    "passive_prefetch": false,
    "passive_prefetch_keywords": ["文档", "手册", "文档说", "doc says"],

    "chunker": {
      "max_chunk_size": 512,
      "overlap": 50
    },

    "embedding": {
      "enabled": false,
      "provider": "openai",
      "model": "text-embedding-3-small",
      "dimension": 1536,
      "api_key_env": "OPENAI_API_KEY"
    },

    "retrieval": {
      "default_top_k": 5,
      "bm25_top_n_for_rerank": 50,
      "vector_top_n_for_rerank": 50,
      "rrf_k": 60
    }
  }
}
```

未配置 `embedding.enabled = true` 时，全部用 BM25。

---

## 6. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 抽 BM25 时改坏 memory 模块 | 每步都跑 `pytest tests/test_memory_*` |
| sqlite-vec 在某些平台装不上 | 优雅降级，不阻塞主流程 |
| Embedding API key 缺失 | 启动时清晰报错，引导用户配置或关闭 vector |
| 索引数据库膨胀 | manifest 记录大小，提供 `compact` 命令 |
| 多个 agent 并发写索引 | SQLite WAL 模式 + 文件锁 |
| BM25 抽取后老的 memory 测试不过 | TODO-4 单独验收 + 全量回归 |
| 中文检索效果 | 复用 memory 已有的 CJK bigram 分词逻辑 |

---

## 7. 进度追踪表

| # | Milestone | TODO | 状态 |
|---|-----------|------|------|
| 1 | M1 | 子包骨架 + types | ✅ done |
| 2 | M1 | parsers | ✅ done |
| 3 | M1 | chunker | ✅ done |
| 4 | M1 | BM25 抽取共享 | ✅ done |
| 5 | M1 | SQLite store | ✅ done |
| 6 | M1 | pipeline 入口 | ✅ done |
| 7 | M1 | M1 验收 | ✅ done |
| 8 | M2 | 增量索引 cache | ✅ done |
| 9 | M2 | 查询改写 | ✅ done |
| 10 | M2 | Rerank | ✅ done |
| 11 | M2 | M2 验收 | ✅ done |
| 12 | M3 | 向量后端 vector.py | ✅ done |
| 13 | M3 | sqlite-vec 存储 | ✅ done |
| 14 | M3 | Hybrid 融合 | ✅ done |
| 15 | M3 | M3 验收 | ✅ done |
| 16 | M4 | agent 工具 | ✅ done |
| 17 | M4 | /ask 命令 | ✅ done |
| 18 | M4 | 系统提示集成 | ✅ done |
| 19 | M5 | 评测框架 | ✅ done |
| 20 | M5 | 测试套件 | ✅ done |
| 21 | M5 | 文档 | ✅ done |
| 22 | M5 | 全局验收 | ✅ done |

> **实现完成情况（实测）**：全量 `pytest -q` → **371 passed**（基线 313 + 新增 58），
> pre-existing 的 9 failed / 11 errors 计数保持不变（零回归）。BM25 定义仅剩
> `knowledge/bm25.py` 一处。4 个 console entry 均可 import。未装 sqlite-vec
> 时全部功能走 BM25 降级路径正常。eval CLI 输出 MRR/Hit@K/Recall@K 正常。

---

## 8. 预估工作量

| Milestone | 任务数 | 说明 |
|-----------|--------|------|
| M1 | 7 | 骨架 + 基础检索（核心工作量） |
| M2 | 4 | 增量 + 改写 + 精排 |
| M3 | 4 | 可选向量栈（中等） |
| M4 | 3 | 集成入口（简单） |
| M5 | 4 | 测评 + 测试 + 文档（重要） |

> 推荐执行顺序：M1 → M2 → M4 → M3 → M5。把 agent 集成（M4）放在 M3 前，是因为先有"能用"的 agent 工具能更早验证整体设计；向量增强可以最后再加。

---

## 9. 验收标准（全局）

完成全部 22 任务后，必须满足：

1. ✅ `pytest -q` ≥ 313 passed（无回归）
2. ✅ 4 个 console entry 可用：`minicode-py / -gateway / -headless / -cron`
3. ✅ 默认安装下零外部依赖
4. ✅ `/ask` 命令工作，输出含引用
5. ✅ Agent 能成功调 `knowledge_query` 并基于结果回答
6. ✅ 评测脚本输出 MRR / Hit@K 数字
7. ✅ 未装 sqlite-vec 时整套系统仍可用
8. ✅ 新增子包不影响现有 memory / tools / tui / agent / model / runtime / security / prompt
9. ✅ 文档 `docs/knowledge.md` 存在且涵盖所有用法

---

*Last updated: 2026-05-19*
