# 方案审核报告 — 状态栏逐模型用量：v1.6 + 执行计划 v1.0

- Author: wilbur
- Version: 1.0
- Date: 2026-09-07
- 审核对象：`docs/plan/260902_liveUsageUpdatePlan.md` v1.6 与 `docs/plan/260907_liveUsageExecutionPlan.md` v1.0
- 代码基线：`90d7417`（工作区仅新增执行计划文件）
- 审核范围：E1–E4 修复边界是否可实现且无新问题；原方案关键端到端不变量；不改业务代码、不扩范围
- 联合开工判定：**可以按两份方案联合开工**。冲突处仍以执行计划为准，并把下文 High 当作执行计划的写死约束；不必重写方案 D，也不必扩大 §8 文件范围。

## 总览

- 审核文件：计划 2 份；对照实现 `chatView.js` / `statusBar.js` / `index.html` / `sse.js` / `agentManager.py` / `sseCodec.py` / `agent.py` / `conversation.py` / `types.py` / `sessionStore.py` / `usageStore.py` / `server.py` / `tests/testToolCardCollapse.py`
- 发现问题：🔴 0 个 / 🟠 3 个 / 🟡 2 个 / 🔵 1 个
- 阻塞联合开工：无
- 整体评价：原方案 D 与当前 `90d7417` 兼容；主代理补充的 E1–E4 都是真实竞态/对接缺口，方向正确、可在现有状态机上最小落地。真正的风险不是设计错误，而是实施时仍按原文 §7.5.3/§7.5.4 字面实现，把 E1–E4 做成“加几个 if”而不改提交语义。

## 基线核对

| 项 | 当前基线 | 执行计划 |
|---|---|---|
| commit | `90d7417` | 一致 |
| `uv run pytest -q` | 主代理记录 79 passed | 本次不重跑业务、不改代码 |
| `chatView.js` | 1.19，`?v=1.19` | 从 1.19 起 +0.1 → 1.20 |
| `index.html` | 1.13 | 1.13 → 1.14 |
| `statusBar.js` | 1.3，**无 cache-bust** | 1.3 → 1.4，且必须加 `?v=` |
| `webApiSpec.md` | 1.18（2026-09-07） | 1.18 → 1.19 |
| `agent.py` / `types.py` / `agentManager.py` / `sseCodec.py` | 1.21 / 1.8 / 1.9 / 1.4 | 与原文 §8 续接仍成立 |
| 已上线且须保留 | 工具参数折叠、订阅模型发现 | 不得覆盖；`styles.css?v=1.19` 断言保持 |

原文 §8 的 chatView 1.18→1.19、index 1.12→1.13、webApiSpec 1.17.1→1.18 已过时；实施只按执行计划“当前文件头 +0.1”。

## E1–E4 验证结论

### E1：旧权威 GET 覆盖新 usage — 可实现，必须用修订号而非 connectionId

**原方案漏洞（已坐实）**：`statusBar.refresh()` 今天只比较 `currentSessionId`。原文 §7.5.3 给权威 GET 的守卫是 session/generation/requestId。同会话连续两泵时这三者都可以仍匹配：

1. S1 `completed` → `goIdle()` → `onStreamClosed` 发出权威 GET
2. 同 session 立即 send S2（`open()` 不发生，generation 不增加；若无新 refresh，requestId 也不变）
3. S2 `usageUpdate` 已写入更高累计
4. S1 GET 返回旧 usage/cost，原文“authoritative 可完整替换”会回退

当前 `chatView.send()` 不调用任何 status 重置，上述顺序在基线状态机上可复现。

**执行计划修正足够**：用量修订号（接受有效 usageUpdate/pending 时递增）与“发出后有新用量则降级为单调合并，无新用量才允许权威降 cost”能覆盖该洞，且不会用全程 `Math.max` 禁掉终态价格校准。

**不能用 E3 的 connectionId 代替 usageRevision**。新流已开始但尚无 usage 时，S1 权威 GET 仍应被允许下调价格；connection 代际会把这次校准误杀。E3 只决定旧 closed 还能否碰当前视图；E1 决定**已经发出的 HTTP** 对 usage/context/cost 的提交语义。两者都要。

无新逻辑洞。实施必须把“无 snapshot”分支写死，见 High-1。

### E2：本地 abort 早于 stop 落账 — 可实现，且与现网时序吻合

**原方案漏洞（已坐实）**：

- `chatView.stop()`：先 `stopChat` Promise，再同步 `stream.abort()`，再 `await stopDone`
- `sse.js`：`AbortController.abort()` 后 `done` catch 成 `'aborted'` 并 `then(onStreamClosed)`
- 现网 `onStreamClosed`：**先** `statusBar.refresh()`，再看 phase
- 后端 `requestStop()` 才 `_recordUsage()` 再关订阅

因此本窗口点停止时，权威 GET 必然可以早于 `_recordUsage`。带上本方案的流中 live usage 后，会把状态栏从已显示的精确累计打回上一泵 sessions/usage.db，且再无刷新。

**执行计划修正足够**：stop Promise 挂在本流上；stopping UI 立即收口；该流权威 GET 等 POST 完成尝试后再发；等待后重新验身份；自然 closed 仍 fire-and-forget；失败仍 best-effort GET、不宣称落账成功；禁止 await refresh 再 `goIdle`；不新增轮询。后端 requestStop/finally 原子认领仍走原文 §7.3.2。

**与 7.5.4 的合并点必须写死**，否则会做成“phase===stopping 时才等待”，或在 `goIdle` 后用 `appStore.stream === closedStream` 把延迟 GET 丢掉。见 High-2。

基线里 `phase='stopping'` 会吞后续 `completed`（只记 `terminalSeen`、不 `goIdle`），所以“先 completed 再 stop 把 stream 置空”的主路径不成立；E2 仍要按 `closedStream.stopRequest` 而不是事后 `currentStream.phase` 判断。

### E3：`currentStream=null` 旧连接身份 — 可实现，不需重构状态机

**原方案漏洞（已坐实）**：v1.6 的 null 特例是必要的（`completed`/`error`/`stopped` 先 `goIdle`）。但只检查：

- `closedSessionId === currentSessionId`
- `closedStream.connectionId === closedConnectionId`（旧对象上的 id 不会被新对象改写）
- `currentStream` 为 null 或仍是 closedStream

因此 A→B→A 回到空闲、或同 session S1 结束后又开并结束 S2，迟到的 S1 closed 仍会给当前视图做权威 refresh。`confirm` 复用对象已被 connectionId 覆盖保护；E3 补的是**丢弃后的旧对象 + 当前空闲**。

**执行计划修正足够**：`latestBoundConnection`（至少 sessionId+connectionId）；send/confirm/attach 经 `bindConnection` 更新；close/open/showEmpty 清除；**goIdle 不得清除**（否则当前连接的终态 null 特例自己也被废掉）。event/failed 继续要求 `appStore.stream === 捕获对象` 且 connectionId 匹配。attach 未初始化 reset/404/preInitBuf、send 409 meta、waitingConfirm 早退均保留。

无新状态机分支需求。见 Medium-1。

### E4：资产版本 — 可实现，但“必要时”过弱

当前 `index.html`：`statusBar.js` 无 query；`chatView.js?v=1.19`；`styles.css?v=1.19`。`tests/testToolCardCollapse.py` 对后两处有精确字符串断言。执行计划允许改该测试、要求 `statusUsage → statusBar → chatView`、不弱化无关断言。正确。

本任务会改 `statusBar.js` 并新增 `statusUsage.js`，chatView 将调用 `resetForSession` / `applyUsageUpdate`。只 bump chatView 时，浏览器会留下旧 `statusBar.js`，运行期 `statusBar.applyUsageUpdate is not a function`，SSE 回调中断。见 High-3。

插入位置：现网 `sidebarView.js` 与 `statusBar.js` 相邻，其后才是 slashCommand/fileMention/fileExplorer/`chatView.js?v=1.19`。在 statusBar 前插入 `statusUsage.js` 即可，不必重排无关脚本，也不动 `subscriptionModels.js`（在 chatView 之后，与本次无关）。

## 原方案端到端不变量（对照当前代码）

| 不变量 | 基线事实 | 联合方案后 |
|---|---|---|
| JSONL snake_case / 内存 camelCase | `conversation._accumulateUsage` 读 `prompt_tokens`；`usageTotal` 为 camelCase | 事件只拷贝 `usageTotal`，不透传 raw usage |
| 完整合法 terminal usage 才发事件 | adapter 终态才给 usage；Responses 空 `{}` 会被归一成合法全 0 | 门卫只控 yield，不改 adapter/conversation |
| 每泵至多一条 usageTurns，lastUsage 仅终态 | `_recordUsage` 写 delta + lastUsage；中间无写 | 中间 `updateUsage(..., lastUsage=None)` 已有 API |
| 流中不写 DB | 仅终态 `writeUsageTurn` | 保持；liveCost 用 `event.usage - startUsage` |
| 未知 SSE 事件变 error 终态 | `eventToFrame` 兜底 error | Core yield 与 Core 映射必须同一变更；DTO 后仍留 Core 分支 |
| CLI 忽略新类型 | `runUserMessage` 只认 text/reasoning/terminal | usageUpdate 被跳过且不终止循环 |
| stop/finally 不双写、关闭不先于落账尝试 | 现网裸 `usageRecorded`，requestStop 先记再关 | 改锁内认领、锁外 wait/I/O/set；与 E2 前端等待互补 |
| 终态账单用实际 adapter 模型 | 现网读 sessions 索引，可被流中 `/model` 改写 | 构造期固化 `configProviderId/model`；字段已存在 |
| 构造期无 SQLite/YAML | `__init__` 只快照 `startUsage` | 费用改首个有效 usage 惰性初始化 |
| usageUpdate 非终态、不改 phase | `terminalEventTypes` 仅 completed/confirm/error | 前端 stopping 已忽略非终态；新 case 只 `applyUsageUpdate` |
| confirm 复用对象 | `confirm()` 不 new stream，只改 phase/abort | 必须新 connectionId；E3 同时更新 latestBound |

这些不变量与 E1–E4 无冲突。E1 只改权威 GET 的提交降级；E2 只推迟**本地 stop** 的那一次权威 GET；E3 只收紧 null 特例；E4 只对接缓存。

## 问题清单

### [High] E1 无 snapshot 时不能因 revision 前进而丢弃 GET

**位置**: 执行计划 §2 E1；原方案 §7.5.3 pending/fallback

**问题**: 若把“revision 已前进”实现成整单丢弃 HTTP，则无快照时的 fallback/权威 GET 永远建不成 snapshot，pending 也无法按原协议在 refresh 后应用。首屏用量会卡在空状态，直到另一次 refresh。

**复现**: 切到无缓存会话 → 先到 usageUpdate（pending，revision++）→ 此前发出的 GET 返回。

**最小修正**:

```text
refresh 发出时捕获 startRevision、requestId、generation、sessionId、authoritative。
响应能提交（身份仍匹配）时：
  1. 无 snapshot：先用响应建立 snapshot（location/model/window 照常）。
  2. usage/context/cost：
     - authoritative 且 usageRevision === startRevision：允许完整替换（可降 cost）。
     - 否则：只做与非权威相同的单调合并（旧 usage 不回退；cost 不因旧响应抬回/打回）。
  3. pending：
     - acceptedRevision <= startRevision 且本请求是 authoritative：不得在校准后再把旧 liveCost 抬回。
     - acceptedRevision > startRevision：仍按三字段支配规则合并。
resetForSession 必须把 snapshot/pending/in-flight/usageRevision 一起清掉，并递增 generation。
每次 refresh（含权威）递增 requestId；权威启动要使更早非权威响应失效，避免“cost 仍不减”把已校准价格抬回。
```

**测试**: 可控 deferred Promise：① 旧权威 GET 晚于新 SSE，用量不回退；② 无新事件时权威降低 cost；③ 无 snapshot + 请求前 pending；④ 无 snapshot + 请求后 pending。禁止用全程 `Math.max` 让 ② 失败。

### [High] E2/E3 必须按 closedStream 合并，不能回退到 7.5.4 字面

**位置**: `chatView.js` `stop` / `onStreamClosed` / `goIdle`；原方案 §7.5.4；执行计划 E2、E3

**问题**: 原文在身份检查后立刻 `void refresh`，再用 `currentStream` 做 phase 收口。E2 若挂在 `currentStream.phase === 'stopping'` 上，或延迟 GET 仍要求 `appStore.stream === closedStream`，则：

- `goIdle` 后延迟 GET 被丢，本地 stop 永远不再校准；
- 等待期间切会话/开新流会写到新视图；
- 若误 `await refresh` 再 `goIdle`，停止按钮会重新卡住。

**复现**: 本地 stop → abort 的 `done` 先于 `stopChat` resolve；或在 POST pending 时切到 B / 再 send。

**最小修正**（与现有五条 closed 路径合并，不重写状态机）:

```javascript
function bindConnection(streamState) {
  var connectionId = nextConnectionId++;
  streamState.connectionId = connectionId;
  latestBound = { sessionId: window.appStore.currentSessionId, connectionId: connectionId };
  return connectionId;
}
function isLatestBound(sessionId, connectionId) {
  return latestBound
    && latestBound.sessionId === sessionId
    && latestBound.connectionId === connectionId
    && sessionId === window.appStore.currentSessionId;
}

// stop(): 先挂 Promise，再 abort。UI 已是 stopping。不要 await refresh。
stream.stopRequest = window.api.stopChat(sessionId).catch(function () { return null; });
if (stream.abort) stream.abort();

function onStreamClosed(closedSessionId, closedStream, closedConnectionId) {
  if (closedSessionId !== window.appStore.currentSessionId) return;
  if (closedStream.connectionId !== closedConnectionId) return;
  if (!isLatestBound(closedSessionId, closedConnectionId)) return;

  var currentStream = window.appStore.stream;
  if (currentStream && currentStream !== closedStream) return;

  function refreshIfStillLatest() {
    if (!isLatestBound(closedSessionId, closedConnectionId)) return;
    void window.statusBar.refresh({ authoritative: true });
  }
  if (closedStream.stopRequest) {
    void Promise.resolve(closedStream.stopRequest).finally(refreshIfStillLatest);
  } else {
    refreshIfStillLatest();
  }

  if (!currentStream) { focusComposerIfReady(); return; }
  if (currentStream.phase === 'waitingConfirm') return;
  if (currentStream.phase === 'stopping') { goIdle(); focusComposerIfReady(); return; }
  if (!currentStream.terminalSeen) {
    markInterrupted();
    showError('连接中断：未收到终态事件，刷新页面可恢复最新状态。');
  }
  goIdle();
  focusComposerIfReady();
}
```

约束：`latestBound` 只在 `bindConnection` 更新；`close/open/showEmpty` 清除；`goIdle` 不清除。event/failed 仍用 session+stream 对象+connectionId，不用 latestBound。自然 closed 不碰 `stopRequest`。

**测试**: 行为测试，不能只 grep 源码。① abort/done 早于 POST：resolve 前 0 次权威 GET，resolve 后恰好 1 次；② 等待期间切会话或新流：新视图 0 次；③ POST reject：仍 1 次 best-effort GET；④ 自然 completed：不依赖 stop Promise，仍最终 refresh。

### [High] E4 必须 cache-bust `statusBar.js` 与 `statusUsage.js`

**位置**: 执行计划 §2 E4；`webApp/frontend/index.html` 现 `statusBar.js` 无 query；`tests/testToolCardCollapse.py` L160–161

**问题**: 计划写“必要时”给 statusBar 加版本。本任务必改 statusBar 且 chatView 会调用新方法。只更新 `chatView.js?v=1.20` 时，旧缓存 statusBar 会在 SSE 回调里抛错。新建 `statusUsage.js` 也应带 `?v=1.0`，避免后续热修被缓存。

**复现**: 用户已打开过旧 UI，刷新后 chatView 新、statusBar 旧。

**最小修正**:

```html
<script src="/static/js/statusUsage.js?v=1.0"></script>
<script src="/static/js/statusBar.js?v=1.4"></script>
<!-- 中间脚本不变 -->
<script src="/static/js/chatView.js?v=1.20"></script>
```

`testToolCardCollapse.py` 只把 `chatView.js?v=1.19` 改为 `?v=1.20`；**保留** `styles.css?v=1.19`。新加载顺序与 statusBar/statusUsage 的 query 放在 liveUsage 测试里断言，不要为过测试而改弱折叠用例。

**测试**: index 源码含 `statusUsage.js?v=` 且出现在 `statusBar.js` 之前、`statusBar` 在 `chatView` 之前；折叠测试仍过。

### [Medium] E3 latestBound 的清除点写错会废掉合法终态 refresh

**位置**: 执行计划 E3；`chatView.goIdle` / `open` / `close` / `showEmpty`

**问题**: 若在 `goIdle` 清 latestBound，当前连接 completed 后的 null 特例无法 refresh。若 close/open 不清，A→B→A 仍中招。

**修复**: 单点 `bindConnection` 写入；仅 close/open/showEmpty 清除。验收含：当前连接自然 completed 仍最终 refresh；A→B→A、同 session 两流结束后旧 closed、confirm 复用对象的旧 closed/event/failed 均不碰新视图。

### [Medium] 权威 GET 必须使更早的非权威请求失效

**位置**: 原 §7.5.3“新发的权威 refresh 用 requestId 使更早请求失效”；E1 不得全程 Math.max

**问题**: 非权威“同值则 cost 仍不减”。若权威已按新价格降低 cost，更早发出的非权威 GET 后到且 usage 相同，会把 cost 抬回去。E1 已禁止用 Math.max 堵死降价，但必须明确 requestId 对**所有**更早 refresh 生效，不只是更早的权威 GET。

**修复**: 每次 refresh 递增 requestId；迟到响应三者不匹配则既不提交也不清理新 in-flight。单飞只用于同 generation 的非权威。

### [Low] `statusUsage` 模块名与 DOM id 同名

**位置**: `index.html` `#statusUsage`；计划模块 `window.statusUsage`

**问题**: 浏览器会把 id 暴露为 `window.statusUsage`。helper 脚本会覆盖该引用。现网 `statusBar` 用 `getElementById`，可工作。

**修复**: 不改名、不扩范围。statusBar 继续只走 `getElementById('statusUsage')`，禁止用 `window.statusUsage` 当 DOM。

## 优点记录

- 方案 D 仍是唯一与“每泵一条账单 / 不估 token”相容的桥：数据已在 `appendAssistantMessage` 后出现，只缺事件。
- E1 选修订号而不是禁用权威降价，保住了 yaml 改价后的终态校准。
- E2 把“本地 abort ≠ 后端落账”从现网 `stop()`+`sse.abort` 时序里钉死，且不引入轮询、不阻塞停止 UI。
- E3 只加最近连接身份，不重构 waitingConfirm/stopping/attach 404。
- 执行计划把文件范围锁在原文十个文件 + `testToolCardCollapse.py`，避免顺手改 adapter/schema/usageStore API。

## 修复优先级建议

1. 实施时直接按本报告 High-2 伪代码改 `onStreamClosed`/`stop`/`bindConnection`，不要先落地 7.5.4 再打补丁。
2. statusBar 用 usageRevision + requestId 实现 E1，并覆盖无 snapshot 四条 deferred 测试。
3. index 同步三处 JS cache-bust 与折叠测试的 chatView 断言。

以上三项是实施必遵，不是新需求。原方案 §9/§10 与执行计划 Phase B–D 细目仍全部继承。

## 联合开工判定

**可以开工。** 派实施子代理时明确：

1. 主方案：v1.6；执行补充：v1.0；本报告 High 为写死约束。
2. 工具：read/write/edit/bash；禁止嵌套子代理；不自动 git commit；不碰真实用户会话/费用库。
3. 版本从当前文件头 +0.1，不得按原文 §8 过时数字改。
4. 异步关键路径必须用 deferred Promise 行为测试，不能只静态匹配字符串。
5. 全量 `uv run pytest -q` 不得放宽既有断言。
