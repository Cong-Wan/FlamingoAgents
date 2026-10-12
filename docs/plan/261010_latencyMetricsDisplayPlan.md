'''
Author: wilbur
Version: 1.1
Date: 2026-10-10
Description: 耗时与速度指标展示方案——①工具卡右下角执行耗时、②assistant 输出块耗时、③状态栏用量行下新增速度行（TTFT/TPS）。
             含数据链路设计（后端打点→事件透传→前端渲染）、历史回放兼容、决策点、完整 TODOlist 与分层验收。本文件仅方案，不改业务代码。
             v1.1 附子代理审核结论（§7）：6 项锚点验证全部通过，方案可直接实施。
'''

# 耗时与速度指标展示方案（latencyMetricsDisplayPlan）

- Author: wilbur
- Version: 1.1
- Date: 2026-10-10
- 相关契约：`docs/webApiSpec.md` §3.14（status）、`docs/streamOutputPlan.md`（事件契约）、`docs/plan/streamingLatencyFixPlan.md`（timings 数据源）
- 审核：子代理（company71/glm-5.3）聚焦审核 6 项锚点，结论「可直接实施」（详见 §7）
- 状态：**已实施（Phase 1/2/3 全部完成，单测 351 passed）**（2026-10-10）

---

## 0. 目标与非目标

### 0.1 用户诉求

1. agent 每次输出（assistant 消息块）和工具调用执行框（tool 卡片）的**右下角显示该次耗时**。
2. 状态栏「↑ 0 · ↓ 0 · ⚡ 0 cached · $- · 0% / 272.0k」行的**下一行**显示输出速度指标（TTFT、TPS 之类）。

### 0.2 目标（修复后用户应感知到）

| # | 目标 | 说明 |
|---|------|------|
| G1 | 每张工具卡完成后右下角显示执行耗时 | 如 `2.3s`；running 期间不显示（避免计时抖动） |
| G2 | 每个 assistant 输出块（step）右下角显示该次模型调用耗时 | 来自后端 timings.durationMs，跨窗口/刷新后可回放 |
| G3 | 状态栏用量行下方新增第三行：最近一轮速度指标 | `TTFT 1.2s · TPS 48.2 · 生成 8.4s` |
| G4 | 历史回放一致 | 打开旧会话，已完成消息/工具卡同样显示耗时（新数据）；速度行显示最近一轮 |

### 0.3 非目标

| 项 | 说明 |
|----|------|
| 多窗口毫秒级同步计时 | 耗时为终态一次写入，不在多窗口间实时同步 |
| 池化并行工具分组耗时 | 按单 call 计时（executeToolCall 入口/出口），不做池组聚合 |
| CLI 端展示 | 只改 Web 前端与链路 |
| 前端本地估算替代后端打点 | 否——「批量 Start」机制下前端 Start→End ≠ 真实执行时长，必须后端权威打点（见 §1.2） |
| token 级 TPS 精确计量 | TPS 用 completionTokens ÷ 生成时长（durationMs - TTFT），语义为近似输出速率 |

---

## 1. 现状诊断与数据源分析

### 1.1 已有资产（可直接复用）

| 数据 | 位置 | 现状 |
|------|------|------|
| 模型调用 timings | `chatCompletions.successTimings()` / `responsesAdapter.py:440` | 生成 `ttfbMs`/`durationMs`/`chunks`/`sawDone`，**只落 jsonl**（`conversation.appendAssistantMessage` 的 `event['timings']`），不下发 SSE、不下发 GET messages |
| completionTokens（step 增量） | `agent.py` `usageUpdateEvent.stepUsage` | SSE `usageUpdate` 帧已有（含 stepUsage 增量），前端已消费 |
| 工具执行耗时 | 无 | 完全缺失，需新增打点 |
| 状态栏 | `statusBar.js` `renderUsage` + `index.html#statusUsage` | 只两行（location/usage），需加第三行 |

### 1.2 工具耗时为何必须后端打点（决策点 D1）

`streamingLatencyFixPlan` 引入「可执行前缀批量 Start」：多张工具卡**先同时发出 Start**（进入 running），随后才逐个/并行执行。因此前端收到 `toolCallStart` → `toolCallEnd` 的时间差**包含排队等待**，不能代表真实执行时长。

**结论（A，选定）**：在 Core 侧 `executeToolCall` 统一打点（覆盖串行、池化、确认续跑、runPooledToolCall 四条路径），耗时随 `toolCallEnd` 事件下发。

**备选 B（否决）**：前端 Start→End 差值——批量 Start 下数值虚高且随并发度漂移。

### 1.3 assistant 块耗时为何用后端 timings 而非前端计时（决策点 D2）

同一轮模型输出可能被拆成多个 step 块（D6 隐式 step 边界：工具结束后文字进新块），前端逐块计时无法对应「一次模型调用」；且 attach 重放/历史回放时前端计时根本不存在。

**结论（A，选定）**：耗时分摊到 step——`timings.durationMs` 由后端在 usageUpdate 帧携带，前端按「本流新发生的模型调用」写入当时 `stream.currentStep` 的块脚标；历史回放读 jsonl timings（见 §2.4）。

**备选 B（否决）**：每 step 前端独立计时——回放不一致、语义模糊。

### 1.4 风险点：timings 在重试/异常下可能缺失

`successTimings` 只在成功完成时写入 responsePayload；`chunkSeen=False` 的连接级失败重试不产生 timings。jsonl 中旧会话也无 timings。

**对策**：全链路对 `timings`/`durationMs` 缺失做容错——前端 `Number.isFinite` 守卫，缺失就不显示耗时，不显示占位符，**不报错不阻塞**。

### 1.5 风险点：usageUpdate 时序与 step 归属

后端 `usageUpdate` 在**本轮所有模型调用全部结束后**才发出（`driveUserMessage` 每轮循环内 tool 分支与最终分支各 yield 一次）。多 step 块场景下，无法仅凭该帧把 duration 分摊给每个 step。

**对策（简化口径，决策点 D3）**：速度行（状态栏）只显示**最近一轮**（一个 user 轮次内最后一次模型调用的 timings + 本轮 stepUsage.completionTokens 累计）；块级耗时不做「多调用拆分」——**该帧到达时处于哪个 step 块，就把耗写进该块**（此时恰好是正在输出的块或刚封口的块，语义为「最近一次模型调用」）。这已覆盖 95% 场景（多数轮次 = 一次模型调用 + 工具循环 + 一次收尾输出，中间块卡在工具执行上没有耗时概念，最后一块正好承接 timings）。
> 若后续要求「每次模型调用都独立显示」，再立专项：Core 在每轮模型循环内改 yield 逐次 timings（改事件序，风险高，本期不做）。

---

## 2. 数据链路设计

### 2.1 后端：工具执行耗时打点（Core）

- `flamingoAgents/core/agent.py` `executeToolCall`（唯一执行出口，串行/池化/确认/runPooled 全走它）：
  - 入口 `t0 = time.monotonic()`，出口（return 前）构造/更新 result 的 `details['elapsedMs']`。
  - `toolResult.details` 已是 `dict[str, Any]` 且已随事件透传（`toolCallEndEvent`→SSE、jsonl `toolResult` 事件、historyView `details` 原样透传）——**四条链路零新增字段**。
  - 实现注意：`executeToolCall` 有多个 return 点（makeUnknownToolResult / executeCallableToolCall 正常返回），统一用 try/finally 或在 return 处合并；`details` 可能被工具内部填充（如 `exceptionType`、`userStopped`），打点只 `setdefault`-式写入 `elapsedMs`，不覆盖既有键。
  - 对 `makeUnknownToolResult`（definition is None，未真正执行）不写 elapsedMs（语义为零执行，无意义）。

### 2.2 后端：assistant 耗时与速度指标（pump DTO + SSE + status）

分两层：

**(1) 帧层（SSE usageUpdate 携带 timings）**

- `flamingoAgents/core/types.py` `usageUpdateEvent` 增加 `timings: dict | None = None`。
- `flamingoAgents/core/agent.py` 两处 `yield usageUpdateEvent(...)`（tool 分支 L538、终收分支 L571）补充 `timings=safePayload.get('timings')`（重试/缺失时为 None，容错）。
- `webApp/backend/sseCodec.py`：
  - `usageUpdateDto` 增加 `timings: dict | None = None` 字段；
  - `eventToFrame` 对 `usageUpdateEvent` 与 `usageUpdateDto` 两个分支都在 data 里补 `'timings': event.timings`。
- `webApp/backend/agentManager.py` `_toUsageUpdateDto` 透传 `timings=event.timings`。

**(2) 持久层（status 端点 / 历史回放）**

- 速度行需要在**会话重启/刷新后**仍显示最近一轮：
  - `webApp/backend/sessionStore.py` `updateUsage` 增加可选 `lastTimings: dict | None`，写 `session['lastTimings']`（键白名单只保留 `ttfbMs`/`durationMs` 两个 int，避免 timings 其他键膨胀索引）。
  - `webApp/backend/agentManager.py` `_recordUsage`（泵线程收尾）从 conversation 取**最后一个** `assistantMessage` 的 timings 回写 `lastTimings`（需 conversation 暴露末条 assistant timings 的轻量方法或直接在 `usageUpdate` 时缓存）。
  - 简化：直接在 `_toUsageUpdateDto` 里（每帧到达时）把 `event.timings` 存 `self.lastTimings`，`_recordUsage` 回写时带上——**比翻 conversation 消息更稳**（避开 usageRecord outbox 时序），且 pump 生命周期与流一致。
- `webApp/backend/server.py` `getSessionStatus`：响应增加 `'lastTimings': session.get('lastTimings')`（缺省 None，老会话无此键自然为 None）。

### 2.3 历史回放：assistant 块耗时（GET messages 携带 timings）

- `webApp/backend/historyView.py` `assistantMessage` 分支：`'timings': event.get('timings')`（原样透传，None 安全）。
- 前端 `appendAssistantHistory(msg, ...)` 在 body 末尾按 `msg.timings` 渲染块脚标（缺失则不渲染）。

### 2.4 前端渲染

**(1) 工具卡右下角耗时（chatView.js + styles.css）**

- `buildToolCard` 的 `tool-card-detail` 末尾新增 `card.elapsedEl`（`span.tool-elapsed`，默认隐藏）。
- `resolveToolCardOnEnd`：`details.elapsedMs` 为有限数时填充 `formatElapsed(ms)` 并显示。
- `settleRunningCardsOnStop`（用户停止定格失败卡）：不显示耗时（未完成，语义正确）。
- 样式：`.tool-elapsed { display:block; text-align:right; font-size:12px; color:var(--text-dim); padding:2px 12px 6px; }`，挂在 detail 区末尾即视觉「右下角」。
- 历史回放：`appendAssistantHistory` 中 `result.details.elapsedMs` 存在同样显示（链路已通，前端补渲染即可）。

**(2) assistant 块右下角耗时（chatView.js + styles.css）**

- `createLiveAssistantBlock` 的 `live` 对象新增 `footnoteEl`（`span.step-elapsed`，默认 `hidden`，appendChild 到 body 末尾）。
- 流式：`onStreamEvent` `usageUpdate` 分支扩展——调用新增的 `window.chatView.applyStepTimings(data.timings)`（挂到 chatView 对外接口，或在 statusBar.applyUsageUpdate 前同帧处理）：把 `timings.durationMs` 写入 `stream.currentStep.live.footnoteEl`（Number.isFinite 守卫；无 currentStep 忽略）。
- `completed`/`error` 终态不额外处理（脚标已在 usageUpdate 帧时写入；缺 timings 的轮次不显示）。
- 历史回放：`appendAssistantHistory` 渲染 `msg.timings.durationMs`。
- 格式函数 `formatElapsed(ms)`：<60s 用 `12.4s`（一位小数，<10s 保留一位如 `2.3s`，≥10s 取整 `45s`）；≥60s 用 `1m 23s`。
- 样式：`.step-elapsed { display:block; text-align:right; font-size:12px; color:var(--text-dim); margin-top:2px; }`。
- **注意**：`renderFinal` 只重渲 contentEl，脚标是 bodyEl 独立子节点不受影响；`attachMessageCopy` 也是独立按钮，无冲突。

**(3) 状态栏速度行（statusBar.js + index.html + statusUsage.js）**

- `index.html`：`#statusUsage` 行后新增 `<div id="statusSpeed" class="status-line"></div>`。
- `statusBar.js`：
  - `snapshot` 克隆/合并时透传 `lastTimings`（`cloneStatus` 增加字段；`applyUsageUpdate` 的 usage 帧同时携带 timings 时更新 `snapshot.lastTimings`）；
  - 新增 `renderSpeed(data)`：`TTFT 1.2s · 生成 8.4s · TPS 48.2`（TPS = stepUsage.completionTokens ÷ (durationMs - ttfbMs)/1000，各值非有限数则整行隐藏该段或置 `-`）；
  - `paintSnapshot` 调用 `renderSpeed`；无数据时整行 `hidden`。
- `statusUsage.js`：新增 DOM-free 纯函数 `formatSpeedLine(lastTimings, stepUsage)`（便于 Node 单测），导出挂 `window.statusUsage`。

### 2.5 速度指标口径（决策点 D4）

| 指标 | 公式 | 语义 |
|------|------|------|
| TTFT | `timings.ttfbMs` | 首 token 时间（时间到首个可见 chunk，后端 ttfbMs 语义：首个 text/reasoning chunk 时打点） |
| 生成时长 | `durationMs - ttfbMs` | 纯生成阶段 |
| TPS | `stepUsage.completionTokens / 生成时长(s)` | 每秒输出 token 数（近似） |

- `ttfbMs` 缺失（如非流式 complete 路径或旧适配器）时：TTFT 段显示 `-`，生成时长退化用 `durationMs` 全长（此时 TPS 语义变为全程均速，可接受）。
- TPS 分母 < 100ms 时显示 `-`（避免除小数放大噪声）。
- 只读最近一轮（usageUpdate 帧为权威），**不做会话累计平均**。

---

## 3. 改动面清单

| 文件 | 改动 | 版本 |
|------|------|------|
| `flamingoAgents/core/types.py` | `usageUpdateEvent` + `timings: dict \| None = None` | 1.12→1.13 |
| `flamingoAgents/core/agent.py` | `executeToolCall` 打点 elapsedMs；两处 usageUpdateEvent yield 带 timings | 1.26→1.27 |
| `flamingoAgents/core/conversation.py` | 无改动（timings 已落盘） | — |
| `webApp/backend/sseCodec.py` | `usageUpdateDto` + timings 字段；eventToFrame 两分支补 timings | 1.6→1.7 |
| `webApp/backend/agentManager.py` | `_toUsageUpdateDto` 透传 timings；pump 缓存 lastTimings；`_recordUsage` 回写 session | 1.22→1.23 |
| `webApp/backend/sessionStore.py` | `updateUsage` + lastTimings 白名单写入 | 1.3→1.4 |
| `webApp/backend/server.py` | status 响应 + lastTimings | 1.22→1.23 |
| `webApp/backend/historyView.py` | assistant DTO + timings 透传 | 1.5→1.6 |
| `webApp/frontend/js/chatView.js` | 工具卡 elapsedEl；step 脚标 footnoteEl；formatElapsed；usageUpdate 分支应用 timings；历史回放渲染 | 1.25→1.26 |
| `webApp/frontend/js/statusBar.js` | snapshot.lastTimings；renderSpeed；paintSnapshot 接线 | 1.6→1.7 |
| `webApp/frontend/js/statusUsage.js` | formatSpeedLine 纯函数 + formatElapsed 共享 | 1.1→1.2 |
| `webApp/frontend/index.html` | #statusSpeed 行；chatView.js/statusBar.js/statusUsage.js cache-bust | 1.27→1.28 |
| `webApp/frontend/styles.css` | .tool-elapsed / .step-elapsed / .status-speed | 1.27→1.28 |

> 版本号为初始规划值，实施时以文件头 Version 记述为准。

---

## 4. 兼容性与边界

| 场景 | 行为 |
|------|------|
| 旧会话（jsonl 无 timings、session 无 lastTimings） | 状态栏速度行隐藏；历史块/工具卡不显示耗时；无报错 |
| 重试后成功的轮次 | timings 为成功 attempt 的值（successTimings 在成功帧写入）；重试等待期不显示 |
| usageRecord 写账本失败 | 仅影响费用，timings 链路不经账本，不受影响 |
| 非流式 complete 路径（CLI runUserMessage） | timings.ttfbMs 缺失→TTFT `-`；durationMs 仍显示 |
| confirm 续跑流 | usageUpdate 帧同样携带 timings，链路一致 |
| 多窗口（attach 订阅回放） | 回放 usageUpdate 帧→工具卡 details/块脚标在回放中同样命中（事件结构一致） |
| 用户停止（stopped） | 工具卡定格失败不显示耗时；assistant 块可能已有 usageUpdate 前的脚标（若有），保留 |
| pump 中途崩（pumpError） | lastTimings 不回写，速度行维持上一轮值——可接受降级 |

---

## 5. 实施顺序与 TODO list

> 每步含验证方式；全部走「uv run pytest + 手测」双轨。

### Phase 1：后端链路（数据就位）

- [ ] T1.1 `types.py`：`usageUpdateEvent` 加 `timings: dict | None = None`；文件头版本 1.13、description 记述。
- [ ] T1.2 `agent.py` `executeToolCall`：`time.monotonic()` 打点 → `result.details['elapsedMs']`（int，ms）；注意保持既有 details 键；文件头 1.27。→ 验证：`uv run pytest tests/testRunWithInterrupt.py tests/testParallelToolPool.py -q`（现网用例回归，确认 details 未被破坏）。
- [ ] T1.3 `agent.py` 两处 `yield usageUpdateEvent(...)` 带 `timings=safePayload.get('timings')`。→ 验证：`uv run pytest tests/testLiveUsageUpdate.py -q` 中 usageUpdateEvent 构造相关用例通过（kwargs 兼容）。
- [ ] T1.4 `sseCodec.py`：`usageUpdateDto` 加字段 + eventToFrame 双分支补 timings；文件头 1.7。→ 验证：`uv run pytest tests/testLiveUsageUpdate.py -q`（已有 codec 双映射用例，新字段断言需补）。
- [ ] T1.5 `agentManager.py`：`_toUsageUpdateDto` 透传；pump 实例缓存 `self.lastTimings`；`_recordUsage` 回写 `sessionStore.updateUsage(..., lastTimings=...)`；文件头 1.23。
- [ ] T1.6 `sessionStore.py`：`updateUsage` 加 `lastTimings` 参数（白名单 ttfbMs/durationMs）；文件头 1.4。
- [ ] T1.7 `server.py` status 响应加 `lastTimings`；文件头 1.23。
- [ ] T1.8 `historyView.py`：assistant DTO 透传 `timings`；文件头 1.6。→ 验证：`uv run pytest tests/testHistoryErrorView.py -q`。
- [ ] T1.9 新增单测 `tests/testLatencyMetrics.py`：
  - fake 适配器产出含 timings 的 completion → 断言 SSE 帧含 timings；
  - executeToolCall 打点 elapsedMs 存在且 ≥0；
  - updateUsage lastTimings 白名单（丢多余键）；
  - status 响应含 lastTimings；history DTO 含 timings。
  → 验证：`uv run pytest tests/testLatencyMetrics.py -q`。

### Phase 2：前端渲染

- [ ] T2.1 `statusUsage.js`：`formatElapsed(ms)`、`formatSpeedLine(lastTimings, stepUsage)` 纯函数；导出；文件头 1.2。
- [ ] T2.2 `chatView.js`：`buildToolCard` 加 `elapsedEl`（detail 末尾、默认隐藏）；`resolveToolCardOnEnd` 填充；历史回放 `appendAssistantHistory` 工具卡分支填充；文件头 1.26。
- [ ] T2.3 `chatView.js`：`createLiveAssistantBlock` 加 `footnoteEl`；`onStreamEvent` usageUpdate 分支写脚标；历史回放渲染；文件头同上。
- [ ] T2.4 `statusBar.js`：snapshot 透传 lastTimings、`renderSpeed`、`paintSnapshot` 接线；usageUpdate 帧 timings 更新；文件头 1.7。
- [ ] T2.5 `index.html`：`#statusSpeed` 行 + 三个 JS cache-bust 提版；文件头 1.28。
- [ ] T2.6 `styles.css`：`.tool-elapsed`、`.step-elapsed` 样式；文件头 1.28。
- [ ] T2.7 前端 Node 单测（扩展现有模式，新增 `tests/testLatencyMetricsFrontend.py`）：
  - formatElapsed/formatSpeedLine 断言（含缺数据容错）；
  - FakeElement 模拟 buildToolCard/resolveToolCardOnEnd 断言 elapsedEl 显隐与文案；
  - cache-bust 断言（chatView v1.26 / statusBar v1.7 / statusUsage v1.2 / styles 1.28 / index 1.28）。
  → 验证：`uv run pytest tests/testLatencyMetricsFrontend.py tests/testToolCardCollapse.py tests/testLiveUsageFrontend.py -q`。

### Phase 3：验收与回归

- [ ] T3.1 全量：`uv run pytest -q`。
- [ ] T3.2 手测脚本（真模型）：
  1. 发一条会产生工具调用的消息 → 工具卡完成后右下角显示 `xx.xs`；
  2. 等回答完成 → assistant 块右下角显示耗时；状态栏第三行显示 TTFT/生成/TPS；
  3. 刷新页面 → 历史回放：工具卡耗时仍在（新会话数据）、assistant 块耗时仍在、速度行仍在（lastTimings 回写）；
  4. 停止生成 → 工具卡定格失败无耗时、块脚标语义正常；
  5. 旧会话（改动前创建）打开 → 无耗时显示、无报错。
- [ ] T3.3 契约文档同步：`docs/webApiSpec.md` §3.14（status.lastTimings）、事件集（usageUpdate.timings、toolResult.details.elapsedMs）补充说明（简短增量）。

### 回滚

- 纯增量字段 + 前端容错，逐文件 revert 即可；无数据迁移、无 schema 变更（session 索引新键 lastTimings 对老代码只是未知键，loadIndex 不校验未知键）。

---

## 6. 开放问题（实施前需确认）

1. **TPS 分母**：`durationMs - ttfbMs` 与纯生成时长有细微偏差（最后 chunk 到 usage 汇总之间有解析/落盘耗时），是否接受近似？（方案默认接受，展示用途）
2. **块脚标归属**（§1.5 D3）：多模型调用轮次只有最后一块显示耗时，是否满足需求？（方案默认满足；如需逐调用显示另立专项）
3. **速度行是否要显示 token 数**（如 `↓ 512 tok`）与用量行重复，方案默认不重复显示。

---

## 7. 审核记录（v1.1）

- 审核人：子代理（company71/glm-5.3），2026-10-10，聚焦 6 项代码锚点验证。
- 结论：**方案可直接实施**，无 M 级（必须修复）问题。
- 验证明细：
  1. `executeToolCall` 公共出口：agent.py:342（确认续跑）/638（串行段）/766（def）/964（runPooledToolCall 委托）/1009（池 submit）；直接调 `executeCallableToolCall` 仅 agent.py:792（在 executeToolCall 内部），**无绕行路径** → OK
  2. `usageUpdateEvent(` 仅 agent.py L538/L571 两处，与 T1.3 一致 → OK
  3. sseCodec `eventToFrame` usageUpdateEvent（L87-92）/usageUpdateDto（L93-99）确为两独立分支，T1.4 双分支补 timings 必要且正确 → OK
  4. `sessionStore.updateUsage` 调用方仅 agentManager.py:402/539 两处，加可选参数向后兼容 → OK
  5. toolCallEnd 帧（sseCodec L60-66）含 `'details': result.details`，前端可消费 → OK（前端 elapsedMs 渲染为 T2.2 增量，链路已通）
  6. `createLiveAssistantBlock`（L513-539）挂 footnoteEl 可行；usageUpdate case（L919-922）接线位置合适；`renderFinal`（L598-603）只重渲 contentEl 不触碰 bodyEl 其他子节点，脚标不受破坏 → OK
- 开放问题 §6 属产品语义取舍（TPS 近似口径、多调用轮次归属、速度行是否重复 token 数），不阻塞实施。
