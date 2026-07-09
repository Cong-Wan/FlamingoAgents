# Function Call Callable Registry 方案文档

**日期：** 2026-07-06  
**作者：** wilbur  
**状态：** 待用户审阅  
**推荐方案：** Code-first Callable Tool Registry  

---

## 1. 背景

当前 FlamingoAgents 已经能把工具 schema 暴露给 OpenAI-compatible Chat Completions，并能解析模型返回的 `tool_calls`。但是，现有实现的工具体系并不是真正清晰的 function call 体系。

目前核心链路是：

```text
config/tools.yaml
  -> loadToolConfig()
  -> toolDefinition(name, description, parameters, runtime, permissions)
  -> buildModelTools()
  -> model returns tool_calls
  -> executeTool(definition, arguments, context, toolCallId)
  -> runtime.type / runtime.operation switch
  -> executeFileRead / executeFileWrite / executeFileEdit / executeShellRuntime
```

这个链路的问题是：工具定义里没有绑定具体 Python 调用函数。工具只是 YAML 中的一段配置，真正执行逻辑集中在 `toolRuntime.py` 的大分发器里。

这导致新增工具时，开发者不是“定义一个函数并注册为工具”，而是需要同时维护 YAML、runtime 分发规则、schema、权限和执行逻辑。随着工具数量变多，这会变得混乱。

---

## 2. 调研范围

本方案基于以下代码和文档调研。

### 2.1 当前项目

- `config/tools.yaml`
- `flamingoAgents/core/agent.py`
- `flamingoAgents/core/conversation.py`
- `flamingoAgents/core/types.py`
- `flamingoAgents/models/chatCompletions.py`
- `flamingoAgents/tools/toolConfig.py`
- `flamingoAgents/tools/toolPolicy.py`
- `flamingoAgents/tools/toolRuntime.py`
- `flamingoAgents/tools/toolSchema.py`
- `manualChecks.py`

### 2.2 pi 实现

- `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/docs/sdk.md`
- `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/docs/extensions.md`
- `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/index.js`
- `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/*.d.ts`
- `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/tools/tool-definition-wrapper.js`
- `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/agent-session.js`

pi 的关键设计是：`ToolDefinition` 不只是 schema，它还包含 `execute` 方法。built-in tools 通过 factory 创建，例如 `createReadToolDefinition(cwd)`、`createBashToolDefinition(cwd)`。模型可见 schema 是从 tool definition 投影出来的，执行时直接调用绑定的 `execute`。

### 2.3 LangChain 实现

当前项目没有安装 LangChain。为避免凭印象判断，已临时下载 `langchain-core` 到 `/tmp/flamingoLangchainInspect` 做只读调研，没有改动项目依赖。

重点查看：

- `/tmp/flamingoLangchainInspect/langchain_core/tools/base.py`
- `/tmp/flamingoLangchainInspect/langchain_core/tools/structured.py`
- `/tmp/flamingoLangchainInspect/langchain_core/tools/convert.py`
- `/tmp/flamingoLangchainInspect/langchain_core/tools/simple.py`

LangChain 的关键设计是：函数可以通过 `@tool` 或 `StructuredTool.from_function()` 转换为 tool。tool 内部有真实 callable，schema 来自显式 `args_schema` 或函数签名推断，运行时通过 `invoke()` / `run()` 调用该 callable。

---

## 3. 当前实现的问题

### 3.1 `toolDefinition` 没有真实 callable

当前结构：

```python
@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    runtime: dict[str, Any]
    permissions: list[permissionRule]
```

这里没有 `execute` 字段。也就是说，工具定义无法回答一个最基本的问题：

> 这个工具最终调用哪个 Python 函数？

这正是当前 function call 看起来混乱的根因。

### 3.2 `toolRuntime.py` 承担了过多职责

当前 `executeTool()` 负责：

1. 参数类型检查；
2. JSON Schema 子集校验；
3. 根据 `runtime.type` 分发；
4. 根据 `runtime.operation` 再分发；
5. 执行 file 或 shell 的具体逻辑；
6. 异常包装。

这使它变成一个中央执行器和中央工具实现集合。新增工具需要修改中央模块，违反开闭原则，也让工具边界不清晰。

### 3.3 YAML 同时承担“定义”和“实现”两种角色

当前 `config/tools.yaml` 同时声明：

- 模型可见的 name / description / parameters；
- runtime 类型；
- runtime operation；
- pathField / contentField / commandField；
- permission rules。

这会让配置文件承担过多职责。配置应该控制“启用和策略”，不应该承担“实现哪个函数”的职责。

### 3.4 新增工具路径不自然

理想情况下，新增一个工具应该是：

```text
写一个 Python 函数 -> 包成 toolDefinition -> 注册
```

当前则是：

```text
写 schema -> 写 YAML runtime -> 确保 runtime.type 被支持 -> 修改分发器 -> 写执行函数 -> 接权限
```

这不是好的 function call 体验。

---

## 4. 设计目标

新的工具体系必须满足以下目标。

### 4.1 每个工具都有明确调用函数

每个工具定义必须能直接指向一个 Python callable，例如：

```python
execute=readTool
```

看到工具定义，就能知道模型调用该工具时最终执行哪个函数。

### 4.2 模型 schema 和执行函数来自同一个定义

不能出现 schema 在 YAML、执行函数在另一个 switch 分支、权限在第三处的割裂状态。

目标结构是：

```text
toolDefinition
  - name
  - description
  - parameters
  - execute
  - permissions
```

模型 schema 从 `toolDefinition` 投影生成，运行时也从同一个 `toolDefinition` 调用 `execute`。

### 4.3 配置只管启用和权限

YAML 不再描述 runtime 实现。配置文件只负责：

- 启用哪些工具；
- 给哪些工具配置权限规则；
- 后续可选地配置工具策略参数。

### 4.4 新增工具不修改中央 executor

新增工具时，不应该修改 `executeTool()` 的分发逻辑。

新增流程应变成：

```text
新增函数 -> 新增 factory -> 注册到 builtin tool map 或 custom registry
```

### 4.5 保持当前项目轻量

第一阶段不引入 LangChain，不引入 Pydantic。原因：

- 当前项目是 pure library，依赖极少；
- 当前 schema 子集已经够 read/write/edit/bash 使用；
- 直接引入 LangChain/Pydantic 会把改造范围放大；
- 先解决结构问题，再考虑签名推断。

---

## 5. 推荐方案概述

推荐采用：

> **Code-first Callable Tool Registry**

核心思想：

```text
工具由 Python 代码定义。
配置只决定启用和权限。
模型只看 schema。
运行时通过 registry 找到具体 callable 并执行。
```

新的链路：

```text
createAgent(workDir)
  -> loadToolSettings(config/tools.yaml)
  -> createBuiltinToolDefinitions(workDir)
  -> apply enabledTools and permissions
  -> toolRegistry(definitions)

agent.runUserMessage()
  -> buildModelTools(registry.list())
  -> modelAdapter.complete(messages, tools)
  -> parse assistant.toolCalls
  -> processToolBatch()
  -> registry.get(call.toolName)
  -> evaluateToolCall(definition, call)
  -> executeToolCall(definition, call, context)
  -> definition.execute(arguments, context)
  -> wrap toolOutput into toolResult
  -> conversation.addToolResult(result)
```

最关键的变化：

```text
旧：toolName -> runtime.type switch -> operation switch -> concrete function
新：toolName -> toolRegistry -> toolDefinition.execute -> concrete function
```

---

## 6. 核心数据结构

### 6.1 `toolOutput`

建议新增 `toolOutput`，表示工具函数本身的输出。它不关心 `toolCallId`，也不需要重复 `toolName`。

```python
@dataclass
class toolOutput:
    content: str
    isError: bool = False
    details: dict[str, Any] = field(default_factory=dict)
```

原因：

- 具体工具函数只应该关心业务输出；
- `toolCallId` 和 `toolName` 是调用上下文，由 executor 统一包装；
- 这和 pi 的 `AgentToolResult` 思路一致：工具返回内容和 details，外层 runtime 负责关联 call id。

### 6.2 `toolDefinition`

建议新的 `toolDefinition`：

```python
toolExecuteFunction = Callable[[dict[str, Any], toolContext], toolOutput]
toolPrepareFunction = Callable[[dict[str, Any]], dict[str, Any]]
toolPreviewFunction = Callable[[dict[str, Any]], str]

@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: toolExecuteFunction
    permissions: list[permissionRule] = field(default_factory=list)
    prepareArguments: toolPrepareFunction | None = None
    preview: toolPreviewFunction | None = None
```

字段说明：

- `name`：模型调用的函数名；
- `description`：模型可见描述；
- `parameters`：OpenAI tool/function parameters schema；
- `execute`：真实 Python callable；
- `permissions`：调用前权限规则；
- `prepareArguments`：兼容模型输出或旧参数格式的预处理钩子；
- `preview`：需要用户确认时，生成更友好的确认预览。

### 6.3 `toolRegistry`

建议新增 registry：

```python
class toolRegistry:
    def __init__(self, definitions: list[toolDefinition]):
        self.definitions = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: toolDefinition) -> None:
        if definition.name in self.definitions:
            raise RuntimeError(f'工具名称重复：{definition.name}')
        self.definitions[definition.name] = definition

    def get(self, name: str) -> toolDefinition | None:
        return self.definitions.get(name)

    def list(self) -> list[toolDefinition]:
        return list(self.definitions.values())
```

职责：

- 管理 name 到 toolDefinition 的映射；
- 保证工具名唯一；
- 给 agent 提供统一查询入口。

---

## 7. 工具执行流程

### 7.1 新的 `executeToolCall()`

建议 executor 接收完整 `toolCall`，而不是只接收 arguments：

```python
def executeToolCall(definition: toolDefinition, call: toolCall, context: toolContext) -> toolResult:
    arguments = call.arguments
    if not isinstance(arguments, dict):
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content='toolCall.arguments 必须是对象。',
            details={'invalidArguments': True},
        )

    if definition.prepareArguments:
        arguments = definition.prepareArguments(arguments)

    schemaError = validateArguments(definition.parameters, arguments)
    if schemaError:
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content=f'工具参数不符合 schema：{schemaError}',
            details={'schemaError': schemaError},
        )

    try:
        output = definition.execute(arguments, context)
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=output.isError,
            content=output.content,
            details=output.details,
        )
    except Exception as error:
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content=f'工具执行异常：{type(error).__name__}: {error}',
            details={'exceptionType': type(error).__name__},
        )
```

这里 executor 不再知道 `read`、`write`、`edit`、`bash`。它只负责通用执行框架。

### 7.2 权限判断保持在 agent 层

权限仍在执行前判断：

```python
def processToolBatch(sessionId: str, toolCalls: list[toolCall], startIndex: int) -> runResult | None:
    currentConversation = self.getConversation(sessionId)
    context = toolContext(workDir=self.workDir, debugConsole=self.debugConsole)
    for index in range(startIndex, len(toolCalls)):
        call = toolCalls[index]
        definition = self.toolRegistry.get(call.toolName)
        if definition is None:
            currentConversation.addToolResult(self.makeUnknownToolResult(call))
            continue

        decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
        if decision.requiresApproval:
            return self.createConfirmationResult(sessionId, toolCalls, index, definition, call, decision)

        result = executeToolCall(definition, call, context)
        currentConversation.addToolResult(result)
    return None
```

这个设计保持当前确认机制不被破坏。

### 7.3 `preview` 用于确认展示

当前 `runResult.commandPreview` 是：

```python
commandPreview=str(call.arguments)
```

这太粗糙。新的 toolDefinition 可以提供 preview：

```python
def bashPreview(arguments: dict[str, Any]) -> str:
    return str(arguments.get('command', ''))
```

然后 agent 使用：

```python
preview = definition.preview(call.arguments) if definition.preview else str(call.arguments)
```

这样确认提示更像真实工具调用，而不是一坨 dict。

---

## 8. 内置工具设计

### 8.1 文件结构建议

第一阶段建议使用较小的文件拆分：

```text
flamingoAgents/tools/
  toolDefinition.py   # toolOutput、toolDefinition、defineTool
  toolRegistry.py     # toolRegistry
  toolExecutor.py     # validate + executeToolCall
  builtinTools.py     # createReadTool/createWriteTool/createEditTool/createBashTool
  toolPolicy.py       # 保留权限判断，适配新 definition
  toolSchema.py       # definition -> model tool schema
  toolConfig.py       # 加载 enabledTools 和 permissions
```

如果后续文件变大，再拆成：

```text
flamingoAgents/tools/fileTools.py
flamingoAgents/tools/shellTools.py
flamingoAgents/tools/toolValidation.py
```

第一阶段不建议过度拆分。

### 8.2 `defineTool()`

提供一个简单 helper：

```python
def defineTool(
    *,
    name: str,
    description: str,
    parameters: dict[str, Any],
    execute: toolExecuteFunction,
    permissions: list[permissionRule] | None = None,
    prepareArguments: toolPrepareFunction | None = None,
    preview: toolPreviewFunction | None = None,
) -> toolDefinition:
    return toolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        execute=execute,
        permissions=permissions or [],
        prepareArguments=prepareArguments,
        preview=preview,
    )
```

这类似 pi 的 `defineTool()`，但保持 Python 项目轻量。

---

## 9. 最终工具形态：以 read/write/edit/bash 为准

本节明确“一个工具最终长什么样”。新的工具不是 YAML 里的 runtime 描述，而是一个完整的 Python tool definition。每个工具在系统中同时有五层形态：

1. **源码执行函数**：例如 `readTool(arguments, context) -> toolOutput`，这是最终被调用的真实函数；
2. **工具定义对象**：例如 `createReadTool()` 返回的 `toolDefinition`，绑定 name、description、parameters、execute、permissions、preview；
3. **Registry 记录**：`toolRegistry` 内部保存 `name -> toolDefinition`；
4. **模型可见 schema**：`buildModelTool(definition)` 只投影 name、description、parameters 给模型；
5. **运行时调用链**：模型返回 `toolCall` 后，executor 根据 registry 找到 definition，再调用 `definition.execute(arguments, context)`。

### 9.1 通用形态

所有工具最终都遵循同一个结构：

```python
@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: toolExecuteFunction
    permissions: list[permissionRule] = field(default_factory=list)
    prepareArguments: toolPrepareFunction | None = None
    preview: toolPreviewFunction | None = None
```

运行时不再识别 `runtime.type` 和 `runtime.operation`。executor 只做通用流程：校验参数、调用 `execute`、把 `toolOutput` 包装成 `toolResult`。

```python
output = definition.execute(arguments, context)
result = toolResult(
    toolCallId=call.id,
    toolName=definition.name,
    isError=output.isError,
    content=output.content,
    details=output.details,
)
```

### 9.2 `read` 的最终形态

#### 9.2.1 Python definition

```python
def createReadTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='read',
        description='读取本地文本文件，可通过 offset 和 limit 控制读取的行范围。',
        parameters={
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'offset': {'type': 'integer', 'minimum': 1, 'default': 1},
                'limit': {'type': 'integer', 'minimum': 1, 'default': 2000},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
        execute=readTool,
        permissions=permissions or [],
        preview=previewReadTool,
    )
```

#### 9.2.2 真实执行函数

```python
def previewReadTool(arguments: dict[str, Any]) -> str:
    path = str(arguments.get('path', ''))
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    return f'{path} offset={offset} limit={limit}'


def readTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    if offset < 1 or limit < 1:
        return toolOutput(content='read.offset 和 read.limit 必须大于 0。', isError=True)
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedText = ''.join(lines[startIndex:startIndex + limit])
    previewText, previewTruncated = makePreview(selectedText)
    return toolOutput(
        content=previewText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': startIndex + limit < len(lines) or previewTruncated,
        },
    )
```

#### 9.2.3 模型看到的 schema

```python
{
    'type': 'function',
    'function': {
        'name': 'read',
        'description': '读取本地文本文件，可通过 offset 和 limit 控制读取的行范围。',
        'parameters': {
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'offset': {'type': 'integer', 'minimum': 1, 'default': 1},
                'limit': {'type': 'integer', 'minimum': 1, 'default': 2000},
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
}
```

#### 9.2.4 模型调用如何落到函数

```python
call = toolCall(
    id='call_read_1',
    toolName='read',
    arguments={'path': 'docs/guide.md', 'offset': 1, 'limit': 40},
)

definition = registry.get('read')
output = definition.execute(call.arguments, context)
```

最终返回给 conversation 的结果形态：

```python
toolResult(
    toolCallId='call_read_1',
    toolName='read',
    isError=False,
    content='第一行文本\n第二行文本\n',
    details={
        'path': '/project/docs/guide.md',
        'offset': 1,
        'limit': 40,
        'totalLines': 2,
        'truncated': False,
    },
)
```

### 9.3 `write` 的最终形态

#### 9.3.1 Python definition

```python
def createWriteTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='write',
        description='创建或完整覆盖本地文本文件。',
        parameters={
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'content': {'type': 'string'},
            },
            'required': ['path', 'content'],
            'additionalProperties': False,
        },
        execute=writeTool,
        permissions=permissions or [],
        preview=previewWriteTool,
    )
```

#### 9.3.2 真实执行函数

```python
def previewWriteTool(arguments: dict[str, Any]) -> str:
    content = str(arguments.get('content', ''))
    return f"{arguments.get('path', '')} bytes={len(content.encode('utf-8'))}"


def writeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
    content = str(arguments['content'])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    previewText, truncated = makePreview(content)
    return toolOutput(
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
            'contentPreview': previewText,
            'truncated': truncated,
        },
    )
```

#### 9.3.3 模型调用如何落到函数

```python
call = toolCall(
    id='call_write_1',
    toolName='write',
    arguments={'path': 'notes/todo.md', 'content': '- review function call design\n'},
)

definition = registry.get('write')
output = definition.execute(call.arguments, context)
```

最终结果形态：

```python
toolResult(
    toolCallId='call_write_1',
    toolName='write',
    isError=False,
    content='已写入文件：/project/notes/todo.md',
    details={
        'path': '/project/notes/todo.md',
        'bytes': 30,
        'contentPreview': '- review function call design\n',
        'truncated': False,
    },
)
```

### 9.4 `edit` 的最终形态

#### 9.4.1 Python definition

```python
def createEditTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='edit',
        description='对已有文本文件进行精确文本替换。每个 oldText 必须唯一匹配。',
        parameters={
            'type': 'object',
            'properties': {
                'path': {'type': 'string'},
                'edits': {
                    'type': 'array',
                    'minItems': 1,
                    'items': {
                        'type': 'object',
                        'properties': {
                            'oldText': {'type': 'string'},
                            'newText': {'type': 'string'},
                        },
                        'required': ['oldText', 'newText'],
                        'additionalProperties': False,
                    },
                },
            },
            'required': ['path', 'edits'],
            'additionalProperties': False,
        },
        execute=editTool,
        permissions=permissions or [],
        preview=previewEditTool,
    )
```

#### 9.4.2 真实执行函数

```python
def previewEditTool(arguments: dict[str, Any]) -> str:
    edits = arguments.get('edits', [])
    editCount = len(edits) if isinstance(edits, list) else 0
    return f"{arguments.get('path', '')} edits={editCount}"


def editTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
    edits = arguments['edits']
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    originalContent = path.read_text(encoding='utf-8')
    replacements: list[tuple[int, int, str]] = []
    for index, editItem in enumerate(edits):
        oldText = editItem['oldText']
        newText = editItem['newText']
        matchCount = originalContent.count(oldText)
        if matchCount != 1:
            return toolOutput(content=f'第 {index + 1} 个 oldText 必须精确且唯一匹配，当前匹配数：{matchCount}。', isError=True)
        startIndex = originalContent.index(oldText)
        endIndex = startIndex + len(oldText)
        replacements.append((startIndex, endIndex, newText))

    replacements.sort(key=lambda item: item[0])
    previousEnd = -1
    for startIndex, endIndex, newText in replacements:
        if startIndex < previousEnd:
            return toolOutput(content='多个 edits 不能重叠。', isError=True)
        previousEnd = endIndex

    updatedContent = originalContent
    for startIndex, endIndex, newText in sorted(replacements, key=lambda item: item[0], reverse=True):
        updatedContent = updatedContent[:startIndex] + newText + updatedContent[endIndex:]

    diffText = ''.join(difflib.unified_diff(
        originalContent.splitlines(keepends=True),
        updatedContent.splitlines(keepends=True),
        fromfile=str(path) + ':before',
        tofile=str(path) + ':after',
        n=3,
    ))
    path.write_text(updatedContent, encoding='utf-8')
    previewText, truncated = makePreview(diffText)
    return toolOutput(
        content=previewText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits), 'diffTruncated': truncated},
    )
```

#### 9.4.3 模型调用如何落到函数

```python
call = toolCall(
    id='call_edit_1',
    toolName='edit',
    arguments={
        'path': 'notes/todo.md',
        'edits': [{'oldText': '- review function call design', 'newText': '- review callable tool registry'}],
    },
)

definition = registry.get('edit')
output = definition.execute(call.arguments, context)
```

最终结果形态：

```python
toolResult(
    toolCallId='call_edit_1',
    toolName='edit',
    isError=False,
    content='--- /project/notes/todo.md:before\n+++ /project/notes/todo.md:after\n@@ -1 +1 @@\n- review function call design\n+ review callable tool registry\n',
    details={'path': '/project/notes/todo.md', 'editCount': 1, 'diffTruncated': False},
)
```

### 9.5 `bash` 的最终形态

#### 9.5.1 Python definition

```python
def createBashTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='bash',
        description='在工作目录中执行 bash 命令。curl、python、grep、open 均通过此工具执行。',
        parameters={
            'type': 'object',
            'properties': {
                'command': {'type': 'string'},
                'timeout': {'type': 'integer', 'minimum': 1, 'default': 30},
            },
            'required': ['command'],
            'additionalProperties': False,
        },
        execute=bashTool,
        permissions=permissions or [],
        preview=previewBashTool,
    )
```

#### 9.5.2 真实执行函数

```python
def previewBashTool(arguments: dict[str, Any]) -> str:
    return str(arguments.get('command', ''))


def bashTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    command = arguments.get('command')
    if not isinstance(command, str) or not command.strip():
        return toolOutput(content='bash.command 必须是非空字符串。', isError=True)

    timeout = int(arguments.get('timeout', defaultTimeoutSeconds))
    if timeout < 1:
        timeout = defaultTimeoutSeconds
    if timeout > maxTimeoutSeconds:
        timeout = maxTimeoutSeconds

    try:
        completedProcess = subprocess.run(
            ['bash', '-lc', command],
            cwd=str(context.workDir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdoutPreview, stdoutTruncated = makePreview(completedProcess.stdout)
        stderrPreview, stderrTruncated = makePreview(completedProcess.stderr)
        return toolOutput(
            content=f'exitCode: {completedProcess.returncode}\nstdout:\n{stdoutPreview}\nstderr:\n{stderrPreview}',
            isError=completedProcess.returncode != 0,
            details={
                'command': command,
                'timeout': timeout,
                'exitCode': completedProcess.returncode,
                'stdoutPreview': stdoutPreview,
                'stderrPreview': stderrPreview,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
    except subprocess.TimeoutExpired as error:
        stdoutText = error.stdout if isinstance(error.stdout, str) else ''
        stderrText = error.stderr if isinstance(error.stderr, str) else ''
        stdoutPreview, stdoutTruncated = makePreview(stdoutText)
        stderrPreview, stderrTruncated = makePreview(stderrText)
        return toolOutput(
            content=f'命令超时，已终止。timeout: {timeout}\nstdout:\n{stdoutPreview}\nstderr:\n{stderrPreview}',
            isError=True,
            details={
                'command': command,
                'timeout': timeout,
                'timeoutExpired': True,
                'stdoutPreview': stdoutPreview,
                'stderrPreview': stderrPreview,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
```

#### 9.5.3 permission 如何附着到 bash

权限不再写在 bash 的 runtime 中，而是从 `toolPermissions.bash` 解析后附着到 definition：

```python
permissionsByTool = loadToolSettings(configPath).permissionsByTool
bashDefinition = createBashTool(permissionsByTool.get('bash', []))
```

运行时仍然是先评估权限，再执行函数：

```python
decision = evaluateToolCall(bashDefinition, call, debugConsole=debugConsole)
if decision.requiresApproval:
    return confirmationRequiredResult
output = bashDefinition.execute(call.arguments, context)
```

### 9.6 registry 中的最终形态

当 `enabledTools` 是 `['read', 'write', 'edit', 'bash']` 时，builder 最终会创建四个 definition 并注册：

```python
definitions = [
    createReadTool(permissionsByTool.get('read', [])),
    createWriteTool(permissionsByTool.get('write', [])),
    createEditTool(permissionsByTool.get('edit', [])),
    createBashTool(permissionsByTool.get('bash', [])),
]
registry = toolRegistry(definitions)
```

registry 内部等价于：

```python
{
    'read': readDefinition,
    'write': writeDefinition,
    'edit': editDefinition,
    'bash': bashDefinition,
}
```

其中每个 definition 都有自己的 `execute` 函数：

```python
readDefinition.execute is readTool
writeDefinition.execute is writeTool
editDefinition.execute is editTool
bashDefinition.execute is bashTool
```

### 9.7 一次完整工具调用的最终形态

以 `edit` 为例，最终链路是：

```text
模型看到 edit schema
  -> 模型返回 tool_call: name=edit, arguments={path, edits}
  -> agent 从 registry 获取 editDefinition
  -> evaluateToolCall(editDefinition, call)
  -> executeToolCall(editDefinition, call, context)
  -> editDefinition.execute(call.arguments, context)
  -> editTool(arguments, context)
  -> toolOutput(content=diffText, details={path, editCount, diffTruncated})
  -> executor 包装成 toolResult(toolCallId, toolName, isError, content, details)
  -> conversation.addToolResult(result)
  -> 下一轮 modelAdapter.complete(messages, tools)
```

这就是新方案下工具的最终形态：**工具是一个带 schema 和 execute callable 的 Python 对象，模型只看到 schema，运行时只调用 callable。**

---

## 10. 配置文件新设计

### 10.1 建议从 `version: 1` 升到 `version: 2`

旧配置承担 runtime 实现。新配置只承担启用和权限。

建议新格式：

```yaml
version: 2

enabledTools:
  - read
  - write
  - edit
  - bash

toolPermissions:
  bash:
    - id: deletionCommand
      field: command
      action: requireApproval
      reason: 删除命令需要用户确认
      match:
        type: regex
        patterns:
          - '(^|[;&|]\s*)rm\s+(-[A-Za-z]*\s+)*[^\n;&|]+'
          - '(^|[;&|]\s*)rmdir\s+[^\n;&|]+'
          - '(^|[;&|]\s*)unlink\s+[^\n;&|]+'
          - '(^|[;&|]\s*)find\s+[^\n;&|]*\s-delete(\s|$)'
          - 'os\.(remove|unlink|rmdir)\s*\('
          - 'shutil\.rmtree\s*\('
          - 'pathlib\.[A-Za-z0-9_\.]+\.(unlink|rmdir)\s*\('
```

### 10.2 `toolConfig.py` 的新职责

`toolConfig.py` 不再解析完整 tool definition。它只解析：

```python
@dataclass
class toolSettings:
    enabledTools: list[str]
    permissionsByTool: dict[str, list[permissionRule]]
```

然后 builder 负责组装：

```python
settings = loadToolSettings(configPath)
definitions = createBuiltinTools(settings.enabledTools, settings.permissionsByTool)
```

### 10.3 兼容旧配置的策略

建议第一轮实现时可以不保留旧配置兼容，因为当前项目还在早期阶段，直接升级为 `version: 2` 更清楚。

如果需要兼容旧配置，可以临时支持：

- 读取旧 `version: 1`；
- 忽略 runtime；
- 使用旧 tools 数组里的 name 作为 enabledTools；
- 使用旧 permissions。

但这会增加迁移代码。除非你明确要求保留旧格式，否则不建议第一版做兼容。

---

## 11. Agent 层改造

### 11.1 构造函数

当前：

```python
self.toolDefinitions = {definition.name: definition for definition in toolDefinitions}
```

建议改成：

```python
self.toolRegistry = toolRegistry(toolDefinitions)
```

或者为了最小改动，也可以内部仍保留 dict，但命名改为 registry 更准确。

### 11.2 模型工具 schema

当前：

```python
modelTools = buildModelTools(list(self.toolDefinitions.values()))
```

建议：

```python
modelTools = buildModelTools(self.toolRegistry.list())
```

`toolSchema.py` 仍然只投影模型需要的字段：

```python
def buildModelTool(definition: toolDefinition) -> dict[str, Any]:
    return {
        'type': 'function',
        'function': {
            'name': definition.name,
            'description': definition.description,
            'parameters': definition.parameters,
        },
    }
```

`execute`、`permissions`、`preview` 不暴露给模型。

### 11.3 工具调用处理

当前：

```python
definition = self.toolDefinitions.get(call.toolName)
if definition is None:
    currentConversation.addToolResult(self.makeUnknownToolResult(call))
    continue
result = self.executeToolCall(call)
```

建议：

```python
definition = self.toolRegistry.get(call.toolName)
if definition is None:
    currentConversation.addToolResult(self.makeUnknownToolResult(call))
    continue
context = toolContext(workDir=self.workDir, debugConsole=self.debugConsole)
result = executeToolCall(definition, call, context)
```

### 11.4 pending confirmation

当前 pending 结构可以保持不变：

```python
@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCalls: list[toolCall]
    currentIndex: int
```

但是 confirmation result 的 `commandPreview` 建议改名或扩展。

当前：

```python
commandPreview: str | None = None
```

更准确的名字是：

```python
toolPreview: str | None = None
```

为了兼容现有 API，可以第一阶段保留 `commandPreview`，但其内容由 `definition.preview()` 生成。

---

## 12. 与模型适配层的关系

`chatCompletionsAdapter` 当前已经做了两件关键事情：

1. 请求中传 `tools` 和 `tool_choice: auto`；
2. 从 response 的 `tool_calls` 解析出内部 `toolCall`。

这部分不需要大改。

仍然保持：

```python
requestPayload = {
    'model': self.config.model,
    'messages': [self.convertMessage(message) for message in messages],
    'tools': tools,
    'tool_choice': 'auto',
}
```

解析仍然保持：

```python
parsedToolCalls.append(toolCall(
    id=rawCall.get('id') or f'call_{index + 1}',
    toolName=functionValue.get('name') or '',
    arguments=arguments,
))
```

也就是说，本次方案主要改工具定义和执行层，不改模型协议层。

---

## 13. 与 LangChain 的对应关系

| LangChain | FlamingoAgents 新方案 |
|---|---|
| `BaseTool` | `toolDefinition` |
| `StructuredTool.from_function()` | `defineTool(execute=someFunction)` |
| `args_schema` | `parameters` |
| `invoke()` / `run()` | `executeToolCall()` |
| `_run()` / `func` | `toolDefinition.execute` |
| `ToolMessage` | `toolResult` |
| `tool_call_id` | `toolCall.id`，由 executor 包装进 `toolResult` |

我们只吸收 LangChain 的核心思想：

```text
函数是工具的执行核心，schema 是工具的输入契约。
```

不引入 LangChain 的 callback manager、Runnable、Pydantic 推断等复杂能力。

---

## 14. 与 pi 的对应关系

| pi | FlamingoAgents 新方案 |
|---|---|
| `ToolDefinition.name` | `toolDefinition.name` |
| `ToolDefinition.description` | `toolDefinition.description` |
| `ToolDefinition.parameters` | `toolDefinition.parameters` |
| `ToolDefinition.execute` method | `toolDefinition.execute(arguments, context)` |
| `createReadToolDefinition(cwd)` | `createReadTool()` |
| `wrapToolDefinition()` | `executeToolCall()` 的统一包装 |
| `tool_call` hook | `evaluateToolCall()` / 后续 hook 扩展 |
| `tool_result` hook | 后续可扩展，不在第一阶段实现 |

我们更接近 pi 的工具定义方式：

```text
Definition = schema + metadata + execute
```

不同点是：FlamingoAgents 第一阶段保持同步执行和简单文本结果，不引入 streaming update / AbortSignal / TUI render。

---

## 15. 迁移策略

### 15.1 第一阶段：结构修正

目标：让 read/write/edit/bash 全部变成 callable tool。

需要修改：

- `flamingoAgents/core/types.py`
  - 新增 `toolOutput`。
- `flamingoAgents/tools/toolConfig.py`
  - 从完整工具定义 loader 改成 tool settings loader。
- `flamingoAgents/tools/toolRuntime.py`
  - 拆分或改造为通用 executor，不再包含 runtime switch。
- `flamingoAgents/tools/toolSchema.py`
  - 适配新的 `toolDefinition`。
- `flamingoAgents/tools/builtinTools.py`
  - 新增 built-in tools factory。
- `flamingoAgents/core/agent.py`
  - 用 registry 查询工具并调用 executor。
- `flamingoAgents/builder.py`
  - 从 `loadToolConfig()` 改成 `loadToolSettings()` + `createBuiltinTools()`。
- `config/tools.yaml`
  - 升级到 `version: 2`。
- `manualChecks.py`
  - 更新验证逻辑。

### 15.2 第二阶段：轻量装饰器

在第一阶段稳定后，可以新增：

```python
@tool(name='currentTime', description='获取当前时间。')
def currentTimeTool(timezone: str = 'local', *, context: toolContext) -> str:
    now = datetime.now().astimezone()
    return now.isoformat(timespec='seconds')
```

装饰器负责：

- 从函数签名生成简单 JSON Schema；
- 过滤 `context` 注入参数；
- 自动包装返回值为 `toolOutput`。

第二阶段不急，因为第一阶段最重要的是先把 callable 边界打清楚。

### 15.3 第三阶段：自定义工具扩展

后续可以支持用户传入 custom tools：

```python
def createAgent(
    workDir: str | Path,
    *,
    customTools: list[toolDefinition] | None = None,
    toolsConfigPath: str | Path | None = None,
) -> agent:
    settings = loadToolSettings(toolsConfigPath)
    definitions = createBuiltinTools(settings.enabledTools, settings.permissionsByTool)
    definitions.extend(customTools or [])
    return agent(toolDefinitions=definitions, workDir=Path(workDir).resolve())
```

这就接近 pi SDK 的 `customTools` 模式。

---

## 16. 验证方案

当前项目已有 `manualChecks.py`，继续沿用它作为无测试框架的验证入口。

建议更新或新增以下检查。

### 16.1 tool definition 检查

验证：

- built-in tools 包含 `read/write/edit/bash`；
- 每个 definition 都有 callable `execute`；
- `buildModelTools()` 不泄漏 `execute`、`permissions`、`preview`；
- schema 中仍然是 OpenAI tools 格式。

### 16.2 executor 检查

验证：

- 参数非 dict 返回错误；
- schema 校验失败返回错误；
- concrete callable 被实际调用；
- callable 抛异常会被包装成 `toolResult(isError=True)`；
- executor 正确注入 `toolCallId` 和 `toolName`。

### 16.3 built-in tools 检查

验证：

- read 能读取文件；
- write 能写入文件；
- edit 能精确替换并返回 diff；
- bash 能执行命令；
- bash 非零 exit code 标记为错误；
- bash timeout 生效；
- 路径逃逸仍然被阻止。

### 16.4 permission 检查

验证：

- bash 的删除命令触发确认；
- read 不触发确认；
- permission rules 从新 `toolPermissions` 正确绑定到对应 toolDefinition。

### 16.5 agent state machine 检查

验证：

- 模型返回 read tool call 后，agent 能执行具体函数并继续模型循环；
- 需要确认的 bash 调用不会提前执行；
- 用户拒绝后返回 blocked result；
- 用户批准后继续执行 pending batch；
- unknown tool 返回明确错误。

---

## 17. 风险和处理

### 17.1 风险：改动涉及多个核心文件

处理方式：

- 分阶段做；
- 第一阶段只迁移现有四个工具；
- 不引入 decorator、不引入 custom tool loading；
- 保持 `agent.py` 状态机行为尽量不动。

### 17.2 风险：配置格式升级破坏旧配置

处理方式：

- 项目早期建议直接升级到 `version: 2`；
- 如果你希望保留兼容，可在 `loadToolSettings()` 中短期支持旧格式；
- 兼容逻辑只迁移 name 和 permissions，不继续支持 runtime。

### 17.3 风险：schema validator 能力有限

当前 validator 只支持常用 JSON Schema 子集。第一阶段不扩大范围，保持现状即可。

后续如果加 decorator 或更复杂工具，再考虑支持：

- `boolean`
- `number`
- `enum`
- `description`
- `default` 应用

第一阶段不需要为了未来可能的工具过度实现。

### 17.4 风险：tool function 返回格式不统一

通过新增 `toolOutput` 解决。

工具函数统一返回：

```python
toolOutput(
    content='已读取文件：sample.txt',
    isError=False,
    details={'path': '/project/sample.txt', 'totalLines': 12},
)
```

executor 统一包装成：

```python
toolResult(
    toolCallId=call.id,
    toolName=definition.name,
    isError=output.isError,
    content=output.content,
    details=output.details,
)
```

---

## 18. 不建议的方案

### 18.1 不建议继续强化 YAML runtime

继续在 YAML 中增加：

```yaml
runtime:
  type: python
  function: flamingoAgents.tools.fileTools:readTool
```

看似灵活，但问题很多：

- 字符串 import 运行时才报错；
- IDE 无法可靠追踪；
- 安全边界更复杂；
- 配置继续承担实现职责；
- 工具函数和 schema 仍然割裂。

### 18.2 不建议第一阶段直接引入 LangChain

LangChain 很完整，但对当前项目来说太重。我们只需要吸收它的工具设计思想，不需要引入整个 Runnable/callback/Pydantic 体系。

### 18.3 不建议第一阶段做过度插件化

例如动态加载用户 Python 文件、热更新工具、远程工具市场等，都不适合第一阶段。

当前最重要的是先把内置工具的 callable 边界立起来。

---

## 19. 最终推荐

我推荐采用：

> **方案 A：Code-first Callable Tool Registry**

具体判断：

1. 它能直接解决当前 function call 没有具体调用函数的问题；
2. 它和 pi 的 `ToolDefinition.execute` 方向一致；
3. 它吸收 LangChain “函数即工具”的核心思想；
4. 它不会让项目引入重依赖；
5. 它能让新增工具变得非常明确；
6. 它保留当前 agent 状态机、confirmation、manualChecks 的大部分结构。

一句话版本：

```text
把工具从 YAML runtime 描述，改成 Python callable definition；
YAML 只保留 enabledTools 和 permissions；
agent 通过 registry 找 toolDefinition，再调用 definition.execute。
```

---

## 20. 审阅重点

请重点审阅以下决策：

1. 是否接受第一阶段不引入 LangChain / Pydantic；
2. 是否接受 `config/tools.yaml` 升级到 `version: 2`，不继续保留 runtime；
3. 是否接受新增 `toolOutput`，由 executor 包装成 `toolResult`；
4. 是否接受第一阶段只迁移 read/write/edit/bash，不做 custom tool 插件系统；
5. 是否接受后续第二阶段再做 `@tool` 装饰器和 schema 推断。

如果以上方向通过，下一步可以基于本文档编写正式实现计划。
