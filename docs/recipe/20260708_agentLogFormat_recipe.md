# 日志格式重构 + 工具输出完整性

## 背景

当前一次模型请求-回复被拆成 5 类日志记录，其中大量重复：

- `modelRequest`：完整请求体
- `modelResponse`：完整模型回复（含 assistant 文本、tool_calls、usage、timings）
- `message`（assistant）：assistant 文本 —— **已在 modelResponse 里**
- `toolCall`：每个工具调用的参数预览 —— **已在 modelResponse.tool_calls 里**
- `toolResult`：工具执行结果（唯一的新信息）

其中 `modelCompletion` 对象本来同时持有 request 和 response，却被拆成两条写。

另外，日志写入时存在两个问题：

1. `redactText` 脱敏会改写用户输入（把密钥模式替换成 `<redacted>`），破坏日志忠实性。
2. 工具内部（builtinTools.py）用 `makePreview` 把工具输出截断到 4000 字符，工具结果不完整。

## 目标

1. 请求体 + 完整模型回复合并为一条 `modelTurn` 记录。
2. 日志只保留 `modelTurn` + `toolResult` 两类记录，消除重复。
3. `toolResult` 存完整内容，去掉 Preview 字段。
4. 工具内部去掉截断（read/edit/write 返回完整内容；bash 用显式参数控制）。
5. 删除脱敏，日志忠实记录用户输入。

## 不在范围内

- `modelError` 事件（agent.py:203）已是「request + 错误信息」一条记录，符合精神，不动。
- 历史 `.jsonl` 文件不迁移、不重写。
- preview caching 行为：本次改动与缓存命中率无关（工具结果一旦产生即为固定字符串，后续轮次重发字节不变照样命中），不做任何缓存相关处理。

## 详细设计

### 文件 1：`flamingoAgents/core/agent.py`（1.5 → 1.6）

`continueModelLoop` 里第 108-110 行，两条合并成一条：

```python
# 原：
if isinstance(requestPayload, dict):
    currentConversation.logger.logEvent({'type': 'modelRequest', 'request': requestPayload})
if isinstance(responsePayload, dict):
    currentConversation.logger.logEvent({'type': 'modelResponse', 'response': responsePayload})

# 改为：
if isinstance(requestPayload, dict) and isinstance(responsePayload, dict):
    currentConversation.logger.logEvent({
        'type': 'modelTurn',
        'request': requestPayload,
        'response': responsePayload,
    })
```

`logModelError` 不动。

### 文件 2：`flamingoAgents/core/conversation.py`（1.3 → 1.4）

`addMessage` 不再写日志（消息状态照常保留供下一轮请求使用）：

```python
def addMessage(self, message: chatMessage) -> None:
    self.messages.append(message)
```

`addToolResult` 去掉 Preview 字段，直接存完整 content/details：

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

移除 `from flamingoAgents.utils.preview import makePreview` 导入。

### 文件 3：`flamingoAgents/tools/builtinTools.py`（1.0 → 1.1）

去掉所有工具内部的 `makePreview` 截断。

**readTool**：content 返回完整选中文本（已有 offset/limit 行级控制保护）。

```python
# 原：
previewText, previewTruncated = makePreview(selectedText)
return toolOutput(content=previewText, details={... 'truncated': truncated or previewTruncated})

# 改为：
return toolOutput(
    content=selectedText,
    details={
        'path': str(path), 'offset': offset, 'limit': limit,
        'totalLines': len(lines), 'truncated': truncated,
    },
)
```

**writeTool**：details 去掉 contentPreview/truncated。

```python
# 原：
previewText, truncated = makePreview(content)
return toolOutput(content=f'已写入文件：{path}', details={... 'contentPreview': previewText, 'truncated': truncated})

# 改为：
return toolOutput(
    content=f'已写入文件：{path}',
    details={'path': str(path), 'bytes': len(content.encode('utf-8'))},
)
```

**editTool**：content 返回完整 diff。

```python
# 原：
previewText, truncated = makePreview(diffText)
return toolOutput(content=previewText or '...', details={... 'diffTruncated': truncated})

# 改为：
return toolOutput(
    content=diffText or '文件内容未发生变化。',
    details={'path': str(path), 'editCount': len(edits)},
)
```

**bashTool**：新增 `maxOutput` 参数，控制 stdout/stderr 输出体积。

schema 增加：
```python
'maxOutput': {'type': 'integer', 'minimum': -1, 'default': 2000},
```

取值规则：
- `maxOutput == -1`：完全不截断。
- `maxOutput >= 0`：stdout 和 stderr 各最多保留 maxOutput 字符，超出截断。
- 未传时按 default 2000 处理。

执行逻辑（正常完成分支和超时分支统一）：

```python
maxOutput = int(arguments.get('maxOutput', 2000))

def clip(text):
    if maxOutput < 0 or len(text) <= maxOutput:
        return text, False
    return text[:maxOutput] + '\n<truncated>', True

stdoutText, stdoutTruncated = clip(completedProcess.stdout)
stderrText, stderrTruncated = clip(completedProcess.stderr)
return toolOutput(
    content=(
        f'exitCode: {completedProcess.returncode}\n'
        f'stdout:\n{stdoutText}\n'
        f'stderr:\n{stderrText}'
    ),
    isError=completedProcess.returncode != 0,
    details={
        'command': command, 'timeout': timeout, 'exitCode': completedProcess.returncode,
        'maxOutput': maxOutput,
        'stdoutTruncated': stdoutTruncated, 'stderrTruncated': stderrTruncated,
    },
)
```

移除 `from flamingoAgents.utils.preview import makePreview` 导入。

### 文件 4：`flamingoAgents/utils/jsonl.py`（1.2 → 1.3）

`logEvent` 去掉 `redactText` 脱敏：

```python
# 原：
eventText = json.dumps(eventToWrite, ensure_ascii=False, sort_keys=True)
safeText = redactText(eventText)
with self.logPath.open('a', encoding='utf-8') as fileObj:
    fileObj.write(safeText + '\n')

# 改为：
eventText = json.dumps(eventToWrite, ensure_ascii=False, sort_keys=True)
with self.logPath.open('a', encoding='utf-8') as fileObj:
    fileObj.write(eventText + '\n')
```

删除无人调用的死方法 `logPreviewEvent`。
移除 `from flamingoAgents.utils.preview import makePreview` 和 `from flamingoAgents.utils.redaction import redactText` 导入。

### 删除文件

- `flamingoAgents/utils/redaction.py`：脱敏逻辑，删除后无任何引用。
- `preview.py` 的 `makePreview` 和 `previewLimit`：删除后无任何引用。`toJsonable` 仍被 jsonl.py 使用，保留。

### preview.py 处理

删除 `makePreview` 和 `previewLimit` 后，`preview.py` 只剩 `toJsonable`。文件名不再贴切，但重命名会扩大改动范围，本次保留文件名，更新文件头说明。后续可独立整理。

## 改动后的日志时间轴

```
[modelTurn]     request(完整) + response(完整)
[toolResult]    完整 content + details
[modelTurn]
[toolResult]
[modelTurn]     最终纯文本回复
```

零重复，每条记录都是独立的新信息。

## 成功标准

1. 一次「用户消息 → 多轮工具调用 → 最终回复」的完整会话，日志里只有 `modelTurn` 和 `toolResult` 两种类型。
2. 每个 `modelTurn` 同时包含完整 request（model/messages/tools/tool_choice）和完整 response（choices/usage/timings）。
3. `toolResult` 字段为 `type/timestamp/toolCallId/toolName/isError/content/details`，无 Preview 字段。
4. 日志内容与实际发给 LLM 的请求体、工具真实返回值一致，无脱敏、无截断（除 bash 受 maxOutput 控制）。
5. read/edit/write 返回完整内容；bash 默认截断到 2000 字符，`maxOutput=-1` 时不截断。
6. `redaction.py` 删除，`preview.py` 只剩 `toJsonable`，无残留死代码引用。

## 验证方式

由于项目不使用测试框架，按 `docs/addCallableToolFunction.md` 的手动验证规范：

```bash
uv run python -m py_compile flamingoAgents/core/agent.py flamingoAgents/core/conversation.py flamingoAgents/tools/builtinTools.py flamingoAgents/utils/jsonl.py flamingoAgents/utils/preview.py
uv run python manualChecks.py all --debug
uv run python manualChecks.py all
```

并通过 askModel.py 跑一次真实会话，检查新生成的 `.agentLogs/*.jsonl` 格式符合上述时间轴。
