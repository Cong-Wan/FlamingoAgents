'''
Author: wilbur
Version: 1.1
Date: 2026-08-08
Description: 针对 docs/chatUiStreamingIssues.md 六个聊天 UI/流式问题的详细修复方案与分阶段实施计划（含决策点、契约变更、文件改动、验收标准、完整 TODOlist）。本文件仅方案，不改业务代码。v1.1：按 pi 审核报告修订——补 dangling 重放 step 归属（S1）、pending 恢复 thinking 归属（M1）、stream=False reasoning 归一化（M2）、清理 D6 草稿歧义（M3）、对齐工具可见目标口径（M4）、补风险/用例/工期。
'''

# 聊天界面流式 / 历史渲染修复方案

- Author: wilbur
- Version: 1.1
- Date: 2026-08-08
- 上游诊断：`docs/chatUiStreamingIssues.md`（v1.1）
- 相关契约：`docs/streamOutputPlan.md`、`docs/webApiSpec.md`
- 状态：**方案待实施**（未改业务代码）

---

## 0. 目标与非目标

### 0.1 目标（修复后用户应感知到）

1. **Thinking 流式可见**：模型输出 reasoning 时，默认展开显示正文，标题为「思考中…」；结束后可折叠为「已思考」。
2. **Thinking 可回看**：刷新 / 重进会话后，历史消息仍能看到该 step 的 thinking（默认折叠）。
3. **工具「执行中」可见**：工具开始执行时立即可见 running 卡（Start 到达即插入 DOM、无额外延迟）；慢工具全程保持「执行中」直到 End。
4. **Live / History 布局一致**：流式过程与刷新后，都按 **模型 step** 呈现——每个 step 一个 assistant 头像块：`thinking → content → 本 step 的 tools`。
5. **可上翻历史**：输出过程中用户上翻后不再被强制拽回底部；靠近底部时自动跟随；离底时有「回到底部」入口。
6. **不破坏现有能力**：pending 确认、dangling 灰卡、停止/中断、用量回写、SSE 契约主干保持可用。

### 0.2 非目标（本期不做）

| 项 | 说明 |
|----|------|
| 工具 batch 并行执行 | 仍串行；安全/确认语义更简单 |
| reasoning 回灌模型请求 | 仅 UI + jsonl/history 展示，**不**写入发往模型的 `messages[]` |
| 历史消息 UI 合并为单头像 turn | 采用方案 A（按 step 拆块），不做方案 B 聚合 |
| 改 maxModelSteps / 计费逻辑 | 与本专题无关 |
| 大规模前端框架化 | 继续原生 JS |

### 0.3 成功标准（总验收）

- [ ] 含 think 的模型：流式中能实时看到 think 正文（默认展开）
- [ ] 同一会话刷新后：历史仍显示各 step 的 thinking（默认折叠）
- [ ] 多 step + 多工具：live 出现多个 assistant 块，刷新后块数/顺序一致
- [ ] 工具卡出现在「产生它的 step」的 content 下方，不堆在最终长正文下面
- [ ] 慢工具（如 `sleep`/长 bash）执行期间可见「执行中」（Start 到达至卡片插入 DOM 无额外延迟，且卡片归属正确 step 块）
- [ ] 流式输出时上翻 ≥1 屏后视口不被自动拉回；点「回到底部」恢复跟随
- [ ] pending 确认、拒绝、dangling、stop 行为与现契约一致
- [ ] 旧 jsonl（无 `reasoning` 字段）仍可正常打开，thinking 缺省即可

---

## 1. 决策点（实施前锁定）

### D1. Live / History 对齐策略 — **选定 A**

| 选项 | 做法 | 取舍 |
|------|------|------|
| **A（选定）** | Live 按模型 step 拆 assistant 块，与历史 1:1 | 与后端数据模型一致；刷新不跳变 |
| B | 历史合并为单头像 turn | 聚合边界复杂（pending/dangling/中断） |

**落地含义：**

- 前端不再「整轮用户消息只建一个 live 块」。
- 每个 step 的 `reasoning + text + 本批 tools` 落在同一壳内。
- 下一步模型调用开始时 **封口当前块、新建下一块**。

### D2. reasoning 字段名与边界 — **选定**

| 项 | 约定 |
|----|------|
| jsonl / history DTO 字段名 | `reasoning`（string，可空） |
| 是否进入 `chatMessage` 发模型 | **否**。仅日志 + UI |
| resume 重建内存 messages | 仍只恢复 `content/toolCalls`；`reasoning` 不进模型上下文 |
| 空 reasoning | 字段可省略或 `""`；UI 不渲染 thinking 块 |
| 旧日志 | 无字段 = 无 thinking，不报错 |

### D3. 工具「更早可见」做到哪一步 — **选定 L1+L2（本期）**

| 级别 | 内容 | 本期 |
|------|------|------|
| L1 | `toolCallStart` 后确保消费者能先画一帧（泵/前端），running 样式加强 | ✅ |
| L2 | 每个 step 在 `finalChunk` 后、执行前 **立刻** 对该批所有 call 发 Start（可先批量 Start 再串行 exec——可选） | ✅ 保持现有「逐个 Start→exec→End」，强化可见性即可 |
| L3 | 模型仍在流式吐 `tool_calls` 时就发 pending/start | ❌ 本期不做（改 adapter 事件模型，成本高） |

说明：问题 6 的「完全看不到」很大程度会被 **方案 A 拆块 + 智能贴底** 改善体感；慢工具本身 Start→exec 已能显示 running。本期不引入新的 SSE 事件类型。

### D4. thinking 交互 — **选定**

| 阶段 | summary | open |
|------|---------|------|
| 首包 `reasoningDelta` | 「思考中…」 | `true`（自动展开） |
| 本 step 出现首个 `textDelta` 或 `toolCallStart` 或 step 封口 | 「已思考」 | `false`（自动折叠，**除非用户手动点过**） |
| 历史回放 | 「已思考」 | `false` |
| 用户手动 toggle | 设置 `userToggledThinking=true`，之后不再自动改 open |
| pending 恢复块（`buildLiveFromHistory`） | 「已思考」 | `false`；**复用历史已渲染的 thinking 壳**，不再插入新空壳；续流 thinking 由新 step 块承担 |

### D5. 贴底阈值 — **选定**

- `nearBottom`：`scrollHeight - scrollTop - clientHeight <= 80`（px）
- 用户滚动时更新 `stickToBottom`
- 仅 `stickToBottom===true` 时自动 `scrollToBottom`
- 离底且流式中：右下角显示「↓ 回到底部」按钮（有新增量时可加小红点/文案「有新输出」）

### D6. step 边界如何让前端知道 — **选定：隐式推断（不新增 SSE 事件）**

不新增 `stepStart` 事件，用现有事件推断：

```text
规则（live）：
- send / confirm 开始：创建 step#1 live 块
- 收到 toolCallStart/End：挂到「当前 step 块」——仅当卡片是本块新建时；
  命中 toolCards 注册表原位更新（dangling 重放 / pending 恢复）不改 step 归属
- 当前块已经出现过「归属本块的」toolCallEnd（sawToolEnd=true），之后又收到
  textDelta 或 reasoningDelta：
    → 视为下一模型 step 开始：封口当前块，新建 step 块，增量写到新块
- 实现：让 upsertToolCardOnStart / resolveToolCardOnEnd 区分「注册表命中归位」
  与「本块新建」，仅后者将 currentStep.sawToolEnd 置 true；
  下一包 reasoningDelta/textDelta 触发 newStep()。
```

**边界情况：**

| 情况 | 处理 |
|------|------|
| step 无工具，直接 completed | 单块，正常 |
| step 有工具但无 text | 块内可无 contentEl 内容，仅 thinking+tools |
| confirmationRequired | 不 newStep；停在当前块 pending 卡 |
| continueConfirmation 后续又进模型 | 批准/拒绝后的下一包 reasoning/text → newStep（因已有 tool 阶段） |
| dangling 重放（发新消息时先执行上轮未闭环工具） | 工具事件归位历史灰卡，不置 sawToolEnd、不 newStep；后续模型输出仍落 step#1 |
| 仅 error | 不强制 newStep |
| stop 中断 | 不 newStep，当前块打「已中断」 |

> 若隐式推断在联调中不稳，**后备**：在 `agent.driveModelLoop` 每步开始 `yield` 一个新事件 `stepStart`（需改 `types.py` / `sseCodec` / 契约）。方案默认走隐式，后备写进风险表。注意：启用后备 = 修订 `streamOutputPlan.md` §6.2 已决事项（「不设 stepFinish 类事件」），须先更新上游契约再实施。

---

## 2. 目标架构

### 2.1 数据流（修复后）

```text
model SSE
  ├─ reasoning_content ──► reasoningChunk ──► reasoningDeltaEvent ──► UI thinking
  │                         └─ 累积 reasoningParts
  ├─ content ────────────► textChunk ───────► textDeltaEvent ─────► UI content
  └─ tool_calls(delta) ──►（仍只累积）──► finalChunk
                              │
                              ▼
                    appendAssistantMessage(
                      content, toolCalls, reasoning=joined, usage...
                    )  → jsonl.assistantMessage.reasoning
                              │
                              ▼
                    driveToolBatch: Start → exec → End  → UI 当前 step 块
                              │
                              ▼
                    下一步 completeStream → UI newStep()（见 D6）
```

### 2.2 Live DOM 结构（方案 A）

```text
messageList
  └─ user bubble
  └─ assistant step 1
  │    ├─ thinking (optional)
  │    ├─ content
  │    └─ tool cards (本 step)
  └─ assistant step 2
  │    ├─ thinking
  │    ├─ content
  │    └─ tool cards
  └─ assistant step 3
       ├─ thinking
       └─ content (最终回答)
```

History 已是「每 assistantMessage 一块」，补上 thinking 后与 live 对齐。

### 2.3 stream 状态机扩展（前端）

```js
// 概念结构（非最终代码）
stream = {
  phase: 'streaming' | 'waitingConfirm' | 'stopping',
  stickToBottom: true,
  steps: [ /* 已封口块，可选只持引用 */ ],
  currentStep: {
    live: { rowEl, bodyEl, thinkingEl, thinkingContentEl, contentEl, summaryEl, ... },
    textBuf: '',
    reasoningBuf: '',
    sawToolEnd: false,
    userToggledThinking: false,
  },
  abort, terminalSeen, pending
}
```

---

## 3. 分阶段实施计划

```text
Phase 0  准备与契约标注          （0.5d）
Phase 1  问题5 智能贴底          （0.5d）  ← 独立、低风险、立刻改善体验
Phase 2  问题4 reasoning 全链路  （1–1.5d）
Phase 3  问题1 流式 thinking UI  （0.5d）  ← 依赖 Phase2 字段约定，可并行前端
Phase 4  问题2+3 live 按 step 拆块（2–2.5d）← 核心结构改造
Phase 5  问题6 工具 running 可见 （0.5–1d）← 可与 Phase4 部分重叠
Phase 6  回归、契约文档、收尾     （0.5–1d）
```

**建议总工期：5–7 人日**  
**推荐合并 PR 策略：** Phase1 可单独合；Phase2+3 一起；Phase4+5 一起；Phase6 文档/回归。

依赖关系：

```text
Phase0
  ├─► Phase1（无依赖，可最先合）
  ├─► Phase2 ─► Phase3
  │                │
  └─► Phase4 ◄─────┘（Phase3 的 thinking 控件应复用于 step 块）
         └─► Phase5
                └─► Phase6
```

---

## 4. 各 Phase 详细设计

### Phase 0 — 准备

1. 通读 `chatUiStreamingIssues.md`、本方案、现 `streamOutputPlan.md` §6 事件表。  
2. 在 `webApiSpec.md` / `streamOutputPlan.md` 标「待修订」占位（正式 diff 放 Phase6）。  
3. 准备联调会话：启用 reasoning 的模型 + 会多 tool 的任务 + 可长跑 bash。

### Phase 1 — 智能贴底（问题 5）

#### 改动文件

| 文件 | 改动 |
|------|------|
| `webApp/frontend/js/chatView.js` | `stickToBottom`、条件滚动、回到底部按钮逻辑 |
| `webApp/frontend/styles.css` | 回到底部按钮样式 |
| `webApp/frontend/index.html` | 可选：按钮挂载点（或 JS 创建） |

#### 实现要点

1. `var stickToBottom = true`（可挂 `appStore.stream` 或模块级 + 会话切换重置）。  
2. `messageListEl` 监听 `scroll`：  
   `stickToBottom = (scrollHeight - scrollTop - clientHeight) <= 80`。  
3. 将所有流式路径的 `scrollToBottom()` 换为：

```js
function maybeScrollToBottom() {
  if (stickToBottom) scrollToBottom();
  else showJumpToBottom(true);
}
```

4. 发送新消息、打开会话 `reloadSession` 结束：强制 `stickToBottom=true` 并贴底一次。  
5. 「↓ 回到底部」：点击后 `stickToBottom=true; scrollToBottom(); hide button`。  
6. `goIdle` / `close` 时隐藏按钮。

#### 验收

- [ ] 流式中停在底部：仍自动跟随  
- [ ] 上翻后新 delta 不抢视口  
- [ ] 显示回到底部；点击恢复跟随  
- [ ] 切换会话 / 发送新消息重置为跟随  

---

### Phase 2 — reasoning 持久化（问题 4）

#### 改动文件

| 文件 | 改动 |
|------|------|
| `flamingoAgents/models/chatCompletions.py` | 累积 `reasoningParts`；写入 `responsePayload` |
| `flamingoAgents/core/types.py` | 视需要：`modelCompletion` / 解析结构带 `reasoning`（**可不进 chatMessage**） |
| `flamingoAgents/core/conversation.py` | `appendAssistantMessage` 写 `reasoning`；resume **忽略**回灌 |
| `webApp/backend/historyView.py` | DTO 增加 `reasoning` |
| `docs/webApiSpec.md` | messages 项增加字段说明（可 Phase6 统一改文档） |

#### 实现要点

**A. chatCompletions.consumeSseStream**

```python
reasoningParts: list[str] = []
# processSseData 内：
if reasoning:
    reasoningParts.append(reasoning)
    yield reasoningChunk(text=reasoning)

# 流结束：
reasoningText = ''.join(reasoningParts)
responsePayload = {
  'model': ...,
  'choices': [{'message': messagePayload}],
  'usage': ...,
}
if reasoningText:
    responsePayload['reasoning'] = reasoningText
    # 禁止：messagePayload 不得放 reasoning（保持 choices[0].message 与非流式同构，
    # 避免 convertMessage 误带出；reasoning 一律由 appendAssistantMessage 从顶层读取）
```

**B. conversation.appendAssistantMessage**

从 `responsePayload.get('reasoning')` 取字符串，写入 jsonl：

```python
event = {
  'type': 'assistantMessage',
  'content': message.content,
  'toolCalls': message.toolCalls,
  'usage': ...,
  'timings': ...,
  'model': ...,
}
if reasoning:
    event['reasoning'] = reasoning
```

**C. resume**

`_resumeFromLog` 重建 `chatMessage` 时 **不要** 把 reasoning 拼进 content，也不要新字段进模型消息。

**D. historyView**

```python
item = {
  'kind': 'assistant',
  'content': ...,
  'toolCalls': ...,
  'reasoning': event.get('reasoning') or '',  # 或仅非空时带
  ...
}
```

**E. stream=False 回退路径（`complete()`）**

非流式 `complete()` 返回 provider 原始 payload，reasoning 在 `choices[0].message.reasoning_content` 中。在出口归一化（约 5 行）：

```python
# complete() 返回前：
msg = payload.get('choices', [{}])[0].get('message', {})
if msg.get('reasoning_content'):
    payload['reasoning'] = msg['reasoning_content']  # 仅顶层，不删原字段、不入 chatMessage
```

#### 兼容

- 读路径：`event.get('reasoning')` 缺省 `''`
- stream=False 回退：同样经顶层 `reasoning` 落库（见 E）  
- 不改 usage / 计费  
- 不改发往 provider 的 request body

#### 验收

- [ ] 新对话 jsonl 的 assistantMessage 含 `reasoning`（有 think 的模型）  
- [ ] `GET /api/sessions/{id}/messages` 返回 `reasoning`  
- [ ] 旧 jsonl 无字段不报错
- [ ] stream=False 配置下 jsonl 同样含 `reasoning`（complete() 归一化生效）  
- [ ] resume 后下一轮请求 messages **不含** reasoning 正文  

---

### Phase 3 — 流式 thinking UI（问题 1）+ 历史 thinking（接 Phase2）

#### 改动文件

| 文件 | 改动 |
|------|------|
| `webApp/frontend/js/chatView.js` | thinking 展开/文案/历史渲染 |
| `webApp/frontend/styles.css` | 可选：思考中动画/样式 |

#### 实现要点

1. 抽取统一 `mountThinking(bodyEl, {reasoning, streaming})`：  
   - streaming 首包：unhide + open + summary「思考中…」  
   - 结束/历史：summary「已思考」、默认 open=false  
2. `summary` click 时标记 `userToggledThinking`。  
3. `appendAssistantHistory`：若 `msg.reasoning` 非空，在 content **之前** 插入 thinking 块。  
4. 与 Phase4 衔接：thinking 挂在 **step 块** 上，不是整轮唯一块。

#### 验收

- [ ] 流式中自动展开可见 think 正文  
- [ ] step 结束后（或转 text/tool）自动折叠为「已思考」（未手动打断时）  
- [ ] 刷新后历史可见折叠 thinking  
- [ ] 无 reasoning 的助手消息不出现空 thinking 壳  

---

### Phase 4 — Live 按 step 拆块（问题 2 + 3）

#### 改动文件

| 文件 | 改动 |
|------|------|
| `webApp/frontend/js/chatView.js` | **主战场**：stream.currentStep / newStep / 事件路由 |

#### 实现要点

**4.1 去掉「整轮单 live」**

`send()` / `confirm()`：

```js
stream.currentStep = createStep(); // 内含 createLiveAssistantBlock
stream.steps = [stream.currentStep];
```

**4.2 newStep()**

```js
function beginNewStepIfNeeded(eventKind) {
  var step = stream.currentStep;
  if (!step) { stream.currentStep = createStep(); return; }
  // 上一 step 已走过工具阶段，又来了模型输出 → 新 step
  if (step.sawToolEnd && (eventKind === 'textDelta' || eventKind === 'reasoningDelta')) {
    stream.currentStep = createStep();
    stream.steps.push(stream.currentStep);
  }
}
```

在 `textDelta` / `reasoningDelta` 入口先调 `beginNewStepIfNeeded`。

**4.3 工具挂到当前 step**

```js
function liveBodyEl() {
  return stream.currentStep.live.bodyEl;
}
// toolCallEnd 时（仅卡片为本块新建，非注册表归位）：
stream.currentStep.sawToolEnd = true;
```

**4.4 text/reasoning buffer 按 step**

每个 step 自有 `textBuf` / `reasoningBuf`，禁止跨 step 追加到旧 contentEl。

**4.5 pending / confirm**

- `confirmationRequired`：卡在 **当前 step**  
- `confirm()` 续流：不立刻 newStep；等后续 delta 按规则决定  
- `enterWaitingConfirm` 恢复：沿用历史最后 assistant 块为 currentStep 壳

**4.6 中断标记**

`markInterrupted` 打在 `currentStep.live` 上。

**4.7 与历史一致性自检**

同一次多 step 对话：流式结束不刷新时的 DOM 块数 ≈ 刷新后 `kind==assistant` 条数（允许 pending/中断边界差 0–1，需在回归记录）。

#### 验收

- [ ] 多 tool step 对话：live 多个头像，工具不在最终长文下方  
- [ ] 刷新后块顺序与 live 结束时一致（工具在对应 step 下）  
- [ ] 单 step 无工具：仍单头像  
- [ ] pending 确认卡仍可点批准/拒绝并续流  

---

### Phase 5 — 工具 running 可见（问题 6）

#### 改动文件

| 文件 | 改动 |
|------|------|
| `webApp/frontend/js/chatView.js` | running 态强调；Start 后 `maybeScrollToBottom` |
| `webApp/frontend/styles.css` | `.status-running` / 骨架闪烁 |
| `flamingoAgents/core/agent.py` | **可选**：`driveToolBatch` 在 Start 后、`executeToolCall` 前保持 yield 语义清晰（已是）；评估是否对同批先全部 Start 再 exec（**默认不改串行语义**） |
| `webApp/backend/agentManager.py` | **可选排查**：确认 `put(Start)` 不被批量缓冲；一般无需改 |

#### 实现要点

1. **前端**：`upsertToolCallOnStart` 立即插入 running 卡 + 可选 pulse 动画。  
2. **短工具**：接受「一闪而过」；可通过最小展示时间（如 running 至少 120ms 再切 done）优化体感——**可选，默认不做延迟假状态**，避免结果时间线造假。  
3. **慢工具**：人工用 `bash sleep 3` 验收「执行中」可持续看见。  
4. **不**在模型流式 tool_calls 阶段发假 Start（D3）。  
5. pending：未批准前不得变为 running（保持 confirmationRequired 路径）。

#### 验收

- [ ] `bash sleep 3`：先出现执行中，3s 后变完成  
- [ ] 快 `read`：至少出现卡片（状态可能很快变完成）  
- [ ] 需确认的工具：先待确认，批准后才执行中  
- [ ] 拒绝：被拒绝态，无执行中误报  

---

### Phase 6 — 回归与文档

1. 更新 `docs/webApiSpec.md`：`messages[].reasoning`  
2. 更新 `docs/streamOutputPlan.md`：如有行为说明（thinking UI / step 呈现，若影响事件语义）  
3. `docs/chatUiStreamingIssues.md` 顶部标记「修复方案见 chatUiStreamingFixPlan.md；实施状态…」  
4. 全路径手测清单（§6）打勾  
5. 如有 Playwright 基建则补 2–3 条关键路径；**无则不强行引入测试框架**（遵循项目「严谨使用测试框架」——此处按用户规则：**不主动加测试框架**；以手测清单为准）

---

## 5. 契约与 API 变更草案

### 5.1 `GET /api/sessions/{sessionId}/messages`

assistant 项增加：

```json
{
  "kind": "assistant",
  "content": "...",
  "reasoning": "可选，思维链全文",
  "toolCalls": [ ... ],
  "usage": { ... },
  "model": "...",
  "timestamp": "..."
}
```

- 旧客户端忽略未知字段 → 兼容  
- `reasoning` 可空字符串或省略  

### 5.2 jsonl `assistantMessage`

```json
{
  "type": "assistantMessage",
  "content": "...",
  "reasoning": "...",
  "toolCalls": [ ... ],
  "usage": { ... },
  "model": "...",
  "timings": { ... },
  "timestamp": "..."
}
```

### 5.3 SSE 事件集

**本期不新增事件类型。**  
仍为：`textDelta` / `reasoningDelta` / `toolCallStart` / `toolCallEnd` / `confirmationRequired` / `completed` / `error`。

### 5.4 发往模型的 messages

**无变更。** reasoning 不得进入 provider 请求。

---

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 隐式 step 边界误判 | live 多拆/少拆一块 | 联调多模型；不稳则加 `stepStart` 事件（后备） |
| reasoning 误进模型上下文 | 污染 prompt、泄思维、费 token | code review 卡 convertMessage / resume |
| 拆 step 后 pending 归位失败 | 确认框/卡片错位 | 回归 pending 专测；toolCallId 注册表保持全局 |
| markdown 全量重渲染 × 多 step | 性能 | 仅当前 step contentEl 重渲染（已是） |
| 自动折叠 thinking 打扰用户 | UX | userToggled 后不再自动改 open |
| 最小展示时间假 running | 时间线不真实 | 默认不做 |
| dangling 重放 + 拆 step 归属误判 | 幽灵空 step 块，live/历史块数不一致 | 仅本块新建卡片才置 sawToolEnd；M11 专测 |
| pending 恢复 thinking/正文归属错乱 | 双 thinking 壳或增量写错容器 | restored 块复用历史 thinking 壳；续流走新 step 块 |
| CSS 缓存导致「没改上」 | 误判失败 | 硬刷新；必要时 static 加版本 query |

---

## 7. 完整 TODOlist

> 勾选框供实施时打勾。顺序即推荐实施顺序。

### Phase 0 — 准备

- [ ] **T0.1** 确认 D1–D6 与产品一致（本方案默认已选 A / reasoning 不回灌 / 无新 SSE）
- [ ] **T0.2** 准备联调账号与模型：至少 1 个带 `reasoning_content` 的模型
- [ ] **T0.3** 准备复现脚本话术：多文件读取 + 长 bash + 需确认命令
- [ ] **T0.4** 在 `chatUiStreamingIssues.md` 顶部加链接指向本方案

### Phase 1 — 智能贴底（问题 5）

- [ ] **T1.1** `chatView.js`：增加 `NEAR_BOTTOM_PX = 80` 与 `stickToBottom` 状态
- [ ] **T1.2** `messageListEl` 绑定 `scroll` 更新 `stickToBottom`
- [ ] **T1.3** 实现 `maybeScrollToBottom()` / `showJumpToBottom()` / `hideJumpToBottom()`
- [ ] **T1.4** 替换流式路径所有无条件 `scrollToBottom`（text/reasoning/tool/confirm）
- [ ] **T1.5** 发送消息、`reloadSession` 完成、点击跳转按钮时强制贴底
- [ ] **T1.6** `styles.css` + 按钮 DOM（固定在消息区右下）
- [ ] **T1.7** `close` / `goIdle` / `showEmpty` / 切会话时重置状态（含隐藏「回到底部」按钮）
- [ ] **T1.8** 手测验收 Phase1 四条

### Phase 2 — reasoning 持久化（问题 4）

- [ ] **T2.1** `chatCompletions.py`：`reasoningParts` 累积；`responsePayload['reasoning']`
- [ ] **T2.2** 确认 `convertMessage` / request 构建 **不** 读取 reasoning
- [ ] **T2.3** `conversation.appendAssistantMessage`：jsonl 写入 `reasoning`
- [ ] **T2.4** `_resumeFromLog`：不把 reasoning 注入 `chatMessage.content`
- [ ] **T2.5** `historyView.loadMessages`：DTO 透传 `reasoning`
- [ ] **T2.6** 版本头 / description 小版本递增（涉及改动的 py 文件）
- [ ] **T2.7** 手测：新 jsonl 有字段；旧 jsonl 可打开；messages API 有字段
- [ ] **T2.8** `complete()`（stream=False 回退）出口归一化 `reasoning_content` → 顶层 `responsePayload['reasoning']`（不入 chatMessage）

### Phase 3 — thinking UI（问题 1 + 历史）

- [ ] **T3.1** 抽取 `buildThinkingBlock` 增强：summaryEl 可更新文案
- [ ] **T3.2** 流式 `reasoningDelta`：首包展开 +「思考中…」
- [ ] **T3.3** step 转 text/tool/封口：自动折叠 +「已思考」（尊重 userToggled）
- [ ] **T3.4** `appendAssistantHistory` 渲染 `msg.reasoning`
- [ ] **T3.5** 无 reasoning 不渲染空壳
- [ ] **T3.6** 手测：流式可见、结束后可回看、刷新仍在

### Phase 4 — live 按 step 拆块（问题 2 + 3）

- [ ] **T4.1** 设计并实现 `createStep()` / `stream.currentStep` / `stream.steps`
- [ ] **T4.2** `send()` / `confirm()` 改为创建 step 而非单一 `live`
- [ ] **T4.3** 实现 `beginNewStepIfNeeded`（D6 规则）
- [ ] **T4.4** `textDelta` / `reasoningDelta` 写入 **当前 step** buffer
- [ ] **T4.5** `toolCallStart/End` 挂到当前 step；仅卡片为本块新建时 `sawToolEnd=true`（注册表命中 = dangling/pending 恢复，不置位、不 newStep）
- [ ] **T4.6** 改造 `liveBodyEl` / `markInterrupted` / `createLiveAssistantBlock` 调用点
- [ ] **T4.7** pending / `enterWaitingConfirm` / `buildLiveFromHistory` 适配 step 模型：restored 块**复用历史已渲染的 thinking 壳**（不插入新空壳，避免双 thinking）；dangling 重放归位历史灰卡
- [ ] **T4.8** `discardPendingConfirm`、stop、error 路径适配
- [ ] **T4.9** 手测：多 step live 多头像；刷新对齐；最终正文独立块
- [ ] **T4.10** 手测：单 step、仅工具无正文、仅正文无工具

### Phase 5 — 工具 running（问题 6）

- [ ] **T5.1** running 样式加强（CSS pulse / 更明显「执行中」）
- [ ] **T5.2** 确认 Start 事件到达后立即插入 DOM（无批量 rAF 合并问题）
- [ ] **T5.3** 慢工具手测 `sleep`/长命令
- [ ] **T5.4** 快工具手测 read：卡片会出现
- [ ] **T5.5** pending 批准前不显示 running；批准后显示
- [ ] **T5.6** （可选）评估同批先全部 Start 再 exec——**默认不做**，若做需单独立项

### Phase 6 — 文档与总回归

- [ ] **T6.1** 更新 `webApiSpec.md` messages.reasoning
- [ ] **T6.2** 必要时补 `streamOutputPlan.md` UI/落库说明（事件集无新增则简述行为）
- [ ] **T6.3** 更新 `chatUiStreamingIssues.md` 实施状态
- [ ] **T6.4** 本方案文首状态改为「实施中/已完成」并填日期
- [ ] **T6.5** 总验收清单（§0.3）全部打勾
- [ ] **T6.6** 回归：dangling、stop、409 并发流、/model 切换放弃 pending、附件发送
- [ ] **T6.7** 若 T5.6（同批先全部 Start 再 exec）未实施，在 `chatUiStreamingIssues.md` 显式记录问题 6 根因 A（Start 发送时机）遗留至后续迭代

### 横切注意（每期都要）

- [ ] **TX.1** 前端改动提醒硬刷新；CSS 注意选择器优先级（勿再被通用类覆盖）
- [ ] **TX.2** 所有新建/修改代码文件头 Version 小版本 + Description
- [ ] **TX.3** 不引入测试框架；不写 exploit/无关重构
- [ ] **TX.4** 精准修改：不顺手「优化」无关相邻代码

---

## 8. 手测用例表（实施验收用）

| ID | 步骤 | 期望 |
|----|------|------|
| M1 | 发送需推理的问题 | 流式出现「思考中…」+ 正文；后变「已思考」 |
| M2 | M1 后刷新 | 历史有折叠 thinking，内容一致 |
| M3 | 要求连续读 3 个文件再总结 | live ≥2 个 assistant 块；工具在中间块；最终块长文 |
| M4 | M3 后刷新 | 块数/工具归属与结束前一致 |
| M5 | 流式中上翻 | 不强制回底；按钮可回底 |
| M6 | `bash sleep 5` | 先「执行中」再「完成」 |
| M7 | 触发需确认命令 | 待确认；批准后执行中→完成；拒绝→被拒绝 |
| M8 | 点停止 | 半截「已中断」；可上翻 |
| M9 | 打开旧会话（升级前 jsonl） | 正常显示，无 reasoning 不炸 |
| M10 | 纯附件 / 空 think 模型 | 无空 thinking 壳；布局正常 |
| M11 | 流式中断后刷新（制造 dangling）→ 发新消息 | dangling 工具归位历史灰卡执行；live 无空头像块；刷新后块数一致 |

---

## 9. 回滚策略

| Phase | 回滚 |
|-------|------|
| 1 | 还原 chatView 滚动相关 + 删按钮样式 |
| 2 | jsonl 多写的 `reasoning` 字段可保留（向前兼容）；代码回退即可 |
| 3 | 仅前端，回退 chatView thinking 交互 |
| 4 | 风险最高：保留 Phase2 落库，live 临时退回单块（feature flag 可选 `USE_STEP_LIVE=true`） |
| 5 | 仅样式/微调，易回滚 |

建议 Phase4 若工期紧：**先合 1+2+3**，4+5 下一迭代——但问题 2/3 用户感知强，仍建议同迭代完成。

---

## 10. 文件改动总表

| 文件 | P1 | P2 | P3 | P4 | P5 | P6 |
|------|----|----|----|----|----|-----|
| `webApp/frontend/js/chatView.js` | ● | | ● | ● | ● | |
| `webApp/frontend/styles.css` | ● | | ○ | | ● | |
| `webApp/frontend/index.html` | ○ | | | | | |
| `flamingoAgents/models/chatCompletions.py` | | ● | | | | |
| `flamingoAgents/core/conversation.py` | | ● | | | | |
| `flamingoAgents/core/types.py` | | ○ | | | | |
| `webApp/backend/historyView.py` | | ● | | | | |
| `flamingoAgents/core/agent.py` | | | | | ○ | |
| `docs/webApiSpec.md` | | | | | | ● |
| `docs/streamOutputPlan.md` | | | | | | ○ |
| `docs/chatUiStreamingIssues.md` | | | | | | ● |
| `docs/chatUiStreamingFixPlan.md` | 本文件 | | | | | ● |

● 必改　○ 可选/视情况

---

## 11. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-08-08 | 首版：六问题修复方案、D1–D6 决策、六阶段计划、完整 TODOlist、验收与回滚 |
| 1.1 | 2026-08-08 | pi 审核修订：补 dangling 重放 step 归属规则（S1）、pending 恢复 thinking 复用（M1）、stream=False reasoning 归一化（M2）、清理 D6 草稿歧义并声明后备冲突（M3）、工具可见目标/验收口径对齐 D3（M4）、补风险 2 条、M11 用例、showEmpty 重置、Phase4 工期上调 |
