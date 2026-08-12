'''
Author: wilbur
Version: 1.4
Date: 2026-08-11
Description: 聊天界面流式/历史渲染问题根因落档（thinking 不显示与不落库、工具卡位置漂移、多头像拆分、流式强制贴底、工具仅完成后才可见）。含直接代码证据与修复建议顺序。v1.2 增加修复方案链接。v1.3：标注实施状态（fixPlan Phase 1–5 已实施）；问题 6 根因 A 遗留记录。v1.4：问题 6 增补 streamingLatencyFixPlan 落地注记（read1 + 可执行前缀批量 Start = 旧 T5.6，事件序脚本验证通过；final 前空白留二期 skeleton）。
'''

# 聊天界面流式 / 历史渲染问题根因落档

> 日期：2026-08-08  
> 范围：`webApp/frontend/js/chatView.js`、`webApp/backend/historyView.py`、`flamingoAgents/core/agent.py`、`flamingoAgents/core/conversation.py`、`flamingoAgents/models/chatCompletions.py`  
> 状态：**Phase 1–5 已实施（2026-08-08，见 fixPlan），手测验收待联调**  
> **修复方案（含分阶段计划与 TODOlist）→ [`docs/chatUiStreamingFixPlan.md`](./chatUiStreamingFixPlan.md)**

---

## 1. 问题清单

| # | 用户现象 | 根因层 | 一句话 |
|---|----------|--------|--------|
| 1 | LLM 输出 think 时界面不显示，首轮 think 结束后才出现「已思考」 | 前端 UI | thinking 用默认闭合的 `<details>`，summary 写死「已思考」，流式中不自动展开 |
| 2 | 工具调用卡显示在正文最下面；刷新后工具回到正文上方 | 前端 live vs 历史 | live 单块：正文容器固定在前、工具永远 `append` 在后；刷新后按多条 assistant 重画 |
| 3 | 工具调用被拆成多个来回（一个头像挂 2 个工具、另一个挂 3 个） | 后端 agent 循环 + 历史 1:1 映射 | 每轮模型调用落一条 `assistantMessage`，历史每条单独一头像 |
| 4 | 本轮对话完成后看不到 thinking 历史 | 全链路未持久化 | `reasoning_content` 只走 SSE，jsonl / GET messages / 历史渲染全都不存、不画 |
| 5 | LLM 正在输出时无法上翻历史，被强制跟着往下刷 | 前端滚动策略 | 几乎所有流式增量事件都无条件 `scrollToBottom()`，无「用户上翻则暂停贴底」 |
| 6 | 工具调用只有完成后才显示，正在调用的不显示 | 后端事件时机 + 短工具连发 | `toolCallStart` 要等整步模型流结束才发；执行快时 Start/End 几乎同时到前端，running 态不可感知 |

---

## 2. 结构性总图

一次用户消息在后端是 **多 step agent 循环**；前端 live 却按 **单 assistant 块** 渲染；历史又按 **每 step 一条消息** 回放。thinking 还只活在内存/SSE 里。

```text
用户一条消息
  └─ driveModelLoop
       ├─ step1 模型 → assistantMessage#1 (tools=2, content 常为空) + reasoning(仅 SSE)
       ├─ 执行 2 个工具
       ├─ step2 模型 → assistantMessage#2 (tools=3, content 常为空) + reasoning(仅 SSE)
       ├─ 执行 3 个工具
       └─ step3 模型 → assistantMessage#3 (tools=0, content=最终回答) + reasoning(仅 SSE)

Live UI（流式中，单一头像）:
  [thinking details 默认折叠]
  [contentEl ← 所有 textDelta 拼进同一容器]
  [tool card …] ← 所有工具 append 在 content 后面
  且每次增量都 scrollToBottom()

History UI（刷新 / 重进）:
  头像1: 2 tools（常无正文）
  头像2: 3 tools（常无正文）
  头像3: 最终正文
  全程无 thinking
```

因此问题 2/3/4 会绑在一起出现；问题 1/5 是 live 交互层缺陷；问题 6 是工具事件发出时机问题。

---

## 3. 问题 1：think 流式时不显示，结束后才见「已思考」

### 3.1 根因

后端会实时推 `reasoningDelta`，前端也写入 DOM，但 UI 选择了：

1. 用 **默认闭合** 的 `<details>` 承载 thinking；
2. summary 文案写死为 **「已思考」**（完成态措辞）；
3. 初始 `hidden`，首包 reasoning 才去掉，且 **从不 `open = true`**。

用户默认只看到折叠标题，看不到正在涌入的 think 正文；体感就是「想完了才冒出一个已思考」。

### 3.2 代码证据

创建 live 块时 thinking 默认 hidden + 未 open：

```js
// webApp/frontend/js/chatView.js — buildThinkingBlock / createLiveAssistantBlock
function buildThinkingBlock() {
  var details = document.createElement('details'); // 默认不带 open
  details.className = 'thinking-block';
  var summary = document.createElement('summary');
  summary.textContent = '已思考'; // 写死完成态文案
  ...
}

function createLiveAssistantBlock() {
  var thinking = buildThinkingBlock();
  thinking.el.classList.add('hidden'); // 初始隐藏
  shell.body.appendChild(thinking.el);
  shell.body.appendChild(contentEl);
  ...
}
```

收到 reasoning 时只 unhide + 写文本，不展开：

```js
// webApp/frontend/js/chatView.js — onStreamEvent
case 'reasoningDelta':
  stream.reasoningBuf += data.text || '';
  if (stream.live) {
    stream.live.thinkingEl.classList.remove('hidden');
    stream.live.thinkingContentEl.textContent = stream.reasoningBuf;
    // 没有 thinkingEl.open = true
    // 没有把 summary 改成「思考中…」
  }
  scrollToBottom();
  break;
```

后端确实在推实时 reasoning：

```python
# flamingoAgents/models/chatCompletions.py — processSseData
reasoning = delta.get('reasoning_content')
if reasoning:
    yield reasoningChunk(text=reasoning)
```

```python
# flamingoAgents/core/agent.py — driveModelLoop
elif isinstance(chunk, reasoningChunk):
    yield reasoningDeltaEvent(text=chunk.text)
```

**结论：链路能推 think，UI 选择不展示正文。**

---

## 4. 问题 2：工具卡在正文最下；刷新后跑到正文上方

### 4.1 根因

这是 **live 单块模型** vs **历史多块模型** 的结构性差异，不是 CSS 偶然错位。

| 阶段 | 布局 |
|------|------|
| Live（流式中） | 一个 assistant 壳：`thinking → contentEl → tool cards(append 末尾)`。工具相对正文永远在下方 |
| History（刷新后） | 每个模型 step 单独一头像；中间 step 常是「空正文 + 工具」，最终 step 才是「长正文」。工具视觉上回到正文上方 |

### 4.2 代码证据

发送时整轮只建一个 live 块：

```js
// webApp/frontend/js/chatView.js — send()
window.appStore.stream = {
  phase: 'streaming',
  textBuf: '',
  reasoningBuf: '',
  live: createLiveAssistantBlock(), // 整轮共用这一个
  ...
};
```

DOM 顺序写死：thinking → content；工具再 append 到 body 末尾：

```js
shell.body.appendChild(thinking.el);
shell.body.appendChild(contentEl);

function upsertToolCardOnStart(toolCall, preview) {
  ...
  liveBodyEl().appendChild(card.el); // 永远在 contentEl 后面
}
```

所有 step 的正文都累加进同一个 `textBuf/contentEl`：

```js
case 'textDelta':
  stream.textBuf += data.text || '';
  if (stream.live) renderMarkdown(stream.live.contentEl, stream.textBuf);
```

刷新走历史：每个 assistant 单独一块，先 content 再该条 tools：

```js
// webApp/frontend/js/chatView.js — appendAssistantHistory
function appendAssistantHistory(msg, toolResults, pending) {
  var shell = buildAssistantShell(); // 新头像
  shell.body.appendChild(contentEl);
  renderMarkdown(contentEl, msg.content || '');
  (msg.toolCalls || []).forEach(function (toolCall) {
    ...
    shell.body.appendChild(card.el);
  });
  messageListEl.appendChild(shell.row);
}
```

真实会话 jsonl 印证「多 step、中间正文为空、工具分批」：

```text
# webData/sessionLogs/session_4441789a0937.jsonl（摘录形态）
ASSISTANT content_len=0 tools=1
TOOL ...
ASSISTANT content_len=0 tools=1
TOOL ...
ASSISTANT content_len=0 tools=2
TOOL ...
TOOL ...
...
ASSISTANT content_len>0 tools=0   ← 最终回答
```

---

## 5. 问题 3：工具被拆成多个来回 / 多个头像

### 5.1 根因

后端 `driveModelLoop` 的真实数据形态是 **多 step**；历史 UI 又 **1 条 assistantMessage = 1 个头像** 映射。  
流式中通常仍是 1 个 live 头像；**一刷新就变成 N 个头像**——这也是问题 2「一刷新布局就变」的另一面。

### 5.2 代码证据

每步模型调用都落库一条 assistant：

```python
# flamingoAgents/core/agent.py — driveModelLoop
for stepIndex in range(self.maxModelSteps):
    ...
    currentConversation.appendAssistantMessage(assistantMessage, ...)  # 每步都落库
    if not assistantMessage.toolCalls:
        yield completedEvent(...)
        return
    terminated = yield from self.driveToolBatch(...)  # 执行工具后继续下一步
```

落库无「同轮 UI 合并」标记：

```python
# flamingoAgents/core/conversation.py — appendAssistantMessage
self.logger.logEvent({
    'type': 'assistantMessage',
    'content': message.content,
    'toolCalls': message.toolCalls,
    'usage': responsePayload.get('usage'),
    'timings': responsePayload.get('timings'),
})
```

历史 API 原样拆成多条：

```python
# webApp/backend/historyView.py — loadMessages
elif eventType == 'assistantMessage':
    messages.append({
        'kind': 'assistant',
        'content': event.get('content', ''),
        'toolCalls': toolCalls,
        ...
    })
```

前端历史遍历每条都新建头像：

```js
messages.forEach(function (msg) {
  if (msg.kind === 'assistant') {
    lastAssistant = appendAssistantHistory(...); // 内部 buildAssistantShell()
  }
});
```

**结论：一个头像挂 2 个工具、另一个挂 3 个 = 两次模型 step 各自返回的 toolCalls 批次，不是前端把一次调用拆坏了。**

---

## 6. 问题 4：本轮结束后看不到 thinking 历史

### 6.1 根因（最硬）

**reasoning 从未进入持久化链路。**  
流式中或许能在 DOM 里看到（若手动展开 details）；刷新 / 重进会话 / 只靠历史接口 → 一定没有。

### 6.2 证据链（模型 → 落库 → API → 渲染，整条断掉）

**A. 流式解析：reasoning 只 yield，不进入最终 message**

```python
# flamingoAgents/models/chatCompletions.py — processSseData / consumeSseStream
text = delta.get('content')
if text:
    contentParts.append(text)              # content 会进最终消息
    yield textChunk(text=text)

reasoning = delta.get('reasoning_content')
if reasoning:
    yield reasoningChunk(text=reasoning)   # 只推流
    # 没有 reasoningParts.append(...)

# 最终合成
messagePayload = {'role': 'assistant', 'content': ''.join(contentParts)}
if synthesizedToolCalls:
    messagePayload['tool_calls'] = synthesizedToolCalls
# 无 reasoning / thinking 字段
```

**B. jsonl 落库：assistantMessage 无 reasoning 字段**

```python
# flamingoAgents/core/conversation.py
self.logger.logEvent({
    'type': 'assistantMessage',
    'model': ...,
    'content': message.content,
    'toolCalls': message.toolCalls,
    'usage': ...,
    'timings': ...,
})
```

真实日志 key 仅有：

```text
['content', 'model', 'timestamp', 'timings', 'toolCalls', 'type', 'usage']
# 没有 reasoning / thinking
```

**C. 历史 API 不返回 thinking**

```python
# webApp/backend/historyView.py
messages.append({
    'kind': 'assistant',
    'content': event.get('content', ''),
    'toolCalls': toolCalls,
    'usage': ...,
    'model': ...,
})
```

**D. 历史渲染不建 thinking 块**

```js
// webApp/frontend/js/chatView.js — appendAssistantHistory
// 只有 contentEl + tool cards
// 没有 buildThinkingBlock()
renderMarkdown(contentEl, msg.content || '');
(msg.toolCalls || []).forEach(...);
```

**结论：thinking 只活在 `stream.reasoningBuf` + 当前 DOM；一切「以历史为准」的路径永久丢失。**

---

## 7. 问题 5：输出过程中无法上翻历史，被迫跟着往下刷

### 7.1 根因

`messageList` 在几乎所有流式增量路径上 **无条件调用 `scrollToBottom()`**，没有：

- 检测用户是否已离开底部；
- 「贴底跟随 / 用户上翻暂停」开关；
- 仅在 `nearBottom` 时才自动滚动。

因此用户一旦尝试往上翻看历史，下一条 `textDelta` / `reasoningDelta` / 工具事件会立刻把视口拽回底部。

### 7.2 代码证据

贴底函数本身无条件：

```js
// webApp/frontend/js/chatView.js
function scrollToBottom() {
  messageListEl.scrollTop = messageListEl.scrollHeight;
}
```

流式事件中的强制贴底：

```js
case 'textDelta':
  ...
  scrollToBottom();
  break;

case 'reasoningDelta':
  ...
  scrollToBottom();
  break;

case 'confirmationRequired':
  ...
  scrollToBottom();
  break;
```

工具卡创建 / 更新同样贴底：

```js
function upsertToolCardOnStart(...) {
  ...
  liveBodyEl().appendChild(card.el);
  scrollToBottom();
}

function resolveToolCardOnEnd(...) {
  ...
  scrollToBottom();
}
```

发送用户气泡、创建 live 块、历史渲染结束等路径也统一贴底（这些可接受；问题在于 **流式高频增量也无条件贴底**）。

**结论：不是浏览器强制，是前端滚动策略缺失「用户上翻则停止自动跟随」。**

---

## 8. 问题 6：工具只有完成后才显示，正在调用的不显示

### 8.1 用户现象

工具调用过程中看不到「执行中」卡片；往往工具跑完后才突然出现一张已完成/失败的卡。

### 8.2 根因（两层叠加）

#### 根因 A — `toolCallStart` 发得太晚（主因）

> **实施注记（2026-08-08）**：本期未改根因 A（Start 发送时机需等本 step 模型流结束、finalChunk 落库后才发出）；fixPlan T5.6「同批先全部 Start 再 exec」**遗留至后续迭代**。Phase 5 已强化 running 可见性（Start 到达即时插入 DOM + 呼吸动画）。
>
> **实施注记（2026-08-11，`docs/streamingLatencyFixPlan.md`）**：旧 T5.6 已落地为该方案 D2「可执行前缀批量 Start」（`agent.py` v1.12）：final 后同批可执行前缀先全部 Start 再串行 exec，事件序表 #1–#8 脚本验证通过（`scripts/verifyBatchStart.py`）。根因 A 的「final 前空白」仍未消除，需二期 skeleton（该方案 Phase5，冰区）。另新增本方案覆盖的上游根因：适配器 `read(4096)` 在 chunked SSE 上阻塞凑批（`chatCompletions.py` v1.12 改 `read1`）；快工具肉眼 running 依赖条件让帧（Phase3.5，手测触发）。

前端/后端**并非没有** running 态设计：

- 后端会先 `yield toolCallStartEvent`，再 `executeToolCall`，再 `yield toolCallEndEvent`
- 前端 `toolCallStart` 会建状态为 `running`（「执行中」）的卡片

但 **Start 的发出时机不在「模型开始吐 tool_calls」时**，而在：

1. 本 step 的 **整段模型流式响应全部结束**（`finalChunk` 到手、assistant 落库）之后；
2. 才进入 `driveToolBatch`，对每个 tool **此时才** `yield toolCallStartEvent`；
3. 紧接着同步 `executeToolCall`（阻塞当前生成器）；
4. 结束后才 `yield toolCallEndEvent`。

因此在模型还在流式输出 tool call 参数、或还在 think 的整段时间里，UI **完全没有工具卡**。用户体感是「调用中不可见」。

#### 根因 B — 短工具 Start/End 连发，running 来不及感知

`read` 等快速工具：`executeToolCall` 可能在数毫秒～数十毫秒内返回。此时：

1. 泵线程 `put(Start)` 后立刻继续生成器跑执行；
2. 很快再 `put(End)`；
3. SSE 连续两帧到达前端；
4. 浏览器同一帧或相邻帧内卡片从无 → running → done。

用户几乎只能看到 **完成后** 的卡。慢工具（长 `bash`）理论上应能看到「执行中」；若仍完全看不到，再查 SSE 刷新/缓冲，但代码路径上 Start 是会先入队的。

#### 根因 C — 同一 batch 内工具串行

模型一步返回多个 `toolCalls` 时，`driveToolBatch` **for 循环串行**：

```text
Start#1 → exec#1 → End#1 → Start#2 → exec#2 → End#2 → ...
```

不会「三个同时 running」。下一个的 Start 必须等上一个 End。结合根因 B，一批快工具看起来就像「一口气蹦出多张已完成卡」。

### 8.3 代码证据

**模型流式阶段只转发 text/reasoning，工具要等 final 后才处理：**

```python
# flamingoAgents/core/agent.py — driveModelLoop
for chunk in self.modelAdapter.completeStream(...):
    if isinstance(chunk, textChunk):
        yield textDeltaEvent(text=chunk.text)
    elif isinstance(chunk, reasoningChunk):
        yield reasoningDeltaEvent(text=chunk.text)
    elif isinstance(chunk, finalChunk):
        completion = chunk.completion
# 整段流结束后才：
currentConversation.appendAssistantMessage(assistantMessage, ...)
if not assistantMessage.toolCalls:
    yield completedEvent(...)
    return
terminated = yield from self.driveToolBatch(...)  # 这里才开始发 tool 事件
```

**Start 紧挨着同步执行，再 End（无中间让出/无「参数流式中」事件）：**

```python
# flamingoAgents/core/agent.py — driveToolBatch
yield toolCallStartEvent(toolCall=call, preview=self.buildToolPreview(definition, call))
result = self.executeToolCall(call)          # 同步阻塞
currentConversation.addToolResult(result)
yield toolCallEndEvent(toolResult=result)
```

**流式适配器在 delta 里累积 tool_calls，但只在流结束合成 final，过程中不对外发 tool 事件：**

```python
# flamingoAgents/models/chatCompletions.py — processSseData / consumeSseStream
for rawToolCall in delta.get('tool_calls') or []:
    # 只写入 toolCallAccum，不 yield 任何 tool 相关 chunk
    ...
# 流结束后才 synthesizedToolCalls → finalChunk(completion=...)
```

**前端其实支持 running 卡（说明不是「前端不会画执行中」）：**

```js
// webApp/frontend/js/chatView.js
case 'toolCallStart':
  if (data.toolCall) upsertToolCardOnStart(data.toolCall, data.preview || '');
  break;

function upsertToolCardOnStart(toolCall, preview) {
  ...
  var card = buildToolCard(toolCall, 'running', preview); // STATUS_MAP.running =「执行中」
  liveBodyEl().appendChild(card.el);
}
```

**结论：**  
- 不是前端丢了 `toolCallStart`；  
- 是 **Start 出现得太晚**（模型整步结束后才发）+ **执行快时 Start/End 连发**，导致「正在调用」态对用户不可见或几乎不可见。

### 8.4 与问题 2/3 的关系

- 问题 6 解释「**何时**出现工具卡」；  
- 问题 2 解释「出现后挂在正文哪」；  
- 问题 3 解释「刷新后为何多个头像分批」。  
三者叠加：流式中工具晚出现且堆在正文下；刷新后变成多头像历史块。

---

## 9. 影响面汇总

| 能力 | 现状 |
|------|------|
| 流式看 think 正文 | 差：默认折叠 + 文案完成态 |
| 结束后回看 think | 无：未落库 |
| 流式中工具与正文时序 | 扭曲：全部工具堆在最终正文下方 |
| 刷新后工具/正文布局 | 与 live 不一致：多头像分 step |
| 流式中上翻历史 | 不可用：强制贴底 |
| 工具「执行中」可见性 | 差：Start 过晚 + 短工具连发；前端有 running 态但感知不到 |
| 后端 agent 多 step 语义 | 正确；问题在 UI 映射、事件时机与持久化缺口 |

---

## 10. 建议修复顺序（未实施，仅落档）

### P0 — 问题 4：reasoning 持久化

1. `chatCompletions.consumeSseStream`：累积 `reasoningParts`，写入 `responsePayload` / message 扩展字段（需约定字段名，如 `reasoning`）。  
2. `conversation.appendAssistantMessage`：jsonl 增加 `reasoning`（或 `thinking`）字段。  
3. resume 路径同步恢复（若仅 UI 展示、不回灌模型，需明确不进入 `chatMessage` 发给模型，避免污染上下文）。  
4. `historyView.loadMessages`：DTO 透传 `reasoning`。  
5. `appendAssistantHistory`：有 reasoning 时渲染 thinking 块（建议默认折叠，summary「已思考」）。

### P0 — 问题 5：智能贴底

1. 维护 `stickToBottom` 标志：距底部阈值阈值时为 true，用户上翻为 false。  
2. 仅当 `stickToBottom === true` 时调用 `scrollToBottom`。  
3. 可选：离底时显示「↓ 回到底部 / 有新输出」按钮，点击恢复跟随。

### P1 — 问题 1：流式 thinking 可见性

1. 首包 `reasoningDelta`：`thinkingEl.open = true`，summary 改为「思考中…」。  
2. 本轮 `completed` 或本 step 结束：自动折叠（或保持用户手动状态），summary 改回「已思考」。  
3. 与问题 4 字段对齐，避免 live / history 两套 DOM 结构。

### P1 — 问题 6：工具「执行中」可见

按侵入性从低到高：

1. **保底（低改动）**：确保 `toolCallStart` 在 `executeToolCall` 前发出后，SSE/泵路径能立刻被消费（避免与 End 粘包体感）；前端对 running 卡做更醒目样式/骨架。  
2. **推荐**：在 `completeStream` 聚合出完整 tool call（id+name+arguments 齐）时，尽早向 agent 层暴露「即将调用」信号；或在 `finalChunk` 之后、`executeToolCall` 之前 **强制先 yield Start 并保证消费者处理一帧**（生成器层已 yield，重点是别让前端丢帧）。  
3. **增强**：模型仍在流式吐 `tool_calls` 参数时就发 `toolCallStart`（或 `toolCallPending`），arguments 未齐时卡片显示「准备中…」，齐了再转「执行中」。  
4. **可选并行**：同 batch 多工具若无依赖可并行执行并同时 Start（产品/安全需评估）。  
5. 回归：pending 确认路径仍是「先 confirmationRequired，批准后再 Start」——不要把未批准工具画成执行中。

### P1/P2 — 问题 2 & 3：live / history 布局对齐

二选一（或组合），需产品决策：

**方案 A — live 按 step 拆块（更贴近历史与真实 agent 语义）**

- 每次新的模型 step 开始时新建 assistant 壳（或至少在 `toolCallStart` 前若上一段已有 content/tools 则封口再开新块）。  
- 工具挂在「产生它们的那次 assistant」下，而不是整轮唯一 content 下方。  
- 最终回答单独一块。  
- 刷新前后视觉一致。

**方案 B — 历史合并为「一轮用户消息」气泡**

- GET messages 或前端把连续 assistant+tool 合并为一个 UI turn。  
- 保留 step 内工具顺序，但共用一个头像。  
- 实现成本在历史合并规则（dangling / pending / 中断边界）。

推荐优先 **方案 A**：与后端数据模型一致，少做聚合猜测。

### 回归关注点

- pending 确认卡片归位（现有 toolCallId 注册表）  
- dangling 灰卡  
- 停止 / 中断半截消息  
- resume 后 reasoning 是否误回灌模型  
- 长会话滚动性能（markdown 全量重渲染已有，拆块后需确认）

---

## 11. 相关文件索引

| 文件 | 角色 |
|------|------|
| `webApp/frontend/js/chatView.js` | live/历史渲染、thinking UI、工具卡、强制贴底 |
| `webApp/frontend/js/sse.js` | SSE 帧解析 |
| `webApp/backend/sseCodec.py` | 库事件 → SSE 帧 |
| `webApp/backend/historyView.py` | jsonl → GET messages DTO |
| `flamingoAgents/core/agent.py` | driveModelLoop 多 step、事件流、toolCallStart/End 时机 |
| `flamingoAgents/core/conversation.py` | jsonl 落库 / resume |
| `flamingoAgents/models/chatCompletions.py` | 流式 content / reasoning_content / tool_calls 解析 |
| `flamingoAgents/tools/toolRuntime.py` | 同步 executeToolCall |

---

## 12. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-08-08 | 首版：5 个 UI/持久化问题根因 + 代码证据 + 修复顺序建议 |
| 1.1 | 2026-08-08 | 增补问题 6：工具仅完成后可见；Start 过晚 + 短工具 Start/End 连发 + batch 串行 |
| 1.2 | 2026-08-08 | 文首增加修复方案链接 `chatUiStreamingFixPlan.md` |
| 1.3 | 2026-08-08 | 标注实施状态（fixPlan Phase 1–5 已实施，手测待联调）；问题 6 根因 A 遗留记录 |
| 1.4 | 2026-08-11 | 问题 6 增补 `streamingLatencyFixPlan.md` 落地注记：read1 修复 + 旧 T5.6=可执行前缀批量 Start 已实施并脚本验证；final 前空白留二期 skeleton |
