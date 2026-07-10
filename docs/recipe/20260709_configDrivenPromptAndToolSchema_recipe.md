# 配置驱动的系统提示词与工具 Schema

## 背景

当前系统提示词和工具 schema 均硬编码在 Python 中：

1. **系统提示词**写死在 `flamingoAgents/core/agent.py` 顶部的 `systemPrompt` 模块级字符串，通过 `getConversation()` 创建会话时传给 `conversation`。
2. **工具 schema**（`name` / `description` / `parameters`）写死在 `flamingoAgents/tools/builtinTools.py` 的各个工厂函数里；`config/tools.yaml`（version 2）只承载 `enabledTools` 白名单和 `toolPermissions`，不含 schema。

这导致声明式内容（提示词、schema）与可执行逻辑耦合在一起，调整提示词或工具参数都需要改 Python 代码。

## 目标

1. 系统提示词从 `config/systemPrompt.md` 读取，`agent` 不再持有硬编码常量。
2. 工具的 schema（`name` / `description` / `parameters`）与权限从 `config/tools.yaml` 读取；Python 端只保留 `name → (execute, preview)` 的可执行映射。
3. `config/tools.yaml` 升级为 version 3：所有工具 schema + 权限合到一个文件的 `tools` 数组中；声明的工具全部启用，删掉即禁用。
4. `createAgent` 增加 `systemPromptPath` 参数，与现有 `modelConfigPath` / `toolsConfigPath` 风格一致，默认指向 `config/systemPrompt.md`。

## 不在范围内

- 不引入 importlib 动态加载（YAML 里不写 execute 的模块路径），按工具名硬映射。
- 不保留 `enabledTools` 白名单（声明的工具全部启用）。
- `execute` / `preview` 等 Python 可调用逻辑不外置，仍写死在 `builtinTools.py`。
- 历史 `.jsonl` 会话日志不迁移（系统提示词变化后新会话自然用新内容）。

## 详细设计

### 文件 1：`config/systemPrompt.md`（新建）

内容 = 现 `agent.py` 顶部的 `systemPrompt` 字符串原文，作为纯 Markdown 文本保存。

### 文件 2：`config/tools.yaml`（version 2 → version 3，整体重写）

新结构：`tools` 数组，每项含 `name` / `description` / `parameters` / 可选 `permissions`。权限嵌套在工具下（取代旧的顶层 `toolPermissions` 按工具名分组）。声明的工具全部启用。

以 `read`（无权限）和 `bash`（带权限）为例：

```yaml
version: 3

tools:
  - name: read
    description: 读取本地文本文件，可通过 offset 和 limit 控制读取的行范围。
    parameters:
      type: object
      properties:
        path: { type: string }
        offset: { type: integer, minimum: 1, default: 1 }
        limit: { type: integer, minimum: 1, default: 2000 }
      required: [path]
      additionalProperties: false

  - name: write
    description: 创建或完整覆盖本地文本文件。
    parameters:
      type: object
      properties:
        path: { type: string }
        content: { type: string }
      required: [path, content]
      additionalProperties: false

  - name: edit
    description: 对已有文本文件进行精确文本替换。每个 oldText 必须唯一匹配。
    parameters:
      type: object
      properties:
        path: { type: string }
        edits:
          type: array
          minItems: 1
          items:
            type: object
            properties:
              oldText: { type: string }
              newText: { type: string }
            required: [oldText, newText]
            additionalProperties: false
      required: [path, edits]
      additionalProperties: false

  - name: bash
    description: 在工作目录中执行 bash 命令。curl、python、grep、open 均通过此工具执行。
      权限提示：删除类命令会请求用户确认。maxOutput 控制 stdout/stderr 保留字符数，默认 2000，-1 表示不截断。
    parameters:
      type: object
      properties:
        command: { type: string }
        timeout: { type: integer, minimum: 1, default: 30 }
        maxOutput: { type: integer, minimum: -1, default: 2000 }
      required: [command]
      additionalProperties: false
    permissions:
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

注：`description` 含换行时用 YAML 的缩进续行书写（见 bash 工具）。

### 文件 3：`flamingoAgents/tools/toolConfig.py`（1.1 → 1.2）

- `toolSettings` 结构重写：去掉 `enabledTools` 和 `permissionsByTool`，只保留 `toolSchemas: list[toolSchemaSpec]`。
- 新增轻量数据类 `toolSchemaSpec`，承载从 YAML 解析出的单个工具声明（schema + 内嵌权限）：

```python
@dataclass
class toolSchemaSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permissions: list[permissionRule]
```

```python
@dataclass
class toolSettings:
    toolSchemas: list[toolSchemaSpec]
```

- `parseToolSettings`：校验 `version == 3`；遍历 `tools` 数组，为每个工具项解析 `name` / `description` / `parameters` 以及内嵌的 `permissions`，组装成 `toolSchemaSpec`。
- `parsePermissions` 逻辑不变，只是权限来源从顶层 `toolPermissions[toolName]` 改为工具项内的 `permissions` 字段。

### 文件 4：`flamingoAgents/tools/builtinTools.py`（1.2 → 1.3）

- 删除各 `createReadTool` / `createWriteTool` / `createEditTool` / `createBashTool` 工厂函数（它们原本内嵌 schema）。
- 删除顶层 `createBuiltinTools(enabledTools, permissionsByTool, ...)` 旧签名。
- 保留所有 `execute` / `preview` 函数（`readTool` / `previewReadTool` / `writeTool` / `previewWriteTool` / `editTool` / `previewEditTool` / `bashTool` / `previewBashTool`）。
- 新增可执行映射表，按工具名查 execute/preview：

```python
executableMap: dict[str, tuple[toolExecuteFunction, toolPreviewFunction]] = {
    'read': (readTool, previewReadTool),
    'write': (writeTool, previewWriteTool),
    'edit': (editTool, previewEditTool),
    'bash': (bashTool, previewBashTool),
}
```

- 新 `createBuiltinTools(toolSchemas, debugConsole=...)` 签名：接收 `toolSchemaSpec` 列表，逐个按 `name` 去 `executableMap` 查 execute/preview，拼成 `toolDefinition`；查不到时报错 `未知工具实现：{name}`。

### 文件 5：`flamingoAgents/builder.py`（1.1 → 1.2）

- 新增默认路径常量 `defaultSystemPromptPath = <projectRoot>/config/systemPrompt.md`。
- `createAgent` 新增 `systemPromptPath: str | Path | None = None` 参数。
- 读取系统提示词文本：`systemPromptText = Path(systemPromptPath or defaultSystemPromptPath).read_text(encoding='utf-8')`，文件不存在则报错。
- 把 `systemPromptText` 传入 `agent` 构造。
- 调整对 `createBuiltinTools` 的调用：传 `settings.toolSchemas`（替代旧的 `enabledTools` + `permissionsByTool`）。

### 文件 6：`flamingoAgents/core/agent.py`（1.7 → 1.8）

- 删除模块级 `systemPrompt` 常量字符串。
- `agent.__init__` 新增 `systemPrompt: str` 参数并保存为实例字段 `self.systemPrompt`。
- `getConversation` 创建会话时改用 `self.systemPrompt`（替代原模块常量）。

## 数据流

```
config/systemPrompt.md ──► builder 读取 ──► agent(systemPrompt=...)
                                                 │
                                                 ▼
                                         getConversation → conversation(systemPrompt)

config/tools.yaml ──► toolConfig.loadToolSettings ──► toolSettings[toolSchemaSpec...]
                                                         │
                                                         ▼
                                builtinTools.createBuiltinTools(toolSchemas)
                                                         │ 按 name 查 executableMap
                                                         ▼
                                              toolDefinition[] ──► agent ──► toolRegistry
```

## 验收标准

1. `config/systemPrompt.md` 存在且内容与原 `agent.py` 中 `systemPrompt` 常量一致；删除该常量后，agent 用新内容初始化会话。
2. `config/tools.yaml` version=3，`tools` 数组含 read/write/edit/bash 四个工具的完整 schema；bash 带删除命令权限规则。
3. 运行 `askModel.py`（或等价调用）能正常装配 agent 并完成一次工具调用循环，模型收到的 tools schema 与 YAML 声明一致。
4. `builtinTools.py` 不再硬编码任何 `parameters` / `description`；仅保留 execute/preview 函数与映射表。
5. 在 `tools.yaml` 中删除某工具后，该工具不再注册；声明一个 `executableMap` 中没有实现的名字时，装配阶段报错。
6. `createAgent(systemPromptPath=<自定义路径>)` 能用自定义提示词覆盖默认。
