'''
Author: wilbur
Version: 1.3
Date: 2026-08-11
Description: 多窗口并行流式方案：解决「会话 A 流式中切到会话 B，A 的前端流被 abort、按钮由停止变回发送、重发撞 409」问题。
             核心思路 = 后端泵广播化（事件历史 + 多订阅者）+ 新增 attach 端点（带 streamResume meta 回放）+ 前端重进会话时
             按 baseCount 截断历史渲染并重放续流。库（flamingoAgents）零改动。
             v1.1：按 pi 审核报告（docs/codeReview/260811_multiWindowStreamingPlan.md）修订——H1 attach 在途句柄未登记致快速切换错位
             （改 attaching 占位流态 + sessionId 守卫）；stop 不广播终态致其他窗口误报连接中断（补 stopped 终态广播 + 前端静默 goIdle）；
             §5.2 守卫清单纠正（toolCallEnd 天然安全，真正漏的是 error case）；H4 attach 初始化前失败的统一兜底（含 pending 恢复）；
             confirm 流 queued 用户消息补渲染；订阅者反注册 + 回放 delta 压缩；补 §2.3 替代方案取舍记录。
             v1.2：按 pi 复审报告（docs/codeReview/260811_multiWindowStreamingPlan_v2.md）修订——§5.1 改乐观 attach
             （先渲染全量历史再后台 attach，消除 status.streaming 依赖/git 子进程进关键路径/空白页风险）；N1 占位身份守卫；
             N2 attach 逻辑落在 reloadSession 内（confirmationMismatch 自愈自动获益）；非 404 失败补提示；
             E13 confirm 流工具阶段 queued 气泡已知边界声明；L2/L3 注释与内存论证纠正；contract 不再改 status。
             v1.3：Phase1/2/3 代码实施完毕（agentManager 广播化、sseCodec、server attach 端点、chatView 乐观 attach、webApiSpec v1.7）；
             泵语义脚本验证 + curl 端到端（attach 回放/stopped 广播/404/幂等）通过；E1~E13 UI 验收待用户确认。
'''

# 多窗口并行流式方案（multiWindowStreamingPlan）

## 1. 背景与问题

### 1.1 现象

会话 A 流式输出中，用户切到会话 B（同标签页 hash 路由切换），A 的发送按钮由「停止」变回「发送」，感觉像中断；
用户在 A 重发消息撞 409「该会话已有活跃流」；但过一会 A 的内容其实跑完了（后端泵线程一直在跑）。

### 1.2 根因（已调研确认，详见代码注释）

1. **路由切换主动掐流**：`main.js route()` → `chatView.close()`（chatView.js:926）→ `stream.abort()` + `appStore.stream = null`。
   这是契约 §5 的既定设计（"路由切走时主动 abort 前端流，后端泵线程继续跑到终态，重进自愈"）。
2. **"自愈"只愈一半**：重进会话时 `reloadSession()` 只拉 `messages + pending` 渲染历史，
   既不查询「该会话是否有活跃流」，也没有 SSE reattach 机制（webApiSpec.md:499 明确"本期不做 SSE 重连"）。
   → 按钮永远回到「发送」，后续增量全部丢失，重发撞 409。

### 1.3 排除项（调研结论）

- 后端 `activeStreams` 按 sessionId 隔离，不同会话泵线程互不影响；LLM 请求每请求独立 urllib 连接，无共享客户端。
- 前端无跨标签页通信（无 storage 事件/BroadcastChannel/轮询）。真·多浏览器窗口场景下，只要 A 窗口不切路由，流本身不断。

## 2. 目标 / 非目标

### 2.1 目标

- G1：会话 A 流式中切走再切回，A 显示「停止」按钮，流式内容从断点**无丢失、无重复**地续播。
- G2：同一会话可在**多个浏览器窗口**同时观看（第二个窗口 attach 后同步渲染）；任一窗口可点停止。
- G3：不同会话并行流式互不干扰（后端已支持，前端回归验证）。
- G4：切走期间流已结束（completed/confirmationRequired/error）的会话，重进行为与现状一致（历史自愈 / pending 弹框）。

### 2.2 非目标

- 不做前端多流并存（切走后前端仍 abort 订阅，靠 reattach 回放恢复；不在后台维护其他会话的增量缓冲）。
- 不做侧栏「流式中」指示器、不做 409 自动转 attach（保留 409 作为并发写保护，见 §6 已知边界 E6）。
- 库（flamingoAgents）零改动；不动确认流程语义、不动 usage 回写口径。

### 2.3 替代方案取舍（审核意见：记录为何不选更简方案）

| 方案 | 做法 | 取舍 |
|---|---|---|
| A 纯前端 DOM 驻留 | close 不 abort、messageList 按 sessionId 缓存切换，~50 行零后端改动 | 只覆盖 G1+G3，**覆盖不了 G2（多浏览器窗口同会话同步观看）**，弃 |
| B 轮询续播 | 重进后定时拉 messages diff 渲染 | 正确性 trivially 成立，但无 token 级实时性，体验退化，弃 |
| **C 泵广播 + attach 回放（本方案）** | 后端多订阅者 + 事件历史回放 | G2 是硬需求（目标明确「支撑多窗口并行」），故选 C；代价是后端泵重构 + 前端 reattach 状态机 |

## 3. 总体设计

```
                 ┌──────────── 后端 ────────────┐
  POST /chat/stream ──► streamPump（广播化）      │
  POST /chat/confirm ──►  ├─ history[]（全量事件缓冲）│
  POST /chat/attach  ──►  ├─ subscribers[]（每订阅者一队列）│
  POST /chat/stop    ──►  └─ meta {baseCount, userMessage} │
                 └──────────────────────────────┘
   attach 响应 = [streamResume(meta)] + history 回放 + 实时事件
```

- **泵广播化**：`streamPump` 由「单队列单消费者」改为「事件历史 + 订阅者集合」。
  原始 `/api/chat/stream` 调用方 = 订阅者 0；`/api/chat/attach` = 后续订阅者，先入 meta 帧再回放历史再跟实时。
- **baseCount 水位线**：路由层在**启动泵之前**用 `historyView.loadMessages(sessionId)` 计数（与前端 GET messages 同一坐标系），
  存入 `pump.meta.baseCount`。attach 时前端据此把「本次流已落盘的尾巴」从历史渲染中截掉，改由事件回放重建，做到不丢不重。
- **userMessage 回放**：用户消息由 `send()` 直接渲染、不在事件流里；meta 携带 `userMessage`（含附件块拼接后文本），
  attach 前端用它补渲染用户气泡（走既有 ATTACHMENT_RE 解析，与历史渲染同构）。

### 3.1 为什么用 baseCount 而不是「最后一条 user 消息」匹配

dangling 恢复流会先落盘工具结果、后落盘用户消息（agent.py driveUserMessage 的 queued 路径），
「最后一条 user 消息」水位在该场景下会把上一轮的 assistant 正文切掉导致丢显示。
baseCount 由服务端在泵启动前采样，对所有路径（普通/dangling/confirm 续跑）都精确。

## 4. 后端改动

### 4.1 `agentManager.py` — streamPump 广播化

```python
class streamPump:
    def __init__(self, sessionId, agentInstance, stream, meta=None):
        # meta = {'baseCount': int, 'userMessage': str|None}
        self.meta = meta or {}
        self.subLock = threading.Lock()
        self.history = []        # 全量事件（含终态）；服务in-flight订阅者的断连前回放与 closed 竞态窗口的迟到 subscribe
        self.subscribers = []    # list[queue.Queue]
        self.closed = False
        # stopFlag / thread 不变；删除单一 eventQueue

    def subscribe(self) -> queue.Queue:
        q = queue.Queue()
        with self.subLock:
            for event in compactDeltas(self.history):  # 合并连续 textDelta/reasoningDelta，避免前端回放 O(n²) 重渲染
                q.put(event)
            if self.closed:
                q.put(None)
            else:
                self.subscribers.append(q)
        return q

    def unsubscribe(self, q) -> None:
        # 订阅者断连反注册（sseGen finally 调用），防止死订阅队列继续堆积事件
        with self.subLock:
            if q in self.subscribers:
                self.subscribers.remove(q)

    def _broadcast(self, event) -> None:
        with self.subLock:
            self.history.append(event)
            for sub in self.subscribers:
                sub.put(event)

    def _pump(self):
        startUsage = self._currentUsage()
        try:
            for event in self.stream:
                if self.stopFlag.is_set():
                    # 审核修复：stop 必须广播终态，否则其他 attach 窗口 onStreamClosed 命中 !terminalSeen 误报「连接中断」。
                    # stopped 语义按 errorEvent 下发（terminalEventTypes 成员），前端按 errorType='stopped' 静默 goIdle。
                    self._broadcast(errorEvent(message='已停止。', errorType='stopped'))
                    break
                self._broadcast(event)
                if isinstance(event, terminalEventTypes):
                    break
        except Exception as error:
            self._broadcast(errorEvent(message=str(error), errorType=type(error).__name__))
        finally:
            self.stream.close()
            self._recordUsage(startUsage)
            unregisterStream(self.sessionId)
            with self.subLock:
                self.closed = True
                for sub in self.subscribers:
                    sub.put(None)
```

- `startStream(sessionId, agentInstance, stream, meta=None)`：透传 meta；登记/启动同锁不变；同会话并发仍 409。
- 新增 `getActivePump(sessionId)`：managerLock 内查 `activeStreams`（attach 路由用）。
- 新增 `compactDeltas(history)`（模块级纯函数）：合并相邻同型 textDelta/reasoningDelta（text 拼接），其余事件原样保序；
  仅作用于 subscribe 回放副本，不改 history 本体。
- 并发正确性：`subscribe()` 与 `_broadcast()`/结束置 closed 都在 `subLock` 内 → 回放与实时无缝衔接，不丢不重。
- 内存：history 随单轮流增长；事件数无硬上限（长输出 delta 多），但单轮流有界且泵结束随 activeStreams 注销被 GC，
  实测可接受；死订阅由 unsubscribe 清理。

### 4.2 `sseCodec.py` — sseGen 支持 meta 首帧

```python
def encodeResumeFrame(meta) -> str:
    payload = json.dumps(
        {'baseCount': meta.get('baseCount', 0), 'userMessage': meta.get('userMessage')},
        ensure_ascii=False,
    )
    return f'event: streamResume\ndata: {payload}\n\n'

def sseGen(eventQueue, meta=None, pump=None):
    # meta 非 None（attach 订阅）时先发 streamResume 帧；其后只消费订阅队列（含回放），None 哨兵结束。
    # pump 非 None 时 finally 反注册订阅（客户端断连/生成器关闭清理死订阅）。
    try:
        if meta is not None:
            yield encodeResumeFrame(meta)
        while True:
            try:
                event = eventQueue.get(timeout=keepAliveIntervalSeconds)
            except queue.Empty:
                yield ': keep-alive\n\n'
                continue
            if event is None:
                return
            yield encodeSse(event)
    finally:
        if pump is not None:
            pump.unsubscribe(eventQueue)
```

- `sseResponse(pump)` 改为 `sseResponse(eventQueue, meta=None, pump=None)`；**所有调用点（含 /chat/stream、/chat/confirm 的
  订阅者 0）都传 pump**，保证任何订阅断连都反注册。
- 原 `/api/chat/stream`、`/api/chat/confirm` 响应 **不带** meta（`meta=None`），前端 send()/confirm() 路径零改动。

### 4.3 `server.py` — 采样 baseCount + 新端点

1. `chatStream`：`stream = agentInstance.runUserMessageStream(...)` 之后、`startStream` **之前**：
   ```python
   baseCount = len(historyView.loadMessages(sessionId))
   pump = agentManager.startStream(sessionId, agentInstance, stream,
                                   meta={'baseCount': baseCount, 'userMessage': cleanMessage})
   ```
   时序安全：生成器惰性，appendUserMessage 在泵线程首次迭代才发生（agent.py:115），采样必然在此之前；
   同会话并发被 startStream 409 拒绝，无第二个写者。
   注意：`cleanMessage` 是附件块拼接后的文本，与落盘/GET messages 的 content 一致，前端可直接走历史渲染同构路径。
2. `chatConfirm`：同样采样 `baseCount`（continueConfirmationStream 之前），`userMessage=None`。
3. 新端点：
   ```python
   @authedApi.post('/api/chat/attach')
   def chatAttach(body: dict = Body(...)):
       sessionId = checkSessionId(...)
       requireSession(sessionId)
       pump = agentManager.getActivePump(sessionId)
       if pump is None:
           raise HTTPException(status_code=404, detail='该会话无活跃流。')
       return StreamingResponse(sseGen(pump.subscribe(), meta=pump.meta, pump=pump),
                                media_type='text/event-stream', headers=sseHeaders)
   ```
   - 404 = 无活跃流（前端静默保持历史态，**不弹错误条**）。
   - waitingConfirm 态不是活跃流（泵已在 confirmationRequired 终态结束）→ attach 404 → 走既有 GET pending 恢复路径，语义不变。

## 5. 前端改动（chatView.js 为主）

### 5.1 open/reloadSession 流程（v1.2 改乐观 attach：历史先渲染，attach 后台进行）

设计要点：不再用 status.streaming 预判（避免 git 子进程进 open 关键路径，复审 L1），attach 本身就是探针。
**attach 逻辑落在 reloadSession 内**（复审 N2）：confirmationMismatch 自愈等所有走 reloadSession 的路径自动获得 reattach 能力。

```plain
reloadSession(sessionId):
  Promise.all([getMessages, getPending])
  lastAssistant = renderHistory(messages, pending)   // 现状不变：历史立即可见，无空白页、无额外延迟
  if pending: enterWaitingConfirm(pending, lastAssistant)
  attachStream(sessionId, messages)                   // 乐观 attach：404/失败 → 保持历史态；成功 → 截断重渲染 + 回放
```

attach 模式（审核 H1 修复：发起 attach 时**立即**登记占位流态，close()/send()/updateComposer 全部可见）：

```js
function attachStream(sessionId, messages) {
  var preInitBuf = [];   // streamResume 之前到达的事件缓冲（理论上 meta 必为首帧，防御性缓冲）
  var initialized = false;
  // 占位流态：phase='attaching' → close() 能 abort 在途 attach（H1）；send() 被 stream 非空拦截、
  // updateComposer 禁用输入与按钮（补「attach 初始化前可发送撞 409」缺口）
  var placeholder = { phase: 'attaching', abort: null, currentStep: null, steps: [], terminalSeen: false, pending: null };
  window.appStore.stream = placeholder;
  updateComposer();
  var handle = window.sse.streamPost('/api/chat/attach', { sessionId: sessionId }, function (event, data) {
    if (!initialized) {
      if (event !== 'streamResume') { preInitBuf.push({ event: event, data: data }); return; }
      if (sessionId !== window.appStore.currentSessionId) return; // H1 守卫：已切走，丢弃迟到初始化
      if (window.appStore.stream !== placeholder) return;         // N1 守卫：已被新 attach 替换
      initAttachedStream(sessionId, messages, data || {});
      initialized = true;
      preInitBuf.forEach(function (item) { onStreamEvent(item.event, item.data); });
      preInitBuf = null;
      return;
    }
    onStreamEvent(event, data);
  });
  placeholder.abort = handle.abort;
  handle.done.then(function () {
    if (!initialized) { resetToHistoryState(false); return; } // 未初始化即结束（极端竞态）
    onStreamClosed();
  }).catch(function (error) {
    if (!initialized) { resetToHistoryState(error.status !== 404, error); return; } // 404 静默；其余提示
    onStreamFailed(error);
  });

  // 历史已在 attach 前渲染（含 pending），兜底只需复位 composer；404=竞态结束属常态，静默
  function resetToHistoryState(withHint, error) {
    if (sessionId !== window.appStore.currentSessionId) return; // 已切走，不污染新视图
    if (window.appStore.stream !== placeholder) return;         // N1：别清掉新 attach 的占位
    window.appStore.stream = null;
    updateComposer();
    if (withHint) showError('流恢复失败（' + error.message + '）；页面为静态历史，流可能仍在后台运行，刷新重试。');
  }
}

function initAttachedStream(sessionId, messages, meta) {
  var baseCount = Math.min(meta.baseCount || 0, messages.length);
  renderHistory(messages.slice(0, baseCount), null); // 截断：本次流已落盘的尾巴由回放重建
  if (meta.userMessage) {
    appendUserMessage(meta.userMessage); // 历史渲染同构（ATTACHMENT_RE 解析）
  } else {
    // confirm 流（userMessage=null）可能中途落盘 queued 用户消息（agent.py:149-151）：
    // baseCount 之后的 user 消息不属于回放事件，需补渲染，否则丢气泡（审核 🟡）。
    // 位置为近似（实际时序在部分工具事件之后），声明为可接受的视觉偏差。
    messages.slice(baseCount).forEach(function (msg) {
      if (msg.kind === 'user') appendUserMessage(msg.content);
    });
  }
  var stream = window.appStore.stream; // 复用占位态，迁移为 streaming
  stream.phase = 'streaming';
  stream.currentStep = null;
  stream.steps = [];
  updateComposer(); // → 停止按钮
}
```

要点：
- `currentStep` 懒建（首个 textDelta/reasoningDelta 经 beginNewStepIfNeeded 建块；工具事件经 liveBodyEl 兜底建块），
  避免「dangling 工具全部归位历史卡片」时留下空 assistant 气泡。
- 乐观 attach 下历史先全量渲染、attach 成功后截断重渲染（流式中会渲染两次，有轻微闪烁，可接受）；
  pending 恢复/确认框逻辑完全沿用现状，与 attach 无耦合。
- `updateComposer` 新增 `phase === 'attaching'` 分支：输入框禁用、按钮禁用（文案「发送」）。
- `chatView.close()` 无需改动：占位态带 abort，切路由即终止在途 attach；attaching 态被 close 后 done/catch 回调
  经 sessionId 守卫 + 占位身份守卫（N1）双重拦截静默退出。

### 5.2 onStreamEvent 空 step 防御（审核纠正版）

attach 首事件可能是 toolCallStart/终态事件（currentStep 为 null）。真正调用 `collapseThinkingIfOpen(stream.currentStep.live)`
的站点是 **toolCallStart / confirmationRequired / completed / error 四处**（chatView.js:635/657/664 等；
textDelta/reasoningDelta 先行 beginNewStepIfNeeded 天然安全；**toolCallEnd 无此调用、经 liveBodyEl 短路，无需改**）。
尤其 `error` case 不能漏：pendingConfirmationExists 直通错误流（agent.py:96-101）的 history 只有一个 errorEvent，
attach 回放时 `stream.currentStep.live` 必抛 TypeError。
统一改法：四处调用前加 `if (stream.currentStep)` 守卫（正常 send 流 currentStep 必存在，行为不变）。

### 5.3 handleStreamError 新增 stopped 分支

```js
if (data.errorType === 'stopped') {
  // 其他窗口点了停止（后端广播的终态）：半截消息加「已中断」标记，静默回空闲，不弹错误条
  markInterrupted();
  goIdle();
  return;
}
```
（按停止的窗口自身处于 stopping 态，onStreamEvent 只记 terminalSeen 后由 onStreamClosed goIdle，不经此分支。）

### 5.4 不变的部分（明确列出不改）

- `chatView.close()`：仍 abort 订阅 + 清 stream（后端泵不受影响，重进靠 attach 恢复）——这是本方案成立的前提。
- `send()` / `confirm()` / `stop()` 主路径：零改动（原流不带 meta；停止走既有 /chat/stop → 广播终态 → 各窗口 onStreamClosed → goIdle）。
- `onStreamClosed` / `onStreamFailed` / `goIdle`：零改动（attach 404 由 attach 自己的 catch 拦截，不进 onStreamFailed 弹条）。

## 6. 边界与异常场景

| # | 场景 | 预期行为 |
|---|---|---|
| E1 | A 流式中切 B 再切回 A（流仍在跑） | 历史先渲染 → attach 成功 → 截断重渲染 + 回放 → 按钮「停止」，内容无丢无重 |
| E2 | 切走期间流已 completed | attach 404 → 全量历史渲染（现状自愈），按钮「发送」 |
| E3 | 切走期间进入 waitingConfirm | 泵已终态 → attach 404 → GET pending 命中 → 既有 enterWaitingConfirm 弹框 |
| E4 | attach 后流中收到 confirmationRequired | 回放事件驱动：pending 卡片 + 弹框 + waitingConfirm 态（复用既有 case） |
| E5 | 两个浏览器窗口同会话 | 各自 attach，同步渲染；任一窗口停止 → 泵广播 stopped 终态 → 另一窗口 handleStreamError stopped 分支：「已中断」标记 + 静默 goIdle（不弹错误条）；另一窗口再点批准旧确认 → confirmationMismatch → 自愈刷新并 reattach |
| E6 | attach 后用户再发消息 | composer 已禁用（流式中）；极端竞态下后端仍 409 兜底（保留，不自动转 attach） |
| E7 | dangling 恢复流中切走再回 | baseCount 精确截断；回放 toolCallStart/End 命中历史卡片注册表归位（复用既有 dangling 重放路径），用户消息由 meta.userMessage 补渲染 |
| E8 | attach 后流刚好终态 | 回放含终态事件 → 既有 completed/error/confirmationRequired 处理 → goIdle/弹框 |
| E9 | attach 404（无活跃流/竞态结束） | 静默保持已渲染的历史态，不弹错误条；attach 即探针，无预判开销 |
| E10 | baseCount > messages.length | 审核结论：坐标系一致（同走 historyView.loadMessages）实际不可能发生；`Math.min` 钳制属无害防御，保留 |
| E11 | attach 在途时快速切走（A→B） | 占位流态被 close() abort；迟到的 streamResume/回调经 sessionId 守卫 + 占位身份守卫（N1）双重拦截，不污染 B 视图 |
| E12 | attach 初始化前断连/服务重启（非 404） | 历史已先渲染（乐观 attach），仅需占位复位；showError 提示「流恢复失败…流可能仍在后台运行」，无空白页 |
| E13 | confirm 流工具执行阶段被 attach | queued 用户消息在工具批执行完才落盘（agent.py driveConfirmation），此时 GET messages 尚无该气泡、meta.userMessage=null → 本窗口暂缺该气泡；流结束后刷新/重进自愈。**已知边界，声明不修** |

## 7. 契约文档更新（webApiSpec.md）

- 新增 `POST /api/chat/attach` 端点定义（404=无活跃流；响应=SSE，首帧 streamResume）。
- §5 状态机修订：路由切走不再等于「前端感觉中断」——重进会话经 attach 续播；「本期不做 SSE 重连」（:499）声明更新为「attach 回放式重连」。

## 8. 实施计划（TODO）

### Phase 1：后端泵广播化 + attach 端点
- [x] T1.1 streamPump 重构：history/subscribers/subLock/closed、_broadcast、subscribe()/unsubscribe()；_pump 改广播与多哨兵；**stop 分支补 stopped 终态广播**
- [x] T1.2 startStream 增加 meta 参数；新增 getActivePump；compactDeltas 回放压缩
- [x] T1.3 sseCodec：encodeResumeFrame + sseGen(eventQueue, meta, pump) 签名调整（finally 反注册）；server 调用点同步
- [x] T1.4 chatStream/chatConfirm 采样 baseCount 并传 meta；新增 POST /api/chat/attach
- [ ] T1.5 验证：curl 起流 → 另起 curl attach → 首帧 streamResume + 压缩回放 + 实时续播；stop 后两路都收到 stopped 终态并正常收尾

### Phase 2：前端 reattach
- [x] T2.1 reloadSession 内接乐观 attach（历史先渲染 + attachStream）；attachStream 含 attaching 占位态 + sessionId 守卫 + N1 占位身份守卫 + 非 404 提示；initAttachedStream 含 confirm 流 queued 用户消息补渲染
- [x] T2.2 onStreamEvent 守卫：toolCallStart/confirmationRequired/completed/error 四处 currentStep 空守卫
- [x] T2.3 updateComposer 增加 attaching 分支；handleStreamError 增加 stopped 静默分支
- [ ] T2.4 验证（目标导向手动清单）：
  - [ ] E1 切走切回续播无丢无重、按钮停止
  - [ ] E2/E3 切走期间结束/待确认的重进行为同现状
  - [ ] E4 attach 后遇确认弹框可批准续跑
  - [ ] E5 双窗口同会话同步渲染 + 跨窗口停止（另一窗口「已中断」静默收尾，无错误条）
  - [ ] E11 快速 A→B 切换无视图错位；E12 attach 失败有提示且历史完整
  - [ ] G3 两会话并行流式互不干扰
  - [ ] confirmationMismatch 自愈路径自动 reattach（N2）
  - [ ] 停止/错误条/已中断标记等既有行为回归

### Phase 3：契约与收尾
- [x] T3.1 webApiSpec.md 更新（attach 端点、§5 状态机措辞）
- [x] T3.2 涉及文件头版本号与 Description 更新（agentManager/sseCodec/server/chatView）

## 9. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 回放重建与历史渲染视觉差异（thinking 折叠态等） | 低 | 回放走与 live 完全相同的渲染路径；completed 后不再重拉历史（与现状 live 流一致） |
| subLock 内 queue.put 阻塞 | 低 | Queue 无界，put 不阻塞；广播开销 O(订阅数)，订阅数 ≤ 窗口数 |
| baseCount 采样与落盘时序 | 低 | 生成器惰性 + startStream 同锁 409，采样点必然先于任何本次流写盘（审核已核实 ✓） |
| 多窗口确认状态分叉（E5 批准竞态） | 中 | 既有 confirmationMismatch 自愈路径覆盖（自愈走 reloadSession 自动 reattach，N2）；文档声明，不做跨窗口弹框同步 |
| attach 在途快速切换错位（H1/N1） | 高→已设计 | attaching 占位流态 + sessionId 守卫 + 占位身份守卫（§5.1） |
| 流式会话重进时历史渲染两次（乐观 attach） | 低 | 先全量后截断重渲染，轻微闪烁；换来 open 关键路径零额外延迟与零空白页风险 |
| 长回合回放重复渲染卡顿 | 低 | subscribe 回放经 compactDeltas 压缩为单帧（§4.1） |
