# 方案文档审核报告 — `docs/plan/260902_liveUsageUpdatePlan.md`

- Author: wilbur
- Version: 1.0
- Date: 2026-09-04
- 审核对象：模型输出期间状态栏用量更新调研与修复方案（方案 D，状态：待审核）
- 审核方式：全文通读 + 全部代码引用逐条对照 HEAD（2655e4b）实际源码核实

## 总览

- 审核文件：1 份方案文档，涉及 10 个源码文件的引用
- 发现问题：🔴 1 个 / 🟠 2 个 / 🟡 4 个 / 🔵 4 个
- 整体评价：**调研归因准确、方案边界划分正确，可以作为实施基线**；三个延迟点的诊断与代码完全吻合，方案 D"事件通路 + 账单整泵落库"的取舍合理。但存在一个分阶段部署/回滚顺序上的 Critical 缺陷（sseCodec 未知事件兜底会把 usageUpdate 编码成 error 终态帧），以及 DTO 字段来源歧义、文件版本基线错误等需要修订后再实施的问题。

---

## 问题清单

### 🔴 [Critical] C1 — 分阶段实施与回滚顺序会触发 sseCodec 的 error 兜底，前端将流误判为终态错误

**位置**: 方案 §9 Phase 1/Phase 2 划分、§13 回滚步骤 2→3；对应代码 `webApp/backend/sseCodec.py` `eventToFrame()` 末尾兜底、`agentManager.py` `_pump()`

**问题**: `eventToFrame()` 对所有未识别的事件类型兜底为 `error` 帧：

```python
# sseCodec.py 现状
return 'error', {'message': f'未知事件类型：{type(event).__name__}', 'errorType': type(event).__name__}
```

而 `_pump()` 对事件**无类型筛选**地 `_broadcast`。因此只要 Core yield 了 `usageUpdateEvent` 而 sseCodec 尚无映射（以下两个场景必然出现）：

1. **Phase 1 单独合入/部署**（文档把 Core 事件与 sseCodec 映射拆在两个 Phase，且 Phase 1 的测试全部是 Core 级 fake adapter 测试，不经过 sseCodec，**不会暴露此问题**）；
2. **按 §13 回滚步骤 2→3 的顺序执行**：先移除 sseCodec/pump 处理、后移除 Core yield——中间态恰是"Core 在 yield、codec 无映射"。

每个 `usageUpdateEvent` 都会被编码为 `event: error, data: {"message":"未知事件类型：usageUpdateEvent", ...}` 广播给前端。前端 `onStreamEvent` 的 error 分支是**终态处理**：`terminalSeen = true` → `handleStreamError` → 内联错误块"未知事件类型：usageUpdateEvent" → `goIdle()` 置空 stream → **后续所有 textDelta/toolCall 事件被 `if (!stream) return` 静默丢弃**。用户视角：消息体里插入红色错误块、流冻结，而泵实际仍在正常跑。这是灾难级的部署事故。

**修复方案**（三选一，推荐 a）:

- a. **调整阶段划分**：把 T2.7（sseCodec 映射）与 T1.x 捆绑进 Phase 1（映射可先发不含 cost 的最小 DTO），Phase 2 只做 pump 中间更新与 liveCost。并在 §9 明确"Phase 1 不可单独部署"改为"Phase 1+T2.7 原子合入"；
- b. §13 回滚顺序倒置：先摘 Core yield（步骤 3 提前），再移除 Web 层处理；或明确"回滚必须单 commit 整体 revert，禁止按步骤逐个回滚"；
- c. 改 `eventToFrame` 兜底策略为"未知**非终态**事件跳过不编码"——但这会削弱现有"未知终态可见"的防御设计，不推荐。

同时在 §11 风险表补一行：**"分阶段/分步回滚造成未知事件落 error 兜底 → 前端误判终态"（高）**。

---

### 🟠 [High] H1 — §7.2 未指明 `usage` 字段来源与命名转换，实施者可能透传 snake_case 的 provider 原生 usage

**位置**: 方案 §7.2（`usageUpdateEvent` 定义与 DTO 示例）、§7.3 SSE DTO 示例

**问题**: 方案只说"计算该 step delta"，未指明字段来源。代码中存在两套命名：

- `responsePayload['usage']`（adapter 产物）是 **provider 原生 snake_case**：`prompt_tokens` / `completion_tokens` / `prompt_tokens_details.cached_tokens`；
- `conversation.usageTotal` 是 **camelCase**：`promptTokens` / `cachedTokens` / `completionTokens`，且 `lastTurnTokens` 已同步更新。

而 §7.3 的 SSE DTO 示例是 camelCase。若实施者直觉地从刚拿到的 `responsePayload.get('usage')` 取值组装事件，前端将收到 `promptTokens: undefined`，状态栏全 0——错误静默且测试（若只测 Core 层）不一定覆盖。

**修复方案**: 在 §7.2 明确写死数据来源（并加入 T1.x 任务描述）：

```text
event.usage       = dict(conversation.usageTotal)          # camelCase，含历史累计
event.stepUsage   = {k: usageTotal[k] - stepSnapshot[k] for k in usageTotal}
event.contextTokens = conversation.lastTurnTokens           # 与终态 _recordUsage 同源
```

并在 §10.1 Core 测试中断言 `usage` 的 key 集合恰为三个 camelCase 字段。

---

### 🟠 [High] H2 — §8 版本基线与当前文件头不符，按表执行会产生错误版本号

**位置**: 方案 §8"需要修改的文件"表

**问题**: 三个文件的"预计版本"基于过时基线（HEAD 实际文件头）：

| 文件 | 文档写的 | HEAD 实际 | 应为 |
|---|---|---|---|
| `flamingoAgents/core/agent.py` | 1.20 → 1.21 | **v1.21** | 1.21 → 1.22 |
| `webApp/backend/agentManager.py` | 1.7 → 1.8 | **v1.9** | 1.9 → 1.10 |
| `webApp/backend/sseCodec.py` | 1.2 → 1.3 | **v1.4** | 1.4 → 1.5 |

（`statusBar.js` 1.3、`types.py` 1.8、`server.py` 1.15、`chatView.js` 1.18 与实际相符；`webApiSpec.md` 实际为三段式 1.17.1，"+0.1"语义应明确为 → 1.18。）

**修复方案**: 修订 §8 表格基线；或在实施时统一以"当前文件头 +0.1"为准并删除具体数字，避免文档与代码再次脱节。

---

### 🟡 [Medium] M1 — 约半数代码行号引用与 HEAD 不符

**位置**: 方案 §3 全节（调研引用）

**问题**: 核对结果（结论本身均正确，仅定位漂移）：

| 文档引用 | HEAD 实际 |
|---|---|
| `chatCompletions.py:76-80`（include_usage） | 218-219 |
| `chatCompletions.py:170-221`（usage 暂存） | 342-426（consumeSseStream） |
| `chatCompletions.py:302-308`（meta.usage） | 528-529 |
| `responsesAdapter.py:495-502`（terminal usage） | 585-589（terminal 事件）/ 730-733（usage 提取） |
| `agent.py:269-274`（appendAssistantMessage） | 290-295 |
| `agentManager.py:156` / `280-301` | 159 / 313-338 |
| `server.py:260-284` | 233-289 |
| `sessionStore.py:121-149`、`conversation.py:121-130/166-178`、`usageStore.py:71-87/115-130`、`statusBar.js:42-60`、`chatView.js:934-937` | ✅ 相符 |

**修复方案**: 实施文档的行号会随代码演进失效，建议改为**函数名 + 语义定位**（如"`consumeSseStream()` 内 usage 暂存"），仅保留少数关键锚点并注明"行号以 HEAD 2655e4b 为准"。

---

### 🟡 [Medium] M2 — T3.7 前端 Node 测试按现有模板会直接 crash：statusBar.js 顶层依赖 DOM

**位置**: 方案 §10.3、T3.7；参照模板 `tests/testSubscriptionModelsJs.py`

**问题**: `statusBar.js` 是 IIFE，顶层立即执行 `document.getElementById('composerStatus')` 并挂载 `window.statusBar`。`testSubscriptionModelsJs.py` 的模式（Node 直接 `require` 文件）对它会在第一行抛 `ReferenceError: document is not defined`。文档"仅测试环境导出"的说法在无构建步骤的 IIFE 项目中也没有落地手段（JS 无条件编译）。

**修复方案**: 明确其一：

- 推荐：把纯计算（合并快照、↑/↓/⚡ 归一化、百分比 clamp、cost 格式化）拆成独立无 DOM 模块（如 `webApp/frontend/js/statusUsage.js`，挂 `window.statusUsage` + Node 下挂 `global`），statusBar.js 调用它，测试直接 require 新模块——与 `subscriptionModels.js` 的可测结构同构；
- 或：测试脚本内先 stub `global.document = {getElementById: () => null}` 与 `global.window = {}` 再 require——可行但脆弱。

同步修订 §10.3 的"仅测试环境导出"措辞。

---

### 🟡 [Medium] M3 — liveCost 计算的两个边界未定义：价格表缺失降级、模型标识的数据源竞态

**位置**: 方案 §7.3、T2.1/T2.5、§11 风险表

**问题**:

1. **价格缺失降级**：`loadCostMap()` 无该 `providerId/modelId` key 时（yaml 已删模型），`querySessionCost` 的既定语义是按 0 计（`if cost:` 才累加）。§7.3 的 liveCost 计算未写同样的降级，实施者可能直接 `costMap[key]['input']` 触发 KeyError。
2. **模型标识来源**：T2.1 让路由从索引读 provider/model 传入 meta。`chatStream` 中 `requireSession`（读索引①）与 `getAgent`（内部再读索引②）是两次独立读，PATCH /model 落在两次读之间时 meta 与 agent 实际模型不一致（毫秒级窗口）。更可靠的数据源已经存在：`agent.modelAdapter.config` 持有 `provider`/`model`/`configProviderId`。

**修复方案**:

```python
# liveCost 计算（pump 内）
cost = usageStore.loadCostMap().get(f'{providerId}/{modelId}')
deltaCost = usageStore.calcTurnCost(delta['promptTokens'], delta['cachedTokens'],
                                    delta['completionTokens'], cost) if cost else 0.0
```

T2.1 改为"优先从 `agent.modelAdapter.config`（configProviderId/model）固化，路由 meta 兜底"，并在 §7.3 注明 cost 缺失按 0。

---

### 🟡 [Medium] M4 — §7.3 Web DTO 包装方式二选一未定，history 存储类型影响面未交代

**位置**: 方案 §7.3（"可采用一个 Web 层不可持久化的事件包装对象，或让 sseCodec 接收泵已计算好的 DTO"）

**问题**: 两个选项对实现的影响不同：history 里存 core `usageUpdateEvent` 则 cost 需要挂在别处（文档已禁止动态挂属性，只能 pump 维护 `id→cost` 旁路表，attach 回放与 trim 时序复杂）；存 Web DTO 则 `compactDeltas`/`sseCodec`/`_trimHistoryIfNeeded` 都要认识新类型。留给实施时"选代码量更少"的开放式决策，容易做出 history 一致性缺口（如旁路表在 trim 后仍增长）。

**修复方案**: 方案直接定死——pump 识别 `usageUpdateEvent` 后**替换为 Web DTO（含 cost）入 history**，`compactDeltas` 天然不合并（非 delta 类型），`sseCodec` 只认 DTO。删掉二选一表述，避免实施摇摆。

---

### 🔵 [Low] L1 — 中间 `updateUsage` 会反复 touch `updatedAt`

`sessionStore.updateUsage` 无条件刷新 `updatedAt`，流中每个 model step 都会重写 sessions.json 并影响 `listSessions` 排序键。发消息时已 touch 过、会话本就在顶部，实际影响可忽略，但建议在 §7.3 注明该副作用，或给 `updateUsage` 加 `skipTouch` 可选参（若加，需同步契约 §2.1）。

### 🔵 [Low] L2 — 遗漏两个 CLI 事件消费者的兼容性说明

`askModel.py` / `sdkEntry.py` 以 `isinstance` 链消费事件流，新增事件类型对它们天然无影响（未匹配即忽略）。建议 §7.2 补一句说明，避免实施者额外排查。

### 🔵 [Low] L3 — 契约更新范围少列一处

T4.1/T4.2 列了 §4.3/§3.14/状态机，但 `webApiSpec.md` §2.1（session 对象）的 `usage` 字段语义也随"模型 step 中间回写"变化（原表述为"泵线程终态回写"），应一并更新。

### 🔵 [Low] L4 — `usageTurns` 中间写入的既有守卫与 T2.9 断言措辞

`writeUsageTurn` 对全 0 delta 不落行。T2.9"DB 写入次数终态前为 0"的断言应明确用 monkeypatch 计数调用次数而非查行数，避免空转泵流（全 0）造成终态后查行数也为 0 的假通过。

---

## 优点记录

1. **归因完全准确**：三个延迟点（provider 终态才给 usage / `_recordUsage` 只在泵 finally / 前端只在 closed 后 refresh）与 HEAD 代码逐行吻合，§4 时序图可直接作为实施者的心智模型。
2. **语义辨析是文档最大价值**："每个模型调用完成"vs"逐 chunk"的区分（§1/§5）从协议本质（cached 由 provider 判定、tokenizer 不一致）论证了为何不做伪精确统计，避免了错误承诺。
3. **账单边界划分正确**：流中只发事件 + 中间更新 sessions，`usageTurns`/`lastUsage` 保持整泵语义，配合 T2.6/T2.9/T2.10 的幂等与写入次数断言，系统性防住了双计费——这是本类改动最容易翻车的地方。
4. **不改 adapter 的判断正确**：两个 adapter 的 usage 请求/归一化/终态注入均已到位，核实无误。
5. §7.1 不变量清单、§10.4 数据对账公式、§11 风险表质量高，特别是"liveCost 用 `event.usage - startUsage` 而非逐事件 `+=`"直接掐灭了重复累加这一高频 bug。

## 修复优先级建议

1. **C1（分阶段/回滚的 error 兜底）**——决定文档的阶段结构与回滚章节，不修则实施必然踩雷；修订 Phase 1 范围 + 回滚顺序 + 风险表。
2. **H1（usage 字段来源）**——一行文档的事，但不写清会产出前端全 0 的静默错误；连同 T1.x 任务描述与 §10.1 断言一起补。
3. **H2 + M1（版本基线与行号）**——一次性勘误，避免实施时按错误版本号/行号操作。

以上三项修订完成后，本方案可进入实施。
