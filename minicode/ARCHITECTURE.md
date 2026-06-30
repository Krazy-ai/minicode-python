# `minicode/` 目录详解

> MiniCode 的 Python 实现版本：一个轻量、可嵌入、跨平台的终端 Coding Agent。
> 本文从「整体定位 → 顶层文件 → 子包」三个层次详细介绍 `minicode/` 包内每一份代码的作用、相互关系与关键设计决策。

---

## 0. 一句话概览

`minicode/` 是整个仓库的核心包，把一个完整的 **LLM 编码代理** 拆成 8 个子包 + 16 个顶层文件，总计约 **25 700 行 Python**。

```
minicode/                 16 files / 3 797 lines     入口、配置、协议、工具骨架
├── agent/                 5 files / 1 645 lines     Agent 主循环 + 工具调度 + 错误恢复
├── memory/                7 files / 3 699 lines     上下文窗口 + 跨会话记忆 + 工作记忆
├── model/                 6 files / 1 946 lines     LLM provider 适配（Anthropic/OpenAI/OpenRouter/Mock）
├── prompt/                5 files / 1 166 lines     System prompt 构建、skills、USER.md
├── runtime/               8 files / 2 311 lines     全局 Store、hooks、成本统计、日志
├── security/              5 files / 1 463 lines     权限管理、Auto 模式、worktree 隔离
├── tools/                30 files / 5 470 lines     30+ 内置工具（文件/Git/Web/代码导航等）
└── tui/                  19 files / 4 183 lines     全屏终端 UI（transcript / 输入 / 渲染）
```

---

## 1. 整体架构

### 1.1 启动到完成一轮对话的流程

```
┌──────────────────────────────────────────────────────────────────────┐
│  入口: console_scripts → minicode.main:main                          │
└──────────────────────────────────────────────────────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  config.py  加载 runtime（env + ~/.mini-code/settings.json）   │
   └───────────────────────────────────────────────────────────────┘
                 │
                 ▼
   ┌─────────────────────────────────┐    ┌───────────────────────┐
   │  tools/  创建 ToolRegistry      │◀───┤  mcp.py  接入 MCP 服务  │
   └─────────────────────────────────┘    └───────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  security/permissions  生成 PermissionManager                  │
   └───────────────────────────────────────────────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  model/model_registry  根据模型名创建 ModelAdapter             │
   └───────────────────────────────────────────────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  memory/  初始化 ContextManager、MemoryManager                 │
   │  prompt/  build_system_prompt（注入 skills / MCP / memory）    │
   │  runtime/state  创建全局 Store                                  │
   └───────────────────────────────────────────────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  TTY?  ── yes ──▶  tui/  + tty_app.run_tty_app                 │
   │         ── no  ──▶  stdin 行模式（main.py 内）                 │
   └───────────────────────────────────────────────────────────────┘
                 │
                 ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  agent/agent_loop.run_agent_turn                               │
   │      ┌─ model.next() → AgentStep                               │
   │      ├─ 若 tool_calls → ToolScheduler 并发/串行执行             │
   │      │     └─ 失败 → ErrorClassifier + NudgeGenerator           │
   │      ├─ 若 progress → 注入 NUDGE_CONTINUE 再循环                │
   │      └─ 若 final/空响应 → 结束本轮                              │
   └───────────────────────────────────────────────────────────────┘
```

### 1.2 横切关注点（Cross-cutting）

| 关注点 | 责任模块 | 触发点 |
| --- | --- | --- |
| 权限审批 | `security/permissions.py` | 工具调用前、文件/命令执行前 |
| 上下文压缩 | `memory/context_manager.py` | 每轮 agent_loop 结束 |
| 记忆注入 | `memory/memory.py`、`prompt/prompt.py` | 每轮 build_system_prompt |
| 成本统计 | `runtime/cost_tracker.py`、`model/*_adapter.py` | 每次 LLM API 调用 |
| Hook 事件 | `runtime/hooks.py` | AGENT_START / PRE_TOOL_USE / POST_TOOL_USE / AGENT_STOP |
| 日志 | `runtime/logging_config.py` | 各模块通过 `get_logger(name)` 获取 |
| 全局状态 | `runtime/state.py` | Store 在各处共享只读视图与 mutator |

---

## 2. 顶层文件（`minicode/*.py`）

> 这些文件不属于任何子包，定义了进程入口、跨子包的协议契约、与磁盘/外部进程打交道的边界。

### 2.1 入口与命令行

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `__init__.py` | 2 | 包标识，纯占位 |
| `main.py` | 433 | **CLI 主入口**（`minicode-py`）。负责解析 argparse、装配 runtime/tools/permissions/model/memory/store，按 TTY 与否分别走 TUI 或 stdin 行模式 |
| `tty_app.py` | ~700 | **全屏 TUI 主循环**。后台 agent 线程、键盘事件路由、实时 transcript 渲染、交互式权限弹窗、会话自动保存 |
| `headless.py` | ~250 | **无头执行模式**（CI/CD/管道）。一次性接收一个 prompt，跑完即退；适合 `docker compose run` |
| `gateway.py` | ~150 | **零依赖 HTTP 网关**。仅用 stdlib 实现 `/health` 与 `POST /run`，供平台桥接二次封装 |
| `cron_runner.py` | ~130 | **定时任务运行器**（`minicode-cron`）。读 JSON 配置批量跑 headless 任务，支持 `cron.example.json` 模板 |
| `install.py` | ~250 | **交互式安装向导**（`--install`）。配置模型/API key，把 launcher 装到平台专属 bin 目录 |

### 2.2 命令路由

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `cli_commands.py` | ~220 | **TUI 内部斜杠命令**。`SLASH_COMMANDS` 元数据 + `try_handle_local_command`（`/help` `/status` `/mcp` `/sessions` 等）。直接返回字符串，不进 agent loop |
| `local_tool_shortcuts.py` | ~100 | **工具快捷调用解析**。把 `/grep pattern::src/` 这种语法翻译成 `{"toolName": "grep_files", "input": {...}}`，由 ToolRegistry 执行 |
| `manage_cli.py` | ~200 | **管理子命令**（`minicode mcp list/add/remove`、`minicode skills ...`、`minicode valid-config`）。每个子命令支持 `--project/--user` 切换 scope |

### 2.3 协议与契约

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `types.py` | ~90 | **核心类型契约**：`ChatMessage` / `ToolCall` / `AgentStep` / `StepDiagnostics` / `ModelAdapter` Protocol。所有跨子包传递的数据结构都在这里 |
| `tooling.py` | 396 | **工具系统骨架**：`ToolDefinition` / `ToolRegistry` / `ToolContext` / `ToolResult` / `BackgroundTaskResult`。还实现了智能截断 `_smart_truncate_output`（按工具类型 head/tail 保留） |
| `workspace.py` | ~40 | **工作区路径解析门卫**。所有文件类工具都应通过 `resolve_tool_path()` 把用户输入转绝对路径并触发权限检查 |

### 2.4 持久化与外部接入

| 文件 | 行数 | 角色 |
| --- | --- | --- |
| `config.py` | ~600 | **配置加载与诊断**。合并 env / `~/.mini-code/settings.json` / `~/.claude/settings.json` / 项目级 `.mcp.json`；含模型名拼写建议、provider 校验、诊断报告 |
| `history.py` | ~35 | **命令历史持久化**。`~/.mini-code/history.json`，最多 200 条，供 TUI 上下方向键回溯 |
| `mcp.py` | ~700 | **MCP 协议集成**。最小但安全的 stdio MCP 客户端：白名单命令（禁 `sh/bash/cmd`）、危险字符过滤、50MB payload 上限、懒加载、远端 tools/resources/prompts 包装成本地 ToolDefinition |
| `cron.example.json` | — | 定时任务示例配置 |

---

## 3. 子包详解

### 3.1 `agent/` — Agent 主循环（1 645 行）

> **MiniCode 的"心脏"**。把 LLM 调用、工具执行、错误恢复、上下文管理串成一个稳定的多轮循环。

```
agent/
├── __init__.py
├── agent_loop.py          ← run_agent_turn，model→tool→model 循环
├── agent_intelligence.py  ← ErrorClassifier / NudgeGenerator / ToolScheduler
├── agent_metrics.py       ← 每步耗时与成败计数器
└── agent_protocol.py      ← 跨子包共享的 agent 协议常量
```

**关键设计**

- **progress/final 协议**：assistant 文本以 `<progress>` 开头表示中间进度，需自动注入 `NUDGE_CONTINUE` 让模型继续；`<final>` 表示真正完结。
- **ToolScheduler 双批**：只读工具走并发批（ThreadPoolExecutor），写文件 / 命令执行走串行批，保证副作用顺序。
- **错误自动恢复**：失败的工具结果会被 `ErrorClassifier` 归类（`syntax` / `permission` / `timeout` / `not_found` 等），再由 `NudgeGenerator` 生成定向恢复提示重新喂给模型。
- **Metrics**：`agent_metrics.AgentMetricsCollector` 在每步记录耗时、token、工具成败，供 `/status` 命令展示。

---

### 3.2 `model/` — LLM 适配层（1 946 行）

```
model/
├── __init__.py
├── model_registry.py     ← Provider 检测、ModelInfo 目录、create_model_adapter 工厂
├── anthropic_adapter.py  ← Claude Messages API（含 prompt cache & streaming）
├── openai_adapter.py     ← OpenAI / OpenRouter / 自定义 OpenAI 兼容端点
├── mock_model.py         ← 离线/测试用的规则模拟器
└── api_retry.py          ← 语义错误分类 + 自适应指数退避
```

**关键设计**

- **统一工厂**：`create_model_adapter(model, tools, runtime, force_mock)` 一处替代了原本散落在 `main.py / headless.py / gateway.py` 的 if/elif。
- **Provider 自动检测**：`detect_provider()` 按 `OPENROUTER_API_KEY` → 模型前缀 → `OPENAI_API_KEY` → `CUSTOM_API_BASE_URL` → 默认 Anthropic 的顺序匹配。
- **自适应重试**：`api_retry.classify_error()` 把错误分为 `RATE_LIMIT` / `OVERLOAD` / `NETWORK_ERROR` / `AUTH_ERROR` / `INPUT_ERROR`，分类决定退避倍率与最大重试次数。
- **Prompt cache**：Anthropic adapter 利用 `SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 让静态前缀跨会话缓存。

---

### 3.3 `memory/` — 上下文与记忆（3 699 行）

> 三层记忆系统，让 agent 既能在长对话中保持稳定，又能跨会话沉淀项目知识。

```
memory/
├── __init__.py
├── context_manager.py    ← Token 估算、多级压缩、ContextManager
├── memory.py             ← 三层 MEMORY.md (user/project/local) + BM25/TF-IDF 检索
├── memory_injector.py    ← 任务相关记忆按需注入 system prompt
├── working_memory.py     ← 压缩过程中保护"活跃任务"上下文
├── context_isolation.py  ← 子 agent 的隔离上下文沙箱
└── session.py            ← 会话持久化（含 delta 增量保存）
```

**关键设计**

- **多级压缩**：`ContextManager` 维护压缩级别（0/1/2/3），首轮压缩目标 70%，逐次更激进至 30%；压缩策略含 5 个阶段：删 progress → 截大结果 → 压缩 tool 对 → 优先级删除 → 加分层摘要。
- **三层 MEMORY.md**：USER（跨项目，`~/.mini-code/memory/`） / PROJECT（项目共享，可入仓） / LOCAL（项目内不入库）。检索使用 BM25 + 中英术语扩展（`函数 ↔ function/method`）。
- **工作记忆保护**：`working_memory.WorkingMemoryTracker` 把活跃任务相关上下文标记为受保护，压缩时强制保留。
- **会话增量保存**：`session.py` 用 delta 文件机制，每 N 次 delta 才做一次完整 dump，大幅降低长会话 I/O 开销。

---

### 3.4 `security/` — 权限与隔离（1 463 行）

```
security/
├── __init__.py
├── permissions.py    ← PermissionManager / PermissionGate / 路径归一化 LRU
├── auto_mode.py      ← 4 种权限模式（default/auto/bypass/plan）+ 危险命令检测
├── file_review.py    ← Edit 工具的统一 diff 预览与审批流
└── safe_execution.py ← worktree 隔离执行高风险命令
```

**关键设计**

- **三种权限决策模式**：`allow_once / allow_always / allow_turn / allow_all_turn / deny_*` —— 与 TS 版完全对齐。
- **路径归一化缓存**：`_normalize_path` 用 `functools.lru_cache(maxsize=512)`，避免每次工具调用都做昂贵的 `Path.resolve()` syscall。
- **危险命令分级**：`auto_mode.py` 维护 `SAFE / LOW / MEDIUM / HIGH / DANGEROUS` 五级，含 `rm -rf /`、`curl|sh`、`mkfs` 等正则黑名单。
- **Worktree 隔离**：`safe_execution.WorktreeIsolator` 为高风险命令创建临时 git worktree，执行完自动清理，主工作区不受污染。

---

### 3.5 `prompt/` — Prompt 构建与技能（1 166 行）

```
prompt/
├── __init__.py
├── prompt.py          ← build_system_prompt（动态段落组装）
├── prompt_pipeline.py ← PromptSection / PromptPipeline / 文件 mtime 缓存
├── skills.py          ← SKILL.md 发现/加载/安装/卸载
└── user_profile.py    ← USER.md 解析、合并、CLI 命令处理
```

**关键设计**

- **段落级缓存边界**：`SYSTEM_PROMPT_DYNAMIC_BOUNDARY` 字符串把 prompt 划成静态前缀（永久缓存）+ 动态后缀（每轮重建），适配 Anthropic prompt cache。
- **Skills 多源发现**：搜索顺序 `<cwd>/.mini-code/skills/` → `~/.mini-code/skills/` → `<cwd>/.claude/skills/` → `~/.claude/skills/`，重名时取首个。
- **USER.md 合并**：项目级覆盖全局级，所有标量字段「project 非空 → 用 project，否则用 global」，列表字段做去重合并。

---

### 3.6 `runtime/` — 运行时基础设施（2 311 行）

```
runtime/
├── __init__.py
├── state.py            ← Zustand 风格 Store / mutator / 订阅者通知
├── hooks.py            ← 生命周期 Hook 事件总线（PreToolUse/PostToolUse/Stop 等）
├── cost_tracker.py     ← Token & 成本统计、各模型定价表
├── logging_config.py   ← 分级日志、轮转、JSON 格式
├── background_tasks.py ← 后台子进程注册表 + 槽位管理
├── task_graph.py       ← 跨步骤工作流 DAG + 持久化任务节点
└── task_tracker.py     ← 单轮 todo 跟踪，渲染到 TUI 进度条
```

**关键设计**

- **Zustand 风格 Store**：`create_app_store(initial)` 返回带 `get_state / set_state / subscribe` 的不可变更新容器，跨子包共享只读视图。
- **Hook 事件**：参考 Claude Code，`fire_hook_sync(HookEvent.PRE_TOOL_USE, ...)` 支持外部脚本、日志、自定义行为挂载。
- **跨平台进程探活**：`background_tasks._is_process_alive(pid)` 在 Windows 用 ctypes 调 `OpenProcess + GetExitCodeProcess`，Unix 用 `os.kill(pid, 0)`。
- **任务图 vs 任务跟踪**：`task_graph` 是 **持久化、跨步骤** 的工作流定义；`task_tracker` 是 **会话内** 的临时 todo 渲染。两者明确解耦。

---

### 3.7 `tools/` — 内置工具集（5 470 行 / 30 文件）

> 所有真正"做事"的工具都在这里。每个工具暴露三件套：`description`（给 LLM）+ `input_schema`（JSON Schema）+ 执行函数。

| 类别 | 工具 | 文件 |
| --- | --- | --- |
| 用户交互 | `ask_user` | `ask_user.py` |
| 文件读写 | `list_files` / `read_file` / `write_file` / `modify_file` / `edit_file` / `patch_file` | `list_files.py` `read_file.py` ... |
| 批量操作 | `batch_copy` / `batch_move` / `batch_delete` | `batch_ops.py` |
| 内容搜索 | `grep_files` / `file_tree` | `grep_files.py` `file_tree.py` |
| 代码智能 | `find_symbols` / `find_references` / `get_ast_info` / `code_review` | `code_nav.py` `code_review.py` |
| 命令执行 | `run_command` | `run_command.py` |
| 子 agent | `task` | `task.py` |
| Git 工作流 | `git` | `git.py` |
| 网络 | `web_fetch` / `web_search` / `http_request` | `web_fetch.py` `web_search.py` `http_utils.py` |
| 任务管理 | `todo_write` | `todo_write.py` |
| Skill 加载 | `load_skill` | `load_skill.py` |
| 可视化 | `diff_viewer` | `diff_viewer.py` |
| 测试 | `test_runner` | `test_runner.py` |
| **可选 utility 集** | base64/url 编解码、json/csv/regex/hash/uuid/text/archive/crypto | `encoding_utils.py` `json_utils.py` `csv_utils.py` `regex_utils.py` `crypto_utils.py` `text_utils.py` `archive_utils.py` |

**关键设计**

- **Profile 分层装载**：`MINI_CODE_TOOL_PROFILE=core`（默认）只加载常用工具；`full/all/utility` 才加载完整 utility 集，避免对模型暴露过多工具。
- **Edit 工具的精确匹配**：`edit_file.py` 支持精确字符串匹配 + 模糊空白 + 多次命中检测 + 不匹配行号诊断，替代 patch 类工具的脆弱性。
- **task 工具的子 agent**：`task.py` 启动一个独立 agent loop，自带 system prompt、过滤后的工具集、轮次上限，结果摘要回主上下文 —— 隔离上下文污染。

---

### 3.8 `tui/` — 全屏终端 UI（4 183 行）

```
tui/
├── __init__.py
├── chrome.py           ← banner / panel / status_line / slash_menu / permission_prompt
├── screen.py           ← 屏幕原语：alternate screen、隐藏光标
├── theme.py            ← 莫兰迪低饱和度配色（ANSI 256 / 24-bit）
├── markdown.py         ← 终端 Markdown 渲染（语法高亮、表格、列表、引用）
├── transcript.py       ← 对话历史渲染（含工具输出折叠）
├── renderer.py         ← 整屏组装与脏区刷新
├── input.py            ← 输入提示符
├── input_parser.py     ← 键盘事件解析（光标键/鼠标滚轮/粘贴）
├── input_handler.py    ← 输入分发到命令/工具快捷调用/agent
├── navigation.py       ← 历史回溯 / 自动补全
├── event_flow.py       ← 全屏事件主循环
├── session_flow.py     ← /resume 会话恢复流程
├── tool_helpers.py     ← 工具调用展示辅助
├── tool_lifecycle.py   ← 工具调用 lifecycle UI 状态机
├── state.py            ← TUI 局部状态
├── runtime_control.py  ← 暂停/继续/取消等运行控制
├── ui_hints.py         ← 屏幕底部提示行
└── types.py            ← TranscriptEntry 等 UI 类型
```

**关键设计**

- **alternate screen 双缓冲**：进入/退出 TUI 时通过 `enter_alternate_screen / exit_alternate_screen` 保留终端原始内容。
- **后台 agent 线程**：`tty_app.py` 把 `run_agent_turn` 放到独立线程，主线程专注 60fps 渲染与键盘事件。
- **工具输出折叠**：长输出默认折叠到 3 行预览，按 Enter 展开全文；transcript 自动滚动跟随。

---

## 4. 数据流与生命周期关键链路

### 4.1 一次工具调用的完整路径

```
LLM 返回 tool_calls
    ↓
agent_loop.run_agent_turn
    ↓
agent_intelligence.ToolScheduler.schedule(calls)
    ↓ (拆并发批/串行批)
ToolRegistry.execute(name, input, ToolContext)
    ↓
tooling._smart_truncate_output           ← 智能截断超长输出
    ↓
runtime/hooks.fire_hook_sync(PRE_TOOL_USE)
    ↓
security/permissions.PermissionManager   ← 路径/命令/编辑审批
    ↓
工具实现函数 (tools/*.py)
    ↓
ToolResult(ok=..., output=..., backgroundTask=...)
    ↓
runtime/hooks.fire_hook_sync(POST_TOOL_USE)
    ↓
agent_metrics 记录耗时与状态
    ↓ (失败时)
ErrorClassifier + NudgeGenerator → 自动恢复提示
    ↓
追加为 tool_result 消息，回到 LLM
```

### 4.2 一次完整会话的状态拓扑

```
runtime/state.Store (单例)
    ├── session_id, workspace, model
    ├── tool_calls_count, total_cost_usd
    ├── context_usage_pct, api_error_count
    └── busy / idle

memory/ContextManager
    ├── messages[]                  ← 完整对话历史
    ├── _token_cache                ← 按消息 id 缓存 token 数
    └── compaction_history[]        ← 历次压缩记录

memory/MemoryManager
    ├── memories[USER]              ← MemoryFile（entries[]）
    ├── memories[PROJECT]
    └── memories[LOCAL]

security/PermissionManager
    ├── workspace_root              ← 已归一化
    ├── session_allowed_paths       ← 本会话审批通过的路径
    ├── allowed_directory_prefixes  ← 持久化到 permissions.json
    └── turn_allowed_edits          ← 本轮内允许的编辑（end_turn 重置）
```

---

## 5. 阅读建议

如果你是第一次接触这个项目，推荐按下面的顺序阅读：

1. **入口与配置**：`main.py` → `config.py` → `tooling.py` → `types.py`（30 分钟内可以掌握「外部调用怎么进入 agent loop」）
2. **核心循环**：`agent/agent_loop.py` → `agent/agent_intelligence.py`（理解 model→tool→model 怎么转起来）
3. **模型与权限**：`model/model_registry.py` → `security/permissions.py`（理解请求怎么发出、副作用怎么把关）
4. **记忆与压缩**：`memory/context_manager.py` → `memory/memory.py`（理解长对话稳定性的来源）
5. **工具实现**：从 `tools/read_file.py` 与 `tools/edit_file.py` 入门，再看 `tools/task.py`（子 agent）和 `tools/run_command.py`（命令执行）
6. **UI 与体验**：`tty_app.py` → `tui/event_flow.py` → `tui/renderer.py`（理解全屏 TUI 怎么搭起来的）

---

## 6. 参考

- 顶层 `docs/RAG_INTEGRATION_PLAN.md`：把 RAG 能力融合进 memory 子系统的方案
- `docs/`：其它运行时与部署文档
- 仓库根 `README.md`：用户向使用文档

> 本文档自动跟随代码演进。如发现描述与实际不符，欢迎更新此文件。
