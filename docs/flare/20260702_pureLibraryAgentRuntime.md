# FlamingoAgents 纯库 Agent Runtime 实现计划

> **面向智能体工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 来逐任务实现此计划。步骤使用复选框（`- [ ]`）语法进行追踪。

**目标：** 将 FlamingoAgents 改造成没有内置 CLI/HTTP、工具配置驱动、权限由 runtime 强制、confirmation 状态机可靠、模型 auth 与 adapter 分离的纯 Python library。

**架构：** 以 `builder.py/createAgent()` 作为唯一公开组合根；core 只负责编排会话、模型循环和 confirmation 状态机；tools 由 `config/tools.yaml` 声明并由 `toolConfig/toolPolicy/toolRuntime/toolSchema` 执行；models 将配置、auth 和 chat completions adapter 分离。验证统一通过 `manualChecks.py` 的普通函数、`expect()` 和 `RuntimeError` 完成，不引入任何测试依赖。

**技术栈：** Python 3.12、uv、PyYAML、urllib、subprocess、threading、JSONL 日志、无框架手动验证。

---

## 文件结构

本计划创建或修改以下文件：

```plain
config/tools.yaml
  声明 read/write/edit/bash 四个工具、参数 schema、runtime 和 requireApproval 权限规则。

flamingoAgents/builder.py
  纯库组合根：解析 workDir/logDir/debug，加载模型配置、auth、tools，返回可用 agent。

flamingoAgents/__init__.py
  包根只 re-export createAgent 和 packageVersion。

flamingoAgents/core/types.py
  定义 chatMessage、toolCall、toolResult、toolContext、runResult、pendingConfirm 等共享数据结构。

flamingoAgents/core/ports.py
  定义 core 依赖的 model/debug 协议，避免 core 依赖具体实现。

flamingoAgents/core/conversation.py
  维护会话消息并记录 JSONL 事件，改为使用 utils.preview。

flamingoAgents/core/agent.py
  纯 Agent：模型循环、工具批处理、confirmationRequired、pending 游标、session lock。

flamingoAgents/models/modelConfig.py
  读取 config/models.yaml 或环境变量，返回 resolvedModelConfig，不写 os.environ。

flamingoAgents/models/modelAuth.py
  把 apiKey 变成 Authorization header。

flamingoAgents/models/chatCompletions.py
  OpenAI-compatible chat completions adapter，不读取环境变量，不依赖 jsonlLog。

flamingoAgents/tools/toolConfig.py
  加载并校验 config/tools.yaml，编译 permission regex。

flamingoAgents/tools/toolSchema.py
  把 toolDefinition 转成模型 function-call schema。

flamingoAgents/tools/toolPolicy.py
  根据 toolDefinition.permissions 强制判断 requireApproval。

flamingoAgents/tools/toolRuntime.py
  通用 file/shell runtime，包含手写参数校验、workDir 沙箱、bash timeout。

flamingoAgents/utils/redaction.py
  脱敏规则。

flamingoAgents/utils/preview.py
  预览截断与 JSON-safe 转换。

flamingoAgents/utils/jsonl.py
  只负责 JSONL 写入。

manualChecks.py
  无框架主验证入口，包含详细 --debug 输出。

pyproject.toml
  删除内置命令入口，保留库包配置。
```

删除以下旧入口或旧边界文件：

```plain
flamingoAgents/app/cli.py
flamingoAgents/app/server.py
flamingoAgents/app/__init__.py
flamingoAgents/app/
flamingoAgents/tools/guard.py
flamingoAgents/tools/registry.py
flamingoAgents/tools/router.py
flamingoAgents/tools/file.py
flamingoAgents/tools/bash.py
flamingoAgents/models/registry.py
```

任务采用自底向上顺序：先建好 utils / tools / models 新模块（每个任务自带独立验证，不依赖旧 agent 链路），再切换 core Agent 与公开 builder 并删除入口，最后重写 manualChecks 并清理旧模块。这样每个任务的验证都能独立通过，最终态干净。

**关于中间态（Task 4 之前整包不可 import 是预期的）：** Task 1 把 `makePreview`/`redactText`/`toJsonable` 从 `utils.jsonl` 移到 `utils.preview`/`utils.redaction`，但旧的 `tools/file.py`、`tools/bash.py`、`core/agent.py`、`models/chatCompletions.py` 仍从 `utils.jsonl` 导入它们；同时 `core/types.py` 的旧 `modelConfig`/`toolSpec` 要到 Task 4 才移除。因此 **Task 1 ~ Task 3 期间 `import flamingoAgents` 及现有 `manualChecks.py` 会处于 ImportError 状态，这是预期的中间态**，期间不要用整包 import 或旧 `manualChecks.py` 做 sanity check，只能运行各任务自带的内联验证命令。整包在 Task 4 切换 agent/builder、Task 5 重写 manualChecks 后恢复可用，Task 6 清理旧模块后达到最终干净态。

所有详细打印都通过 `--debug` 控制。库代码使用 `debugConsole.debug()`；`manualChecks.py` 解析 `--debug` 并把它传给检查函数、fake agent 和 `createAgent(debug=True)`。

---

### Task 1: 拆分日志辅助能力并保持现有 JSONL 行为

**目标：** JSONL 日志仍可写入、脱敏和截断，但 preview/redaction 不再藏在 `jsonl.py` 中，后续工具 runtime 可以不依赖日志 writer。

**涉及的文件：**

- `flamingoAgents/utils/redaction.py` — secret 脱敏规则。
- `flamingoAgents/utils/preview.py` — JSON-safe 转换和预览截断。
- `flamingoAgents/utils/jsonl.py` — JSONL writer，只负责写事件。
- `flamingoAgents/core/conversation.py` — 改为从 `utils.preview` 导入 `makePreview`。

------

#### Step 1 — 实现

创建 `flamingoAgents/utils/redaction.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Provides reusable secret redaction helpers for Flamingo Agents logs and previews.
'''

from __future__ import annotations

import re

secretPatterns = [
    re.compile(r'(?i)(api[_-]?key|token|secret|password)(["\'\s:=]+)([^"\'\s,}]+)'),
    re.compile(r'(?i)(bearer\s+)([A-Za-z0-9._\-]+)'),
    re.compile(r'sk-[A-Za-z0-9]{12,}'),
]


def redactText(text: str) -> str:
    redactedText = text
    redactedText = secretPatterns[0].sub(lambda match: f'{match.group(1)}{match.group(2)}<redacted>', redactedText)
    redactedText = secretPatterns[1].sub(lambda match: f'{match.group(1)}<redacted>', redactedText)
    redactedText = secretPatterns[2].sub('sk-<redacted>', redactedText)
    return redactedText
```

创建 `flamingoAgents/utils/preview.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Provides JSON-safe conversion and truncated preview helpers for logs and tool results.
'''

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from flamingoAgents.utils.redaction import redactText

previewLimit = 4000


def toJsonable(value: Any) -> Any:
    if is_dataclass(value):
        return toJsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): toJsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [toJsonable(item) for item in value]
    return value


def makePreview(value: Any, limit: int = previewLimit) -> tuple[str, bool]:
    if isinstance(value, str):
        rawText = value
    else:
        rawText = json.dumps(toJsonable(value), ensure_ascii=False, sort_keys=True)
    redactedText = redactText(rawText)
    if len(redactedText) <= limit:
        return redactedText, False
    return redactedText[:limit] + '\n<truncated>', True
```

完整替换 `flamingoAgents/utils/jsonl.py`：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Writes JSONL audit events using shared redaction and preview helpers.
'''

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flamingoAgents.utils.preview import makePreview, toJsonable
from flamingoAgents.utils.redaction import redactText


class jsonlLog:
    def __init__(self, logPath: Path):
        self.logPath = logPath
        self.logPath.parent.mkdir(parents=True, exist_ok=True)

    def logEvent(self, event: dict[str, Any]) -> None:
        eventToWrite = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            **toJsonable(event),
        }
        eventText = json.dumps(eventToWrite, ensure_ascii=False, sort_keys=True)
        safeText = redactText(eventText)
        with self.logPath.open('a', encoding='utf-8') as fileObj:
            fileObj.write(safeText + '\n')

    def logPreviewEvent(self, eventType: str, payload: dict[str, Any]) -> None:
        previewText, truncated = makePreview(payload)
        self.logEvent({
            'type': eventType,
            'payloadPreview': previewText,
            'truncated': truncated,
        })
```

完整替换 `flamingoAgents/core/conversation.py`：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Maintains in-memory conversation state and mirrors key events to JSONL logs using shared preview helpers.
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.types import chatMessage, toolResult
from flamingoAgents.utils.jsonl import jsonlLog
from flamingoAgents.utils.preview import makePreview


class conversation:
    def __init__(self, sessionId: str, logPath: Path, systemPrompt: str):
        self.sessionId = sessionId
        self.logger = jsonlLog(logPath)
        self.messages: list[chatMessage] = []
        self.addMessage(chatMessage(role='system', content=systemPrompt))

    def addMessage(self, message: chatMessage) -> None:
        self.messages.append(message)
        if message.role == 'assistant' and message.toolCalls:
            if message.content:
                self.logger.logEvent({
                    'type': 'message',
                    'role': message.role,
                    'content': message.content,
                })
            for call in message.toolCalls:
                argumentsPreview, argumentsTruncated = makePreview(call.arguments)
                self.logger.logEvent({
                    'type': 'toolCall',
                    'role': 'assistant',
                    'toolCallId': call.id,
                    'toolName': call.toolName,
                    'argumentsPreview': argumentsPreview,
                    'argumentsTruncated': argumentsTruncated,
                })
            return

        self.logger.logEvent({
            'type': 'message',
            'role': message.role,
            'content': message.content,
            'toolCallId': message.toolCallId,
            'name': message.name,
        })

    def addToolResult(self, result: toolResult) -> None:
        resultPreview, resultTruncated = makePreview(result.content)
        detailsPreview, detailsTruncated = makePreview(result.details)
        self.logger.logEvent({
            'type': 'toolResult',
            'toolCallId': result.toolCallId,
            'toolName': result.toolName,
            'isError': result.isError,
            'contentPreview': resultPreview,
            'contentTruncated': resultTruncated,
            'detailsPreview': detailsPreview,
            'detailsTruncated': detailsTruncated,
        })
        self.messages.append(chatMessage(
            role='tool',
            content=result.content,
            toolCallId=result.toolCallId,
            name=result.toolName,
        ))
```

------

#### Step 2 — 运行验证

运行无框架验证命令：

```bash
$ uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from flamingoAgents.utils.jsonl import jsonlLog
from flamingoAgents.utils.preview import makePreview
from flamingoAgents.utils.redaction import redactText

with TemporaryDirectory() as tempDir:
    logPath = Path(tempDir) / 'agent.jsonl'
    logger = jsonlLog(logPath)
    logger.logEvent({'type': 'sample', 'token': 'sk-12345678901234567890', 'content': 'x' * 4100})
    text = logPath.read_text(encoding='utf-8')
    assert '<redacted>' in text
    assert '12345678901234567890' not in text
    preview, truncated = makePreview('x' * 4101)
    assert truncated is True
    assert '<truncated>' in preview
    assert redactText('Authorization: Bearer abc123') == 'Authorization: Bearer <redacted>'
print('PASS utils split')
PY
# 预期：输出 PASS utils split，运行无异常，JSONL 文件内容已脱敏且 preview 会截断。
```

如果验证不通过，修复本任务涉及文件并重复运行上述命令，直到输出完全符合预期。

------

✅ **完成的标志：** 第二步验证通过 —— 运行无异常，输出 `PASS utils split`。在满足此条件之前不要开始下一个任务。

------

### Task 2: 新增配置驱动工具系统

**目标：** `config/tools.yaml` 能声明全部工具；工具加载、schema 转换、权限判断和 runtime 执行都能在不依赖旧 registry/guard/file/bash 的情况下独立工作。

**涉及的文件：**

- `config/tools.yaml` — 声明工具。
- `flamingoAgents/tools/toolConfig.py` — 加载和校验工具配置。
- `flamingoAgents/tools/toolSchema.py` — 转模型 function-call schema。
- `flamingoAgents/tools/toolPolicy.py` — 强制 requireApproval。
- `flamingoAgents/tools/toolRuntime.py` — 通用 file/shell runtime。

------

#### Step 1 — 实现

创建 `config/tools.yaml`：

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
            - '(^|[;&|]\s*)rm\s+(-[A-Za-z]*\s+)*[^\n;&|]+'
            - '(^|[;&|]\s*)rmdir\s+[^\n;&|]+'
            - '(^|[;&|]\s*)unlink\s+[^\n;&|]+'
            - '(^|[;&|]\s*)find\s+[^\n;&|]*\s-delete(\s|$)'
            - 'os\.(remove|unlink|rmdir)\s*\('
            - 'shutil\.rmtree\s*\('
            - 'pathlib\.[A-Za-z0-9_\.]+\.(unlink|rmdir)\s*\('
```

创建 `flamingoAgents/tools/toolConfig.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Loads config-driven tool definitions and compiles runtime permission rules.
'''

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Pattern

import yaml

permissionAction = Literal['requireApproval']


@dataclass
class permissionRule:
    id: str
    field: str
    action: permissionAction
    reason: str
    patterns: list[Pattern[str]]


@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    runtime: dict[str, Any]
    permissions: list[permissionRule]


defaultToolsConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'tools.yaml'


def loadToolConfig(configPath: str | Path | None = None, debugConsole=None) -> list[toolDefinition]:
    path = Path(configPath) if configPath is not None else defaultToolsConfigPath
    if debugConsole:
        debugConsole.debug(f'加载工具配置 path={path}')
    if not path.exists():
        raise RuntimeError(f'工具配置文件不存在：{path}')
    with path.open('r', encoding='utf-8') as configFile:
        rawConfig = yaml.safe_load(configFile) or {}
    return parseToolConfig(rawConfig, source=str(path), debugConsole=debugConsole)


def parseToolConfig(rawConfig: Any, source: str = '<memory>', debugConsole=None) -> list[toolDefinition]:
    if not isinstance(rawConfig, dict):
        raise RuntimeError(f'工具配置必须是 YAML 对象：{source}')
    version = rawConfig.get('version')
    if version != 1:
        raise RuntimeError(f'工具配置 version 必须是 1，实际为：{version}')
    rawTools = rawConfig.get('tools')
    if not isinstance(rawTools, list) or not rawTools:
        raise RuntimeError('工具配置 tools 必须是非空数组。')

    seenNames: set[str] = set()
    definitions: list[toolDefinition] = []
    for index, rawTool in enumerate(rawTools):
        definition = parseTool(index, rawTool)
        if definition.name in seenNames:
            raise RuntimeError(f'工具名称重复：{definition.name}')
        seenNames.add(definition.name)
        definitions.append(definition)

    if debugConsole:
        debugConsole.debug(f'工具配置加载完成 count={len(definitions)} names={",".join(sorted(seenNames))}')
    return definitions


def parseTool(index: int, rawTool: Any) -> toolDefinition:
    if not isinstance(rawTool, dict):
        raise RuntimeError(f'第 {index + 1} 个工具必须是对象。')

    name = readRequiredString(rawTool, 'name', f'第 {index + 1} 个工具')
    description = readRequiredString(rawTool, 'description', f'工具 {name}')
    permissionSummary = rawTool.get('modelPermissionSummary')
    if isinstance(permissionSummary, str) and permissionSummary.strip():
        description = f'{description}\n\n权限提示：{permissionSummary.strip()}'

    parameters = rawTool.get('parameters')
    if not isinstance(parameters, dict) or parameters.get('type') != 'object':
        raise RuntimeError(f'工具 {name} 的 parameters 必须是 type=object 的对象。')

    runtime = rawTool.get('runtime')
    if not isinstance(runtime, dict):
        raise RuntimeError(f'工具 {name} 缺少 runtime 对象。')
    runtimeType = runtime.get('type')
    if runtimeType not in {'file', 'shell'}:
        raise RuntimeError(f'工具 {name} 的 runtime.type 不支持：{runtimeType}')
    if runtimeType == 'file' and runtime.get('operation') not in {'read', 'write', 'edit'}:
        raise RuntimeError(f'工具 {name} 的 file operation 不支持：{runtime.get("operation")}')

    permissions = parsePermissions(name, rawTool.get('permissions', []))
    return toolDefinition(
        name=name,
        description=description,
        parameters=parameters,
        runtime=runtime,
        permissions=permissions,
    )


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
        field = readRequiredString(rawRule, 'field', f'工具 {toolName} permission {ruleId}')
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
            field=field,
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

创建 `flamingoAgents/tools/toolSchema.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Converts internal config-driven tool definitions into model function-call schemas.
'''

from __future__ import annotations

from typing import Any

from flamingoAgents.tools.toolConfig import toolDefinition


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

创建 `flamingoAgents/tools/toolPolicy.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Enforces config-driven tool permission rules before runtime execution.
'''

from __future__ import annotations

from dataclasses import dataclass

from flamingoAgents.core.types import toolCall
from flamingoAgents.tools.toolConfig import toolDefinition


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

创建 `flamingoAgents/tools/toolRuntime.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Executes config-driven file and shell tool runtimes with schema validation and workDir sandboxing.
'''

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

from flamingoAgents.core.types import toolContext, toolResult
from flamingoAgents.tools.toolConfig import toolDefinition
from flamingoAgents.utils.preview import makePreview

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30


def executeTool(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str = '') -> toolResult:
    if context.debugConsole:
        context.debugConsole.debug(f'执行工具 runtime tool={definition.name} runtimeType={definition.runtime.get("type")} callId={toolCallId}')
    if not isinstance(arguments, dict):
        return toolResult(toolCallId, definition.name, True, 'toolCall.arguments 必须是对象。', {'invalidArguments': True})

    schemaError = validateArguments(definition.parameters, arguments)
    if schemaError:
        return toolResult(toolCallId, definition.name, True, f'工具参数不符合 schema：{schemaError}', {'schemaError': schemaError})

    runtimeType = definition.runtime.get('type')
    try:
        if runtimeType == 'file':
            return executeFileRuntime(definition, arguments, context, toolCallId)
        if runtimeType == 'shell':
            return executeShellRuntime(definition, arguments, context, toolCallId)
        return toolResult(toolCallId, definition.name, True, f'未知 runtime.type：{runtimeType}', {'unknownRuntime': runtimeType})
    except Exception as error:
        return toolResult(
            toolCallId=toolCallId,
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
        if not isinstance(value, int):
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


def executeFileRuntime(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    operation = definition.runtime.get('operation')
    if operation == 'read':
        return executeFileRead(definition, arguments, context, toolCallId)
    if operation == 'write':
        return executeFileWrite(definition, arguments, context, toolCallId)
    if operation == 'edit':
        return executeFileEdit(definition, arguments, context, toolCallId)
    return toolResult(toolCallId, definition.name, True, f'未知 file operation：{operation}', {'unknownFileOperation': operation})


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


def executeFileRead(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    pathField = str(definition.runtime.get('pathField', 'path'))
    offsetField = str(definition.runtime.get('offsetField', 'offset'))
    limitField = str(definition.runtime.get('limitField', 'limit'))
    path = resolveSafePath(str(arguments[pathField]), context.workDir)
    offset = int(arguments.get(offsetField, 1))
    limit = int(arguments.get(limitField, 200))
    if offset < 1 or limit < 1:
        return toolResult(toolCallId, definition.name, True, 'read.offset 和 read.limit 必须大于 0。')
    if context.debugConsole:
        context.debugConsole.debug(f'读取文件 path={path} offset={offset} limit={limit}')
    if not path.exists() or not path.is_file():
        return toolResult(toolCallId, definition.name, True, f'文件不存在或不是普通文件：{path}', {'path': str(path)})
    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedText = ''.join(lines[startIndex:startIndex + limit])
    truncated = startIndex + limit < len(lines)
    previewText, previewTruncated = makePreview(selectedText)
    return toolResult(
        toolCallId=toolCallId,
        toolName=definition.name,
        isError=False,
        content=previewText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': truncated or previewTruncated,
        },
    )


def executeFileWrite(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    pathField = str(definition.runtime.get('pathField', 'path'))
    contentField = str(definition.runtime.get('contentField', 'content'))
    path = resolveSafePath(str(arguments[pathField]), context.workDir)
    content = arguments[contentField]
    if context.debugConsole:
        context.debugConsole.debug(f'写入文件 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    previewText, truncated = makePreview(content)
    return toolResult(
        toolCallId=toolCallId,
        toolName=definition.name,
        isError=False,
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
            'contentPreview': previewText,
            'truncated': truncated,
        },
    )


def executeFileEdit(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    pathField = str(definition.runtime.get('pathField', 'path'))
    editsField = str(definition.runtime.get('editsField', 'edits'))
    path = resolveSafePath(str(arguments[pathField]), context.workDir)
    edits = arguments[editsField]
    if context.debugConsole:
        context.debugConsole.debug(f'编辑文件 path={path} editCount={len(edits)}')
    if not path.exists() or not path.is_file():
        return toolResult(toolCallId, definition.name, True, f'文件不存在或不是普通文件：{path}', {'path': str(path)})

    originalContent = path.read_text(encoding='utf-8')
    replacements: list[tuple[int, int, str, str]] = []
    for index, editItem in enumerate(edits):
        oldText = editItem['oldText']
        newText = editItem['newText']
        matchCount = originalContent.count(oldText)
        if matchCount != 1:
            return toolResult(toolCallId, definition.name, True, f'第 {index + 1} 个 oldText 必须精确且唯一匹配，当前匹配数：{matchCount}。')
        startIndex = originalContent.index(oldText)
        endIndex = startIndex + len(oldText)
        replacements.append((startIndex, endIndex, oldText, newText))

    replacements.sort(key=lambda item: item[0])
    previousEnd = -1
    for startIndex, endIndex, oldText, newText in replacements:
        if startIndex < previousEnd:
            return toolResult(toolCallId, definition.name, True, '多个 edits 不能重叠。')
        previousEnd = endIndex

    updatedContent = originalContent
    for startIndex, endIndex, oldText, newText in sorted(replacements, key=lambda item: item[0], reverse=True):
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
    return toolResult(
        toolCallId=toolCallId,
        toolName=definition.name,
        isError=False,
        content=previewText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits), 'diffTruncated': truncated},
    )


def executeShellRuntime(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    commandField = str(definition.runtime.get('commandField', 'command'))
    timeoutField = str(definition.runtime.get('timeoutField', 'timeout'))
    command = arguments.get(commandField)
    if not isinstance(command, str) or not command.strip():
        return toolResult(toolCallId, definition.name, True, 'bash.command 必须是非空字符串。')
    timeout = int(arguments.get(timeoutField, defaultTimeoutSeconds))
    if timeout < 1:
        timeout = defaultTimeoutSeconds
    if timeout > maxTimeoutSeconds:
        timeout = maxTimeoutSeconds
    if context.debugConsole:
        context.debugConsole.debug(f'执行 shell command={command} timeout={timeout} cwd={context.workDir}')
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
        isError = completedProcess.returncode != 0
        return toolResult(
            toolCallId=toolCallId,
            toolName=definition.name,
            isError=isError,
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
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
        stdoutText = error.stdout if isinstance(error.stdout, str) else (error.stdout or b'').decode('utf-8', errors='replace')
        stderrText = error.stderr if isinstance(error.stderr, str) else (error.stderr or b'').decode('utf-8', errors='replace')
        stdoutPreview, stdoutTruncated = makePreview(stdoutText)
        stderrPreview, stderrTruncated = makePreview(stderrText)
        return toolResult(
            toolCallId=toolCallId,
            toolName=definition.name,
            isError=True,
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
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

------

#### Step 2 — 运行验证

运行无框架验证命令：

```bash
$ uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from flamingoAgents.core.types import toolCall, toolContext
from flamingoAgents.tools.toolConfig import loadToolConfig
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRuntime import executeTool
from flamingoAgents.tools.toolSchema import buildModelTools
from flamingoAgents.utils.debug import debugConsole

def byName(definitions, name):
    return next(definition for definition in definitions if definition.name == name)

def expect(condition, message):
    if not condition:
        raise RuntimeError(message)

definitions = loadToolConfig(debugConsole=debugConsole(True))
expect({definition.name for definition in definitions} == {'read', 'write', 'edit', 'bash'}, '工具名集合不正确')
modelTools = buildModelTools(definitions)
expect(modelTools[0]['type'] == 'function', '模型工具 schema 包装不正确')
bashDefinition = byName(definitions, 'bash')
rmDecision = evaluateToolCall(bashDefinition, toolCall('call_rm', 'bash', {'command': 'rm file'}), debugConsole=debugConsole(True))
grepDecision = evaluateToolCall(bashDefinition, toolCall('call_grep', 'bash', {'command': 'grep keyword file'}), debugConsole=debugConsole(True))
expect(rmDecision.requiresApproval is True, 'rm 未触发确认')
expect(grepDecision.requiresApproval is False, 'grep 被误判为危险命令')
with TemporaryDirectory() as tempDir:
    context = toolContext(workDir=Path(tempDir), debugConsole=debugConsole(True))
    writeResult = executeTool(byName(definitions, 'write'), {'path': 'sample.txt', 'content': 'alpha\nbeta\n'}, context, 'call_write')
    expect(not writeResult.isError, writeResult.content)
    readResult = executeTool(byName(definitions, 'read'), {'path': 'sample.txt', 'offset': 1, 'limit': 1}, context, 'call_read')
    expect('alpha' in readResult.content, readResult.content)
    editResult = executeTool(byName(definitions, 'edit'), {'path': 'sample.txt', 'edits': [{'oldText': 'beta', 'newText': 'gamma'}]}, context, 'call_edit')
    expect(not editResult.isError, editResult.content)
    blockedResult = executeTool(byName(definitions, 'read'), {'path': '../outside.txt'}, context, 'call_escape')
    expect(blockedResult.isError, '路径逃逸没有被阻止')
    bashResult = executeTool(bashDefinition, {'command': 'printf hello', 'timeout': 5}, context, 'call_bash')
    expect(not bashResult.isError and 'hello' in bashResult.content, bashResult.content)
print('PASS tool config policy runtime')
PY
# 预期：输出详细 debug 日志和 PASS tool config policy runtime；运行无异常；rm 触发确认、grep 不触发、文件路径逃逸被阻止。
```

如果验证不通过，修复本任务涉及文件并重复运行上述命令，直到输出完全符合预期。

------

✅ **完成的标志：** 第二步验证通过 —— 运行无异常，输出 `PASS tool config policy runtime`。在满足此条件之前不要开始下一个任务。

------

### Task 3: 分离模型配置、auth 和 Chat Completions adapter

**目标：** 模型配置加载不写 `os.environ`，adapter 不读取环境变量也不依赖 JSONL logger，并且 tool call arguments 非 dict 时会被拒绝。

**涉及的文件：**

- `flamingoAgents/models/modelConfig.py` — 读取模型配置并解析 apiKey。
- `flamingoAgents/models/modelAuth.py` — 创建 Authorization header。
- `flamingoAgents/models/chatCompletions.py` — adapter 使用注入 auth，返回 modelCompletion。

------

#### Step 1 — 实现

创建 `flamingoAgents/models/modelConfig.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Loads model configuration and resolves API keys without mutating process environment.
'''

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


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


defaultModelConfigPath = Path(__file__).resolve().parents[2] / 'config' / 'models.yaml'


def loadModelConfig(
    providerId: str = '101',
    modelId: str | None = None,
    configPath: str | Path | None = None,
    debugConsole=None,
) -> resolvedModelConfig:
    path = Path(configPath) if configPath is not None else defaultModelConfigPath
    if path.exists():
        return loadModelConfigFromYaml(providerId=providerId, modelId=modelId, configPath=path, debugConsole=debugConsole)
    return loadModelConfigFromEnv(debugConsole=debugConsole)


def loadModelConfigFromEnv(debugConsole=None) -> resolvedModelConfig:
    model = os.getenv('FLAMINGO_AGENTS_MODEL', '').strip()
    baseUrl = os.getenv('FLAMINGO_AGENTS_BASE_URL', '').strip()
    apiKey = os.getenv('FLAMINGO_AGENTS_API_KEY', '').strip()
    apiKeyEnv = os.getenv('FLAMINGO_AGENTS_API_KEY_ENV', 'OPENAI_API_KEY').strip()
    if not apiKey and apiKeyEnv:
        apiKey = os.getenv(apiKeyEnv, '').strip()

    missingFields = []
    if not model:
        missingFields.append('FLAMINGO_AGENTS_MODEL')
    if not baseUrl:
        missingFields.append('FLAMINGO_AGENTS_BASE_URL')
    if not apiKey:
        missingFields.append(apiKeyEnv or 'FLAMINGO_AGENTS_API_KEY')
    if missingFields:
        joinedFields = ', '.join(missingFields)
        raise RuntimeError(f'模型配置缺失：{joinedFields}')

    if debugConsole:
        debugConsole.debug(f'从环境变量加载模型配置 model={model} baseUrl={baseUrl}')
    return resolvedModelConfig(
        config=modelConfig(
            provider='openaiCompatible',
            model=model,
            baseUrl=baseUrl,
            apiType='openaiCompatible',
            supportsToolCalling=True,
        ),
        apiKey=apiKey,
    )


def loadModelConfigFromYaml(
    providerId: str = '101',
    modelId: str | None = None,
    configPath: str | Path | None = None,
    debugConsole=None,
) -> resolvedModelConfig:
    path = Path(configPath) if configPath is not None else defaultModelConfigPath
    if not path.exists():
        raise RuntimeError(f'模型配置文件不存在：{path}')

    with path.open('r', encoding='utf-8') as configFile:
        rawConfig = yaml.safe_load(configFile) or {}
    if not isinstance(rawConfig, dict):
        raise RuntimeError('模型配置文件必须是 YAML 对象。')

    providers = rawConfig.get('providers')
    if not isinstance(providers, dict):
        raise RuntimeError('模型配置缺少 providers 对象。')

    providerConfig = providers.get(providerId)
    if not isinstance(providerConfig, dict):
        raise RuntimeError(f'模型配置缺少 provider：{providerId}')

    baseUrl = providerConfig.get('baseUrl')
    if not isinstance(baseUrl, str) or not baseUrl.strip():
        raise RuntimeError(f'provider {providerId} 缺少 baseUrl。')

    models = providerConfig.get('models')
    if not isinstance(models, list) or not models:
        raise RuntimeError(f'provider {providerId} 缺少 models。')

    selectedModel = selectModel(models, modelId, providerId)
    selectedModelId = selectedModel.get('id')
    if not isinstance(selectedModelId, str) or not selectedModelId.strip():
        raise RuntimeError(f'provider {providerId} 的模型缺少 id。')

    api = selectedModel.get('api') or providerConfig.get('api')
    if api != 'openai-completions':
        raise RuntimeError(f'当前仅支持 openai-completions，实际配置为：{api}')

    rawApiKey = providerConfig.get('apiKey')
    if not isinstance(rawApiKey, str) or not rawApiKey.strip():
        raise RuntimeError(f'provider {providerId} 缺少 apiKey。')
    apiKey = resolveApiKey(rawApiKey.strip(), providerId)

    if debugConsole:
        debugConsole.debug(f'从 YAML 加载模型配置 provider={providerId} model={selectedModelId} baseUrl={baseUrl.strip()}')
    return resolvedModelConfig(
        config=modelConfig(
            provider=providerId,
            model=selectedModelId.strip(),
            baseUrl=baseUrl.strip(),
            apiType='openaiCompatible',
            supportsToolCalling=True,
        ),
        apiKey=apiKey,
    )


def selectModel(models: list[Any], modelId: str | None, providerId: str) -> dict[str, Any]:
    if modelId is None:
        firstModel = models[0]
        if isinstance(firstModel, dict):
            return firstModel
    else:
        for modelItem in models:
            if isinstance(modelItem, dict) and modelItem.get('id') == modelId:
                return modelItem
    raise RuntimeError(f'provider {providerId} 缺少可用模型：{modelId or "<first>"}')


def resolveApiKey(rawApiKey: str, providerId: str) -> str:
    if rawApiKey.startswith('${') and rawApiKey.endswith('}'):
        envName = rawApiKey[2:-1].strip()
        if not envName:
            raise RuntimeError(f'provider {providerId} 的 apiKey 环境变量名为空。')
        value = os.getenv(envName, '').strip()
        if not value:
            raise RuntimeError(f'模型配置缺失：{envName}')
        return value
    if rawApiKey.startswith('$'):
        envName = rawApiKey[1:].strip()
        if not envName:
            raise RuntimeError(f'provider {providerId} 的 apiKey 环境变量名为空。')
        value = os.getenv(envName, '').strip()
        if not value:
            raise RuntimeError(f'模型配置缺失：{envName}')
        return value
    return rawApiKey
```

创建 `flamingoAgents/models/modelAuth.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Builds model authorization data from resolved model credentials.
'''

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class modelAuth:
    authorizationHeader: str


def createModelAuth(apiKey: str) -> modelAuth:
    cleanKey = apiKey.strip()
    if not cleanKey:
        raise RuntimeError('模型 apiKey 不能为空。')
    return modelAuth(authorizationHeader=f'Bearer {cleanKey}')
```

完整替换 `flamingoAgents/models/chatCompletions.py`：

```python
'''
Author: wilbur
Version: 1.4
Date: 2026-07-02
Description: Adapts internal chat messages and tool schemas to OpenAI-compatible chat completions using injected model auth.
'''

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from flamingoAgents.core.types import chatMessage, toolCall
from flamingoAgents.models.modelAuth import modelAuth
from flamingoAgents.models.modelConfig import modelConfig


@dataclass
class modelCompletion:
    message: chatMessage
    requestPayload: dict[str, Any]
    responsePayload: dict[str, Any]


class modelRequestError(Exception):
    def __init__(self, message: str, requestPayload: dict[str, Any], statusCode: int | None = None, responseBody: str = ''):
        super().__init__(message)
        self.requestPayload = requestPayload
        self.statusCode = statusCode
        self.responseBody = responseBody


class chatCompletionsAdapter:
    def __init__(self, config: modelConfig, auth: modelAuth, debugConsole=None):
        self.config = config
        self.auth = auth
        self.debugConsole = debugConsole

    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> modelCompletion:
        requestPayload = {
            'model': self.config.model,
            'messages': [self.convertMessage(message) for message in messages],
            'tools': tools,
            'tool_choice': 'auto',
        }
        requestUrl = self.config.baseUrl.rstrip('/') + '/chat/completions'
        requestBytes = json.dumps(requestPayload).encode('utf-8')
        request = urllib.request.Request(
            requestUrl,
            data=requestBytes,
            method='POST',
            headers={
                'Authorization': self.auth.authorizationHeader,
                'Content-Type': 'application/json',
            },
        )
        if self.debugConsole:
            self.debugConsole.debug(
                f'调用模型 provider={self.config.provider} model={self.config.model} '
                f'messages={len(messages)} tools={len(tools)} url={requestUrl}'
            )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                responseText = response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            errorText = error.read().decode('utf-8', errors='replace')
            raise modelRequestError(
                message=f'模型请求失败：status={error.code} body={errorText[:1000]}',
                requestPayload=requestPayload,
                statusCode=error.code,
                responseBody=errorText,
            ) from error
        except urllib.error.URLError as error:
            raise modelRequestError(
                message=f'模型请求失败：{error.reason}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=str(error.reason),
            ) from error

        payload = json.loads(responseText)
        if self.debugConsole:
            usage = payload.get('usage') if isinstance(payload, dict) else None
            self.debugConsole.debug(f'模型响应完成 model={self.config.model} usage={usage}')
        return modelCompletion(
            message=self.parseAssistantPayload(payload),
            requestPayload=requestPayload,
            responsePayload=payload,
        )

    def convertMessage(self, message: chatMessage) -> dict[str, Any]:
        if message.role == 'tool':
            return {
                'role': 'tool',
                'tool_call_id': message.toolCallId,
                'content': message.content,
            }
        converted: dict[str, Any] = {
            'role': message.role,
            'content': message.content,
        }
        if message.role == 'assistant' and message.toolCalls:
            converted['tool_calls'] = [
                {
                    'id': call.id,
                    'type': 'function',
                    'function': {
                        'name': call.toolName,
                        'arguments': json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.toolCalls
            ]
        return converted

    def parseAssistantPayload(self, payload: dict[str, Any]) -> chatMessage:
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices:
            raise RuntimeError('模型响应缺少 choices。')
        rawMessage = choices[0].get('message')
        if not isinstance(rawMessage, dict):
            raise RuntimeError('模型响应缺少 message。')

        parsedToolCalls: list[toolCall] = []
        rawToolCalls = rawMessage.get('tool_calls') or []
        if not isinstance(rawToolCalls, list):
            raise RuntimeError('模型响应 tool_calls 必须是数组。')
        for index, rawCall in enumerate(rawToolCalls):
            if not isinstance(rawCall, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call 必须是对象。')
            functionValue = rawCall.get('function') or {}
            if not isinstance(functionValue, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.function 必须是对象。')
            argumentsText = functionValue.get('arguments') or '{}'
            if not isinstance(argumentsText, str):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是字符串。')
            try:
                arguments = json.loads(argumentsText)
            except json.JSONDecodeError as error:
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 不是合法 JSON。') from error
            if not isinstance(arguments, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是 JSON 对象。')
            parsedToolCalls.append(toolCall(
                id=rawCall.get('id') or f'call_{index + 1}',
                toolName=functionValue.get('name') or '',
                arguments=arguments,
            ))

        content = rawMessage.get('content') or ''
        return chatMessage(role='assistant', content=content, toolCalls=parsedToolCalls)
```

------

#### Step 2 — 运行验证

运行无框架验证命令：

```bash
$ uv run python - <<'PY'
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfigFromYaml, modelConfig

with TemporaryDirectory() as tempDir:
    configPath = Path(tempDir) / 'models.yaml'
    configPath.write_text(
        'providers:\n'
        '  "abc":\n'
        '    baseUrl: http://127.0.0.1:9/v1\n'
        '    api: openai-completions\n'
        '    apiKey: inline-key\n'
        '    models:\n'
        '      - id: model-a\n',
        encoding='utf-8')
    before = os.environ.get('FLAMINGO_AGENTS_ABC_API_KEY')
    resolved = loadModelConfigFromYaml(providerId='abc', configPath=configPath)
    after = os.environ.get('FLAMINGO_AGENTS_ABC_API_KEY')
    assert resolved.apiKey == 'inline-key'
    assert before == after

auth = createModelAuth('abc123')
assert auth.authorizationHeader == 'Bearer abc123'
adapter = chatCompletionsAdapter(modelConfig('p', 'm', 'http://127.0.0.1:9/v1', 'openaiCompatible'), auth)
message = adapter.parseAssistantPayload({
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
assert message.toolCalls[0].arguments['path'] == 'sample.txt'
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
sourceText = Path('flamingoAgents/models/chatCompletions.py').read_text(encoding='utf-8')
assert 'import os' not in sourceText
assert 'os.getenv' not in sourceText
assert 'jsonlLog' not in sourceText
print('PASS model config auth adapter')
PY
# 预期：输出 PASS model config auth adapter；运行无异常；配置加载不写 os.environ，adapter 不含 os.getenv/jsonlLog，非对象 arguments 被拒绝。
```

如果验证不通过，修复本任务涉及文件并重复运行上述命令，直到输出完全符合预期。

------

✅ **完成的标志：** 第二步验证通过 —— 运行无异常，输出 `PASS model config auth adapter`。在满足此条件之前不要开始下一个任务。

------

### Task 4: 切换 Core Agent、公开 Builder，并删除内置入口

**目标：** 包根可通过 `createAgent` 导入纯库 Agent；Agent 不再接收 `confirmDeletion`，能处理多 tool call pending、错误 session 不消费 pending、pending 期间拒绝新消息，并且项目不再暴露 CLI/HTTP 命令。

**涉及的文件：**

- `flamingoAgents/core/types.py` — 更新 pendingConfirm 为批处理，移除旧 toolSpec/modelConfig。
- `flamingoAgents/core/ports.py` — 新增端口协议。
- `flamingoAgents/core/agent.py` — 改成纯 Agent 状态机。
- `flamingoAgents/builder.py` — 新增纯库工厂。
- `flamingoAgents/__init__.py` — re-export createAgent。
- `pyproject.toml` — 删除命令入口。
- 删除 `flamingoAgents/app/`。

------

#### Step 1 — 实现

完整替换 `flamingoAgents/core/types.py`：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Defines shared lower-camel-case data structures for messages, tools, runtime context, confirmations, and agent results.
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

创建 `flamingoAgents/core/ports.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Defines lightweight core protocols for model adapters and debug output.
'''

from __future__ import annotations

from typing import Any, Protocol

from flamingoAgents.core.types import chatMessage


class modelAdapterPort(Protocol):
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> Any:
        pass


class debugPort(Protocol):
    isDebug: bool

    def debug(self, message: str) -> None:
        pass
```

完整替换 `flamingoAgents/core/agent.py`：

```python
'''
Author: wilbur
Version: 1.3
Date: 2026-07-02
Description: Coordinates pure Agent sessions, model loops, config-driven tool execution, approval state, and JSONL logging.
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
from flamingoAgents.tools.toolConfig import toolDefinition
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRuntime import executeTool
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
        self.toolDefinitions = {definition.name: definition for definition in toolDefinitions}
        self.workDir = workDir
        self.logDir = logDir
        self.debugConsole = debugConsole
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversation] = {}
        self.pendingConfirms: dict[str, pendingConfirm] = {}
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
            pending = self.pendingConfirms.get(confirmationId)
            if pending is None or pending.sessionId != sessionId:
                return runResult(sessionId=sessionId, status='error', message='确认请求不存在或 sessionId 不匹配。')
            self.pendingConfirms.pop(confirmationId, None)
            currentConversation = self.getConversation(sessionId)
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
            modelTools = buildModelTools(list(self.toolDefinitions.values()))
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
            definition = self.toolDefinitions.get(call.toolName)
            if definition is None:
                currentConversation.addToolResult(self.makeUnknownToolResult(call))
                continue
            decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
            if decision.requiresApproval:
                confirmationId = 'confirm_' + uuid4().hex[:12]
                self.pendingConfirms[confirmationId] = pendingConfirm(
                    sessionId=sessionId,
                    confirmationId=confirmationId,
                    reason=decision.reason,
                    toolCalls=toolCalls,
                    currentIndex=index,
                )
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
                    commandPreview=str(call.arguments),
                    toolCall=call,
                )
            result = self.executeToolCall(call)
            currentConversation.addToolResult(result)
        return None

    def executeToolCall(self, call: toolCall) -> toolResult:
        definition = self.toolDefinitions.get(call.toolName)
        if definition is None:
            return self.makeUnknownToolResult(call)
        context = toolContext(workDir=self.workDir, debugConsole=self.debugConsole)
        return executeTool(definition, call.arguments, context, toolCallId=call.id)

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
        return any(pending.sessionId == sessionId for pending in self.pendingConfirms.values())

    def getSessionLock(self, sessionId: str) -> RLock:
        with self.sessionLocksGuard:
            lock = self.sessionLocks.get(sessionId)
            if lock is None:
                lock = RLock()
                self.sessionLocks[sessionId] = lock
            return lock

    def getConversation(self, sessionId: str) -> conversation:
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

创建 `flamingoAgents/builder.py`：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Pure-library assembly factory: resolves paths, loads model config/auth and tools, and returns a ready-to-use agent.
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfig
from flamingoAgents.tools.toolConfig import loadToolConfig
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
    definitions = loadToolConfig(configPath=toolsConfigPath, debugConsole=printer)
    return agent(
        modelAdapter=adapter,
        toolDefinitions=definitions,
        workDir=workDirPath,
        logDir=resolvedLogDir,
        debugConsole=printer,
    )
```

完整替换 `flamingoAgents/__init__.py`：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Exposes the pure-library public API for Flamingo Agents.
'''

from flamingoAgents.builder import createAgent

packageVersion = '0.1.0'

__all__ = ['createAgent', 'packageVersion']
```

完整替换 `pyproject.toml`：

```toml
[project]
name = "flamingo-agents"
version = "0.1.0"
description = "Local Flamingo Agents as a pure library"
requires-python = ">=3.12"
dependencies = [
    "pyyaml>=6.0.3",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["flamingoAgents"]
```

删除旧入口目录：

```bash
$ rm -r flamingoAgents/app
```

------

#### Step 2 — 运行验证

运行无框架验证命令：

```bash
$ uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from flamingoAgents import createAgent
from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import chatMessage, toolCall
from flamingoAgents.models.chatCompletions import modelCompletion
from flamingoAgents.tools.toolConfig import loadToolConfig
from flamingoAgents.utils.debug import debugConsole

def expect(condition, message):
    if not condition:
        raise RuntimeError(message)

builtAgent = createAgent(Path('.'), debug=True)
expect(type(builtAgent).__name__ == 'agent', 'createAgent 未返回 agent')
expect(not Path('flamingoAgents/app').exists(), 'app 目录仍然存在')
pyproject = Path('pyproject.toml').read_text(encoding='utf-8')
expect('[project.scripts]' not in pyproject, 'pyproject 仍包含命令入口')


class fakeModel:
    def complete(self, messages, tools):
        last = messages[-1]
        if last.role == 'user' and last.content == 'batch':
            return modelCompletion(
                message=chatMessage(role='assistant', content='', toolCalls=[
                    toolCall('c1', 'read', {'path': 'sample.txt'}),
                    toolCall('c2', 'bash', {'command': 'rm sample.txt'}),
                    toolCall('c3', 'read', {'path': 'sample.txt'}),
                ]),
                requestPayload={},
                responsePayload={},
            )
        return modelCompletion(message=chatMessage(role='assistant', content='done'), requestPayload={}, responsePayload={})


with TemporaryDirectory() as tempDir:
    workDir = Path(tempDir)
    (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
    testAgent = agent(
        modelAdapter=fakeModel(),
        toolDefinitions=loadToolConfig(debugConsole=debugConsole(True)),
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugConsole=debugConsole(True),
    )
    confirmResult = testAgent.runUserMessage('batch', sessionId='s')
    expect(confirmResult.status == 'confirmationRequired', confirmResult.message)
    expect((workDir / 'sample.txt').exists(), '需要确认时不应执行 rm')

    newMessageResult = testAgent.runUserMessage('again', sessionId='s')
    expect(newMessageResult.status == 'error', 'pending 期间新消息应被拒绝')

    wrongSessionResult = testAgent.continueConfirmation('wrong', confirmResult.confirmationId, True)
    expect(wrongSessionResult.status == 'error', '错误 sessionId 不应消费 pending')
    expect(testAgent.hasPendingConfirmation('s'), '错误 sessionId 不应清掉真实 pending')

    approvedResult = testAgent.continueConfirmation('s', confirmResult.confirmationId, True)
    expect(approvedResult.status == 'completed', approvedResult.message)
print('PASS agent state machine')
PY
# 预期：输出详细 debug 日志和 PASS agent state machine；createAgent 可用、app 目录已删、命令入口已删；
#       多 tool call 在确认处不丢后续工具、错误 session 不消费 pending、pending 期间拒绝新消息。
```

如果验证不通过，修复本任务涉及文件并重复运行上述命令，直到输出完全符合预期。

------

✅ **完成的标志：** 第二步验证通过 —— 运行无异常，输出 `PASS agent state machine`。在满足此条件之前不要开始下一个任务。

------

### Task 5: 重写 manualChecks.py 为无框架主验证入口

**目标：** `manualChecks.py` 不再依赖任何入口层或旧模块，所有检查项使用新栈（toolConfig/toolPolicy/toolRuntime/toolSchema/modelConfig/modelAuth/chatCompletions/agent），并通过 `--debug` 控制详细输出；`uv run python manualChecks.py all` 全部 PASS。

**涉及的文件：**

- `manualChecks.py` — 完整重写。

------

#### Step 1 — 实现

完整替换 `manualChecks.py`：

```python
'''
Author: wilbur
Version: 2.0
Date: 2026-07-02
Description: Framework-free manual validation entrypoint for the pure-library Flamingo Agents runtime, with --debug controlled output.
'''

from __future__ import annotations

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import chatMessage, toolCall, toolContext
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter, modelCompletion
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfigFromYaml, modelConfig
from flamingoAgents.tools.toolConfig import loadToolConfig
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRuntime import executeTool
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
        return modelCompletion(message=chatMessage(role='assistant', content='done'), requestPayload={}, responsePayload={})


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def printPass(name: str) -> None:
    print(f'PASS {name}')


def printDebug(debugEnabled: bool, message: str) -> None:
    if debugEnabled:
        print(f'[manual debug] {message}', flush=True)


def byName(definitions, name):
    return next(definition for definition in definitions if definition.name == name)


def runToolConfigCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 tool config 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadToolConfig(debugConsole=printer)
    expect({definition.name for definition in definitions} == {'read', 'write', 'edit', 'bash'}, '工具名集合不正确')
    modelTools = buildModelTools(definitions)
    expect(modelTools[0]['type'] == 'function', '模型工具 schema 包装不正确')
    expect(all('permissions' not in tool['function'] for tool in modelTools), '模型 schema 泄漏了 permissions')
    printPass('tool config')


def runPermissionCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 permission policy 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadToolConfig(debugConsole=printer)
    bashDefinition = byName(definitions, 'bash')
    readDefinition = byName(definitions, 'read')
    expect(evaluateToolCall(bashDefinition, toolCall('a', 'bash', {'command': 'rm file'}), debugConsole=printer).requiresApproval is True, 'rm 未触发确认')
    expect(evaluateToolCall(bashDefinition, toolCall('b', 'bash', {'command': 'grep keyword file'}), debugConsole=printer).requiresApproval is False, 'grep 被误判')
    expect(evaluateToolCall(bashDefinition, toolCall('c', 'bash', {'command': 'find . -delete'}), debugConsole=printer).requiresApproval is True, 'find -delete 未触发确认')
    expect(evaluateToolCall(readDefinition, toolCall('d', 'read', {'path': 'sample.txt'}), debugConsole=printer).requiresApproval is False, 'read 不应触发确认')
    printPass('permission policy')


def runToolRuntimeCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 tool runtime 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadToolConfig(debugConsole=printer)
    with TemporaryDirectory() as tempDir:
        context = toolContext(workDir=Path(tempDir), debugConsole=printer)
        writeDefinition = byName(definitions, 'write')
        readDefinition = byName(definitions, 'read')
        editDefinition = byName(definitions, 'edit')
        bashDefinition = byName(definitions, 'bash')

        writeResult = executeTool(writeDefinition, {'path': 'sample.txt', 'content': 'alpha\nbeta\n'}, context, 'call_write')
        expect(not writeResult.isError, writeResult.content)
        readResult = executeTool(readDefinition, {'path': 'sample.txt', 'offset': 1, 'limit': 1}, context, 'call_read')
        expect('alpha' in readResult.content, readResult.content)
        editResult = executeTool(editDefinition, {'path': 'sample.txt', 'edits': [{'oldText': 'beta', 'newText': 'gamma'}]}, context, 'call_edit')
        expect(not editResult.isError, editResult.content)

        for escapePath in ['../outside.txt', '/tmp/outside.txt', '~/secret.txt']:
            escapeResult = executeTool(readDefinition, {'path': escapePath}, context, 'call_escape')
            expect(escapeResult.isError, f'路径逃逸没有被阻止：{escapePath}')

        bashResult = executeTool(bashDefinition, {'command': 'printf hello', 'timeout': 5}, context, 'call_bash')
        expect(not bashResult.isError and 'hello' in bashResult.content, bashResult.content)
        failResult = executeTool(bashDefinition, {'command': 'exit 7', 'timeout': 5}, context, 'call_fail')
        expect(failResult.isError and failResult.details.get('exitCode') == 7, '非零退出码未被标记为错误')
        timeoutResult = executeTool(bashDefinition, {'command': 'sleep 2', 'timeout': 1}, context, 'call_timeout')
        expect(timeoutResult.isError and timeoutResult.details.get('timeoutExpired') is True, '超时未被捕获')
        clampedResult = executeTool(bashDefinition, {'command': 'printf clamp', 'timeout': 999}, context, 'call_clamp')
        expect(not clampedResult.isError and clampedResult.details.get('timeout') == 120, 'timeout 未被限制到 120')
    printPass('tool runtime')


def runLoggerCheck() -> None:
    with TemporaryDirectory() as tempDir:
        logPath = Path(tempDir) / 'agent.jsonl'
        logger = jsonlLog(logPath)
        logger.logEvent({'type': 'sample', 'token': 'sk-12345678901234567890', 'content': 'x' * 4100})
        logText = logPath.read_text(encoding='utf-8')
        expect('<redacted>' in logText, 'secret 未脱敏')
        expect('12345678901234567890' not in logText, 'secret 原文泄露')
    printPass('jsonl logger')


def runAdapterParseCheck() -> None:
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
    printPass('adapter parse')


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
    printPass('model config auth adapter')


def buildFakeAgent(workDir: Path, debugEnabled: bool) -> agent:
    return agent(
        modelAdapter=fakeModel(),
        toolDefinitions=loadToolConfig(debugConsole=debugConsole(debugEnabled)),
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
    printPass('agent state machine')


def runPureLibraryApiCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始纯库 API 检查')
    from flamingoAgents import createAgent

    builtAgent = createAgent(Path('.'), debug=debugEnabled)
    expect(type(builtAgent).__name__ == 'agent', 'createAgent 未返回 agent')
    expect(not Path('flamingoAgents/app').exists(), 'app 目录仍然存在')
    pyproject = Path('pyproject.toml').read_text(encoding='utf-8')
    expect('[project.scripts]' not in pyproject, 'pyproject 仍包含命令入口')
    manualSource = Path('manualChecks.py').read_text(encoding='utf-8')
    expect('flamingoAgents.app' not in manualSource, 'manualChecks 仍依赖 app 层')
    printPass('pure library api')


def main() -> None:
    parser = argparse.ArgumentParser(description='运行无测试框架的手动验证')
    parser.add_argument('check', choices=[
        'all', 'toolConfig', 'permission', 'runtime', 'logger', 'adapter', 'modelAuth', 'agent', 'pureLibrary',
    ])
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    args = parser.parse_args()

    if args.check in {'all', 'toolConfig'}:
        runToolConfigCheck(args.debug)
    if args.check in {'all', 'permission'}:
        runPermissionCheck(args.debug)
    if args.check in {'all', 'runtime'}:
        runToolRuntimeCheck(args.debug)
    if args.check in {'all', 'logger'}:
        runLoggerCheck()
    if args.check in {'all', 'adapter'}:
        runAdapterParseCheck()
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

运行无框架验证命令：

```bash
$ uv run python manualChecks.py all --debug
# 预期：打印详细 [manual debug] / [debug ...] 日志，并依次输出：
#   PASS tool config
#   PASS permission policy
#   PASS tool runtime
#   PASS jsonl logger
#   PASS adapter parse
#   PASS model config auth adapter
#   PASS agent state machine
#   PASS pure library api
# 运行无异常。
```

如果验证不通过，修复本任务涉及文件并重复运行上述命令，直到八项全部 PASS。

------

✅ **完成的标志：** 第二步验证通过 —— 运行无异常，八项检查全部输出 `PASS`。在满足此条件之前不要开始下一个任务。

------

### Task 6: 删除旧工具/模型边界文件并做全量验收

**目标：** 旧 `guard/registry/router/file/bash` 与旧 `models/registry.py` 全部删除，且代码中没有任何残留引用；最终 `uv run python manualChecks.py all` 全绿、`from flamingoAgents import createAgent` 可用。

**涉及的文件：**

- 删除 `flamingoAgents/tools/guard.py`
- 删除 `flamingoAgents/tools/registry.py`
- 删除 `flamingoAgents/tools/router.py`
- 删除 `flamingoAgents/tools/file.py`
- 删除 `flamingoAgents/tools/bash.py`
- 删除 `flamingoAgents/models/registry.py`

------

#### Step 1 — 实现

删除旧边界文件并清理缓存：

```bash
$ rm flamingoAgents/tools/guard.py flamingoAgents/tools/registry.py flamingoAgents/tools/router.py flamingoAgents/tools/file.py flamingoAgents/tools/bash.py flamingoAgents/models/registry.py
$ find flamingoAgents -type d -name __pycache__ -prune -exec rm -rf {} +
```

------

#### Step 2 — 运行验证

确认没有残留引用：

```bash
$ rg -n "createDefaultRegistry|checkToolCall|detectDeletionCommand|makeBlockedToolResult|confirmationNeeded|executeRead|executeWrite|executeEdit|executeBash|from flamingoAgents.app|import flamingoAgents.app|flamingoAgents.models.registry|flamingoAgents.tools.guard|flamingoAgents.tools.router|flamingoAgents.tools.file|flamingoAgents.tools.bash|flamingoAgents.tools.registry|confirmDeletion" flamingoAgents manualChecks.py pyproject.toml
# 预期：无任何输出（旧符号与旧 import 全部清除）。
```

运行全量无框架验证：

```bash
$ uv run python manualChecks.py all
# 预期：八项检查全部 PASS，运行无异常。
```

确认纯库入口可用：

```bash
$ uv run python -c "from flamingoAgents import createAgent; print('import ok')"
# 预期：输出 import ok。
```

如果残留引用扫描有输出，定位并删除对应 import / 引用后重跑；如果 manualChecks 任一项失败，修复相关实现后重跑，直到三条命令全部符合预期。

------

✅ **完成的标志：** 第二步验证通过 —— 残留引用扫描无输出、`uv run python manualChecks.py all` 八项全 PASS、纯库 import 输出 `import ok`。

---

## 自我复审

**1. 规范覆盖（对照 recipe 成功标准）：**

- 包根只公开 `createAgent` / `packageVersion` → Task 4 `__init__.py`、Task 5 `runPureLibraryApiCheck`。
- 删除内置 CLI/HTTP 入口、删除 `[project.scripts]` → Task 4、Task 5 `runPureLibraryApiCheck`。
- Agent 不接收 `confirmDeletion`、只返回 `confirmationRequired` → Task 4 `agent.py`、Task 5 `runAgentStateCheck`。
- pending 支持多 toolCall 批处理、错误 session 不消费、pending 期间拒绝新消息 → Task 4 `processToolBatch`/`continueConfirmation`、Task 5 `runAgentStateCheck`。
- tools 全部来自 `config/tools.yaml`、无硬编码 schema → Task 2、Task 5 `runToolConfigCheck`。
- `permissions.action=requireApproval` 由 `toolPolicy` 强制 → Task 2、Task 5 `runPermissionCheck`。
- file runtime 无法逃逸 `workDir` → Task 2 `resolveSafePath`、Task 5 `runToolRuntimeCheck`。
- 非 dict tool arguments 不打穿 Agent → Task 3 `parseAssistantPayload`、Task 2 `executeTool`、Task 5 `runAdapterParseCheck`/`runToolRuntimeCheck`。
- adapter 不读环境变量、配置不写 `os.environ` → Task 3、Task 5 `runModelAuthCheck`。
- adapter 不直接依赖 `jsonlLog` → Task 3、Task 5 `runModelAuthCheck`。
- session 级锁 → Task 4 `getSessionLock`。
- 旧模块删除 → Task 6。

**2. 占位符扫描：** 已检查，无 TODO/TBD/「稍后实现」/「类似 Task N」/省略号。每个代码块为完整可运行内容。

**3. 类型一致性：** `executeTool(definition, arguments, context, toolCallId)` 在 Task 2 定义，Task 4 `executeToolCall` 与 Task 5 `runToolRuntimeCheck` 调用签名一致；`loadToolConfig(configPath=, debugConsole=)` 在 Task 2 定义，Task 4 builder / Task 5 各处调用一致；`chatCompletionsAdapter(config, auth, debugConsole)` 与 `complete(messages, tools) -> modelCompletion` 在 Task 3 定义，Task 4 builder 与 fakeModel 调用一致；`agent(modelAdapter, toolDefinitions, workDir, logDir, debugConsole, maxModelSteps)` 在 Task 4 定义，Task 5 `buildFakeAgent` 调用一致；`pendingConfirm(sessionId, confirmationId, reason, toolCalls, currentIndex)` 字段在 Task 4 types 与 agent 使用一致。

**4. 验证完整性：** 每个任务都给出了确切运行命令、预期关键输出与 ✅ 完成条件；所有详细输出受 `--debug` 控制（库内 `debugConsole.debug()`，manualChecks `printDebug` 与 `debugConsole(args.debug)`）。

---

## 执行交接

计划已完成并保存到 `docs/flare/20260702_pureLibraryAgentRuntime.md`。两种执行选项：

1. **子代理驱动（推荐）** —— 我为每个任务分派一个全新的子代理，在任务之间进行复审，快速迭代。
2. **内联执行** —— 使用 executing-plans 在本会话中执行任务，带复审检查点的批处理。

选择哪种方式？
