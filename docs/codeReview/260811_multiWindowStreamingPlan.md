'''
Author: wilbur
Version: 1.0
Date: 2026-08-11
Description: multiWindowStreamingPlan 方案审核报告：核实 baseCount 采样时序、attach 回放与 onStreamEvent 兼容性、
             并发竞态、替代方案与遗漏场景；结论 = 方案总体成立，4 个 High 必修（close 不 abort 未完成 attach、
             stop 无终态广播、守卫清单漏 error case、attach 前初始化失败无回落）。
'''

# 方案审核报告 — multiWindowStreamingPlan（多窗口并行流式）

## 总览

- 审核对象：`docs/multiWindowStreamingPlan.md` v1.0
- 核实源码：`webApp/backend/agentManager.py` (v1.4)、`sseCodec.py` (v1.0)、`server.py` (v1.5)、
  `webApp/frontend/js/chatView.js` (v1.5)、`sse.js` (v1.0)、`main.js` (v1.0)，
  旁证 `flamingoAgents/core/agent.py`（生成器惰性、queued/dangling 落盘顺序）、`historyView.py`（坐标系一致性）
- 发现问题：🔴 0 / 🟠 4 / 🟡 4 / 🔵 3
- 整体评价：方案核心架构（泵广播化 + baseCount 水位 + attach 回放）**方向正确、并发骨架严密**，
  baseCount 采样时序经核实**确实安全**。主要缺口集中在：①前端 attach 尚未初始化完成时的生命周期管理
  （close 不 abort、失败不回落）；②stop 场景无终态广播导致 E5 预期错误；③守卫清单与现状代码不符（漏 error case）。

---

## 用户关注点逐项结论

### 1) baseCount 采样时序：✅ 安全

- `runUserMessageStream` / `continueConfirmationStream` 均为**惰性生成器**（agent.py:66/82，函数体首个 `yield` 前不执行），
  `appendUserMessage` 发生在泵线程首次迭代内（agent.py:115）→ 路由层在 `startStream` 之前采样必然先于任何本次流写盘。
- 同会话第二个写者被 `startStream` 的 managerLock 内 409 检查排除；采样虽是锁外只读，但即使 409 拒绝也无副作用。
- 坐标系一致：baseCount 与前端 GET messages 同走 `historyView.loadMessages`（同一过滤口径），dangling 路径
  「先落 toolResult、后落 queued userMessage」（agent.py:105-114）不影响 baseCount 精确性——方案 §3.1 的论证成立。
- 细微点（非问题）：采样发生在 409 检查之前，属无害只读；E10 的 `baseCount > messages.length` 实际不可能发生
  （jsonl append-only 且删除会话被 409 拦截），`Math.min` 钳制是无害防御，可保留但注释应写明是纯防御。

### 2) attach 回放与 onStreamEvent 兼容性：⚠️ 基本兼容，守卫清单有错误（见问题 H3）

已核实兼容的路径：
- **dangling 归位**：截断历史 [0:baseCount] 末尾未配对 toolCalls 渲染为 dangling 灰卡入注册表 → 回放 toolCallStart/End
  命中注册表走既有归位路径，`ownedByLiveBlock=false` 不触发 newStep ✓（E7 成立）。
- **step 边界**：回放路径所有新卡片 `ownedByLiveBlock=true`，D6 隐式边界重建与原始 live 完全一致 ✓。
- **thinking 块**：reasoningDelta 回放 → 展开「思考中…」→ text/tool/终态折叠「已思考」，与 live 同路径 ✓；
  快速回放仅有一次展开→折叠跳变，可接受。
- **toolCallEnd 无需守卫**：`resolveToolCardOnEnd` 经 `liveBodyEl()` 兜底建 step，且
  `if (resolved.ownedByLiveBlock)` 为 false 时短路不访问 `stream.currentStep`，天然安全 ✓。
- **空气泡规避**：dangling 全部归位时 `currentStep` 保持 null，不留空 assistant 壳 ✓（懒建策略正确）。

### 3) 并发与竞态：⚠️ subscribe/broadcast 骨架正确，stop 语义有缺口（H2）

- `subscribe()` 回放+登记 与 `_broadcast()`/closed 置位同在 `subLock` 内 → 回放与实时无缝衔接，不丢不重 ✓。
- 「泵结束瞬间 attach」：`getActivePump` 拿到引用后即使泵随即 unregister+closed，`subscribe()` 走 closed 分支
  回放全量 history + None 哨兵 ✓；`getActivePump` 返回 None 则 404 → 前端回落 ✓。两条路径都闭合。
- 缺口：stop 时泵 `break` 出循环**不广播任何终态事件**（agentManager.py 现状即如此），单窗口时代靠前端正
  `phase='stopping'` 掩盖，多窗口下其他 attach 窗口会误报「连接中断」（问题 H2）。

### 4) 更简单的替代方案：方案未记录取舍（见「替代方案评估」）

### 5) 遗漏场景：见问题 H1/H4/M1/M2 及边界补充

---

## 问题清单

### 🟠 H1. close() 无法 abort「尚未收到 streamResume」的 attach 连接 → 跨会话渲染污染

**位置**: 方案 §5.1 `attachStream` / `initAttachedStream` 伪代码 + chatView.js:926 `close()`
**问题**: attach 的 SSE fetch 已发出、但 `streamResume` 未到达期间，`window.appStore.stream` 仍为 null
（stream 对象在 `initAttachedStream` 里才创建）。此时用户切走会话：
1. `chatView.close()` 只 abort `stream.abort` → **abort 不到这个在途 attach**，fetch 继续运行；
2. `streamResume` 到达后 `initAttachedStream(A)` 仍会执行：`renderHistory(A 的消息)` 直接 wipe 掉当前会话 B 的视图、
   `appStore.stream` 被置成 A 的流、composer 在 B 会话里显示「停止」；
3. 后续 A 的增量事件渲染进 B 的页面；更糟的是 A 的 `done.then(onStreamClosed)` 在流结束时可能把 B 自己的 stream 状态
   goIdle 掉。用户点「停止」用的是 `currentSessionId=B`，整个状态机错位。
这是真实可达的竞态（快速 A→B 切换 + 慢网络/首帧延迟），且违反会话隔离。

**修复方案**（两处都要）：
```js
// 1) 模块级挂起句柄，close() 兜底 abort
var pendingAttach = null;
function attachStream(sessionId, messages) {
  var handle = window.sse.streamPost(...);
  pendingAttach = handle;
  handle.done.then(...).finally(function () { if (pendingAttach === handle) pendingAttach = null; });
}
// chatView.close() 增加：
if (pendingAttach) { pendingAttach.abort(); pendingAttach = null; }

// 2) initAttachedStream 入口会话守卫（双保险，防 abort 与帧到达竞态）
function initAttachedStream(sessionId, messages, meta) {
  if (window.appStore.currentSessionId !== sessionId) { attachHandle.abort(); return; }
  ...
}
```

### 🟠 H2. stop 不广播终态事件 → 其他 attach 窗口误报「连接中断」，E5 预期不成立

**位置**: 方案 §6 E5 vs agentManager.py `_pump`（`if self.stopFlag.is_set(): break`，无终态广播）
**问题**: 泵被 stop 时直接 break，订阅者只收到 None 哨兵。单窗口时代发起 stop 的窗口处于 `phase='stopping'`
静默收尾；但**其他 attach 窗口**处于 `streaming` 态，`onStreamClosed` 命中 `!stream.terminalSeen` 分支
→ `markInterrupted()` + 弹错误条「连接中断：未收到终态事件，刷新页面可恢复最新状态」（chatView.js
onStreamClosed）。方案 E5 写的「广播终态 → 另一窗口 onStreamClosed → goIdle」**与泵的实际行为不符**——
根本没有终态被广播。这是误导性错误提示 + 方案文档的事实性错误。

**修复方案**（二选一，推荐前者）：
```python
# agentManager._pump：stop 分支补一个终态广播（webApp 层事件，不动库）
for event in self.stream:
    if self.stopFlag.is_set():
        self._broadcast(errorEvent(message='已手动停止。', errorType='stopped'))
        break
    ...
# chatView.js handleStreamError 增加：
if (data.errorType === 'stopped') { markInterrupted(); goIdle(); return; } // 静默，不弹条
```
这样发起 stop 的窗口（stopping 态只记 terminalSeen）与其他窗口（收到 stopped → 已中断标记 + 静默 goIdle）
语义一致，且非目标里的「不动确认流程语义」不受影响。

### 🟠 H3. §5.2 守卫清单与现状代码不符：漏 `error` case，`toolCallEnd` 并无该调用

**位置**: 方案 §5.2 vs chatView.js onStreamEvent
**问题**: 方案列的四个 case 是 `toolCallStart / toolCallEnd / confirmationRequired / completed`，但核实现状代码：
- `toolCallEnd`（chatView.js toolCallEnd case）**没有** `collapseThinkingIfOpen` 调用，不需要守卫；
- **`error` case 有** `collapseThinkingIfOpen(stream.currentStep.live)`（终态统一折叠），方案漏掉了。
  attach 回放首事件即 error 的场景真实存在：`pendingConfirmationExists` / `emptyMessage` 直通错误流
  （agent.py:96-101）整个 history 只有一个 errorEvent，`currentStep` 必为 null → `stream.currentStep.live`
  抛 TypeError → onEvent 在 sse.js 解析循环内炸出 → done reject → attach catch 走 onStreamFailed 弹条，
  且半截渲染状态残留。

**修复方案**: 与其逐个 case 点射，不如收敛为一个安全访问器，一处解决所有现状+未来 case：
```js
function currentLive() {
  var stream = window.appStore.stream;
  return (stream && stream.currentStep) ? stream.currentStep.live : null;
}
// 全部四个调用点改为 collapseThinkingIfOpen(currentLive())
//（collapseThinkingIfOpen 本身已容忍 null live）
```
同时修正方案 §5.2 的清单描述（toolCallEnd 移除、error 补上）。

### 🟠 H4. attach 在 streamResume 之前失败/断连 → 空白视图，无回落

**位置**: 方案 §5.1 `attachStream` 的 `handle.done` 链
**问题**: 方案只为 404 写了 `renderFullHistoryFallback()`，但还有两类「attach 未初始化即终结」：
1. 非 404 错误（500/网络错误）→ `onStreamFailed` → 弹条 + goIdle，但**历史从未渲染**，消息区空白、
   composer 可用，用户面对一个空会话页；
2. 连接在 streamResume 前被服务器关闭（如 uvicorn 重启）→ done resolve 'closed' → `onStreamClosed`
   里 `stream` 为 null 直接 return → 同样空白无任何提示。

**修复方案**: 以 `initialized` 标志统一兜底，而不是只认 404：
```js
handle.done.then(function () {
  if (!initialized) { renderFullHistoryFallback(); return; } // 未初始化即关闭：按无活跃流回落
  onStreamClosed();
}).catch(function (error) {
  if (!initialized) {           // 含 404 与一切预检/网络失败：静默回落历史态
    if (error.status !== 404) showError(error.message); // 非 404 给提示但仍渲染历史
    renderFullHistoryFallback();
    return;
  }
  onStreamFailed(error);
});
```
另外方案需明确 `renderFullHistoryFallback()` 的语义 = **现状 reloadSession 全路径**
（全量历史 + GET pending + enterWaitingConfirm），否则 E3（attach 404 时 pending 弹框恢复）落不了地——
当前 §5.1 伪代码只写了「全量历史渲染」，pending 恢复是缺失的。

### 🟡 M1. confirm 续流可能落盘 queued 用户消息，attach 丢失该用户气泡

**位置**: 方案 §4.3-2（chatConfirm `userMessage=None`）vs agent.py:143-148
**问题**: `driveUserMessage` dangling 路径若 `driveToolBatch` 以 confirmationRequired 终止（agent.py:107-108），
queued 用户消息**留在队列未落盘**；随后用户批准确认 → `driveConfirmation` 的 `takeQueuedUserMessage`
（agent.py:145-147）在 confirm 流中途落盘这条 user 消息。此时 attach 该 confirm 流：
baseCount 采样于 confirm 流启动前（不含它）、meta.userMessage=None（不补渲染）→ 该用户气泡两头落空，
直到下次全量 reload 才出现。发生概率低（需 dangling + 批量工具中触发确认 + 切窗口），但属于「不丢不重」
承诺的破洞。

**修复方案**: chatConfirm 采样 meta 时从 conversation 读 queued 消息一并带上：
```python
# chatConfirm 内，continueConfirmationStream 之后、startStream 之前
queued = None
with agentInstance.sessionLocksGuard:
    conv = agentInstance.conversations.get(sessionId)
    if conv is not None:
        queued = getattr(conv, 'queuedUserMessage', None)  # 只读 peek，不 take
meta = {'baseCount': baseCount, 'userMessage': queued}
# 前端 initAttachedStream 对 confirm 流同样走 appendUserMessage 分支即可（现有逻辑已兼容）
```
（若库未暴露该字段的只读访问，可加 property 或接受为已知边界写入 §6——但至少要显式声明。）

### 🟡 M2. 订阅者队列不反注册，断连客户端的队列滞留至流结束

**位置**: 方案 §4.1/§4.2（无 unsubscribe 设计）
**问题**: 客户端 abort/断网后，StreamingResponse 侧生成器被回收，但其队列仍挂在 `pump.subscribers` 里，
泵继续向死队列 put 直至流结束。单轮流内内存有界（事件量受回合长度限制），不会泄漏到流外，属「不健壮」
而非「泄漏」，但与方案自己宣称的「泵结束随 activeStreams 注销被 GC」口径不符（死队列是被 pump 持有的）。

**修复方案**: sseGen 增加 finally 反订阅：
```python
def sseGen(eventQueue, pump, meta=None):
    try:
        ... 原循环 ...
    finally:
        pump.unsubscribe(eventQueue)

# streamPump
def unsubscribe(self, q):
    with self.subLock:
        if q in self.subscribers:
            self.subscribers.remove(q)
```

### 🟡 M3. 长回合 attach 回放 markdown O(n²) 重渲染，可能卡顿

**位置**: 方案 §5.1 回放 vs chatView.js textDelta case（每个 delta 全量 `renderMarkdown(textBuf)`）
**问题**: live 流 delta 是人在看的节奏到达，O(n²) 无感；attach 回放是**瞬时连发**（几百~几千个 textDelta
一口气 replay），每个都全量 marked.parse + DOMPurify，长回复（几万字）回放会出现明显的主线程卡顿。
**修复方案**: 回放追平期间批量合并渲染，例如 initialized 后设 `catchingUp=true`，textDelta 只累 textBuf
不 render，遇到非 delta 事件或 `queueMicrotask`/50ms 定时器 flush 一次；或在 onStreamEvent 层做 rAF 节流。
（实现细节可留给 Phase 2，但方案应把「回放性能」从风险表 Low 提升为有明确对策的一项。）

### 🟡 M4. attach 初始化完成前 composer 处于「发送」态，可撞 409

**位置**: 方案 §5.1 open 流程
**问题**: open() 早期 `updateComposer()` 时 stream 为 null → composer 可用；从 Promise.all 返回（status.streaming=true）
到 streamResume 到达之间存在窗口（正常几十~几百 ms，慢网更长），用户可输入并点发送 → 后端 409 弹条。
概率低但与本次要修的原始 bug（按钮态误导）同性质。
**修复方案**: 进入 attach 模式时先置一个临时 stream 态（如 `phase:'attaching'`，composer 禁用、按钮「连接中」），
initAttachedStream 再转 streaming；404 回落时 goIdle。该态也顺带给 H1 的 close() 一个明确的 abort 锚点。

### 🔵 L1. status 端点的 git subprocess 进入 open 关键路径

**位置**: 方案 §5.1（Promise.all 加 getSessionStatus）vs server.py getSessionStatus（`subprocess.run(git ...)`）
**问题**: 该端点每次调用同步起 git 子进程（超时 2s），此前只在 statusBar 异步刷新；搬进 open 的 Promise.all 后，
会话打开延迟被 status 的最慢路径（git 异常时最多 +2s）拖住。attach 模式还额外串行一段 attach 往返。
**修复方案**: 可接受但建议优化——`/api/sessions/{id}/status` 的 streaming 字段改为由
`GET /api/chat/attach` 自身语义承载（404=不 streaming），open 直接尝试 attach、404 回落现状路径，
省掉 status 依赖；或拆一个轻量 `/sessions/{id}/streamState` 端点。若保留现状，至少在方案中记录该延迟代价。

### 🔵 L2. 文档事实性错误：history 内存上限的论证不成立

**位置**: 方案 §4.1「内存：history 随单轮流增长，受 maxModelSteps=32 限制」
**问题**: maxModelSteps 限制的是**模型步数**，不是事件数——单步的 textDelta/reasoningDelta 可以有数千个，
history 实际按「回合产出 token 数 / chunk 粒度」增长。结论（不设上限可接受）没问题，但论证是错的，
会误导后续维护者。
**修复方案**: 改为「事件数 ∝ 回合输出长度，单轮流存续期间有界，泵结束随订阅者释放被 GC；不设上限」。

### 🔵 L3. 方案 §4.1「history 泵结束后保留供迟到 attach 回放」实际不可达

**位置**: 方案 §4.1 streamPump 注释
**问题**: 泵 finally 里 `unregisterStream` 后 `getActivePump` 即返回 None → attach 直接 404，走历史自愈。
「结束后保留回放」唯一真实窗口是 getActivePump 已拿到引用、subscribe 前的竞态（由 closed 分支覆盖）。
描述本身会误导读者以为存在「终态后仍可 attach 回放」的通道。
**修复方案**: 注释改为「closed 分支仅覆盖 getActivePump→subscribe 间的竞态；泵注销后 attach 恒 404」。

---

## 替代方案评估（方案未记录，应补充取舍说明）

| 方案 | 改动量 | 覆盖目标 | 结论 |
|---|---|---|---|
| **A. 前端 DOM 驻留**（close 不 abort，messageList 按 sessionId  detach/reattach 缓存） | 纯前端 ~50 行 | G1+G3 完整；**G2（多窗口）不覆盖**；刷新页面退回现状半自愈 | 若 G2 不是硬需求，这是性价比最高的方案 |
| **B. 轮询续播**（status.streaming=true → 每 1-2s 拉 messages 增量渲染直至流结束） | 后端仅加 status 字段 | G1/G3 正确性 trivially 成立（jsonl 单一事实源），无 token 级实时性 | 体验降级明显，仅作兜底思路 |
| **C. 本方案（泵广播 + attach 回放）** | 前后端均改 | G1-G4 全覆盖，实时性无损 | G2 为硬需求时合理，但应在 §2 记录「为何不用 A」 |

建议：在方案 §2 非目标/背景补一段取舍记录，明确 G2 是选择 C 的决定性理由，避免后续评审反复。

---

## 优点记录

- baseCount 水位线选型正确：§3.1 对 dangling「先落 toolResult 后落 user 消息」的分析与 agent.py:105-114 完全一致，
  排除了「最后一条 user 消息」匹配的错误直觉。
- subscribe/broadcast/closed 的 subLock 设计闭合了「回放与实时衔接」「结束瞬间 attach」两个最难的竞态，骨架可直接落地。
- 「原 stream/confirm 响应不带 meta、前端 send/confirm 零改动」的兼容性切分干净。
- 守卫清单虽列错（H3），但「懒建 currentStep 避免 dangling 归位留空气泡」的判断准确，与 liveBodyEl 兜底语义自洽。

---

## 修复优先级建议

1. **H1（close 不 abort 在途 attach）**——违反会话隔离，必须先修，且修复成本低（pendingAttach + 会话守卫）。
2. **H3（守卫清单漏 error case）**—— pendingConfirmationExists 直通错误流下 attach 必崩，改 `currentLive()` 一处收敛。
3. **H2（stop 无终态广播）**——G2 多窗口是方案核心卖点之一，其主交互（跨窗口停止）当前必然误报「连接中断」。
4. H4 与 H1 同区域一并改；M1/M2 随 Phase 1/2 顺带落地；L1-L3 修订文档措辞。
