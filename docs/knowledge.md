# MiniCode 知识库（Knowledge / RAG）使用手册

> 为 agent 提供外部文档知识库子系统。默认零依赖（纯 BM25），
> 可选安装向量增强（`[rag-vector]`）启用 hybrid 检索。

---

## 1. 概述：knowledge vs memory

MiniCode 有两个互补的"知识"子系统，**互补不替换**：

| | `minicode/memory/` | `minicode/knowledge/` |
|---|---|---|
| 定位 | agent 自己的**内省记忆** | 外部**文档知识库** |
| 内容 | 过往决策、项目约定、模式 | README / 设计文档 / 代码 / 手册 |
| 注入方式 | 自动注入 system prompt | 工具按需调用 / `/ask` |
| 存储 | `.mini-code-memory/`（JSON + MEMORY.md） | `.mini-code/knowledge/<index>/index.db`（SQLite） |
| 检索 | BM25 + usage/recency | BM25 (+ 可选向量) + rerank |

两者共享底层的分词与 BM25 原语（`minicode/knowledge/bm25.py`）。

---

## 2. 快速开始

### 2.1 Agent 视角（工具）

agent 自动拥有三个工具：

- **`knowledge_ingest`**：构建/更新索引
  - `docs_dir`：要索引的目录（如 `./docs`）
  - `index_name`：索引名（默认 `default`）
  - `scope`：`project`（默认）或 `user`
- **`knowledge_query`**：检索知识库（只读）
  - `query`：自然语言问题
  - `top_k`：返回条数（1-20，默认 5）
- **`knowledge_status`**：查看索引状态（只读）

典型对话：

```
你：帮我把 ./docs 目录索引一下
agent →（调用 knowledge_ingest docs_dir="./docs"）

你：项目的鉴权流程是怎样的？
agent →（调用 knowledge_query query="鉴权流程"）→ 基于命中 chunk 回答，
        并标注 [source: docs/auth.md#Authentication]
```

当工作区存在任何索引时，system prompt 会自动追加提示，引导 agent
优先使用 `knowledge_query` 而非手动逐个读文件。

### 2.2 用户视角（`/ask`）

在 TUI 里直接问知识库，**不走 agent loop、不调工具**，纯生成：

```
/ask 这个项目的记忆系统有哪几种作用域？
```

流程：检索 top-k chunks → 拼装 `<context>` → 透传给当前模型作答 →
输出答案 + 折叠的引用清单。若模型不可用（离线/未配置），会优雅降级为
直接展示检索到的原文片段与来源。

其它命令：

- `/knowledge`：列出所有索引及其文档/chunk 数量。

---

## 3. 索引管理

### 3.1 作用域（scope）

- **项目级**（默认）：`<workspace>/.mini-code/knowledge/<index>/`
  跟代码走，可随仓库共享给团队。
- **用户级**：`~/.mini-code/knowledge/<index>/`
  跨项目复用（如个人常用参考资料）。

`query` / `status` 不指定 scope 时，会自动在 project → user 顺序查找。

### 3.2 多个索引

同一 workspace 可维护多个索引，用 `index_name` 区分，例如：

- `default`：项目文档
- `api`：第三方 API 参考
- `standards`：团队规范

### 3.3 增量索引

`ingest` 默认增量：用 `sha256(content)` 指纹比对，只重建**新增/修改**的
文档，删除源已消失的文档。未变化的文档跳过解析+分块，大幅加速重建。

### 3.4 存储布局

```
.mini-code/knowledge/<index_name>/
├── index.db      # SQLite：documents / chunks / inverted_index / manifest
├── vectors.db    # 【可选】sqlite-vec 向量表
└── meta.json     # 【可选】embedder / dimension / model
```

---

## 4. 配置项（settings.json）

全部可选，未配置时使用默认值：

```json
{
  "knowledge": {
    "default_index": "default",
    "default_scope": "project",

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

未设置 `embedding.enabled = true` 时，全部走纯 BM25。

---

## 5. 启用向量增强（可选）

默认零依赖纯 BM25 已能工作。若想启用 hybrid（BM25 + 向量 RRF 融合）：

### 5.1 安装 extras

```bash
pip install -e ".[rag-vector]"   # 引入 sqlite-vec（几百 KB 的 SQLite 扩展）
```

### 5.2 配置 embedding

在 `~/.mini-code/settings.json` 里：

```json
{
  "knowledge": {
    "embedding": {
      "enabled": true,
      "provider": "openai",
      "model": "text-embedding-3-small",
      "dimension": 1536,
      "api_key_env": "OPENAI_API_KEY"
    }
  }
}
```

并设置 `export OPENAI_API_KEY=sk-...`。

> embedding 通过外部 API 计算，**不在本地下载模型**。
> 之后 `ingest` 会自动为 chunk 计算向量并存入 `vectors.db`，
> `query` 会自动走 hybrid 检索。

### 5.3 降级行为

以下任一情况都会**透明降级**为纯 BM25，不报错、不阻塞：

- 未安装 `sqlite-vec`
- `embedding.enabled` 为 false
- 向量库未构建 / API key 缺失 / 网络失败

---

## 6. 评测

评测检索质量（MRR / Hit@K / Recall@K）：

```bash
python -m minicode.knowledge.eval --index default --dataset eval.jsonl
```

数据集为 JSONL，每行：

```json
{"query": "how does auth work", "relevant_chunks": ["<chunk_id>", "..."]}
```

`chunk_id` 形如 `<doc_id>#<position>`，可用 `knowledge_query` 的返回结果确定。

也可在代码里调用：

```python
from minicode.knowledge.eval import EvalSample, evaluate
report = evaluate([EvalSample(query="...", relevant_chunks={"..."})], index_name="default")
print(report.format())
```

---

## 7. 扩展点

- **新 embedder**：在 `minicode/knowledge/vector.py` 继承 `EmbeddingProvider`，
  实现 `embed_batch()`，并在 `create_provider()` 注册。
- **自定义 chunker**：在 `minicode/knowledge/chunker.py` 按 mime 增加分支。
- **新解析后缀**：在 `minicode/knowledge/parsers.py` 的 `_SUFFIX_MIME` 添加。
- **rerank 信号**：在 `minicode/knowledge/reranker.py` 的 `rerank()` 追加启发式。

---

## 8. FAQ

**Q：需要装什么依赖？**
A：默认零依赖（Python 标准库 + SQLite）。向量增强才需要 `sqlite-vec`。

**Q：中文检索效果如何？**
A：复用 memory 已有的 CJK bigram 分词 + 中英术语双向扩展，中文查询可用。

**Q：knowledge 会影响现有 memory 吗？**
A：不会。BM25 原语抽取为共享模块，memory 行为完全不变。

**Q：索引库会不会很大？**
A：SQLite + 倒排索引，通常远小于原始文档。`knowledge_status` 可查看大小。

**Q：`/ask` 和让 agent 回答有什么区别？**
A：`/ask` 是一次性纯生成（不调工具、不进 agent loop），更快更省；
让 agent 回答则可能触发多轮工具调用。
