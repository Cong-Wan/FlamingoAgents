<!--
Author: wilbur
Version: 1.1
Date: 2026-07-02
Description: Marks this pure Agent Tool Runtime design as superseded by the unified pure library Agent Runtime recipe.
-->

# Pure Agent Tool Runtime 修复方案

> Superseded by `docs/recipe/20260702_pureLibraryAgentRuntime_recipe.md`. This file is retained only as historical design input and must not be used as an execution source.

## 1. 背景与目标

本方案用于修复当前 Flamingo Agents 中的 Critical + High 边界问题，并按方案三做一次结构性收口。目标不是继续在现有 `read/write/edit/bash` 硬编码工具体系上打补丁，而是把 Agent 改成更纯粹的库：Agent 不关心 CLI/HTTP，不主动问用户，只返回结构化状态。

本方案覆盖：

1. 工具定义完全来自 `config/tools.yaml`；
2. 权限规则由配置声明，由代码强制拦截；
3. runtime 只提供通用执行能力，不知道具体有哪些 tools；
4. Agent 遇到 `requireApproval` 只返回 `confirmationRequired`；
5. 模型 API key 解析从 `chatCompletionsAdapter` 移走；
6. 文件 runtime 必须守住 `workDir` 沙箱；
7. tool call 参数必须先校验；
8. pending confirmation 支持多 toolCall 批处理和错误 session 防护；
9. 用 pytest 覆盖核心边界。

本方案不覆盖 CLI/HTTP 入口重构。CLI/HTTP 会在另一份方案里从当前 Agent 中剥离出去。

---

## 2. 总体架构边界

目标边界如下：

```plain
config/tools.yaml
  └── 唯一声明所有 function-call tools、schema、runtime、permissions

config/models.yaml
  └── 声明模型 provider、baseUrl、model、apiKey 来源

flamingoAgents/core/
  └── 纯 Agent：会话状态、模型-工具循环、confirmation 状态机
  └── 不知道 CLI/HTTP，不知道 rm，不知道 regex，不读环境变量

flamingoAgents/models/
  └── 模型配置加载、auth 解析、chat completions 协议适配
  └── adapter 不读 os.getenv，不写 JSONL

flamingoAgents/tools/
  └── 加载 tools.yaml
  └── 生成 toolDefinition
  └── 根据 permissions 做 requireApproval 拦截
  └── 根据 runtime 执行通用能力 shell/file

flamingoAgents/utils/
  └── preview、redaction、jsonl、debug 等辅助能力
```

核心原则：

- 所有可被模型调用的 tools 都来自 `config/tools.yaml`。
- 代码不维护 `read/write/edit/bash` 工具清单。
- 代码只维护通用 runtime 能力，例如 `shell`、`file`。
- `permissions.action=requireApproval` 是运行时强制动作，不是给模型看的建议。
- 模型只看到标准 function-call schema 和权限摘要，不看到内部 `permissions` / `runtime`。
- Agent 只处理 `allowed` / `approvalRequired` / `approved` / `rejected` 状态，不处理具体交互方式。

---

## 3. 旧代码删除与替换

### 3.1 删除或替换的旧边界

删除：

```plain
flamingoAgents/tools/guard.py
```

原因：删除命令正则不应硬编码在代码里，应从 `config/tools.yaml` 的 `permissions` 读取。

删除旧实现：

```plain
flamingoAgents/tools/registry.py
```

不再允许它通过 `createDefaultRegistry()` 硬编码工具 schema。实施时优先删除该文件；如果因为对外 import 兼容必须暂时保留文件名，它也只能薄薄转发到 `toolConfig.py` 生成的配置化 definitions，且不得包含任何默认工具、fallback 工具或硬编码 schema。

删除：

```plain
flamingoAgents/models/registry.py
```

原因：它实际是 model config loader，名字不准确，而且混合了 YAML、环境变量、API key 校验、`os.environ` 写入等职责。

### 3.2 新增模块

新增：

```plain
flamingoAgents/tools/toolConfig.py
flamingoAgents/tools/toolPolicy.py
flamingoAgents/tools/toolRuntime.py
flamingoAgents/tools/toolSchema.py
flamingoAgents/models/modelConfig.py
flamingoAgents/models/modelAuth.py
flamingoAgents/core/ports.py
config/tools.yaml
```

可选新增：

```plain
flamingoAgents/agentFactory.py
```

该 factory 只能做纯库装配，不涉及 CLI/HTTP。

---

## 4. `config/tools.yaml` 设计

`config/tools.yaml` 是所有 function-call tools 的唯一来源。

示例结构：

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

字段说明：

- `name`：模型调用的工具名，也是 runtime 查找定义的 key。
- `description`：给模型看的工具描述。
- `modelPermissionSummary`：可选，加载时拼入最终 description，让模型知道删除类命令会触发确认。
- `parameters`：标准 JSON Schema，用于 function-call 参数描述。
- `runtime`：本地执行方式声明。
- `permissions`：运行时强制规则，不原样发给模型。

`permissions.action` 第一版只支持：

```yaml
action: requireApproval
```

语义固定：

```plain
命中规则 → Agent 返回 confirmationRequired
未命中规则 → 直接执行 runtime
```

不设计配置层的 `allow` / `deny`。用户确认阶段的选择是 `approved=True/False`。

---

## 5. Tool config、policy、schema、runtime

### 5.1 `toolConfig.py`

职责：

```plain
读取 config/tools.yaml
校验 version/tools/name/runtime/permissions
生成 toolDefinition
```

启动时校验：

- `version` 必须是 `1`；
- `tools` 必须是非空数组；
- `tool.name` 必须唯一；
- `parameters` 必须是对象；
- `runtime.type` 第一版只支持 `file`、`shell`；
- file runtime 第一版只支持 `read`、`write`、`edit` operation；
- `permissions[].action` 只能是 `requireApproval`；
- `permissions[].match.type` 第一版只支持 `regex`；
- 每个 regex 必须能编译，否则启动失败。

内部结构示例：

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

### 5.2 `toolPolicy.py`

职责：代码强制放行和拦截。

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
找到 call.toolName 对应 toolDefinition
遍历 definition.permissions
读取 rule.field 对应 argument
用 rule.patterns 匹配
命中 action=requireApproval → requiresApproval=True
否则 requiresApproval=False
```

### 5.3 `toolSchema.py`

职责：把 `toolDefinition` 转成模型 function-call schema。

只输出：

```plain
name
description
parameters
```

不输出：

```plain
runtime
permissions
```

输出结构：

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

### 5.4 `toolRuntime.py`

职责：根据 `toolDefinition.runtime` 执行通用 runtime。

它只认识 runtime 类型，不认识具体 tool 名。

第一版支持：

```plain
runtime.type = file
runtime.type = shell
```

`file` runtime 支持 operation：

```plain
read
write
edit
```

`shell` runtime 支持：

```plain
commandField
timeoutField
cwd = workDir
```

新增工具时，如果现有 runtime 能表达，只改 `config/tools.yaml`。只有新增一种底层执行能力，例如 `database`、`browser`、`http`，才需要扩展 `toolRuntime.py`。

---

## 6. 纯 Agent confirmation 状态机

### 6.1 Agent 不负责用户交互

删除旧构造参数：

```python
confirmDeletion: confirmationHandler | None = None
```

Agent 不再调用 `input()`、CLI callback 或 HTTP callback。遇到 `requireApproval` 后，统一返回：

```plain
runResult(status='confirmationRequired')
```

外部宿主负责问用户，然后调用：

```python
agent.continueConfirmation(sessionId, confirmationId, approved=True)
```

或：

```python
agent.continueConfirmation(sessionId, confirmationId, approved=False)
```

### 6.2 对外 API

保留两个核心 API：

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

### 6.3 pendingConfirm 数据结构

从只保存单个 toolCall 改成保存整批工具调用和当前位置：

```python
@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCalls: list[toolCall]
    currentIndex: int
```

原因：模型一次可能返回多个 tool calls。HTTP/CLI 外壳不在本方案内，但纯 Agent 必须保证协议顺序不丢工具。

### 6.4 批处理流程

Agent 内部使用统一流程：

```plain
processToolBatch(sessionId, toolCalls, startIndex)
```

流程：

```plain
从 currentIndex 开始遍历 toolCalls
找到 toolDefinition
调用 toolPolicy.evaluate()
不需要确认 → toolRuntime 执行 → addToolResult → 继续下一个
需要确认 → 保存 pendingConfirm → 返回 confirmationRequired
```

`continueConfirmation()` 后：

```plain
approved=True
  → 执行当前 pending toolCall
  → addToolResult
  → currentIndex + 1
  → 继续 processToolBatch()

approved=False
  → 不执行 runtime
  → 生成 blocked toolResult
  → currentIndex + 1
  → 继续 processToolBatch()
```

如果剩余工具又触发确认，返回新的 `confirmationRequired`。如果本批处理完，继续模型循环。

### 6.5 pending 期间禁止新消息

如果某 session 有 pending confirmation：

```python
agent.runUserMessage(..., sessionId=sameSession)
```

必须返回 error，提示先调用 `continueConfirmation()`。

否则会产生非法模型消息顺序：

```plain
assistant tool_call
user message
tool result
```

### 6.6 continueConfirmation 先校验再消费

`continueConfirmation()` 必须：

```plain
先 get pending
校验 sessionId
校验通过后再 pop 或推进状态
```

不能先 `pop()`。错误 sessionId 不应清掉真实 pending。

### 6.7 并发锁

因为纯 Agent 可能被任意宿主并发调用，Agent 内部使用 session 级锁：

```plain
sessionId -> RLock
```

同一 session 的 `runUserMessage()` / `continueConfirmation()` 串行执行；不同 session 可以并发。

---

## 7. 模型配置与 API key 边界

### 7.1 删除旧 model registry

删除：

```plain
flamingoAgents/models/registry.py
```

替换为：

```plain
flamingoAgents/models/modelConfig.py
flamingoAgents/models/modelAuth.py
```

### 7.2 `modelConfig.py`

职责：

```plain
读取 config/models.yaml
选择 provider/model
校验 baseUrl、api、model id
解析 apiKey 来源
不写 os.environ
```

支持：

```yaml
apiKey: llama.cpp
```

也支持：

```yaml
apiKey: ${OPENAI_API_KEY}
```

解析后直接返回结果，不写回环境变量。

### 7.3 `modelAuth.py`

职责：把解析出的 key 转成请求授权信息。

结构：

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

### 7.4 `modelConfig` 数据结构

调整为：

```python
@dataclass
class modelConfig:
    provider: str
    model: str
    baseUrl: str
    apiType: str
    supportsToolCalling: bool = True
```

删除：

```python
apiKeyEnv
```

### 7.5 `chatCompletionsAdapter`

构造：

```python
chatCompletionsAdapter(config=modelConfig, auth=modelAuth, debugConsole=debugConsole)
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

### 7.6 模型请求日志

adapter 不再接收：

```python
logger: jsonlLog | None
```

改为返回结构化完成结果：

```python
@dataclass
class modelCompletion:
    message: chatMessage
    requestPayload: dict[str, Any]
    responsePayload: dict[str, Any]
```

Agent 或日志层决定怎么记录 `modelRequest` / `modelResponse` / `modelError`。

---

## 8. Runtime 安全边界与错误处理

### 8.1 tool_call.arguments 校验

模型层解析 tool call 时必须保证：

```plain
function.arguments 是合法 JSON
解析结果必须是 dict
```

否则返回模型响应错误，不进入工具流程。

### 8.2 file runtime 沙箱

文件路径必须限制在 `workDir` 内。

第一版规则：

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

路径解析流程：

```plain
root = workDir.resolve()
resolved = (root / path).resolve()
resolved 必须 relative_to(root)
```

不满足时返回 `toolResult(isError=True)`。

### 8.3 shell runtime

规则：

```plain
command 必须是非空 string
timeout 默认 30
timeout 最小 1
timeout 最大 120
cwd 固定 workDir
```

shell runtime 不判断删除命令。删除确认由 `toolPolicy` 在执行前处理。

### 8.4 runtime 错误返回

runtime 不抛业务异常给 Agent。工具执行问题统一返回：

```python
toolResult(isError=True, content='...', details={...})
```

包括：

```plain
未知 runtime type
未知 file operation
路径越界
参数缺失
命令超时
exitCode != 0
```

Agent 只负责把 toolResult 写回 conversation，再继续模型循环。

---

## 9. 测试与验证方案

### 9.1 测试框架

新增 pytest：

```bash
uv add --dev pytest
```

主验证命令：

```bash
uv run pytest
```

`manualChecks.py` 可保留为临时手动验证脚本，但不再作为主验收。

### 9.2 Tool config 测试

覆盖：

- `config/tools.yaml` 能加载；
- `version` 必须是 `1`；
- `tools` 必须非空；
- `tool.name` 必须唯一；
- `runtime.type` 必须合法；
- `permissions.action` 只能是 `requireApproval`；
- regex 必须能编译；
- 可用 tools 来自 tools.yaml，而不是 `createDefaultRegistry()`。

### 9.3 Permission policy 测试

覆盖：

```plain
bash.command = "rm file" → approvalRequired
bash.command = "grep keyword file" → allowed
bash.command = "find . -delete" → approvalRequired
无 permissions 的 tool → allowed
```

重点是验证 policy 根据配置规则拦截，而不是验证 bash 本身。

### 9.4 Runtime 测试

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

### 9.5 Agent 状态机测试

使用 fake model，不调用真实模型。

覆盖：

1. 普通工具调用：allowed → runtime 执行 → completed；
2. requireApproval：返回 confirmationRequired，且不执行危险命令；
3. approved=True：执行 pending toolCall，并继续模型循环；
4. approved=False：生成 blocked toolResult，并继续模型循环；
5. pending 期间同 session 新消息返回 error；
6. 错 sessionId 调 `continueConfirmation()` 不消费 pending；
7. 多 toolCall 批处理不丢剩余工具。

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

### 9.6 Model config / auth 测试

覆盖：

```plain
apiKey: llama.cpp → Authorization = Bearer llama.cpp
apiKey: ${TEST_API_KEY} → 从环境变量读取
apiKey: ${MISSING_KEY} → config/auth 阶段报错
chatCompletionsAdapter 不 import os
chatCompletionsAdapter 不读取环境变量
请求头使用 modelAuth.authorizationHeader
```

### 9.7 Adapter parse 测试

覆盖：

```plain
tool_call.arguments = '{"path":"a.txt"}' → dict 成功
tool_call.arguments = '[]' → error
tool_call.arguments = '"abc"' → error
tool_call.arguments 非法 JSON → error
```

---

## 10. 实施顺序建议

1. 新增 `config/tools.yaml` 和 tool config loader。
2. 新增 `toolPolicy.py`，用配置驱动 `requireApproval`。
3. 新增 `toolRuntime.py`，迁移 file/shell 通用 runtime，并守住 `workDir`。
4. 删除 `tools/guard.py`，删除硬编码 `createDefaultRegistry()`。
5. 改 Agent 工具循环：policy → confirmationRequired → continueConfirmation → runtime。
6. 改 pendingConfirm 为批处理状态，补 session lock。
7. 新增 `modelConfig.py` / `modelAuth.py`，删除 `models/registry.py`。
8. 改 `chatCompletionsAdapter`：注入 auth，不读环境变量，不接收 jsonlLog。
9. 补 pytest 测试，覆盖 Critical + High 验收场景。
10. 跑 `uv run pytest`，必要时保留并更新 `manualChecks.py`。

---

## 11. 成功标准

实现完成后必须满足：

- 模型可调用 tools 全部来自 `config/tools.yaml`；
- 代码里不再硬编码 `read/write/edit/bash` 工具 schema；
- `permissions.action=requireApproval` 由代码强制拦截；
- Agent 遇到 requireApproval 只返回 `confirmationRequired`；
- Agent 不持有 CLI/HTTP 回调；
- pending confirmation 支持多 toolCall 批处理；
- 错 sessionId 不会消费 pending；
- pending 期间同 session 新消息被拒绝；
- file runtime 无法逃逸 `workDir`；
- 非 dict tool arguments 不会打穿 Agent；
- `chatCompletionsAdapter` 不读取环境变量；
- 配置加载不写 `os.environ`；
- pytest 覆盖上述边界并通过。
