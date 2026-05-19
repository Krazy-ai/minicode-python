# MiniCode 项目安全隐患与适配性检查报告

## 报告摘要

本报告对 MiniCode 项目（Python 和 TypeScript 双版本）进行了全面的安全隐患和适配性检查，发现以下关键问题：

- **高危问题**: 4 个
- **中危问题**: 6 个  
- **低危问题**: 5 个
- **跨语言适配差异**: 10+ 个功能模块不对等

---

## 一、高危险隐患（需立即修复）

### 1.1 命令注入漏洞 - `mcp.ts`
**位置**: `ts-src/src/mcp.ts:308`  
**问题描述**: 
```typescript
const child = spawn(command, this.config.args ?? [], {
  cwd: this.config.cwd ? path.resolve(this.cwd, this.config.cwd) : this.cwd,
  ...
})
```
MCP 服务器的 `command` 和 `args` 直接从配置文件 (`mcp.json`) 读取后传给 `spawn()`，**没有进行任何验证或沙箱化**。恶意配置文件可执行任意命令。

**风险等级**: 🔴 高  
**影响范围**: 所有使用 MCP 集成的场景  
**修复建议**:
- 对 `command` 进行白名单验证（仅允许已知可执行文件路径）
- 使用 `which`/`where` 验证命令是否存在且位于预期路径
- 限制 `args` 中不允许包含 shell 元字符（如 `|`, `&&`, `;`, `` ` `` 等）
- 添加配置文件的签名验证机制

---

### 1.2 路径遍历漏洞 - `workspace.ts`
**位置**: `ts-src/src/workspace.ts:9-22`  
**问题描述**:
```typescript
const resolved = path.resolve(context.cwd, targetPath)
if (!context.permissions) {
  const workspaceRoot = path.resolve(context.cwd)
  const relative = path.relative(workspaceRoot, resolved)
  
  if (
    relative === '..' ||
    relative.startsWith(`..${path.sep}`) ||
    path.isAbsolute(relative)
  ) {
    throw new Error(`Path escapes workspace: ${targetPath}`)
  }
}
```

**缺陷**:
1. 对符号链接 (symlink) 无效，攻击者可通过 symlink 绕过检查
2. 当 `path.isAbsolute(relative)` 为 true 时，合法的相对路径（解析后仍在工作区内）会被误判为绝对路径而拒绝
3. Windows 路径分隔符问题：`path.sep` 在 Windows 是 `\`，但 JavaScript 也接受 `/`

**风险等级**: 🔴 高  
**影响范围**: 所有文件读写操作  
**修复建议**:
- 使用 `fs.realpath()` 对路径进行规范化后再检查
- 对符号链接进行解析和验证
- 同时检查 `../` 和 `..\` 两种前缀
- 统一使用权限模块的路径检查逻辑

---

### 1.3 不安全的文件写入 - `install.ts`
**位置**: `ts-src/src/install.ts:105-113`  
**问题描述**:
安装脚本在 `~/.local/bin/` 目录下创建 bash 启动脚本时：
1. 脚本内容中硬编码了 `repoRoot` 路径
2. 未检查 `launcherPath` 是否已存在，可能覆盖用户已有文件
3. 如果攻击者能篡改 `import.meta.url` 解析出的路径，可能导致执行恶意脚本

**风险等级**: 🔴 高  
**影响范围**: 安装过程  
**修复建议**:
- 在写入前检查文件是否已存在，存在则提示用户确认
- 验证 `repoRoot` 路径的合法性（确保在预期的安装目录内）
- 使用原子写入（先写临时文件再重命名）

---

### 1.4 敏感信息泄露风险 - `config.ts`
**位置**: `ts-src/src/config.ts:162-171`  
**问题描述**:
```typescript
return {
  model: model,
  baseUrl: base_url,
  authToken: auth_token,
  apiKey: api_key,
  maxOutputTokens: max_output_tokens,
  mcpServers: effective.get("mcpServers", {}),
  sourceSummary: `config: ${MINI_CODE_SETTINGS_PATH} > ${CLAUDE_SETTINGS_PATH} > process.env`,
}
```

`sourceSummary` 字段暴露了配置文件路径。如果此对象被日志记录或错误输出，可能泄露敏感信息的位置。

**风险等级**: 🟠 中高  
**影响范围**: 配置加载和日志记录  
**修复建议**:
- 不要在 `sourceSummary` 中包含配置文件路径
- 日志输出时对认证信息进行脱敏处理

---

## 二、中危险隐患

### 2.1 跨平台适配问题 - `bin/minicode`
**位置**: `ts-src/bin/minicode:1-7`  
**问题描述**: 启动脚本是纯 Bash 脚本，使用了 `BASH_SOURCE` 等 Bash 特有变量，在 Windows CMD/PowerShell 上无法直接使用。

**风险等级**: 🟡 中  
**修复建议**:
- 提供 Windows 的 `.cmd` 或 `.ps1` 启动脚本
- 或使用 Node.js 编写跨平台的启动器

---

### 2.2 资源泄漏 - `mcp.ts`
**位置**: `ts-src/src/mcp.ts:307-350`  
**问题描述**:
```typescript
async close(): Promise<void> {
  for (const pending of this.pending.values()) {
    clearTimeout(pending.timeout)
    pending.reject(
      new Error(`MCP server "${this.serverName}" closed before completing the request.`),
    )
  }
  this.pending.clear()

  if (!this.process) {
    this.protocol = null
    return
  }

  this.process.kill()  // 没有等待进程真正退出
  this.process = null
  this.protocol = null
}
```

`spawn` 创建的子进程在以下情况可能泄漏：
- `close()` 方法调用 `this.process.kill()` 后没有等待进程真正退出
- 如果子进程忽略 SIGTERM/SIGKILL，可能成为僵尸进程
- stdout/stderr 流没有显式销毁

**风险等级**: 🟡 中  
**修复建议**:
- `close()` 应等待进程退出后再清理
- 添加超时强制终止机制
- 显式调用 `child.stdout.destroy()` 和 `child.stderr.destroy()`

---

### 2.3 未处理的 Promise 拒绝 - `mcp.ts`
**位置**: `ts-src/src/mcp.ts:439, 467`  
**问题描述**:
```typescript
// handleStdoutChunk
this.handleMessage(JSON.parse(payload) as JsonRpcMessage)

// handleStdoutChunkAsLines
this.handleMessage(JSON.parse(line) as JsonRpcMessage)
```

`JSON.parse()` 如果解析失败会抛出异常，但该异常未被 try-catch 包裹，会导致未处理的 Promise 拒绝和进程崩溃。

**风险等级**: 🟡 中  
**修复建议**:
- 所有 `JSON.parse()` 调用都用 try-catch 包裹
- 对无效的 JSON 数据进行日志记录而非直接崩溃

---

### 2.4 路径遍历风险（符号链接绕过）- `permissions.ts`
**位置**: `ts-src/src/permissions.ts:57-63`  
**问题描述**:
```typescript
function isWithinDirectory(root: string, target: string): boolean {
  const relative = path.relative(root, target)
  return (
    relative === '' ||
    (!relative.startsWith(`..${path.sep}`) &&
      relative !== '..' &&
      !path.isAbsolute(relative))
  )
}
```

`isWithinDirectory` 函数使用 `path.relative()` 判断路径关系，但：
1. 没有处理符号链接，攻击者可以创建指向工作区外的符号链接来绕过此检查
2. 在 Windows 上，路径大小写不敏感的问题可能导致安全检查失效（如 `C:\Users` vs `c:\users`）

**风险等级**: 🟡 中  
**修复建议**:
- 使用 `fs.realpathSync()` 规范化路径后再比较
- Windows 上进行大小写规范化比较

---

### 2.5 配置竞态条件 - `config.ts`
**位置**: `ts-src/src/config.ts:123-134`  
**问题描述**:
1. `mergeSettings` 对 `env` 和 `mcpServers` 进行浅合并，如果配置项嵌套更深，会导致部分配置被覆盖丢失
2. 多个进程同时写入同一配置文件时可能产生竞态条件

**风险等级**: 🟡 中  
**修复建议**:
- 对深度嵌套对象使用深合并逻辑
- 文件写入使用文件锁或原子操作（先写 `.tmp` 再 `rename`）

---

### 2.6 危险命令检测可被绕过 - `permissions.ts`
**位置**: `ts-src/src/permissions.ts:103`  
**问题描述**:
```typescript
function classifyDangerousCommand(command: string, args: string[]): string | null {
  // ...
  if (
    command === 'node' ||
    command === 'python3' ||
    command === 'bun' ||
    command === 'bash' ||
    command === 'sh'
  ) {
    return `${command} can execute arbitrary local code (${signature})`
  }
}
```

对危险命令的检测仅基于简单的字符串匹配，容易被绕过：
- 可以使用绝对路径（如 `/usr/bin/bash` 而非 `bash`）
- 可以使用编码技巧绕过检测
- Python 版还检测了 `python`，但 TS 版缺失

**风险等级**: 🟡 中  
**修复建议**:
- 对命令进行规范化（使用 `which` 解析绝对路径）后再比对
- 支持正则表达式模式匹配，而非简单字符串包含
- 补充缺失的命令检测（如 `python`）

---

## 三、低危险隐患

### 3.1 类型安全问题 - `mcp.ts`
**位置**: `ts-src/src/mcp.ts` 多处  
**问题描述**: 大量使用 `as unknown` 和 `as` 类型断言绕过 TypeScript 类型检查，可能在运行时导致类型不匹配错误。

**风险等级**: 🟢 低  
**修复建议**:
- 使用 Zod 或其他运行时验证库替代类型断言
- 对 MCP 响应进行 schema 验证

---

### 3.2 Windows 路径分隔符问题
**位置**: `permissions.ts:59`, `workspace.ts:16`  
**问题描述**: 代码使用 `` `..${path.sep}` `` 检查路径遍历，在 Windows 上 `path.sep` 是 `\`，但 JavaScript 和许多 API 也接受 `/` 作为路径分隔符。

**风险等级**: 🟢 低中  
**修复建议**:
- 同时检查 `../` 和 `..\` 两种前缀
- 或使用 `path.normalize()` 标准化后再检查

---

### 3.3 无限循环风险 - `agent-loop.ts`
**位置**: `ts-src/src/agent-loop.ts:91`  
**问题描述**:
```typescript
for (let step = 0; maxSteps == null || step < maxSteps; step++) {
```

当 `maxSteps` 为 `null` 或 `undefined` 时，循环没有上限。虽然有 `emptyResponseRetryCount` 等计数器进行局部限制，但没有全局退出机制。

**风险等级**: 🟢 低  
**修复建议**:
- 为 `maxSteps` 设置合理的默认上限（如 50）
- 添加超时机制防止长时间运行

---

### 3.4 readline 资源泄漏 - `index.ts`
**位置**: `ts-src/src/index.ts:65, 136`  
**问题描述**: readline 接口在异常情况下可能未正确关闭。

**风险等级**: 🟢 低  
**修复建议**:
- 将 `rl.close()` 移到 `finally` 块中
- 或确保所有异常路径都能正确清理

---

### 3.5 环境变量覆盖风险 - `config.ts`
**位置**: `ts-src/src/config.ts:164-166`  
**问题描述**: 环境变量优先级设计不当：`process.env` 会覆盖配置文件中的设置。恶意进程可通过设置环境变量篡改行为。

**风险等级**: 🟢 低  
**修复建议**:
- 明确文档说明环境变量优先级
- 提供配置选项禁用环境变量覆盖

---

## 四、跨语言适配差异

### 4.1 严重功能缺失

| 缺失模块 | Python 有 | TypeScript 缺失 | 影响 |
|---------|----------|----------------|------|
| `session.py` (356行) | ✅ | ❌ | 无会话持久化与恢复 |
| `sub_agents.py` (366行) | ✅ | ❌ | 无子代理系统 |
| `auto_mode.py` (440行) | ✅ | ❌ | 无自动模式 |
| `memory.py` | ✅ | ❌ | 无三级记忆系统 |
| `context_manager.py` | ✅ | ❌ | 无上下文窗口管理 |
| `cost_tracker.py` | ✅ | ❌ | 无 API 成本追踪 |
| `hooks.py` | ✅ | ❌ | 无生命周期钩子 |
| `state.py` | ✅ | ❌ | 无应用状态管理 |
| `task_tracker.py` | ✅ | ❌ | 无任务追踪 |
| `poly_commands.py` | ✅ | ❌ | 无多态命令系统 |

### 4.2 工具模块差异

| 维度 | Python | TypeScript |
|------|--------|------------|
| 工具数量 | 29 个 | 11 个 |
| Python 独有工具 | `api_tester`, `ask_user`, `code_nav`, `code_review`, `db_explorer`, `diff_viewer`, `docker_helper`, `file_tree`, `git`, `governance_audit*`, `notebook_edit`, `run_with_debug`, `test_runner`, `todo_write`, `web_fetch`, `web_search` | - |

### 4.3 错误消息语言不一致

**问题**: TypeScript 版 `agent-loop.ts` 中多处错误消息使用中文，而 Python 版全部使用英文：

```typescript
// TS 版（中英文混杂）
' 诊断信息: ${parts.join('; ')}。'
'模型在 thinking 阶段触发 max_tokens，正在继续请求后续步骤...'
'工具执行后模型返回空响应，已停止当前回合。'
'达到最大工具步数限制，已停止当前回合。'

// Python 版（全英文）
f"Diagnostics: {'; '.join(parts)}."
"Model hit max_tokens during thinking; requesting the next step."
"Model returned an empty response after tool execution..."
"Reached the maximum tool step limit for this turn."
```

**建议**: 统一使用英文错误消息，或实现国际化支持。

### 4.4 测试覆盖率差异

| 维度 | Python | TypeScript |
|------|--------|------------|
| 测试文件数 | **15 个** | **0 个** |
| 测试框架 | pytest | 无配置 |
| 覆盖模块 | agent_loop, anthropic_adapter, cli_commands, config, mcp, mock_model, permissions, prompt, session, skills, tools, tui, tty_app | 无 |

**TS 版本完全没有项目级别的测试**，这是最严重的适配差异。

### 4.5 设计差异

| 维度 | Python | TypeScript |
|------|--------|------------|
| 核心循环 | 同步 | 异步 (`async/await`) |
| ToolDefinition | `validator` 函数 | Zod schema |
| Skill Install API | 位置参数 | 对象参数 `{cwd, sourcePath, name?, scope?}` |
| 包命名 | `minicode-py` | `mini-code` |

---

## 五、Python 特有问题

### 5.1 同步 vs 异步设计
**问题**: Python 核心循环为同步，而 TS 为异步。这导致：
- Python 版本无法利用异步 I/O 的优势
- MCP 客户端在 Python 中使用 `subprocess`+线程，性能不如 TS 的 `spawn`+事件

**建议**: 考虑将 Python 版本改为 `async/await` 模式

### 5.2 依赖管理
**问题**: `pyproject.toml` 中 `dependencies = []`，零依赖设计虽然轻量，但：
- 没有 HTTP 客户端库，如何实现 API 调用？
- 没有 JSON 验证库，配置解析可能失败

**检查**: 确认是否使用了标准库的 `urllib` 或 `http.client`

---

## 六、修复优先级建议

### P0 - 立即修复（高危）
1. ✅ `mcp.ts` 命令注入漏洞
2. ✅ `workspace.ts` 路径遍历漏洞
3. ✅ `install.ts` 不安全文件写入
4. ✅ `config.ts` 敏感信息泄露

### P1 - 尽快修复（中危）
5. `permissions.ts` 符号链接绕过
6. `mcp.ts` 未处理的 Promise 拒绝
7. `mcp.ts` 资源泄漏
8. `permissions.ts` 危险命令检测绕过
9. `config.ts` 配置竞态条件
10. `bin/minicode` 跨平台适配

### P2 - 计划修复（低危）
11. `mcp.ts` 类型安全问题
12. Windows 路径分隔符问题
13. `agent-loop.ts` 无限循环风险
14. `index.ts` readline 资源泄漏
15. `config.ts` 环境变量覆盖

### P3 - 功能对齐（适配性）
16. 为 TS 版本补充缺失的 10+ 个高级功能模块
17. 为 TS 版本补充缺失的 18+ 个工具
18. 统一错误消息语言
19. 为 TS 版本编写项目测试
20. 同步/异步设计统一

---

## 七、安全检查清单

- [ ] 命令注入防护
- [ ] 路径遍历防护
- [ ] 符号链接安全处理
- [ ] 配置文件验证
- [ ] 敏感信息脱敏
- [ ] 资源泄漏防护
- [ ] 错误处理完整性
- [ ] 跨平台兼容性
- [ ] 测试覆盖率
- [ ] 依赖安全性

---

## 八、总结

MiniCode 项目在以下方面表现良好：
- ✅ 权限管理系统设计完善
- ✅ MCP 集成架构合理
- ✅ 配置管理系统灵活
- ✅ 双版本代码结构一致

但需要重点关注：
- 🔴 4 个高危安全漏洞
- 🟡 6 个中危设计缺陷
- 📊 TypeScript 版本功能严重不足（缺失 10+ 模块、18+ 工具、0 测试）
- 🌐 跨语言适配差异较大

**建议**: 优先修复 P0 级别的安全隐患，然后逐步对齐两个版本的功能和测试覆盖率。

---

**报告生成日期**: 2026-04-05  
**检查范围**: `py-src/` 和 `ts-src/` 全部源代码  
**风险评级标准**: OWASP Risk Rating Methodology
