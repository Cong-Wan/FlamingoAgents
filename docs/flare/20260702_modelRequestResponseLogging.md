# 模型请求体与回复体完整记录 实现计划

> **面向智能体工作者：** 必需子技能：使用 subagent-driven-development（推荐）或 executing-plans 来逐任务实现此计划。步骤使用复选框（`- [ ]`）语法进行追踪。

**目标：** 让 Flamingo Agents 在每次调用模型时，把**完整的请求体（messages + tools + tool_choice）**和**完整的回复体（含 usage）**，以及**HTTP 错误响应体**，都完整写入 session 的 JSONL 日志，供事后用 `jq` 格式化审阅。

**架构：** 在 `openaiAdapter.complete()` 的三个时机——请求体构造后、响应体解析前、HTTP 错误发生时——通过一个注入的 per-session `jsonlLog` 写入完整 payload。因为 `jsonlLog` 是 per-session 的、而 `openaiAdapter` 是全局单例，所以 logger 不在 `__init__` 持有，而是由 `agent.continueModelLoop()` 每次调用 `complete()` 时把 `conversation.logger` 作为参数传进去。`logger` 参数设默认值 `None`，不破坏现有调用契约。

**技术栈：** Python 3.12+、标准库 `urllib`/`json`、`pyyaml`、`uv` 管理环境。

---

## 范围确认（已与用户拍板）

1. **tools schema 每轮完整记录**（方案 A）——日志自包含，接受文件变大。
2. **不加开关**——always 记录，不为想象中的需求加配置项。
3. **事件命名**：`modelRequest` / `modelResponse` / `modelResponseError`，与现有 `modelError` 风格一致。
4. **不新增任何控制台打印**——本次改动只做文件日志写入，不碰 `debugConsole`/`print`。

---

## 文件结构

本计划只修改 2 个既有文件，不新建任何生产代码文件。验证用脚本是一次性的，验证通过后删除。

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `flamingoAgents/models/openai.py` | 修改 | `complete()` 增加 `logger` 参数；在请求构造后、响应解析前、HTTP 错误时记录完整 payload |
| `flamingoAgents/core/agent.py` | 修改 | `continueModelLoop()` 调用 `complete()` 时传入 `conversation.logger` |
| `manualChecks.py` | 修改 | `fakeModel.complete()` 签名同步加 `logger=None`，避免调用元数变化导致 TypeError |

**为什么这样分**：`openaiAdapter` 是全局单例（`buildAgent` 只建一次），而 `jsonlLog` 是 per-session 的（`conversation` 里创建，一个 session 一个 `.jsonl`）。所以 logger 不能由 adapter 持有，必须每次调用时传入。改动落点恰好对应两个文件，职责清晰。

---

## Task 1: openaiAdapter 记录完整请求体、回复体与错误响应

**目标：** `openaiAdapter.complete()` 在请求体构造完成后记录 `modelRequest`（含完整 messages/tools/tool_choice），在响应解析为 dict 后、解析为 chatMessage 前记录 `modelResponse`（含完整 usage），在 HTTPError 时记录 `modelResponseError`（含完整错误体与状态码）。

**涉及的文件：**

- `flamingoAgents/models/openai.py` — OpenAI 兼容请求适配器，本次新增完整 payload 落盘能力

---

#### Step 1 — 实现

用以下完整内容**覆盖** `flamingoAgents/models/openai.py`（版本 1.1 → 1.2）：

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Converts internal messages and tools to OpenAI-compatible chat completion requests, and logs full request/response payloads through an injected per-session logger.
'''

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from flamingoAgents.core.types import chatMessage, modelConfig, toolCall
from flamingoAgents.utils.jsonl import jsonlLog


class openaiAdapter:
    def __init__(self, config: modelConfig, debugConsole=None):
        self.config = config
        self.debugConsole = debugConsole

    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]], logger: jsonlLog | None = None) -> chatMessage:
        apiKey = os.getenv(self.config.apiKeyEnv, '').strip()
        if not apiKey:
            raise RuntimeError(f'环境变量缺失：{self.config.apiKeyEnv}')

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
                'Authorization': f'Bearer {apiKey}',
                'Content-Type': 'application/json',
            },
        )
        if self.debugConsole:
            self.debugConsole.debug(
                f'调用模型 provider={self.config.provider} model={self.config.model} '
                f'messages={len(messages)} tools={len(tools)} url={requestUrl}'
            )
        if logger is not None:
            logger.logEvent({
                'type': 'modelRequest',
                'url': requestUrl,
                'model': self.config.model,
                'request': requestPayload,
            })
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                responseText = response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            errorText = error.read().decode('utf-8', errors='replace')
            if logger is not None:
                logger.logEvent({
                    'type': 'modelResponseError',
                    'status': error.code,
                    'body': errorText,
                })
            raise RuntimeError(f'模型请求失败：status={error.code} body={errorText[:1000]}') from error
        except urllib.error.URLError as error:
            raise RuntimeError(f'模型请求失败：{error.reason}') from error

        payload = json.loads(responseText)
        if logger is not None:
            logger.logEvent({
                'type': 'modelResponse',
                'model': self.config.model,
                'response': payload,
            })
        return self.parseAssistantPayload(payload)

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
        for index, rawCall in enumerate(rawToolCalls):
            functionValue = rawCall.get('function') or {}
            argumentsText = functionValue.get('arguments') or '{}'
            try:
                arguments = json.loads(argumentsText)
            except json.JSONDecodeError as error:
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 不是合法 JSON。') from error
            parsedToolCalls.append(toolCall(
                id=rawCall.get('id') or f'call_{index + 1}',
                toolName=functionValue.get('name') or '',
                arguments=arguments,
            ))

        content = rawMessage.get('content') or ''
        return chatMessage(role='assistant', content=content, toolCalls=parsedToolCalls)
```

**关键说明：**
- `logger: jsonlLog | None = None` —— 默认值 `None` 保证向后兼容（不传 logger 时不记录，行为同旧版）。
- 三个记录时机严格遵循"先落盘再继续"原则：`modelResponse` 写在 `json.loads` 之后、`parseAssistantPayload` 之前，这样**即使解析抛异常，完整响应也已落盘**。
- `logEvent` 内部已对整行做 `redactText`（脱敏 api_key/token/secret 等）。请求体本就不含 API key（key 只在 HTTP header），所以脱敏主要作用于消息内容里可能出现的密钥文本——这是期望行为。
- 导入 `jsonlLog` 无循环依赖：`utils/jsonl.py` 不导入任何 `flamingoAgents` 模块。
- **URLError 分支（DNS/连接拒绝/超时）有意不记录**：网络层错误无响应体，没有 body 可记；且 `agent.py` 的 `except Exception` 会补记一条 `modelError`，信息不丢失。这是与 HTTPError 的有意不对称。
- **HTTPError 时会出现两条事件**：complete 内先记 `modelResponseError`（status+body），随后 raise RuntimeError 被 agent 的 `except Exception` 再记一条 `modelError`（errorType=RuntimeError）。二者靠相邻时间戳关联，属期望行为，非重复 bug。

---

#### Step 2 — 运行验证

本任务用**纯净 mock 脚本**验证（monkeypatch `urllib.request.urlopen`），不依赖网络和真实 API key，可稳定复现。

在项目根目录创建一次性验证脚本 `verifyOpenaiLogging.py`：

```python
import json
import os
import urllib.request
import urllib.error
from io import BytesIO
from pathlib import Path

from flamingoAgents.core.types import chatMessage, modelConfig
from flamingoAgents.models.openai import openaiAdapter
from flamingoAgents.utils.jsonl import jsonlLog

os.environ['FAKE_API_KEY'] = 'sk-fake-test-key-1234567890'

cfg = modelConfig(
    provider='fake', model='m1', baseUrl='http://fake/v1',
    apiKeyEnv='FAKE_API_KEY', apiType='openaiCompatible',
)
adapter = openaiAdapter(cfg)

logPath = Path('verifyOpenaiLogging.jsonl')
if logPath.exists():
    logPath.unlink()
logger = jsonlLog(logPath)

# case 1：成功响应，应记录 modelRequest + modelResponse
successBody = json.dumps({
    'choices': [{'message': {'role': 'assistant', 'content': 'hi there'}}],
    'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
    'id': 'chatcmpl-success',
}).encode('utf-8')


class fakeResponse:
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


originalUrlopen = urllib.request.urlopen
urllib.request.urlopen = lambda req, timeout=60: fakeResponse(successBody)
result = adapter.complete(
    [chatMessage(role='user', content='hello')],
    [{'type': 'function', 'function': {'name': 'noop', 'description': 'd', 'parameters': {'type': 'object', 'properties': {}}}}],
    logger,
)
urllib.request.urlopen = originalUrlopen

# case 2：HTTP 错误，应记录 modelResponseError
def errorUrlopen(req, timeout=60):
    raise urllib.error.HTTPError(
        req.full_url, 429, 'Too Many Requests', {},
        BytesIO(b'{"error":{"message":"rate limited"}}'),
    )


urllib.request.urlopen = errorUrlopen
try:
    adapter.complete([chatMessage(role='user', content='x')], [], logger)
except RuntimeError:
    pass
urllib.request.urlopen = originalUrlopen

# 检查日志
events = [json.loads(line) for line in logPath.read_text(encoding='utf-8').strip().split('\n')]
types = [e['type'] for e in events]
print('记录的事件类型:', types)

assert 'modelRequest' in types, '缺少 modelRequest'
assert 'modelResponse' in types, '缺少 modelResponse'
assert 'modelResponseError' in types, '缺少 modelResponseError'

reqEvent = next(e for e in events if e['type'] == 'modelRequest')
assert 'tools' in reqEvent['request'], '请求体缺少 tools'
assert reqEvent['request']['tool_choice'] == 'auto', '请求体缺少 tool_choice'
assert reqEvent['request']['messages'][0]['content'] == 'hello', '请求体 messages 不完整'

respEvent = next(e for e in events if e['type'] == 'modelResponse')
assert respEvent['response']['usage']['total_tokens'] == 7, '响应体 usage 不完整'

errEvent = next(e for e in events if e['type'] == 'modelResponseError')
assert errEvent['status'] == 429, '错误状态码不完整'

print('✅ Task 1 验证通过：modelRequest / modelResponse / modelResponseError 三类事件均完整记录')
print('  assistant 内容:', result.content)
logPath.unlink()
```

先做一次纯 import 构造检查，再运行验证脚本：

```bash
$ uv run python -c "import flamingoAgents.models.openai, flamingoAgents.core.agent; print('import ok')"
# 预期：import ok（确认语法/导入无误）

$ uv run python verifyOpenaiLogging.py
# 预期：
# 记录的事件类型: ['modelRequest', 'modelResponse', 'modelRequest', 'modelResponseError']
# ✅ Task 1 验证通过：modelRequest / modelResponse / modelResponseError 三类事件均完整记录
#   assistant 内容: hi there
```

验证通过后删除一次性脚本：

```bash
$ rm verifyOpenaiLogging.py
```

---

✅ **完成的标志：** `import ok` 输出正常；`verifyOpenaiLogging.py` 打印 `✅ Task 1 验证通过` 且脚本退出码为 0（所有 assert 通过）。**在满足此条件之前不要开始下一个任务。**

---

## Task 2: agent 调用链把 per-session logger 传入 complete

**目标：** `agent.continueModelLoop()` 在调用 `modelAdapter.complete()` 时，把当前 session 的 `conversation.logger` 作为第三个参数传入，使 Task 1 新增的记录逻辑在生产路径上生效。

**涉及的文件：**

- `flamingoAgents/core/agent.py` — Agent 核心闭环，本次修改一处调用点
- `manualChecks.py` — 项目自带的 fake model，`complete()` 签名需同步加 `logger=None`

---

#### Step 1 — 实现

对 `flamingoAgents/core/agent.py` 做两处精确修改（版本 1.1 → 1.2）：

**修改 1：文件头**

```python
'''
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Coordinates model calls, tool execution, confirmation handling, sessions, and JSONL-backed conversations; passes per-session logger to the model adapter for full payload logging.
'''
```

**修改 2：`continueModelLoop` 里的调用点**

把：

```python
            try:
                assistantMessage = self.modelAdapter.complete(conversation.messages, self.registry.listModelTools())
            except Exception as error:
```

替换为：

```python
            try:
                assistantMessage = self.modelAdapter.complete(
                    conversation.messages,
                    self.registry.listModelTools(),
                    conversation.logger,
                )
            except Exception as error:
```

**修改 3：同步 manualChecks.py 的 fakeModel.complete 签名**

Task 2 把 `complete()` 改为 3 个位置参数后，`manualChecks.py` 的 fakeModel 仍是 2 参签名，会被多传一个 `conversation.logger` 而抛 `TypeError: complete() takes 3 positional arguments but 4 were given`，导致 `manualChecks.py agent` / `http` / `all` 全部跑不通。必须同步。

把 `manualChecks.py` 中：

```python
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> chatMessage:
```

替换为：

```python
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]], logger=None) -> chatMessage:
```

fakeModel 用不到 logger，故参数留空即可（不记录日志，与现有行为一致）。`manualChecks.py` 文件头 description 补一句"同步 complete logger 参数"并递增小版本。

**关键说明：**
- agent.py 只新增第三个位置参数 `conversation.logger`，其余逻辑不动。
- `conversation.logger` 是 `jsonlLog` 实例（在 `conversation.__init__` 中创建），与 Task 1 的 `logger: jsonlLog | None` 类型匹配。
- manualChecks.py 的 fakeModel 同样加 `logger=None`（不使用），保证所有 `complete()` 调用方签名一致。

---

#### Step 2 — 运行验证

用 mock adapter + 真实 registry 验证接线（不依赖网络/key）。fake adapter 的 `complete()` 收到的第三个参数被记录，断言它非 None。

在项目根目录创建一次性验证脚本 `verifyAgentLogger.py`：

```python
from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import chatMessage
from flamingoAgents.tools.registry import createDefaultRegistry

calls = []


class fakeAdapter:
    def complete(self, messages, tools, logger=None):
        calls.append(logger)
        return chatMessage(role='assistant', content='done', toolCalls=[])


a = agent(
    modelAdapter=fakeAdapter(),
    registry=createDefaultRegistry(),
    workDir=Path('.'),
    logDir=Path('.agentLogs'),
)

result = a.runUserMessage('hello', sessionId='verifyAgentLogger')
assert result.status == 'completed', f'状态异常：{result.status}'
assert calls, 'complete 未被调用'
assert calls[0] is not None, 'complete 未收到 logger'

print('✅ Task 2 验证通过：agent 把 conversation.logger 传给了 complete')
print('  result.message =', result.message)

# 清理本次验证产生的临时日志
for f in Path('.agentLogs').glob('*_verifyAgentLogger.jsonl'):
    f.unlink()
```

运行：

```bash
$ uv run python verifyAgentLogger.py
# 预期：
# ✅ Task 2 验证通过：agent 把 conversation.logger 传给了 complete
#   result.message = done
```

**额外回归验证（不依赖网络，强烈推荐）**：跑项目自带的 `manualChecks.py agent` 检查。它走真实 agent 循环 + fakeModel（fakeModel 不联网），正好验证 Task 2「修改 3」的 fakeModel 3 参签名不崩、且整条调用链贯通：

```bash
$ uv run python manualChecks.py agent
# 预期：依次跑完 agent core 各步，最后 printPass('agent core')，无 TypeError
```

验证通过后删除一次性脚本：

```bash
$ rm verifyAgentLogger.py
```

---

✅ **完成的标志：** `verifyAgentLogger.py` 打印 `✅ Task 2 验证通过` 且退出码为 0（assert 全部通过），`calls[0] is not None` 确认 logger 已传入。

---

## （可选）端到端确认

> 本节依赖局域网 llama.cpp 服务（`http://192.168.0.101:8080/v1`）在线。若该服务不可达，**跳过本节不影响计划完成判定**——Task 1/Task 2 的 mock 验证已充分覆盖目标。

若服务在线，可做一次真实会话端到端确认。注意 CLI 主循环用 `input('你> ')` 且无 EOF 保护，必须用 `/exit` 正常退出，避免管道 EOF 导致崩溃：

```bash
$ printf '帮我用 ls 列出 docs 目录\n/exit\n' | uv run Flamingo --session-id verifyE2E
# 预期：Agent 调用一次 ls 工具后回复，正常退出

$ grep -o '"type":"model[A-Za-z]*"' .agentLogs/*_verifyE2E.jsonl | sort | uniq -c
# 预期：能看到 modelRequest 与 modelResponse 各 2 次（一次首轮 + 一次拿到工具结果后）

$ jq 'select(.type=="modelRequest") | .request | {model, msgCount: (.messages|length), toolCount: (.tools|length)}' .agentLogs/*_verifyE2E.jsonl
# 预期：每轮请求的 messages 数与 tools 数（tools 应为 4：read/write/edit/bash）

$ jq 'select(.type=="modelResponse") | .response.usage' .agentLogs/*_verifyE2E.jsonl
# 预期：完整的 usage 对象（prompt_tokens/completion_tokens/total_tokens）

$ rm .agentLogs/*_verifyE2E.jsonl   # 清理验证日志
```

---

## 自我复审

**1. 规范覆盖**
- "完整记录请求体（messages + tools）" → Task 1 `modelRequest`，验证脚本断言 `tools in request` 且 `messages[0].content == 'hello'` ✅
- "完整记录回复体" → Task 1 `modelResponse`，验证脚本断言 `response.usage.total_tokens == 7` ✅
- "完整记录错误响应" → Task 1 `modelResponseError`（用户拍板的事件名），验证脚本断言 `status == 429` ✅
- "生产路径生效" → Task 2 把 `conversation.logger` 传入 ✅
- 无遗漏需求。

**2. 占位符扫描**
- 全文无 TODO / TBD / "类似 Task N" / 省略号。openai.py 给出完整文件，agent.py 给出精确 oldText/newText。✅

**3. 类型一致性**
- Task 1 `complete` 签名：`logger: jsonlLog | None = None`
- Task 2 传入：`conversation.logger`（`jsonlLog` 实例，见 `conversation.py:19` `self.logger = jsonlLog(logPath)`）
- Task 1 验证脚本 `jsonlLog(logPath)` 与 Task 2 验证脚本 fake adapter 的 `complete(self, messages, tools, logger=None)` 签名一致。✅
- Task 2 同步更新 `manualChecks.py` 的 `fakeModel.complete` 加 `logger=None`，与生产调用元数一致，避免 `manualChecks.py` 跑通性被破坏。✅
- 事件类型字符串 `modelRequest`/`modelResponse`/`modelResponseError` 在实现与验证中拼写完全一致。✅

**4. 验证完整性**
- Task 1：import 构造检查 + mock 脚本（含成功与错误两 case），三层（导入/运行/assert）齐全。✅
- Task 2：mock adapter 断言 `calls[0] is not None`，运行命令明确。✅
- 两个验证脚本均纯净、不依赖网络/key，可稳定复现。✅

---

## 执行交接

计划已完成并保存到 `docs/flare/20260702_modelRequestResponseLogging.md`。两种执行选项：

**1. 子代理驱动（推荐）** —— 我为每个任务分派一个全新的子代理，在任务之间进行复审，快速迭代

**2. 内联执行** —— 使用 executing-plans 在本会话中执行任务，带复审检查点的批处理

**选择哪种方式？**
