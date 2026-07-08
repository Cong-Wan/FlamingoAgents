# Flamingo Agents 日志格式重构 实现计划

> **面向智能体工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 来逐任务实现此计划。步骤使用复选框（`- [ ]`）语法进行追踪。

**目标：** 把一次模型请求-回复从拆成 5 类记录重构为「一条 modelTurn（完整请求+完整回复）+ toolResult（完整内容）」两类记录，去掉工具内部截断和日志脱敏，使日志忠实记录发给 LLM 的真实内容。

**架构：** modelCompletion 对象本来就同时持有 request 和 response，合并它们即可。日志层不再用 makePreview 截断、不再用 redactText 脱敏。工具层去掉内部截断（read/edit/write 返回完整内容），bash 改用显式 maxOutput 参数控制输出体积。

**技术栈：** Python 3.12+，uv 管理，pyyaml；纯标准库（subprocess/json/dataclasses）。

**对应规范：** `docs/recipe/20260708_agentLogFormat_recipe.md`

---

## 文件结构

| 文件 | 操作 | 职责 | 版本 |
|---|---|---|---|
| `flamingoAgents/core/agent.py` | 修改 | 合并 modelRequest+modelResponse 为 modelTurn | 1.5 → 1.6 |
| `flamingoAgents/core/conversation.py` | 修改 | addMessage 不记日志；addToolResult 存完整内容 | 1.3 → 1.4 |
| `flamingoAgents/tools/builtinTools.py` | 修改 | 去工具内部截断；bash 加 maxOutput 参数 | 1.0 → 1.1 |
| `flamingoAgents/utils/jsonl.py` | 修改 | 去脱敏、删死方法 logPreviewEvent | 1.2 → 1.3 |
| `flamingoAgents/utils/preview.py` | 修改 | 删 makePreview/previewLimit，保留 toJsonable | 1.0 → 1.1 |
| `flamingoAgents/utils/redaction.py` | 删除 | 脱敏逻辑，删除后无引用 | — |

**任务依赖：** Task 1-4 改动互相独立（不同文件）。Task 5（删除/清理）依赖 Task 1-4 完成（必须先确保无残留引用才能删除）。Task 6（端到端）依赖 Task 5。

**执行边界：** Task 1-5 由 SDD 自动批次执行（含 finalReview）；Task 6 依赖真实 API key + 网络且输出非确定，必须由人工手动执行，不纳入自动批次。

---

## Task 1: agent.py — 合并 modelTurn

**目标：** `continueModelLoop` 中把原先分开写入的 `modelRequest` 和 `modelResponse` 合并为一条 `modelTurn` 记录，同时包含完整 request 和完整 response。

**涉及的文件：**

- `flamingoAgents/core/agent.py` — Agent 协调核心，记录模型交互

---

#### Step 1 — 实现

修改 `flamingoAgents/core/agent.py` 的文件头，版本 1.5 → 1.6：

```python
'''
Author: wilbur
Version: 1.6
Date: 2026-07-08
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state.
'''
```

在 `continueModelLoop` 方法中，找到这段代码（约第 106-110 行）：

```python
            requestPayload = getattr(completion, 'requestPayload', None)
            responsePayload = getattr(completion, 'responsePayload', None)
            if isinstance(requestPayload, dict):
                currentConversation.logger.logEvent({'type': 'modelRequest', 'request': requestPayload})
            if isinstance(responsePayload, dict):
                currentConversation.logger.logEvent({'type': 'modelResponse', 'response': responsePayload})
```

替换为：

```python
            requestPayload = getattr(completion, 'requestPayload', None)
            responsePayload = getattr(completion, 'responsePayload', None)
            if isinstance(requestPayload, dict) and isinstance(responsePayload, dict):
                currentConversation.logger.logEvent({
                    'type': 'modelTurn',
                    'request': requestPayload,
                    'response': responsePayload,
                })
```

注意：`logModelError` 方法（约第 203 行）保持不动，它已经是「request + 错误信息」一条记录，符合精神。

---

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/core/agent.py
# 预期：无任何输出（语法正确）
```

---

✅ **完成的标志：** py_compile 无输出无报错。打开 `agent.py` 确认 `continueModelLoop` 中只写一条 `modelTurn` 事件，`logModelError` 未被改动。

---

## Task 2: conversation.py — 日志精简 + toolResult 完整化

**目标：** `addMessage` 不再写任何日志（消息状态照常保留供下一轮请求使用）；`addToolResult` 去掉 Preview 截断字段，直接存完整的 content 和 details。

**涉及的文件：**

- `flamingoAgents/core/conversation.py` — 会话状态与日志镜像

---

#### Step 1 — 实现

修改 `flamingoAgents/core/conversation.py` 的文件头，版本 1.3 → 1.4：

```python
'''
Author: wilbur
Version: 1.4
Date: 2026-07-08
Description: Maintains per-session conversation state (messages, session lock, pending confirmation). Messages are kept for the next model request; only tool results are mirrored to JSONL logs.
'''
```

移除 import 行（删除这一行）：

```python
from flamingoAgents.utils.preview import makePreview
```

把 `addMessage` 方法整体替换为（从带 assistant/toolCalls 分支判断，简化为只保留消息状态）：

```python
    def addMessage(self, message: chatMessage) -> None:
        self.messages.append(message)
```

把 `addToolResult` 方法整体替换为：

```python
    def addToolResult(self, result: toolResult) -> None:
        self.logger.logEvent({
            'type': 'toolResult',
            'toolCallId': result.toolCallId,
            'toolName': result.toolName,
            'isError': result.isError,
            'content': result.content,
            'details': result.details,
        })
        self.messages.append(chatMessage(
            role='tool',
            content=result.content,
            toolCallId=result.toolCallId,
            name=result.toolName,
        ))
```

---

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/core/conversation.py
# 预期：无任何输出（语法正确）
```

---

✅ **完成的标志：** py_compile 无输出无报错。确认 conversation.py 中已无 `makePreview` 引用，`addMessage` 只剩两行，`addToolResult` 的事件 dict 含 `content`/`details` 而非 `contentPreview`/`detailsPreview`。

---

## Task 3: builtinTools.py — 去截断 + bash maxOutput

**目标：** read/write/edit 工具去掉内部 makePreview 截断，返回完整内容；bash 工具新增 `maxOutput` 参数（default 2000，-1 不截断）控制 stdout/stderr 体积。

**涉及的文件：**

- `flamingoAgents/tools/builtinTools.py` — read/write/edit/bash 四个内置工具

---

#### Step 1 — 实现

修改文件头，版本 1.0 → 1.1：

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Defines built-in callable tools for file read/write/edit and bash execution.
'''
```

移除 import 行（删除这一行）：

```python
from flamingoAgents.utils.preview import makePreview
```

**改动 A — `readTool` 函数整体替换为：**

```python
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
```

**改动 B — `writeTool` 函数整体替换为：**

```python
def writeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    path = resolveSafePath(str(arguments['path']), context.workDir)
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
```

**改动 C — `editTool` 的返回部分替换。** 找到 `editTool` 函数末尾这段：

```python
    path.write_text(updatedContent, encoding='utf-8')
    previewText, truncated = makePreview(diffText)
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具完成 path={path} diffChars={len(diffText)} truncated={truncated}')
    return toolOutput(
        content=previewText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits), 'diffTruncated': truncated},
    )
```

替换为：

```python
    path.write_text(updatedContent, encoding='utf-8')
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具完成 path={path} diffChars={len(diffText)}')
    return toolOutput(
        content=diffText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits)},
    )
```

**改动 D — `createBashTool` 的 schema 增加 maxOutput。** 找到 `createBashTool` 函数：

```python
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
```

替换为（properties 增加 maxOutput）：

```python
def createBashTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='bash',
        description='在工作目录中执行 bash 命令。curl、python、grep、open 均通过此工具执行。\n\n权限提示：删除类命令会请求用户确认。maxOutput 控制 stdout/stderr 保留字符数，默认 2000，-1 表示不截断。',
        parameters={
            'type': 'object',
            'properties': {
                'command': {'type': 'string'},
                'timeout': {'type': 'integer', 'minimum': 1, 'default': 30},
                'maxOutput': {'type': 'integer', 'minimum': -1, 'default': 2000},
            },
            'required': ['command'],
            'additionalProperties': False,
        },
        execute=bashTool,
        permissions=permissions or [],
        preview=previewBashTool,
    )
```

**改动 E — `bashTool` 函数整体替换为：**

```python
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
```

注意：`clip` 函数定义在 try 块之前，try 和 except 两个分支都能调用（同一函数作用域）。`previewBashTool`、`decodeProcessText`、`resolveSafePath`、`maxTimeoutSeconds`、`defaultTimeoutSeconds` 保持不动。

---

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/tools/builtinTools.py
# 预期：无任何输出（语法正确）

$ uv run python -c "from flamingoAgents.tools.builtinTools import createBashTool; d=createBashTool(); import json; print(json.dumps(d.parameters, ensure_ascii=False, indent=2))"
# 预期：打印的 schema 中包含 "maxOutput" 字段，minimum 为 -1，default 为 2000
```

---

✅ **完成的标志：** py_compile 无输出；第二条命令打印的 schema 含 `maxOutput` 字段。确认 builtinTools.py 中已无 `makePreview` 引用。

---

## Task 4: jsonl.py — 去脱敏 + 删死方法

**目标：** `logEvent` 写入前不再调用 redactText 脱敏，忠实记录原始内容；删除无人调用的 `logPreviewEvent` 方法。

**涉及的文件：**

- `flamingoAgents/utils/jsonl.py` — JSONL 审计日志写入器

---

#### Step 1 — 实现

修改 `flamingoAgents/utils/jsonl.py` 文件头，版本 1.2 → 1.3：

```python
'''
Author: wilbur
Version: 1.3
Date: 2026-07-08
Description: Writes JSONL audit events faithfully without redaction or truncation.
'''
```

把整个文件替换为：

```python
'''
Author: wilbur
Version: 1.3
Date: 2026-07-08
Description: Writes JSONL audit events faithfully without redaction or truncation.
'''

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flamingoAgents.utils.preview import toJsonable


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
        with self.logPath.open('a', encoding='utf-8') as fileObj:
            fileObj.write(eventText + '\n')
```

说明：移除了 `makePreview` 和 `redactText` 两个 import，删除了 `logPreviewEvent` 方法，`logEvent` 不再调用 `redactText`。保留 `toJsonable`（用于把 dataclass 转成可序列化结构）。

---

#### Step 2 — 运行验证

```bash
$ uv run python -m py_compile flamingoAgents/utils/jsonl.py
# 预期：无任何输出（语法正确）
```

---

✅ **完成的标志：** py_compile 无输出。确认 jsonl.py 中已无 `redactText`、`makePreview`、`logPreviewEvent` 字样。

---

## Task 5: 删除 redaction.py + 清理 preview.py

**目标：** 删除已无引用的 `redaction.py`；从 `preview.py` 中删除 `makePreview` 和 `previewLimit`，只保留 `toJsonable`。

**前置依赖：** Task 1、2、3、4 必须全部完成（确保 makePreview、redactText 已无任何调用方）。

**涉及的文件：**

- `flamingoAgents/utils/redaction.py` — 删除
- `flamingoAgents/utils/preview.py` — 清理

---

#### Step 1 — 实现

**删除文件 `flamingoAgents/utils/redaction.py`：**

```bash
$ rm flamingoAgents/utils/redaction.py
```

**把 `flamingoAgents/utils/preview.py` 整个文件替换为：**

```python
'''
Author: wilbur
Version: 1.1
Date: 2026-07-08
Description: Provides JSON-safe conversion helper for logs.
'''

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


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
```

说明：删除了 `makePreview`、`previewLimit`、`json` import、`redactText` import。`toJsonable` 不依赖 json，所以 json import 也一并移除。

---

#### Step 2 — 运行验证

```bash
# 确认 redaction.py 已删除
$ ls flamingoAgents/utils/redaction.py
# 预期：报错 No such file or directory

# 全量语法编译
$ uv run python -m py_compile flamingoAgents/core/agent.py flamingoAgents/core/conversation.py flamingoAgents/tools/builtinTools.py flamingoAgents/utils/jsonl.py flamingoAgents/utils/preview.py
# 预期：无任何输出（全部语法正确）

# 确认无残留引用
$ rg -n "makePreview|previewLimit|redactText|from flamingoAgents.utils.redaction" flamingoAgents/ --type py
# 预期：无任何输出（所有引用已清除）

# 确认导入链完整（能正常 import 整个包）
$ uv run python -c "from flamingoAgents import createAgent; print('import OK')"
# 预期：打印 import OK

# 确认日志目录被 git 忽略（脱敏已删除，日志将明文记录，必须确保不会进版本库）
$ git check-ignore .agentLogs
# 预期：打印 .agentLogs（已被忽略）
```

---

✅ **完成的标志：** redaction.py 不存在；全量 py_compile 无输出；rg 搜索无 makePreview/previewLimit/redactText 残留；createAgent 能正常导入；`.agentLogs` 被 `git check-ignore` 确认忽略（脱敏删除后明文日志的安全保障）。

---

## Task 6: 端到端验证 — 跑 askModel.py 检查日志格式

> ⚠️ **人工关卡：** 本任务依赖真实 API key + 网络，且模型输出非确定，不可重复、无法在隔离子进程中稳定通过。**不纳入 SDD 自动批次**，由人工在 Task 1-5（含 finalReview）通过后手动执行。

**目标：** 执行真实会话，确认新生成的 `.jsonl` 日志只含 `modelTurn` 和 `toolResult` 两类记录，modelTurn 含完整 request+response，toolResult 含完整 content/details。

**前置依赖：** Task 5 完成。

**涉及的文件：**

- `askModel.py` — 端到端入口（不修改，仅运行）
- `.agentLogs/<新生成的日志>.jsonl` — 检查对象

---

#### Step 1 — 清理旧预览文件并运行

```bash
# 清理之前生成的预览文件，避免干扰（可选，保留旧会话日志无妨）
$ rm -f .agentLogs/preview_B1.jsonl .agentLogs/preview_B2.jsonl .agentLogs/preview_B2.json

# 记录运行前的日志文件，便于运行后定位新文件
$ ls .agentLogs/ > /tmp/before.txt

# 运行真实会话
$ uv run python askModel.py
# 预期：打印 "==== 会话状态 ====" sessionId、status: completed，以及 "==== 模型回复 ====" 跟一段模型对文档的总结
```

#### Step 2 — 检查日志格式

```bash
# 找到新生成的日志文件
$ ls -t .agentLogs/*.jsonl | head -1
# 预期：打印一个 20260708_session_xxxxxx.jsonl 文件名

# 检查类型分布（应该只有 modelTurn 和 toolResult）
$ LOG=$(ls -t .agentLogs/*.jsonl | head -1) && uv run python -c "
import json
from collections import Counter
types = Counter()
with open('$LOG', encoding='utf-8') as f:
    for line in f:
        line = line.strip()
        if line:
            types[json.loads(line)['type']] += 1
print('类型分布:', dict(types))
"
# 预期：{'modelTurn': N, 'toolResult': M}，绝对不能出现 message/modelRequest/modelResponse/toolCall

# 检查第一条 modelTurn 结构完整
$ LOG=$(ls -t .agentLogs/*.jsonl | head -1) && uv run python -c "
import json
with open('$LOG', encoding='utf-8') as f:
    for line in f:
        o = json.loads(line)
        if o['type'] == 'modelTurn':
            print('modelTurn 顶层字段:', sorted(o.keys()))
            print('request 字段:', sorted(o['request'].keys()))
            print('response 字段:', sorted(o['response'].keys()))
            break
"
# 预期：顶层含 request/response/timestamp/type；request 含 messages/model/tool_choice/tools；response 含 choices/usage 等

# 检查 toolResult 字段（应含完整 content/details，无 Preview 字段）
$ LOG=$(ls -t .agentLogs/*.jsonl | head -1) && uv run python -c "
import json
with open('$LOG', encoding='utf-8') as f:
    for line in f:
        o = json.loads(line)
        if o['type'] == 'toolResult':
            print('toolResult 字段:', sorted(o.keys()))
            print('有 contentPreview 吗:', 'contentPreview' in o)
            print('content 长度:', len(o.get('content','')))
            break
"
# 预期：字段含 type/timestamp/toolCallId/toolName/isError/content/details；无 contentPreview；content 长度大于 0
```

---

✅ **完成的标志：**
1. askModel.py 运行正常，status 为 completed。
2. 新日志类型分布只有 modelTurn 和 toolResult。
3. modelTurn 含完整 request（messages/model/tool_choice/tools）和 response（choices/usage 等）。
4. toolResult 含完整 content/details，无 contentPreview 字段。

---

## 自我复审记录

1. **规范覆盖：** recipe 中所有改动点（agent.py modelTurn、conversation.py 精简、builtinTools.py 去截断+maxOutput、jsonl.py 去脱敏+删死方法、删 redaction.py、preview.py 清理）均有对应任务（Task 1-5），端到端验证（Task 6）覆盖成功标准 1-6。recipe 验证部分误引用的 manualChecks.py 已修正为 askModel.py。

2. **占位符扫描：** 无 TODO/TBD，所有代码块为完整函数或完整替换段，无省略号。

3. **类型一致性：** toolResult 的 content/details 字段名与 `core/types.py` 中 toolResult dataclass 一致；modelTurn 的 request/response 字段名与 chatCompletions.py 的 modelCompletion.requestPayload/responsePayload 一致；bash maxOutput schema 字段名与 bashTool 中 arguments.get('maxOutput') 一致。

4. **验证完整性：** 每个 Task 都有 py_compile（Task 1-5）或运行命令（Task 6），关键输出有明确预期。删除类操作（Task 5）额外用 rg 确认无残留引用 + import 冒烟测试。
