# 方案文档复审报告 — `docs/plan/260902_liveUsageUpdatePlan.md` v1.1

- Author: wilbur
- Version: 1.0
- Date: 2026-09-04
- 复审对象：v1.1（声称已按 `docs/codeReview/260904_liveUsageUpdatePlanReview.md` 修订）
- 对照：上一轮 11 个问题 C1 / H1 / H2 / M1 / M2 / M3 / M4 / L1 / L2 / L3 / L4
- 复审模型：xaiSubscription / grok-4.6
- 方式：全文通读方案 + 审核报告，并按清单用源码抽查（未改业务代码）
- 源码基线：当前工作区文件头（与方案声称的 `2655e4b` 文件头一致，见抽查记录）

---

## 结论

**需再修订。** 上一轮 11 个问题均已落到正文/任务/风险/回滚，调研归因与方案 D 边界仍然正确；但修订后出现至少三处会误导实施的 High 缺口：Phase 2「sseCodec 改认 DTO / 替代 core 映射」与 §10.2.5「两种输入均编码」互相矛盾（可重新打开 C1），§7.2 快照未写死值拷贝，以及新建 `statusUsage.js` 未列入 `index.html` 的 script 引入。

---

## 11 个旧问题落实情况表

| 问题号 | 状态 | 落点（章节/任务） | 一句话说明 |
|---|---|---|---|
| C1 | 已落实 | §7.1 不变量 7；§9 Phase 1 标题「原子合入」+ T1.5 红线 + T1.9；§11 风险表首行；§13 回滚步骤 3–4 | 四处均写明「Core yield 与 sseCodec 映射必须同一原子变更 / 回滚先摘 yield」，C1 灾难路径描述与源码一致。遗留的 Phase 2「替代映射」是**新问题 N1**，不算 C1 未落实。 |
| H1 | 已落实 | §3.1 口径对照；§7.2 写死 `usageTotal` 三 camelCase 字段；T1.3 / T1.8；§10.1.5；§11 风险表「snake_case 透传」行 | 禁止从 `responsePayload['usage']` 组装；测试要求 key 集合恰为三字段。快照拷贝未写死是**新问题 N2**。 |
| H2 | 已落实 | §8 版本基线表 | 与当前文件头完全一致：types 1.8→1.9、agent 1.21→1.22、agentManager 1.9→1.10、sseCodec 1.4→1.5、statusBar 1.3→1.4、chatView 1.18→1.19、webApiSpec 1.17.1→1.18。 |
| M1 | 已落实 | 文首「引用约定」+ 全文 §3 改为函数名/语义锚点 | 行号已撤；基线 commit 注明 `2655e4b`。抽查定位均可对上函数。 |
| M2 | 已落实 | §7.5；§8 新建 `statusUsage.js`；T3.1 / T3.7；§10.3 | 纯计算拆无 DOM 模块、挂载写法与 `subscriptionModels.js` 同构、明确 `statusBar.js` 不可整体 require。html 入口遗漏是**新问题 N3**。 |
| M3 | 已落实 | §7.3 liveCost 示例 + 价格缺失 `if cost else 0.0`；T2.1 / T2.5；§11 `/model` 行 | 降级对齐 `querySessionCost`；模型标识改为泵内读 `agent.modelAdapter.config` 的 `configProviderId/model`，且比原建议的「路由 meta 兜底」更干净，`server.py` 无需改。 |
| M4 | 已落实 | §7.3「包装方式定死」；T2.2；§7.4 | 定死为 Web DTO dataclass 替换入 history/广播；禁止偷挂属性；不维护旁路表。DTO 模块归属未定则是**新问题 N7**。 |
| L1 | 已落实 | §7.3 中间索引更新「副作用」段；§11 低风险行 | 写明 `updateUsage` 无条件刷新 `updatedAt` 并原子重写 sessions.json；明确不加 `skipTouch`。 |
| L2 | 已落实 | §7.2「CLI 兼容性」段 | `askModel.py` / `sdkEntry.py` 的 isinstance 链未匹配即忽略，源码证实。 |
| L3 | 已落实 | T4.1 / T4.2 / T4.3 | 契约覆盖 §4.3、§3.14、**§2.1**、状态机。当前 §2.1 确有「泵线程每轮结束后回写」表述，需要改。 |
| L4 | 已落实 | §3.4；T2.9；§10.2.4 | 明确 `writeUsageTurn` 用 monkeypatch 计调用次数，不用查行数；源码证实全 0 delta 不落行。 |

---

## 新问题清单

### N1 — High

- **位置**：§7.3 分阶段说明、§7.4、§8 sseCodec 行、T2.7、§10.2.5、§11 C1 行（写「T1.9/T10.2.5」）
- **问题**：Phase 2 后 sseCodec 到底认几种输入，方案自相矛盾。
  - T2.7：「`sseCodec` 改认 Web DTO……Phase 1 的 core 事件直通映射**由 DTO 转换替代**」
  - §7.4 / §8：「Phase 1 认 core；Phase 2 起认 Web DTO」
  - §10.2.5：「Phase 1 core 事件与 Phase 2 Web DTO **两种输入均**编码为 `usageUpdate` 帧、绝不落入 error 兜底」
- **为什么是问题**：`eventToFrame()` 对未知类型兜底为 error 终态帧（源码已证实）。若实施者按 T2.7 删掉 core 分支：
  1. 泵漏转换、测试直接喂 `usageUpdateEvent`、或回滚/分步合入的中间态，会再次触发 C1：前端 `error` → `handleStreamError` → `goIdle()` 置空 `stream` → 后续事件被 `if (!stream) return` 丢弃。
  2. §10.2.5 与 T1.9 将无法同时成立，实施者会改测试去迁就「只认 DTO」，把 C1 回归网拆掉。
- **修复建议**：
  - 把 T2.7 改成「**增加** Web DTO 分支并编码 `cost`，**保留** core `usageUpdateEvent` 分支（无 cost）」；删掉「替代」「改认」字样。
  - §7.3 / §7.4 / §8 改为：「Phase 2 起 sseCodec **同时**认识两种输入；泵负责把 core 事件换成 DTO 再入 history；core 分支作为漏转换的安全网」。
  - §10.2.5 保持「两种输入均编码」，并加一句：喂 dict / 匿名对象必须仍走 error 兜底（防止把兜底改软）。
  - §11 C1 行把「T10.2.5」改成「§10.2 第 5 条」，避免被当成任务号。

### N2 — High

- **位置**：§7.2 代码示例；T1.2
- **问题**：step 快照没有写死「值拷贝」。示例只有 yield 侧：

  ```python
  usageNow = currentConversation.usageTotal
  stepUsage={key: max(0, usageNow[key] - stepStart[key]) for key in (...)}
  ```

  T1.2 只说「每个 model step 调用前快照 `usageTotal`」，没有 `dict(...)` / 三字段 int 拷贝。
- **为什么是问题**：`conversation.usageTotal` 是同一可变 dict。若写成 `stepStart = currentConversation.usageTotal`，`appendAssistantMessage` → `_accumulateUsage` 原地累加后 `stepStart is usageNow`，`stepUsage` 全 0。H1 已把 yield 字段来源写死，却把快照这一半留给直觉。T1.7「stepUsage 分别精确」能抓住，但若测试只断言 `usage` 累计，会静默放行。
- **修复建议**：在 §7.2 示例和 T1.2 写死：

  ```python
  # 外层 while True 开头、内层 for attempt 之外，每个 step 一次值拷贝
  stepStart = {key: int(currentConversation.usageTotal.get(key, 0) or 0)
               for key in ('promptTokens', 'cachedTokens', 'completionTokens')}
  ```

  禁止引用赋值。并与 N10 一并写明循环层级。

### N3 — High

- **位置**：§8 文件表、Phase 3 T3.1–T3.8、§7.5
- **问题**：新建 `webApp/frontend/js/statusUsage.js`，但方案未列出 `webApp/frontend/index.html`，也没有任何任务改 script 引入顺序。
- **为什么是问题**：当前 `index.html` 在 `statusBar.js` 之前没有该文件：

  ```
  sidebarView.js → statusBar.js → … → chatView.js → subscriptionModels.js
  ```

  `statusBar.js` 是顶层 IIFE，立刻碰 DOM；`statusUsage.js` 必须在 `statusBar.js` **之前**加载，否则 `applyUsageUpdate()` 访问 `window.statusUsage` 会抛 `TypeError`。`sse.js` 的 `onEvent` 无 try/catch，异常会打爆 SSE 读取循环，后续 `textDelta/toolCall/completed` 全部停掉——用户视角接近 C1（流冻结），只是触发条件变成「忘了加 script」。T3.8 `node --check` 只做语法检查，抓不到缺标签。
- **修复建议**：
  - §8 表增加 `webApp/frontend/index.html`：在 `statusBar.js` 前插入 `<script src="/static/js/statusUsage.js"></script>`。
  - 新增 T3.1b（或并入 T3.1）：明确顺序 `statusUsage.js` → `statusBar.js` → `chatView.js`。
  - 人工验收加一条：打开页面无 `statusUsage is not defined`。

### N4 — Medium

- **位置**：§7.3 末句 vs §7.5 / T3.1 / T3.3
- **问题**：费用字段合并策略不一致。§7.3：「Phase 1 的直通帧不含 `cost`（前端按缺失**跳过**费用更新即可）」。§7.5 / T3.1：「只覆盖 `usage/cost/contextTokens`」，没说 `cost` 缺省时跳过。
- **为什么是问题**：现有 `renderUsage()` 是 `data.cost > 0 ? '$'+data.cost.toFixed(4) : '$-'`。`undefined > 0` 为 false，**不会 NaN**，但会把 refresh 缓存里已有的 `$0.00xx` 刷成 `$-`。Phase 1 直通、attach 回放缺字段、或 DTO 漏 cost 时，费用行会闪一下再等 `onStreamClosed` 校准。
- **修复建议**：在 §7.5 合并规则和 T3.1/T3.7 写死：`usage/contextTokens` 有则覆盖；`cost` 仅当 `typeof data.cost === 'number'` 才覆盖，否则保留快照；T3.7 加「无 cost 字段不丢原 cost」断言。

### N5 — Medium

- **位置**：§11「attach 重放旧 usage 造成 UI 倒退」；T4.7；T3.x 全缺对应实现任务
- **问题**：风险表仍是二选一（「按字段不小于当前缓存才应用，或以事件原顺序回放」），T3 没有锁定单调合并，T4.7 却要求「重放不倒退到更旧值」。
- **为什么是问题**：第二窗口 `chatView.open` 会 `statusBar.refresh()`，此时 sessions 索引可能已是本泵最新中间值（T2.3）。随后 attach 按 history 从头回放：先 `usageUpdate`（step1 累计）再 step2。无单调守卫就会先显示旧累计再跳回新累计，验收 T4.7 必挂，且两窗口会闪一下倒退。
- **修复建议**：在 T3.3 定死单调策略（推荐：对各 token 字段 `new >= cached` 才应用，否则忽略该帧但仍接受后续更大值；`cost` 同样不减，缺字段走 N4）。T3.7 加「乱序/回放旧帧不降低显示值」。删掉 §11 的二选一。

### N6 — Medium

- **位置**：§7.3 liveCost 示例；T2.4 / T2.5
- **问题**：每次 `usageUpdate` 都 `querySessionCost(sessionId)` + `loadCostMap()`。前者对该 session 全表扫描 `usageTurns` 再逐行套价；后者每次读 yaml。
- **为什么是问题**：本方案 T2.6 保证流中不写 `usageTurns`，单会话同时只有一个泵，所以一次泵流内 `querySessionCost` **结果不变**。多工具长循环（T4.5/T4.6 正是目标场景）会把同一 SQLite 全量查询重复 N 次。功能不错，但和「修改面最小」不符，也没评估。
- **修复建议**：T2.1/`__init__` 固化 `self.dbBaseCost = usageStore.querySessionCost(sessionId)`（可同时缓存 cost 行）。每次 liveCost = `dbBaseCost + deltaCost`。注明：流中改价仍以终态 refresh 为准（与 §7.6 已有声明一致）。

### N7 — Medium

- **位置**：§7.3 Web DTO；T2.2；sseCodec `eventToFrame` 的 `isinstance` 链
- **问题**：Web DTO「小 dataclass（含 cost）」未规定模块归属，也未规定 sseCodec 的 import 路径。
- **为什么是问题**：`eventToFrame` 全是 `isinstance`。DTO 若做成 dict，必落入 error 兜底（C1）。若放在 `agentManager.py`，sseCodec 反向 import 泵模块，边界脏；若放在 `types.py`，Web 价格字段进入 Core（违反 §7.1/T1.1）。实施者会在三个错误位置里选。
- **修复建议**：定死一个 Web 层模块（例如 `webApp/backend/sseCodec.py` 旁的 `webEvents.py`，或直接放 `sseCodec.py`），pump 与 sseCodec 共用。禁止 dict、禁止放进 `flamingoAgents.core.types`。T2.2 写明类名与 import。

### N8 — Medium

- **位置**：§10.4 数据对账
- **问题**：公式写 `sessions.usage = conversation.usageTotal = Σ JSONL assistantMessage.usage`，未提 JSONL 仍是 snake_case 原生 usage。
- **为什么是问题**：`appendAssistantMessage` 把 `responsePayload['usage']` 原样写入 JSONL（`prompt_tokens` / `prompt_tokens_details.cached_tokens`）。直接对 JSONL `usage` 做 camelCase 求和会得到全 0 或对不上。H1 已强调两套口径，§10.4 又把它们写进等号，测试作者容易混用。
- **修复建议**：§10.4 改为：Σ JSONL 时必须经与 `_accumulateUsage` 相同的映射（`prompt_tokens` → `promptTokens` 等）；并加一句「JSONL `assistantMessage.usage` 永不改成 camelCase」。

### N9 — Medium

- **位置**：§7.3 泵固化 provider/model；现有 `_recordUsage`；§12 非目标「不顺手重构」
- **问题**：liveCost 用泵固化的 `configProviderId/model`，终态账单仍走 `sessionStore.getSession` 的 `providerId/modelId`。
- **为什么是问题**：这是**既有行为**，不是本方案发明的。但 PATCH `/sessions/{id}/model` 在活跃流时仍先改索引再 409（「本轮仍跑旧模型」）。于是：
  - liveCost：旧模型价（泵 config，正确反映本轮实际）
  - `writeUsageTurn`：新模型 id（索引已被改）
  - 终态 `$`：按新模型当前价汇总
  流中 `$` 与关闭后 `$` 在「流中切模型」这个已文档化边缘会系统性分叉。方案把模型标识问题提到 M3 高度，却只修了 live 路径，终态仍读索引，实施者会以为竞态已彻底消失。
- **修复建议**：至少在 §7.3/§11 注明该既有分叉，验收不覆盖「流中 /model」。若愿意多改两行：`_recordUsage` 写账单改用泵固化的 provider/model（与 liveCost 同源），并声明这是顺手收口而非范围蔓延。不修代码就必须在非目标里点名。

### N10 — Low

- **位置**：§7.2「每个模型 step 开始前快照」；T1.2「每个 model step 调用前快照」
- **问题**：未写死「外层 `while True` 开头、内层 `for attempt` 之外，每 step 只快照一次」。
- **为什么是问题**：`driveModelLoop` 是外层 step + 内层最多 4 次 attempt。按当前重试语义（`chunkSeen` 则不重试，`appendAssistantMessage` 在 retry 循环之后），快照放进内层**通常**仍能算对 stepUsage，也不会多次 yield。但 T1.2「调用前」可被读成每次 `completeStream` 前。未来若重试策略变了，内层快照会错。这是文档精度问题，不是当前逻辑必炸。
- **修复建议**：T1.2 / §7.2 改成「外层 while 每次迭代开头、retry 循环之外；同一 step 多次 attempt 不得多次快照、不得多次 yield」。

### N11 — Low

- **位置**：T3.4；§7.5 第 3 点；`server.py` `getSessionStatus`
- **问题**：后端 `contextUsedPercent` 是 `round(clamp((contextTokens/contextWindow)*100, 0, 100), 1)`。T3.4 只说「null/0/边界 clamp」，没要求保留 1 位小数、未知窗口为 `null`/`-`。
- **为什么是问题**：前端若 `toFixed` 过多位或不 round，流中百分比与终态 refresh 会对不上（例如 12.34 vs 12.3）。口径分裂虽小，状态栏正好展示一位。
- **修复建议**：T3.4 / T3.7 写死与 `getSessionStatus` 同一公式：`round(max(0, min(100, ctx/window*100)), 1)`；`contextWindow` 缺/0 → 百分比 `null`，展示 `-`。

### N12 — Low

- **位置**：§7.2 典型序列；契约 T4.1 将同步的「典型事件序列」
- **问题**：只写了纯文本与「有工具」两条，没有 `confirmationRequired` 路径。
- **为什么是问题**：实际顺序是 `usageUpdate`（非终态，锁内 yield）→ `driveToolBatch` 可能直接 `confirmationRequired`（终态，锁外 yield）。第一泵会在待确认前先发一条累计 usage，confirm 新泵 `startUsage` 从该累计起步。漏写的话，实施者可能把 usageUpdate 放到 toolCalls 判断之后、或放到 confirm 泵里重复发已入账的 step。
- **修复建议**：§7.2 与 T4.1 补：

  ```
  需确认：textDelta* → usageUpdate → confirmationRequired
         ‖（新泵）toolCallStart → toolCallEnd → textDelta* → usageUpdate → completed
  ```

### N13 — Low

- **位置**：§11 C1 处理列「T1.9/T10.2.5」
- **问题**：任务编号只有 T1.x / T2.x / T3.x / T4.x，「T10.2.5」不是任务号，是 §10.2 第 5 条。
- **为什么是问题**：实施勾选清单时会去找不存在的 T10.2.5。
- **修复建议**：改为「T1.9 与 §10.2 第 5 条」。

---

## 源码抽查记录

以下均为本次用 grep/read 核对的断言，不是记忆。

### 1. `sseCodec.eventToFrame()` 未知事件兜底

- **断言**：未知类型编码为 error 终态帧。
- **证据**：`eventToFrame()` 末尾注释「走到这里属于未知事件，按 error 帧兜底」，`return 'error', {'message': f'未知事件类型：{type(event).__name__}', 'errorType': type(event).__name__}`。
- **现有映射**：`textDeltaEvent` / `reasoningDeltaEvent` / `toolCallStartEvent` / `toolCallEndEvent` / `confirmationRequiredEvent` / `completedEvent` / `retryNoticeEvent` / `errorEvent`。无 `usageUpdate`。
- **结论**：C1 灾难路径在当前代码仍然成立；修订必要性描述准确。

### 2. `agentManager.startStream` / `streamPump`

- **`startStream(sessionId, agentInstance, stream, meta)`**：`pump = streamPump(sessionId, agentInstance, stream, meta=meta)`。把 agent 传给泵。
- **`streamPump.__init__`**：`self.agent = agentInstance`；`self.usageRecorded = False`；`self.startUsage = self._currentUsage()`。
- **`_pump`**：`for event in self.stream:`，仅判断 `stopFlag` 与 `terminalEventTypes`，**无类型筛选**地 `_broadcast(event)`。
- **`_broadcast`**：`doneEvent` 已置位则直接 return；否则 append history 并分发给订阅者。
- **`_recordUsage`**：`usageRecorded` 首行守卫幂等；读 `conversation.usageTotal` 算 delta；`usageStore.writeUsageTurn(...)`；`sessionStore.updateUsage(..., lastUsage=delta)`。账单的 provider/model 来自 `sessionStore.getSession`，不是 adapter.config。
- **`_currentUsage`**：拷贝三 camelCase 字段，缺 conversation 则全 0。
- **`requestStop`**：置 `stopFlag` → interrupt → `_recordUsage` → unregister → `_sealStopped`（写 stopped error、关订阅、置 `doneEvent`）。
- **结论**：泵已持有 agent；「server.py 无需修改」成立。C1「无筛选 broadcast」成立。终态账单仍读索引（N9）。

### 3. `agent.py`

- **`__init__`**：`self.modelAdapter = modelAdapter`。确实有该属性。
- **`driveModelLoop`**：外层 `while True`（`stepIndex` / `maxModelSteps`）+ 内层 `for attempt in range(MODEL_RETRY_MAX_ATTEMPTS + 1)`。`appendAssistantMessage` 在 retry 循环**之后**、`toolCalls` 判断**之前**。无工具则 `yield completedEvent`；有工具则 `driveToolBatch`。
- **无 finalChunk**：`completion is None` 且 interrupt → `return`；否则 `RuntimeError` 进重试/error。`modelInterruptedError` → `return`。重试耗尽 → `yield errorEvent` 后 `return`。这些路径都**不会**调用 `appendAssistantMessage`，因此按方案「append 后才 yield usageUpdate」不会发伪 usage。
- **锁**：`runUserMessageStream` / `continueConfirmationStream` 在 `with getSessionLock` 内迭代；**非终态**（含未来的 usageUpdate）在锁内 yield；终态在锁释放后 yield。
- **结论**：§7.2 插入点与循环结构兼容；快照应在外层 while 开头（N10）。usageUpdate 是非终态，泵处理它时同一线程仍持会话锁。

### 4. `conversation.py`

- **`usageTotal` 初值**：`{'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}`。key 恰为方案所写三个 camelCase。
- **`_accumulateUsage`**：从 snake_case 原生 usage 取值：`prompt_tokens`、`prompt_tokens_details.cached_tokens`、`completion_tokens`，累加到 camelCase；`lastTurnTokens = promptTokens + completionTokens`（该次调用，不是累计）。
- **`appendAssistantMessage`**：JSONL 事件 `'usage': responsePayload.get('usage')`（原生 snake_case），然后 `_accumulateUsage`。
- **结论**：H1 口径正确。§10.4 若直接 Σ JSONL camelCase 会错（N8）。

### 5. `types.py`

- 事件均为小驼峰 dataclass：`textDeltaEvent`、`retryNoticeEvent` 等。
- **`terminalEventTypes = (completedEvent, confirmationRequiredEvent, errorEvent)`**。usageUpdate 不应加入。T1.4 正确。
- 文件头 Version **1.8**。

### 6. `modelConfig.py` / adapter.config

- **`modelConfig` 字段**：`provider`、`model`、`configProviderId`（默认 None）。
- **`__post_init__`**：`configProviderId is None` 时 `= self.provider`。
- **yaml 加载 `loadModelConfigFromYaml`**：`provider=providerId` 且 `configProviderId=providerId`，`model=selectedModelId`。yaml 的 providers key 就是这里的 `providerId`。
- **环境变量回退**：两者都是 `'openaiCompatible'`。
- **`chatCompletionsAdapter.__init__` / `responsesAdapter.__init__`**：`self.config = config`，即 `modelConfig` 实例。
- **`createAgent`（builder）**：adapter 用 `resolved.config` 构造，再 `agent(modelAdapter=adapter, ...)`。
- **结论**：`pump.agent.modelAdapter.config.configProviderId` / `.model` 存在且就是 yaml providerId/modelId 的权威来源。用 `provider` 在当前加载器下碰巧相等，但方案指定 `configProviderId` 正确（与 `responsesAdapter` 诊断字段写法一致）。`configProviderId` 不是 `provider` 的别名概念上的 API 名，而是 yaml key。

### 7. `usageStore.py`

- **`loadCostMap()`**：`costMap[f'{providerId}/{model["id"]}']`。与方案 `f'{self.pumpProviderId}/{self.pumpModelId}'` 一致。
- **`calcTurnCost(promptTokens, cachedTokens, completionTokens, cost: dict)`**：签名与方案示例一致。
- **`querySessionCost`**：`cost = costMap.get(...)`；`if cost: total += calcTurnCost(...)`。无 key / 空 → 该行 0。
- **`writeUsageTurn`**：`if not any(... > 0 for key in tokenKeys): return`。全 0 不落行。T2.9 monkeypatch 计数必要。
- **`tokenKeys = ('promptTokens', 'cachedTokens', 'completionTokens')`**。
- **`normalizeCostForRead`**：缺字段补 0，返回的 dict 恒为真（四个 key）。因此 `if cost` 在「模型仍在 yaml 但 cost 全 0」时仍会调用 `calcTurnCost`（结果 0），与「无 key」走 else 0.0 对外观察相同。方案对齐的是无 key 分支，正确。

### 8. `sessionStore.updateUsage`

- 签名：`updateUsage(sessionId, usage, contextTokens=None, lastUsage=None)`。
- **`if lastUsage is not None:`** 才写 `session['lastUsage']`。传 `None` 不覆盖既有 lastUsage。
- **`session['updatedAt'] = nowIso()`** 无条件，然后 `saveIndex`（临时文件 + rename，见文件头）。
- **结论**：T2.3 / L1 描述正确。

### 9. `subscriptionModels.js` 挂载

- 文件包装：`(function (root) { ... root.subscriptionModels = {...}; })(typeof window !== 'undefined' ? window : globalThis);`
- **结论**：§7.5 / T3.1「与 subscriptionModels.js 同构」可行。`tests/testSubscriptionModelsJs.py` 是 pytest 起 Node `require` 再读 `global.subscriptionModels`。T3.7 模式成立。`statusBar.js` 顶层 `document.getElementById('composerStatus')` 等三处，不可整体 require——M2 判断正确。

### 10. `statusBar.js`

- IIFE，顶层立即取 `#composerStatus` / `#statusLocation` / `#statusUsage`。
- **`renderUsage(data)`**：↑ = `max(0, promptTokens-cachedTokens)`；↓ = completion；⚡ = cached；`$` = `data.cost > 0 ? toFixed(4) : '$-'`；`%` 直接用 `data.contextUsedPercent`。
- **`refresh()`**：`api.getSessionStatus` → `renderLocation` + `renderUsage`。无内部快照。
- **结论**：「不可整体 require」成立。`cost` 缺省不会 NaN（N4/D7）。文件头 Version **1.3**。

### 11. `chatView.js` 流事件

- **`onStreamEvent`**：`if (!stream) return`；`phase === 'stopping'` 时只把 completed/error/confirmationRequired 记 `terminalSeen` 然后 **return**（丢弃包括 usageUpdate 在内的非终态）；`phase !== 'streaming'` 也 return。
- **switch** 无 default，未知事件（含尚未实现的 usageUpdate）被忽略，**不会**当 error。
- **`case 'error'`**：`terminalSeen = true` → `handleStreamError`。非 stopped / 非 confirmationMismatch 的未知 error（含「未知事件类型：usageUpdateEvent」）走内联错误块或 errorBar，然后 **`goIdle()`**。
- **`goIdle()`**：`window.appStore.stream = null`。
- **结论**：C1 后果描述完全准确。stopping 丢 usageUpdate 见 D6。文件头 Version **1.18**。

### 12. `server.py` `chatStream` / `chatConfirm`

- 都是 `requireSession` → `getAgent` → `startStream(sessionId, agentInstance, stream, meta=...)`。meta 只有 `baseCount` / `userMessage`，没有 provider/model。
- **结论**：泵已有 `self.agent`，T2.1 直读 config **不需要改 server.py**。§8 与任何 T 任务均无冲突。

### 13. 文件头版本 vs §8 表

| 文件 | 当前文件头 | 方案基线 | 核对 |
|---|---|---|---|
| `flamingoAgents/core/types.py` | 1.8 | 1.8 → 1.9 | 一致 |
| `flamingoAgents/core/agent.py` | 1.21 | 1.21 → 1.22 | 一致 |
| `webApp/backend/agentManager.py` | 1.9 | 1.9 → 1.10 | 一致 |
| `webApp/backend/sseCodec.py` | 1.4 | 1.4 → 1.5 | 一致 |
| `webApp/frontend/js/statusBar.js` | 1.3 | 1.3 → 1.4 | 一致 |
| `webApp/frontend/js/chatView.js` | 1.18 | 1.18 → 1.19 | 一致 |
| `docs/webApiSpec.md` | 1.17.1 | 1.17.1 → 1.18 | 一致 |
| `webApp/backend/server.py` | 1.15 | 不改 | 一致 |

H2 已正确勘误。

### 14. CLI 消费者

- `askModel.consumeStream` / `sdkEntry.consumeStream`：`isinstance` 链覆盖 reasoning/text/toolStart/toolEnd/confirmation/completed/error，无 else。新类型天然忽略。
- **结论**：L2 正确。

### 15. `docs/webApiSpec.md` 现有表述

- **§2.1**：`usage`「泵线程每轮结束后回写」；`contextTokens`「泵线程终态随 usage 一并回写」；`updatedAt` 含「用量回写时刷新」。T4.2 必须改这里。L3 属实。
- **§3.14**：usage/context/lastUsage「单一数据源为 sessions 索引（泵线程先回写索引后放流结束哨兵）」；cost「泵流进行中落后一轮属预期」。中间回写后前半句要改，cost 终态权威仍对。
- **§4.3**：事件表无 usageUpdate；典型序列无 usageUpdate；`retryNotice` 已是「非终态」先例。
- **§5 状态机**：流式中只有 text/reasoning/tool/completed/error/confirmation/stop。T4.3 要加「usageUpdate 只重绘状态栏、不迁 phase」。
- **结论**：契约范围列全了；当前正文确实需要这四处。

### 16. `compactDeltas`

- 只合并相邻 `textDeltaEvent` / `reasoningDeltaEvent`。其余类型原样保序。
- **结论**：Web DTO 只要不是这两种，天然不合并。方案正确。

### 17. `getSessionStatus` 百分比

- `if contextWindow: usedPercent = round(max(0.0, min(100.0, (contextTokens/contextWindow)*100)), 1)`，否则 `None`。
- **结论**：T3.4 应与此对齐（N11）。

### 18. 前端入口

- `webApp/frontend/index.html` 287 行加载 `statusBar.js`，无 `statusUsage.js`。静态目录有文件也不会自动执行。N3 成立。

### 19. 测试命名

- 现有 `tests/testSubscriptionModelsJs.py`、`tests/testAdapterFactory.py` 等：`test` + 小驼峰。`tests/testLiveUsageUpdate.py` 符合。前端 Node 测试既有独立 py 文件的先例；方案把纯函数断言放进同一文件或仿 `testSubscriptionModelsJs.py` 都可以，不是错误。

---

## 内部一致性检查结果

1. **§8「server.py 无需修改」vs Phase 任务** — **通过**。T1–T4 无 server.py 项；`chatStream`/`chatConfirm` 已把 `agentInstance` 交给泵。
2. **T 任务编号连续性** — **通过**。T1.1–T1.9、T2.1–T2.10、T3.1–T3.8、T4.1–T4.10，无跳号。§11「T10.2.5」不是任务号（N13），不影响勾选表本身。
3. **风险表与正文引用** — **不通过（轻）**。C1/H1/M3/L1 均有正文对应；但 C1 行引用「T10.2.5」编号错误；attach 倒退仍二选一而 T3 无实现任务（N5）；`/model` 行写「不读索引」只覆盖 liveCost，未提 `_recordUsage` 仍读索引（N9）。
4. **纯文本单调用仍「结束才更新」与验收措辞** — **通过**。§1/§5/§11 末行/§14 一致：精确 usage 最早在该次模型终态；方案消除的是多工具整轮延迟。
5. **CLI 消费者兼容说明** — **通过**。与 `askModel.py`/`sdkEntry.py` 源码一致。
6. **compactDeltas 只合并 text/reasoning，Web DTO 天然不合并** — **通过**。
7. **history 存 Web DTO 后，sseCodec 是否还要认识 core** — **不通过**。这就是 N1：§7.3/T2.7 说替代，§10.2.5 说两种都要。attach 回放走 history 里的 DTO，**运行时**可以只靠 DTO 分支；但删 core 分支会失去漏转换安全网，且与测试条文冲突。
8. **前端 applyUsageUpdate 无缓存 fallback vs「不在每个 textDelta 上发 HTTP」** — **通过**。fallback 只在无快照时一次 `refresh()`（T3.5），textDelta 不请求。与方案 A 否决一致。
9. **契约更新范围 §2.1 / §3.14 / §4.3 / 状态机** — **通过**。T4.1–T4.3 四处都列了。
10. **Phase 1「sseCodec 先映射 core（无 cost）」与 Phase 2「改认 DTO」** — **不通过**。见 N1。这是修订引入的主要内部矛盾。
11. **§7.3「缺失跳过 cost」vs §7.5「覆盖 cost」** — **不通过**。见 N4。
12. **M3「泵内固化」与「server.py 无需修改」** — **通过**。二者互相支撑，且优于原审核「路由 meta 兜底」。

---

## D 节潜在点成立性

### D1 — 快照时机应在 while True 每次迭代开头、retry 循环之外

**部分成立。** 结构确为外层 step + 内层 attempt；yield 在 retry 之后，因此**多次 yield** 不会因为快照放错而自动发生。按当前「`chunkSeen` 则不重试、无 finalChunk 不 append」语义，内层快照通常仍能得到正确 stepUsage。但方案没写死层级（N10），也没写值拷贝（N2）。放错成引用赋值的危害远大于放进内层。若未来允许 chunk 后重试，内层快照会把已失败 attempt 的累计切掉。应在 T1.2 写死外层一次值拷贝。

### D2 — usageUpdate 在会话锁内 yield，泵消费时 usageTotal 是否被并发改写

**不成立（作为并发写风险）。** usageUpdate 是非终态，`runUserMessageStream` 在 `with getSessionLock` 内 yield；泵线程就是该生成器的消费者，处理事件时**同一线程仍持会话锁**。方案用事件自带的 `event.usage`（拷贝后的 dict），不需要再读 conversation。即便再读，持锁下也没有其他驱动循环。`requestStop` 的 `_recordUsage` 只拿 `sessionLocksGuard` 不拿会话锁，这是**既有** stop 竞态，不是本方案引入；中间路径用事件快照，不受影响。

附带：中间 `updateUsage` 的磁盘 I/O 会插在两次模型 step 之间并延长持锁时间；同会话此时本就不能开第二路流（409），可接受，方案可不改。

### D3 — requestStop 已 `_recordUsage` 后，泵是否还能 broadcast 后续 usageUpdate

**不成立（作为未防护漏洞）。** 现有机制足够：`requestStop` 置 `stopFlag` 并 `_sealStopped`（`doneEvent`）；`_pump` 下一轮先看 `stopFlag` 就 break；`_broadcast` 见 `doneEvent` 直接 return。若 usageUpdate 在 stop 之后才从生成器出来，不会广播。若中间 `updateUsage` 与终态 `_recordUsage` 交错：后者带 `lastUsage=delta`，前者 `lastUsage=None` 不覆盖；`usageRecorded` 防双写 DB。方案应在 T2.10 旁加一句「stop 后 usageUpdate 不得再 broadcast」，但不是新洞。

### D4 — confirmation 新泵 startUsage 快照是否正确

**不成立（作为缺陷）。** `chatConfirm` 新建 `startStream` → 新 `streamPump.__init__` 再 `_currentUsage()`。上一泵终态已把确认前那个 model step 计入 usageTotal/`usageTurns`。新泵从当前累计起步，后续 `event.usage - startUsage` 只含本 confirm 泵增量。§7.2 宜补确认序列（N12），以免实施者在 confirm 泵重发上一 step。

### D5 — attach 回放：DTO 编码是否畅通；第二窗口是否先看到旧累计

**部分成立。**
- 编码：history 存 Web DTO 且 sseCodec 认 DTO 时，attach 路径畅通。若 Phase 2 删 core 映射且 history 里残留 core 对象，会 C1（N1）。
- UI 倒退：**成立为风险**。open 时 `refresh()` 可能已是最新中间索引；attach 再从头回放 step1 累计会造成数字回退。方案风险表提到了，但 T3 未锁定单调应用（N5）。T4.7 无法仅靠「原序回放」过关，因为 refresh 与回放不是同一时间原点。

### D6 — stopping 相位丢弃 usageUpdate 是否可接受

**不成立（作为必须改的缺陷）。** `onStreamEvent` 在 `stopping` 对非终态直接 return，usageUpdate 会被丢。点停止后前端本就停渲染，`onStreamClosed` 仍 `statusBar.refresh()`，终态校准在。可接受。方案可在 T3.6 加半句「stopping 丢弃 usageUpdate，依赖关闭后 refresh」，避免实施者为此改 phase 逻辑。

### D7 — Phase 1 无 cost 时前端会不会显示 NaN

**部分成立。** 现有 `renderUsage` 用 `data.cost > 0` 守卫，`undefined` 不会走到 `toFixed`，显示 `$-` 而非 NaN。无缓存 fallback 也不会 NaN。真正问题是无条件覆盖 `cost: undefined` 会把已有费用刷成 `$-`（N4）。方案「不能显示 NaN」字面成立，但「缺失跳过费用更新」未落到合并函数。

### D8 — contextUsedPercent 前后端口径

**部分成立。** 后端已 clamp + round 1 位；前端方案只说用缓存 `contextWindow` 重算并 clamp，未要求 round 1 位与 `null` 窗口（N11）。不写死会出现流中 `12.345%`、refresh 后 `12.3%`。公式本身双方都能算，不是不可行，是任务精度不够。

### D9 — 中间 updateUsage 反复写 sessions.json

**不成立（作为否决项）。** 源码确认每次都 `updatedAt` + 原子全量重写。L1 评估（发消息已 touch、会话已在列表顶、不加 skipTouch）仍然成立。多工具长循环会多次 rewrite，单用户规模可接受。与 D6/D2 一并保持现状即可。

### D10 — liveCost 每次 querySessionCost 全量 turns

**成立（作为应修的设计点）。** 流中不写 usageTurns ⇒ 单泵内该查询幂等。每次 usageUpdate 全表扫描 + 重读 yaml 无收益。应在 `__init__` 缓存 `dbBaseCost`（N6）。不是正确性 bug，但是方案自己引入的热路径。长会话 + 多 step 时比 sessions.json rewrite 更值得写进风险表。

### D11 — `_recordUsage` 仍从索引读 provider/model，与泵固化值可能不一致

**成立（既有问题，本方案把它暴露成 live vs 终态分叉）。** PATCH `/model` 在活跃流：先 `updateSessionModel` 写索引，再 `dropAgentIfIdle` 失败 → 409，agent/adapter 仍是旧模型。liveCost 用泵 config（旧，实际在跑的）；`writeUsageTurn` 用索引（新）。这不是本方案引入的账单 bug，但 M3 的措辞会让人以为模型标识竞态已关毕。方案应注明或顺手让 `_recordUsage` 用泵固化值（N9）。

### D12 — JSONL snake_case vs usageUpdate camelCase 是否强调够

**部分成立。** §3.1 / §7.2 / T1.3 / T1.8 / §11 H1 行已经反复强调，**事件路径**够。不够的是 §10.4 对账公式把 JSONL usage 直接放进等号（N8）。实施测试时最容易在这里混口径。

### D13 — 测试文件命名；前端 Node 测试放哪

**不成立（作为问题）。** `tests/testLiveUsageUpdate.py` 与现有 `testXxxYyy.py` 小驼峰一致。前端用 pytest 拉 Node 跑 `statusUsage.js`，与 `tests/testSubscriptionModelsJs.py` 同构。放在同一 py 或单独 `testStatusUsageJs.py` 都行；方案说「同一测试文件覆盖 Core、pump、SSE 与前端纯函数」可行，不是错误。

### D14 — 新建 JS 是否要改 html script 顺序

**成立。** `statusUsage.js` 必须在 `statusBar.js` 之前进入 `index.html`。方案 §8 / Phase 3 完全没提（N3）。这是修订 M2 拆模块后漏掉的入口文件，实施按文件表做会在浏览器直接炸。

---

## 修复优先级

若再出一版 v1.2，建议按这个顺序改文档（仍不改业务代码）：

1. **N1（High）** — 定死 sseCodec **同时**认识 core 事件与 Web DTO，T2.7 改为「增加 DTO 分支」而不是「替代」。这是唯一可能把已关闭的 C1 再打开的修订缺陷。
2. **N2（High）** — §7.2 / T1.2 写死外层 step 一次值拷贝。H1 写死了 yield 字段来源，快照是同一枚硬币的另一面。
3. **N3（High）** — §8 + T3.1 补 `index.html` script 顺序。否则 Phase 3 按表实施不可运行。
4. **N4 + N5（Medium）** — 合并函数：缺 `cost` 跳过；token/cost 单调不减。否则 T4.7 与 Phase 1 无 cost 帧会对着干。
5. **N6 + N7（Medium）** — 缓存 `dbBaseCost`；Web DTO 模块归属写死。都是实施时必问的问题。
6. **N8 + N9（Medium）** — 对账公式加 snake_case 映射；注明终态账单仍读索引（或顺手改用泵固化值）。
7. **N10–N13（Low）** — 快照循环层级、百分比 round 1 位、确认序列、风险表任务号。一并改掉以免实施歧义。

上一轮 11 条本身不必重开。v1.2 把上列 High 三条写死后，即可作为实施基线。
