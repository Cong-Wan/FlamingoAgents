<!--
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Reviews Flamingo Agents module boundaries, responsibility leakage, cross-layer coupling, and related correctness risks.
-->

# 代码审核报告 — 模块边界与职责归属深度排查

## 总览

- 审核范围：`flamingoAgents/` 全量 Python 模块、`config/models.yaml`、`pyproject.toml`、`manualChecks.py`。
- 验证命令：`uv run python manualChecks.py all`，当前全部通过。
- 发现问题：🔴 1 个 / 🟠 7 个 / 🟡 9 个 / 🔵 2 个。
- 整体评价：当前代码功能闭环能跑通，但边界不干净。主要问题集中在：模型配置与模型协议适配互相越界、工具注册泄漏 OpenAI 协议形状、Agent 核心直接掺入工具安全策略和 JSONL 日志、HTTP pending confirmation 状态管理不完整、文件工具没有守住 `workDir` 边界。

---

## 建议的模块边界

| 模块 | 应该负责 | 不应该负责 |
| --- | --- | --- |
| `app/*` | CLI/HTTP 输入输出、参数解析、HTTP 状态码、组合装配 | 模型协议细节、工具执行细节、会话内部状态 |
| `core/*` | 会话状态、模型-工具循环编排、确认流程的领域状态 | 读环境变量、拼 OpenAI tools schema、写 JSONL 文件、直接知道 bash 删除正则 |
| `models/*` | 模型配置解析、模型协议适配、请求/响应转换 | 从全局环境临时取密钥、写审计日志、知道会话 logger |
| `tools/*` | 工具定义、参数校验、工具执行、工具安全边界 | OpenAI 专属 schema、HTTP/CLI 确认方式、跨 session pending 状态 |
| `utils/*` | 小而纯的通用能力，如 preview、redaction、JSONL writer | 成为所有模块都依赖的“杂货铺” |
| `manualChecks.py` | 验证脚本或测试入口 | 替代正式测试分层、长期承载全部测试场景 |

---

## 问题清单

### 🔴 Critical — 文件工具没有守住 `workDir` 边界，读写可逃逸到项目外

**位置**：`flamingoAgents/tools/file.py:18-22`、`executeRead()` / `executeWrite()` / `executeEdit()`

**问题**：`normalizePath()` 只是把相对路径拼到 `workDir`，但没有 `resolve()` 后校验路径是否仍在 `workDir` 内。模型只要传 `../outside.txt` 或绝对路径，就能读写 `workDir` 外的文件。`toolContext.workDir` 这个边界形同虚设。

**为什么边界不清**：文件工具既然接收 `workDir`，它就必须负责执行层沙箱边界；不能把“模型别乱传路径”当成上层责任。

**修复方案**：在文件工具层强制归一化并拒绝越界路径。示例：

```python
def normalizePath(pathValue: str, workDir: Path) -> Path:
    rootPath = workDir.resolve()
    path = Path(pathValue).expanduser()
    if path.is_absolute():
        resolvedPath = path.resolve()
    else:
        resolvedPath = (rootPath / path).resolve()
    try:
        resolvedPath.relative_to(rootPath)
    except ValueError as error:
        raise ValueError(f'路径超出工作目录：{pathValue}') from error
    return resolvedPath
```

并在 `executeRead/write/edit` 捕获这个错误，返回 `toolResult(isError=True)`。

---

### 🟠 High — `chatCompletionsAdapter` 读取 API key，职责确实越界

**位置**：`flamingoAgents/models/chatCompletions.py:25-27`

**问题**：

```python
apiKey = os.getenv(self.config.apiKeyEnv, '').strip()
if not apiKey:
    raise RuntimeError(f'环境变量缺失：{self.config.apiKeyEnv}')
```

这个类的职责应该是“把内部消息转换成 OpenAI Chat Completions 请求并解析响应”。它不应该自己知道 API key 来自哪个环境变量，更不应该做配置完整性校验。配置加载器已经在 `models/registry.py:41`、`models/registry.py:124` 做过一次环境变量校验，适配器这里又校验一遍，职责重复。

**为什么边界不清**：

- `modelConfig` 暴露的是 `apiKeyEnv`，不是可直接用于请求的 credential。
- 适配器为了发请求被迫去碰全局环境变量。
- 以后如果 credential 来自文件、Keychain、HTTP proxy、token provider，协议适配器都要改。

**修复方案**：配置层或装配层一次性解析 credential，适配器只接收已解析的授权信息。

```python
@dataclass
class modelConfig:
    provider: str
    model: str
    baseUrl: str
    apiKey: str
    apiType: str
    supportsToolCalling: bool = True
```

或更干净：

```python
@dataclass
class modelAuth:
    authorizationHeader: str

class chatCompletionsAdapter:
    def __init__(self, config: modelConfig, auth: modelAuth, debugConsole=None):
        self.config = config
        self.auth = auth
```

然后请求头只用：

```python
headers={'Authorization': self.auth.authorizationHeader, 'Content-Type': 'application/json'}
```

---

### 🟠 High — YAML 内联 `apiKey` 被写回 `os.environ`，配置加载器污染全局状态

**位置**：`flamingoAgents/models/registry.py:111-124`

**问题**：当 `config/models.yaml` 的 `apiKey` 不是 `$ENV` 引用时，代码会生成环境变量名并执行：

```python
os.environ[apiKeyEnv] = apiKey
```

这属于全局副作用。加载配置不应该修改进程全局环境，尤其 API key 属于凭据，写入 `os.environ` 会让其他库、后续代码、子进程更容易读到它。

**为什么边界不清**：这是为了迁就 `chatCompletionsAdapter` 只会读 `apiKeyEnv` 的设计，导致配置层反过来伪造环境变量。配置层、凭据层、协议适配层互相绑死。

**修复方案**：删除写环境变量行为。`loadModelConfigFromYaml()` 直接返回 resolved credential，或返回 `credentialSource` + 装配层解析。

```python
@dataclass
class resolvedModelConfig:
    provider: str
    model: str
    baseUrl: str
    apiKey: str
    apiType: str
```

如果必须支持 `$ENV`：

```python
def resolveApiKey(rawApiKey: str) -> str:
    if rawApiKey.startswith('${') and rawApiKey.endswith('}'):
        return readRequiredEnv(rawApiKey[2:-1].strip())
    if rawApiKey.startswith('$'):
        return readRequiredEnv(rawApiKey[1:].strip())
    return rawApiKey.strip()
```

---

### 🟠 High — Guard 在参数校验前执行，模型返回非对象参数会让 Agent 崩掉

**位置**：`flamingoAgents/core/agent.py:110`、`flamingoAgents/tools/guard.py:44`、`flamingoAgents/models/chatCompletions.py:122-126`

**问题**：`parseAssistantPayload()` 用 `json.loads()` 解析 `tool_call.arguments`，但没有要求结果必须是 `dict`。如果模型返回：

```json
[]
```

`toolCall.arguments` 会变成 list。随后 `agent.continueModelLoop()` 在进入 router 之前调用：

```python
guard = checkToolCall(call)
```

而 `checkToolCall()` 里直接：

```python
command = str(call.arguments.get('command', ''))
```

list 没有 `.get()`，这里会抛 `AttributeError`。这个异常不在模型调用 try/except 内，也不在 router 的工具执行 try/except 内，会直接打穿 `runUserMessage()`。

**为什么边界不清**：参数结构校验应该先发生，并且应该由模型解析层或工具路由层统一负责。安全策略 `guard` 不应该接收未校验的任意 JSON。

**修复方案**：两层都可以补，但至少一层必须补。

1. 模型解析层保证 `arguments` 是对象：

```python
arguments = json.loads(argumentsText)
if not isinstance(arguments, dict):
    raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是 JSON 对象。')
```

2. 或 `checkToolCall()` 防御式处理：

```python
def checkToolCall(call: toolCall) -> guardDecision:
    if call.toolName != 'bash':
        return guardDecision(allowed=True)
    if not isinstance(call.arguments, dict):
        return guardDecision(allowed=False, reason='bash.arguments 必须是对象')
```

更推荐：router 先校验参数，再调用 policy。

---

### 🟠 High — HTTP 删除确认只保存单个 `toolCall`，多工具调用协议会断

**位置**：`flamingoAgents/core/types.py:79-83`、`flamingoAgents/core/agent.py:112-127`、`flamingoAgents/core/agent.py:64-79`

**问题**：OpenAI tool calling 要求 assistant 一次返回的每个 `tool_call` 都要有对应 tool result。当前 pending confirmation 只保存一个危险调用：

```python
pendingConfirm(..., toolCall=call)
```

如果 assistant 一次返回多个工具调用：

1. 第 1 个工具已执行；
2. 第 2 个是删除命令，HTTP 返回 `confirmationRequired`；
3. 第 3 个工具调用还没执行，也没有保存；
4. `/confirm` 后只补第 2 个结果，然后直接进入下一轮模型调用。

结果是第 3 个 tool call 永远没有 tool result，消息序列违反协议。

**为什么边界不清**：确认状态不是“某个 toolCall”的状态，而是“当前 assistant tool batch 的执行进度”。`pendingConfirm` 的模型太小，没表达真实流程边界。

**修复方案**：pending 状态保存整个批次和游标，例如：

```python
@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCalls: list[toolCall]
    currentIndex: int
```

`continueConfirmation()` 执行当前危险调用后，应该继续处理同一批剩余工具调用，而不是直接进入下一轮模型调用。也可以更简单：遇到任何需要确认的工具调用时，先不执行本批任何工具，确认后按完整批次顺序执行。

---

### 🟠 High — 有 pending confirmation 时仍允许新用户消息进入，会破坏会话协议

**位置**：`flamingoAgents/core/agent.py:54-63`、`flamingoAgents/core/agent.py:112-127`

**问题**：HTTP 模式下 `/chat` 返回 `confirmationRequired` 后，客户端完全可以对同一个 `sessionId` 再发一条新 `/chat`。`runUserMessage()` 不检查当前 session 是否有 pending confirmation，会直接追加新的 user message。

这会出现 assistant 已发起 tool call、但还没有对应 tool result，中间插入 user message 的非法序列。模型 API 很可能拒绝，或者上下文语义混乱。

**为什么边界不清**：Agent 核心拥有 pending 状态，就必须拥有“会话是否可继续接收用户消息”的状态机规则。不能把这个约束留给 HTTP 客户端自觉遵守。

**修复方案**：`runUserMessage()` 开头按 session 检查 pending：

```python
if sessionId and self.hasPendingConfirmation(sessionId):
    return runResult(
        sessionId=sessionId,
        status='error',
        message='当前会话有待确认工具调用，请先调用 /confirm。',
    )
```

更好是给 `runResult` 增加 `errorCode='pendingConfirmationExists'`，让 HTTP 层返回 409。

---

### 🟠 High — `continueConfirmation()` 先 pop 再校验 session，错误请求能清掉别人的确认

**位置**：`flamingoAgents/core/agent.py:65-67`

**问题**：当前逻辑：

```python
pending = self.pendingConfirms.pop(confirmationId, None)
if pending is None or pending.sessionId != sessionId:
    return runResult(...)
```

如果调用方拿着真实 `confirmationId` 但传错 `sessionId`，这条 pending 已经被 pop 掉了。真实 session 后续无法再确认，相当于被错误请求或恶意请求取消。

**为什么边界不清**：pending store 的生命周期管理应该在“确认身份匹配后”才消费状态；查找与删除是两个动作，不能合并。

**修复方案**：

```python
pending = self.pendingConfirms.get(confirmationId)
if pending is None or pending.sessionId != sessionId:
    return runResult(sessionId=sessionId, status='error', message='确认请求不存在或 sessionId 不匹配。')
self.pendingConfirms.pop(confirmationId, None)
```

并考虑按 `sessionId` 分桶：`dict[sessionId, pendingConfirm]`，减少跨 session 猜 ID 的表面积。

---

### 🟠 High — `ThreadingHTTPServer` 共享 Agent 状态，但核心状态无锁

**位置**：`flamingoAgents/app/server.py:139`、`flamingoAgents/core/agent.py:50-51`、`flamingoAgents/utils/jsonl.py:68-70`

**问题**：HTTP 入口使用 `ThreadingHTTPServer`，多个请求会并发访问同一个 `agent` 实例。`agent.conversations`、`agent.pendingConfirms`、每个 conversation 的 `messages` 都是普通 dict/list，没有锁。JSONL 日志也直接 append 文件，没有 session 级或文件级同步。

**后果**：同一 session 并发 `/chat` 或 `/confirm` 时，可能出现消息乱序、pending 被重复消费、日志交错、模型上下文不一致。

**为什么边界不清**：HTTP 层引入了并发模型，core 层却仍按单线程 CLI 心智写。既然 `app/server.py` 选择线程服务器，就必须在 core 或 server adapter 明确并发边界。

**修复方案**：最小改法：Agent 内部加 session lock。

```python
self.lock = threading.RLock()

def runUserMessage(...):
    with self.lock:
        ...
```

更好：按 sessionId 细粒度锁，避免不同 session 互相阻塞。JSONL writer 也应由同一 session lock 包住，或者 logger 自己持有 lock。

---

### 🟡 Medium — 模型适配器直接依赖 `jsonlLog`，协议层和审计日志层耦合

**位置**：`flamingoAgents/models/chatCompletions.py:16`、`flamingoAgents/models/chatCompletions.py:25`、`flamingoAgents/models/chatCompletions.py:52-78`、`flamingoAgents/core/agent.py:90-93`

**问题**：`complete()` 签名包含具体日志类型：

```python
def complete(..., logger: jsonlLog | None = None) -> chatMessage:
```

并在模型层写 `modelRequest` / `modelResponse` / `modelResponseError` 事件。这让模型协议适配器知道“当前会话用 JSONL 记录”。

**为什么边界不清**：模型适配器应该只处理协议请求/响应；日志属于横切关注点。现在 core 为了让模型层写日志，把 `conversation.logger` 传进去，导致 core、conversation、models、utils/jsonl 四层串在一起。

**修复方案**：改成通用 observer/callback，或由 core 包裹模型调用并记录。

```python
class modelObserver(Protocol):
    def onRequest(self, payload: dict[str, Any]) -> None: ...
    def onResponse(self, payload: dict[str, Any]) -> None: ...
```

或者 `complete()` 返回：

```python
@dataclass
class modelCompletion:
    message: chatMessage
    requestPayload: dict[str, Any]
    responsePayload: dict[str, Any]
```

由 core 决定怎么落日志。

---

### 🟡 Medium — 工具注册表输出 OpenAI tools schema，工具层泄漏模型协议

**位置**：`flamingoAgents/tools/registry.py:28-40`、`flamingoAgents/core/agent.py:90-92`

**问题**：`tools/registry.py` 的 `listModelTools()` 直接生成 OpenAI-compatible tools schema：

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

工具层因此知道 OpenAI 的 `type=function/function={...}` 包装格式。

**为什么边界不清**：工具注册表应该是协议中立的，只描述本地工具。OpenAI schema 包装应该由 `chatCompletionsAdapter` 或专门的 `toolSchemaAdapter` 做。

**修复方案**：

```python
# tools/registry.py
catalog.listDefinitions() -> list[toolSpec]

# models/chatCompletions.py
convertTools(toolSpecs: list[toolSpec]) -> list[dict[str, Any]]
```

然后 core 调用：

```python
assistantMessage = self.modelAdapter.complete(conversation.messages, self.registry.listDefinitions())
```

模型适配器自己把中立工具定义转成目标协议格式。

---

### 🟡 Medium — Agent 和 Router 重复执行删除策略，`executeTool()` 返回类型不可信

**位置**：`flamingoAgents/core/agent.py:110-136`、`flamingoAgents/tools/router.py:47-49`

**问题**：Agent 先 `checkToolCall()`，Router 又 `checkToolCall()`。并且 `router.executeTool()` 声明返回 `toolResult`，但遇到未批准删除时会 `raise confirmationNeeded`。

**为什么边界不清**：

- 删除策略到底属于 Agent 还是 Router 不清楚。
- `executeTool()` 既像“执行工具”，又像“执行前安全闸”。
- 调用方从类型签名看不出它可能抛出业务级确认异常。

**修复方案**：选一个边界：

1. Router 只执行，不做确认策略；Agent/Policy 层统一决定是否允许。
2. 或 Router 返回明确 union：

```python
@dataclass
class toolExecutionDecision:
    result: toolResult | None = None
    confirmationReason: str | None = None
```

不要在同一个接口里混用 `toolResult` 和业务异常。

---

### 🟡 Medium — `conversation` 同时负责内存消息和 JSONL 文件 I/O

**位置**：`flamingoAgents/core/conversation.py:17-49`、`flamingoAgents/core/conversation.py:52-70`

**问题**：`conversation.addMessage()` 和 `addToolResult()` 不只是修改内存消息，还直接写 JSONL。日志路径不可写、磁盘满、JSON 序列化失败，都会影响会话状态更新。

**为什么边界不清**：conversation 是领域状态对象，JSONL 是审计持久化实现。把二者绑在一起，会让 core 业务逻辑被文件系统故障牵连。

**修复方案**：conversation 只维护 `messages`，事件通过注入的 `eventSink` 处理。

```python
class conversation:
    def __init__(self, sessionId: str, systemPrompt: str, eventSink: eventSink | None = None):
        self.eventSink = eventSink or nullEventSink()
```

或者 agent 在调用 `conversation.addMessage()` 后单独 `logger.logMessage()`，至少让状态变更和日志 I/O 分层。

---

### 🟡 Medium — `utils/jsonl.py` 变成杂货铺，preview/redaction 被工具层反向依赖

**位置**：`flamingoAgents/utils/jsonl.py:14-45`、`flamingoAgents/tools/file.py:15`、`flamingoAgents/tools/bash.py:14`

**问题**：`makePreview()`、`redactText()` 放在 `jsonl.py`，导致 file/bash 工具为了做输出预览而依赖 JSONL 日志模块。

**为什么边界不清**：preview/redaction 是通用文本处理能力；JSONL writer 是一种日志持久化实现。工具执行层不应该 import 日志 writer 所在模块。

**修复方案**：拆成：

- `utils/preview.py`：`makePreview()`
- `utils/redaction.py`：`redactText()` / secret patterns
- `utils/jsonl.py`：只保留 `jsonlLog`，依赖 preview/redaction

然后工具层只 import `utils.preview.makePreview`。

---

### 🟡 Medium — CLI 和 HTTP 各自复制 `buildAgent()`，组合根分裂

**位置**：`flamingoAgents/app/cli.py:30-43`、`flamingoAgents/app/server.py:113-126`

**问题**：两个入口几乎复制同一段装配逻辑：创建 debug、加载模型配置、创建 adapter、创建 registry、创建 agent。差异只有 `confirmDeletion`。

**为什么边界不清**：组合根应该集中表达“系统怎么被拼起来”。现在装配散在两个入口，未来改模型配置、日志、工具注册时容易一个入口改了另一个漏掉。

**修复方案**：新增共享装配函数，例如 `flamingoAgents/app/buildAgent.py`：

```python
def buildAgent(debugEnabled: bool, workDir: Path, confirmDeletion: confirmationHandler | None) -> agent:
    printer = debugConsole(debugEnabled)
    config = loadModelConfig()
    adapter = chatCompletionsAdapter(config, printer)
    return agent(
        modelAdapter=adapter,
        registry=createDefaultRegistry(),
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugConsole=printer,
        confirmDeletion=confirmDeletion,
    )
```

CLI/HTTP 只传不同 confirmation handler。

---

### 🟡 Medium — `models/registry.py` 名称和职责不符，还隐藏硬编码配置策略

**位置**：`flamingoAgents/models/registry.py:22-29`、`flamingoAgents/models/registry.py:55-62`

**问题**：文件名叫 `registry.py`，但实际做的是模型配置加载：默认路径、YAML 解析、环境变量 fallback、默认 providerId=`101`、默认选择第一个模型。它并不是“模型注册表”。

**为什么边界不清**：配置发现策略是 app/装配层的职责；配置文件解析是 config loader 的职责；模型协议适配是 adapter 的职责。当前全部塞在 `models/registry.py`，名字也会和 `tools/registry.py` 混淆。

**修复方案**：

- 改名为 `models/config.py` 或 `models/configLoader.py`。
- `loadModelConfig()` 接收显式 `configPath/providerId/modelId`。
- CLI/HTTP 增加参数或统一默认值，而不是模型层硬编码 repo 根路径。

---

### 🟡 Medium — HTTP 层缺少错误分类，把所有 Agent 错误都映射成 500

**位置**：`flamingoAgents/app/server.py:66`、`flamingoAgents/app/server.py:86`、`flamingoAgents/app/server.py:89-96`

**问题**：

```python
statusCode = 200 if result.status != 'error' else 500
```

Agent 返回 error 可能是用户输入问题、pending confirmation 不匹配、模型服务故障、工具参数错误。HTTP 层全部返回 500，不利于客户端处理。

`readJson()` 解析失败直接返回 `{}`，最后变成 `message 必须是非空字符串`，没有准确报告 invalid JSON。

**为什么边界不清**：core 只给 `status='error'`，没有错误类别；HTTP adapter 被迫猜状态码，最后只能全当服务器错误。

**修复方案**：给 `runResult` 增加 `errorCode` 或 `errorKind`：

```python
errorKind = Literal['badRequest', 'conflict', 'modelFailure', 'toolFailure', 'internal']
```

HTTP 层映射：

- `badRequest` -> 400
- `conflict` pending 问题 -> 409
- `modelFailure` -> 502
- `internal` -> 500

`readJson()` 应该在 JSONDecodeError 时直接响应 400 invalid JSON。

---

### 🟡 Medium — Core 端口没有协议类型，靠 `Any` 和隐式方法签名连接模块

**位置**：`flamingoAgents/core/agent.py:34-47`、`flamingoAgents/core/types.py:46`

**问题**：`agent.__init__()` 的 `modelAdapter: Any`、`toolContext.debugConsole: Any | None` 让模块边界靠“约定俗成”。比如 `modelAdapter` 必须有 `complete(messages, tools, logger)`，但类型系统没有表达。`debugConsole` 必须有 `.debug()` 和 `.isDebug`，也没有接口。

**为什么边界不清**：端口层没有定义协议，导致 core 实际依赖了具体对象形状，但代码上看不出来。

**修复方案**：新增 `core/ports.py`：

```python
class modelAdapterPort(Protocol):
    def complete(self, messages: list[chatMessage], tools: list[toolSpec]) -> chatMessage: ...

class debugPort(Protocol):
    isDebug: bool
    def debug(self, message: str) -> None: ...
```

然后 `agent` 依赖 Protocol，而不是 `Any`。

---

### 🟡 Medium — `manualChecks.py` 通过了，但覆盖不到本次暴露的边界问题

**位置**：`manualChecks.py:33-70`、`manualChecks.py:175-224`

**问题**：现有手动检查能验证 happy path：文件工具、bash、删除拒绝、HTTP 确认。但没有覆盖：

- `../` 路径逃逸；
- 模型返回非 dict `tool_call.arguments`；
- 一次 assistant 多个 tool calls 且中间有确认；
- pending confirmation 未处理时再次 `/chat`；
- 错 sessionId 调 `/confirm` 不应删除真实 pending；
- 并发同 session 请求。

**为什么边界不清**：测试没有按模块边界失败模式设计，只按端到端 happy path 设计，所以边界问题会长期潜伏。

**修复方案**：保留 `manualChecks.py` 可以，但应补正式测试文件，按模块拆：

- file tool sandbox tests
- model adapter parse tests
- agent confirmation state machine tests
- HTTP error mapping tests

---

### 🔵 Low — `models/registry.py` 有未使用导入，说明职责曾经混杂但清理不完整

**位置**：`flamingoAgents/models/registry.py:10-13`

**问题**：`json`、`urllib.error`、`urllib.request` 当前未使用。文件头还说 “runs direct endpoint validation”，但当前文件没有直接 endpoint validation 函数。

**修复方案**：删除未使用 import，更新文件头 description。这个问题本身不严重，但反映该模块职责从“配置 + 直连测试”演化后没有收口。

---

### 🔵 Low — 文档里的 CLI 命令和 `pyproject.toml` 暴露命令不一致

**位置**：`pyproject.toml:10-11`、`docs/flamingoAgentsFlow.md:15`

**问题**：`pyproject.toml` 暴露：

```toml
Flamingo = "flamingoAgents.app.cli:main"
flamingo-agents-server = "flamingoAgents.app.server:main"
```

但文档写的是 `flamingo-agents` 与 `flamingo-agents-server`。入口边界对使用者不清晰。

**修复方案**：统一命令名。要么改 pyproject 增加 `flamingo-agents`，要么文档统一写 `Flamingo`。

---

## 修复优先级建议

1. **先修文件工具沙箱边界**：这是最直接的安全边界问题，`workDir` 既然存在就必须有效。
2. **再修确认状态机**：包括多 tool call pending、pending 时禁止新 user message、`continueConfirmation()` 先校验再 pop。这些会直接破坏模型 tool-call 协议。
3. **然后收口模型配置/凭据边界**：把 `apiKeyEnv -> apiKey/authHeader` 的解析移到配置或装配层，删除 adapter 里的 `os.getenv()` 和配置层的 `os.environ` 写入。
4. **接着拆协议泄漏和日志耦合**：工具注册表保持协议中立；模型 adapter 不直接依赖 `jsonlLog`。
5. **最后整理组合根和 utils**：共享 `buildAgent()`，拆 `preview/redaction/jsonl`，补正式边界测试。
