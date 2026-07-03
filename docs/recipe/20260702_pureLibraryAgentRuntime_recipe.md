<!--
Author: wilbur
Version: 1.1
Date: 2026-07-02
Description: 互补合并 pureLibraryAgent 与 pureAgentToolRuntime 两份方案，并修正验证约束：统一采用无框架 manualChecks 手动验证。
-->

# FlamingoAgents 纯库 Agent Runtime 统一改造方案

## 1. 背景与统一目标

当前 `flamingoAgents/` 同时承担了三类职责：

1. **应用入口**：`flamingoAgents/app/cli.py` 和 `flamingoAgents/app/server.py` 提供 CLI / HTTP 两个入口；
2. **Agent 核心**：`flamingoAgents/core/agent.py` 维护会话、模型循环、工具调用和删除确认；
3. **工具与模型运行时**：`tools/registry.py`、`tools/guard.py`、`models/registry.py`、`models/chatCompletions.py` 负责工具 schema、权限判断、模型配置和请求。

这导致两个层面的边界问题：

- 外层边界：包本身还是一个带 CLI/HTTP 入口的应用，不是纯库；
- 内层边界：Agent、tools、model adapter 之间仍然存在硬编码工具、硬编码权限、环境变量副作用、日志耦合和 confirmation 状态机缺陷。

本统一方案将两份旧 recipe 互补合并：

- `20260702_pureLibraryAgent_recipe.md` 负责的 **纯库外壳**：删除内置 CLI/HTTP，提供公开 `createAgent()` 工厂；
- `20260702_pureAgentToolRuntime_recipe.md` 负责的 **纯 Agent Runtime 内核**：工具配置化、权限运行时强制、模型 auth 分离、confirmation 状态机修复、workDir 沙箱和 manualChecks 验收。

统一目标：

> 将 FlamingoAgents 改造成一个没有内置 CLI/HTTP、没有交互副作用、工具和模型配置驱动、Agent 状态机可由任意宿主调用的纯 Python library。

---

## 2. 合并原则

### 2.1 外壳纯库化

`flamingoAgents` 包不再提供内置 CLI / HTTP server，不再拥有输入输出方式。

保留：

```plain
from flamingoAgents import createAgent
```

删除：

```plain
flamingoAgents/app/cli.py
flamingoAgents/app/server.py
pyproject.toml [project.scripts]
```

CLI、HTTP、GUI、notebook、IDE extension 等都应作为外部 host，自行调用 library API。

### 2.2 内核纯 Agent Runtime

Agent 核心只负责：

```plain
会话状态
模型-工具循环
confirmationRequired 状态机
tool result 回灌
```

Agent 不负责：

```plain
input()
HTTP status code
CLI prompt
删除命令正则
工具 schema 硬编码
环境变量读取
模型请求日志文件格式
```

### 2.3 配置是声明，代码是强制

`config/tools.yaml` 是模型可调用 tools 的唯一来源。权限规则也在配置中声明，但必须由代码强制执行：

```plain
permissions.action=requireApproval
  → 命中时 Agent 返回 confirmationRequired
  → 未经 continueConfirmation(..., approved=True) 不执行 runtime
```

### 2.4 不保留同步确认回调

旧方案中 `createAgent(confirmDeletion=...)` 与纯 Agent Runtime 冲突。统一后删除 `confirmDeletion`。

外部 host 若需要同步确认，可以自己包装：

```python
result = agent.runUserMessage(message, sessionId='s1')
if result.status == 'confirmationRequired':
    approved = askUser(result.reason, result.commandPreview)
    result = agent.continueConfirmation(result.sessionId, result.confirmationId, approved)
```

---

## 3. 当前代码事实

调研时当前代码仍处于旧边界：

| 位置 | 当前事实 | 统一方案处理 |
| --- | --- | --- |
| `flamingoAgents/app/` | 仍存在 CLI / HTTP 入口 | 删除整个目录 |
| `pyproject.toml` | 仍暴露 `Flamingo` / `flamingo-agents-server` | 删除 `[project.scripts]` |
| `manualChecks.py` | 仍 import `app.server.makeHttpHandler` | 移除 HTTP check，扩展 manualChecks 作为主验收 |
| `core/agent.py` | 直接 import `guard.py`，持有 `confirmDeletion` | 改为 policy + confirmation 状态机 |
| `pendingConfirm` | 只保存单个 `toolCall` | 改为保存整批 tool calls 和游标 |
| `continueConfirmation()` | 先 `pop()` 再校验 session | 先校验，成功后再消费 |
| `tools/registry.py` | 硬编码 `read/write/edit/bash` schema | 改为从 `config/tools.yaml` 加载 |
| `tools/guard.py` | 删除规则硬编码 | 删除，用 `toolPolicy.py` 读取配置规则 |
| `tools/file.py` | 路径可通过 `../` 或绝对路径逃逸 | file runtime 强制 workDir sandbox |
| `models/registry.py` | inline apiKey 写回 `os.environ` | 删除全局环境副作用 |
| `chatCompletionsAdapter` | import `os`，读环境变量，依赖 `jsonlLog` | 注入 `modelAuth`，返回结构化 completion |

---

## 4. 最终架构边界

目标结构：

```plain
config/tools.yaml
  └── 唯一声明 function-call tools、schema、runtime、permissions

config/models.yaml
  └── 声明 provider、baseUrl、model、apiKey 来源

flamingoAgents/__init__.py
  └── 只 re-export createAgent 和 packageVersion

flamingoAgents/builder.py
  └── 纯库组合根：加载 config、auth、tools，装配 agent

flamingoAgents/core/
  ├── agent.py
  ├── conversation.py
  ├── types.py
  └── ports.py
      └── 会话状态、模型-工具循环、confirmation 状态机、端口协议

flamingoAgents/models/
  ├── modelConfig.py
  ├── modelAuth.py
  └── chatCompletions.py
      └── 模型配置加载、auth 解析、chat completions 协议适配

flamingoAgents/tools/
  ├── toolConfig.py
  ├── toolSchema.py
  ├── toolPolicy.py
  └── toolRuntime.py
      └── 配置加载、schema 转换、权限评估、通用 runtime 执行

flamingoAgents/utils/
  ├── debug.py
  ├── jsonl.py
  ├── preview.py
  └── redaction.py
      └── 小而独立的辅助能力
```

核心依赖方向：

```plain
builder → models/tools/core/utils
core → ports/types/conversation/tools policy/runtime abstractions
models → core types + model auth/config
工具 runtime → core types + utils preview/redaction
utils → 不反向依赖业务模块
```

---

## 5. 公开 API 契约

### 5.1 包根导出

`flamingoAgents/__init__.py` 最终只公开：

```python
from flamingoAgents.builder import createAgent

packageVersion = '0.1.0'

__all__ = ['createAgent', 'packageVersion']
```

不在包根导出 `agent`、`toolCall`、`confirmationHandler` 等内部类型。需要深度自定义的使用者从子模块显式 import。

### 5.2 `createAgent()`

新增：

```plain
flamingoAgents/builder.py
```

建议 API：

```python
def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    logDir: str | Path | None = None,
    modelConfigPath: str | Path | None = None,
    toolsConfigPath: str | Path | None = None,
    providerId: str = '101',
    modelId: str | None = None,
) -> agent:
    ...
```

职责：

```plain
resolve workDir/logDir
创建 debugConsole
加载 model config
解析 model auth
创建 chatCompletionsAdapter
加载 tools.yaml
创建 pure agent
```

禁止：

```plain
input()
HTTP server
CLI 参数解析
confirmDeletion 回调
os.environ 写入
硬编码工具 schema
```

### 5.3 Agent API

保留两个核心调用：

```python
result = agent.runUserMessage(message, sessionId=None)
```

```python
result = agent.continueConfirmation(sessionId, confirmationId, approved)
```

返回状态：

```plain
completed
confirmationRequired
error
```

`confirmationRequired` 表示：Agent 已经保存 pending 状态，等待宿主调用 `continueConfirmation()`。

---

## 6. 删除 CLI / HTTP 入口与 packaging 调整

### 6.1 删除文件

删除：

```plain
flamingoAgents/app/cli.py
flamingoAgents/app/server.py
flamingoAgents/app/__init__.py
flamingoAgents/app/
```

原因：这些是 host 层，不属于纯 library。

### 6.2 修改 `pyproject.toml`

删除：

```toml
[project.scripts]
Flamingo = "flamingoAgents.app.cli:main"
flamingo-agents-server = "flamingoAgents.app.server:main"
```

更新 description：

```toml
description = "Local Flamingo Agents as a pure library"
```

### 6.3 `manualChecks.py`

`manualChecks.py` 是无框架主验证入口，不能依赖已删除 app 层。

移除：

```plain
http.client
ThreadingHTTPServer
threading
from flamingoAgents.app.server import makeHttpHandler
runHttpCheck()
choices 中的 http
all 中的 http 分支
```

主验收改为扩展后的 `manualChecks.py`。

---

## 7. Tool 配置系统

### 7.1 `config/tools.yaml`

新增：

```plain
config/tools.yaml
```

它是所有模型可调用 function-call tools 的唯一来源。

示例：

```yaml
version: 1

tools:
  - name: read
    description: 读取本地文本文件，可通过 offset 和 limit 控制读取的行范围。
    parameters:
      type: object
      properties:
        path:
          type: string
        offset:
          type: integer
          minimum: 1
          default: 1
        limit:
          type: integer
          minimum: 1
          default: 200
      required:
        - path
      additionalProperties: false
    runtime:
      type: file
      operation: read
      pathField: path
      offsetField: offset
      limitField: limit
      root: workDir
    permissions: []

  - name: write
    description: 创建或完整覆盖本地文本文件。
    parameters:
      type: object
      properties:
        path:
          type: string
        content:
          type: string
      required:
        - path
        - content
      additionalProperties: false
    runtime:
      type: file
      operation: write
      pathField: path
      contentField: content
      root: workDir
    permissions: []

  - name: edit
    description: 对已有文本文件进行精确文本替换。每个 oldText 必须唯一匹配。
    parameters:
      type: object
      properties:
        path:
          type: string
        edits:
          type: array
          minItems: 1
          items:
            type: object
            properties:
              oldText:
                type: string
              newText:
                type: string
            required:
              - oldText
              - newText
            additionalProperties: false
      required:
        - path
        - edits
      additionalProperties: false
    runtime:
      type: file
      operation: edit
      pathField: path
      editsField: edits
      root: workDir
    permissions: []

  - name: bash
    description: 在工作目录中执行 bash 命令。curl、python、grep、open 均通过此工具执行。
    modelPermissionSummary: 删除类命令会请求用户确认。
    parameters:
      type: object
      properties:
        command:
          type: string
        timeout:
          type: integer
          minimum: 1
          maximum: 120
          default: 30
      required:
        - command
      additionalProperties: false
    runtime:
      type: shell
      commandField: command
      timeoutField: timeout
      cwd: workDir
    permissions:
      - id: deletionCommand
        field: command
        action: requireApproval
        reason: 删除命令需要用户确认
        match:
          type: regex
          patterns:
            - '(^|[;&|]\\s*)rm\\s+(-[A-Za-z]*\\s+)*[^\\n;&|]+'
            - '(^|[;&|]\\s*)rmdir\\s+[^\\n;&|]+'
            - '(^|[;&|]\\s*)unlink\\s+[^\\n;&|]+'
            - '(^|[;&|]\\s*)find\\s+[^\\n;&|]*\\s-delete(\\s|$)'
            - 'os\\.(remove|unlink|rmdir)\\s*\\('
            - 'shutil\\.rmtree\\s*\\('
            - 'pathlib\\.[A-Za-z0-9_\\.]+\\.(unlink|rmdir)\\s*\\('
```

### 7.2 `toolConfig.py`

新增：

```plain
flamingoAgents/tools/toolConfig.py
```

职责：

```plain
读取 tools.yaml
校验 version/tools/name/parameters/runtime/permissions
编译 regex
生成内部 toolDefinition
```

启动时校验：

- `version` 必须是 `1`；
- `tools` 必须是非空数组；
- `tool.name` 必须唯一；
- `parameters` 必须是 JSON Schema object；
- `runtime.type` 第一版只支持 `file`、`shell`；
- file runtime 第一版只支持 `read`、`write`、`edit`；
- `permissions[].action` 第一版只支持 `requireApproval`；
- `permissions[].match.type` 第一版只支持 `regex`；
- 每个 regex 必须能编译。

建议内部结构：

```python
@dataclass
class permissionRule:
    id: str
    field: str
    action: Literal['requireApproval']
    reason: str
    patterns: list[Pattern[str]]

@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    runtime: dict[str, Any]
    permissions: list[permissionRule]
```

### 7.3 `toolSchema.py`

新增：

```plain
flamingoAgents/tools/toolSchema.py
```

职责：把内部 `toolDefinition` 转成模型 function-call schema。

输出：

```python
{
    'type': 'function',
    'function': {
        'name': definition.name,
        'description': definition.description,
        'parameters': definition.parameters,
    },
}
```

不输出：

```plain
runtime
permissions
```

`modelPermissionSummary` 可以在加载时拼入 description，让模型知道高风险操作会触发确认，但它不是权限本身。

### 7.4 `toolPolicy.py`

新增：

```plain
flamingoAgents/tools/toolPolicy.py
```

职责：根据配置强制拦截。

接口：

```python
def evaluateToolCall(definition: toolDefinition, call: toolCall) -> policyDecision:
    ...
```

返回：

```python
@dataclass
class policyDecision:
    requiresApproval: bool
    reason: str = ''
    permissionId: str = ''
```

流程：

```plain
读取 call.arguments[rule.field]
用 rule.patterns 匹配
命中 action=requireApproval → requiresApproval=True
否则 requiresApproval=False
```

### 7.5 `toolRuntime.py`

新增：

```plain
flamingoAgents/tools/toolRuntime.py
```

职责：根据 `definition.runtime` 执行通用 runtime。

第一版支持：

```plain
runtime.type = file
runtime.type = shell
```

它只认识 runtime 类型，不认识具体 tool 名。新增工具时，如果现有 runtime 能表达，只改 `config/tools.yaml`。

---

## 8. Tool 参数校验与 runtime 安全

### 8.1 tool call arguments

模型层解析 tool calls 时必须保证：

```plain
function.arguments 是合法 JSON
解析结果必须是 dict
```

否则返回模型响应错误，不进入 policy/runtime。

### 8.2 schema 校验

工具执行前应按 `definition.parameters` 校验 `call.arguments`。

不新增额外依赖。第一版用手写参数校验覆盖当前 JSON Schema 形状：

```plain
required
additionalProperties
type
minimum
maximum
array minItems
object items required
```

校验失败时返回：

```python
toolResult(isError=True, content='工具参数不符合 schema：...')
```

不得进入实际 file/shell runtime。

### 8.3 file runtime workDir 沙箱

文件路径必须限制在 `workDir` 内。

规则：

```plain
path 必须是 workDir 内的相对路径
禁止绝对路径
禁止 ~
禁止 ../ 逃逸
```

允许：

```plain
sample.txt
docs/a.md
./src/main.py
```

拒绝：

```plain
/Users/wilbur/.ssh/id_rsa
../outside.txt
~/secret.txt
```

路径解析：

```plain
root = workDir.resolve()
resolved = (root / path).resolve()
resolved 必须 relative_to(root)
```

不满足时返回 `toolResult(isError=True)`。

### 8.4 shell runtime

规则：

```plain
command 必须是非空 string
timeout 默认 30
timeout 最小 1
timeout 最大 120
cwd 固定 workDir
```

shell runtime 不判断删除命令。删除确认只由 `toolPolicy` 在执行前处理。

### 8.5 runtime 错误返回

runtime 不向 Agent 抛业务异常。工具执行问题统一返回：

```python
toolResult(isError=True, content='...', details={...})
```

包括：

```plain
未知 runtime type
未知 file operation
schema 校验失败
路径越界
参数缺失
命令超时
exitCode != 0
```

---

## 9. Agent confirmation 状态机

### 9.1 删除 `confirmDeletion`

删除：

```python
confirmDeletion: confirmationHandler | None = None
```

Agent 不再调用：

```plain
input()
CLI callback
HTTP callback
```

遇到需要确认的工具调用，统一返回：

```plain
runResult(status='confirmationRequired')
```

### 9.2 `pendingConfirm` 批处理结构

从单个 tool call：

```python
@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCall: toolCall
```

改成整批工具调用和游标：

```python
@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCalls: list[toolCall]
    currentIndex: int
```

原因：模型一次 assistant message 可能返回多个 tool calls。每个 tool call 都必须有对应 tool result。

### 9.3 批处理流程

Agent 内部新增统一流程：

```plain
processToolBatch(sessionId, toolCalls, startIndex)
```

流程：

```plain
从 currentIndex 开始遍历 toolCalls
找到 toolDefinition
校验 arguments
执行 toolPolicy.evaluate()
不需要确认 → toolRuntime 执行 → addToolResult → 继续下一个
需要确认 → 保存 pendingConfirm → 返回 confirmationRequired
```

### 9.4 `continueConfirmation()` 流程

`approved=True`：

```plain
执行当前 pending toolCall
addToolResult
currentIndex + 1
继续 processToolBatch()
```

`approved=False`：

```plain
不执行 runtime
生成 blocked toolResult
currentIndex + 1
继续 processToolBatch()
```

如果剩余工具又触发确认，返回新的 `confirmationRequired`。如果本批处理完，继续模型循环。

### 9.5 pending 期间禁止新消息

如果某 session 有 pending confirmation：

```python
agent.runUserMessage(..., sessionId=sameSession)
```

必须返回 error，提示先调用 `continueConfirmation()`。

原因：不能形成非法消息序列：

```plain
assistant tool_call
user message
tool result
```

### 9.6 先校验再消费 pending

`continueConfirmation()` 必须：

```plain
先 get pending
校验 confirmationId 存在
校验 sessionId 匹配
校验通过后再 pop 或推进状态
```

不能先 `pop()`。

### 9.7 session 级锁

Agent 内部使用 session 级锁：

```plain
sessionId -> RLock
```

同一 session 的 `runUserMessage()` / `continueConfirmation()` 串行执行；不同 session 可以并发。

即使库本身不再内置 HTTP，任意外部 host 仍可能并发调用同一个 Agent，因此锁仍属于 core 边界。

---

## 10. Model config / auth / adapter 边界

### 10.1 删除旧 model registry

删除：

```plain
flamingoAgents/models/registry.py
```

替换为：

```plain
flamingoAgents/models/modelConfig.py
flamingoAgents/models/modelAuth.py
```

### 10.2 `modelConfig.py`

职责：

```plain
读取 config/models.yaml
选择 provider/model
校验 baseUrl、api、model id
解析 apiKey 来源为原始 key 字符串
不写 os.environ
```

建议结构：

```python
@dataclass
class modelConfig:
    provider: str
    model: str
    baseUrl: str
    apiType: str
    supportsToolCalling: bool = True

@dataclass
class resolvedModelConfig:
    config: modelConfig
    apiKey: str
```

支持：

```yaml
apiKey: llama.cpp
```

也支持：

```yaml
apiKey: ${OPENAI_API_KEY}
```

`${ENV}` 只读取环境变量，不写回环境变量。

### 10.3 `modelAuth.py`

职责：把解析出的 key 转成请求授权信息。

```python
@dataclass
class modelAuth:
    authorizationHeader: str
```

示例：

```plain
apiKey = llama.cpp
authorizationHeader = Bearer llama.cpp
```

### 10.4 `chatCompletionsAdapter`

构造：

```python
chatCompletionsAdapter(
    config=modelConfig,
    auth=modelAuth,
    debugConsole=debugConsole,
)
```

职责：

```plain
chatMessage -> OpenAI messages
toolDefinition/schema -> OpenAI tools
发送 HTTP POST /chat/completions
OpenAI response -> chatMessage
```

禁止：

```plain
os.getenv()
jsonlLog 参数
配置解析
apiKey 校验
```

### 10.5 模型请求日志

adapter 不再接收：

```python
logger: jsonlLog | None
```

改为返回结构化结果：

```python
@dataclass
class modelCompletion:
    message: chatMessage
    requestPayload: dict[str, Any]
    responsePayload: dict[str, Any]
```

Agent 或日志层决定如何记录：

```plain
modelRequest
modelResponse
modelError
```

HTTP error 可通过专门异常携带：

```python
class modelRequestError(Exception):
    requestPayload: dict[str, Any]
    statusCode: int | None
    responseBody: str
```

Agent 捕获后写入 session logger，再返回 `runResult(status='error')`。

---

## 11. Logging 与 utils 拆分

当前 `utils/jsonl.py` 同时包含：

```plain
jsonlLog
makePreview
redactText
```

统一方案建议拆分：

```plain
utils/preview.py      → makePreview()
utils/redaction.py    → redactText()
utils/jsonl.py        → jsonlLog，只负责 JSONL 写入
```

工具 runtime 只依赖 `preview.py` / `redaction.py`，不依赖 JSONL writer。

conversation 可以继续维护 session logger，但模型 adapter 不直接写 JSONL。

---

## 12. 旧模块删除与替换

删除：

```plain
flamingoAgents/app/
flamingoAgents/tools/guard.py
flamingoAgents/models/registry.py
```

替换旧实现：

```plain
flamingoAgents/tools/registry.py
flamingoAgents/tools/router.py
```

推荐直接删除旧 `registry.py` / `router.py`，由 `toolConfig.py` / `toolRuntime.py` / `toolPolicy.py` 承担职责。

如果为了短期 import 兼容保留文件名，它们只能是 thin shim：

```plain
不得包含默认工具 schema
不得包含删除正则
不得 fallback 到硬编码 read/write/edit/bash
```

新增：

```plain
flamingoAgents/builder.py
flamingoAgents/core/ports.py
flamingoAgents/tools/toolConfig.py
flamingoAgents/tools/toolSchema.py
flamingoAgents/tools/toolPolicy.py
flamingoAgents/tools/toolRuntime.py
flamingoAgents/models/modelConfig.py
flamingoAgents/models/modelAuth.py
flamingoAgents/utils/preview.py
flamingoAgents/utils/redaction.py
config/tools.yaml
```

---

## 13. 无框架验证方案

### 13.1 主验证方式

严格不引入任何测试框架。不得新增任何测试依赖，也不得使用 Python 内置或第三方测试框架。

主验证入口是扩展后的：

```bash
uv run python manualChecks.py all
```

`manualChecks.py` 使用普通函数、`expect()` 和异常来做检查。每个检查项通过时打印 `PASS <name>`，失败时抛出 `RuntimeError` 并让进程非零退出。

### 13.2 Tool config 检查

新增 manual check，例如：

```plain
runToolConfigCheck()
```

覆盖：

- `config/tools.yaml` 能加载；
- `version` 必须是 `1`；
- `tools` 必须非空；
- `tool.name` 必须唯一；
- `runtime.type` 必须合法；
- `permissions.action` 只能是 `requireApproval`；
- regex 必须能编译；
- 可用 tools 来自 `tools.yaml`，不是 `createDefaultRegistry()`。

### 13.3 Permission policy 检查

新增 manual check，例如：

```plain
runPermissionPolicyCheck()
```

覆盖：

```plain
bash.command = "rm file" → approvalRequired
bash.command = "grep keyword file" → allowed
bash.command = "find . -delete" → approvalRequired
无 permissions 的 tool → allowed
```

### 13.4 Runtime 检查

新增 manual check，例如：

```plain
runToolRuntimeCheck()
```

File runtime：

```plain
read workDir 内文件 → 成功
write workDir 内文件 → 成功
edit workDir 内文件 → 成功
../outside.txt → blocked error
/absolute/path → blocked error
~/secret.txt → blocked error
```

Shell runtime：

```plain
printf hello → 成功
exitCode != 0 → toolResult.isError=True
timeout → toolResult.isError=True
timeout 上限限制到 120
```

### 13.5 Agent 状态机检查

新增 manual check，例如：

```plain
runAgentStateCheck()
```

使用 fake model，不调用真实模型。

覆盖：

1. 普通工具调用：allowed → runtime 执行 → completed；
2. requireApproval：返回 confirmationRequired，且不执行危险命令；
3. approved=True：执行 pending toolCall，并继续模型循环；
4. approved=False：生成 blocked toolResult，并继续模型循环；
5. pending 期间同 session 新消息返回 error；
6. 错 sessionId 调 `continueConfirmation()` 不消费 pending；
7. 多 toolCall 批处理不丢剩余工具；
8. 同一 session 并发调用不会破坏消息顺序。

多 toolCall 场景：

```plain
assistant 返回：
  call_1 allowed
  call_2 requireApproval
  call_3 allowed

首次 run：
  call_1 执行
  call_2 pending
  call_3 暂不丢失

approve 后：
  call_2 执行
  call_3 继续执行
  再回到模型循环
```

### 13.6 Model config / auth 检查

新增 manual check，例如：

```plain
runModelAuthCheck()
```

覆盖：

```plain
apiKey: llama.cpp → Authorization = Bearer llama.cpp
apiKey: ${TEST_API_KEY} → 从环境变量读取
apiKey: ${MISSING_KEY} → config/auth 阶段报错
config loader 不写 os.environ
chatCompletionsAdapter 不 import os
chatCompletionsAdapter 不读取环境变量
请求头使用 modelAuth.authorizationHeader
```

### 13.7 Adapter parse 检查

扩展现有：

```plain
runAdapterParseCheck()
```

覆盖：

```plain
tool_call.arguments = '{"path":"a.txt"}' → dict 成功
tool_call.arguments = '[]' → error
tool_call.arguments = '"abc"' → error
tool_call.arguments 非法 JSON → error
```

### 13.8 纯库 API 检查

新增 manual check，例如：

```plain
runPureLibraryApiCheck()
```

覆盖：

```plain
from flamingoAgents import createAgent → 成功
flamingoAgents/app/ 不存在
pyproject.toml 不包含 [project.scripts]
manualChecks.py 不 import flamingoAgents.app
```

---

## 14. 推荐实施顺序

### 阶段 1：manualChecks 验收清单先行

1. 扩展 `manualChecks.py` 的检查项名称和 `choices`；
2. 先把当前已知 Critical/High 边界写成普通 Python 检查函数；
3. 每个检查函数使用现有 `expect()` 风格，不引入任何测试框架。

验证：

```bash
uv run python manualChecks.py all
```

此阶段允许新增检查项先失败，用于锁定目标行为；后续阶段逐步让它们全部 PASS。

### 阶段 2：纯库外壳收口

1. 新增 `builder.py/createAgent()`；
2. 修改 `__init__.py` re-export；
3. 删除 `flamingoAgents/app/`；
4. 删除 `pyproject.toml [project.scripts]`；
5. 移除 `manualChecks.py` 对 app 层的依赖。

验证：

```bash
uv run python -c "from flamingoAgents import createAgent; print('import ok')"
uv run python manualChecks.py all
```

### 阶段 3：工具系统配置化

1. 新增 `config/tools.yaml`；
2. 新增 `toolConfig.py`；
3. 新增 `toolSchema.py`；
4. 新增 `toolPolicy.py`；
5. 新增 `toolRuntime.py`；
6. 删除 `tools/guard.py`；
7. 移除硬编码 `createDefaultRegistry()`。

验证：`runToolConfigCheck()`、`runPermissionPolicyCheck()`、`runToolRuntimeCheck()` 全部 PASS。

### 阶段 4：Agent 状态机重构

1. 删除 `confirmDeletion`；
2. 改为 `policy → confirmationRequired → continueConfirmation → runtime`；
3. `pendingConfirm` 改成批处理；
4. pending 期间拒绝新消息；
5. `continueConfirmation()` 先校验再消费；
6. 加 session 级锁。

验证：`runAgentStateCheck()` 全部 PASS。

### 阶段 5：模型配置与 auth 分离

1. 删除 `models/registry.py`；
2. 新增 `modelConfig.py`；
3. 新增 `modelAuth.py`；
4. 修改 `chatCompletionsAdapter` 注入 auth；
5. adapter 不再 import `os`；
6. config loader 不写 `os.environ`。

验证：`runModelAuthCheck()`、`runAdapterParseCheck()` 全部 PASS。

### 阶段 6：日志与 utils 解耦

1. adapter 返回 `modelCompletion`；
2. Agent 或日志层记录 request/response/error；
3. 拆 `preview.py` / `redaction.py`；
4. `jsonl.py` 只保留 JSONL writer。

验证：logger、adapter、agent 相关 manual checks 全部 PASS。

### 阶段 7：旧模块清理与全量验收

1. 删除无用 shim；
2. 删除未使用 import；
3. 更新 manual checks；
4. 全量运行 manual checks。

验证：

```bash
uv run python manualChecks.py all
uv run python -c "from flamingoAgents import createAgent; print('import ok')"
```

---

## 15. 成功标准

实现完成后必须满足：

- `flamingoAgents/app/` 不存在；
- `pyproject.toml` 不再暴露 CLI/HTTP scripts；
- 包根只公开 `createAgent` 和 `packageVersion`；
- `createAgent()` 使用最终 config/tool/model/auth runtime 装配；
- Agent 不接收 `confirmDeletion`；
- Agent 不主动问用户；
- Agent 遇到 `requireApproval` 只返回 `confirmationRequired`；
- pending confirmation 支持多 toolCall 批处理；
- 错 sessionId 不会消费 pending；
- pending 期间同 session 新消息被拒绝；
- 同一 session 并发调用不会破坏消息顺序；
- 模型可调用 tools 全部来自 `config/tools.yaml`；
- 代码里不再硬编码 `read/write/edit/bash` 工具 schema；
- `permissions.action=requireApproval` 由代码强制拦截；
- file runtime 无法逃逸 `workDir`；
- 非 dict tool arguments 不会打穿 Agent；
- `chatCompletionsAdapter` 不读取环境变量；
- 配置加载不写 `os.environ`；
- adapter 不直接依赖 `jsonlLog`；
- `uv run python manualChecks.py all` 覆盖上述边界并通过。

---

## 16. 不在本统一方案范围

不做：

- 提供新的内置 CLI；
- 提供新的内置 HTTP server；
- 设计 GUI / notebook / web host；
- 支持除 `file` / `shell` 之外的新 runtime 类型；
- 支持 `requireApproval` 之外的权限动作；
- 完整重写 `docs/flamingoAgentsFlow.md`。

说明：`docs/flamingoAgentsFlow.md` 删除入口后会过时，建议后续单独更新，避免本方案过宽。

---

## 17. 旧 recipe 关系说明

本方案互补合并并替代：

```plain
docs/recipe/20260702_pureLibraryAgent_recipe.md
docs/recipe/20260702_pureAgentToolRuntime_recipe.md
```

继承关系：

| 旧文档 | 本方案吸收内容 | 本方案调整内容 |
| --- | --- | --- |
| `pureLibraryAgent` | 删除 app、删除 scripts、新增 `createAgent()`、包根 re-export | 删除 `confirmDeletion`，builder 改用最终 runtime/model/auth 装配，验证改为无框架 manualChecks |
| `pureAgentToolRuntime` | config tools、policy/runtime/schema、model auth、confirmation 状态机、workDir 沙箱 | 把 CLI/HTTP 删除纳入同一方案，统一 factory 名称为 `builder.py/createAgent()`，并去掉测试框架要求 |

后续执行以本文件为准。
