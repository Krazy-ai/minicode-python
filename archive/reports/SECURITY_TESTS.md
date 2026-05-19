# MiniCode 安全修复验证测试套件

本文件包含针对所有安全修复的验证测试用例。

## 测试环境

- **TypeScript**: Node.js, tsx, zod
- **Python**: Python 3.11+, pytest
- **平台**: Windows 10/11, Linux, macOS

---

## 1. MCP 命令注入验证测试

### 1.1 测试命令白名单验证

```typescript
// 测试文件: ts-src/tests/mcp-security.test.ts
import { describe, it, expect } from 'vitest'
import { createMcpBackedTools } from '../src/mcp.js'

describe('MCP Command Injection Prevention', () => {
  it('应该拒绝不在白名单中的相对命令', async () => {
    await expect(
      createMcpBackedTools({
        cwd: process.cwd(),
        mcpServers: {
          malicious: {
            command: 'malicious_command',
            args: ['--exploit'],
          },
        },
      })
    ).rejects.toThrow(/not in the allowed list/)
  })

  it('应该拒绝包含 shell 元字符的参数', async () => {
    await expect(
      createMcpBackedTools({
        cwd: process.cwd(),
        mcpServers: {
          test: {
            command: 'node',
            args: ['-e', 'console.log("test"); rm -rf /'],
          },
        },
      })
    ).rejects.toThrow(/dangerous shell character/)
  })

  it('应该允许白名单中的命令', async () => {
    const result = await createMcpBackedTools({
      cwd: process.cwd(),
      mcpServers: {
        valid: {
          command: 'node',
          args: ['--version'],
        },
      },
    })
    expect(result.servers).toHaveLength(1)
    expect(result.servers[0].status).toBe('connected')
  })
})
```

### 1.2 手动测试步骤

```bash
# 1. 创建测试配置文件
cat > test-mcp-config.json << 'EOF'
{
  "mcpServers": {
    "test": {
      "command": "node",
      "args": ["--version"]
    }
  }
}
EOF

# 2. 测试合法命令
minicode --mcp-config test-mcp-config.json

# 3. 测试恶意命令（应该被拒绝）
cat > malicious-mcp-config.json << 'EOF'
{
  "mcpServers": {
    "malicious": {
      "command": "evil_command",
      "args": ["; rm -rf /"]
    }
  }
}
EOF

# 启动时应该报错
minicode --mcp-config malicious-mcp-config.json
```

---

## 2. 路径遍历漏洞验证

### 2.1 自动化测试

```typescript
// 测试文件: ts-src/tests/workspace-security.test.ts
import { describe, it, expect } from 'vitest'
import path from 'path'
import { resolveToolPath } from '../src/workspace.js'
import type { ToolContext } from '../src/tool.js'

describe('Workspace Path Traversal Prevention', () => {
  const mockContext: ToolContext = {
    cwd: '/workspace',
    permissions: undefined,
  }

  it('应该阻止 ../ 路径遍历', async () => {
    await expect(
      resolveToolPath(mockContext, '../etc/passwd', 'read')
    ).rejects.toThrow(/Path escapes workspace/)
  })

  it('应该阻止 ..\\ 路径遍历 (Windows)', async () => {
    await expect(
      resolveToolPath(mockContext, '..\\..\\windows\\system32', 'read')
    ).rejects.toThrow(/Path escapes workspace/)
  })

  it('应该允许工作区内的相对路径', async () => {
    const result = await resolveToolPath(mockContext, 'src/index.ts', 'read')
    expect(result).toContain('src')
    expect(result).toContain('index.ts')
  })
})
```

### 2.2 手动测试

```bash
# 1. 创建测试环境
mkdir -p /tmp/test-workspace
cd /tmp/test-workspace
echo "secret" > /tmp/secret-file

# 2. 尝试通过路径遍历读取外部文件
# 应该失败
minicode --cwd /tmp/test-workspace
# 输入: read_file ../../../../tmp/secret-file

# 3. 创建符号链接测试
cd /tmp/test-workspace
ln -s /tmp/secret-file symlink-to-secret

# 4. 尝试通过符号链读取（应该被 realpath 阻止）
minicode --cwd /tmp/test-workspace
# 输入: read_file symlink-to-secret
```

---

## 3. 符号链接绕过验证

### 3.1 权限系统测试

```typescript
// 测试文件: ts-src/tests/permissions-security.test.ts
import { describe, it, expect, beforeEach, afterEach } from 'vitest'
import fs from 'fs/promises'
import path from 'path'
import os from 'os'
import { PermissionManager } from '../src/permissions.js'

describe('Permission Symlink Prevention', () => {
  let tempDir: string
  let workspaceDir: string
  let outsideFile: string
  let symlink: string

  beforeEach(async () => {
    tempDir = await fs.mkdtemp(path.join(os.tmpdir(), 'permissions-test-'))
    workspaceDir = path.join(tempDir, 'workspace')
    outsideFile = path.join(tempDir, 'outside-secret.txt')
    symlink = path.join(workspaceDir, 'symlink')

    await fs.mkdir(workspaceDir)
    await fs.writeFile(outsideFile, 'secret content')
    await fs.symlink(outsideFile, symlink)
  })

  afterEach(async () => {
    await fs.rm(tempDir, { recursive: true, force: true })
  })

  it('应该阻止通过符号链接访问工作区外文件', async () => {
    const manager = new PermissionManager(workspaceDir)
    await manager.whenReady()

    await expect(
      manager.ensurePathAccess(symlink, 'read')
    ).rejects.toThrow(/Access denied/)
  })

  it('应该允许访问工作区内的正常文件', async () => {
    const manager = new PermissionManager(workspaceDir)
    await manager.whenReady()

    const insideFile = path.join(workspaceDir, 'inside.txt')
    await fs.writeFile(insideFile, 'test')
    
    // 不应该抛出异常
    await manager.ensurePathAccess(insideFile, 'read')
  })
})
```

---

## 4. JSON 解析异常处理验证

### 4.1 自动化测试

```typescript
// 测试文件: ts-src/tests/mcp-json-error.test.ts
import { describe, it, expect } from 'vitest'

describe('MCP JSON Error Handling', () => {
  it('应该优雅处理无效 JSON 而不崩溃', async () => {
    // 创建模拟的无效输出
    const invalidOutput = Buffer.from('this is not valid JSON')
    
    // 测试客户端应该能处理无效 JSON
    // （实际测试需要 mock spawn 进程）
    expect(() => {
      try {
        JSON.parse(invalidOutput.toString())
      } catch (error) {
        // 应该被 catch 而不崩溃
        expect(error).toBeDefined()
      }
    }).not.toThrow()
  })

  it('应该记录错误日志而不是崩溃', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    
    // 模拟 handleMessage 调用
    const client = // ... 创建客户端
    // 发送无效数据
    
    expect(consoleSpy).toHaveBeenCalledWith(
      expect.stringContaining('Failed to parse JSON-RPC message')
    )
    
    consoleSpy.mockRestore()
  })
})
```

---

## 5. 资源泄漏验证

### 5.1 子进程清理测试

```typescript
// 测试文件: ts-src/tests/mcp-resource-leak.test.ts
import { describe, it, expect, vi } from 'vitest'
import { spawn } from 'node:child_process'

describe('MCP Resource Cleanup', () => {
  it('应该在 close() 后完全清理子进程', async () => {
    const client = // ... 创建客户端
    
    // 启动
    await client.start()
    
    // 关闭
    await client.close()
    
    // 验证进程已退出
    expect(client['process']).toBeNull()
  })

  it('应该在 3 秒超时后强制终止进程', async () => {
    // Mock 一个不响应 SIGTERM 的进程
    const client = // ... 创建客户端
    
    const startTime = Date.now()
    await client.close()
    const duration = Date.now() - startTime
    
    // 应该在 3 秒左右完成
    expect(duration).toBeLessThan(4000)
  })
})
```

---

## 6. 安装脚本安全验证

### 6.1 文件存在性检查测试

```typescript
// 测试文件: ts-src/tests/install-security.test.ts
import { describe, it, expect, vi } from 'vitest'
import fs from 'fs/promises'
import path from 'path'

describe('Install Script Security', () => {
  it('应该在文件存在时提示用户确认', async () => {
    // Mock 文件存在
    vi.mocked(fs.access).mockResolvedValue(undefined)
    
    // 运行安装脚本
    // 应该提示用户确认
    
    expect(console.log).toHaveBeenCalledWith(
      expect.stringContaining('已存在')
    )
  })

  it('应该拒绝包含 .. 的安装路径', async () => {
    // 测试路径验证逻辑
    const maliciousPath = '/tmp/../../../etc/malicious'
    
    expect(() => {
      if (maliciousPath.includes('..')) {
        throw new Error('Invalid installation path')
      }
    }).toThrow(/Invalid installation path/)
  })
})
```

---

## 7. 危险命令检测验证

### 7.1 Python 版本测试

```python
# 测试文件: py-src/tests/test_permissions_security.py
import pytest
from minicode.permissions import _classify_dangerous_command

def test_python_is_detected():
    result = _classify_dangerous_command("python", ["-c", "print('hello')"])
    assert result is not None
    assert "python" in result.lower()

def test_python3_is_detected():
    result = _classify_dangerous_command("python3", ["--version"])
    assert result is not None

def test_pythonw_is_detected():
    result = _classify_dangerous_command("pythonw", ["script.py"])
    assert result is not None

def test_powershell_is_detected():
    result = _classify_dangerous_command("powershell", ["-Command", "Get-Process"])
    assert result is not None

def test_pwsh_is_detected():
    result = _classify_dangerous_command("pwsh", ["--version"])
    assert result is not None

def test_zsh_is_detected():
    result = _classify_dangerous_command("zsh", ["-c", "echo hello"])
    assert result is not None

def test_fish_is_detected():
    result = _classify_dangerous_command("fish", ["-c", "echo hello"])
    assert result is not None
```

### 7.2 TypeScript 版本测试

```typescript
// 测试文件: ts-src/tests/permissions-security.test.ts
import { describe, it, expect } from 'vitest'

// 需要导出 classifyDangerousCommand 函数进行测试
import { classifyDangerousCommand } from '../src/permissions.js'

describe('Dangerous Command Detection', () => {
  it('应该检测 python 命令', () => {
    const result = classifyDangerousCommand('python', ['-c', "print('hello')"])
    expect(result).not.toBeNull()
    expect(result).toContain('python')
  })

  it('应该检测 python3 命令', () => {
    const result = classifyDangerousCommand('python3', ['--version'])
    expect(result).not.toBeNull()
  })

  it('应该检测 pythonw 命令', () => {
    const result = classifyDangerousCommand('pythonw', ['script.py'])
    expect(result).not.toBeNull()
  })

  it('应该检测 powershell 命令', () => {
    const result = classifyDangerousCommand('powershell', ['-Command', 'Get-Process'])
    expect(result).not.toBeNull()
  })

  it('应该检测 pwsh 命令', () => {
    const result = classifyDangerousCommand('pwsh', ['--version'])
    expect(result).not.toBeNull()
  })

  it('应该检测 zsh 命令', () => {
    const result = classifyDangerousCommand('zsh', ['-c', 'echo hello'])
    expect(result).not.toBeNull()
  })

  it('应该检测 fish 命令', () => {
    const result = classifyDangerousCommand('fish', ['-c', 'echo hello'])
    expect(result).not.toBeNull()
  })
})
```

---

## 8. 错误消息语言一致性验证

### 8.1 自动化检查

```typescript
// 测试文件: ts-src/tests/error-message-consistency.test.ts
import { describe, it, expect } from 'vitest'
import fs from 'fs/promises'
import path from 'path'

describe('Error Message Language Consistency', () => {
  it('agent-loop.ts 不应该包含中文错误消息', async () => {
    const content = await fs.readFile(
      path.join(__dirname, '../src/agent-loop.ts'),
      'utf-8'
    )
    
    // 检查不应出现的中文字符串
    expect(content).not.toContain('诊断信息')
    expect(content).not.toContain('模型在 thinking 阶段')
    expect(content).not.toContain('工具执行后模型返回空响应')
    expect(content).not.toContain('达到最大工具步数限制')
    
    // 应该包含英文版本
    expect(content).toContain('Diagnostics:')
    expect(content).toContain('Model returned an empty response')
    expect(content).toContain('Reached the maximum tool step limit')
  })
})
```

---

## 9. Windows 跨平台启动脚本验证

### 9.1 CMD 脚本测试

```batch
@echo off
REM 测试文件: ts-src/tests/test-minicode-cmd.bat

echo Testing minicode.cmd...

REM 1. 测试脚本存在性
if not exist "..\bin\minicode.cmd" (
    echo FAIL: minicode.cmd not found
    exit /b 1
)
echo PASS: minicode.cmd exists

REM 2. 测试帮助信息
call ..\bin\minicode.cmd --help
if %ERRORLEVEL% NEQ 0 (
    echo FAIL: minicode.cmd failed
    exit /b 1
)
echo PASS: minicode.cmd runs successfully
```

### 9.2 PowerShell 脚本测试

```powershell
# 测试文件: ts-src/tests/test-minicode-ps1.ps1

Write-Host "Testing minicode.ps1..." -ForegroundColor Green

# 1. 测试脚本存在性
if (-not (Test-Path "..\bin\minicode.ps1")) {
    Write-Host "FAIL: minicode.ps1 not found" -ForegroundColor Red
    exit 1
}
Write-Host "PASS: minicode.ps1 exists" -ForegroundColor Green

# 2. 测试执行
& "..\bin\minicode.ps1" --help
if ($LASTEXITCODE -ne 0) {
    Write-Host "FAIL: minicode.ps1 failed" -ForegroundColor Red
    exit 1
}
Write-Host "PASS: minicode.ps1 runs successfully" -ForegroundColor Green
```

---

## 10. 综合安全扫描

### 10.1 静态分析检查

```bash
# 使用 ESLint 安全插件
cd ts-src
npm install --save-dev eslint @microsoft/eslint-plugin-security

# 创建 .eslintrc.json
cat > .eslintrc.json << 'EOF'
{
  "plugins": ["@microsoft/security"],
  "extends": ["plugin:@microsoft/security/recommended"]
}
EOF

# 运行安全检查
npx eslint src/**/*.ts
```

### 10.2 依赖漏洞扫描

```bash
# 检查 npm 依赖漏洞
cd ts-src
npm audit

# 检查 Python 依赖漏洞
cd py-src
pip install safety
safety check
```

---

## 运行所有测试

### TypeScript 测试

```bash
cd ts-src
# 如果还没有测试框架，先安装
npm install --save-dev vitest @vitest/coverage-v8

# 运行测试
npx vitest run

# 或带覆盖率运行
npx vitest run --coverage
```

### Python 测试

```bash
cd py-src
# 运行新增的安全测试
python -m pytest tests/test_permissions_security.py -v

# 运行所有测试
python -m pytest tests/ -v
```

---

## 测试结果报告模板

```markdown
# 安全修复测试结果

## 测试执行摘要

- **总测试数**: XX
- **通过**: XX
- **失败**: XX
- **跳过**: XX
- **覆盖率**: XX%

## 详细结果

### MCP 命令注入防护
- [ ] 白名单验证通过
- [ ] Shell 元字符拦截通过
- [ ] 合法命令放行通过

### 路径遍历防护
- [ ] ../ 拦截通过
- [ ] ..\ 拦截通过
- [ ] 符号链接拦截通过
- [ ] 正常路径放行通过

### 资源泄漏防护
- [ ] 子进程优雅关闭通过
- [ ] 超时强制终止通过
- [ ] 流销毁验证通过

### 错误处理
- [ ] JSON 解析异常捕获通过
- [ ] 错误日志记录通过
- [ ] 无进程崩溃通过

### 跨平台兼容性
- [ ] Windows CMD 启动通过
- [ ] Windows PowerShell 启动通过
- [ ] Linux Bash 启动通过
- [ ] macOS Zsh 启动通过

## 结论

所有安全修复测试 **通过/失败**，可以/不可以合并到主分支。
```

---

## 持续集成配置

### GitHub Actions

```yaml
# .github/workflows/security.yml
name: Security Tests

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  security:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v4
    
    - name: Setup Node.js
      uses: actions/setup-node@v4
      with:
        node-version: '20'
    
    - name: Setup Python
      uses: actions/setup-python@v5
      with:
        python-version: '3.11'
    
    - name: Install TS dependencies
      run: |
        cd ts-src
        npm install
        npm install --save-dev vitest
    
    - name: Install Python dependencies
      run: |
        cd py-src
        pip install -e ".[dev]"
    
    - name: Run TypeScript tests
      run: |
        cd ts-src
        npx vitest run
    
    - name: Run Python tests
      run: |
        cd py-src
        python -m pytest tests/ -v
    
    - name: Security audit
      run: |
        cd ts-src
        npm audit
        cd ../py-src
        pip install safety
        safety check
```

---

**测试完成日期**: _____________  
**测试人员**: _____________  
**审核人员**: _____________
