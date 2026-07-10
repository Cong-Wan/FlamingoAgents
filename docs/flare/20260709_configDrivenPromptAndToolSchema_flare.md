# 配置驱动的系统提示词与工具 Schema 实现计划

> **面向智能体工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 来逐任务实现此计划。步骤使用复选框（`- [ ]`）语法进行追踪。

**目标：** 将系统提示词与工具 schema 从 Python 硬编码迁移到 config 配置文件驱动，Python 端只保留 `name → (execute, preview)` 的可执行映射。

**架构：** `config/systemPrompt.md` 提供系统提示词文本；`config/tools.yaml`（version 3）的 `tools` 数组声明每个工具的 schema 与内嵌权限；`toolConfig.py` 解析为 `toolSchemaSpec` 列表；`builtinTools.py` 用 `executableMap` 按 name 拼装 `toolDefinition`；`builder.py` 读取提示词并整合装配。

**技术栈：** Python 3.12、PyYAML、uv 包管理；无测试框架，验证通过手动运行 + debug 打印完成。

---

## 文件结构

**新建：**
- `config/systemPrompt.md` — 系统提示词纯文本（从 `agent.py` 常量迁出）

**重写：**
- `config/tools.yaml` — version 3：`tools` 数组合并 schema + 内嵌权限（取代旧 version 2 的 enabledTools + toolPermissions）

**修改（Python）：**
- `flamingoAgents/tools/toolConfig.py`（1.1 → 1.2）— 解析 version 3，产出 `toolSettings(toolSchemas)`，新增 `toolSchemaSpec`
- `flamingoAgents/tools/builtinTools.py`（1.2 → 1.3）— 删除 4 个工厂函数，新增 `executableMap` 映射表，`createBuiltinTools` 改为按 schema 拼装
- `flamingoAgents/core/agent.py`（1.7 → 1.8）— 删除模块级 `systemPrompt` 常量，`__init__` 接收 `systemPrompt` 参数
- `flamingoAgents/builder.py`（1.1 → 1.2）— 读取 `systemPrompt.md`，调用新 `createBuiltinTools(toolSchemas)`，新增 `systemPromptPath` 参数

---

### Task 1: 创建配置数据文件

**目标：** 产出 `config/systemPrompt.md`（内容与原 `agent.py` 常量一致）和 `config/tools.yaml`（version 3，含 read/write/edit/bash 四个工具 schema，bash 带删除命令权限），可通过 PyYAML 正确解析。

**涉及的文件：**

- `config/systemPrompt.md` — 系统提示词纯文本
- `config/tools.yaml` — version 3 工具 schema + 权限配置

------

#### Step 1 — 实现

创建 `config/systemPrompt.md`，内容为原 `agent.py` 顶部 `systemPrompt` 常量的原文：

```
你是 Flamingo Agents。你可以正常聊天，也可以调用配置中声明的工具。联网查询只能通过 shell runtime 中的 curl 等简单 shell 命令完成。如果 curl 因反爬、登录墙、验证码、403 或空结果失败，你必须诚实说明失败，不尝试绕过。需要确认的工具调用必须等待宿主调用 continueConfirmation。
```

将 `config/tools.yaml` 完整重写为如下内容（version 3）。注意 bash 的 `description` 用双引号字符串并内联 `\n\n` 转义，以忠实保留原描述中的段落分隔：

```yaml
version: 3

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
          default: 2000
      required:
        - path
      additionalProperties: false

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

  - name: bash
    description: "在工作目录中执行 bash 命令。curl、python、grep、open 均通过此工具执行。\n\n权限提示：删除类命令会请求用户确认。maxOutput 控制 stdout/stderr 保留字符数，默认 2000，-1 表示不截断。"
    parameters:
      type: object
      properties:
        command:
          type: string
        timeout:
          type: integer
          minimum: 1
          default: 30
        maxOutput:
          type: integer
          minimum: -1
          default: 2000
      required:
        - command
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

------

#### Step 2 — 运行验证

```bash
$ cd /Users/wilbur/project/FlamingoAgents
$ uv run python -c "
import yaml
from pathlib import Path
c = yaml.safe_load(open('config/tools.yaml', encoding='utf-8'))
print('version', c['version'])
print('tools', [t['name'] for t in c['tools']])
bash = [t for t in c['tools'] if t['name']=='bash'][0]
print('bashPerms', len(bash['permissions']))
print('bashPermId', bash['permissions'][0]['id'])
print('bashDescHasNewlines', '\n\n' in bash['description'])
prompt = Path('config/systemPrompt.md').read_text(encoding='utf-8')
print('promptChars', len(prompt))
print('promptHead', prompt[:26])
"
```

预期输出（关键部分）：

```
version 3
tools ['read', 'write', 'edit', 'bash']
bashPerms 1
bashPermId deletionCommand
bashDescHasNewlines True
promptChars <正整数>
promptHead 你是 Flamingo Agents。你可以正常聊天
```

------

✅ **完成的标志：** PyYAML 成功解析 `tools.yaml`，version=3，4 个工具名正确，bash 带 1 条权限且 description 含段落分隔；`systemPrompt.md` 非空且以原提示词开头。

---

### Task 2: toolConfig.py 解析 version 3

**目标：** `toolConfig.py` 能解析 version 3 结构，产出 `toolSettings(toolSchemas)`，每个 `toolSchemaSpec` 含 name/description/parameters/permissions；加载全程输出 debug 日志。

**涉及的文件：**

- `flamingoAgents/tools/toolConfig.py` — 解析 version 3 工具 schema + 内嵌权限

------

#### Step 1 — 实现

将 `flamingoAgents/tools/toolConfig.py` 完整替换为：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-09
Description: Loads tool schemas (name/description/parameters) and embedded permission rules from a single YAML config (version 3). Schemas are declarative; executable handlers remain in builtinTools.py.
'''

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Pattern

import yaml

from flamingoAgents.tools.toolDefinition import permissionRule


@dataclass
class toolSchemaSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    permissions: list[permissionRule]


@dataclass
class toolSettings:
    toolSchemas: list[toolSchemaSpec]


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
    if version != 3:
        raise RuntimeError(f'工具配置 version 必须是 3，实际为：{version}')

    rawTools = rawConfig.get('tools')
    if not isinstance(rawTools, list) or not rawTools:
        raise RuntimeError('工具配置 tools 必须是非空数组。')

    toolSchemas: list[toolSchemaSpec] = []
    seenNames: set[str] = set()
    for index, rawTool in enumerate(rawTools):
        if not isinstance(rawTool, dict):
            raise RuntimeError(f'tools 第 {index + 1} 项必须是对象。')
        schema = parseToolSchema(rawTool, source, index + 1, debugConsole=debugConsole)
        if schema.name in seenNames:
            raise RuntimeError(f'工具名称重复：{schema.name}')
        seenNames.add(schema.name)
        toolSchemas.append(schema)
        if debugConsole:
            debugConsole.debug(
                f'解析工具 schema tool={schema.name} '
                f'paramKeys={",".join((schema.parameters.get("properties") or {}).keys())} '
                f'permissionCount={len(schema.permissions)}'
            )

    if debugConsole:
        debugConsole.debug(
            f'工具设置加载完成 toolCount={len(toolSchemas)} '
            f'tools={",".join(s.name for s in toolSchemas)}'
        )
    return toolSettings(toolSchemas=toolSchemas)


def parseToolSchema(rawTool: dict[str, Any], source: str, position: int, debugConsole=None) -> toolSchemaSpec:
    label = f'{source} tools[{position}]'
    name = readRequiredString(rawTool, 'name', label)
    description = readRequiredString(rawTool, 'description', label)
    parameters = rawTool.get('parameters')
    if not isinstance(parameters, dict):
        raise RuntimeError(f'{label} parameters 必须是对象。')
    if parameters.get('type') != 'object':
        raise RuntimeError(f'{label} parameters.type 必须是 object。')
    permissions = parsePermissions(name, rawTool.get('permissions'), label)
    return toolSchemaSpec(
        name=name,
        description=description,
        parameters=parameters,
        permissions=permissions,
    )


def parsePermissions(toolName: str, rawPermissions: Any, label: str) -> list[permissionRule]:
    if rawPermissions is None:
        return []
    if not isinstance(rawPermissions, list):
        raise RuntimeError(f'{label} permissions 必须是数组。')
    parsedRules: list[permissionRule] = []
    for index, rawRule in enumerate(rawPermissions):
        if not isinstance(rawRule, dict):
            raise RuntimeError(f'{label} permissions 第 {index + 1} 条必须是对象。')
        ruleId = readRequiredString(rawRule, 'id', f'{label} permission {index + 1}')
        fieldName = readRequiredString(rawRule, 'field', f'{label} permission {ruleId}')
        action = readRequiredString(rawRule, 'action', f'{label} permission {ruleId}')
        if action != 'requireApproval':
            raise RuntimeError(f'{label} permission {ruleId} action 不支持：{action}')
        reason = readRequiredString(rawRule, 'reason', f'{label} permission {ruleId}')
        rawMatch = rawRule.get('match')
        if not isinstance(rawMatch, dict) or rawMatch.get('type') != 'regex':
            raise RuntimeError(f'{label} permission {ruleId} 只支持 match.type=regex。')
        rawPatterns = rawMatch.get('patterns')
        if not isinstance(rawPatterns, list) or not rawPatterns:
            raise RuntimeError(f'{label} permission {ruleId} 缺少 regex patterns。')
        patterns: list[Pattern[str]] = []
        for patternIndex, patternText in enumerate(rawPatterns):
            if not isinstance(patternText, str) or not patternText:
                raise RuntimeError(f'{label} permission {ruleId} 第 {patternIndex + 1} 个 regex 必须是非空字符串。')
            try:
                patterns.append(re.compile(patternText, re.IGNORECASE))
            except re.error as error:
                raise RuntimeError(f'{label} permission {ruleId} regex 无法编译：{patternText}') from error
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

------

#### Step 2 — 运行验证

```bash
$ cd /Users/wilbur/project/FlamingoAgents
$ uv run python -c "
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.tools.toolConfig import loadToolSettings
s = loadToolSettings(debugConsole=debugConsole(True))
print('toolCount', len(s.toolSchemas))
for t in s.toolSchemas:
    print(t.name, 'paramKeys=', list((t.parameters.get('properties') or {}).keys()), 'perms=', len(t.permissions))
"
```

预期输出（关键部分，debug 行显示解析过程）：

```
[debug HH:MM:SS] 加载工具设置 path=.../config/tools.yaml
[debug HH:MM:SS] 解析工具 schema tool=read paramKeys=path,offset,limit permissionCount=0
[debug HH:MM:SS] 解析工具 schema tool=write paramKeys=path,content permissionCount=0
[debug HH:MM:SS] 解析工具 schema tool=edit paramKeys=path,edits permissionCount=0
[debug HH:MM:SS] 解析工具 schema tool=bash paramKeys=command,timeout,maxOutput permissionCount=1
[debug HH:MM:SS] 工具设置加载完成 toolCount=4 tools=read,write,edit,bash
toolCount 4
read paramKeys= ['path', 'offset', 'limit'] perms= 0
write paramKeys= ['path', 'content'] perms= 0
edit paramKeys= ['path', 'edits'] perms= 0
bash paramKeys= ['command', 'timeout', 'maxOutput'] perms= 1
```

------

✅ **完成的标志：** `loadToolSettings` 返回 4 个 toolSchemaSpec，每个含正确的 paramKeys；bash 含 1 条权限；debug 日志打印每个工具解析过程。

---

### Task 3: builtinTools.py 执行映射层

**目标：** `builtinTools.py` 不再硬编码任何 schema/description；保留所有 execute/preview 函数，新增 `executableMap` 映射表；新签名 `createBuiltinTools(toolSchemas)` 按 name 拼装 `toolDefinition`，找不到实现时报错。

**涉及的文件：**

- `flamingoAgents/tools/builtinTools.py` — 删工厂函数，加映射表，改 createBuiltinTools 签名

------

#### Step 1 — 实现

将 `flamingoAgents/tools/builtinTools.py` 完整替换为（保留原有 execute/preview 函数体不变，仅调整文件头、删除工厂函数、新增映射表与新 createBuiltinTools）：

```python
'''
Author: wilbur
Version: 1.3
Date: 2026-07-09
Description: Provides executable handlers (execute/preview) for built-in tools and a name-keyed registry mapping them to schema-driven tool definitions. Schemas and permissions come from config/tools.yaml.
'''

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

from flamingoAgents.core.types import toolContext, toolOutput
from flamingoAgents.tools.toolConfig import toolSchemaSpec
from flamingoAgents.tools.toolDefinition import defineTool, toolDefinition, toolExecuteFunction, toolPreviewFunction

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30


# --- read ---

def previewReadTool(arguments: dict[str, Any]) -> str:
    path = str(arguments.get('path', ''))
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    return f'{path} offset={offset} limit={limit}'


def readTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
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
    if context.debugConsole:
        context.debugConsole.debug(
            f'读取工具完成 path={path} totalLines={len(lines)} '
            f'returnedChars={len(selectedText)} truncated={truncated}'
        )
    return toolOutput(
        content=selectedText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': truncated,
        },
    )


# --- write ---

def previewWriteTool(arguments: dict[str, Any]) -> str:
    content = str(arguments.get('content', ''))
    return f"{arguments.get('path', '')} bytes={len(content.encode('utf-8'))}"


def writeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    content = str(arguments['content'])
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具开始 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具完成 path={path}')
    return toolOutput(
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
        },
    )


# --- edit ---

def previewEditTool(arguments: dict[str, Any]) -> str:
    edits = arguments.get('edits', [])
    editCount = len(edits) if isinstance(edits, list) else 0
    return f"{arguments.get('path', '')} edits={editCount}"


def editTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
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
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具完成 path={path} diffChars={len(diffText)}')
    return toolOutput(
        content=diffText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits)},
    )


# --- bash ---

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
    maxOutput = int(arguments.get('maxOutput', 2000))
    if context.debugConsole:
        context.debugConsole.debug(f'bash 工具开始 command={command} timeout={timeout} maxOutput={maxOutput} cwd={context.workDir}')

    def clip(text: str) -> tuple[str, bool]:
        if maxOutput < 0 or len(text) <= maxOutput:
            return text, False
        return text[:maxOutput] + '\n<truncated>', True

    try:
        completedProcess = subprocess.run(
            ['bash', '-lc', command],
            cwd=str(context.workDir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdoutText, stdoutTruncated = clip(completedProcess.stdout)
        stderrText, stderrTruncated = clip(completedProcess.stderr)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具完成 exitCode={completedProcess.returncode}')
        return toolOutput(
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutText}\n'
                f'stderr:\n{stderrText}'
            ),
            isError=completedProcess.returncode != 0,
            details={
                'command': command,
                'timeout': timeout,
                'maxOutput': maxOutput,
                'exitCode': completedProcess.returncode,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
    except subprocess.TimeoutExpired as error:
        stdoutText = decodeProcessText(error.stdout)
        stderrText = decodeProcessText(error.stderr)
        stdoutText, stdoutTruncated = clip(stdoutText)
        stderrText, stderrTruncated = clip(stderrText)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具超时 command={command} timeout={timeout}')
        return toolOutput(
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutText}\n'
                f'stderr:\n{stderrText}'
            ),
            isError=True,
            details={
                'command': command,
                'timeout': timeout,
                'maxOutput': maxOutput,
                'timeoutExpired': True,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )


def decodeProcessText(value: str | bytes | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return ''


# --- schema-driven assembly ---

executableMap: dict[str, tuple[toolExecuteFunction, toolPreviewFunction]] = {
    'read': (readTool, previewReadTool),
    'write': (writeTool, previewWriteTool),
    'edit': (editTool, previewEditTool),
    'bash': (bashTool, previewBashTool),
}


def createBuiltinTools(toolSchemas: list[toolSchemaSpec], debugConsole=None) -> list[toolDefinition]:
    definitions: list[toolDefinition] = []
    for schema in toolSchemas:
        handlers = executableMap.get(schema.name)
        if handlers is None:
            raise RuntimeError(f'未知工具实现：{schema.name}')
        execute, preview = handlers
        definition = defineTool(
            name=schema.name,
            description=schema.description,
            parameters=schema.parameters,
            execute=execute,
            permissions=schema.permissions,
            preview=preview,
        )
        definitions.append(definition)
        if debugConsole:
            debugConsole.debug(
                f'绑定工具实现 tool={schema.name} '
                f'permissions={len(schema.permissions)}'
            )
    if debugConsole:
        debugConsole.debug(f'工具定义装配完成 count={len(definitions)}')
    return definitions
```

------

#### Step 2 — 运行验证

```bash
$ cd /Users/wilbur/project/FlamingoAgents
$ uv run python -c "
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.tools.toolConfig import loadToolSettings, toolSchemaSpec
from flamingoAgents.tools.builtinTools import createBuiltinTools
s = loadToolSettings(debugConsole=debugConsole(True))
d = createBuiltinTools(s.toolSchemas, debugConsole=debugConsole(True))
print('definitions', len(d))
for t in d:
    print(t.name, 'hasExecute=', callable(t.execute), 'hasPreview=', callable(t.preview), 'perms=', len(t.permissions))
# 验证未实现工具报错
fake = toolSchemaSpec(name='nonexistent', description='x', parameters={'type':'object','properties':{},'additionalProperties':False}, permissions=[])
try:
    createBuiltinTools([fake])
    print('ERROR 未报错')
except RuntimeError as e:
    print('expectedError:', e)
"
```

预期输出（关键部分）：

```
[debug HH:MM:SS] 绑定工具实现 tool=read permissions=0
[debug HH:MM:SS] 绑定工具实现 tool=write permissions=0
[debug HH:MM:SS] 绑定工具实现 tool=edit permissions=0
[debug HH:MM:SS] 绑定工具实现 tool=bash permissions=1
[debug HH:MM:SS] 工具定义装配完成 count=4
definitions 4
read hasExecute= True hasPreview= True perms= 0
write hasExecute= True hasPreview= True perms= 0
edit hasExecute= True hasPreview= True perms= 0
bash hasExecute= True hasPreview= True perms= 1
expectedError: 未知工具实现：nonexistent
```

------

✅ **完成的标志：** 4 个 toolDefinition 装配成功且 execute/preview 均可调用，bash 带 1 条权限；声明未实现的工具名时抛 `RuntimeError: 未知工具实现：...`；builtinTools.py 中已无任何硬编码 parameters/description。

---

### Task 4: 装配整合（agent.py + builder.py）

**目标：** `agent` 不再持有硬编码系统提示词，从 `systemPrompt` 参数接收；`builder.createAgent` 读取 `config/systemPrompt.md`、用新 `createBuiltinTools(toolSchemas)` 装配工具、支持 `systemPromptPath` 覆盖；端到端装配成功并打印 debug 日志。

**涉及的文件：**

- `flamingoAgents/core/agent.py` — 删除模块级 systemPrompt 常量，`__init__` 加 systemPrompt 参数
- `flamingoAgents/builder.py` — 读取系统提示词，调用新 createBuiltinTools，新增 systemPromptPath 参数

------

#### Step 1 — 实现

修改 `flamingoAgents/core/agent.py`，两处编辑：

第一处（合并：删除模块级 `systemPrompt` 常量 + 升级文件头版本号 + 在 `__init__` 增加 `systemPrompt` 参数与字段）。⚠️ 注意真实文件在 `import buildModelTools` 与 `class agent:` 之间是 `空行 + 常量行 + 空行 + 空行`（常量尚在），不是两个空行；下面的 oldText 已按真实文件逐字节写出。将：

```python
'''
Author: wilbur
Version: 1.7
Date: 2026-07-09
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state. Model turns are logged as atomic events (systemMessage/userMessage/assistantMessage/toolResult) instead of full request/response payloads.
'''

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.ports import modelAdapterPort
from flamingoAgents.core.types import pendingConfirm, runResult, toolCall, toolContext, toolResult
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
```

改为：

```python
'''
Author: wilbur
Version: 1.8
Date: 2026-07-09
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state. System prompt is injected at construction; model turns are logged as atomic events (systemMessage/userMessage/assistantMessage/toolResult) instead of full request/response payloads.
'''

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.ports import modelAdapterPort
from flamingoAgents.core.types import pendingConfirm, runResult, toolCall, toolContext, toolResult
from flamingoAgents.tools.toolDefinition import toolDefinition
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRegistry import toolRegistry
from flamingoAgents.tools.toolRuntime import executeToolCall as executeCallableToolCall
from flamingoAgents.tools.toolSchema import buildModelTools


class agent:
    def __init__(
        self,
        modelAdapter: modelAdapterPort,
        toolDefinitions: list[toolDefinition],
        workDir: Path,
        logDir: Path,
        systemPrompt: str,
        debugConsole=None,
        maxModelSteps: int = 8,
    ):
        self.modelAdapter = modelAdapter
        self.toolRegistry = toolRegistry(toolDefinitions, debugConsole=debugConsole)
        self.workDir = workDir
        self.logDir = logDir
        self.systemPrompt = systemPrompt
        self.debugConsole = debugConsole
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversation] = {}
        self.sessionLocks: dict[str, RLock] = {}
        self.sessionLocksGuard = RLock()
```

第二处，`getConversation` 方法里创建会话处，把引用模块常量改为实例字段。将：

```python
            newConversation = conversation(
                sessionId=sessionId,
                logPath=logPath,
                systemPrompt=systemPrompt,
                debugConsole=self.debugConsole,
            )
```

改为：

```python
            newConversation = conversation(
                sessionId=sessionId,
                logPath=logPath,
                systemPrompt=self.systemPrompt,
                debugConsole=self.debugConsole,
            )
```

将 `flamingoAgents/builder.py` 完整替换为：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-09
Description: Pure-library assembly factory: resolves paths, loads model config/auth, system prompt, and schema-driven tools, then returns a ready-to-use agent.
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


defaultSystemPromptPath = Path(__file__).resolve().parents[1] / 'config' / 'systemPrompt.md'


def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    logDir: str | Path | None = None,
    modelConfigPath: str | Path | None = None,
    toolsConfigPath: str | Path | None = None,
    systemPromptPath: str | Path | None = None,
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
    definitions = createBuiltinTools(settings.toolSchemas, debugConsole=printer)
    resolvedSystemPromptPath = Path(systemPromptPath).resolve() if systemPromptPath else defaultSystemPromptPath
    if printer.isDebug:
        printer.debug(f'加载系统提示词 path={resolvedSystemPromptPath}')
    if not resolvedSystemPromptPath.exists():
        raise RuntimeError(f'系统提示词文件不存在：{resolvedSystemPromptPath}')
    systemPromptText = resolvedSystemPromptPath.read_text(encoding='utf-8')
    if printer.isDebug:
        printer.debug(f'系统提示词加载完成 chars={len(systemPromptText)}')
    return agent(
        modelAdapter=adapter,
        toolDefinitions=definitions,
        workDir=workDirPath,
        logDir=resolvedLogDir,
        systemPrompt=systemPromptText,
        debugConsole=printer,
    )
```

------

#### Step 2 — 运行验证

```bash
$ cd /Users/wilbur/project/FlamingoAgents
$ uv run python -c "
import tempfile, os
from flamingoAgents import createAgent
a = createAgent('.', debug=True)
print('agentType', type(a).__name__)
print('systemPromptChars', len(a.systemPrompt))
print('systemPromptHead', a.systemPrompt[:20])
print('toolCount', len(a.toolRegistry.list()))
# 验证 systemPromptPath 覆盖
tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False, encoding='utf-8')
tmp.write('自定义提示词测试')
tmp.close()
a2 = createAgent('.', debug=False, systemPromptPath=tmp.name)
print('customPrompt', repr(a2.systemPrompt))
os.unlink(tmp.name)
"
```

预期输出（关键部分）：

```
[debug HH:MM:SS] 装配 Agent workDir=.../FlamingoAgents logDir=.../.agentLogs providerId=101 modelId=None
[debug ...] 加载工具设置 path=.../config/tools.yaml
[debug ...] 解析工具 schema tool=read ... permissionCount=0
[debug ...] 解析工具 schema tool=write ... permissionCount=0
[debug ...] 解析工具 schema tool=edit ... permissionCount=0
[debug ...] 解析工具 schema tool=bash ... permissionCount=1
[debug ...] 工具设置加载完成 toolCount=4 tools=read,write,edit,bash
[debug ...] 绑定工具实现 tool=read permissions=0
[debug ...] 绑定工具实现 tool=write permissions=0
[debug ...] 绑定工具实现 tool=edit permissions=0
[debug ...] 绑定工具实现 tool=bash permissions=1
[debug ...] 工具定义装配完成 count=4
[debug ...] 加载系统提示词 path=.../config/systemPrompt.md
[debug ...] 系统提示词加载完成 chars=<正整数>
agentType agent
systemPromptChars <与 promptChars 一致的正整数>
systemPromptHead 你是 Flamingo Agents。你可以正常聊天
toolCount 4
customPrompt '自定义提示词测试'
```

（可选，若模型服务可用）端到端确认模型循环可触发工具 schema：

```bash
$ uv run python askModel.py
# 预期：debug 输出显示 agent 装配 + 模型循环开始，tools=4
```

------

✅ **完成的标志：** `createAgent('.', debug=True)` 成功返回 `agent` 实例，工具数 4，systemPrompt 来自 `config/systemPrompt.md` 且字符数与文件一致；`systemPromptPath` 覆盖生效（自定义内容被使用）；agent.py 中已无模块级 systemPrompt 常量。

---

## 自我复审

**1. 规范覆盖：** 逐条对照 recipe 验收标准——
- 系统提示词从 config 读取、删除常量 → Task 1 + Task 4 ✓
- tools.yaml version 3 含 4 工具 schema、bash 带权限 → Task 1 ✓
- 装配可完成工具调用循环 → Task 4 装配验证（端到端标为可选，依赖模型可用性）✓
- builtinTools 不硬编码 schema → Task 3 ✓
- 删除工具不注册、未实现工具报错 → Task 3 验证含报错检查；"删除即禁用"由 tools.yaml 决定，删除条目后 Task 2 自然不解析 ✓
- systemPromptPath 覆盖 → Task 4 验证含覆盖检查 ✓

**2. 占位符扫描：** 全文无 TODO/省略号，每个改动文件均为完整代码或精确替换块。✓

**3. 类型一致性：**
- `toolSchemaSpec`（Task 2 定义）→ Task 3 `createBuiltinTools(toolSchemas: list[toolSchemaSpec])`、`executableMap` 引用一致 ✓
- `toolSettings.toolSchemas`（Task 2）→ Task 4 builder `settings.toolSchemas` 一致 ✓
- `agent.__init__` 新增 `systemPrompt: str`（Task 4）→ builder 传 `systemPrompt=systemPromptText` 一致 ✓
- `toolExecuteFunction` / `toolPreviewFunction`（toolDefinition.py 已存在）→ Task 3 executableMap 类型注解引用一致 ✓

**4. 验证完整性：** 每个任务均有 `uv run python -c` 运行命令，含 debug 日志预期与关键输出断言；Task 3 含报错路径验证；Task 4 含覆盖路径验证。✓

---

## 执行交接

计划已完成并保存到 `docs/flare/20260709_configDrivenPromptAndToolSchema_flare.md`。两种执行选项：

**1. 子代理驱动（推荐）** —— 我为每个任务分派一个全新的子代理，在任务之间进行复审，快速迭代

**2. 内联执行** —— 使用 executing-plans 在本会话中执行任务，带复审检查点的批处理

选择哪种方式？
