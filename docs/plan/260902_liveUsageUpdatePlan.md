# 模型输出期间状态栏用量更新调研与修复方案

- Author: wilbur
- Version: 1.6
- Date: 2026-09-04
- 状态：已由 `xaiSubscription/grok-4.6` 最终复审通过，可实施（无 High/Medium 问题）
- 修订记录：
  - v1.1：落实首轮审核 C1/H1/H2/M1-M4/L1-L4。
  - v1.2：落实复审 N1-N13：sseCodec 永久保留 Core/DTO 双映射、step 基线明确为外层循环值拷贝、补 `index.html` 加载顺序、缺失 cost 保留原值、累计 usage 单调防倒退、泵级费用基线/价格缓存、DTO 固定定义于 `sseCodec.py`、JSONL 对账显式做 snake_case→camelCase 映射、终态账单使用泵实际模型、百分比口径锁定、补 confirmation 序列及引用勘误。
  - v1.3：落实 v1.2 复审 P1-P13：空/不完整 usage 不发事件、保留 `responsePayload` 安全守卫、费用初始化移出 `managerLock` 并改为首个 usage 事件惰性一次初始化、如实注明 `querySessionCost()` 内部的第二次 YAML 读取、锁定 `_pump()` 转换位置与 stop 竞态边界、会话切换显式重置状态、完善前端数字归一化/pending 比较/refresh 竞态、逐文件执行 `node --check`。
  - v1.4：落实 v1.3 复审 N1-N7：所有流事件/关闭回调绑定 session 与 stream 身份；给出 `_recordUsage()` 无死锁原子认领完整伪代码并保证持久化异常不阻断 seal；费用状态区分 ready/unavailable/缺模型；澄清非法 provider usage 只控制“不发事件”而不改变 `_accumulateUsage` 既有行为；open/fallback 共用单飞 refresh；迟到 promise 的 finally 使用 generation/requestId 守卫。
  - v1.5：吸收 v1.4 通过复审后的 Low 建议：attach 身份包装不得替换 `streamResume/resetToHistoryState` 分支；closed refresh 明确 fire-and-forget 且只收口本流；failed 包装保留 send 409 meta；注明 Responses 空原始 usage 会被 adapter 归一为合法全 0；构造字段补 `usageRecorded=False`。
  - v1.6：落实 v1.5 复审的两个 Medium：SSE 身份从 session+stream 对象升级为 session+stream+connectionId，覆盖 confirm 复用 stream 对象时前泵迟到关闭；`onStreamClosed` 写出与现有 waitingConfirm/stopping/断流状态机合并后的完整伪代码。
- 调研范围：`flamingoAgents/core/{agent,conversation,types}.py`、`flamingoAgents/models/{chatCompletions,responsesAdapter}.py`、`webApp/backend/{agentManager,server,sessionStore,sseCodec,usageStore}.py`、`webApp/frontend/{index.html,js/{chatView,statusBar,subscriptionModels}.js}`、`docs/webApiSpec.md`
- 本文性质：调研结论与实施计划；本次不直接修改业务代码
- 引用约定：代码定位一律用**函数名/语义锚点**（行号随演进漂移，调研基线 commit `2655e4b`）

## 1. 问题定义与假设

用户看到：

```text
↑ 0 · ↓ 0 · ⚡ 0 cached · $- · 0% / 272.0k
```

只有一次浏览器 SSE 请求（一个泵流）完全结束后才变化，希望“模型每次输出”时更新。

“每次输出”存在两种解释，不能混为一谈：

1. **每个模型 API 调用完成时更新（本方案推荐并以此为实施目标）**：一次用户请求可能经历“模型输出工具调用 → 执行工具 → 再请求模型 → 最终回答”，每个模型调用在收到 provider 的终态 usage 后立即更新状态栏，不等整个 Agent 工具循环结束。
2. **每个文本 token/chunk 到达时更新**：要求正文每吐一段文字就改变数字。当前 provider 协议不能精确提供全部指标；只能做估算，不应伪装成账单级准确值。

若用户实际要求第 2 种语义，需要另行确认“允许估算 ↓，而 ↑/⚡/$/% 仍待模型终态校准”。在未确认前，不实现逐 chunk 假精确统计。

## 2. 结论摘要

当前“整轮结束才更新”不是单点 bug，而是三个时机叠加的必然结果：

1. **上游 usage 到达晚**：Chat Completions 通过 `stream_options.include_usage=true` 请求 usage，但 provider 通常只在 `[DONE]` 前的终态 chunk 返回；Responses API 也只在 `response.completed/response.done/response.incomplete` 终态响应中给 usage。
2. **Web 持久化更晚**：`conversation.usageTotal` 虽然在**每个模型调用完成后**已经累加，但 `streamPump._recordUsage()` 只在整个泵流 `finally` / stop 收尾时把它写入 sessions 索引与 `usage.db`。
3. **前端刷新最晚**：`chatView.onStreamClosed()` 只在 SSE 连接关闭后调用 `statusBar.refresh()`；流中的 `textDelta/reasoningDelta/toolCall*` 不触发状态栏更新，现有 SSE 事件也没有 usage 事件。

因此，当前内部已经存在“每次模型调用完成”的精确数据，但它没有被暴露为 Agent/SSE 事件，也没有在该边界触发 UI 更新。

## 3. 这些值现在分别记录在哪里

### 3.1 原始 usage：provider 终态响应

#### Chat Completions

- `flamingoAgents/models/chatCompletions.py` `buildRequestPayload()`：流式请求显式发送 `stream_options={"include_usage": true}`。
- `consumeSseStream()`：消费 SSE 时暂存最后携带 usage 的 chunk，组装进 `responsePayload['usage']`。
- `processSseData()`：provider chunk 带 usage 时只作为内部 meta 读取；不会向 Agent 单独 yield usage chunk。

#### Responses API（ChatGPT Codex / xAI）

- `flamingoAgents/models/responsesAdapter.py`：terminal 事件分支（`response.completed/response.done/response.incomplete`）统一走 `_finalizeTerminal()`。
- `_finalizeTerminal()` 内用 `normalizeUsage()` 归一化原始 usage（`input_tokens` → `prompt_tokens` 等 snake_case 原生格式）。
- 最终写入 `responsePayload['usage']`。

结论：两个 adapter 都只在 `finalChunk` 中把可用的精确 usage 交给 Agent；文本 delta 本身不带完整 usage。

**两套字段口径不得混用：**

- `responsePayload['usage']` 与 JSONL `assistantMessage.usage`：provider 原生 snake_case（`prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`）；
- `conversation.usageTotal` 与 Web DTO：项目归一化 camelCase（`promptTokens` / `completionTokens` / `cachedTokens`）。

### 3.2 会话内存累计与 JSONL 历史

- `flamingoAgents/core/agent.py` `driveModelLoop()`：每个模型调用得到 `finalChunk` 后调用 `conversation.appendAssistantMessage()`。
- `flamingoAgents/core/conversation.py` `appendAssistantMessage()`：把原始 `responsePayload.usage` 记录到 `assistantMessage` JSONL 事件，然后调用 `_accumulateUsage()`。
- `__init__` / `_accumulateUsage()` 维护：
  - `usageTotal.promptTokens`：会话累计总输入（包含 cached 子集）；
  - `usageTotal.cachedTokens`：会话累计缓存命中；
  - `usageTotal.completionTokens`：会话累计输出；
  - `lastTurnTokens = promptTokens + completionTokens`：最近一次模型调用的上下文占用近似值。
- 会话 JSONL 路径：`~/.flamingo/logs/webData/<workDir映射目录>/<sessionId>.jsonl`（由 `agentManager.getAgent()` 注入 logDir）。

这里是**最早出现精确完整 usage 的位置**，时间点为单次模型 API 调用终态，而不是整个 Agent 泵流终态。

### 3.3 sessions 索引（状态接口的 token / context 数据源）

- 文件：项目内 `webData/sessions.json`。
- `webApp/backend/agentManager.py` `streamPump.__init__()`：泵启动时快照 `startUsage`。
- `streamPump._recordUsage()`：整个泵流终态执行，计算 `finalUsage - startUsage`。
- `webApp/backend/sessionStore.py` `updateUsage()`：把累计 `usage`、`contextTokens`、整泵 `lastUsage` 写回 sessions 索引。它无条件刷新 `updatedAt` 并原子重写 sessions.json（临时文件 + rename）。
- `webApp/backend/server.py` `getSessionStatus()`：`GET /api/sessions/{sessionId}/status` 从 sessions 索引读取 `usage/contextTokens/lastUsage`。

所以状态接口在流进行中读到的是上一轮泵流的值。

### 3.4 usage.db（费用与时序统计数据源）

- 文件：`~/.flamingo/logs/usage.db`。
- 表：`usageTurns`，字段含 session/provider/model/timestamp/prompt/cached/completion。
- `webApp/backend/usageStore.py` `writeUsageTurn()`：写入一个泵流的 token 增量；delta 全 0 时不落行。
- `querySessionCost()`：按 session 汇总 usageTurns，再套用 `config/models.yaml` 当前价格；内部使用 `loadCostMap()` + `calcTurnCost()`，costMap 无对应模型 key 时该行按 0 计。
- 费用公式：`(prompt-cached)*input + cached*cacheRead + completion*output`，单位换算为百万 token。

当前泵流没结束就还没写 usageTurns，因此 `$` 同样落后一整个泵流。

### 3.5 状态栏最终显示公式

`webApp/frontend/js/statusBar.js` `renderUsage()`：

- `↑ = max(0, usage.promptTokens - usage.cachedTokens)`：会话累计非缓存输入；
- `↓ = usage.completionTokens`：会话累计输出；
- `⚡ = usage.cachedTokens`：会话累计缓存命中；
- `$ = status.cost`：会话累计估算费用，0 时显示 `$-`；
- `% = round(clamp(contextTokens / contextWindow × 100, 0, 100), 1)`；
- `272.0k = contextWindow`：来自 `config/models.yaml` 当前 provider/model 的 `contextWindow`，不是运行时 token 统计。

`webApp/frontend/js/chatView.js` `onStreamClosed()` 只在 SSE 关闭后 `statusBar.refresh()`；打开会话（`chatView.open()`）和 `/model` 切换（`slashCommand.js`）后也会刷新，但流中不刷新。

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
      → JSONL 写 assistantMessage.usage（snake_case 原生）
      → conversation.usageTotal / lastTurnTokens 已更新（camelCase） ← 精确数据最早可用
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

因此成功标准应是：**每个成功模型调用收到包含标准输入/输出计数的完整 terminal usage 后立刻准确更新；纯文本单调用仍只能在该调用结束时更新，但多工具步骤不再等整轮 Agent 结束。**

## 6. 方案对比

| 方案 | 做法 | 准确性 | 代价/问题 | 结论 |
|---|---|---:|---|---|
| A. 每个 delta 调 `GET status` | 前端在 textDelta 时轮询/请求 | 无提升 | 后端 sessions/db 本来就没更新；制造大量 HTTP/git/SQLite 查询 | 否决 |
| B. 浏览器按字符估 token | 每个 delta 本地估 ↓ | 仅近似 | ↑/⚡/$/% 仍不准；不同 tokenizer 偏差大 | 非默认，可作为明确标“估算”的后续功能 |
| C. 每个模型调用写 usageTurns | 每次 terminal usage 都落 DB、索引并刷新 | 精确 | 改变 `usageTurns` 与 `lastUsage` 的“一个泵流一条”既有语义；stop/并发幂等和迁移面更大 | 可行但不够精准修改，暂不选 |
| **D. 新增 usageUpdate SSE + 中间索引更新，账单仍整泵落库** | 单模型调用完成后发精确累计 usage；Web 层计算当前流的临时费用；前端直接合并渲染；最终仍按原逻辑落账 | **精确到模型调用终态** | 增加一个非终态事件与少量泵状态 | **推荐** |

## 7. 推荐修复设计

### 7.1 不变量

1. JSONL 的 `assistantMessage.usage` 格式不变，继续保存 provider 原生 snake_case。
2. `conversation.usageTotal` 的累计语义不变，`promptTokens` 仍包含 cached 子集。
3. `usageTurns` 继续保持“每个泵流一个 delta”；每条记录的 provider/model 改用**该泵实际 adapter 配置**，不再受流中 `/model` 改写 sessions 索引影响。
4. sessions 索引中的 `lastUsage` 仍表示最近完整泵流增量；中间更新只写 `usage/contextTokens`，不提前覆盖 `lastUsage`。
5. 泵终态仍执行 `_recordUsage()`：写 usage.db、回写最终 sessions、关闭连接；它是最终校准与持久化兜底。
6. 新事件是非终态，不加入 `terminalEventTypes`。
7. **Core 事件与 sseCodec Core 映射必须同一原子变更合入/回滚**：`eventToFrame()` 对未知事件类型兜底编码为 error 终态帧，任何“Core 在 yield 而 codec 无映射”的中间态都会让前端误判终态并丢弃后续事件。
8. Phase 2 引入 Web DTO 后，sseCodec **永久同时支持 Core `usageUpdateEvent` 与 Web `usageUpdateDto`**：正常运行 history 只存 DTO；Core 分支作为漏转换/分步部署安全网，不得被 DTO 分支替代或删除。
9. `_recordUsage()` 在 requestStop/finally 竞争下保持 at-most-once，非 owner 必须等 owner 完成尝试后再关闭订阅；持久化异常不阻断 unregister/seal。
10. 每条浏览器 SSE 的 event/closed/failed 回调只可作用于其创建时绑定的 `sessionId + streamState + connectionId`；confirm 会复用 streamState，因此仅对象身份不足，旧连接不得更新或结束新连接。

### 7.2 Core：新增单模型调用用量事件

在 `flamingoAgents/core/types.py` 新增小驼峰 dataclass：

```python
@dataclass
class usageUpdateEvent:
    usage: dict[str, int]
    stepUsage: dict[str, int]
    contextTokens: int
```

在 `agent.driveModelLoop()` 中，**每次外层 `while True` 迭代开头、内层 `for attempt` 重试循环之外**做一次值拷贝。同一模型 step 的多次 attempt 共用该基线，不得重新快照，也不得按 attempt 多发事件。Core 使用自己的局部字段元组，不得反向导入 Web `usageStore`：

```python
usageTotalKeys = ('promptTokens', 'cachedTokens', 'completionTokens')
stepStart = {
    key: int(currentConversation.usageTotal.get(key, 0) or 0)
    for key in usageTotalKeys
}
```

禁止写成 `stepStart = currentConversation.usageTotal`：后者只是可变 dict 引用，`_accumulateUsage()` 原地累加后会让 `stepUsage` 全部变成 0。

模型调用成功后必须保留现有 `responsePayload` 类型守卫，先把非 dict 归一为安全对象，再执行 `appendAssistantMessage()`：

```python
responsePayload = getattr(completion, 'responsePayload', None)
safePayload = responsePayload if isinstance(responsePayload, dict) else {}
rawUsage = safePayload.get('usage')
```

只有 **Core 实际收到的** terminal usage 是 dict，且 `prompt_tokens` 与 `completion_tokens` 都是非布尔、非负整数时才发事件。Core payload 中的 `usage={}`、仅有 `total_tokens`、字段缺失、null/字符串/浮点/负值或完全缺失均视为 unavailable；Core 不能自行把它们补齐后广播成全 0/旧 context 更新。`prompt_tokens_details` 可缺失，cached 按既有 `_accumulateUsage()` 语义为 0。注意 Responses adapter 会把 provider 原始 `{}` 经 `normalizeUsage()` 转成带标准字段的全 0 dict，Core 会把它视为“合法全 0”；本方案不反向修改 adapter 以保留“原始是否为空”的信息：

```python
hasTerminalUsage = isinstance(rawUsage, dict) and all(
    isinstance(rawUsage.get(key), int)
    and not isinstance(rawUsage.get(key), bool)
    and rawUsage[key] >= 0
    for key in ('prompt_tokens', 'completion_tokens')
)
currentConversation.appendAssistantMessage(assistantMessage, safePayload)
if hasTerminalUsage:
    usageNow = {
        key: int(currentConversation.usageTotal.get(key, 0) or 0)
        for key in usageTotalKeys
    }
    yield usageUpdateEvent(
        usage=usageNow,
        stepUsage={key: max(0, usageNow[key] - stepStart[key]) for key in usageTotalKeys},
        contextTokens=int(currentConversation.lastTurnTokens or 0),
    )
```

该门卫只决定是否发 usageUpdate，不清洗或改写传给 `appendAssistantMessage()` 的 provider usage：JSONL 与 `_accumulateUsage()` 保持既有行为。对于非标准字段，数字字符串/浮点/负值可能被既有 `int()` 转换后累计，非法字符串也可能在既有 `_accumulateUsage()` 中抛 `ValueError`；本方案不承诺这些 provider 协议违规输入“不抛错”，也不借机修改 adapter/conversation 口径。只要 append 未成功，便不会执行后续 yield。

字段来源锁定：

- `event.usage`：`conversation.usageTotal` 的 camelCase **值拷贝**；
- `event.stepUsage`：当前累计值减外层 step 基线；
- `event.contextTokens`：`conversation.lastTurnTokens`；
- 禁止直接透传 `responsePayload['usage']`，因为它是 snake_case。

事件顺序：

```text
纯文本：
textDelta* → usageUpdate → completed

免确认工具：
textDelta* → usageUpdate → toolCallStart → toolCallEnd
→ textDelta* → usageUpdate → completed

需确认：
textDelta* → usageUpdate → confirmationRequired
‖（新泵）toolCallStart → toolCallEnd → textDelta* → usageUpdate → completed

拒绝确认：
textDelta* → usageUpdate → confirmationRequired
‖（新泵）toolCallEnd(孤儿,isError=true) → textDelta* → usageUpdate → completed
```

`‖` 表示前一泵终态关闭、confirm 创建新泵；新泵的 `startUsage` 从确认前已累计的 usage 开始，不重复计算上一 step。

重试失败、无 `finalChunk`、中途断流或 provider 未给 terminal usage 时不发 usageUpdate，不伪造数字。

**CLI 兼容性**：`askModel.py` / `sdkEntry.py` 以 isinstance 链消费事件流，未匹配类型即忽略，新增事件类型对这两个消费者天然无影响，无需改动。

### 7.3 Web 泵：实际模型、泵级费用快照、中间索引与 DTO

#### 7.3.1 泵实际模型与费用基线

`streamPump.__init__()` 由 `startStream()` 在 `managerLock` 内调用，因此这里只做无 I/O 的字段固化，**禁止在该构造函数里访问 SQLite/YAML**：

```python
config = self.agent.modelAdapter.config
self.pumpProviderId = str(config.configProviderId or config.provider)
self.pumpModelId = str(config.model)
self.liveCostState = 'pending'  # pending | ready | unavailable
self.dbBaseCost: float | None = None
self.pumpModelCost: dict | None = None
self.usageRecorded = False  # 保留现有字段，由下述 lock 保护 check/set
self.usageRecordLock = threading.Lock()
self.usageRecordDone = threading.Event()
```

`configProviderId` 对应 `models.yaml` 的 provider key，`model` 对应实际 model id；不由路由重新读取可被 `/model` 改写的索引，消除“索引模型 ≠ 当前 adapter 模型”的竞态。

账单基线与模型价格在**泵线程收到第一个有效 usageUpdate、且通过 stop/done 检查后**惰性初始化一次。这样既不阻塞持有 `managerLock` 的 `startStream()`，也不为纯报错/无 usage 的泵做无用查询：

```python
def _ensureLiveCostState(self) -> None:
    if self.liveCostState != 'pending':
        return
    try:
        dbBaseCost = usageStore.querySessionCost(self.sessionId)
        costMap = usageStore.loadCostMap()
        pumpModelCost = costMap.get(f'{self.pumpProviderId}/{self.pumpModelId}')
    except Exception as error:
        self.liveCostState = 'unavailable'
        self.dbBaseCost = None
        self.pumpModelCost = None
        try:
            self._logDiagEvent('liveCostInitError', error, traceback.format_exc())
        except Exception:
            pass
        return
    self.dbBaseCost = dbBaseCost
    self.pumpModelCost = pumpModelCost  # load 成功但模型缺 key：None 表示该模型增量按 0 计
    self.liveCostState = 'ready'
```

费用查询是状态栏辅助能力：状态从 pending 只转移一次，ready/unavailable 均不在后续 step 重试。`querySessionCost()` 或显式 `loadCostMap()` 抛异常时统一进入 unavailable，DTO cost 为 `None`；显式 `loadCostMap()` 正常返回 `{}` 或缺少当前模型 key 时仍是 ready，按既有费用语义把本泵增量计为 0。任何初始化/诊断失败都不得终止模型/SSE 主流程。

调用次数口径必须如实区分：

- pump **直接调用** `querySessionCost()` 一次、`loadCostMap()` 一次，后续模型 step 不再调用；
- 当前 `querySessionCost()` 内部本身还会调用一次 `loadCostMap()`，因此走真实函数时首个 usageUpdate 最多读取 YAML 两次。这是为避免修改 `usageStore.querySessionCost()` 公共签名而接受的固定一次性成本；测试不得错误断言底层 `loadCostMap()` 总调用次数为 1；
- 两次读取间若管理员恰好修改 YAML，live 基线与本泵增量可能短暂使用不同价格；窗口极小且终态 refresh 权威校准，本方案不为此扩展 usageStore API。

`_recordUsage()` 写 usageTurns 时使用 `self.pumpProviderId/self.pumpModelId`，确保流中 `/model` 返回 409 但已提前改写索引时，本泵账单仍记录实际运行模型。sessions 的 provider/model 字段不在 `_recordUsage()` 中回滚，下一泵仍按 `/model` 契约使用新模型。

`server.py` 无需修改：`startStream(sessionId, agentInstance, stream, meta)` 已把实际 agent 交给 pump。

#### 7.3.2 中间索引更新与 liveCost

识别/转换必须放在 `_pump()` 现有 `stopFlag` 检查之后、`_broadcast()` 之前；不得藏进 `_broadcast()`，避免已停止事件仍产生中间写副作用：

```python
for event in self.stream:
    if self.stopFlag.is_set() or self.doneEvent.is_set():
        break
    if isinstance(event, usageUpdateEvent):
        event = self._toUsageUpdateDto(event)  # update sessions → 惰性费用初始化 → 构造 DTO
    if self.stopFlag.is_set() or self.doneEvent.is_set():
        break
    self._broadcast(event)
    if isinstance(event, terminalEventTypes):
        break
```

`_toUsageUpdateDto()` 按以下顺序执行：

1. 尝试 `sessionStore.updateUsage(sessionId, event.usage, contextTokens=event.contextTokens, lastUsage=None)`：令 status 的 token/context 读路径看到最新调用；不改 lastUsage。
   - 已接受副作用：`updateUsage()` 会刷新 `updatedAt` 并原子重写 sessions.json。发消息时会话已 touch 且通常位于列表顶部；每个模型 step 仅一条事件，不增加 `skipTouch` 参数，避免扩张写路径契约。
   - 中间索引是辅助读路径：该写入若因 OSError 等失败，记录诊断后仍继续构造/广播 DTO，不能把正常模型流变成 error 终态；终态 `_recordUsage()` 保留最终持久化尝试。
2. 调用 `_ensureLiveCostState()`（每泵至多初始化一次）。
3. 以 `event.usage - startUsage` 计算本泵截至当前的累计 delta，不逐事件 `+=`：

```python
delta = {
    key: max(0, int(event.usage[key]) - int(self.startUsage[key]))
    for key in usageStore.tokenKeys
}
if self.liveCostState != 'ready' or self.dbBaseCost is None:
    liveCost = None
else:
    deltaCost = (
        usageStore.calcTurnCost(
            delta['promptTokens'],
            delta['cachedTokens'],
            delta['completionTokens'],
            self.pumpModelCost,
        )
        if self.pumpModelCost
        else 0.0
    )
    liveCost = self.dbBaseCost + deltaCost
```

中间事件不调用 `writeUsageTurn()`，避免终态 `_recordUsage()` 再写导致重复计费。终态关闭后，前端仍按原逻辑 `refresh()`，以 usage.db 与 sessions 索引校准。

并发边界明确如下：

- 若进入本轮循环时 stop/done 已置位，不执行中间 update/费用初始化/DTO 广播；
- `requestStop()` 仍可能恰好发生在第一次 flag 检查与 `_toUsageUpdateDto()` 之间。为消除这一个极窄窗口需要给 stop 与 sessions I/O 新增跨线程锁，超出本需求；本方案不新增该锁。该竞态最多把**生成器刚刚已经累计的同一份会话 usage**再写一次 sessions，且 `lastUsage=None` 不覆盖终态值、不写 DB，第二次 flag 检查与 `_broadcast()` 的 doneEvent 守卫会阻止 DTO 发出；
- 测试要求最终 sessions usage/context 与 conversation 一致、lastUsage 正确、DB 唯一落账、stop 后无 DTO 广播；不做无法由现有锁模型保证的“并发竞态下 updateUsage 调用次数必须为 0”断言。

终态 `_recordUsage()` 的现有裸布尔 `usageRecorded` 不是跨线程原子协议。本次函数本来就要改写实际模型字段，为保证 requestStop 与 pump finally 不双记账，顺手收口为“锁内原子认领、锁外等待/落账、完成通知”。实现结构必须等价于以下伪代码，不得把 `wait()` 或 `set()` 放在 `usageRecordLock` 内：

```python
def _recordUsage(self) -> None:
    with self.usageRecordLock:
        if self.usageRecorded:
            isOwner = False
        else:
            self.usageRecorded = True  # 沿用既有 at-most-once：认领后失败不重试，避免 DB 已写而 sessions 失败时重复插行
            isOwner = True

    if not isOwner:
        self.usageRecordDone.wait()    # 已释放 usageRecordLock；不设 timeout
        return

    try:
        try:
            with self.agent.sessionLocksGuard:
                currentConversation = self.agent.conversations.get(self.sessionId)
            if currentConversation is None:
                return                # 仍会执行外层 finally.set()
            finalUsage = {
                key: int(currentConversation.usageTotal.get(key, 0) or 0)
                for key in self.startUsage
            }
            delta = {key: finalUsage[key] - self.startUsage[key] for key in finalUsage}
            usageStore.writeUsageTurn(
                self.sessionId,
                self.pumpProviderId,
                self.pumpModelId,
                delta,
            )
            sessionStore.updateUsage(
                self.sessionId,
                finalUsage,
                contextTokens=int(currentConversation.lastTurnTokens or 0),
                lastUsage=delta,
            )
        except Exception as error:
            # 持久化是收尾辅助路径：记录诊断但不得向 requestStop/_pump.finally 冒泡，
            # 否则 stop API 500，且后续 unregister/_sealStopped/_closeSubscribers 会被跳过。
            try:
                self._logDiagEvent('usageRecordError', error, traceback.format_exc())
            except Exception:
                pass
    finally:
        self.usageRecordDone.set()     # 覆盖 owner 的所有 return/异常路径；不得再次获取 usageRecordLock
```

非 owner 等待完成事件，避免 requestStop 提前关闭订阅后前端权威 refresh 早于实际落账。owner 的 I/O 异常被内部吸收并一定 set done，调用方总能继续 unregister/seal；本方案不对真实 SQLite/文件系统永久阻塞另加 timeout（与现有同步持久化风险相同）。

该协议不会与 `managerLock` 死锁：

- owner 是 requestStop 时，它持 managerLock 完成 I/O、set done 后再执行可重入的 unregister/seal；pump waiter 被唤醒后返回；
- owner 是 pump 时，它不持 managerLock 执行 I/O并先 set done；持 managerLock 等待的 requestStop 随后继续 unregister/seal，pump 之后再申请 managerLock。

禁止实现成“持 `usageRecordLock` 等待 done”或“finally 中重新拿同一锁再 set”，也禁止让 `currentConversation is None` 绕过 finally。

#### 7.3.3 Web DTO 定义位置与 history 类型

为避免新增后端模块和循环依赖，Web DTO **固定定义在 `webApp/backend/sseCodec.py`**：

```python
@dataclass
class usageUpdateDto:
    usage: dict[str, int]
    stepUsage: dict[str, int]
    contextTokens: int
    cost: float | None
```

- `agentManager.py` 从 `sseCodec.py` 导入 `usageUpdateDto`；`sseCodec.py` 不导入 agentManager，不形成循环依赖。
- 禁止使用 dict/匿名对象作为 history 事件：`eventToFrame()` 使用 isinstance 链，未知类型会落入 error 兜底。
- 禁止把含价格的 DTO 放入 `flamingoAgents/core/types.py`，保持 Core 与 Web 计价职责分离。
- pump 把 core `usageUpdateEvent` 转为 `usageUpdateDto` 后，再把 DTO 写入 history 并 broadcast；正常 history 不存 core usage 事件。
- `compactDeltas()` 只合并 `textDeltaEvent/reasoningDeltaEvent`，DTO 原样保序且不合并。

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

`sseCodec.eventToFrame()` 永久保留两个明确分支：

1. `usageUpdateEvent → usageUpdate`：Core 安全网，编码 `usage/stepUsage/contextTokens`，不含 cost；
2. `usageUpdateDto → usageUpdate`：正常 Web 路径，额外编码 cost；费用初始化失败时允许 `cost: null`，前端按缺失费用处理。

Phase 1 先加入 Core 分支并与 Core yield 原子合入；Phase 2 **增加** DTO 分支，禁止替代/删除 Core 分支。未知 dict/匿名对象仍必须走现有 error 兜底，不能为了兼容 DTO 而软化未知事件检查。

事件进入 pump history 后，第二窗口 attach 会按原序回放。正常 history 存 DTO，因此能直接恢复当时的精确累计值和 liveCost。先更新 sessions/计算 liveCost，再入 history/broadcast，保证事件与读路径状态一致。

### 7.5 前端状态栏直接应用事件

#### 7.5.1 无 DOM 纯计算模块与加载顺序

新建 `webApp/frontend/js/statusUsage.js`，用与 `subscriptionModels.js` 同构的挂载方式：

```javascript
(function (root) {
  // pure helpers
  root.statusUsage = { /* helpers */ };
})(typeof window !== 'undefined' ? window : globalThis);
```

纯模块负责：

- 累计 usage 新旧比较与快照合并；
- `↑/↓/⚡` 归一化；
- context 百分比；
- cost 合并与格式化。

`statusBar.js` 顶层立即访问 DOM，不能被 Node 直接 require；Node 测试只 require `statusUsage.js`。

必须修改 `webApp/frontend/index.html`，在 `statusBar.js` **之前**加载 helper：

```html
<script src="/static/js/sidebarView.js"></script>
<script src="/static/js/statusUsage.js"></script>
<script src="/static/js/statusBar.js"></script>
```

依赖顺序锁定为：`statusUsage.js → statusBar.js → chatView.js`。

#### 7.5.2 状态快照、数值归一化与陈旧事件

`statusBar.js` 保存 `snapshotSessionId`、当前 status 快照、`pendingUsageUpdate`、refresh promise/序号与 reset generation。

输入规则锁定如下：

1. incoming `usage` 必须是对象，且 `promptTokens/cachedTokens/completionTokens` 三项都能归一为有限、非负整数；缺 usage、缺任一 key、NaN/Infinity/负数均丢弃整帧。当前 status 快照中的老/缺失字段只在读侧归一为 0，不回写伪字段。
2. `applyUsageUpdate(data)` 只处理当前 `appStore.currentSessionId`；`snapshotSessionId !== currentSessionId` 等同于“当前会话无快照”，不能因仍保存上一会话快照而直接丢事件。
3. 事件新旧只由三项**会话累计 usage**判断：candidate 三项都不小于 current 才接受；任一项倒退则整帧忽略（连同 context/cost）。`contextTokens` 不是累计值，不参与新旧判断；三项相等时允许用更新帧刷新 context/cost。

接受事件后：

- usage 覆盖为已归一的 incoming 累计值；
- contextTokens 仅在能归一为有限非负数时覆盖；
- cost 仅在 `Number.isFinite(data.cost)` 时处理，否则保留已有 cost（Core 安全网帧不含 cost、DTO 初始化失败为 null，均不能把已有费用刷成 `$-`）；
- 先把 current cost 归一为 `Number.isFinite(snapshot.cost) ? snapshot.cost : 0`，再用 `Math.max(currentCost, data.cost)`，防止 undefined 导致 NaN，并保证活跃流中费用不倒退；
- `workDir/gitBranch/providerId/modelId/contextWindow` 不由 usageUpdate 覆盖。

context 百分比严格对齐后端：`round(clamp(contextTokens / contextWindow × 100, 0, 100), 1)`；`contextWindow` 缺失或为 0 时结果为 `null`，显示 `-`。

#### 7.5.3 会话重置、无缓存 fallback 与 refresh 竞态

不能依赖 `statusBar.hide()` 作为会话切换钩子：当前 A→B 走 `chatView.close() + open(B)`，只有空态 `showEmpty()` 才调用 hide。新增 `statusBar.resetForSession(sessionId)` 并锁定调用点：

- `chatView.open(sessionId)` 在设置 `appStore.currentSessionId` 后立即 `resetForSession(sessionId)`；
- `chatView.close()` / `showEmpty()` 重置为 null；`hide()` 同时执行 null 重置，但只作兜底；
- reset 清空 snapshot/pending/in-flight 引用并递增 generation。每个 refresh 捕获 sessionId + generation + requestId；迟到响应/`finally` 只有三者仍匹配时才能提交或清理当前状态，不能污染新会话。

若当前会话无快照（包括 snapshot 属于旧 session）：

1. 缓存有效 incoming 为 `pendingUsageUpdate`；后续 pending candidate 复用 §7.5.2 的三字段支配规则：三项均不小于现 pending 才替换，任一倒退则忽略，相等可替换以携带较新 context/cost。
2. 同一 session/generation 只保留一个非权威在途 `refresh()`；fallback 与 `chatView.open()` 末尾原有的普通 refresh 必须走同一个单飞入口并复用 promise，不形成两次 GET 或请求风暴。
3. refresh 成功且 session/generation/requestId 仍匹配后建立完整快照，再调用一次 `applyUsageUpdate(pendingUsageUpdate)`；promise 的 catch/finally 也必须核对这三者后才能清当前 in-flight，旧 promise 不得清除新会话请求。

同一会话 refresh 也必须防止早发晚到的 GET 覆盖已接受的 SSE 累计值：

- 普通非权威 refresh 用响应中的累计 usage 判断新旧：响应旧则保留现有 usage/context/cost，响应同值或更新才接受 context 且 cost 仍不减；location/model/window 等非用量字段可正常更新；
- `refresh({authoritative: true})` 可完整替换快照并按管理员当前价格降低 cost；`chatView.onStreamClosed()` 必须显式使用该参数，不能依赖回调执行时 `appStore.stream` 恰好处于哪个 phase；
- 新发的权威 refresh 用 requestId 使更早请求失效；普通 `refresh()` 默认为非权威，只对 usage/context/cost 做上述单调合并，location/model/window 等完整 status 字段仍可更新。

#### 7.5.4 SSE 回调必须绑定连接身份并保留现有状态机

仅给 statusBar 做 generation 守卫不够：`sse.abort()` 后 promise 仍会 resolve；而 `confirm()` 会复用 waitingConfirm 的同一个 streamState，只改 phase/abort。故每次 `streamPost` 必须分配独立 connectionId，event/closed/failed 回调闭包捕获 `sessionId + streamState + connectionId`：

```javascript
var nextConnectionId = 1;

function bindConnection(streamState) {
  var connectionId = nextConnectionId++;
  streamState.connectionId = connectionId; // confirm 在同一对象上覆盖为新连接 id，使前一连接回调立即失效
  return connectionId;
}

function isCurrentConnection(sessionId, streamState, connectionId) {
  return sessionId === window.appStore.currentSessionId
    && window.appStore.stream === streamState
    && streamState.connectionId === connectionId;
}

function onBoundEvent(sessionId, streamState, connectionId, event, data) {
  if (!isCurrentConnection(sessionId, streamState, connectionId)) return;
  onStreamEvent(event, data);
}

function onBoundFailed(sessionId, streamState, connectionId, error, meta) {
  if (!isCurrentConnection(sessionId, streamState, connectionId)) return;
  onStreamFailed(error, meta);  // send 原样透传 {fromSend, isRetry}，保留 409 静默重试
}

function onStreamClosed(closedSessionId, closedStream, closedConnectionId) {
  if (closedSessionId !== window.appStore.currentSessionId) return;
  if (closedStream.connectionId !== closedConnectionId) return; // confirm 已启动新连接时，旧连接到此即止

  var currentStream = window.appStore.stream;
  // completed/error 会先 goIdle()，因此 null 允许本连接做最终 refresh；
  // 非 null 必须仍是本对象，否则同一会话已有另一条新流。
  if (currentStream && currentStream !== closedStream) return;

  void window.statusBar.refresh({authoritative: true}); // fire-and-forget；禁止 await 后再收尾

  if (!currentStream) {
    focusComposerIfReady();
    return;
  }
  // 此处 currentStream === closedStream 且 connectionId 仍匹配。
  if (currentStream.phase === 'waitingConfirm') return; // confirmationRequired 终态必须保留待确认态
  if (currentStream.phase === 'stopping') {
    goIdle();
    focusComposerIfReady();
    return;
  }
  if (!currentStream.terminalSeen) {
    markInterrupted();
    showError('连接中断：未收到终态事件，刷新页面可恢复最新状态。');
  }
  goIdle();
  focusComposerIfReady();
}
```

接入规则：

- send 新建 streamState 后分配 id；confirm **复用对象但必须先分配新 id**；attach 给 placeholder 分配 id。每个 handle 的 event/done/catch 都捕获该次 id，不能继续直接写 `handle.done.then(onStreamClosed)`。
- send 的 failed 包装必须原样透传 `{fromSend, isRetry}`。
- attach 只能在现有回调外层增加 identity 检查，不得替换其协议：event 仍先处理 `streamResume/preInitBuf`；done/failed 在 `initialized=false` 时仍只走 `resetToHistoryState`（保留 404 静默与 session/placeholder 守卫），仅 initialized 后调用绑定 connectionId 的 closed/failed。
- `onStreamClosed()` 必须保留完整现有状态机：null、waitingConfirm、stopping、无终态断流、正常终态五条路径。不得把 waitingConfirm 误清为 idle。
- 旧 session、旧 stream 对象或同一对象的旧 connectionId，其迟到 event/closed/failed 全部静默丢弃；A 的关闭回调不得为 B refresh/goIdle，确认前一泵的迟到 closed 也不得清除新 confirm 泵。
- 权威 refresh 必须 fire-and-forget；只有当前仍为 closedStream 且 connectionId 匹配时才执行 phase/goIdle。`onStreamEvent()` 的 usageUpdate 分支只调用 `statusBar.applyUsageUpdate(data)`，不迁移 phase。

### 7.6 费用一致性与配置变化

- liveCost 只用于当前活跃 SSE 展示，不写盘。
- 一个泵的 `dbBaseCost` 与 `pumpModelCost` 在首个有效 usageUpdate 时从 pending 惰性转为 ready 或 unavailable，失败后本泵不重试；模型 step 间不重复查 SQLite/YAML，且初始化不占用 `managerLock`。
- 当前 API 下，pump 直接调用 `querySessionCost()` 和 `loadCostMap()` 各一次，而前者内部还会读取一次 costMap；接受首个 usageUpdate 最多两次 YAML 读取的固定成本与极窄改价窗口，不为此修改 usageStore 公共签名。
- `_recordUsage()` 使用泵固化的实际 provider/model 写整泵 delta，解决流中 `/model` 提前改索引导致账单套错模型的既有边缘问题。
- 终态 `$` 仍由 status API 查询 usage.db，并按 `config/models.yaml` **查询时当前价**汇总；若流中管理员修改价格，liveCost 与终态可能不同，最终 refresh 权威校准。
- `cost=0` 仍显示 `$-`；load 成功但模型价格 key 缺失时本泵增量按 0 计；query/load 抛异常时 SSE DTO 为 `cost:null`，不阻断 token 更新。

## 8. 需要修改的文件

版本基线以调研基线 `2655e4b` 文件头为准；实施时若文件头已顺延，按当前版本 +0.1 续接。

| 文件 | 精确改动 | 预计版本 |
|---|---|---|
| `flamingoAgents/core/types.py` | 新增 `usageUpdateEvent` | 1.8 → 1.9 |
| `flamingoAgents/core/agent.py` | 外层 model step 值拷贝基线；保留 safePayload 守卫；terminal usage 含标准输入/输出字段时 append 后 yield usageUpdate | 1.21 → 1.22 |
| `webApp/backend/agentManager.py` | 固化实际模型；首个有效 usage 事件在泵线程惰性缓存费用；Core 事件转 DTO；stop 前后检查；中间 sessions 更新；终态原子认领并按实际模型落账 | 1.9 → 1.10 |
| `webApp/backend/sseCodec.py` | 定义可空 cost 的 `usageUpdateDto`；永久编码 Core 事件与 Web DTO 两种 usageUpdate 输入 | 1.4 → 1.5 |
| `webApp/frontend/js/statusUsage.js`（新建） | 无 DOM 纯计算模块：单调合并、归一化、百分比、费用格式化 | 1.0，含规定文件头 |
| `webApp/frontend/js/statusBar.js` | 按 session/generation 缓存 status；严格数值归一；`applyUsageUpdate()`；单请求 fallback + pending；`resetForSession()` | 1.3 → 1.4 |
| `webApp/frontend/js/chatView.js` | 消费 usageUpdate；重置状态栏；每次 streamPost 分配 connectionId；三类回调绑定 session+stream+connection；保留完整关闭状态机 | 1.18 → 1.19 |
| `webApp/frontend/index.html` | 在 statusBar.js 前加载 statusUsage.js | 1.12 → 1.13 |
| `docs/webApiSpec.md` | §2.1/§3.14 中间回写；§4.3 新事件与完整序列；状态机非终态分支；临时/权威 cost | 1.17.1 → 1.18 |
| `tests/testLiveUsageUpdate.py`（新建） | pytest 覆盖 Core、pump、SSE 与前端 Node 纯函数 | 1.0，含规定文件头 |

`server.py` 无需修改：模型标识从 pump 已持有的 `agent.modelAdapter.config` 固化，而非增加路由 meta。

不需要修改 adapter：它们已经正确请求/归一化终态 usage；问题在 finalChunk 之后缺事件和 UI 通路。

## 9. 分阶段实施计划与成功标准

```text
1. Core 事件 + Core SSE 映射原子落地 → 验证：每个有完整 terminal usage 的模型 step 恰好一条，codec 不落 error 兜底
2. Web DTO/中间索引/惰性 liveCost/实际模型落账 → 验证：锁内无费用 I/O、history 只存 DTO、DB 流中不写、终态模型与费用正确
3. 前端 helper/入口/按会话重置/即时重绘 → 验证：旧帧不倒退、缺 cost 不清零、无缓存不风暴、切会话不串状态
4. 契约与全量回归 → 验证：stop/attach/confirm/model 切换/终态对账一致
```

### Phase 1：Core 事件 + Core SSE 编码（原子合入）

- [ ] T1.1 在 `types.py` 新增 `usageUpdateEvent`，字段只含 camelCase token/context，不含 Web 价格配置
- [ ] T1.2 在 `driveModelLoop()` 外层 `while True` 每次迭代开头、retry 循环之外，对三项 usage 做 int **值拷贝**；同一 step 多个 attempt 共享基线，禁止引用赋值
- [ ] T1.3 保留 `responsePayload → safePayload` 类型守卫；`appendAssistantMessage()` 后，仅当 raw usage 的 `prompt_tokens/completion_tokens` 都是非布尔非负整数时，用 §7.2 锁定来源 yield 一条 usageUpdate；空/不完整/非法/无 usage 不发事件
- [ ] T1.4 保证 usageUpdate 不加入 `terminalEventTypes`
- [ ] T1.5 `sseCodec` 增加并永久保留 `usageUpdateEvent → usageUpdate` 分支（usage/stepUsage/contextTokens，无 cost）。T1.1–T1.5 必须同一原子变更合入，Core yield 严禁先于映射单独部署
- [ ] T1.6 测试纯文本顺序：`textDelta* → usageUpdate → completed`
- [ ] T1.7 测试免确认工具与 confirmation 两种顺序；每个有完整合法 usage 的 assistant/model step 恰好一条，confirm 新泵不重复上一 step
- [ ] T1.8 测试 Core 所见 payload：失败/中断/无 finalChunk/非 dict responsePayload 安全且不发；无/空/仅 total/缺字段/null/字符串/浮点/负值不满足门卫（既有 `_accumulateUsage` 若抛错，只断言异常前无事件）；合法全 0 可发，并注明 Responses 原始空 dict 会在 adapter 后成为此类全 0；字段 key/stepUsage 精确
- [ ] T1.9 测试 Core usage 事件经 `eventToFrame()` 编码为 `usageUpdate` 而非 error；未知 dict/匿名对象仍走 error 兜底

成功标准：Core 消费者在每个收到完整 terminal usage 的模型调用后拿到精确累计/本 step/context；原终态与工具事件语义不变；SSE 安全网映射始终存在。

### Phase 2：Web 泵、DTO、费用与终态落账

- [ ] T2.1 pump `__init__()` 仅固化 adapter `configProviderId/model`、费用状态及 `usageRecordLock/usageRecordDone`，禁止 SQLite/YAML I/O；首个有效 usage 事件在泵线程中惰性初始化费用
- [ ] T2.2 在 `sseCodec.py` 定义 `usageUpdateDto` dataclass；agentManager 导入它；禁止 dict、禁止放入 core.types、确认无循环依赖
- [ ] T2.3 按 §7.3.2 伪代码修改 `_pump()`：先检查 stop/done，再识别 Core usageUpdate，尝试 sessions 更新/惰性费用初始化/DTO 转换，第二次检查 stop/done 后才入 history/broadcast；转换不得放进 `_broadcast()`；中间 sessions 写失败只记诊断且不终止模型流
- [ ] T2.4 以 `event.usage - startUsage`（各字段非负）计算当前泵累计 delta，不做逐事件 `+=`
- [ ] T2.5 用 pending/ready/unavailable 状态惰性初始化费用且本泵不重试：querySessionCost 或显式 loadCostMap 抛异常→unavailable/cost=null；load 成功但无模型 key→ready/增量0；主流继续，后续 step 不再直接查询
- [ ] T2.6 中间事件不调用 `writeUsageTurn()`；终态 `_recordUsage()` 保持整泵唯一落账，并改用泵固化的实际 provider/model
- [ ] T2.7 `sseCodec` **增加** `usageUpdateDto → usageUpdate` 分支（含 cost），同时永久保留 T1.5 的 Core 分支；禁止“替代/改认”
- [ ] T2.8 attach history 回放 DTO；确认 `compactDeltas()` 不合并 DTO，history 不维护旁路 cost 表
- [ ] T2.9 monkeypatch 断言：pump 对 `querySessionCost/loadCostMap` 各直接调用一次且仅在首个有效事件发生（两者同时 stub，明确不统计真实 `querySessionCost` 内部的 loadCostMap）；另用集成断言记录真实路径允许 YAML 最多读取两次；两次 usageUpdate 不双加；流中 `writeUsageTurn` 0 次、终态调用 1 次；终态参数是泵实际模型
- [ ] T2.10 严格按 §7.3.2 完整伪代码实现 `usageRecordLock + usageRecordDone`：锁内只认领，锁外 wait/I/O/set，所有 owner 早退/异常 finally set；持久化异常只记诊断不冒泡且仍继续 seal；测试 requestStop/finally 竞争、conversation 缺失与 I/O 抛错均不死锁/不双写/关闭哨兵晚于落账

成功标准：事件发出前 sessions 已有最新 token/context；正常 history 只存 DTO；流中不落账；liveCost 手算一致；终态仅一次且记录实际模型。

### Phase 3：前端即时更新

- [ ] T3.1 新建 `statusUsage.js` 无 DOM 纯计算模块，挂 window/globalThis；incoming usage 缺对象/缺 key/非有限/负数则拒绝，合法值归一为非负整数；snapshot 老字段读侧按 0；实现累计新旧判断、单调 cost、精确百分比与格式化
- [ ] T3.2 修改 `index.html`，按 `statusUsage.js → statusBar.js → chatView.js` 顺序加载；更新文件头版本
- [ ] T3.3 `statusBar` 增加 `resetForSession(sessionId)` 与 generation/requestId；`chatView.open()` 显式重置到新 session，`close()/showEmpty()/hide()` 重置为 null；错会话快照按无快照处理
- [ ] T3.4 `statusBar.refresh(options)` 按 session/generation 防迟到响应；默认非权威，不得以陈旧响应降低已接受 SSE usage/context/cost；`authoritative:true` 可完整校准并使更早请求失效
- [ ] T3.5 新增 `applyUsageUpdate()`：三项累计 usage 任一倒退则整帧忽略；接受时覆盖 usage/有效 context；仅有限数字 cost 才处理，current cost 非有限先按 0，再保证活跃流中不减；不丢 location/model/window 字段
- [ ] T3.6 context 百分比严格执行 `round(clamp(...), 1)`；未知/0 窗口为 null，显示 `-`
- [ ] T3.7 无当前 session 快照时，同 session/generation 只发一次非权威 refresh；pending 候选复用同一三字段规则（全不小于才替换、任一倒退忽略、相等可替换），refresh 后应用；切会话清理且迟到请求不污染
- [ ] T3.8 每次 send/confirm/attach 的 streamPost 分配新 connectionId（confirm 覆盖复用对象的旧 id），event/closed/failed 绑定 session+stream+connection；attach 保留 streamResume/preInit/404 reset；send failed 保留 meta；按 §7.5.4 完整 closed 状态机收尾，usageUpdate 不改 phase
- [ ] T3.9 pytest 驱动 Node 直接 require `statusUsage.js`：验证输入归一/减法/百分比/cost/缺 cost 保留/current cost undefined/陈旧帧与 pending 比较/字段不丢/权威 refresh 可校准
- [ ] T3.10 分别执行三条 `node --check`；静态检查三条流均分配/捕获 connectionId、无直接 `then(onStreamClosed)`、attach 未初始化 reset 分支、send meta、closed 未 await，并保留 waitingConfirm 早退/stopping/`!terminalSeen` 分支

成功标准：工具循环每个模型 step 后立即更新五项；attach 旧帧不倒退；Phase 1 无 cost 帧不把费用清零；text delta 不轮询；最终显示与 status API 一致。

### Phase 4：契约、回归与人工验收

- [ ] T4.1 更新 `webApiSpec.md` §4.3：usageUpdate 两种后端对象映射为同一 SSE data、非终态语义、纯文本/免确认/批准/拒绝完整事件序列
- [ ] T4.2 更新 §2.1 与 §3.14：usage/context 可在模型 step 中间回写；lastUsage/usageTurns 仍整泵；liveCost 临时、关闭后 status 权威；usageTurns 记录泵实际模型
- [ ] T4.3 更新状态机：usageUpdate 只重绘状态栏，不迁移 phase；stopping 可忽略并由 closed refresh 校准
- [ ] T4.4 `uv run pytest` 全量通过
- [ ] T4.5 人工验收“模型 → 长 bash/read → 模型”：工具执行期间已显示第一步 usage
- [ ] T4.6 人工验收连续两次工具循环：数字单调、最终值与 JSONL 映射求和一致
- [ ] T4.7 人工验收多窗口 attach：先 status refresh 再从头回放 history 时不倒退，两个窗口最终一致
- [ ] T4.8 人工验收 confirmation 竞态：confirmationRequired 后立即批准/拒绝（不等待旧 SSE done）；旧 connection closed 不清新 confirm 流；确认前 usage 已显示，新泵只计后续增量且最终一条 usageTurns
- [ ] T4.9 人工验收 stop/断流/空 usage：无完整 terminal usage 不发中间事件；已完成 step 可最终对账且不重复计费
- [ ] T4.10 人工验收首次打开、A→B 与同 session 连续连接：open/fallback 共用请求；旧 session/stream/connection 的迟到 event/closed/failed 不渲染、不权威 refresh、不 goIdle 新连接；旧 refresh/finally 不清新状态
- [ ] T4.11 人工验收入口与活跃流 `/model`：页面无 `statusUsage is not defined`；流中切模型返回既有 409 时，本泵 usageTurns 仍记录实际旧模型，下一泵使用新模型

## 10. 测试设计

使用项目既有 `pytest`，前端纯函数断言沿用 `tests/testSubscriptionModelsJs.py` 的“pytest 启动 Node assert”方式，不引入新测试框架。

### 10.1 Core 单元测试

1. fake adapter 返回带 usage 的 finalChunk，无工具：断言 usageUpdate 数量、字段和事件顺序。
2. fake adapter 连续两次完成（第一步工具调用、第二步文本）：断言累计 usage 单调、stepUsage 分别精确且非 0；借此防 `stepStart` 引用同一可变 dict。
3. 同一 step 第一次 attempt 可重试失败、第二次成功：断言只快照一次、只发一条 usageUpdate、step delta 覆盖成功调用。
4. 对 Core 所见 payload：adapter 抛错/中断/无 finalChunk/非 dict responsePayload 无 usageUpdate，非 dict payload 由 safePayload 保证不抛；无/空/仅 total/缺字段/null/字符串/浮点/负值不满足门卫。字段非法时沿用 `_accumulateUsage` 既有转换/异常，只断言 yield；合法全 0 可发。另测 Responses 原始空 dict 经 normalize 后属于合法全 0，避免混淆层级。
5. cached 是 prompt 子集：Core 不做非缓存减法，保留原生累计语义。
6. `usage`/`stepUsage` key 集合恰为 `promptTokens/cachedTokens/completionTokens`，contextTokens 为 int；禁止 snake_case 透传。
7. confirmation 序列：第一泵 usageUpdate 在 confirmationRequired 前；新泵不重发上一 step。

### 10.2 Pump/SSE 单元测试

1. monkeypatch `sessionStore.updateUsage`、`usageStore.querySessionCost/loadCostMap/calcTurnCost/writeUsageTurn`，记录调用顺序、次数和参数。
2. 在 `_pump()` flag 未置位时：断言先中间 update，再惰性初始化费用/构造 DTO，再 broadcast；`lastUsage=None`。flag 预置时三者均不发生。
3. 两个 step：liveCost 始终使用“当前累计减 startUsage”，不重复累计第一步；费用初始化只在首个有效事件发生。
4. 同时 stub `querySessionCost/loadCostMap` 时，断言 pump 对二者各直接调用一次且后续 step 不再调用；另保留一条真实 `querySessionCost` 调用链测试，确认显式模型价格查询叠加内部调用时允许 `loadCostMap` 总计两次，不写错误的“一次”断言。
5. 中间 `sessionStore.updateUsage` 失败：模型事件流继续且 DTO 仍广播；querySessionCost/显式 loadCostMap 任一抛错：状态 unavailable、本泵不重试、DTO cost=null；load 成功但模型 key 缺失：状态 ready、增量0、DTO cost=基线。
6. `eventToFrame()` 对 Core `usageUpdateEvent` 与 Web `usageUpdateDto` 均编码为 `usageUpdate`；前者无 cost、后者含数字或 null cost；普通 dict/匿名对象仍编码为 error。
7. subscribe/attach 回放 history 中的 DTO，终态与 None 哨兵顺序不变；`compactDeltas()` 不合并 DTO。
8. `_recordUsage()`：以 monkeypatch 计数断言终态前 `writeUsageTurn` 0 次、终态后调用 1 次；不用查表行数（全 0 delta 不落行）。
9. `/model` 已改 sessions 索引的模拟场景：终态 `writeUsageTurn` 参数仍是 pump 固化的实际 provider/model。
10. stop/finally：用 barrier 制造两个调用者竞争，断言只有 owner 执行 DB/sessions 回写，非 owner 在锁外等待 `usageRecordDone`；覆盖 owner 正常、conversation 缺失、DB 抛错、sessions 抛错四条路径，均 set done、不向调用方冒泡并继续 seal；关闭哨兵晚于落账尝试；最终无双写且 doneEvent 后无 usage DTO。

### 10.3 前端测试

纯计算全部下沉 `statusUsage.js`（无 DOM、挂 window/globalThis），Node 直接 require：

- incoming usage 缺对象/缺任一 key/NaN/Infinity/负数时拒绝；snapshot 老字段读侧归一为 0；合法 token 归一为非负整数；
- 累计 `{promptTokens:100,cachedTokens:60,completionTokens:20}` 显示 `↑40/↓20/⚡60`；
- context `50/200` 的纯函数数值严格等于 `25`，展示字符串为 `25%`；`25/300` 数值严格等于 `8.3`；超过窗口 clamp 到 100；未知/0 窗口为 null/`-`；
- cost 0 为 `$-`，正数固定四位；无 cost/null 保留已有 cost；current cost 为 undefined/null 时先按 0，不产生 NaN；
- incoming 任一累计 token 小于快照时整帧忽略（包括 context/cost）；三项相等或都更大可应用；pending 比较复用同一规则；
- 活跃帧 cost 小于缓存时保留较大值；权威 refresh 路径可直接替换为较低的新价格；
- 新事件不丢 workDir/gitBranch/providerId/modelId/contextWindow；
- snapshotSessionId 不匹配时视为无快照；不同 session 的事件不能合并到旧快照；reset generation 后旧 refresh 响应失效。

`statusBar.js` 的单请求 fallback 是闭包行为，不为测试而引入 DOM 框架；由可注入/简单 stub 的现有测试方式覆盖可行部分，其余由 T4.10 人工验收。

对 `chatView.js` 另做最小静态/行为护栏：三条 `streamPost` 都分配并捕获 session+stream+connectionId；不得残留直接 `then(onStreamClosed)`；attach 的 streamResume/preInit/reset 分支、send failed meta、closed 的 waitingConfirm/stopping/`!terminalSeen` 分支必须保留；closed 不 await，goIdle 前验证对象与 connection。人工 A→B 及“确认后立即开新 SSE”验收证明旧回调不触碰新连接。

### 10.4 数据对账

JSONL `assistantMessage.usage` 永远保留 snake_case；求和时必须先做与 `conversation._accumulateUsage()` 相同的映射：

```text
mappedJsonlUsage.promptTokens
= Σ assistantMessage.usage.prompt_tokens

mappedJsonlUsage.cachedTokens
= Σ assistantMessage.usage.prompt_tokens_details.cached_tokens

mappedJsonlUsage.completionTokens
= Σ assistantMessage.usage.completion_tokens

sessions.usage
= conversation.usageTotal
= mappedJsonlUsage

本泵 usageTurns.delta
= 终态 usage - 泵 startUsage

本泵 usageTurns.providerId/modelId
= pump.modelAdapter.config.configProviderId/model

状态栏 ↑ + ⚡
= sessions.promptTokens

状态栏最终 $
= querySessionCost(sessionId)
```

## 11. 风险与处理

| 风险 | 等级 | 处理 |
|---|---:|---|
| Core 事件先于 sseCodec 映射，未知事件落 error 终态兜底并冻结前端流 | 高 | Core yield 与 Core codec 分支原子合入；DTO 上线后仍永久保留 Core 分支；T1.9 与 §10.2 第 6 条双输入/未知输入测试 |
| step 基线误用 `stepStart = usageTotal` 引用，导致 stepUsage 全 0 | 高 | 外层 while、retry 之外做三字段 int 值拷贝；Core 双 step/retry 测试断言非 0 精确 delta |
| 新建 statusUsage.js 但未在 statusBar 前加载，运行时 helper 未定义并中断 SSE 回调 | 高 | §8/T3.2 明确修改 index.html；静态检查 script 顺序；T4.11 浏览器验收 |
| 把逐模型 step 写成逐 token 精确统计，形成错误承诺 | 高 | 契约明确“terminal usage 后更新”；逐 chunk 估算另立需求并标 estimated |
| 中间写 DB、requestStop/finally 裸布尔竞争、错误持锁等待或落账异常冒泡导致双计费/死锁/SSE 不关 | 高 | 流中不写 DB；完整伪代码锁内认领、锁外 wait/I/O/set；所有早退/异常 set done；持久化异常只记诊断并继续 seal |
| liveCost 在第二步重复累加第一步 | 高 | 每次使用 `event.usage - startUsage`；禁止无基线 `+=` |
| 流中 `/model` 使 live/终态账单套用不同模型 | 高 | liveCost 与 `_recordUsage()` 都使用 pump 固化的 `configProviderId/model`；测试终态 write 参数 |
| snake_case 原始 usage 误作 camelCase 透传/对账 | 高 | 事件只取 usageTotal；JSONL 对账显式映射；key 集合测试 |
| 空/不完整/非法 usage 触发全 0/context=0 中间事件 | 中 | 事件门卫要求两项非布尔非负整数；门卫只控制 yield，不清洗 JSONL/_accumulateUsage，协议违规输入的既有转换/异常保持不变 |
| 在 `managerLock` 内初始化费用阻塞所有会话 start/stop/attach | 中 | `__init__` 只固化无 I/O 字段；首个有效 usage 在泵线程惰性初始化 |
| stop 与中间转换竞态导致终态后 sessions 再写一次 | 中 | `_pump` 前后两次 flag 检查；窄窗口只允许 lastUsage=None 的同值 sessions 重写，DB/DTO/最终不变量不受影响，不为此加跨线程锁 |
| 旧 session/stream/connection 迟到回调刷新或 goIdle 新连接（confirm 会复用对象） | 高 | 每次 streamPost 新 connectionId；三类回调绑定三重身份；完整 closed 状态机；A→B/立即确认竞态验收 |
| attach 回放旧累计造成 UI 倒退 | 中 | 以三项累计 usage 判旧；任一倒退则整帧忽略；活跃 cost 单调不减；终态 refresh 可权威校准 |
| Core 帧无 cost 或费用初始化失败把已有费用刷成 `$-` | 中 | query/load 异常明确为 unavailable/cost:null；仅有限数字覆盖，null/缺字段保留，current 非有限先按 0 |
| 每个 step 重扫 usageTurns/YAML | 中 | 首个有效事件惰性缓存，后续 step 不再查询；如实接受 querySessionCost 内部 + pump 显式模型价共两次 YAML 读取 |
| 改坏 lastUsage“最近泵流”语义 | 中 | 中间 `updateUsage` 传 `lastUsage=None`；只由终态 `_recordUsage` 覆盖 |
| provider 不返回完整合法 usage | 中 | 无/空/缺标准输入输出字段/非法值均不发 usageUpdate；保持旧值并注明 unavailable |
| 中间 updateUsage 刷新 updatedAt / 重写 sessions.json，或写失败拖垮主流 | 低 | 每模型 step 仅一次；不加 skipTouch；写异常只记诊断并继续广播，终态再持久化 |
| 首个事件两次读价间管理员恰好改 YAML | 低 | 接受极窄临时口径差；不改 usageStore API；终态 status 按查询时当前价权威校准 |
| 纯文本单调用看起来仍“结束才更新” | 低 | 这是精确 usage 最早到达时机；方案消除多工具 Agent 整轮延迟 |

## 12. 非目标

- 不新增 tokenizer 依赖。
- 不做逐字符/逐 chunk 的未标注估算。
- 不修改 provider adapter 的 usage 口径。
- 不迁移 usage.db schema。
- 不改变 usageTurns、lastUsage 的“每泵一条/最近完整泵”语义。
- 不改变 `/model` 的“索引先更新、活跃流返回 409、下一泵生效”接口行为；仅确保当前泵账单记录实际模型。
- 不顺手重构状态栏、泵线程或相邻 UI。

## 13. 回滚

1. **首选：整体 revert 功能提交**。全程无 DB schema/data 迁移，回滚不需要修复历史账单。
2. 前端分步回滚必须按依赖逆序：
   1. 先回退 chatView 的 usageUpdate 消费、`resetForSession` 调用与 authoritative refresh 参数（流身份绑定是独立竞态修复，可选择保留）；
   2. 再把 statusBar 回退为不依赖 statusUsage helper 的原实现；
   3. 最后移除 `index.html` script 与 `statusUsage.js`。不得在 statusBar 仍调用 helper 时先摘 script。
3. 后端分步回滚顺序强制为“先停产事件、最后删共享类型”：
   1. 先只移除 Core `usageUpdateEvent` yield，**暂时保留 dataclass 与两条 codec 映射**；
   2. 移除 pump DTO 转换、中间 sessions 更新与惰性费用状态（此时没有 Core 事件进入 pump）；
   3. 移除 agentManager 对 DTO/Core 类型的导入，再移除 sseCodec 的 DTO 分支与 DTO 定义；
   4. 最后原子移除 sseCodec 的 Core 分支及 Core `usageUpdateEvent` dataclass。
4. **严禁保留 Core yield 而先移除 Core sseCodec 映射**：未识别事件会落入 `eventToFrame()` 的 error 兜底帧，前端按终态错误处理并静默丢弃后续事件；也严禁先删 dataclass 而保留下游 import，避免启动即 ImportError。
5. `_recordUsage()` 的实际模型与原子认领不涉及 schema，可作为独立正确性修复保留；若明确回滚，只恢复旧索引取值/裸布尔，不需要数据迁移，已正确写入的历史行不回改。

## 14. 最终建议

采用方案 D，并把验收措辞锁定为：

> **每个模型 API 调用收到包含标准输入/输出计数的完整 terminal usage 后，状态栏立即精确更新；不再等待同一 Agent 请求中的后续工具和模型步骤全部结束。逐文本 chunk 不宣称精确。**

这利用现有 `appendAssistantMessage()` 已具备的精确模型调用边界，只新增一条从 Core 到 SSE/UI 的事件通路；同时保持账单每泵一条、终态权威校准与旧前端兼容，避免轮询、tokenizer、DB schema 迁移和重复计费。