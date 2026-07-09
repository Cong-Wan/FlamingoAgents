# Function Call Callable Registry 实现计划

> **面向智能体工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 来逐任务实现此计划。步骤使用复选框（`- [ ]`）语法进行追踪。

**目标：** 将 FlamingoAgents 的工具体系从 YAML runtime 分发改为 code-first callable registry，让每个工具定义直接绑定真实 Python callable，并补齐新增函数说明文档。

**架构：** 新增 `toolDefinition`、`toolRegistry`、`builtinTools` 三个清晰边界：工具定义负责 schema + callable，registry 负责名称查找，executor 只负责通用校验和包装。`config/tools.yaml` 升级为 `version: 2`，只保留启用工具和权限规则；agent 通过 registry 获取 definition 后调用 `definition.execute(arguments, context)`。

**技术栈：** Python 3.12+、dataclasses、PyYAML、OpenAI-compatible Chat Completions tools schema、项目现有 `uv` 环境。

---

## 调试输出约定

用户已选择“需要更详细的打印输出”。本计划中的实现必须满足：

- 所有新增或修改的运行时诊断输出都通过 `debugConsole.debug()` 输出。
- `manualChecks.py` 的可见检查输出只在传入 `--debug` 时打印。
- 不传 `--debug` 时，手动验证成功应保持静默并以退出码 0 表示成功。
- 传入 `--debug` 时，关键路径输出详细状态：配置加载、工具创建、registry 注册、权限评估、工具执行、agent 模型循环、手动验证进度。
- 禁止加入任何不受 `--debug` 控制的业务打印。

## 范围检查

本计划只覆盖一个子系统：function call 工具体系从 config runtime 分发迁移到 callable registry。它不引入 LangChain、不引入 Pydantic、不实现装饰器推断、不实现用户自定义插件加载。新增函数说明文档只说明在第一阶段架构下如何新增一个 callable tool。

## 文件结构

将创建或修改以下文件：

- `flamingoAgents/core/types.py` — 增加 `toolOutput`，作为具体工具函数的返回类型。
- `flamingoAgents/utils/debug.py` — 确保可见输出也受 debug 开关控制。
- `flamingoAgents/tools/toolConfig.py` — 从完整工具定义 loader 改为 `version: 2` tool settings loader，只解析 `enabledTools` 和 `toolPermissions`。
- `flamingoAgents/tools/toolDefinition.py` — 新增 callable tool definition 数据结构与 `defineTool()` helper。
- `flamingoAgents/tools/toolRegistry.py` — 新增工具 registry，保证工具名唯一并提供 `get()` / `list()`。
- `flamingoAgents/tools/builtinTools.py` — 新增 read/write/edit/bash 的 callable 实现、factory、preview 和安全路径解析。
- `flamingoAgents/tools/toolRuntime.py` — 改为通用 executor：参数校验、调用 callable、包装 `toolResult`。
- `flamingoAgents/tools/toolPolicy.py` — 适配新的 `toolDefinition.permissions`。
- `flamingoAgents/tools/toolSchema.py` — 从 callable `toolDefinition` 投影 OpenAI tools schema。
- `flamingoAgents/core/agent.py` — 用 `toolRegistry` 替代 dict runtime 分发，并使用 `definition.preview()` 生成确认预览。
- `flamingoAgents/builder.py` — 用 `loadToolSettings()` + `createBuiltinTools()` 装配 agent。
- `config/tools.yaml` — 升级为 `version: 2`，只保留启用工具与权限。
- `manualChecks.py` — 更新无测试框架手动验证，所有输出由 `--debug` 控制。
- `docs/addCallableToolFunction.md` — 新增“如何新增一个函数”的说明文档。

## 执行清单

- [ ] Task 1: 建立共享输出类型与 debug 输出基础设施
- [ ] Task 2: 建立 settings loader、callable definition 与 registry
- [ ] Task 3: 实现 read/write/edit/bash 内置 callable 工具
- [ ] Task 4: 改造 executor、policy、schema 为 callable definition 形态
- [ ] Task 5: 接入 agent、builder，并升级工具配置
- [ ] Task 6: 更新手动验证入口并跑通完整验证
- [ ] Task 7: 新增“如何新增一个函数”说明文档

---

### Task 1: 建立共享输出类型与 debug 输出基础设施

**目标：** 项目中存在统一的 `toolOutput` 返回类型，且可见诊断输出都受 debug 开关控制。

**涉及的文件：**

- `flamingoAgents/core/types.py` — 增加 callable tool 的业务输出数据结构。
- `flamingoAgents/utils/debug.py` — 修复现有 `visible()` 无条件 `print` 违反调试输出约定的问题：将 `visible()` 收紧为受 `isDebug` 门控。当前无调用者，作为受控可见输出预留入口；工具函数的诊断打印仍统一走 `debugConsole.debug()`。

------

#### Step 1 — 实现

- [ ] 将 `flamingoAgents/core/types.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.3
Date: 2026-07-08
Description: Defines shared lower-camel-case data structures for messages, tools, runtime context, confirmations, agent results, and callable tool outputs.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

messageRole = Literal['system', 'user', 'assistant', 'tool']
agentStatus = Literal['completed', 'confirmationRequired', 'error']


@dataclass
class toolCall:
    id: str
    toolName: str
    arguments: dict[str, Any]


@dataclass
class chatMessage:
    role: messageRole
    content: str
    toolCalls: list[toolCall] = field(default_factory=list)
    toolCallId: str | None = None
    name: str | None = None


@dataclass
class toolOutput:
    content: str
    isError: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class toolResult:
    toolCallId: str
    toolName: str
    isError: bool
    content: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class toolContext:
    workDir: Path
    debugConsole: Any | None = None


@dataclass
class runResult:
    sessionId: str
    status: agentStatus
    message: str = ''
    confirmationId: str | None = None
    reason: str | None = None
    commandPreview: str | None = None
    toolCall: toolCall | None = None


@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCalls: list[toolCall]
    currentIndex: int
```

- [ ] 将 `flamingoAgents/utils/debug.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-08
Description: Provides --debug controlled diagnostic printing for Flamingo Agents.
'''

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class debugConsole:
    isDebug: bool = False

    def debug(self, message: str) -> None:
        if self.isDebug:
            nowText = datetime.now().strftime('%H:%M:%S')
            print(f'[debug {nowText}] {message}', flush=True)

    def visible(self, message: str) -> None:
        if self.isDebug:
            print(message, flush=True)
```

------

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/core/types.py flamingoAgents/utils/debug.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python - <<'PY'
from flamingoAgents.core.types import toolOutput
from flamingoAgents.utils.debug import debugConsole

output = toolOutput(content='ok', details={'called': True})
assert output.content == 'ok'
assert output.isError is False
assert output.details == {'called': True}
printer = debugConsole(False)
assert printer.isDebug is False
PY
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复实现后重新运行本任务的两个命令。第二步验证通过前不要进入 Task 2。

------

✅ **完成的标志：** 第二步验证通过 —— 构建通过，运行无异常，关键断言全部成立。

------

### Task 2: 建立 settings loader、callable definition 与 registry

**目标：** 项目可以从 `version: 2` 工具配置中解析启用工具和权限，并能创建带 callable 字段的工具定义和唯一名称 registry。

**涉及的文件：**

- `flamingoAgents/tools/toolConfig.py` — 解析 `enabledTools` 与 `toolPermissions`。
- `flamingoAgents/tools/toolDefinition.py` — 定义 callable tool 的数据结构与 helper。
- `flamingoAgents/tools/toolRegistry.py` — 管理 `name -> toolDefinition` 映射。

------

#### Step 1 — 实现

- [ ] 将 `flamingoAgents/tools/toolConfig.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Loads callable tool settings and compiles runtime permission rules.
'''

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Pattern

import yaml

from flamingoAgents.tools.toolDefinition import permissionRule


@dataclass
class toolSettings:
    enabledTools: list[str]
    permissionsByTool: dict[str, list[permissionRule]] = field(default_factory=dict)


defaultToolsConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'tools.yaml'


def loadToolSettings(configPath: str | Path | None = None, debugConsole=None) -> toolSettings:
    path = Path(configPath) if configPath is not None else defaultToolsConfigPath
    if debugConsole:
        debugConsole.debug(f'加载工具设置 path={path}')
    if not path.exists():
        raise RuntimeError(f'工具配置文件不存在：{path}')
    with path.open('r', encoding='utf-8') as configFile:
        rawConfig = yaml.safe_load(configFile) or {}
    return parseToolSettings(rawConfig, source=str(path), debugConsole=debugConsole)


def parseToolSettings(rawConfig: Any, source: str = '<memory>', debugConsole=None) -> toolSettings:
    if not isinstance(rawConfig, dict):
        raise RuntimeError(f'工具配置必须是 YAML 对象：{source}')
    version = rawConfig.get('version')
    if version != 2:
        raise RuntimeError(f'工具配置 version 必须是 2，实际为：{version}')

    rawEnabledTools = rawConfig.get('enabledTools')
    if not isinstance(rawEnabledTools, list) or not rawEnabledTools:
        raise RuntimeError('工具配置 enabledTools 必须是非空数组。')

    enabledTools: list[str] = []
    seenTools: set[str] = set()
    for index, rawToolName in enumerate(rawEnabledTools):
        if not isinstance(rawToolName, str) or not rawToolName.strip():
            raise RuntimeError(f'enabledTools 第 {index + 1} 项必须是非空字符串。')
        toolName = rawToolName.strip()
        if toolName in seenTools:
            raise RuntimeError(f'启用工具名称重复：{toolName}')
        seenTools.add(toolName)
        enabledTools.append(toolName)

    rawPermissionsByTool = rawConfig.get('toolPermissions', {})
    if rawPermissionsByTool is None:
        rawPermissionsByTool = {}
    if not isinstance(rawPermissionsByTool, dict):
        raise RuntimeError('工具配置 toolPermissions 必须是对象。')

    permissionsByTool: dict[str, list[permissionRule]] = {}
    for rawToolName, rawPermissions in rawPermissionsByTool.items():
        if not isinstance(rawToolName, str) or not rawToolName.strip():
            raise RuntimeError('toolPermissions 的 key 必须是非空工具名。')
        toolName = rawToolName.strip()
        if toolName not in seenTools:
            raise RuntimeError(f'工具权限配置引用了未启用工具：{toolName}')
        permissionsByTool[toolName] = parsePermissions(toolName, rawPermissions)

    for toolName in enabledTools:
        permissionsByTool.setdefault(toolName, [])

    if debugConsole:
        debugConsole.debug(
            f'工具设置加载完成 enabledTools={",".join(enabledTools)} '
            f'permissionTools={",".join(sorted(permissionsByTool.keys()))}'
        )
    return toolSettings(enabledTools=enabledTools, permissionsByTool=permissionsByTool)


def parsePermissions(toolName: str, rawPermissions: Any) -> list[permissionRule]:
    if rawPermissions is None:
        return []
    if not isinstance(rawPermissions, list):
        raise RuntimeError(f'工具 {toolName} 的 permissions 必须是数组。')
    parsedRules: list[permissionRule] = []
    for index, rawRule in enumerate(rawPermissions):
        if not isinstance(rawRule, dict):
            raise RuntimeError(f'工具 {toolName} 的第 {index + 1} 条 permission 必须是对象。')
        ruleId = readRequiredString(rawRule, 'id', f'工具 {toolName} permission {index + 1}')
        fieldName = readRequiredString(rawRule, 'field', f'工具 {toolName} permission {ruleId}')
        action = readRequiredString(rawRule, 'action', f'工具 {toolName} permission {ruleId}')
        if action != 'requireApproval':
            raise RuntimeError(f'工具 {toolName} permission {ruleId} action 不支持：{action}')
        reason = readRequiredString(rawRule, 'reason', f'工具 {toolName} permission {ruleId}')
        rawMatch = rawRule.get('match')
        if not isinstance(rawMatch, dict) or rawMatch.get('type') != 'regex':
            raise RuntimeError(f'工具 {toolName} permission {ruleId} 只支持 match.type=regex。')
        rawPatterns = rawMatch.get('patterns')
        if not isinstance(rawPatterns, list) or not rawPatterns:
            raise RuntimeError(f'工具 {toolName} permission {ruleId} 缺少 regex patterns。')
        patterns: list[Pattern[str]] = []
        for patternIndex, patternText in enumerate(rawPatterns):
            if not isinstance(patternText, str) or not patternText:
                raise RuntimeError(f'工具 {toolName} permission {ruleId} 第 {patternIndex + 1} 个 regex 必须是非空字符串。')
            try:
                patterns.append(re.compile(patternText, re.IGNORECASE))
            except re.error as error:
                raise RuntimeError(f'工具 {toolName} permission {ruleId} regex 无法编译：{patternText}') from error
        parsedRules.append(permissionRule(
            id=ruleId,
            field=fieldName,
            action='requireApproval',
            reason=reason,
            patterns=patterns,
        ))
    return parsedRules


def readRequiredString(rawData: dict[str, Any], key: str, label: str) -> str:
    value = rawData.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f'{label} 缺少非空字符串字段：{key}')
    return value.strip()
```

- [ ] 新建 `flamingoAgents/tools/toolDefinition.py`，内容如下：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Defines callable tool metadata, permission rule types, execution signatures, and a lightweight defineTool helper.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Pattern

from flamingoAgents.core.types import toolContext, toolOutput

permissionAction = Literal['requireApproval']


@dataclass
class permissionRule:
    id: str
    field: str
    action: permissionAction
    reason: str
    patterns: list[Pattern[str]]


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
        permissions=list(permissions or []),
        prepareArguments=prepareArguments,
        preview=preview,
    )
```

- [ ] 新建 `flamingoAgents/tools/toolRegistry.py`，内容如下：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Provides a unique-name registry for callable tool definitions.
'''

from __future__ import annotations

from flamingoAgents.tools.toolDefinition import toolDefinition


class toolRegistry:
    def __init__(self, definitions: list[toolDefinition], debugConsole=None):
        self.definitions: dict[str, toolDefinition] = {}
        self.debugConsole = debugConsole
        for definition in definitions:
            self.register(definition)
        if self.debugConsole:
            self.debugConsole.debug(f'工具 registry 初始化完成 count={len(self.definitions)}')

    def register(self, definition: toolDefinition) -> None:
        if not definition.name.strip():
            raise RuntimeError('工具名称不能为空。')
        if definition.name in self.definitions:
            raise RuntimeError(f'工具名称重复：{definition.name}')
        self.definitions[definition.name] = definition
        if self.debugConsole:
            self.debugConsole.debug(f'注册工具 tool={definition.name}')

    def get(self, name: str) -> toolDefinition | None:
        return self.definitions.get(name)

    def list(self) -> list[toolDefinition]:
        return list(self.definitions.values())
```

------

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/tools/toolConfig.py flamingoAgents/tools/toolDefinition.py flamingoAgents/tools/toolRegistry.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python - <<'PY'
from flamingoAgents.core.types import toolOutput
from flamingoAgents.tools.toolConfig import parseToolSettings
from flamingoAgents.tools.toolDefinition import defineTool
from flamingoAgents.tools.toolRegistry import toolRegistry

settings = parseToolSettings({
    'version': 2,
    'enabledTools': ['sample'],
    'toolPermissions': {
        'sample': [{
            'id': 'danger',
            'field': 'command',
            'action': 'requireApproval',
            'reason': '危险操作需要确认',
            'match': {'type': 'regex', 'patterns': ['rm\\s+']},
        }],
    },
})
assert settings.enabledTools == ['sample']
assert len(settings.permissionsByTool['sample']) == 1

def sampleTool(arguments, context):
    return toolOutput(content='sample')

definition = defineTool(
    name='sample',
    description='示例工具',
    parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
    execute=sampleTool,
    permissions=settings.permissionsByTool['sample'],
)
registry = toolRegistry([definition])
assert registry.get('sample') is definition
assert registry.get('missing') is None
assert registry.list() == [definition]
PY
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复实现后重新运行本任务的两个命令。第二步验证通过前不要进入 Task 3。

------

✅ **完成的标志：** 第二步验证通过 —— `version: 2` settings 可解析，callable definition 可创建，registry 可按名称查询。

------

### Task 3: 实现 read/write/edit/bash 内置 callable 工具

**目标：** read/write/edit/bash 都以 callable tool definition 形式存在，且每个 definition 都绑定真实 Python 函数、schema、权限和预览函数。

**涉及的文件：**

- `flamingoAgents/tools/builtinTools.py` — 内置工具的 callable 实现与 factory。

------

#### Step 1 — 实现

- [ ] 新建 `flamingoAgents/tools/builtinTools.py`，内容如下：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Defines built-in callable tools for file read/write/edit and bash execution.
'''

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any, Callable

from flamingoAgents.core.types import toolContext, toolOutput
from flamingoAgents.tools.toolDefinition import defineTool, permissionRule, toolDefinition
from flamingoAgents.utils.preview import makePreview

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30


def createBuiltinTools(
    enabledTools: list[str],
    permissionsByTool: dict[str, list[permissionRule]],
    debugConsole=None,
) -> list[toolDefinition]:
    builtinFactories: dict[str, Callable[[list[permissionRule]], toolDefinition]] = {
        'read': createReadTool,
        'write': createWriteTool,
        'edit': createEditTool,
        'bash': createBashTool,
    }
    definitions: list[toolDefinition] = []
    for toolName in enabledTools:
        factory = builtinFactories.get(toolName)
        if factory is None:
            raise RuntimeError(f'未知内置工具：{toolName}')
        permissions = permissionsByTool.get(toolName, [])
        definition = factory(permissions)
        definitions.append(definition)
        if debugConsole:
            debugConsole.debug(f'创建内置工具 tool={toolName} permissions={len(permissions)}')
    return definitions


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


def previewReadTool(arguments: dict[str, Any]) -> str:
    path = str(arguments.get('path', ''))
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    return f'{path} offset={offset} limit={limit}'


def readTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    if context.debugConsole:
        context.debugConsole.debug(f'读取工具开始 path={path} offset={offset} limit={limit}')
    if offset < 1 or limit < 1:
        return toolOutput(content='read.offset 和 read.limit 必须大于 0。', isError=True)
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedText = ''.join(lines[startIndex:startIndex + limit])
    truncated = startIndex + limit < len(lines)
    previewText, previewTruncated = makePreview(selectedText)
    if context.debugConsole:
        context.debugConsole.debug(
            f'读取工具完成 path={path} totalLines={len(lines)} '
            f'returnedChars={len(previewText)} truncated={truncated or previewTruncated}'
        )
    return toolOutput(
        content=previewText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': truncated or previewTruncated,
        },
    )


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


def previewWriteTool(arguments: dict[str, Any]) -> str:
    content = str(arguments.get('content', ''))
    return f"{arguments.get('path', '')} bytes={len(content.encode('utf-8'))}"


def writeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
    content = str(arguments['content'])
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具开始 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    previewText, truncated = makePreview(content)
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具完成 path={path} truncated={truncated}')
    return toolOutput(
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
            'contentPreview': previewText,
            'truncated': truncated,
        },
    )


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


def previewEditTool(arguments: dict[str, Any]) -> str:
    edits = arguments.get('edits', [])
    editCount = len(edits) if isinstance(edits, list) else 0
    return f"{arguments.get('path', '')} edits={editCount}"


def editTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
    edits = arguments['edits']
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具开始 path={path} editCount={len(edits)}')
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
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具完成 path={path} diffChars={len(diffText)} truncated={truncated}')
    return toolOutput(
        content=previewText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits), 'diffTruncated': truncated},
    )


def createBashTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='bash',
        description='在工作目录中执行 bash 命令。curl、python、grep、open 均通过此工具执行。\n\n权限提示：删除类命令会请求用户确认。',
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
    if context.debugConsole:
        context.debugConsole.debug(f'bash 工具开始 command={command} timeout={timeout} cwd={context.workDir}')

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
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具完成 exitCode={completedProcess.returncode}')
        return toolOutput(
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
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
        stdoutText = decodeProcessText(error.stdout)
        stderrText = decodeProcessText(error.stderr)
        stdoutPreview, stdoutTruncated = makePreview(stdoutText)
        stderrPreview, stderrTruncated = makePreview(stderrText)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具超时 command={command} timeout={timeout}')
        return toolOutput(
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
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


def resolveSafePath(pathValue: str, workDir: Path) -> Path:
    if pathValue.strip().startswith('~'):
        raise ValueError(f'路径不能使用 ~：{pathValue}')
    rawPath = Path(pathValue)
    if rawPath.is_absolute():
        raise ValueError(f'路径必须是工作目录内的相对路径：{pathValue}')
    root = workDir.resolve()
    resolved = (root / rawPath).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f'路径超出工作目录：{pathValue}') from error
    return resolved


def decodeProcessText(value: str | bytes | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return ''
```

------

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/tools/builtinTools.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from flamingoAgents.core.types import toolContext
from flamingoAgents.tools.builtinTools import createBuiltinTools
from flamingoAgents.utils.debug import debugConsole

with TemporaryDirectory() as tempDir:
    workDir = Path(tempDir)
    definitions = createBuiltinTools(['read', 'write', 'edit', 'bash'], {}, debugConsole=debugConsole(False))
    byName = {definition.name: definition for definition in definitions}
    assert set(byName) == {'read', 'write', 'edit', 'bash'}
    assert all(callable(definition.execute) for definition in definitions)
    context = toolContext(workDir=workDir, debugConsole=debugConsole(False))
    writeOutput = byName['write'].execute({'path': 'sample.txt', 'content': 'alpha\nbeta\n'}, context)
    assert writeOutput.isError is False
    readOutput = byName['read'].execute({'path': 'sample.txt', 'offset': 1, 'limit': 1}, context)
    assert 'alpha' in readOutput.content
    editOutput = byName['edit'].execute({'path': 'sample.txt', 'edits': [{'oldText': 'beta', 'newText': 'gamma'}]}, context)
    assert editOutput.isError is False
    bashOutput = byName['bash'].execute({'command': 'printf ok', 'timeout': 5}, context)
    assert bashOutput.isError is False
    assert 'ok' in bashOutput.content
PY
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复实现后重新运行本任务的两个命令。第二步验证通过前不要进入 Task 4。

------

✅ **完成的标志：** 第二步验证通过 —— 四个内置工具均可创建，且 callable 真实执行成功。

------

### Task 4: 改造 executor、policy、schema 为 callable definition 形态

**目标：** 工具执行链路不再识别 `runtime.type` 或 `runtime.operation`，而是校验参数后直接调用 `definition.execute(arguments, context)` 并包装成 `toolResult`。

**涉及的文件：**

- `flamingoAgents/tools/toolRuntime.py` — 通用 callable executor 与 schema 子集校验。
- `flamingoAgents/tools/toolPolicy.py` — 权限判断使用新的 definition 类型。
- `flamingoAgents/tools/toolSchema.py` — 模型 schema 从 callable definition 投影。

------

#### Step 1 — 实现

- [ ] 将 `flamingoAgents/tools/toolRuntime.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Executes callable tool definitions through shared argument validation and toolResult wrapping.
'''

from __future__ import annotations

from typing import Any

from flamingoAgents.core.types import toolCall, toolContext, toolResult
from flamingoAgents.tools.toolDefinition import toolDefinition


def executeToolCall(definition: toolDefinition, call: toolCall, context: toolContext) -> toolResult:
    if context.debugConsole:
        context.debugConsole.debug(f'执行工具调用开始 tool={definition.name} callId={call.id}')
    arguments = call.arguments
    if not isinstance(arguments, dict):
        return toolResult(call.id, definition.name, True, 'toolCall.arguments 必须是对象。', {'invalidArguments': True})

    try:
        if definition.prepareArguments is not None:
            if context.debugConsole:
                context.debugConsole.debug(f'预处理工具参数 tool={definition.name} callId={call.id}')
            arguments = definition.prepareArguments(arguments)
            if not isinstance(arguments, dict):
                return toolResult(call.id, definition.name, True, '工具参数预处理结果必须是对象。', {'invalidPreparedArguments': True})
    except Exception as error:
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content=f'工具参数预处理异常：{type(error).__name__}: {error}',
            details={'exceptionType': type(error).__name__},
        )

    schemaError = validateArguments(definition.parameters, arguments)
    if schemaError:
        return toolResult(call.id, definition.name, True, f'工具参数不符合 schema：{schemaError}', {'schemaError': schemaError})

    try:
        if context.debugConsole:
            context.debugConsole.debug(f'调用工具函数 tool={definition.name} callId={call.id}')
        output = definition.execute(arguments, context)
        result = toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=output.isError,
            content=output.content,
            details=output.details,
        )
        if context.debugConsole:
            context.debugConsole.debug(f'执行工具调用完成 tool={definition.name} callId={call.id} isError={result.isError}')
        return result
    except Exception as error:
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content=f'工具执行异常：{type(error).__name__}: {error}',
            details={'exceptionType': type(error).__name__},
        )


def validateArguments(parameters: dict[str, Any], arguments: dict[str, Any]) -> str:
    return validateObject(parameters, arguments, 'arguments')


def validateObject(schema: dict[str, Any], value: Any, path: str) -> str:
    if schema.get('type') != 'object':
        return f'{path} schema.type 必须是 object'
    if not isinstance(value, dict):
        return f'{path} 必须是对象'

    properties = schema.get('properties') or {}
    if not isinstance(properties, dict):
        return f'{path}.properties 必须是对象'

    required = schema.get('required') or []
    if not isinstance(required, list):
        return f'{path}.required 必须是数组'
    for key in required:
        if key not in value:
            return f'{path}.{key} 是必填字段'

    if schema.get('additionalProperties') is False:
        allowedKeys = set(properties.keys())
        for key in value.keys():
            if key not in allowedKeys:
                return f'{path}.{key} 不允许出现'

    for key, itemValue in value.items():
        itemSchema = properties.get(key)
        if isinstance(itemSchema, dict):
            itemError = validateValue(itemSchema, itemValue, f'{path}.{key}')
            if itemError:
                return itemError
    return ''


def validateValue(schema: dict[str, Any], value: Any, path: str) -> str:
    expectedType = schema.get('type')
    if expectedType == 'string':
        if not isinstance(value, str):
            return f'{path} 必须是字符串'
        return ''
    if expectedType == 'integer':
        if not isinstance(value, int) or isinstance(value, bool):
            return f'{path} 必须是整数'
        minimum = schema.get('minimum')
        maximum = schema.get('maximum')
        if isinstance(minimum, int) and value < minimum:
            return f'{path} 必须大于等于 {minimum}'
        if isinstance(maximum, int) and value > maximum:
            return f'{path} 必须小于等于 {maximum}'
        return ''
    if expectedType == 'array':
        if not isinstance(value, list):
            return f'{path} 必须是数组'
        minItems = schema.get('minItems')
        if isinstance(minItems, int) and len(value) < minItems:
            return f'{path} 至少需要 {minItems} 项'
        itemSchema = schema.get('items')
        if isinstance(itemSchema, dict):
            for index, itemValue in enumerate(value):
                itemError = validateValue(itemSchema, itemValue, f'{path}[{index}]')
                if itemError:
                    return itemError
        return ''
    if expectedType == 'object':
        return validateObject(schema, value, path)
    return ''
```

- [ ] 将 `flamingoAgents/tools/toolPolicy.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Enforces callable tool permission rules before runtime execution.
'''

from __future__ import annotations

from dataclasses import dataclass

from flamingoAgents.core.types import toolCall
from flamingoAgents.tools.toolDefinition import toolDefinition


@dataclass
class policyDecision:
    requiresApproval: bool
    reason: str = ''
    permissionId: str = ''


def evaluateToolCall(definition: toolDefinition, call: toolCall, debugConsole=None) -> policyDecision:
    if debugConsole:
        debugConsole.debug(f'评估工具权限 tool={definition.name} callId={call.id} permissionCount={len(definition.permissions)}')
    for rule in definition.permissions:
        fieldValue = call.arguments.get(rule.field, '') if isinstance(call.arguments, dict) else ''
        textValue = str(fieldValue)
        for pattern in rule.patterns:
            if pattern.search(textValue):
                if debugConsole:
                    debugConsole.debug(f'工具权限命中 tool={definition.name} callId={call.id} permissionId={rule.id}')
                return policyDecision(
                    requiresApproval=True,
                    reason=rule.reason,
                    permissionId=rule.id,
                )
    return policyDecision(requiresApproval=False)
```

- [ ] 将 `flamingoAgents/tools/toolSchema.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Converts callable tool definitions into model function-call schemas.
'''

from __future__ import annotations

from typing import Any

from flamingoAgents.tools.toolDefinition import toolDefinition


def buildModelTool(definition: toolDefinition) -> dict[str, Any]:
    return {
        'type': 'function',
        'function': {
            'name': definition.name,
            'description': definition.description,
            'parameters': definition.parameters,
        },
    }


def buildModelTools(definitions: list[toolDefinition]) -> list[dict[str, Any]]:
    return [buildModelTool(definition) for definition in definitions]
```

------

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/tools/toolRuntime.py flamingoAgents/tools/toolPolicy.py flamingoAgents/tools/toolSchema.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from flamingoAgents.core.types import toolCall, toolContext, toolOutput
from flamingoAgents.tools.toolDefinition import defineTool
from flamingoAgents.tools.toolRuntime import executeToolCall
from flamingoAgents.tools.toolSchema import buildModelTools

with TemporaryDirectory() as tempDir:
    called = {'value': False}

    def sampleTool(arguments, context):
        called['value'] = True
        return toolOutput(content='hello ' + arguments['name'], details={'workDir': str(context.workDir)})

    definition = defineTool(
        name='sample',
        description='示例工具',
        parameters={
            'type': 'object',
            'properties': {'name': {'type': 'string'}},
            'required': ['name'],
            'additionalProperties': False,
        },
        execute=sampleTool,
    )
    context = toolContext(workDir=Path(tempDir))
    result = executeToolCall(definition, toolCall('call_1', 'sample', {'name': 'flamingo'}), context)
    assert result.isError is False
    assert result.content == 'hello flamingo'
    assert called['value'] is True

    schemaResult = executeToolCall(definition, toolCall('call_2', 'sample', {'name': 123}), context)
    assert schemaResult.isError is True
    assert 'schemaError' in schemaResult.details

    badArgumentsResult = executeToolCall(definition, toolCall('call_3', 'sample', []), context)
    assert badArgumentsResult.isError is True
    assert badArgumentsResult.details['invalidArguments'] is True

    modelTools = buildModelTools([definition])
    assert modelTools[0]['function']['name'] == 'sample'
    assert 'execute' not in modelTools[0]['function']
    assert 'permissions' not in modelTools[0]['function']
PY
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复实现后重新运行本任务的两个命令。第二步验证通过前不要进入 Task 5。

------

✅ **完成的标志：** 第二步验证通过 —— executor 能调用真实 callable，schema 错误能被包装，模型 schema 不泄漏运行时字段。

------

### Task 5: 接入 agent、builder，并升级工具配置

**目标：** `createAgent()` 能从 `version: 2` 配置装配 callable built-in tools，agent 运行时通过 registry 查找工具并执行 callable，确认预览由工具自己的 preview 生成。

**涉及的文件：**

- `flamingoAgents/core/agent.py` — agent 使用 `toolRegistry` 和 callable executor。
- `flamingoAgents/builder.py` — builder 装配 settings + builtin tools。
- `config/tools.yaml` — 升级到 `version: 2`。

------

#### Step 1 — 实现

**设计说明（确认预览字段名）：** 按 recipe §11.4 的兼容策略，`runResult` 继续保留 `commandPreview` 字段名（不改名为 `toolPreview`），但其内容不再由 `str(call.arguments)` 生成，而是由对应工具的 `definition.preview(call.arguments)` 经 `buildToolPreview()` 生成。这样既保持现有公开 API 字段名不变，又让确认提示展示真实命令摘要（例如 bash 显示真实 `command`、read 显示 `path offset limit`）。

- [ ] 将 `flamingoAgents/core/agent.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.5
Date: 2026-07-08
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state.
'''

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.ports import modelAdapterPort
from flamingoAgents.core.types import chatMessage, pendingConfirm, runResult, toolCall, toolContext, toolResult
from flamingoAgents.tools.toolDefinition import toolDefinition
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRegistry import toolRegistry
from flamingoAgents.tools.toolRuntime import executeToolCall as executeCallableToolCall
from flamingoAgents.tools.toolSchema import buildModelTools

systemPrompt = '''你是 Flamingo Agents。你可以正常聊天，也可以调用配置中声明的工具。联网查询只能通过 shell runtime 中的 curl 等简单 shell 命令完成。如果 curl 因反爬、登录墙、验证码、403 或空结果失败，你必须诚实说明失败，不尝试绕过。需要确认的工具调用必须等待宿主调用 continueConfirmation。'''


class agent:
    def __init__(
        self,
        modelAdapter: modelAdapterPort,
        toolDefinitions: list[toolDefinition],
        workDir: Path,
        logDir: Path,
        debugConsole=None,
        maxModelSteps: int = 8,
    ):
        self.modelAdapter = modelAdapter
        self.toolRegistry = toolRegistry(toolDefinitions, debugConsole=debugConsole)
        self.workDir = workDir
        self.logDir = logDir
        self.debugConsole = debugConsole
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversation] = {}
        self.sessionLocks: dict[str, RLock] = {}
        self.sessionLocksGuard = RLock()

    def runUserMessage(self, message: str, sessionId: str | None = None) -> runResult:
        cleanMessage = message.strip()
        realSessionId = sessionId or self.createSessionId()
        if not cleanMessage:
            return runResult(sessionId=realSessionId, status='error', message='消息不能为空。')
        with self.getSessionLock(realSessionId):
            if self.hasPendingConfirmation(realSessionId):
                return runResult(
                    sessionId=realSessionId,
                    status='error',
                    message='当前会话有待确认工具调用，请先调用 continueConfirmation。',
                )
            if self.debugConsole:
                self.debugConsole.debug(f'收到用户消息 sessionId={realSessionId} chars={len(cleanMessage)}')
            currentConversation = self.getConversation(realSessionId)
            currentConversation.addMessage(chatMessage(role='user', content=cleanMessage))
            return self.continueModelLoop(realSessionId)

    def continueConfirmation(self, sessionId: str, confirmationId: str, approved: bool) -> runResult:
        with self.getSessionLock(sessionId):
            currentConversation = self.getConversation(sessionId)
            pending = currentConversation.takePending()
            if pending is None or pending.confirmationId != confirmationId:
                if pending is not None:
                    currentConversation.setPending(pending)
                return runResult(sessionId=sessionId, status='error', message='确认请求不存在或 confirmationId 不匹配。')
            currentCall = pending.toolCalls[pending.currentIndex]
            if self.debugConsole:
                self.debugConsole.debug(
                    f'继续确认 sessionId={sessionId} confirmationId={confirmationId} '
                    f'approved={approved} tool={currentCall.toolName} callId={currentCall.id}'
                )
            if approved:
                result = self.executeToolCall(currentCall)
            else:
                result = self.buildBlockedToolResult(currentCall, pending.reason)
            currentConversation.addToolResult(result)
            batchResult = self.processToolBatch(sessionId, pending.toolCalls, pending.currentIndex + 1)
            if batchResult is not None:
                return batchResult
            return self.continueModelLoop(sessionId)

    def continueModelLoop(self, sessionId: str) -> runResult:
        currentConversation = self.getConversation(sessionId)
        for stepIndex in range(self.maxModelSteps):
            modelTools = buildModelTools(self.toolRegistry.list())
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(currentConversation.messages)} tools={len(modelTools)}'
                )
            try:
                completion = self.modelAdapter.complete(currentConversation.messages, modelTools)
            except Exception as error:
                self.logModelError(currentConversation, error)
                return runResult(sessionId=sessionId, status='error', message=f'模型调用失败：{error}')

            requestPayload = getattr(completion, 'requestPayload', None)
            responsePayload = getattr(completion, 'responsePayload', None)
            if isinstance(requestPayload, dict):
                currentConversation.logger.logEvent({'type': 'modelRequest', 'request': requestPayload})
            if isinstance(responsePayload, dict):
                currentConversation.logger.logEvent({'type': 'modelResponse', 'response': responsePayload})

            assistantMessage = completion.message
            currentConversation.addMessage(assistantMessage)
            if not assistantMessage.toolCalls:
                if self.debugConsole:
                    self.debugConsole.debug(f'模型循环完成 sessionId={sessionId} contentChars={len(assistantMessage.content)}')
                return runResult(sessionId=sessionId, status='completed', message=assistantMessage.content)

            batchResult = self.processToolBatch(sessionId, assistantMessage.toolCalls, 0)
            if batchResult is not None:
                return batchResult

        return runResult(
            sessionId=sessionId,
            status='error',
            message=f'模型循环超过最大步数：{self.maxModelSteps}',
        )

    def processToolBatch(self, sessionId: str, toolCalls: list[toolCall], startIndex: int) -> runResult | None:
        currentConversation = self.getConversation(sessionId)
        for index in range(startIndex, len(toolCalls)):
            call = toolCalls[index]
            definition = self.toolRegistry.get(call.toolName)
            if definition is None:
                currentConversation.addToolResult(self.makeUnknownToolResult(call))
                continue
            decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
            if decision.requiresApproval:
                confirmationId = 'confirm_' + uuid4().hex[:12]
                currentConversation.setPending(pendingConfirm(
                    sessionId=sessionId,
                    confirmationId=confirmationId,
                    reason=decision.reason,
                    toolCalls=toolCalls,
                    currentIndex=index,
                ))
                if self.debugConsole:
                    self.debugConsole.debug(
                        f'工具需要确认 sessionId={sessionId} confirmationId={confirmationId} '
                        f'tool={call.toolName} callId={call.id} permissionId={decision.permissionId}'
                    )
                return runResult(
                    sessionId=sessionId,
                    status='confirmationRequired',
                    confirmationId=confirmationId,
                    reason=decision.reason,
                    commandPreview=self.buildToolPreview(definition, call),
                    toolCall=call,
                )
            result = self.executeToolCall(call)
            currentConversation.addToolResult(result)
        return None

    def executeToolCall(self, call: toolCall) -> toolResult:
        definition = self.toolRegistry.get(call.toolName)
        if definition is None:
            return self.makeUnknownToolResult(call)
        context = toolContext(workDir=self.workDir, debugConsole=self.debugConsole)
        return executeCallableToolCall(definition, call, context)

    def buildToolPreview(self, definition: toolDefinition, call: toolCall) -> str:
        if definition.preview is not None and isinstance(call.arguments, dict):
            try:
                preview = definition.preview(call.arguments)
                if preview:
                    return preview
            except Exception as error:
                if self.debugConsole:
                    self.debugConsole.debug(
                        f'工具预览生成失败 tool={definition.name} callId={call.id} '
                        f'error={type(error).__name__}: {error}'
                    )
        return str(call.arguments)

    def makeUnknownToolResult(self, call: toolCall) -> toolResult:
        return toolResult(
            toolCallId=call.id,
            toolName=call.toolName,
            isError=True,
            content=f'未知工具：{call.toolName}',
            details={'unknownTool': True},
        )

    def buildBlockedToolResult(self, call: toolCall, reason: str) -> toolResult:
        return toolResult(
            toolCallId=call.id,
            toolName=call.toolName,
            isError=True,
            content=f'命令已被用户拒绝：{reason}。',
            details={'blocked': True, 'reason': 'userRejectedApproval'},
        )

    def logModelError(self, currentConversation: conversation, error: Exception) -> None:
        event: dict[str, Any] = {
            'type': 'modelError',
            'errorType': type(error).__name__,
            'message': str(error),
        }
        requestPayload = getattr(error, 'requestPayload', None)
        if isinstance(requestPayload, dict):
            event['request'] = requestPayload
        statusCode = getattr(error, 'statusCode', None)
        if isinstance(statusCode, int):
            event['status'] = statusCode
        currentConversation.logger.logEvent(event)

    def hasPendingConfirmation(self, sessionId: str) -> bool:
        with self.sessionLocksGuard:
            conversation = self.conversations.get(sessionId)
        if conversation is None:
            return False
        return conversation.hasPending()

    def getSessionLock(self, sessionId: str) -> RLock:
        with self.sessionLocksGuard:
            lock = self.sessionLocks.get(sessionId)
            if lock is None:
                lock = RLock()
                self.sessionLocks[sessionId] = lock
            return lock

    def getConversation(self, sessionId: str) -> conversation:
        with self.sessionLocksGuard:
            existing = self.conversations.get(sessionId)
            if existing is not None:
                return existing
            dateText = datetime.now().strftime('%Y%m%d')
            logPath = self.logDir / f'{dateText}_{sessionId}.jsonl'
            newConversation = conversation(sessionId=sessionId, logPath=logPath, systemPrompt=systemPrompt)
            self.conversations[sessionId] = newConversation
            return newConversation

    def createSessionId(self) -> str:
        return 'session_' + uuid4().hex[:12]
```

- [ ] 将 `flamingoAgents/builder.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Pure-library assembly factory: resolves paths, loads model config/auth and callable tools, and returns a ready-to-use agent.
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfig
from flamingoAgents.tools.builtinTools import createBuiltinTools
from flamingoAgents.tools.toolConfig import loadToolSettings
from flamingoAgents.utils.debug import debugConsole


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
    workDirPath = Path(workDir).resolve()
    printer = debugConsole(debug)
    resolvedLogDir = Path(logDir).resolve() if logDir else workDirPath / '.agentLogs'
    if printer.isDebug:
        printer.debug(f'装配 Agent workDir={workDirPath} logDir={resolvedLogDir} providerId={providerId} modelId={modelId}')
    resolved = loadModelConfig(
        providerId=providerId,
        modelId=modelId,
        configPath=modelConfigPath,
        debugConsole=printer,
    )
    auth = createModelAuth(resolved.apiKey)
    adapter = chatCompletionsAdapter(resolved.config, auth, debugConsole=printer)
    settings = loadToolSettings(configPath=toolsConfigPath, debugConsole=printer)
    definitions = createBuiltinTools(settings.enabledTools, settings.permissionsByTool, debugConsole=printer)
    return agent(
        modelAdapter=adapter,
        toolDefinitions=definitions,
        workDir=workDirPath,
        logDir=resolvedLogDir,
        debugConsole=printer,
    )
```

- [ ] 将 `config/tools.yaml` 完整替换为以下内容：

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

------

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/core/agent.py flamingoAgents/builder.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python - <<'PY'
from pathlib import Path

from flamingoAgents import createAgent
from flamingoAgents.tools.toolConfig import loadToolSettings

settings = loadToolSettings()
assert settings.enabledTools == ['read', 'write', 'edit', 'bash']
assert len(settings.permissionsByTool['bash']) == 1
builtAgent = createAgent(Path('.'), debug=False)
assert type(builtAgent).__name__ == 'agent'
assert builtAgent.toolRegistry.get('read') is not None
assert builtAgent.toolRegistry.get('bash') is not None
PY
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复实现后重新运行本任务的两个命令。第二步验证通过前不要进入 Task 6。

------

✅ **完成的标志：** 第二步验证通过 —— 默认工具配置是 v2，`createAgent()` 可完成 callable tool 装配，agent 内部存在 registry。

------

### Task 6: 更新手动验证入口并跑通完整验证

**目标：** `manualChecks.py` 能用 `--debug` 输出详细检查路径，并验证 callable registry、executor、内置工具、权限、agent 状态机和纯库 API 全部运行正常。

**涉及的文件：**

- `manualChecks.py` — 更新为 callable registry 版本的无测试框架手动验证入口。

------

#### Step 1 — 实现

- [ ] 将 `manualChecks.py` 完整替换为以下内容：

```python
'''
Author: wilbur
Version: 2.1
Date: 2026-07-08
Description: Framework-free manual validation entrypoint for callable tool registry runtime, with --debug controlled output.
'''

from __future__ import annotations

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import chatMessage, toolCall, toolContext, toolOutput
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter, modelCompletion
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfigFromYaml, modelConfig
from flamingoAgents.tools.builtinTools import createBuiltinTools
from flamingoAgents.tools.toolConfig import loadToolSettings
from flamingoAgents.tools.toolDefinition import defineTool
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRuntime import executeToolCall
from flamingoAgents.tools.toolSchema import buildModelTools
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.utils.jsonl import jsonlLog


class fakeModel:
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> modelCompletion:
        last = messages[-1]
        if last.role == 'user':
            if 'read sample' in last.content:
                return modelCompletion(
                    message=chatMessage(role='assistant', content='', toolCalls=[toolCall('call_read', 'read', {'path': 'sample.txt'})]),
                    requestPayload={},
                    responsePayload={},
                )
            if 'delete sample' in last.content:
                return modelCompletion(
                    message=chatMessage(role='assistant', content='', toolCalls=[toolCall('call_delete', 'bash', {'command': 'rm sample.txt'})]),
                    requestPayload={},
                    responsePayload={},
                )
            if 'batch' in last.content:
                return modelCompletion(
                    message=chatMessage(role='assistant', content='', toolCalls=[
                        toolCall('c1', 'read', {'path': 'sample.txt'}),
                        toolCall('c2', 'bash', {'command': 'rm sample.txt'}),
                        toolCall('c3', 'read', {'path': 'sample.txt'}),
                    ]),
                    requestPayload={},
                    responsePayload={},
                )
        if last.role == 'tool' and 'alpha sample' in last.content:
            return modelCompletion(
                message=chatMessage(role='assistant', content='sample content: alpha sample'),
                requestPayload={},
                responsePayload={},
            )
        return modelCompletion(message=chatMessage(role='assistant', content='done'), requestPayload={}, responsePayload={})


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def printPass(debugEnabled: bool, name: str) -> None:
    if debugEnabled:
        print(f'PASS {name}', flush=True)


def printDebug(debugEnabled: bool, message: str) -> None:
    if debugEnabled:
        print(f'[manual debug] {message}', flush=True)


def loadDefinitions(debugEnabled: bool):
    printer = debugConsole(debugEnabled)
    settings = loadToolSettings(debugConsole=printer)
    return createBuiltinTools(settings.enabledTools, settings.permissionsByTool, debugConsole=printer)


def byName(definitions, name):
    return next(definition for definition in definitions if definition.name == name)


def runToolConfigCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 callable tool settings / schema 检查')
    definitions = loadDefinitions(debugEnabled)
    expect({definition.name for definition in definitions} == {'read', 'write', 'edit', 'bash'}, '工具名集合不正确')
    expect(all(callable(definition.execute) for definition in definitions), '每个工具必须有 callable execute')
    modelTools = buildModelTools(definitions)
    expect(modelTools[0]['type'] == 'function', '模型工具 schema 包装不正确')
    expect(all(set(tool['function'].keys()) == {'name', 'description', 'parameters'} for tool in modelTools), '模型 schema 泄漏了运行时字段')
    printPass(debugEnabled, 'tool config')


def runPermissionCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 permission policy 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadDefinitions(debugEnabled)
    bashDefinition = byName(definitions, 'bash')
    readDefinition = byName(definitions, 'read')
    expect(evaluateToolCall(bashDefinition, toolCall('a', 'bash', {'command': 'rm file'}), debugConsole=printer).requiresApproval is True, 'rm 未触发确认')
    expect(evaluateToolCall(bashDefinition, toolCall('b', 'bash', {'command': 'grep keyword file'}), debugConsole=printer).requiresApproval is False, 'grep 被误判')
    expect(evaluateToolCall(bashDefinition, toolCall('c', 'bash', {'command': 'find . -delete'}), debugConsole=printer).requiresApproval is True, 'find -delete 未触发确认')
    expect(evaluateToolCall(readDefinition, toolCall('d', 'read', {'path': 'sample.txt'}), debugConsole=printer).requiresApproval is False, 'read 不应触发确认')
    printPass(debugEnabled, 'permission policy')


def runExecutorCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 callable executor 检查')
    printer = debugConsole(debugEnabled)
    with TemporaryDirectory() as tempDir:
        context = toolContext(workDir=Path(tempDir), debugConsole=printer)
        called = {'value': False}

        def sampleTool(arguments, context):
            called['value'] = True
            return toolOutput(content='hello ' + arguments['name'], details={'called': True})

        sampleDefinition = defineTool(
            name='sample',
            description='示例工具',
            parameters={
                'type': 'object',
                'properties': {'name': {'type': 'string'}},
                'required': ['name'],
                'additionalProperties': False,
            },
            execute=sampleTool,
        )
        result = executeToolCall(sampleDefinition, toolCall('call_sample', 'sample', {'name': 'registry'}), context)
        expect(not result.isError and result.content == 'hello registry', result.content)
        expect(called['value'] is True, '真实 callable 没有被调用')
        badArgumentsResult = executeToolCall(sampleDefinition, toolCall('call_bad_arguments', 'sample', []), context)
        expect(badArgumentsResult.isError and badArgumentsResult.details.get('invalidArguments') is True, '非对象 arguments 未被拒绝')
        badSchemaResult = executeToolCall(sampleDefinition, toolCall('call_bad_schema', 'sample', {'name': 1}), context)
        expect(badSchemaResult.isError and 'schemaError' in badSchemaResult.details, 'schema 错误未被拒绝')

        def explodingTool(arguments, context):
            raise ValueError('boom')

        explodingDefinition = defineTool(
            name='explode',
            description='异常工具',
            parameters={'type': 'object', 'properties': {}, 'additionalProperties': False},
            execute=explodingTool,
        )
        exceptionResult = executeToolCall(explodingDefinition, toolCall('call_exception', 'explode', {}), context)
        expect(exceptionResult.isError and exceptionResult.details.get('exceptionType') == 'ValueError', 'callable 异常未被包装')
    printPass(debugEnabled, 'callable executor')


def runToolRuntimeCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 built-in callable tools 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadDefinitions(debugEnabled)
    with TemporaryDirectory() as tempDir:
        context = toolContext(workDir=Path(tempDir), debugConsole=printer)
        writeDefinition = byName(definitions, 'write')
        readDefinition = byName(definitions, 'read')
        editDefinition = byName(definitions, 'edit')
        bashDefinition = byName(definitions, 'bash')

        writeResult = executeToolCall(writeDefinition, toolCall('call_write', 'write', {'path': 'sample.txt', 'content': 'alpha\nbeta\n'}), context)
        expect(not writeResult.isError, writeResult.content)
        readResult = executeToolCall(readDefinition, toolCall('call_read', 'read', {'path': 'sample.txt', 'offset': 1, 'limit': 1}), context)
        expect('alpha' in readResult.content, readResult.content)
        editResult = executeToolCall(editDefinition, toolCall('call_edit', 'edit', {'path': 'sample.txt', 'edits': [{'oldText': 'beta', 'newText': 'gamma'}]}), context)
        expect(not editResult.isError, editResult.content)

        for escapePath in ['../outside.txt', '/tmp/outside.txt', '~/secret.txt']:
            escapeResult = executeToolCall(readDefinition, toolCall('call_escape', 'read', {'path': escapePath}), context)
            expect(escapeResult.isError, f'路径逃逸没有被阻止：{escapePath}')

        bashResult = executeToolCall(bashDefinition, toolCall('call_bash', 'bash', {'command': 'printf hello', 'timeout': 5}), context)
        expect(not bashResult.isError and 'hello' in bashResult.content, bashResult.content)
        failResult = executeToolCall(bashDefinition, toolCall('call_fail', 'bash', {'command': 'exit 7', 'timeout': 5}), context)
        expect(failResult.isError and failResult.details.get('exitCode') == 7, '非零退出码未被标记为错误')
        timeoutResult = executeToolCall(bashDefinition, toolCall('call_timeout', 'bash', {'command': 'sleep 2', 'timeout': 1}), context)
        expect(timeoutResult.isError and timeoutResult.details.get('timeoutExpired') is True, '超时未被捕获')
        clampedResult = executeToolCall(bashDefinition, toolCall('call_clamp', 'bash', {'command': 'printf clamp', 'timeout': 999}), context)
        expect(not clampedResult.isError and clampedResult.details.get('timeout') == 120, 'timeout 未被限制到 120')
    printPass(debugEnabled, 'built-in callable tools')


def runLoggerCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 jsonl logger 检查')
    with TemporaryDirectory() as tempDir:
        logPath = Path(tempDir) / 'agent.jsonl'
        logger = jsonlLog(logPath)
        logger.logEvent({'type': 'sample', 'token': 'sk-12345678901234567890', 'content': 'x' * 4100})
        logText = logPath.read_text(encoding='utf-8')
        expect('<redacted>' in logText, 'secret 未脱敏')
        expect('12345678901234567890' not in logText, 'secret 原文泄露')
    printPass(debugEnabled, 'jsonl logger')


def runAdapterParseCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 adapter parse 检查')
    adapter = chatCompletionsAdapter(
        modelConfig('manual-provider', 'manual-model', 'http://127.0.0.1:9/v1', 'openaiCompatible'),
        createModelAuth('manual-key'),
    )
    parsed = adapter.parseAssistantPayload({
        'choices': [{
            'message': {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': 'call_1',
                    'type': 'function',
                    'function': {'name': 'read', 'arguments': '{"path":"sample.txt"}'},
                }],
            },
        }],
    })
    expect(parsed.toolCalls[0].toolName == 'read', 'tool_call name 解析失败')
    expect(parsed.toolCalls[0].arguments['path'] == 'sample.txt', 'tool_call arguments 解析失败')
    for badArguments in ['[]', '"abc"', '{bad json']:
        try:
            adapter.parseAssistantPayload({
                'choices': [{
                    'message': {
                        'role': 'assistant',
                        'content': '',
                        'tool_calls': [{
                            'id': 'call_bad',
                            'type': 'function',
                            'function': {'name': 'read', 'arguments': badArguments},
                        }],
                    },
                }],
            })
            raise RuntimeError('非法 arguments 没有被拒绝')
        except RuntimeError:
            pass
    printPass(debugEnabled, 'adapter parse')


def runModelAuthCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 model config / auth 检查')
    with TemporaryDirectory() as tempDir:
        inlinePath = Path(tempDir) / 'inline.yaml'
        inlinePath.write_text(
            'providers:\n'
            '  "abc":\n'
            '    baseUrl: http://127.0.0.1:9/v1\n'
            '    api: openai-completions\n'
            '    apiKey: inline-key\n'
            '    models:\n'
            '      - id: model-a\n',
            encoding='utf-8')
        before = os.environ.get('FLAMINGO_AGENTS_ABC_API_KEY')
        resolved = loadModelConfigFromYaml(providerId='abc', configPath=inlinePath, debugConsole=debugConsole(debugEnabled))
        after = os.environ.get('FLAMINGO_AGENTS_ABC_API_KEY')
        expect(resolved.apiKey == 'inline-key', 'inline apiKey 解析失败')
        expect(before == after, '配置加载不应写 os.environ')

        os.environ['TEST_API_KEY'] = 'env-secret'
        envPath = Path(tempDir) / 'env.yaml'
        envPath.write_text(
            'providers:\n'
            '  "envp":\n'
            '    baseUrl: http://127.0.0.1:9/v1\n'
            '    api: openai-completions\n'
            '    apiKey: ${TEST_API_KEY}\n'
            '    models:\n'
            '      - id: model-b\n',
            encoding='utf-8')
        envResolved = loadModelConfigFromYaml(providerId='envp', configPath=envPath)
        expect(envResolved.apiKey == 'env-secret', '${ENV} apiKey 解析失败')

        missingPath = Path(tempDir) / 'missing.yaml'
        missingPath.write_text(
            'providers:\n'
            '  "missp":\n'
            '    baseUrl: http://127.0.0.1:9/v1\n'
            '    api: openai-completions\n'
            '    apiKey: ${MISSING_KEY_NOT_SET}\n'
            '    models:\n'
            '      - id: model-c\n',
            encoding='utf-8')
        try:
            loadModelConfigFromYaml(providerId='missp', configPath=missingPath)
            raise RuntimeError('缺失环境变量没有被拒绝')
        except RuntimeError:
            pass

    auth = createModelAuth('abc123')
    expect(auth.authorizationHeader == 'Bearer abc123', 'Authorization header 生成失败')
    sourceText = Path('flamingoAgents/models/chatCompletions.py').read_text(encoding='utf-8')
    expect('os.getenv' not in sourceText, 'adapter 不应包含 os.getenv')
    expect('jsonlLog' not in sourceText, 'adapter 不应依赖 jsonlLog')
    printPass(debugEnabled, 'model config auth adapter')


def buildFakeAgent(workDir: Path, debugEnabled: bool) -> agent:
    return agent(
        modelAdapter=fakeModel(),
        toolDefinitions=loadDefinitions(debugEnabled),
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugConsole=debugConsole(debugEnabled),
    )


def runAgentStateCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 agent 状态机检查')
    with TemporaryDirectory() as tempDir:
        workDir = Path(tempDir)
        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        testAgent = buildFakeAgent(workDir, debugEnabled)

        readResult = testAgent.runUserMessage('please read sample', sessionId='readSession')
        expect(readResult.status == 'completed', readResult.message)
        expect('alpha sample' in readResult.message, readResult.message)

        confirmResult = testAgent.runUserMessage('please delete sample', sessionId='deleteSession')
        expect(confirmResult.status == 'confirmationRequired', confirmResult.message)
        expect(confirmResult.commandPreview == 'rm sample.txt', '确认预览应显示真实 bash command')
        expect((workDir / 'sample.txt').exists(), '需要确认时不应执行 rm')

        rejectResult = testAgent.continueConfirmation('deleteSession', confirmResult.confirmationId or '', approved=False)
        expect(rejectResult.status == 'completed', rejectResult.message)
        expect((workDir / 'sample.txt').exists(), '拒绝删除后文件不应消失')

        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        batchResult = testAgent.runUserMessage('batch', sessionId='batchSession')
        expect(batchResult.status == 'confirmationRequired', batchResult.message)

        pendingNewMessage = testAgent.runUserMessage('again', sessionId='batchSession')
        expect(pendingNewMessage.status == 'error', 'pending 期间新消息应被拒绝')

        wrongSession = testAgent.continueConfirmation('wrongSession', batchResult.confirmationId or '', approved=True)
        expect(wrongSession.status == 'error', '错误 sessionId 不应消费 pending')
        expect(testAgent.hasPendingConfirmation('batchSession'), '错误 sessionId 不应清掉真实 pending')

        approvedBatch = testAgent.continueConfirmation('batchSession', batchResult.confirmationId or '', approved=True)
        expect(approvedBatch.status == 'completed', approvedBatch.message)
    printPass(debugEnabled, 'agent state machine')


def runPureLibraryApiCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始纯库 API 检查')
    from flamingoAgents import createAgent

    builtAgent = createAgent(Path('.'), debug=debugEnabled)
    expect(type(builtAgent).__name__ == 'agent', 'createAgent 未返回 agent')
    expect(builtAgent.toolRegistry.get('read') is not None, 'createAgent 未装配 read 工具')
    expect(not Path('flamingoAgents/app').exists(), 'app 目录仍然存在')
    pyproject = Path('pyproject.toml').read_text(encoding='utf-8')
    expect('[project.scripts]' not in pyproject, 'pyproject 仍包含命令入口')
    manualSource = Path('manualChecks.py').read_text(encoding='utf-8')
    appLayerNeedle = 'flamingoAgents' + '.app'
    expect(appLayerNeedle not in manualSource, 'manualChecks 仍依赖 app 层')
    printPass(debugEnabled, 'pure library api')


def main() -> None:
    parser = argparse.ArgumentParser(description='运行无测试框架的手动验证')
    parser.add_argument('check', choices=[
        'all', 'toolConfig', 'permission', 'executor', 'runtime', 'logger', 'adapter', 'modelAuth', 'agent', 'pureLibrary',
    ])
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    args = parser.parse_args()

    if args.check in {'all', 'toolConfig'}:
        runToolConfigCheck(args.debug)
    if args.check in {'all', 'permission'}:
        runPermissionCheck(args.debug)
    if args.check in {'all', 'executor'}:
        runExecutorCheck(args.debug)
    if args.check in {'all', 'runtime'}:
        runToolRuntimeCheck(args.debug)
    if args.check in {'all', 'logger'}:
        runLoggerCheck(args.debug)
    if args.check in {'all', 'adapter'}:
        runAdapterParseCheck(args.debug)
    if args.check in {'all', 'modelAuth'}:
        runModelAuthCheck(args.debug)
    if args.check in {'all', 'agent'}:
        runAgentStateCheck(args.debug)
    if args.check in {'all', 'pureLibrary'}:
        runPureLibraryApiCheck(args.debug)


if __name__ == '__main__':
    main()
```

------

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile manualChecks.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python manualChecks.py all --debug
# 预期：构建通过，运行无异常，输出包含以下关键行：
# [manual debug] 开始 callable tool settings / schema 检查
# PASS tool config
# PASS permission policy
# PASS callable executor
# PASS built-in callable tools
# PASS jsonl logger
# PASS adapter parse
# PASS model config auth adapter
# PASS agent state machine
# PASS pure library api
```

```bash
$ uv run python manualChecks.py all
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复实现后重新运行本任务的三个命令。第二步验证通过前不要进入 Task 7。

------

✅ **完成的标志：** 第二步验证通过 —— 手动验证在 `--debug` 下输出详细路径，在无 `--debug` 下静默成功。

------

### Task 7: 新增“如何新增一个函数”说明文档

**目标：** `docs/` 下存在一份可交给后续开发者阅读的新增函数说明，开发者能按文档新增一个 callable tool 并完成手动验证。

**涉及的文件：**

- `docs/addCallableToolFunction.md` — 说明新增 callable 工具函数的步骤、代码位置、权限配置和验证命令。

------

#### Step 1 — 实现

- [ ] 新建 `docs/addCallableToolFunction.md`，内容如下：

````markdown
# 新增 Callable Tool 函数说明

本文说明在 FlamingoAgents 的 callable tool registry 架构下，如何新增一个可被模型 function call 调用的函数。

## 适用范围

本文面向“新增一个工具函数”的场景，例如新增 `currentTime`、`listFiles`、`fetchUrl` 这类模型可调用函数。

第一阶段架构保持轻量：

- 不引入 LangChain。
- 不引入 Pydantic。
- 不使用自动 schema 推断。
- 不使用测试框架。
- 不通过 YAML 描述 runtime 实现。

新增工具的核心路径是：

```text
写 Python callable
  -> 写 createXTool factory
  -> 注册到 createBuiltinTools 的 factory map
  -> 在 config/tools.yaml 的 enabledTools 中启用
  -> 如需权限确认，在 toolPermissions 中配置规则
  -> 用 manualChecks.py --debug 做手动验证
```

## 文件职责

- `flamingoAgents/tools/builtinTools.py`：内置工具函数、preview 函数、factory、factory map。
- `flamingoAgents/tools/toolDefinition.py`：`toolDefinition`、`defineTool()` 和 `permissionRule` 权限规则类型。
- `flamingoAgents/tools/toolRuntime.py`：通用 executor，负责校验参数、调用 `definition.execute()`、包装 `toolResult`。
- `flamingoAgents/tools/toolConfig.py`：只解析 `enabledTools` 和 `toolPermissions`，从 `toolDefinition` 复用 `permissionRule`。
- `config/tools.yaml`：只决定启用哪些工具，以及哪些工具需要权限确认。
- `manualChecks.py`：无测试框架的手动验证入口，输出由 `--debug` 控制。

## 命名与文件头要求

新增 Python 代码必须遵守：

- 函数名使用小驼峰，例如 `currentTimeTool`、`createCurrentTimeTool`、`previewCurrentTimeTool`。
- 如果新增代码文件，文件名使用小驼峰，例如 `dateTools.py`。
- 新建代码文件开头必须包含文件头：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Defines callable tools for date and time operations.
'''
```

如果只是修改已有文件，需要更新文件头版本号的小版本和 description。

## 新增函数步骤

### 1. 写真实执行函数

工具函数签名必须是：

```python
def currentTimeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    ...
```

规则：

- `arguments` 是模型传入的 JSON object。
- `context.workDir` 是当前工作目录。
- `context.debugConsole` 用于 `--debug` 下的详细日志。
- 函数返回 `toolOutput`，不要直接返回 `toolResult`。
- 不要在工具函数里处理 `toolCallId`，executor 会统一包装。

示例：

```python
from datetime import datetime, timezone
from typing import Any

from flamingoAgents.core.types import toolContext, toolOutput


def currentTimeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    timezoneName = str(arguments.get('timezone', 'utc'))
    if context.debugConsole:
        context.debugConsole.debug(f'currentTime 工具开始 timezone={timezoneName}')
    if timezoneName != 'utc':
        return toolOutput(content='currentTime.timezone 第一阶段只支持 utc。', isError=True)
    nowText = datetime.now(timezone.utc).isoformat(timespec='seconds')
    if context.debugConsole:
        context.debugConsole.debug(f'currentTime 工具完成 value={nowText}')
    return toolOutput(
        content=nowText,
        details={'timezone': timezoneName},
    )
```

### 2. 写确认预览函数

preview 用于权限确认界面或日志中展示更清晰的调用摘要。

```python
def previewCurrentTimeTool(arguments: dict[str, Any]) -> str:
    return 'timezone=' + str(arguments.get('timezone', 'utc'))
```

如果工具永远不需要权限确认，也建议写 preview，因为它能让后续调试更清楚。

### 3. 写 createXTool factory

factory 把函数、schema、description、permissions、preview 绑定成一个 `toolDefinition`。

```python
def createCurrentTimeTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='currentTime',
        description='获取当前 UTC 时间，返回 ISO 8601 字符串。',
        parameters={
            'type': 'object',
            'properties': {
                'timezone': {'type': 'string', 'default': 'utc'},
            },
            'required': [],
            'additionalProperties': False,
        },
        execute=currentTimeTool,
        permissions=permissions or [],
        preview=previewCurrentTimeTool,
    )
```

schema 约束：

- 顶层必须是 `type: object`。
- 明确写 `properties`。
- 明确写 `required`。
- 建议写 `additionalProperties: False`，避免模型传入无关参数。
- 第一阶段 validator 支持 `string`、`integer`、`array`、`object`。

### 4. 注册到内置工具 factory map

在 `flamingoAgents/tools/builtinTools.py` 的 `createBuiltinTools()` 中加入映射：

```python
builtinFactories: dict[str, Callable[[list[permissionRule]], toolDefinition]] = {
    'read': createReadTool,
    'write': createWriteTool,
    'edit': createEditTool,
    'bash': createBashTool,
    'currentTime': createCurrentTimeTool,
}
```

注册后，registry 才能通过工具名找到 definition。

### 5. 在配置中启用工具

修改 `config/tools.yaml`：

```yaml
version: 2

enabledTools:
  - read
  - write
  - edit
  - bash
  - currentTime

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
```

如果新工具不需要权限确认，不需要在 `toolPermissions` 下新增该工具。

### 6. 如需权限确认，添加 permission rule

例如工具有 `url` 字段，访问内网地址需要确认：

```yaml
toolPermissions:
  fetchUrl:
    - id: localNetworkUrl
      field: url
      action: requireApproval
      reason: 访问内网地址需要用户确认
      match:
        type: regex
        patterns:
          - '^https?://(127\.0\.0\.1|localhost|192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)'
```

规则说明：

- `field` 必须对应工具 arguments 中的字段名。
- `action` 第一阶段只支持 `requireApproval`。
- `match.type` 第一阶段只支持 `regex`。
- regex 会以 `re.IGNORECASE` 编译。

### 7. 更新 manualChecks.py

为新工具新增一个手动检查函数，并把它接入 `all`。输出必须受 `--debug` 控制。

示例检查逻辑：

```python
def runCurrentTimeToolCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 currentTime 工具检查')
    printer = debugConsole(debugEnabled)
    definitions = loadDefinitions(debugEnabled)
    currentTimeDefinition = byName(definitions, 'currentTime')
    with TemporaryDirectory() as tempDir:
        context = toolContext(workDir=Path(tempDir), debugConsole=printer)
        result = executeToolCall(
            currentTimeDefinition,
            toolCall('call_current_time', 'currentTime', {'timezone': 'utc'}),
            context,
        )
        expect(not result.isError, result.content)
        expect('T' in result.content, 'currentTime 应返回 ISO 8601 时间字符串')
    printPass(debugEnabled, 'currentTime tool')
```

然后在 `main()` 的 choices 和 `all` 分支中接入 `currentTime` 检查。

### 8. 运行手动验证

先做语法编译：

```bash
uv run python -m py_compile flamingoAgents/tools/builtinTools.py manualChecks.py
```

再运行详细验证：

```bash
uv run python manualChecks.py all --debug
```

最后确认无 debug 时成功静默：

```bash
uv run python manualChecks.py all
```

成功标准：

- `--debug` 下看到新工具检查的 `PASS currentTime tool`。
- 无 `--debug` 时命令无输出且退出码为 0。
- `buildModelTools()` 输出中只包含 `name`、`description`、`parameters`，不包含 `execute`、`permissions`、`preview`。
- 触发权限的调用会返回 `confirmationRequired`，不会提前执行真实函数。

## 常见错误

### 错误 1：只写函数，没有注册 factory map

现象：模型调用时返回 `未知工具：currentTime`。

修复：确认 `createBuiltinTools()` 的 `builtinFactories` 中包含：

```python
'currentTime': createCurrentTimeTool,
```

### 错误 2：配置启用了工具，但 Python 没有对应 factory

现象：`createAgent()` 或 `loadDefinitions()` 抛出 `未知内置工具：currentTime`。

修复：补齐 `createCurrentTimeTool()` 并注册到 factory map。

### 错误 3：schema required 与函数读取字段不一致

现象：executor 返回 `工具参数不符合 schema`，或函数内部 KeyError 被包装成 `工具执行异常`。

修复：让 schema、函数读取字段和 preview 使用完全相同的字段名。

### 错误 4：直接返回字符串

现象：executor 包装时出现 `AttributeError`。

修复：工具函数必须返回 `toolOutput`：

```python
return toolOutput(content='结果文本', details={'key': 'value'})
```

### 错误 5：直接 print 调试信息

现象：无 `--debug` 时仍然有输出。

修复：使用：

```python
if context.debugConsole:
    context.debugConsole.debug('调试信息')
```

不要直接调用 `print()`。

## 最小新增清单

- [ ] 真实函数：`currentTimeTool(arguments, context) -> toolOutput`
- [ ] 预览函数：`previewCurrentTimeTool(arguments) -> str`
- [ ] factory：`createCurrentTimeTool(permissions=None) -> toolDefinition`
- [ ] 注册：`'currentTime': createCurrentTimeTool`
- [ ] 配置：`enabledTools` 加入 `currentTime`
- [ ] 权限：需要确认时在 `toolPermissions.currentTime` 中配置规则
- [ ] 手动验证：`manualChecks.py` 加入新工具检查
- [ ] 命令：`uv run python manualChecks.py all --debug`
````

------

#### Step 2 — 运行验证

```bash
$ test -f docs/addCallableToolFunction.md && grep -q "createBuiltinTools" docs/addCallableToolFunction.md && grep -q "uv run python manualChecks.py all --debug" docs/addCallableToolFunction.md
# 预期：构建步骤不适用，运行无异常，命令无输出且退出码为 0。
```

如果验证不通过，修复文档后重新运行本任务的命令。

------

✅ **完成的标志：** 第二步验证通过 —— 文档存在，并明确说明 factory 注册与 `--debug` 手动验证命令。

------

## 最终总验证

完成全部任务后运行：

```bash
$ uv run python -m py_compile flamingoAgents/core/types.py flamingoAgents/utils/debug.py flamingoAgents/tools/toolConfig.py flamingoAgents/tools/toolDefinition.py flamingoAgents/tools/toolRegistry.py flamingoAgents/tools/builtinTools.py flamingoAgents/tools/toolRuntime.py flamingoAgents/tools/toolPolicy.py flamingoAgents/tools/toolSchema.py flamingoAgents/core/agent.py flamingoAgents/builder.py manualChecks.py
# 预期：构建通过，运行无异常，命令无输出且退出码为 0。
```

```bash
$ uv run python manualChecks.py all --debug
# 预期：运行无异常，输出包含所有 PASS 行，并包含配置加载、工具创建、registry 注册、权限评估、工具执行、agent 模型循环的 debug 日志。
```

```bash
$ uv run python manualChecks.py all
# 预期：运行无异常，命令无输出且退出码为 0。
```

## 自我复审结果

- 规范覆盖：已覆盖 recipe 中的 callable definition、toolOutput、toolRegistry、executor、权限保留、preview、v2 config、agent 接入、manualChecks 验证和新增函数文档。
- 占位内容扫描：计划内所有代码块均为完整文件内容，没有待补实现描述。
- 类型一致性：统一使用 `toolDefinition`、`toolOutput`、`toolRegistry`、`executeToolCall()`、`loadToolSettings()`、`createBuiltinTools()`。
- 验证完整性：每个任务都有明确命令，成功标准包含构建通过、运行无异常、关键行为符合预期。
