# 系统工具对话 Agent 设计草案

- 日期：2026-06-29
- 状态：审阅草案
- 范围：仅做架构设计，不包含具体实现代码

## 1. 背景

目标是设计一个基于 Python 的对话 Agent。它可以和用户聊天，也可以调用本机系统工具，并通过简单的 shell 命令完成基础联网查询。

这个 Agent 第一版需要同时支持：

- 命令行对话；
- 本地 HTTP 服务；
- 模型无关架构；
- 本地文件读写与精确编辑；
- 原生 bash 命令执行；
- 简单 `curl` 联网查询；
- 删除类命令执行前确认；
- JSONL 日志审计。

第一版设计刻意保持小而清晰。它会借鉴 pi 风格 coding agent 的核心抽象：统一模型消息、统一工具调用、少量本地工具、结构化工具结果、JSONL 日志记录。

## 2. 已确认需求

### 2.1 运行形态

- 使用 Python 实现。
- 真正开始实现时，Python 环境使用 `uv` 管理。
- 提供两个入口：
  - CLI 命令行聊天程序；
  - 本地 HTTP 服务，对外提供聊天接口。
- CLI 和 HTTP 必须共用同一个 `agentCore`。

### 2.2 模型层

- 架构保持模型无关。
- Agent 内部使用统一的消息格式和工具调用格式。
- 第一版可以优先实现 OpenAI-compatible adapter。
- 后续可以扩展 Claude、Gemini、本地模型或其他 provider。

### 2.3 本地工具层

第一版本地工具只包含：

```text
read
write
edit
bash
```

工具含义：

- `read`：读取文件，可支持 offset / limit。
- `write`：创建或完整覆盖文件。
- `edit`：对已有文件进行精确文本替换。
- `bash`：执行原生 shell 命令。

重要决策：

- 第一版不单独设计 `curl` 工具。
- 第一版不单独设计 `python` 工具。
- 第一版不单独设计 `grep` 工具。
- 第一版不单独设计 `open` 工具。
- `curl`、`python`、`grep`、`open` 都通过原生 `bash` tool 执行。

示例：

```bash
curl -L 'https://example.com' | head -100
python - <<'PY'
print('hello')
PY
grep -R "keyword" .
open https://example.com
```

### 2.4 删除命令策略

- Agent 整体上被视为可信，可以自动执行普通工具。
- 例外：删除类命令必须执行前拦截。
- 如果 `bash` 命令看起来会删除文件或目录，必须先询问用户确认。
- 如果用户拒绝，则命令不执行，并返回 blocked tool result。

### 2.5 联网查询行为

- 联网查询只通过简单 shell 命令完成，主要是 `curl`。
- 第一版不做浏览器自动化。
- 第一版不使用 Playwright。
- 第一版不使用 CDP / 浏览器远程调试端口。
- 第一版不读取登录态浏览器会话。
- 如果 `curl` 因为反爬、登录墙、验证码、403、空结果等原因无法获取有效内容，Agent 应直接说明查询失败，不继续尝试绕过。

### 2.6 日志

- 对话和工具执行记录保存为本地 JSONL 文件。
- 第一版不使用 SQLite。
- 日志需要足够还原执行过程，包括：
  - 用户消息；
  - 助手消息；
  - 工具调用；
  - 工具结果；
  - shell 命令；
  - exit code；
  - stdout / stderr 摘要；
  - 截断信息；
  - 被拦截命令原因。

## 3. 第一版非目标

第一版不做：

- 完整浏览器控制；
- 浏览器登录态读取；
- 反爬绕过；
- 搜索 API 集成；
- 多 provider 完整等价支持；
- 类似 pi 的完整 extension 系统；
- session tree / fork / branch / compaction；
- 并行工具执行；
- TUI 终端界面；
- 容器沙箱。

## 4. 总体架构

```text
cliApp / httpServer
        ↓
agentCore
        ↓
conversationManager
        ↓
modelRegistry
        ↓
modelAdapter
        ↓
assistantMessage / toolCall
        ↓
toolRegistry
        ↓
toolRouter
        ↓
toolGuard
        ↓
read / write / edit / bash
        ↓
toolResult
        ↓
jsonlLogger
```

### 4.1 入口层

`cliApp` 和 `httpServer` 都应该保持很薄。

职责：

- 接收用户输入；
- 把消息交给 `agentCore`；
- 展示或返回助手输出；
- 当删除命令需要确认时，负责和用户交互。

入口层不应该包含模型 provider 逻辑，也不应该包含具体工具执行逻辑。

### 4.2 Agent 核心

`agentCore` 负责主循环：

1. 接收用户消息；
2. 根据对话历史构建模型上下文；
3. 通过 `modelAdapter` 调用模型；
4. 检查助手响应；
5. 如果响应中包含工具调用，则执行工具；
6. 将工具结果追加回上下文；
7. 必要时再次调用模型；
8. 当模型返回最终自然语言回答时结束。

### 4.3 conversationManager

`conversationManager` 维护当前会话的内存态，并为模型调用提供消息列表。

它还负责把最终确定的消息交给 `jsonlLogger`。

第一版行为：

- 当前会话先保存在内存中；
- 每个关键事件都追加写入 JSONL；
- 是否支持从 JSONL resume，可以后续实现阶段再决定。

### 4.4 modelRegistry

`modelRegistry` 负责保存模型配置和 provider 元信息。

第一版最小模型配置可以类似：

```json
{
  "provider": "openaiCompatible",
  "model": "your-model-name",
  "baseUrl": "https://api.example.com/v1",
  "apiKeyEnv": "OPENAI_API_KEY",
  "apiType": "openaiCompatible",
  "compat": {
    "supportsToolCalling": true
  }
}
```

这个设计借鉴 pi 的思路：

```text
provider + api type + model + compatibility flags
```

不要把模型逻辑硬编码进 `agentCore`。

### 4.5 modelAdapter

`modelAdapter` 负责：

- 把内部消息格式转换成 provider 请求格式；
- 把内部工具定义转换成 provider 工具定义格式；
- 调用模型接口；
- 把 provider 响应转换回内部 assistant message。

第一版：

- 只需要实现 OpenAI-compatible adapter；
- 如果 provider 支持原生 tool calling，就使用原生 tool calling；
- 后续可以预留 JSON Action fallback，但第一版不强制实现。

内部 assistant 响应可以统一成：

```json
{
  "role": "assistant",
  "content": "optional text",
  "toolCalls": [
    {
      "id": "call_123",
      "toolName": "bash",
      "arguments": {
        "command": "curl -L https://example.com | head -100",
        "timeout": 30
      }
    }
  ]
}
```

## 5. 工具设计

### 5.1 统一 toolCall

所有工具调用都使用同一种内部格式：

```json
{
  "id": "call_123",
  "toolName": "bash",
  "arguments": {}
}
```

### 5.2 统一 toolResult

所有工具返回都使用同一种内部格式：

```json
{
  "toolCallId": "call_123",
  "toolName": "bash",
  "isError": false,
  "content": "返回给模型看的文本",
  "details": {}
}
```

### 5.3 read 工具

用途：读取文本文件。

输入：

```json
{
  "path": "relative/or/absolute/path",
  "offset": 1,
  "limit": 200
}
```

规则：

- 支持相对路径和绝对路径；
- 返回文本内容；
- 支持输出截断；
- 文件过大时返回截断内容，并在 details 中说明截断信息。

### 5.4 write 工具

用途：创建或完整覆盖文件。

输入：

```json
{
  "path": "relative/or/absolute/path",
  "content": "完整文件内容"
}
```

规则：

- 用于新建文件或整体覆盖文件；
- 是否自动创建父目录可以在实现阶段明确；
- 日志记录路径和内容大小；
- JSONL 中不记录超大完整内容，只记录摘要或截断内容。

### 5.5 edit 工具

用途：对已有文件做精确局部修改，接近 pi 的 edit 能力。

输入：

```json
{
  "path": "relative/or/absolute/path",
  "edits": [
    {
      "oldText": "精确原文",
      "newText": "替换文本"
    }
  ]
}
```

规则：

- 每个 `oldText` 必须精确匹配；
- 每个 `oldText` 必须唯一；
- 多个 edits 不能重叠；
- 如果任意 edit 校验失败，则不应用任何修改；
- 成功后返回 diff 摘要；
- 修改已有文件时优先使用 `edit`，而不是 `write`。

### 5.6 bash 工具

用途：执行原生 shell 命令。

输入：

```json
{
  "command": "curl -L https://example.com | head -100",
  "timeout": 30
}
```

规则：

- 使用宿主系统 bash 执行；
- 在配置的工作目录下执行；
- 可以先收集 stdout / stderr，后续再做流式输出；
- 强制超时；
- 捕获 exit code；
- 返回给模型前对长输出做截断；
- 输出被截断时，可选把完整输出写到临时日志文件；
- 执行前必须经过 `toolGuard`。

## 6. 工具执行生命周期

工具执行链尽量接近 pi 的模型：

```text
assistant toolCall
        ↓
toolRouter 查找工具
        ↓
校验参数
        ↓
beforeToolCall / toolGuard
        ↓
执行工具
        ↓
afterToolCall
        ↓
生成 toolResult
        ↓
写入 JSONL 日志
        ↓
把 toolResult 返回给模型
```

### 6.1 toolRegistry

`toolRegistry` 维护工具名称到工具定义的映射。

第一版注册：

```text
read
write
edit
bash
```

每个工具定义包含：

- name；
- description；
- 参数 schema；
- execute 函数。

### 6.2 toolRouter

`toolRouter` 是所有工具执行的统一入口。

职责：

- 根据工具名查找工具；
- 校验 arguments；
- 调用 `toolGuard`；
- 调用具体工具实现；
- 将异常统一转换为 `toolResult`；
- 将执行事件交给 `jsonlLogger`。

### 6.3 toolGuard

`toolGuard` 负责执行前的强制检查。

第一版强制规则：

- 删除类 `bash` 命令必须用户确认。

第一版可选规则：

- 如果没有 timeout，则补默认 timeout；
- 限制最大 timeout；
- 限制输出大小；
- 对日志中的 API key、token 等明显敏感信息做脱敏。

## 7. 删除命令确认

### 7.1 检测目标

`toolGuard` 检查：

```text
bash.arguments.command
```

需要确认的明显删除操作包括：

```bash
rm file.txt
rm -r folder
rm -rf folder
rmdir folder
unlink file
find . -delete
```

如果 shell 文本中出现以下 Python 删除语义，也需要确认：

```bash
python -c "import os; os.remove('file')"
python -c "import shutil; shutil.rmtree('folder')"
```

### 7.2 检测策略

第一版不要求完美静态分析。规则是：

> 如果删除意图明显，或者不确定但很可能是删除操作，就要求确认。

也就是说宁可误报，也不要漏掉明显删除命令。

### 7.3 CLI 确认行为

CLI 中可以显示：

```text
Agent 想执行一个删除相关命令：
rm -rf dist

是否允许？[y/N]
```

### 7.4 HTTP 确认行为

HTTP 中如果需要确认，可以返回：

```json
{
  "status": "confirmationRequired",
  "confirmationId": "confirm_123",
  "toolCall": {
    "toolName": "bash",
    "arguments": {
      "command": "rm -rf dist"
    }
  },
  "reason": "删除命令需要用户确认"
}
```

后续通过单独确认接口，或者通过后续 `/chat` 消息确认，具体实现阶段再定。

### 7.5 拒绝执行行为

如果用户拒绝，返回给模型的 toolResult：

```json
{
  "toolCallId": "call_123",
  "toolName": "bash",
  "isError": true,
  "content": "命令已被用户拒绝：删除命令需要确认。",
  "details": {
    "blocked": true,
    "reason": "userRejectedDeletionCommand"
  }
}
```

## 8. 通过 curl 做联网查询

第一版不设计独立 `webSearchTool`。

模型需要联网查询时，通过 `bash` 调用 `curl`：

```bash
curl -L 'https://example.com' | head -200
```

或简单访问搜索引擎 HTML 页面：

```bash
curl -L 'https://html.duckduckgo.com/html/?q=example' | head -200
```

预期行为：

- 如果结果可读，就基于内容总结；
- 如果结果被阻挡或不可用，就说明查询失败；
- 不尝试浏览器登录；
- 不尝试验证码处理；
- 不尝试反爬绕过。

## 9. JSONL 日志

### 9.1 日志位置

实现时可以选择默认位置，例如：

```text
.agentLogs/YYYYMMDD_sessionId.jsonl
```

也可以做成用户配置项。

具体路径可以在实施计划阶段最终确定。

### 9.2 日志事件示例

用户消息：

```json
{"type":"message","role":"user","content":"搜索这个主题"}
```

助手工具调用：

```json
{"type":"toolCall","toolCallId":"call_123","toolName":"bash","arguments":{"command":"curl -L https://example.com | head -100"}}
```

工具结果：

```json
{"type":"toolResult","toolCallId":"call_123","toolName":"bash","isError":false,"exitCode":0,"stdoutPreview":"...","stderrPreview":"","truncated":false}
```

被拦截命令：

```json
{"type":"toolResult","toolCallId":"call_456","toolName":"bash","isError":true,"blocked":true,"reason":"userRejectedDeletionCommand"}
```

### 9.3 脱敏与截断

日志应该：

- 截断过长 stdout / stderr；
- 不记录超大文件完整内容；
- 对明显 API key、token、secret 做脱敏；
- 记录输出是否被截断；
- 必要时把完整输出写到临时文件，并在日志中记录路径。

## 10. CLI 流程

```text
用户输入消息
        ↓
agentCore 运行
        ↓
模型可能请求工具
        ↓
普通工具自动执行
        ↓
如果是删除命令，CLI 要求用户确认
        ↓
工具结果返回给模型
        ↓
助手给出最终回答
```

CLI 第一版命令可以很少：

```text
/exit
/help
```

其他命令后续再加。

## 11. HTTP 流程

### 11.1 聊天接口

最小接口：

```http
POST /chat
```

请求：

```json
{
  "sessionId": "optional-session-id",
  "message": "用户消息"
}
```

完成响应：

```json
{
  "sessionId": "session-id",
  "status": "completed",
  "message": "助手最终回答"
}
```

需要确认时响应：

```json
{
  "sessionId": "session-id",
  "status": "confirmationRequired",
  "confirmationId": "confirm_123",
  "reason": "删除命令需要用户确认",
  "commandPreview": "rm -rf dist"
}
```

### 11.2 确认继续

一种可能的确认接口：

```http
POST /confirm
```

请求：

```json
{
  "sessionId": "session-id",
  "confirmationId": "confirm_123",
  "approved": true
}
```

具体 API 形状可以在实施计划阶段最终确定。

## 12. 错误处理

### 12.1 模型错误

模型请求失败时：

- 返回清晰错误；
- 记录 provider、model、状态码或错误摘要；
- 不吞掉 provider 错误。

### 12.2 工具错误

工具执行失败时：

- 转换成 `isError: true` 的 `toolResult`；
- 把失败信息返回给模型；
- 由模型决定是否重试、调整方案或向用户说明失败。

### 12.3 bash 超时

bash 命令超时时：

- 终止进程；
- 返回 timeout result；
- 如果有部分输出，保留部分输出；
- 记录 timeout 元信息。

### 12.4 curl 失败

如果 `curl` 失败、返回 403 或拿不到有效内容：

- 不绕过；
- 把失败信息返回给模型；
- 最终回答应诚实说明查询失败。

## 13. 测试策略

实现时必须使用测试，不只做手工验证。

### 13.1 单元测试

需要测试：

- `toolRouter` 参数校验；
- `edit` 精确匹配行为；
- `bash` 超时行为；
- 删除命令检测；
- JSONL 日志脱敏与截断；
- OpenAI-compatible 响应解析。

### 13.2 集成测试

需要覆盖：

1. CLI 普通对话，不调用工具。
2. CLI 触发 `read`。
3. CLI 触发 `edit`。
4. CLI 触发无害 `bash` 命令。
5. CLI 触发删除类 `bash` 命令，并且用户拒绝。
6. HTTP `/chat` 执行无害工具。
7. HTTP `/chat` 对删除命令返回 `confirmationRequired`。
8. `curl` 查询失败时，最终回答如实说明失败。

### 13.3 安全测试

删除检测至少要覆盖：

```text
rm file
rm -rf folder
rmdir folder
unlink file
find . -delete
python -c "import os; os.remove('file')"
python -c "import shutil; shutil.rmtree('folder')"
```

## 14. 成功标准

第一版实现成功的标准：

- CLI 和 HTTP 共用同一个 `agentCore`。
- OpenAI-compatible 模型调用通过 `modelAdapter` 工作。
- 模型可以调用 `read`、`write`、`edit`、`bash`。
- `curl`、`python`、`grep`、`open` 可以通过 `bash` 使用。
- `edit` 可以做精确文本替换，并返回 diff 摘要。
- 删除相关 `bash` 命令必须用户确认。
- 被用户拒绝的删除命令不会执行。
- JSONL 日志可以还原对话和工具执行路径。
- `curl` 查询失败会如实说明，不进行浏览器或反爬绕过。

## 15. 关键设计决策

1. 第一版保持小而直接。
2. shell 能力使用 pi 风格的原生 `bash` tool。
3. `edit` 是一等本地文件工具。
4. 第一版不单独创建 `curl`、`python`、`grep`、`open` 工具。
5. 保留 provider 抽象，但第一版只实现 OpenAI-compatible。
6. 删除确认是内置 guard，不是可选插件。
7. 第一版使用 JSONL，而不是 SQLite。

## 16. 待审阅问题

以下问题留给审阅后拍板：

1. `write` 覆盖已有文件是否也需要确认，还是只有删除命令需要确认？
2. `edit` 如果删除了文件中大段内容，是否需要确认？
3. HTTP 确认流程使用 `/confirm`，还是通过后续 `/chat` 消息继续？
4. 日志默认放在项目根目录、用户 home，还是做成配置项？
5. 第一版是否需要从 JSONL 恢复会话，还是只写日志不恢复？

## 17. 规范自检结果

- 没有保留 TODO 占位符。
- 架构范围集中在第一版本地系统工具对话 Agent。
- 已明确第一版不做浏览器、反爬绕过、搜索 API 和完整 extension 系统。
- 已把本地 shell 能力收敛为原生 `bash` tool。
- 已把 `edit` 作为一等工具加入。
- 已明确删除命令必须执行前确认。
