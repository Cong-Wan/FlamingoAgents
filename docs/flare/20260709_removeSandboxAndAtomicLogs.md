# 清除沙箱 + 日志重构为原子事件 实现计划

> **面向智能体工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 来逐任务实现此计划。步骤使用复选框（`- [ ]`）语法进行追踪。

**目标：** 删除文件工具的路径沙箱校验，使 read/write/edit 接受任意路径（`~`/绝对路径/`../`，相对路径锚定工作目录）而不再抛 `ValueError`；并把按「模型回合」记录、含大量重复历史的 `modelTurn` 日志，重构为对齐 pi 的零重复原子事件（`systemMessage`/`userMessage`/`assistantMessage`/`toolResult`）。

**架构：** 需求 1 动 `builtinTools.py`（删 `resolveSafePath`，三处工具改为直接解析路径：`expanduser` 展开 `~`、绝对路径直用、相对路径锚定 `context.workDir`，与 `bashTool` 的 `cwd=context.workDir` 对齐）与 `askModel.py`（启用 `--debug`，使诊断输出真正生效——这是 Task 1/2 验证观察「记录...」诊断行的前提；现状 `--debug` 被 argv 静默忽略）。需求 2 动 `conversation.py`（新增三个 append 方法负责「写日志 + 追加内存」，删除 `addMessage`）和 `agent.py`（调用新方法、删 `modelTurn` 写盘）。两个任务改动的文件集不重叠（`{builtinTools.py, askModel.py}` vs `{conversation.py, agent.py}`），实现层可并行。内存对话状态（扁平 list）不动——每轮照常拼全量发给 LLM 是协议要求，与日志去重无关。

**技术栈：** Python 3.13、uv、无第三方依赖新增、无测试框架（手动验证）。

---

## 文件结构

| 文件 | 职责 | 本次改动 |
|------|------|----------|
| `flamingoAgents/tools/builtinTools.py` | 内置工具函数 read/write/edit/bash + factory map | 删 `resolveSafePath`；read/write/edit 改为直接解析路径（`expanduser` + 绝对直用 / 相对锚定 `workDir`） |
| `askModel.py` | 手动验证入口脚本 | 解析 `--debug` 并传入 `createAgent`，使 `--debug` 真正生效（Task 1/2 验证前提） |
| `flamingoAgents/core/conversation.py` | 会话状态 + 日志写入 | 新增 `appendSystemMessage`/`appendUserMessage`/`appendAssistantMessage`（带 debug 输出）；删 `addMessage`；`__init__` 接收 `debugConsole` |
| `flamingoAgents/core/agent.py` | Agent 协调循环 | `runUserMessage`/`continueModelLoop` 改用新 append 方法；删 `modelTurn` 写盘；`getConversation` 传入 `debugConsole`；删未使用的 `chatMessage` 导入 |

两个任务改动文件无交集，可并行。

---

## Task 1: 清除文件工具路径沙箱

**目标：** 删除 `resolveSafePath` 函数及其沙箱校验，使 read/write/edit 接受任意路径（绝对路径、`~`、`../`），不再抛 `ValueError`；保留各工具原有的业务校验（文件不存在、oldText 唯一匹配等）。

**路径解析语义（三处工具统一）：** `rawPath = Path(arguments['path']).expanduser()` 展开 `~`；若为绝对路径则直接使用，否则相对 `context.workDir` 解析——与 `bashTool`（`cwd=context.workDir`）的相对路径基点保持一致。旧沙箱「拒绝 `~`/绝对/越界」的校验全部移除。

> 裸 `Path(arguments['path'])` 并不能支持 `~`（实测 `Path('~/x')` 不展开，且会相对进程 cwd 解析），故必须显式 `expanduser()` 并锚定 `workDir`。

**涉及的文件：**

- `flamingoAgents/tools/builtinTools.py` — 内置工具函数
- `askModel.py` — 手动验证入口脚本（使其 `--debug` 真正生效，供 Task 1/2 验证观察诊断输出）

---

### Step 1 — 实现

#### 1.1 更新文件头（1.1 → 1.2）

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-09
Description: Defines built-in callable tools for file read/write/edit and bash execution. File tools resolve paths directly (~ expanded via expanduser, absolute honored, relative resolved against the working directory) without sandbox validation.
'''
```

#### 1.2 readTool 直接解析路径（当前第 74 行）

```python
# 原：
    path = resolveSafePath(str(arguments['path']), context.workDir)
# 改为：
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
```

#### 1.3 writeTool 直接解析路径（当前第 131 行）

```python
# 原：
    path = resolveSafePath(str(arguments['path']), context.workDir)
# 改为：
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
```

#### 1.4 editTool 直接解析路径（当前第 186 行）

```python
# 原：
    path = resolveSafePath(str(arguments['path']), context.workDir)
# 改为：
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
```

#### 1.5 删除 `resolveSafePath` 函数（当前第 329-341 行）

删除整个函数定义：

```python
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
```

删除后该函数在文件中无任何引用（read/write/edit 已改为直接解析路径）。`Path` 导入（第 12 行 `from pathlib import Path`）保留——三处工具仍在使用。

#### 1.6 启用 askModel.py 的 `--debug`（当前 main() 未传 debug、未解析 argv）

**问题：** `askModel.py` 调用 `createAgent(projectDir)` 时未传 `debug=True`，且无 `argv` 解析，故命令行 `--debug` 被静默忽略，`debugConsole.isDebug` 恒为 `False`，所有诊断输出都不会打印。Task 2 新增的 `appendXxxMessage` 诊断行（以及 Task 1/2 验证里依赖的 `--debug`）都要求此处先修好。

文件头（1.1 → 1.2；源文件 `Version` 字段现为 1.0，但 description 已记到 v1.1，按 description 历史 +1 至 1.2）：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-09
Description: 导入 Flamingo，向大模型发起一次请求，让它阅读 docs/flamingoAgentsFlow.md 并打印模型的回复。
            v1.1 调大 maxModelSteps，避免大文件分多次读取时超过默认 8 步上限。
            v1.2 解析 --debug 并传入 createAgent，使诊断输出真正生效。
'''
```

新增 `argparse` 导入与 `parseArgs`（放在 import 区、`main` 之前）：

```python
# 原：
from pathlib import Path

from flamingoAgents import createAgent
# 改为：
import argparse
from pathlib import Path

from flamingoAgents import createAgent


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='向大模型发起一次请求并打印回复。')
    parser.add_argument('--debug', action='store_true', help='开启诊断输出。')
    return parser.parse_args()
```

`main()` 改为读取并传入 `debug`：

```python
# 原：
def main() -> None:
    projectDir = Path(__file__).resolve().parent
    flamingo = createAgent(projectDir)
# 改为：
def main() -> None:
    args = parseArgs()
    projectDir = Path(__file__).resolve().parent
    flamingo = createAgent(projectDir, debug=args.debug)
```

---

### Step 2 — 运行验证

```bash
# 验证 1：构建通过
uv run python -m py_compile flamingoAgents/tools/builtinTools.py askModel.py
# 预期：无输出，无报错

# 验证 2：源码中 resolveSafePath 彻底消失
grep -rn "resolveSafePath" flamingoAgents/ --include="*.py"
# 预期：无输出（__pycache__ 的 .pyc 已通过 --include="*.py" 排除）

# 验证 3：运行无异常 + 关键输出（绝对路径能读，不再报 ValueError）
uv run python askModel.py --debug
# 预期：模型直接读取绝对路径 /Users/wilbur/project/FlamingoAgents/docs/addCallableToolFunction.md，
#       不再出现「路径必须是工作目录内的相对路径」的 ValueError，最终输出文件内容摘要。
#       （此时 --debug 已生效：会打印「装配 Agent」「读取工具开始/完成」等行；日志仍为旧 modelTurn 格式，属正常，日志重构在 Task 2。）
```

✅ **完成标志：** 上述三条命令全部符合预期——py_compile 无报错、grep 无输出、askModel.py 运行不再报路径错误且输出文件摘要。

---

## Task 2: 日志重构为原子事件

**目标：** 删除 `modelTurn` 事件，把日志改为四类零重复原子事件 `systemMessage`/`userMessage`/`assistantMessage`/`toolResult`；assistantMessage 完整保留 content+toolCalls+model+usage+timings；每个新增 append 方法带 `--debug` 控制的诊断输出。

**涉及的文件：**

- `flamingoAgents/core/conversation.py` — 会话状态 + 日志写入
- `flamingoAgents/core/agent.py` — Agent 协调循环

> 本任务必须同时改完两个文件后才能运行验证（conversation 删了 `addMessage`，agent 若还调用会 AttributeError）。
>
> **路径自洽说明（已核实，无需改动这些路径）：** `addToolResult` 方法保持不变，因此所有调用它的路径都自洽：`continueConfirmation`（agent.py:84 权限确认恢复后追加工具结果）、`processToolBatch`（agent.py:137 未知工具、:163 正常工具执行）。这些路径不涉及 `addMessage`，不受本次重构影响。`logModelError`（agent.py:205）直接调用 `logger.logEvent`，不依赖任何被删除的方法。

---

### Step 1 — 实现

#### 2.1 用以下完整内容覆盖 `flamingoAgents/core/conversation.py`（1.4 → 1.5）

```python
'''
Author: wilbur
Version: 1.5
Date: 2026-07-09
Description: Maintains per-session conversation state (messages, session lock, pending confirmation). Messages are appended as atomic log events (systemMessage/userMessage/assistantMessage/toolResult); in-memory list is kept for the next model request.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock

from flamingoAgents.core.types import chatMessage, pendingConfirm, toolResult
from flamingoAgents.utils.jsonl import jsonlLog


class conversation:
    def __init__(self, sessionId: str, logPath: Path, systemPrompt: str, debugConsole=None):
        self.sessionId = sessionId
        self.logger = jsonlLog(logPath)
        self.messages: list[chatMessage] = []
        self.lock = RLock()
        self.pending: pendingConfirm | None = None
        self.debugConsole = debugConsole
        self.appendSystemMessage(systemPrompt)

    def hasPending(self) -> bool:
        return self.pending is not None

    def setPending(self, pending: pendingConfirm) -> None:
        self.pending = pending

    def takePending(self) -> pendingConfirm | None:
        pending = self.pending
        self.pending = None
        return pending

    def appendSystemMessage(self, content: str) -> None:
        if self.debugConsole:
            self.debugConsole.debug(f'记录 systemMessage chars={len(content)}')
        self.logger.logEvent({'type': 'systemMessage', 'content': content})
        self.messages.append(chatMessage(role='system', content=content))

    def appendUserMessage(self, content: str) -> None:
        if self.debugConsole:
            self.debugConsole.debug(f'记录 userMessage chars={len(content)}')
        self.logger.logEvent({'type': 'userMessage', 'content': content})
        self.messages.append(chatMessage(role='user', content=content))

    def appendAssistantMessage(self, message: chatMessage, responsePayload: dict) -> None:
        toolCallCount = len(message.toolCalls)
        if self.debugConsole:
            self.debugConsole.debug(
                f'记录 assistantMessage contentChars={len(message.content)} '
                f'toolCalls={toolCallCount} model={responsePayload.get("model")}'
            )
        self.logger.logEvent({
            'type': 'assistantMessage',
            'model': responsePayload.get('model'),
            'content': message.content,
            'toolCalls': message.toolCalls,
            'usage': responsePayload.get('usage'),
            'timings': responsePayload.get('timings'),
        })
        self.messages.append(message)

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

说明：
- 无需新增 `toJsonable` 导入：`toolCalls` 直接传 `message.toolCalls`（dataclass 列表），由 `jsonlLog.logEvent` 内部的 `toJsonable` 递归序列化为 JSON 安全结构，无需在此重复转换。
- `__init__` 新增 `debugConsole` 参数并存储。
- `addMessage` 整个删除——原 3 个调用点（conversation:24 的 system、agent:63 的 user、agent:115 的 assistant）全部在 Step 2.2/2.3 改为新方法，改完后无引用。
- `addToolResult` 保持不变（工具执行的 debug 已在 `toolRuntime.py` 和 `agent.py` 充分覆盖，不重复添加）。

#### 2.2 `flamingoAgents/core/agent.py` 文件头（1.6 → 1.7）

```python
'''
Author: wilbur
Version: 1.7
Date: 2026-07-09
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state. Model turns are logged as atomic events (systemMessage/userMessage/assistantMessage/toolResult) instead of full request/response payloads.
'''
```

#### 2.3 删除未使用的 `chatMessage` 导入（当前第 18 行）

`runUserMessage` 改用 `appendUserMessage(cleanMessage)` 后，`chatMessage` 在 agent.py 中不再被构造，导入变为未使用。

```python
# 原：
from flamingoAgents.core.types import chatMessage, pendingConfirm, runResult, toolCall, toolContext, toolResult
# 改为：
from flamingoAgents.core.types import pendingConfirm, runResult, toolCall, toolContext, toolResult
```

#### 2.4 `runUserMessage` 改用 appendUserMessage（当前第 63 行）

```python
# 原：
            currentConversation.addMessage(chatMessage(role='user', content=cleanMessage))
            return self.continueModelLoop(realSessionId)
# 改为：
            currentConversation.appendUserMessage(cleanMessage)
            return self.continueModelLoop(realSessionId)
```

#### 2.5 `continueModelLoop` 删除 modelTurn 写盘，改用 appendAssistantMessage（当前第 105-116 行）

```python
# 原：
            requestPayload = getattr(completion, 'requestPayload', None)
            responsePayload = getattr(completion, 'responsePayload', None)
            if isinstance(requestPayload, dict) and isinstance(responsePayload, dict):
                currentConversation.logger.logEvent({
                    'type': 'modelTurn',
                    'request': requestPayload,
                    'response': responsePayload,
                })

            assistantMessage = completion.message
            currentConversation.addMessage(assistantMessage)
            if not assistantMessage.toolCalls:
# 改为：
            responsePayload = getattr(completion, 'responsePayload', None)
            assistantMessage = completion.message
            currentConversation.appendAssistantMessage(
                assistantMessage,
                responsePayload if isinstance(responsePayload, dict) else {},
            )
            if not assistantMessage.toolCalls:
```

`requestPayload` 不再保留（丢弃，不落盘）。`logModelError`（当前第 205 行）保持不动——它从 `error.requestPayload` 取请求体用于异常诊断，不在「日志去重」语义范围内。

#### 2.6 `getConversation` 创建 conversation 时传入 debugConsole（当前第 241 行）

```python
# 原：
            newConversation = conversation(sessionId=sessionId, logPath=logPath, systemPrompt=systemPrompt)
            self.conversations[sessionId] = newConversation
            return newConversation
# 改为：
            newConversation = conversation(
                sessionId=sessionId,
                logPath=logPath,
                systemPrompt=systemPrompt,
                debugConsole=self.debugConsole,
            )
            self.conversations[sessionId] = newConversation
            return newConversation
```

---

### Step 2 — 运行验证

```bash
# 验证 1：构建通过
uv run python -m py_compile flamingoAgents/core/conversation.py flamingoAgents/core/agent.py
# 预期：无输出，无报错

# 验证 2：运行无异常（需 Task 1 已完成，绝对路径能读，整个会话跑通）
uv run python askModel.py --debug
# 预期：完整跑完，控制台 --debug 下能看到「记录 systemMessage」「记录 userMessage」
#       「记录 assistantMessage ... toolCalls=1」等诊断行，最终输出文件内容摘要

# 验证 3：关键输出——新日志格式符合原子事件设计
LATEST=$(ls -t .agentLogs/*.jsonl | head -1)   # newFormat 示例是 .json，不会进 *.jsonl，无需过滤
echo "检查文件: $LATEST"
python3 -c "
import json
lines = open('$LATEST').readlines()
types = [json.loads(l)['type'] for l in lines]
print('type 序列:', types)
assert 'modelTurn' not in types, '失败：仍存在 modelTurn！'
assert types[0] == 'systemMessage', '失败：首条不是 systemMessage！'
assert 'userMessage' in types, '失败：缺少 userMessage！'
assert 'assistantMessage' in types, '失败：缺少 assistantMessage！'
for l in lines:
    json.loads(l)  # 每行独立可解析 = 合法 JSONL
# 检查 assistantMessage 字段完整性
am = [json.loads(l) for l in lines if json.loads(l)['type'] == 'assistantMessage'][0]
assert set(['type','model','content','toolCalls','usage','timings']).issubset(am.keys()), 'assistantMessage 字段不全'
print('✓ JSONL 合法，无 modelTurn，四类原子事件齐全，assistantMessage 字段完整')
"
# 预期：打印出 type 序列如 ['systemMessage','userMessage','assistantMessage','toolResult',...,'assistantMessage']，
#       末尾断言通过打印 ✓ 行
```

✅ **完成标志：** py_compile 无报错；askModel.py --debug 完整跑通且控制台出现新的「记录 ...」诊断行；验证 3 脚本断言全部通过、打印 ✓ 行。

---

## 不在范围内（重申）

- 历史 `.jsonl`/`.json` 不迁移、不重写（含 `.agentLogs/20260709_session_1879e91f7db8.json` 与 `.newFormat.json` 示例）。
- 内存对话状态（扁平 list）不改。
- `jsonl.py` 追加写实现不改。
- `debugConsole` 机制不改。
- `modelError` 事件不改。
- `bash` 工具不改。
- `docs/` 下历史 recipe/codeReview/flare 文档不改。
- 不写测试文件，不编排 Git 操作。
