'''
Author: wilbur
Version: 1.2
Date: 2026-08-11
Description: 前端流式展示「迟钝」根因落档与分阶段修复方案（含实测证据、决策点、契约边界、文件改动、分层验收、完整 TODOlist）。v1.1 按 docs/codeReview/260811_streamingLatencyFixPlan.md 修订：分层验收现象 B、锁死可执行前缀算法、补全 flush 清单、修正全链路/L 编号/默认改动面。v1.2：Phase 1/2/3 已实施（chatCompletions.py v1.12 read1、agent.py v1.12 可执行前缀批量 Start、chatView.js v1.7 rAF paint 合并），T1.3/T3.4 脚本验证通过；Phase 3.5 让帧待手测触发；手测项待真机确认。
'''

# 流式展示迟钝修复方案

- Author: wilbur
- Version: 1.1
- Date: 2026-08-11
- 相关诊断（会话分析 + 本地复现）：见本文 §1
- 上游相关：`docs/chatUiStreamingIssues.md`（问题 6）、`docs/chatUiStreamingFixPlan.md`（Phase5 / T5.6 遗留）
- 相关契约：`docs/streamOutputPlan.md`、`docs/webApiSpec.md`
- 审核：`docs/codeReview/260811_streamingLatencyFixPlan.md`（v1.0 → v1.1）
- 状态：**Phase 1/2/3 已实施（脚本验证通过）；手测与 Phase 3.5 待确认**

---

## 0. 目标与非目标

### 0.1 用户现象（本次要治的）

| # | 现象 | 体感 |
|---|------|------|
| A | LLM 思考时界面卡住，过一会忽然出来一大截，随后又正常 | 流式「不跟手」 |
| B | 显示「已思考」后界面空白，过一会弹出多张**已执行完成**的工具卡 | 工具「执行中」几乎看不见 |

### 0.2 目标（修复后用户应感知到）

1. **思考/正文增量接近模型真实吐字节奏**：不再长时间真空后一次性喷出大段（在 provider 本身匀速时）。
2. **工具意图尽早可见**：思考结束后到工具真正执行前，不应长时间「什么都没有」；至少在可执行快照就绪（final 后）立刻发出 Start，慢/中速工具能看到 running。
3. **同批工具先 Start 再 exec（结构正确）**：后端事件序保证多张卡先进入 running 语义，再各自 End；快工具肉眼 running 靠「让帧」尽力，不单靠改顺序硬承诺（见 §0.4 分层）。
4. **长正文不卡主线程**：流式 markdown 渲染不因「每 token 全量 parse」导致掉帧。
5. **不破坏现有能力**：pending 确认、dangling、stop、step 拆块、reasoning 落库、SSE 事件名与终态语义保持可用。

### 0.3 非目标（本期默认不做，除非单独立项）

| 项 | 说明 |
|----|------|
| 工具 batch **并行执行** | 仍串行执行；只调整「Start 发出顺序 / 可见时机」 |
| 改 provider / 换 HTTP 栈 | 不引入 httpx/aiohttp；继续 `urllib` + 泵线程 |
| 大规模前端框架化 | 继续原生 JS |
| 打字机假动画（无真实 token 时伪造输出） | 只做真实事件的平滑展示 |
| 修改计费 / maxModelSteps / 模型配置 schema | 与本专题无关 |
| 流式期 tool 骨架（L3 / skeleton） | 二期；见 §2 D3、Phase 5 |

### 0.4 成功标准（总验收，分层）

| # | 标准 | 级别 |
|---|------|------|
| A1 | 含 reasoning 的模型：流式中 thinking 正文**持续增长**，无明显「≥1s 真空后整段贴上」（局域网/直连 provider） | 必过 |
| A2 | 适配器：chunked SSE 下 `iterSseData` 有数据即向下游交付，不为凑满 4096 阻塞 | 必过 |
| B-slow | 同批 ≥2 个**可感知耗时**工具（如 bash/sleep ≥200ms）：先出现 ≥2 张 running，再各自 done/error | 必过 |
| B-fast | 同批 ≥2 个快工具（read 等）：后端事件序为「可执行前缀全部 Start → 再串行 End」；前端尽力露 running；若同帧粘连允许极短/不可见，但**不得**结构错误（无 Start 语义、错序、双卡） | 必过（事件序） |
| B-paint | 快工具肉眼能瞥见 running：T3 后仍「只见完成卡」时，做 §3 Phase3.5 让帧后应可见（至少一帧级） | 条件必过 |
| C | 慢工具：Start 后 running 持续至 End，不闪没 | 必过 |
| D | 长正文（≥2k 字）流式时页面可滚动、不长期假死；流终/stop 无缺尾 | 必过 |
| E | pending / 拒绝 / dangling / stop / 刷新历史 与现契约一致 | 必过 |
| F | 不新增破坏性 SSE 事件名（骨架卡仅 Phase5，走显式可选事件） | 必过 |

---

## 1. 根因落档（含实测）

### 1.1 全链路（对照现实现）

```text
Provider SSE (chunked)
  → chatCompletions.iterSseData(response.read(4096))   ← A1 现状
  → textChunk / reasoningChunk / (内部 toolCallAccum，不外发)
  → agent.driveModelLoop yield textDelta / reasoningDelta
  → [finalChunk 后] appendAssistantMessage → driveToolBatch
       现状：yield Start → executeToolCall → yield End（逐个串行）
  → streamPump._broadcast → history + 各 subscriber Queue
  → sseCodec.sseGen(queue.get) → FastAPI StreamingResponse
       （响应头已有 X-Accel-Buffering: no）
  → 浏览器 fetch ReadableStream
  → sse.js 按 \n\n 拆帧 → 同一次 reader.read 内多帧同步 onEvent
  → chatView.onStreamEvent → DOM
       reasoning: textContent
       text: marked.parse(全文) + DOMPurify + innerHTML
       tool: append running / 改 status
```

说明：泵在每个 agent `yield` 后即 `_broadcast`，再拉下一事件——**后端 Start 与 exec 之间已让出给泵**；快工具「看不见 running」更多卡在 **浏览器同任务同步消费多帧不 paint**，而非泵没 put。

### 1.2 现象 A：卡住 → 一大截

#### 根因 A1（主因，已本地复现）— `response.read(4096)` 在 chunked 上阻塞凑批

**代码：**

```python
# flamingoAgents/models/chatCompletions.py — iterSseData
data = response.read(4096)
```

**机制：**

- 模型流几乎都是 `Transfer-Encoding: chunked`
- CPython `HTTPResponse.read(amt)` 对 chunked 走 `_read_chunked(amt)`
- 会**继续读后续 HTTP chunk**，直到凑够 `amt` 或流结束
- **不是**「底层有多少先返回多少」

**本地复现（2026-08-11）：**

模拟 chunked SSE，每 ~120ms 发一帧小 data：

| API | 行为 |
|-----|------|
| `read(4096)` | 约 1s 后**一次**返回全部 ~1.4KB；流式实时性归零 |
| `read1(4096)` | 每帧立即返回（wait≈0 / ≈interval）；节奏与 server 一致 |

`urllib.request.urlopen` 返回的正是 `http.client.HTTPResponse`，路径一致。

**结论：** 适配器入口即可把「匀速小增量」变成「长时间阻塞 + 一次性释放」。  
这足以单独解释思考阶段的「卡很久再喷一大截」。  
**上一版分析曾把此项降为次要网络细节，权重错误；以本实测为准。**

#### 根因 A2（次因，可叠加）— Provider / 代理突发

- 部分 provider 本身批量吐 reasoning
- 中间代理可能缓冲（已有 `X-Accel-Buffering: no`，主要防 Nginx 类）
- 在 A1 修掉后，若仍突发，再靠前端平滑（A3）兜底

#### 根因 A3（次因，正文路径更重）— 前端高频同步重渲染

```js
// webApp/frontend/js/chatView.js
case 'textDelta':
  textBuf += data.text;
  renderMarkdown(contentEl, textBuf); // 每次全文 marked + DOMPurify + innerHTML
```

- 思考路径用 `textContent`，相对轻；**纯 thinking 卡顿不能主要甩给 marked**
- 长正文时全量 markdown 会占主线程，造成「掉帧 → 积压 → 忽然一大截」
- `sse.js` 同一次 `reader.read` 内多帧同步 `onEvent`，中间不让 paint

### 1.3 现象 B：已思考 → 空白 → 已完成工具卡

#### 根因 B1（主因）— `toolCallStart` 发得太晚

与 `docs/chatUiStreamingIssues.md` 问题 6 一致，**现状未改**：

```text
模型流式阶段：
  reasoning/text → 实时 yield
  tool_calls 片段 → 只进 toolCallAccum，不对外发事件   ← 空白窗口

finalChunk 到手：
  appendAssistantMessage（写 jsonl）
  才 driveToolBatch → toolCallStart
```

因此：thinking 折叠成「已思考」之后，到真正 Start 之间，UI 可以长时间无新节点。  
**本期批量 Start 不消除 final 前空白**（需 Phase5 skeleton）。

#### 根因 B2 — 快工具 Start/End 连发 + batch 串行 + 浏览器同帧消费

```python
# driveToolBatch 现状
yield toolCallStart
result = executeToolCall(...)  # 同步阻塞
yield toolCallEnd
# 下一个工具必须等上一个 End
```

- 快工具（read 等）毫秒级 → 泵连续 broadcast 多帧
- 浏览器同一次 `reader.read` 内同步处理 Start+End → **只 paint 终态**
- 用户几乎只看到完成卡
- 同批不会「多卡同时 running」（串行 exec）

**批量改序（先全部 Start）解决事件结构/顺序；肉眼 running 还需让帧（Phase3.5）。**

#### 根因 B3 — A1 放大剂

`read(4096)` 也会把已入队的多帧 SSE 在上游侧进一步粘连；修 A1 有助于 B 的「成批弹出」，但**不消除** B1 的结构性空白，也**不保证**浏览器 paint running。

### 1.4 与既有 fixPlan 的关系

| 项 | 状态 |
|----|------|
| chatUiStreamingFixPlan Phase1–5 | 已实施：贴底、thinking 展开、step 拆块、running 动画等 |
| 问题 6 根因 A（Start 时机） | **遗留**；旧 T5.6「同批先全部 Start 再 exec」= **本方案 D2 / Phase3** |
| 问题 6 根因 L3（流式期 tool 骨架） | 明确非目标，本期仍二期 |
| `read` vs `read1` | **既往文档未覆盖**；本次新增为 P0 |

**编号对照（避免与旧 fixPlan D3 的 L1/L2/L3 混淆）：**

| 本方案称呼 | 含义 | 旧 fixPlan |
|------------|------|------------|
| read1 修复 | 适配器不凑批 | 未覆盖 |
| 批量 Start | final 后可执行前缀先全部 Start 再串行 exec | T5.6（曾默认不做） |
| 让帧 | 前端消费 Start 串后 rAF 再处理 End | 未覆盖 |
| skeleton | 流式 `tool_calls` 期发 preparing | 旧 L3（未做） |

下文决策编号用 **D1–D6**；阶段用 **Phase0–5**，不再单独用 L0–L3 当主称呼（§2 D3 表内仅作级别别名）。

---

## 2. 决策点（实施前锁定）

### D1. 适配器读 API — **选定 `read1`**

| 选项 | 做法 | 取舍 |
|------|------|------|
| **A（选定）** | `response.read1(4096)` | 流式正确语义；改动面最小 |
| B | `readline` / 自管 socket | 复杂，chunked 头要自己处理 |
| C | 换 httpx stream | 依赖与栈变更，超出本期 |

**落地：**

```python
# 优先 read1；无该方法时退回 read（兼容，不静默吞 IO 错误）
readChunk = getattr(response, 'read1', None)
data = readChunk(4096) if callable(readChunk) else response.read(4096)
```

半行 `buffer` 逻辑保持不变。`amt=4096` 仅作上限，不要求凑满。  
`read1` 返回 `b''` 且流结束 → 与现逻辑一致 `break`；不对短暂空读做复杂重试。

### D2. 工具 Start 批策略 — **选定「可执行前缀：先全部 Start，再串行 exec」**

| 选项 | 做法 | 本期 |
|------|------|------|
| **A（选定）** | 从 startIndex 起收集连续可执行前缀（unknown 或免确认），先依次 yield 全部 Start，再串行 exec+End；遇需确认则停 | ✅ |
| B | 保持 Start→exec→End，仅 sleep 让出 | ❌ 默认不加后端 sleep |
| C | 并行 exec | ❌ 非目标 |

**唯一合法顺序示例：**

```text
[free, free]           → S1 S2 E1 E2
[free, needConfirm]    → S1 E1 → confirmationRequired(call2)   # 不 Start#2
[needConfirm, free]    → confirmationRequired(call1)           # 无任何 Start
[free, unknown, free]  → S1 S2 S3 E1 E2 E3                     # unknown 也 S+E
```

**契约红线（`streamOutputPlan.md` §6.2）：**

- 需确认工具：只发 `confirmationRequired`（终态），**不**发 Start
- 拒绝路径：只发 End（isError），不发 Start
- 未知工具：Start + 合成 error End，可进前缀
- **禁止**先对「确认点之后」的工具 Start

**不动的路径：**

- `driveConfirmation` 批准：该工具 Start→exec→End，再 `driveToolBatch(..., currentIndex+1)`
- 拒绝：仅 End + 续 batch
- dangling：复用同一 `driveToolBatch`

### D3. 「已思考后空白」做到哪一步 — **本期：read1 + 批量 Start + 条件让帧；skeleton 二期**

| 级别别名 | 内容 | 本期 |
|----------|------|------|
| — | 修 `read1`（减少粘包） | ✅ 必做 |
| — | final 后、exec 前：可执行前缀批量 Start（D2） | ✅ 必做 |
| — | 快工具仍不可见 running → 前端让帧（Phase3.5） | ✅ 条件必做 |
| skeleton | 模型仍在吐 `tool_calls` 时发 preparing/skeleton | ⬜ 二期 |

**skeleton 若做（预研，不在本期默认范围）：**

| 方案 | 说明 |
|------|------|
| 新事件 `toolCallPreparing` | 不破坏 Start/End 配对；前端画骨架；final 后 Start 升级 running |
| 提前发「参数未齐」的 Start | 碰配对不变式，**不推荐** |

### D4. 前端渲染策略 — **选定 rAF 合并 + 强制 flush 清单**

| 路径 | 策略 |
|------|------|
| `reasoningDelta` | buffer + `scheduleLivePaint`；rAF 内 `textContent = buf` |
| `textDelta` | buffer + `scheduleLivePaint`；paint 时再 `renderMarkdown` |
| 工具 Start/End / confirm | **立即** DOM，不进文本 paint 队列 |
| 文本与工具交错 | 改工具卡前可先 `flushLivePaint`（可选）；工具本身不节流 |

**`flushLivePaint(step)` 强制调用点（漏则丢尾，必测）：**

1. `beginNewStepIfNeeded` **即将替换** `currentStep` 之前  
2. `completed` / `error` / `confirmationRequired` 入口（改 phase 前）  
3. 进入 `stopping` 时（前端 requestStop 侧）  
4. `goIdle` / `onStreamClosed`（双保险）  
5. `collapseThinkingIfOpen` 前：flush 须**同时**写 reasoningBuf 与 textBuf（避免「已思考」少字）

**不采用：** 流式期纯文本、终态才 markdown（可作后续优化）。

### D5. 观测与回归 — **选定最小埋点开关**

- 默认关闭；建议环境变量 `FLAMINGO_STREAM_TRACE=1`（或前端 debug 开关）
- 最小时间点（够对比即可，不做观测平台）：

```text
t0 适配器侧首 reasoning/text yield
t1 前端首 reasoningDelta 处理
t2 已思考折叠
t3 首 toolCallStart UI
t4 首 toolCallEnd UI
+ renderMarkdown 耗时（可选）
```

### D6. 兼容与回滚

| 项 | 策略 |
|----|------|
| `read1` | 项目 Python 3.13+；`getattr` fallback 到 `read`；不吞 IO 异常 |
| D2 事件顺序 | 前端按 id upsert；重点回归 dangling/pending/拒绝 |
| 前端节流 | 仅 live 路径；历史渲染不变 |
| 让帧 | 仅影响消费节奏，不改事件语义 |

---

## 3. 分阶段实施计划

### Phase 0 — 基线观测（可选但推荐，0.5h）

改代码前抓一轮真实会话（可临时 trace）：

| 点 | 记什么 |
|----|--------|
| t1 | 首 reasoning UI 延迟 |
| — | 相邻 reasoning 间隔分布 |
| t2→t3 | 「已思考」到首张工具卡间隔 |
| t3→t4 | Start 与 End 到达间隔 |

**验证：** 有数字基线，修完可对比（写入 §8）。

### Phase 1 — 适配器流式读修复（P0，核心）

**文件：** `flamingoAgents/models/chatCompletions.py`

**改动：**

1. `iterSseData`：`read` → 优先 `read1`（见 D1 代码）  
2. 文件头 Version / Description 更新  
3. `if not data: break` 保持  
4. 自测：chunked 慢发 SSE，确认多次增量

**验证：**

- [ ] 本地 mock chunked：多次 `read1` 分次返回  
- [ ] 真实 thinking 模型：thinking 区持续增长，不再整段延迟粘贴  
- [ ] `stream=False` 回退不受影响  
- [ ] 错误流 / `[DONE]` / 中断仍正常 final / error  

**风险：** 极低。半行缓冲本就为碎包设计。

### Phase 2 — 前端 paint 合并（P1）

**文件：** `webApp/frontend/js/chatView.js`（默认不改 `sse.js`；让帧见 Phase3.5）

**改动：**

1. step 增加 `paintScheduled` + `scheduleLivePaint` + `flushLivePaint`  
2. `textDelta` / `reasoningDelta`：只改 buffer + schedule；DOM 在 rAF / flush  
3. **按 D4 强制清单 flush**（含 stopping / goIdle / step 切换）  
4. `toolCall*` / `confirmationRequired`：即时 DOM  
5. `maybeScrollToBottom` 放在 paint 回调末尾  

**伪代码：**

```js
function flushLivePaint(step) {
  if (!step || !step.live) return;
  step.paintScheduled = false;
  if (step.reasoningBuf != null && step.live.thinkingContentEl) {
    step.live.thinkingContentEl.textContent = step.reasoningBuf;
  }
  if (step.textBuf) {
    renderMarkdown(step.live.contentEl, step.textBuf);
  }
}

function scheduleLivePaint(step) {
  if (step.paintScheduled) return;
  step.paintScheduled = true;
  requestAnimationFrame(function () {
    if (!step.paintScheduled) return; // 已被 flush 清掉
    flushLivePaint(step);
    maybeScrollToBottom();
  });
}

function beginNewStepIfNeeded(eventKind) {
  // ...
  if (step.sawToolEnd && (eventKind === 'textDelta' || eventKind === 'reasoningDelta')) {
    flushLivePaint(step); // 必做
    stream.currentStep = createStep();
    stream.steps.push(stream.currentStep);
  }
}
```

**验证：**

- [ ] 长正文流式：滚动/点击不长期假死  
- [ ] 流结束瞬间正文完整（无缺尾）  
- [ ] stop 后已缓冲内容不丢（停止后增量仍可按现逻辑丢弃，但 stop 前 buffer 必须上屏）  
- [ ] step 切换时旧块完整  
- [ ] 工具卡仍即时（不被文本 rAF 推迟）  

**风险：** 中低。漏 flush 会丢尾——验收必须盯终态与 stop。

### Phase 3 — 同批可执行前缀批量 Start（P1，对接旧 T5.6）

**文件：** `flamingoAgents/core/agent.py`（`driveToolBatch`）

**唯一算法（删除一切「全批先 Start」草稿）：**

```python
def driveToolBatch(self, sessionId, toolCalls, startIndex):
    currentConversation = self.getConversation(sessionId)
    index = startIndex
    while index < len(toolCalls):
        # 1) 收集从 index 起的可执行前缀：unknown 或 免确认；遇 requiresApproval 停止扩展
        prefix = []  # list[(call, definition|None)]
        while index + len(prefix) < len(toolCalls):
            call = toolCalls[index + len(prefix)]
            definition = self.toolRegistry.get(call.toolName)
            if definition is None:
                prefix.append((call, None))
                continue
            decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
            if decision.requiresApproval:
                break
            prefix.append((call, definition))
        # 2) 前缀全部 Start（preview：unknown 用 str(arguments)，其余 buildToolPreview）
        for call, definition in prefix:
            preview = (
                str(call.arguments) if definition is None
                else self.buildToolPreview(definition, call)
            )
            yield toolCallStartEvent(toolCall=call, preview=preview)
        # 3) 前缀串行 exec + End；jsonl 仍只写 toolResult（不写 Start 记录）
        for call, definition in prefix:
            if definition is None:
                result = self.makeUnknownToolResult(call)
            else:
                result = self.executeToolCall(call)
            currentConversation.addToolResult(result)
            yield toolCallEndEvent(toolResult=result)
        index += len(prefix)
        # 4) 下一项需确认：不 Start，setPending + confirmationRequired + return True
        if index < len(toolCalls):
            call = toolCalls[index]
            definition = self.toolRegistry.get(call.toolName)
            decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
            # definition 必非 None 且 requiresApproval（否则应进前缀）
            confirmationId = 'confirm_' + uuid4().hex[:12]
            currentConversation.setPending(pendingConfirm(
                sessionId=sessionId,
                confirmationId=confirmationId,
                reason=decision.reason,
                toolCalls=toolCalls,
                currentIndex=index,
            ))
            yield confirmationRequiredEvent(
                confirmationId=confirmationId,
                reason=decision.reason,
                commandPreview=self.buildToolPreview(definition, call),
                toolCall=call,
            )
            return True
    return False
```

实现时注意：`evaluateToolCall` 在「组前缀」与「确认分支」可能各调一次——可接受；若要避免双评估，组前缀时缓存 decision 即可。

**jsonl：** 仍为 assistant（含 toolCalls）先落盘，再按执行顺序 `addToolResult`；批量 Start **不改变落盘内容**，只改变 SSE 事件序。

**验证（事件序表，必过）：**

| # | toolCalls | 期望事件序（摘要） |
|---|-----------|-------------------|
| 1 | `[free]` | S1 E1 |
| 2 | `[free, free]` | S1 S2 E1 E2 |
| 3 | `[free, needConfirm]` | S1 E1 → confirmationRequired(call2) |
| 4 | `[needConfirm, free]` | confirmationRequired(call1)（无 Start） |
| 5 | `[free, unknown, free]` | S1 S2 S3 E1 E2 E3 |
| 6 | 批准后续 | Start→End 正常，无双 Start |
| 7 | 拒绝 | 仅 End；配对例外保持 |
| 8 | dangling / stop | 无死锁、锁释放顺序不变 |

**手测 UI：**

- [ ] B-slow：≥2 可感知耗时工具先多 running 再 done  
- [ ] B-fast：事件序正确；肉眼 running 见 Phase3.5  

**风险：** 中。确认边界已用上表锁死。

**默认不加后端 `sleep`：** 泵已在 yield 边界 broadcast；`sleep(0)` 无意义；`sleep(0.01)` 仅作 debug 开关，不进默认路径。

### Phase 3.5 — 快工具让帧（条件 P1）

**触发：** Phase3 手测后，同批快工具仍「只见完成卡」（B-paint 未过）。

**文件：** 优先 `webApp/frontend/js/chatView.js` 或极小改 `sse.js`（**允许**，不再写死禁止）。

**推荐做法（前端）：**

```text
在 SSE 消费侧：若刚处理完一条 toolCallStart，
且缓冲区内下一条是 toolCallEnd（同批连发），
则 await 1× requestAnimationFrame 后再继续 onEvent。
```

备选（弱，仅 debug）：后端每个 Start yield 后 `time.sleep(0.016)`，开关控制。

**验证：** 同批 2+ 快工具至少能瞥见 running 态一帧级；慢工具行为不变。

### Phase 4 — 回归与手测矩阵（P0 收尾）

| 场景 | 期望 |
|------|------|
| 强 reasoning 模型一轮纯回答 | thinking 持续出字；完成后折叠 |
| 思考 + 多 read + 最终回答 | final 前空白仍可能在（无 skeleton）；final 后 Start 及时；事件序正确 |
| 同批 2+ 慢工具 | 多 running → 各自 done |
| 需确认 bash（表 #3/#4） | 确认框正常；无错误双 Start |
| 拒绝确认 | 仅拒绝卡/End；可继续 |
| stop 中途 | 已缓冲文本上屏；中断标记；dangling 合理 |
| 刷新历史 | reasoning/tools 与 live 结构一致 |
| stream=False | 仍只 final 一次，不炸 |

### Phase 5（二期，可选）— 流式 tool 骨架

仅当 Phase1–3.5 后「已思考→工具」空白仍不可接受时立项。

**草案：**

1. adapter 在 `tool_calls` 首次出现 name/id 时 yield skeleton chunk  
2. agent → `toolCallPreparing`（新 SSE event）  
3. 前端骨架卡；final 后 Start 升级 running  
4. 流失败：skeleton → error/移除  

**契约变更：** `docs/streamOutputPlan.md`、`docs/webApiSpec.md`、`sseCodec.py`、前端。  
**本期 TODO 只留冰区项。**

---

## 4. 文件改动清单（本期默认）

| 文件 | Phase | 改动摘要 |
|------|-------|----------|
| `flamingoAgents/models/chatCompletions.py` | 1 | `read`→`read1`（getattr fallback）；头注释 |
| `webApp/frontend/js/chatView.js` | 2 / 3.5 | live paint 合并；flush 清单；可选让帧 |
| `webApp/frontend/js/sse.js` | 3.5 条件 | 仅当让帧放在拆帧层时极小改 |
| `flamingoAgents/core/agent.py` | 3 | `driveToolBatch` 可执行前缀批量 Start |
| `docs/streamingLatencyFixPlan.md` | 全程 | 本方案；实施后勾 TODO / 升版本 |
| `docs/chatUiStreamingIssues.md` | 收尾 | 链到本方案；问题 6 / 旧 T5.6 落地注记 |
| `docs/streamOutputPlan.md` | 仅 Phase5 | preparing 事件 |
| `docs/webApiSpec.md` | 仅 Phase5 | SSE 表 |

**默认不改：** `sseCodec.py` 事件集（无 Phase5 时）、泵线程结构、确认 API。  
**允许条件改：** `sse.js`（让帧）、`agentManager.py`（仅 trace 埋点）。

---

## 5. 风险与回滚

| 风险 | 等级 | 缓解 |
|------|------|------|
| `read1` 异常空读 | 低 | `if not data: break`；手测主流 provider |
| rAF 漏 flush 丢尾字 | 中 | D4 强制清单；stop/终态必测 |
| 批量 Start 与 confirmation 边界搞错 | 中 | 唯一算法 + 事件序表 8 条 |
| 批量 Start 后快工具仍不可见 running | 中 | Phase3.5 让帧；验收分层不把肉眼绑死在 Phase3 alone |
| 前端依赖「Start 后立刻 End」 | 低 | 现有 upsert 按 id，无此假设 |
| 修完仍突发 | 低 | provider；前端节流兜底；可再开 Phase5 |

**回滚：** 各 Phase 独立可逆；优先回滚 Phase3.5 → Phase3 → Phase2；Phase1 证据最硬一般不回滚。

---

## 6. 工期粗估

| Phase | 估时 | 依赖 |
|-------|------|------|
| 0 基线 | 0.5h | 无 |
| 1 read1 | 0.5–1h | 无 |
| 2 paint | 1–2h | 建议 1 后验 A |
| 3 批量 Start | 1–2h | 建议 1 后 |
| 3.5 让帧 | 0.5–1h | 3 后手测触发 |
| 4 回归 | 1–2h | 1–3(.5) 后 |
| **合计** | **约 4.5–8.5h** | |
| 5 skeleton | 另估 1–2d | 可选 |

---

## 7. TODOlist

### Phase 0 — 基线（可选）

- [ ] **T0.1** 选定 1 个强 reasoning 模型 + 1 个多工具会话场景作基线
- [ ] **T0.2** 记录 t1 / 相邻 delta / t2→t3 / t3→t4（见 D5）
- [ ] **T0.3** 基线数字记入 §8

### Phase 1 — `read1`（P0）

- [x] **T1.1** `iterSseData`：优先 `read1(4096)`，`getattr` fallback `read`
- [x] **T1.2** 更新 `chatCompletions.py` 文件头 Version / Description
- [x] **T1.3** 本地 mock chunked SSE：确认分次返回（§1.2 表）——`scripts/verifyRead1.py`：首帧 12ms、相邻间隔≈发送间隔 120ms
- [ ] **T1.4** 真模型手测：thinking 持续增长
- [ ] **T1.5** 回归：`stream=False`、`[DONE]`、HTTP 错误、用户 stop（`[DONE]` 已在 T1.3 覆盖；其余待手测）

### Phase 2 — 前端 paint 合并（P1）

- [x] **T2.1** step：`paintScheduled` + `scheduleLivePaint` + `flushLivePaint`
- [x] **T2.2** `reasoningDelta` / `textDelta` → buffer + schedule
- [x] **T2.3** 按 D4 清单强制 flush（step 切换 / 终态 / stopping / goIdle / onStreamClosed；collapse 前双 buffer）
- [x] **T2.4** 工具与确认事件同步即时 DOM
- [x] **T2.5** `maybeScrollToBottom` 合并进 paint
- [x] **T2.6** 更新 `chatView.js` 文件头 Version / Description
- [ ] **T2.7** 手测：长正文不假死；流终无缺字；stop 前 buffer 上屏；工具卡不因 rAF 晚一拍

### Phase 3 — 批量 Start（P1 / 旧 T5.6）

- [x] **T3.1** 实现 `driveToolBatch` 可执行前缀算法（§3 唯一伪代码）
- [x] **T3.2** 前缀串行 exec + End；jsonl 仅 toolResult 顺序写，不改落盘语义
- [x] **T3.3** 遇 `requiresApproval`：不 Start；setPending + confirmationRequired + return True
- [x] **T3.4** 事件序表 #1–#8 回归（含拒绝 / 未知 / 单工具 / dangling）——`scripts/verifyBatchStart.py` 全过
- [x] **T3.5** 更新 `agent.py` 文件头 Version / Description
- [ ] **T3.6** 手测 B-slow；记录 B-fast 肉眼是否可见 running

### Phase 3.5 — 让帧（条件）

- [ ] **T3.7** 若 T3.6 快工具不可见 running：实现前端 rAF 让帧（chatView 或 sse.js）
- [ ] **T3.8** 验证 B-paint；确认慢工具 / 确认路径无回归

### Phase 4 — 总回归与文档收尾

- [ ] **T4.1** 跑通 §3 Phase4 场景矩阵 + §0.4 分层标准（待手测）
- [ ] **T4.2** 对比 T0 基线，写入 §8（Phase 0 未做，可跳过或手测时补记）
- [x] **T4.3** 更新 `docs/chatUiStreamingIssues.md` 问题 6：链到本方案；标注 read1、旧 T5.6= D2 落地状态
- [x] **T4.4** 本文件 Version 升 1.2+，TODO 勾选，状态改为「已实施 / 手测待确认」

### Phase 5 — 二期冰区（默认不实施）

- [ ] **T5.1** 若 Phase1–3.5 后 final 前空白仍不可接受：立 skeleton 专项
- [ ] **T5.2** adapter skeleton chunk + agent/SSE/前端骨架卡
- [ ] **T5.3** 失败回滚与历史一致性验收

---

## 8. 实施记录

> 实施过程中追加。

| 日期 | Phase | 摘要 | 结果 |
|------|-------|------|------|
| 2026-08-11 | 方案 | 根因 + `read`/`read1` 复现 + v1.0 | 待开发 |
| 2026-08-11 | 方案审核 | `docs/codeReview/260811_streamingLatencyFixPlan.md` | 3 High / 5 Medium |
| 2026-08-11 | 方案修订 | v1.1：分层验收、唯一前缀算法、flush 清单、Phase3.5 让帧、链路与编号修正 | 待开发 |
| 2026-08-11 | Phase 1 | `chatCompletions.py` v1.12：`iterSseData` 优先 `read1(4096)`（getattr fallback read） | `scripts/verifyRead1.py` 通过：首帧 12ms、间隔≈120ms 匀速（修复前 read 为 ~1s 一次性吐出） |
| 2026-08-11 | Phase 3 | `agent.py` v1.12：`driveToolBatch` 可执行前缀批量 Start（D2 唯一算法） | `scripts/verifyBatchStart.py` 事件序表 #1–#8 全过（含批准无双 Start、拒绝仅 End、未知 Start+合成 error End、startIndex 续批） |
| 2026-08-11 | Phase 2 | `chatView.js` v1.7：rAF paint 合并 + D4 强制 flush 清单（step 切换/终态/stopping/goIdle/collapse 前） | 语法检查通过；手测 T2.7 待确认 |
| 2026-08-11 | 收尾 | `chatUiStreamingIssues.md` v1.4 问题 6 落地注记（T4.3） | 完成 |

### 8.1 本地复现摘要（`read` vs `read1`）

```text
条件：chunked SSE，约每 100–150ms 一帧小 JSON data
read(4096)：  阻塞至多帧攒够或结束 → 单次返回全部字节
read1(4096)： 每帧立即返回，间隔≈发送间隔
结论：当前 iterSseData 使用 read(4096) 会破坏流式实时性
```

---

## 9. 推荐实施顺序（给执行者）

```text
1. T1.*   read1              → 立刻验证现象 A
2. T3.*   可执行前缀批量 Start → 验证事件序表 + B-slow
3. T3.7+  若快工具仍无 running → 让帧（Phase3.5）
4. T2.*   paint 合并 + flush 清单 → 长正文与 stop/终态无缺尾
5. T4.*   回归 + 文档
6. 仅当 final 前空白仍不满 → Phase5 skeleton
```

说明：

- **T1 必须最先**，否则 A/B 手测会被适配器攒批污染。  
- T2 与 T3 可对调，但 T2 的 flush 清单不依赖 T3。  
- 不要无脑加后端 sleep。

---

## 10. 一句话结论

- **现象 A 主因**：`HTTPResponse.read(4096)` 在 chunked SSE 上阻塞凑批（已复现）；前端 markdown 为正文路径次因。  
- **现象 B 主因**：`toolCallStart` 绑在 final 之后 + 快工具 Start/End 粘连 + 浏览器同帧同步消费不 paint。  
- **批量 Start** 治「成批完成卡」的**结构/顺序**；**肉眼 running** 靠慢工具自然间隔 + 条件让帧；**final 前空白** 需二期 skeleton。  
- **本期最小闭环**：`read1` + 可执行前缀批量 Start + 前端 rAF paint（强制 flush）+ 条件让帧。
