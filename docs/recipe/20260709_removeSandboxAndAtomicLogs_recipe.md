'''
Author: wilbur
Version: 1.0
Date: 2026-07-09
Description: 清除文件工具沙箱 + 日志重构为 JSONL 原子事件（对齐 pi 的记录模型）。
'''

# 清除沙箱 + 日志重构为原子事件

## 背景

两个独立问题。

### 问题 1：文件工具沙箱拦截绝对路径

`flamingoAgents/tools/builtinTools.py` 的 `resolveSafePath()` 给 read/write/edit 三道防线：禁 `~`、禁绝对路径、禁 `../` 越界。这导致 agent 无法用绝对路径读写工作目录之外的文件。日志（`.agentLogs/20260709_session_1879e91f7db8.jsonl`）可复现：模型传绝对路径 `/Users/wilbur/project/FlamingoAgents/docs/addCallableToolFunction.md` 被 `ValueError` 拦截，被迫多绕一轮 `pwd` 探明工作目录。

要求：彻底清除沙箱，文件工具完全裸跑。

### 问题 2：日志重复且语义混乱

当前日志按「模型回合」记录，每轮写一条 `modelTurn`，内含完整 `request`（全量 messages 历史 + tools schema 数组）和完整 `response`。问题：

- **request.messages 跨轮累积重发**：第 N 轮把前 N-1 轮所有消息又带一遍进日志。
- **tools schema 每轮全量重发**：4 个工具定义每轮重复。
- **tool 结果重复**：`toolResult` 独立记一次，又在下一轮 `modelTurn.request.messages(tool role)` 里出现一次。
- **assistant 回复重复**：`modelTurn.response.message` 记一次，又在下一轮 `modelTurn.request.messages(assistant role)` 里出现一次。

这些重复不是 bug，是「日志 = 完整可重放 request 体」这个语义与「多轮对话自然累积」直接冲突造成的。OpenAI Chat Completions 协议要求每轮重发全量历史，这是内存对话状态的事，但把它原样落盘就造成了日志平方级膨胀。

### 对齐 pi 的记录模型

参考 pi（`@earendil-works/pi-coding-agent`，见 `docs/session-format.md` 与真实 session 文件）的做法：日志只记**增量原子事件**，每条信息只出现一次；要还原某轮真实请求时，由 `buildSessionContext()` 从消息树临时拼装——拼装发生在内存的请求时刻，不落盘。

Flamingo 采用同样的精神，但保留更简单的扁平内存结构（见下文「内存对话状态不动」）。

### 事实澄清：debug 输出已是控制台

`flamingoAgents/utils/debug.py` 的 `debugConsole.debug()` 当前实现就是 `print()` 到控制台，未写进 jsonl。所以「debug 输出走控制台」这条需求当前已满足，本次不改 debug 机制。

## 目标

1. 删除 `resolveSafePath`，read/write/edit 直接用原始路径，保留各自业务校验。
2. 删除 `modelTurn` 事件，日志重构为四类原子事件：`systemMessage`、`userMessage`、`assistantMessage`、`toolResult`。
3. 每条信息只出现一次，零重复。
4. 日志格式为 JSONL（每行一个独立 JSON 对象，`.jsonl` 扩展名）。
5. assistantMessage 完整保留模型回复信息：content + toolCalls + model + usage + timings。
6. systemMessage 在会话首条记录一次。

## 不在范围内

- 历史 `.jsonl` / `.json` 文件不迁移、不重写（包括 `.agentLogs/20260709_session_1879e91f7db8.json` 和 `.newFormat.jsonl`）。
- 内存对话状态（`conversation.messages` 扁平 list）不改——每轮照样拼全量发给 LLM，这是协议要求，与日志无关。
- `jsonl.py` 的追加写实现不动（已天然支持 JSONL 追加）。
- `debugConsole` 机制不动（已是控制台输出）。
- `modelError` 事件不动（已是「request + 错误信息」一条记录，符合原子精神）。
- `docs/` 下的历史 recipe / codeReview / flare 文档不动。
- `bash` 工具不受沙箱改动影响（本来就没走 `resolveSafePath`）。

## 详细设计

### 文件 1：`flamingoAgents/tools/builtinTools.py`（1.1 → 1.2）

**删除整个 `resolveSafePath` 函数。**

read/write/edit 三处调用点改为直接用原始路径：

```python
# readTool（当前第 74 行）
path = Path(arguments['path'])
# 原：path = resolveSafePath(str(arguments['path']), context.workDir)

# writeTool（当前第 131 行）
path = Path(arguments['path'])

# editTool（当前第 186 行）
path = Path(arguments['path'])
```

**保留各自业务校验**（这些是工具自身语义，不是沙箱）：
- read：`if not path.exists() or not path.is_file()` → 文件不存在/不是普通文件。
- write：`path.parent.mkdir(parents=True, exist_ok=True)` 照旧。
- edit：`if not path.exists() or not path.is_file()` + oldText 唯一匹配 + edits 不重叠。

**writeTool 连带改动**：无。writeTool 的 debug 文案为 `f'写入工具开始 path={path} bytes=...'`，本就不引用 `context.workDir`，删除 `resolveSafePath` 后无连带的 workDir 引用需要处理。

**bash 不受影响**：bash 工具用 `context.workDir` 是给子进程设 `cwd`（第 268/278 行），与路径沙箱无关，保持不动。

**改动后行为**：agent 可用绝对路径、`~`、`../`、任意路径读写——完全裸跑。

### 文件 2：`flamingoAgents/core/conversation.py`（1.4 → 1.5）

集中管理消息追加 + 日志写入。新增三个方法记录原子事件，`addToolResult` 不变，`addMessage` 收窄用途。

**新增 `appendSystemMessage`**（会话首条，只记一次）：

```python
def appendSystemMessage(self, content: str) -> None:
    self.logger.logEvent({'type': 'systemMessage', 'content': content})
    self.messages.append(chatMessage(role='system', content=content))
```

**新增 `appendUserMessage`**：

```python
def appendUserMessage(self, content: str) -> None:
    self.logger.logEvent({'type': 'userMessage', 'content': content})
    self.messages.append(chatMessage(role='user', content=content))
```

**新增 `appendAssistantMessage`**（含完整模型回复信息）：

```python
def appendAssistantMessage(self, message: chatMessage, responsePayload: dict) -> None:
    self.logger.logEvent({
        'type': 'assistantMessage',
        'model': responsePayload.get('model'),
        'content': message.content,
        'toolCalls': toJsonable(message.toolCalls),
        'usage': responsePayload.get('usage'),
        'timings': responsePayload.get('timings'),
    })
    self.messages.append(message)
```

`toolCalls` 用 `toJsonable` 序列化（dataclass → dict），保证 JSON 安全。需要新增导入：

```python
from flamingoAgents.utils.preview import toJsonable
```

**`addToolResult` 不变**（已符合原子事件形态）。

**删除 `addMessage`**：当前 `addMessage` 共 3 个调用点（conversation:24 的 system、agent:63 的 user、agent:115 的 assistant），三个调用点全部改为对应的新方法后，`addMessage` 不再有任何引用，直接删除整个方法。

`__init__` 里加 system 消息改用 `appendSystemMessage`：

```python
# __init__（当前第 24 行）
# 原：self.addMessage(chatMessage(role='system', content=systemPrompt))
self.appendSystemMessage(systemPrompt)
```

**`addToolResult` 的两件事分离说明**：当前 `addToolResult` 同时做「写日志」和「追加内存对话历史」两件事，A 方案下保持不变——因为内存历史（发给 LLM 用）和日志（落盘）是两件事，toolResult 作为内存消息照常进 `self.messages`，作为日志照常单独写一条。这不属于「重复」：内存历史是协议要求每轮重发的，日志只记一次。

### 文件 3：`flamingoAgents/core/agent.py`（1.6 → 1.7）

**`__init__` 里的 system 追加**：已移到 `conversation.__init__` 的 `appendSystemMessage`，agent 侧无需再处理 system 日志（system 在 conversation 创建时即记录）。

**`runUserMessage`（当前第 63 行）**：

```python
# 原：
currentConversation.addMessage(chatMessage(role='user', content=cleanMessage))

# 改为：
currentConversation.appendUserMessage(cleanMessage)
```

**`continueModelLoop`（当前第 106-115 行）**：删除 modelTurn 写盘块，改为 appendAssistantMessage。

原代码：

```python
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
```

改为：

```python
responsePayload = getattr(completion, 'responsePayload', None)
currentConversation.appendAssistantMessage(
    completion.message,
    responsePayload if isinstance(responsePayload, dict) else {},
)

assistantMessage = completion.message
```

不再保留 `requestPayload` 引用（丢弃，不落盘）。

**`logModelError`（当前第 217 行）不动**：保持「request + 错误信息」一条记录。注意此处 request 仍来自 `error.requestPayload`，是异常诊断所需，不在「日志去重」的语义范围内。

### 改动后的日志时间轴

一次「用户消息 → 多轮工具调用 → 最终回复」的会话：

```
[systemMessage]      系统提示词（会话首条一次）
[userMessage]        用户原话
[assistantMessage]   模型回复(content + toolCalls + model + usage + timings)
[toolResult]         工具结果
[assistantMessage]   ...
[toolResult]         ...
[assistantMessage]   最终纯文本回复（toolCalls 为空数组）
```

对照样例见 `.agentLogs/20260709_session_1879e91f7db8.newFormat.jsonl`（基于同源旧日志用真实数据生成的格式示例，8 条事件零重复）。

## 成功标准

1. read/write/edit 删除 `resolveSafePath` 后，传入绝对路径、`~`、`../` 均不再被拦截，能正常读写。
2. `resolveSafePath` 函数体从 `builtinTools.py` 中消失，无残留引用（grep `resolveSafePath` 无结果，除 `__pycache__`）。
3. 一次完整会话的日志只有 `systemMessage` / `userMessage` / `assistantMessage` / `toolResult` 四种 type，无 `modelTurn`。
4. 每条信息只出现一次：assistant 回复、tool 结果、system/user 消息均不在其他事件里重复。
5. `assistantMessage` 含字段 `type/model/content/toolCalls/usage/timings`；最终纯文本回复的 `toolCalls` 为空数组 `[]`。
6. `systemMessage` 在会话首条记录一次。
7. 日志为合法 JSONL：每行一个独立 JSON 对象，可逐行 `json.loads` 解析，文件扩展名 `.jsonl`。
8. 日志不再含 `request.messages` 历史和 `tools` schema 数组。

## 验证方式

项目不使用测试框架，按 `docs/addCallableToolFunction.md` 的手动验证规范：

```bash
uv run python -m py_compile flamingoAgents/tools/builtinTools.py \
  flamingoAgents/core/conversation.py flamingoAgents/core/agent.py
uv run python askModel.py --debug
```

跑完后检查新生成的 `.agentLogs/*.jsonl`：

```bash
# 验证 type 分布，应只有四种，无 modelTurn
python3 -c "
import json
lines = open('<新生成的 .jsonl>').readlines()
print('行数:', len(lines))
for line in lines:
    print(json.loads(line)['type'])
"

# 验证 JSONL 合法性：每行独立可解析
python3 -c "
import json
for line in open('<新生成的 .jsonl>'):
    json.loads(line)
print('JSONL 合法')
"

# 验证沙箱已清
grep -rn "resolveSafePath" flamingoAgents/ --include="*.py"   # 应无输出
```

并用绝对路径测一次 read 能成功（如让模型读 `/Users/wilbur/project/FlamingoAgents/docs/addCallableToolFunction.md`，应不再报路径错误）。
