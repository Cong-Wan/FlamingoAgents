# 模型输出期间状态栏用量更新调研与修复方案

- Author: wilbur
- Version: 1.0
- Date: 2026-09-02
- 状态：待审核
- 调研范围：`flamingoAgents/core/{agent,conversation,types}.py`、`flamingoAgents/models/{chatCompletions,responsesAdapter}.py`、`webApp/backend/{agentManager,server,sessionStore,sseCodec,usageStore}.py`、`webApp/frontend/js/{chatView,statusBar}.js`、`docs/webApiSpec.md`
- 本文性质：调研结论与实施计划；本次不直接修改业务代码

## 1. 问题定义与假设

用户看到：

```text
↑ 0 · ↓ 0 · ⚡ 0 cached · $- · 0% / 272.0k
```

只有一次浏览器 SSE 请求（一个泵流）完全结束后才变化，希望“模型每次输出”时更新。

“每次输出”存在两种解释，不能混为一谈：

1. **每个模型 API 调用完成时更新（本方案推荐并以此为实施目标）**：一次用户请求可能经历“模型输出工具调用 → 执行工具 → 再请求模型 → 最终回答”，每个模型调用在收到 provider 的终态 usage 后立即更新状态栏，不等整个 Agent 工具循环结束。
2. **每个文本 token/chunk 到达时更新**：要求正文每吐一段文字就改变数字。当前 provider 协议不能精确提供全部指标；只能做估算，不应伪装成账单级准确值。

若用户实际要求第 2 种语义，需要另行确认“允许估算 ↓，而 ↑/⚡/$/% 仍待模型终态校准”。在未确认前，不建议实现逐 chunk 假精确统计。

## 2. 结论摘要

当前“整轮结束才更新”不是单点 bug，而是三个时机叠加的必然结果：

1. **上游 usage 到达晚**：Chat Completions 通过 `stream_options.include_usage=true` 请求 usage，但 provider 通常只在 `[DONE]` 前的终态 chunk 返回；Responses API 也只在 `response.completed/response.done/response.incomplete` 终态响应中给 usage。
2. **Web 持久化更晚**：`conversation.usageTotal` 虽然在**每个模型调用完成后**已经累加，但 `streamPump._recordUsage()` 只在整个泵流 `finally` / stop 收尾时把它写入 sessions 索引与 `usage.db`。
3. **前端刷新最晚**：`chatView.onStreamClosed()` 只在 SSE 连接关闭后调用 `statusBar.refresh()`；流中的 `textDelta/reasoningDelta/toolCall*` 不触发状态栏更新，现有 SSE 事件也没有 usage 事件。

因此，当前内部其实已经存在“每次模型调用完成”的精确数据，但它没有被暴露为 Agent/SSE 事件，也没有在该边界触发 UI 更新。

## 3. 这些值现在分别记录在哪里

### 3.1 原始 usage：provider 终态响应

#### Chat Completions

- `flamingoAgents/models/chatCompletions.py:76-80`：流请求显式发送 `stream_options={"include_usage": true}`。
- `chatCompletions.py:170-221`：消费 SSE 时暂存最后的 `usage`，组装进 `responsePayload['usage']`。
- `chatCompletions.py:302-308`：只要 provider chunk 带 usage，就作为内部 meta 读取；但不会向 Agent 单独 yield usage chunk。

#### Responses API（ChatGPT Codex / xAI）

- `flamingoAgents/models/responsesAdapter.py:495-502, 640-642`：只在 terminal response 中取 usage 并归一化。
- `responsesAdapter.py:732-733`：最终写入 `responsePayload['usage']`。

结论：两个 adapter 都只在 `finalChunk` 中把可用的精确 usage 交给 Agent；文本 delta 本身不带完整 usage。

### 3.2 会话内存累计与 JSONL 历史

- `flamingoAgents/core/agent.py:269-274`：每个模型调用得到 `finalChunk` 后调用 `conversation.appendAssistantMessage()`。
- `flamingoAgents/core/conversation.py:166-178`：把原始 `responsePayload.usage` 记录到 `assistantMessage` JSONL 事件，然后调用 `_accumulateUsage()`。
- `conversation.py:121-130`：维护：
  - `usageTotal.promptTokens`：会话累计总输入（包含 cached 子集）；
  - `usageTotal.cachedTokens`：会话累计缓存命中；
  - `usageTotal.completionTokens`：会话累计输出；
  - `lastTurnTokens = promptTokens + completionTokens`：最近一次模型调用的上下文占用近似值。
- 会话 JSONL 路径：`~/.flamingo/logs/webData/<workDir映射目录>/<sessionId>.jsonl`（由 `agentManager.getAgent()` 注入 logDir）。

这里是**最早出现精确完整 usage 的位置**，时间点为单次模型 API 调用终态，而不是整个 Agent 泵流终态。

### 3.3 sessions 索引（状态接口的 token / context 数据源）

- 文件：项目内 `webData/sessions.json`。
- `webApp/backend/agentManager.py:156`：泵启动时快照 `startUsage`。
- `agentManager.py:280-301`：整个泵流终态执行 `_recordUsage()`，计算 `finalUsage - startUsage`。
- `webApp/backend/sessionStore.py:121-149`：`updateUsage()` 把累计 `usage`、`contextTokens`、整泵 `lastUsage` 写回 sessions 索引。
- `webApp/backend/server.py:260-284`：`GET /api/sessions/{sessionId}/status` 从 sessions 索引读取 `usage/contextTokens/lastUsage`。

所以状态接口在流进行中读到的是上一轮泵流的值。

### 3.4 usage.db（费用与时序统计数据源）

- 文件：`~/.flamingo/logs/usage.db`。
- 表：`usageTurns`，字段含 session/provider/model/timestamp/prompt/cached/completion。
- `webApp/backend/usageStore.py:71-87`：`writeUsageTurn()` 写入一个泵流的 token 增量。
- `usageStore.py:115-130`：状态接口的 `$` 通过 `querySessionCost()` 汇总该 session 的 usageTurns，再套用 `config/models.yaml` 当前价格。
- 费用公式：`(prompt-cached)*input + cached*cacheRead + completion*output`，单位换算为百万 token。

当前泵流没结束就还没写 usageTurns，因此 `$` 同样落后一整个泵流。

### 3.5 状态栏最终显示公式

`webApp/frontend/js/statusBar.js:42-60`：

- `↑ = max(0, usage.promptTokens - usage.cachedTokens)`：会话累计非缓存输入；
- `↓ = usage.completionTokens`：会话累计输出；
- `⚡ = usage.cachedTokens`：会话累计缓存命中；
- `$ = status.cost`：会话累计估算费用，0 时显示 `$-`；
- `% = contextTokens / contextWindow`，保留一位并限制 0–100；
- `272.0k = contextWindow`：来自 `config/models.yaml` 当前 provider/model 的 `contextWindow`，不是运行时 token 统计。

`webApp/frontend/js/chatView.js:934-937` 明确只在 SSE 关闭后 `statusBar.refresh()`；打开会话和 `/model` 切换后也会刷新，但流中不刷新。

## 4. 当前完整时序与延迟点

```text
provider text/reasoning delta
  → adapter yield textChunk/reasoningChunk
  → agent yield textDelta/reasoningDelta
  → streamPump broadcast
  → 浏览器渲染文字
  （此时通常没有精确 usage）

provider terminal usage
  → adapter finalChunk(responsePayload.usage)
  → agent appendAssistantMessage
      → JSONL 写 assistantMessage.usage
      → conversation.usageTotal / lastTurnTokens 已更新   ← 精确数据最早可用
  → 若有工具：执行工具并再次请求模型
  → 若无工具：completed
  → streamPump finally._recordUsage
      → usage.db 写整泵 delta
      → sessions.json 回写累计 usage/context/lastUsage
  → streamPump 关闭订阅队列
  → 浏览器 SSE closed
  → chatView.onStreamClosed
  → statusBar.refresh → GET status
  → 状态栏终于更新
```

真正缺失的桥梁是“`appendAssistantMessage()` 之后、继续工具循环之前”的 usage 事件。

## 5. 为什么不能精确地每个文本 chunk 更新全部值

1. **↑ 输入 token**：请求发出前理论上可本地 tokenizer 估算，但项目没有各模型一致的 tokenizer；system/tool schema/provider 包装也会影响服务端计数。
2. **⚡ cached**：是否命中缓存由 provider 判定，客户端在输出中途无法推导。
3. **↓ 输出 token**：可按文本近似估算，但 reasoning、隐藏 token、Unicode/模型 tokenizer 差异会造成偏差。
4. **$**：依赖准确的输入、缓存、输出三项，逐 chunk 只能得到假精确值。
5. **%**：当前口径是 provider 最近一次完整调用的 prompt+completion；调用未终态时没有最终 completion，也没有可靠 cached/隐藏 token 信息。

仅在某个 provider 将**累计 usage 随每个 chunk 下发**时才能透传实时精确值，但当前 OpenAI-compatible/Responses 的通用实现和已支持 provider 都不能依赖这一行为。

因此成功标准应是：**每个成功模型调用收到 terminal usage 后立刻准确更新；纯文本单调用仍只能在该调用结束时更新，但多工具步骤不再等整轮 Agent 结束。**

## 6. 方案对比

| 方案 | 做法 | 准确性 | 代价/问题 | 结论 |
|---|---|---:|---|---|
| A. 每个 delta 调 `GET status` | 前端在 textDelta 时轮询/请求 | 无提升 | 后端 sessions/db 本来就没更新；制造大量 HTTP/git/SQLite 查询 | 否决 |
| B. 浏览器按字符估 token | 每个 delta 本地估 ↓ | 仅近似 | ↑/⚡/$/% 仍不准；不同 tokenizer 偏差大 | 非默认，可作为明确标“估算”的后续功能 |
| C. 每个模型调用写 usageTurns | 每次 terminal usage 都落 DB、索引并刷新 | 精确 | 改变 `usageTurns` 与 `lastUsage` 的“一个泵流一条”既有语义；stop/并发幂等和迁移面更大 | 可行但不够精准修改，暂不选 |
| **D. 新增 usageUpdate SSE + 中间索引更新，账单仍整泵落库** | 单模型调用完成后发精确累计 usage；Web 层计算当前流的临时费用；前端直接合并渲染；最终仍按原逻辑落账 | **精确到模型调用终态** | 增加一个非终态事件与少量泵状态 | **推荐** |

## 7. 推荐修复设计

### 7.1 不变量

1. JSONL 的 `assistantMessage.usage` 格式不变。
2. `conversation.usageTotal` 的累计语义不变，`promptTokens` 仍包含 cached 子集。
3. `usageTurns` 继续保持“每个泵流一个 delta”的现有账单语义，避免图表、`queryLastUsageTurn()` 和 `lastUsage` 契约漂移。
4. sessions 索引中的 `lastUsage` 仍表示最近完整泵流增量；中间更新只写 `usage/contextTokens`，不提前覆盖 `lastUsage`。
5. 泵终态仍执行 `_recordUsage()`：写 usage.db、回写最终 sessions、关闭连接；它是最终校准与持久化兜底。
6. 新事件是非终态，不加入 `terminalEventTypes`。

### 7.2 Core：新增单模型调用用量事件

在 `flamingoAgents/core/types.py` 新增小驼峰 dataclass：

```python
@dataclass
class usageUpdateEvent:
    usage: dict[str, int]
    stepUsage: dict[str, int]
    contextTokens: int
```

在 `agent.driveModelLoop()` 每个模型 step 开始前快照 `usageTotal`；`appendAssistantMessage()` 完成后计算该 step delta，并在判断 toolCalls 前 yield：

```text
appendAssistantMessage
  → usageUpdateEvent（精确、非终态）
  → 无工具：completed
  → 有工具：toolCallStart/End → 下一次模型调用
```

这样纯文本事件序列变为：

```text
textDelta* → usageUpdate → completed
```

工具序列变为：

```text
textDelta* → usageUpdate → toolCallStart → toolCallEnd
→ textDelta* → usageUpdate → completed
```

重试失败且没有 finalChunk 时不发 usageUpdate；中途断流且 provider 未给 terminal usage 时也不伪造数字。

### 7.3 Web 泵：中间索引更新与临时费用

`streamPump` 启动时保留现有 `startUsage`，并记录该泵实际使用的 `providerId/modelId`。路由创建 `streamMeta` 时把这两个字段一起传入，不能在中途重新读取可能被 `/model` 改写的索引来猜实际模型。

泵消费到 `usageUpdateEvent` 时，在广播前做两件事：

1. `sessionStore.updateUsage(sessionId, event.usage, contextTokens=event.contextTokens, lastUsage=None)`：令 status 的 token/context 读路径也能看到最新调用；不改 lastUsage。
2. 计算临时会话累计费用供该 SSE 事件展示：
   - 已落库基线：`usageStore.querySessionCost(sessionId)`；
   - 本泵未落库增量：`event.usage - startUsage`；
   - 用泵捕获的实际 provider/model 当前价格调用 `calcTurnCost()`；
   - `liveCost = dbBaseCost + currentPumpDeltaCost`。

注意：不把中间增量写入 usage.db，避免终态 `_recordUsage()` 再写导致重复计费。终态关闭后，前端仍按原逻辑 `refresh()`，以 usage.db 与 sessions 索引重新校准。

为保持 core 与 Web 计价职责分离，建议泵将 core `usageUpdateEvent` 转为 Web SSE DTO 时补 `cost`，不要把价格配置塞进 Agent/adapter。可采用一个 Web 层不可持久化的事件包装对象，或让 `sseCodec` 接收泵已计算好的 DTO；实施时选代码量更少且可测试的方式，禁止动态给 dataclass 偷挂属性。

SSE DTO：

```json
{
  "usage": {"promptTokens": 1571, "cachedTokens": 1024, "completionTokens": 236},
  "stepUsage": {"promptTokens": 420, "cachedTokens": 128, "completionTokens": 86},
  "contextTokens": 1793,
  "cost": 0.000286
}
```

`stepUsage` 用于诊断/潜在消费者；状态栏仍展示会话累计 `usage`，不改现有口径。

### 7.4 SSE 编码与多窗口 attach

- `webApp/backend/sseCodec.py` 增加 `usageUpdate` 映射。
- 事件进入 pump history，第二窗口 attach 时会回放，因此多窗口可得到同一累计值。
- `compactDeltas()` 不合并 usageUpdate；每个模型调用通常只有一条，数量很小。
- 先更新 sessions/计算 liveCost，再 broadcast，保证事件数据对应的状态已准备好。

### 7.5 前端状态栏直接应用事件

`statusBar.js` 增加内部最近状态快照与公开方法，例如 `applyUsageUpdate(data)`：

1. `refresh()` 成功后缓存完整 status DTO。
2. `applyUsageUpdate()` 只覆盖：`usage/cost/contextTokens/contextUsedPercent`。
3. `contextUsedPercent` 用缓存的 `contextWindow` 重新计算；`workDir/gitBranch/contextWindow` 不变。
4. 调用现有 `renderUsage()`，不重复请求 status，不在每个 textDelta 上发 HTTP。
5. 若尚无缓存（极端 attach/打开竞态），退化为调用一次 `refresh()`，不能报错或显示 NaN。

`chatView.onStreamEvent()` 增加 `usageUpdate` 分支并调用该方法；不改变 stream phase、消息渲染或终态处理。`onStreamClosed()` 的最终 refresh 保留，负责落库后的权威校准。

### 7.6 费用一致性与配置变化

- liveCost 只用于当前活跃 SSE 展示，不写盘。
- 终态 `$` 仍来自 usage.db 按 `config/models.yaml` 当前价查询，行为不变。
- 若流过程中管理员修改价格，liveCost 与终态可能短暂不同；最终 refresh 会按新价格校准。这与现有“查询时按当前价计算”契约一致，应在契约注明“流中值为临时值、关闭后权威校准”。
- `cost=0` 仍显示 `$-`。

## 8. 需要修改的文件

| 文件 | 精确改动 | 预计版本 |
|---|---|---|
| `flamingoAgents/core/types.py` | 新增 `usageUpdateEvent` | 按现有头版本 +0.1 |
| `flamingoAgents/core/agent.py` | 每个成功模型调用 append 后 yield usageUpdate | 1.20 → 1.21 |
| `webApp/backend/agentManager.py` | 识别 usageUpdate；中间 sessions 更新；计算 liveCost；保持终态整泵落账 | 1.7 → 1.8 |
| `webApp/backend/server.py` | stream/confirm meta 固化实际 providerId/modelId | 按现有头版本 +0.1 |
| `webApp/backend/sseCodec.py` | 编码 `usageUpdate` SSE DTO | 1.2 → 1.3 |
| `webApp/frontend/js/statusBar.js` | 缓存 status + `applyUsageUpdate()` 局部重绘 | 1.3 → 1.4 |
| `webApp/frontend/js/chatView.js` | 消费 usageUpdate，不改变流状态 | 现有版本 +0.1 |
| `docs/webApiSpec.md` | §3.14 流中数据说明；§4.3 新事件与典型序列；状态机新增非终态分支 | 文档版本 +0.1 |
| `tests/testLiveUsageUpdate.py`（新建） | pytest 覆盖 Core、pump、SSE 与前端 helper | 1.0，含规定文件头 |

不需要修改 adapter：它们已经正确请求/归一化终态 usage；问题在 finalChunk 之后缺事件和 UI 通路。

## 9. 分阶段实施计划与成功标准

```text
1. 建立 Core usageUpdate 事件 → 验证：每个成功 finalChunk 后恰好一条，顺序在工具/终态之前
2. Web 层生成 live DTO → 验证：sessions 中间值更新、usage.db 流中不写、liveCost 手算一致
3. 前端即时重绘 → 验证：工具循环中第一段模型输出结束后状态栏已变，不等整个 SSE 关闭
4. 终态校准与回归 → 验证：关闭后 DB/索引/界面总量一致，stop/attach/confirm 不重复计费
```

### Phase 1：Core 事件

- [ ] T1.1 在 `types.py` 新增 `usageUpdateEvent`，字段只含 token/context，不含 Web 价格配置
- [ ] T1.2 在 `driveModelLoop()` 每个 model step 调用前快照 usage
- [ ] T1.3 在 `appendAssistantMessage()` 后计算非负 step delta 并 yield usageUpdate
- [ ] T1.4 保证 usageUpdate 不加入 `terminalEventTypes`
- [ ] T1.5 测试纯文本顺序：`textDelta* → usageUpdate → completed`
- [ ] T1.6 测试工具循环顺序：每个 assistant/model step 恰好一条 usageUpdate
- [ ] T1.7 测试失败/无 finalChunk 不发伪 usageUpdate

成功标准：Core 消费者能在每个成功模型调用结束时拿到精确累计/本 step/context 数据，原终态与工具事件语义不变。

### Phase 2：Web 泵与 SSE

- [ ] T2.1 stream/confirm meta 固化泵实际 providerId/modelId
- [ ] T2.2 设计小型 Web DTO，禁止动态修改 core dataclass
- [ ] T2.3 pump 在 broadcast usageUpdate 前调用 `sessionStore.updateUsage(..., lastUsage=None)`
- [ ] T2.4 以 `startUsage` 为基线计算当前泵累计 delta，不逐事件重复累加
- [ ] T2.5 复用 `usageStore.calcTurnCost()` 与 `loadCostMap()` 计算 liveCost
- [ ] T2.6 中间事件不调用 `writeUsageTurn()`；终态 `_recordUsage()` 保持整泵唯一落账
- [ ] T2.7 `sseCodec` 编码 usageUpdate DTO
- [ ] T2.8 attach history 回放 usageUpdate；确认事件不被 compactDeltas 误合并
- [ ] T2.9 测试两次 usageUpdate 的 liveCost 不双加、DB 写入次数在终态前为 0/终态后为 1
- [ ] T2.10 测试 stop 与 finally 的现有 `usageRecorded` 幂等不被破坏

成功标准：事件发出前 sessions 已有最新 token/context；流中不新增账单行；事件 cost 与手算一致；终态仍只记一次整泵 delta。

### Phase 3：前端即时更新

- [ ] T3.1 `statusBar.refresh()` 缓存最后一次完整 status DTO
- [ ] T3.2 新增 `applyUsageUpdate()`，合并 usage/cost/context 并复用 `renderUsage()`
- [ ] T3.3 使用缓存 contextWindow 计算百分比，覆盖 null/0/边界 clamp
- [ ] T3.4 无缓存时只触发一次 refresh 兜底，避免并发事件造成请求风暴
- [ ] T3.5 `chatView.onStreamEvent()` 增加 usageUpdate 分支
- [ ] T3.6 保留 `onStreamClosed()` 最终 refresh
- [ ] T3.7 Node assert（由 pytest 驱动）验证非缓存输入减法、cost、百分比与 fallback
- [ ] T3.8 `node --check statusBar.js chatView.js`

成功标准：有工具调用的长请求在每个模型 step 后立即更新全部五项；text delta 期间不轮询；最终显示与 status API 一致。

### Phase 4：契约、回归与人工验收

- [ ] T4.1 更新 `webApiSpec.md` §4.3：usageUpdate 字段、非终态语义、典型序列
- [ ] T4.2 更新 §3.14：sessions 在模型 step 可中间回写；lastUsage/usageTurns 仍整泵语义；cost 终态权威
- [ ] T4.3 更新状态机：usageUpdate 只重绘状态栏，不迁移 phase
- [ ] T4.4 `uv run pytest` 全量通过
- [ ] T4.5 人工验收“模型 → 长 bash/read → 模型”：工具执行期间已显示第一步 usage
- [ ] T4.6 人工验收连续两次工具循环：数字单调、最终值与 JSONL usage 求和一致
- [ ] T4.7 人工验收多窗口 attach：两个窗口最终显示一致，重放不倒退到更旧值
- [ ] T4.8 人工验收 confirmation 新泵：每个 confirm 泵各自最终只落一条 usageTurns
- [ ] T4.9 人工验收 stop/中途断流：没有 terminal usage 时不伪增；若已完成前一 model step，该 step 已显示且最终不重复计费

## 10. 测试设计

使用项目既有 `pytest`，前端纯函数断言沿用 `tests/testSubscriptionModelsJs.py` 的“pytest 启动 Node assert”方式，不引入新测试框架。

### 10.1 Core 单元测试

1. fake adapter 返回带 usage 的 finalChunk，无工具：断言 usageUpdate 数量、字段和事件顺序。
2. fake adapter 连续两次完成（第一步工具调用、第二步文本）：断言累计 usage 单调、stepUsage 分别精确。
3. adapter 抛错/中断：断言没有无法证实的 usageUpdate。
4. cached 是 prompt 子集：断言不在 Core 做减法，保留原生语义。

### 10.2 Pump/SSE 单元测试

1. monkeypatch `sessionStore.updateUsage`、`usageStore.querySessionCost/loadCostMap/writeUsageTurn`。
2. usageUpdate 到达：断言先中间 update 再 broadcast；lastUsage 未传或为 None。
3. 两个 step：liveCost 使用“当前累计减 startUsage”，不重复累计第一步。
4. `_recordUsage()` 终态：usageTurns 只写整个泵 delta 一次。
5. `eventToFrame`/Web DTO 输出字段完整、JSON 可编码。
6. subscribe/attach 回放中含 usageUpdate，终态和 None 哨兵次序不变。

### 10.3 前端测试

将状态合并/百分比计算拆为可导出的纯 helper（仅测试环境导出，不引入抽象层）：

- 累计 `{prompt:100,cached:60,completion:20}` 显示 `↑40/↓20/⚡60`；
- context `50/200` 为 `25%`；超过窗口 clamp 到 `100%`；未知窗口为 `-`；
- cost 0 为 `$-`，正数固定四位；
- 新事件只覆盖 usage/cost/context，不丢 workDir/git/contextWindow；
- 无缓存 fallback 至多触发一次 refresh。

### 10.4 数据对账

终态后验证：

```text
sessions.usage
= conversation.usageTotal
= Σ 当前会话 JSONL assistantMessage.usage

本泵 usageTurns.delta
= 终态 usage - 泵 startUsage

状态栏 ↑ + ⚡
= sessions.promptTokens

状态栏最终 $
= querySessionCost(sessionId)
```

## 11. 风险与处理

| 风险 | 等级 | 处理 |
|---|---:|---|
| 把逐模型 step 写成逐 token 精确统计，形成错误承诺 | 高 | 契约明确“terminal usage 后更新”；逐 chunk 估算另立需求并标 estimated |
| 中间 usage 写 DB，终态再次写导致双计费 | 高 | 推荐方案流中只写 sessions/发 SSE，不写 usageTurns；测试写入次数 |
| liveCost 把第一步在第二步重复累加 | 高 | 每次用 `event.usage - startUsage` 计算本泵累计费用，不做无基线的 `+=` |
| `/model` 流中改变索引导致费用套错模型 | 高 | 泵创建时从实际启动 session 固化 provider/model 到 meta |
| 改坏 lastUsage“最近泵流”语义 | 中 | 中间 `updateUsage` 传 `lastUsage=None`；只由终态 `_recordUsage` 覆盖 |
| attach 重放旧 usage 造成 UI 倒退 | 中 | usage DTO 是会话累计；前端可按各 token 字段不小于当前缓存才应用，或以事件原顺序回放；实现时测试快速切换/attach |
| 每个 usageUpdate 都执行 git/SQLite status GET | 中 | 前端直接应用 SSE DTO，不调用 status；关闭时仅一次权威 refresh |
| provider 不返回 usage | 中 | 不伪造；保持旧值，终态/文档注明 unavailable；可在诊断日志观察 |
| 纯文本单调用看起来仍“结束才更新” | 低 | 这是精确 usage 最早到达时机；方案主要消除多工具 Agent 整轮延迟 |

## 12. 非目标

- 不新增 tokenizer 依赖。
- 不做逐字符/逐 chunk 的未标注估算。
- 不修改 provider adapter 的 usage 口径。
- 不迁移 usage.db schema。
- 不改变 usageTurns、lastUsage 的整泵语义。
- 不顺手重构状态栏、泵线程或相邻 UI。

## 13. 回滚

1. 前端可先停止消费 usageUpdate，恢复“连接关闭后 refresh”；后端新增事件对旧前端无破坏。
2. 再移除 sseCodec/pump 的 usageUpdate 处理与中间 sessions 更新。
3. 最后移除 Core usageUpdate 类型/yield。
4. 全程无 DB schema/data 迁移，回滚不需要修复历史账单。

## 14. 最终建议

采用方案 D，并把验收措辞锁定为：

> **每个模型 API 调用收到 terminal usage 后，状态栏立即精确更新；不再等待同一 Agent 请求中的后续工具和模型步骤全部结束。逐文本 chunk 不宣称精确。**

这利用了现有 `appendAssistantMessage()` 已经具备的精确模型调用边界，只新增一条从 Core 到 SSE/UI 的事件通路，避免轮询、tokenizer、DB 语义迁移和重复计费，是当前代码结构下修改面最小且语义可靠的方案。
