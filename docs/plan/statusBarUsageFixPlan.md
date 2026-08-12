'''
Author: wilbur
Version: 1.1
Date: 2026-08-08
Description: 状态栏 token/费用统计显示修复方案——对照 pi（pi-coding-agent）实现诊断出三个问题：费用 cached 重复计费（真 bug）、↑↓⚡ 口径与 pi 不一致（单轮增量且 prompt 含 cache 导致重复显示）、% 与 ↑ 口径混排观感矛盾。含决策点、分阶段计划、验收与回滚。本文件仅方案，不改业务代码。v1.1：按 pi 审核报告修订——修两处验收错误（M1/M2）、补 usageView tokensOf 重复计数点（M3）、契约 §3.10/§3.14 文本同步反转（M4/M5）、版本引用与风险补充（S1–S3）。
'''

# 状态栏用量 / 费用统计显示修复方案

- Author: wilbur
- Version: 1.1
- Date: 2026-08-08
- 上游问题来源：用户反馈状态栏「↑ 253.4k · ↓ 48 · ⚡ 252.5k cached · $19.5259 · 25.3% / 500.0k」显示有误
- 对照基准：pi 实现（`pi-coding-agent/dist/modes/interactive/components/footer.js`、`pi-ai/dist/api/openai-completions.js`、`dist/core/compaction/compaction.js`、`dist/core/usage-totals.js`）
- 相关契约：`docs/webApiSpec.md` §3.14（status 端点）、§3.10（usage/series）
- 状态：**方案待实施**（未改业务代码）

---

## 0. 目标与非目标

### 0.1 目标（修复后用户应感知到）

1. **费用真实**：`$` 不再因 cached 重复计费而虚高；用量图表页费用同步修正。
2. **↑↓⚡ 口径对齐 pi**：↑ = 会话累计非缓存输入、↓ = 会话累计输出、⚡ = 会话累计缓存命中；三者互不重叠，↑+⚡ = 总输入。
3. **% 含义清晰**：仍为「当前上下文占用」（最近一次模型调用的 prompt+completion），与 ↑ 的「累计消耗」口径不同属正常，但不再出现「同一批 token 数两遍」的矛盾观感。

### 0.2 非目标（本期不做）

| 项 | 说明 |
|----|------|
| 流式期间 % 实时刷新 | contextTokens 仍泵终态回写；实时估算成本高，遗留 |
| 存储层 usage 归一化改造 | jsonl / usageTotal 保持 OpenAI 原生语义（prompt_tokens 含 cached），只在**显示与计费**层做减法 |
| pi 的 R/W/CH 分项符号 | 保持现有 ↑↓⚡ 符号风格，只对齐语义 |
| cacheWrite 统计 | OpenAI 兼容 provider 基本不返回，沿用现有三键模型 |
| 用量图表的桶结构/时区改动 | 仅修费用公式 |

### 0.3 成功标准（总验收）

- [ ] 含 cached 的会话：手算 `(prompt-cached)*input + cached*cacheRead + completion*output` 与界面 `$` 一致
- [ ] 同一 cached 占比高的会话，修复后 `$` 明显低于修复前（量级符合 cacheRead 折扣价）
- [ ] 状态栏 ↑ = 会话累计输入（非缓存部分），多轮对话后 ↑ 单调增长而非每轮跳变
- [ ] ↑ + ⚡ = 会话总输入 token（与 jsonl 中 assistantMessage 的 prompt_tokens 求和一致；累计口径下允许超过上下文窗口，非矛盾）
- [ ] ↓ 为会话累计输出
- [ ] % 仍在 0–100 之间，流终态后刷新为最新一轮上下文占用
- [ ] 无 cached 的模型：↑ = 会话累计 promptTokens（无重复显示问题）；费用公式退化为全价计费，数值与修复前一致

---

## 1. 问题与根因（诊断结论，含代码证据）

### P1 — 费用 cached 重复计费（真 bug，影响账单）

**证据**：`webApp/backend/usageStore.py` `querySessionCost` 与 `querySeries` 同一公式：

```python
total += (promptTokens * cost['input'] + completionTokens * cost['output']
          + cachedTokens * cost['cacheRead']) / 1_000_000
```

**根因**：OpenAI 语义下 `prompt_tokens` **包含** `cached_tokens`（`conversation.py:_accumulateUsage` 存的 `promptTokens` 是全量；`cachedTokens` 取自 `prompt_tokens_details.cached_tokens`，是其子集）。→ cached 部分被按 `input` 全价 + `cacheRead` 折扣价**收两次**。

**pi 对照**：`pi-ai/openai-completions.js` 归一化 `input = max(0, prompt_tokens - cacheRead - cacheWrite)`，input 与 cache 互斥后分别计价，无重复。

**影响量化**：用户截图 cached 占 prompt 99.6%（252.5k/253.4k），若 cacheRead 价为 input 的 1/n，则费用虚高 ≈ cached × input 全价部分，接近翻倍。`$19.5259` 即虚高结果。usage 图表页（querySeries）同公式同病。

### P2 — ↑↓⚡ 口径与 pi 不一致（同一批 token 显示两次）

**同根因衍生点（审核 M3）**：`webApp/frontend/js/usageView.js` `tokensOf = prompt + cached + completion`——prompt 已含 cached 又加一遍，用量图表的合计/总量把 cached 双计；应改为 `prompt + completion`。

**证据**：`webApp/frontend/js/statusBar.js` v1.2 改读 `lastUsage`（最近一轮增量），且 `promptTokens` 未做归一化减法：

```js
'↑ ' + formatCompact(lastUsage.promptTokens || 0),   // 单轮增量，且含 cached
'↓ ' + formatCompact(lastUsage.completionTokens || 0),
'⚡ ' + formatCompact(lastUsage.cachedTokens || 0) + ' cached',  // 是 ↑ 的子集
```

**根因（两层）**：
1. **单轮增量 vs 会话累计**：pi 的 ↑↓ 是 `addUsageToTotals` 逐条累加的**会话累计**；web 显示单轮 delta → `↓ 48` 这类「全会话只输出 48」的误读。
2. **prompt 含 cache 未归一化**：↑ 已含 ⚡，并排显示 → 用户误读总输入 ≈ ↑+⚡（截图 253.4k+252.5k ≈ 505.9k，超过 500k 窗口，自相矛盾）。pi 中 ↑ 与 R 互斥，相加才是总输入。

### P3 — % 与 ↑ 口径混排（观感矛盾，非数据错）

**证据**：`server.py getSessionStatus`：`usedPercent = contextTokens / contextWindow`，`contextTokens = conversation.lastTurnTokens`（最后一个 model step 的 prompt+completion，泵终态回写）。

**分析**：lastTurnTokens ≈ pi `estimateContextTokens` 的「最近 assistant usage total」部分（pi 另有尾部消息估算，差异微小），**口径本身正确**。问题是与 P2 的单轮求和 ↑ 同排后，用户无法从 ↑ 推出 %（截图 253.4k vs 25.3%×500k=126.5k）。P2 改为会话累计后，「累计消耗」与「当前上下文」本就不同口径，观感矛盾自然消解，**本期不为 % 改任何计算逻辑**。

---

## 2. 决策点（实施前锁定）

### D1. ↑↓⚡ 数据源与口径 — **选定：会话累计 + 前端减法归一化**

| 选项 | 做法 | 取舍 |
|------|------|------|
| **A（选定）** | statusBar 改读 status 响应已有的 `usage`（会话累计）；↑ 显示 `max(0, promptTokens - cachedTokens)` | 零后端改动；存储层语义不动；与 pi 显示语义一致 |
| B | 后端 status 响应新增 `nonCachedPromptTokens` 字段 | 契约膨胀，减法逻辑一处就够，没必要 |
| C | 存储层归一化（_accumulateUsage 存非缓存 prompt） | 动 jsonl/resume/usageTotal 语义，回归面大，违背精准修改 |

**落地含义**：
- ↑ = `max(0, usage.promptTokens - usage.cachedTokens)`（会话累计非缓存输入）
- ↓ = `usage.completionTokens`（会话累计输出）
- ⚡ = `usage.cachedTokens`（会话累计缓存命中）
- `$` 仍为会话累计（数据源不变，仅 P1 公式修正后数值变准）
- % / 窗口 不变

### D2. 费用公式修正口径 — **选定**

```python
# querySessionCost / querySeries 统一改为：
nonCachedPrompt = max(0, promptTokens - cachedTokens)
turnCost = (nonCachedPrompt * cost['input'] + cachedTokens * cost['cacheRead']
            + completionTokens * cost['output']) / 1_000_000
```

- `max(0, ...)` 防御异常数据（provider 不回 cached 明细但回了 cachedTokens>promptTokens 的脏数据），对齐 pi 的 `Math.max(0, ...)`。
- **无需数据迁移**：usageTurns 表列不变（存的仍是原生 prompt/cached/completion），公式修正对历史数据**回溯生效**。

### D3. lastUsage 字段处置 — **选定：契约保留，前端停用**

- status 响应的 `lastUsage` 字段保留（契约 §3.14 不变，避免破坏潜在消费者）；前端状态栏不再使用。
- `usageStore.queryLastUsageTurn` 保留（status 端点 lastUsage 回退路径仍需要）。
- 在 `webApiSpec.md` §3.14 补一句语义说明：`usage` 为会话累计（promptTokens 含 cachedTokens，OpenAI 原生语义）；`lastUsage` 为最近一轮增量。

### D4. % 计算 — **选定：不改**

- 保持 `lastTurnTokens / contextWindow`；流式期间显示上一轮终态值属已知限制，写入非目标。
- pi 的「尾部消息估算」差异微小，不引入。

---

## 3. 分阶段实施计划

```text
Phase 1  费用公式修复（P1）          （0.5d）← 真 bug，优先
Phase 2  状态栏 ↑↓⚡ 口径（P2）+ 契约文档（0.5d）
```

**建议总工期：1 人日**。两个 Phase 互不依赖，可一次提交。

---

## 4. 各 Phase 详细设计

### Phase 1 — 费用公式修复

#### 改动文件

| 文件 | 改动 |
|------|------|
| `webApp/backend/usageStore.py` | `querySessionCost`、`querySeries` 两处 turnCost 公式按 D2 修正（v1.2→1.3） |
| `webApp/frontend/js/usageView.js` | `tokensOf` 去掉重复计入的 cached（v1.1→1.2，审核 M3） |

#### 实现要点

1. 两处公式统一替换为 D2 形式；为免两处漂移，抽一个模块级小函数：

```python
def calcTurnCost(promptTokens: int, cachedTokens: int, completionTokens: int, cost: dict) -> float:
    # OpenAI 语义：promptTokens 含 cachedTokens（子集）；cached 按 cacheRead 折扣价、其余按 input 全价，不得重复计费。
    nonCachedPrompt = max(0, promptTokens - cachedTokens)
    return (nonCachedPrompt * cost['input'] + cachedTokens * cost['cacheRead']
            + completionTokens * cost['output']) / 1_000_000
```

2. `querySessionCost`、`querySeries` 的 `if cost:` 分支改为调用 `calcTurnCost(...)`。
3. `cost` 字典键名沿用 `normalizeCostForRead` 的输出（input/output/cacheRead/cacheWrite），先读代码确认键名一致。
4. 文件头 v1.2→1.3，Description 追加说明。
5. `usageView.js` `tokensOf` 改为 `(entry.promptTokens || 0) + (entry.completionTokens || 0)`（cached 是 prompt 子集，不再单加）；文件头 v1.1→1.2。

#### 验收（目标导向，不用测试框架）

- [ ] 用 `uv run python -c` 对 `webData/usage.db` 现有数据分别套新旧公式各算一遍同一会话费用，确认新值 ≤ 旧值且**差值 ≈ Σ cachedTokens×input/1M**（重复收取的正是 cached 的全价部分；cacheRead 项新旧公式都有、相互抵消）
- [ ] 手算一条已知 turn（如 prompt=1000/cached=600/completion=100，input=2、cacheRead=0.5、output=8 /M）→ 期望 (400×2 + 600×0.5 + 100×8)/1e6 = 0.0019，脚本结果一致
- [ ] 起服务后状态栏 `$` 与手算一致；用量图表页费用列同步变小、token 总量不再双计 cached

---

### Phase 2 — 状态栏 ↑↓⚡ 口径 + 契约文档

#### 改动文件

| 文件 | 改动 |
|------|------|
| `webApp/frontend/js/statusBar.js` | renderUsage 改读 `data.usage`（会话累计）+ ↑ 减法归一化（v1.2→1.3） |
| `docs/webApiSpec.md` | §3.14 补 usage/lastUsage 语义 + §3.10 费用公式文本修正（v1.5.1→1.6） |

#### 实现要点

1. `renderUsage(data)`：
   - `var usage = data.usage || {};`
   - ↑ = `formatCompact(Math.max(0, (usage.promptTokens || 0) - (usage.cachedTokens || 0)))`
   - ↓ = `formatCompact(usage.completionTokens || 0)`
   - ⚡ = `formatCompact(usage.cachedTokens || 0) + ' cached'`
   - `$`、`% / 窗口` 逻辑不动
2. 删除/更新 v1.2 引入的「↑↓⚡ 改读 lastUsage」注释，改为新口径说明。
3. `webApiSpec.md` 两处修订：
   - **§3.10**：费用公式文本改为 `(promptTokens−cachedTokens)×input/1M + cachedTokens×cacheRead/1M + completionTokens×output/1M`，并注明「promptTokens 含 cachedTokens（OpenAI 原生语义），不得重复计费」（审核 M4）；
   - **§3.14**：**显式反转 v1.5 的读取指引**——删改「状态栏 ↑↓⚡ 应读此字段（lastUsage）」为：状态栏自 statusBar v1.3 起 ↑↓⚡ 改读 `usage`（会话累计）+ 前端减法归一化（↑=promptTokens−cachedTokens，对齐 pi footer）；`lastUsage` 字段与读路径保留，状态栏不再使用（审核 M5）。
   - 版本 v1.5.1→1.6，头部变更说明追加一行。
4. 自验：`node --check webApp/frontend/js/statusBar.js`。

#### 验收

- [ ] 多轮会话后 ↑ 单调增长（累计），不再每轮重置
- [ ] ↑ + ⚡ = 总输入（手算 jsonl 里 assistantMessage usage 求和验证）
- [ ] 无 cached 模型的会话：↑ 与修复前累计 prompt 一致
- [ ] 新会话（全 0）：显示 `↑ 0 · ↓ 0 · ⚡ 0 cached · $- · - / -` 不报错

---

## 5. 契约与 API 变更

- **无响应结构变更**：status 端点字段不增删；仅 `webApiSpec.md` §3.14 补语义说明（D3）。
- **数值口径变更（行为修正）**：`cost`（status）与 usage/series 的 `cost` 字段数值会变小（去重复计费）——属 bug 修复，不回填历史展示值（实时按当前价计算，天然回溯正确）。

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| cached > prompt 脏数据导致负数 | ↑ 显示负数 | `max(0, ...)`（前后端同防御，对齐 pi） |
| 用户对「费用变少」困惑 | 误以为数据丢失 | commit message 与契约注明是重复计费修复 |
| normalizeCostForRead 键名假设不符 | 公式取错价 | Phase1 实现要点 3 先读代码确认 |
| 前端仍读 lastUsage 的残留调用 | 口径没改全 | grep `lastUsage` 确认仅 status 回退路径保留 |
| provider 不回 `prompt_tokens_details` 嵌套（如 DeepSeek 原生顶层 `prompt_cache_hit_tokens` 未经网关归一化） | cached 恒 0，费用按全价计（偏高但不重复） | 既有数据模型限制；`max(0,·)` 退化安全；本期不改解析（审核 S3） |

## 7. 完整 TODOlist

### Phase 1 — 费用公式修复（P1）

- [ ] **T1.1** 读 `modelConfigStore.normalizeCostForRead` 确认 cost 键名（input/output/cacheRead/cacheWrite）
- [ ] **T1.2** `usageStore.py` 新增 `calcTurnCost` 模块级函数（D2 公式 + max(0,…) 防御）
- [ ] **T1.3** `querySessionCost` 改用 `calcTurnCost`
- [ ] **T1.4** `querySeries` 改用 `calcTurnCost`
- [ ] **T1.5** 文件头 v1.2→1.3 + Description 追加
- [ ] **T1.6** 手算验收：已知 turn 数值对拍 + usage.db 新旧公式对比脚本
- [ ] **T1.7** `usageView.js` `tokensOf` 去掉重复计入的 cached（v1.1→1.2）+ 图表页目验总量下降

### Phase 2 — 状态栏口径 + 契约（P2）

- [ ] **T2.1** `statusBar.js` renderUsage 改读 `data.usage` + ↑ 减法归一化
- [ ] **T2.2** 更新 statusBar 文件头 v1.2→1.3 + 注释口径说明
- [ ] **T2.3** `node --check` 通过；grep `lastUsage` 确认前端无残留使用
- [ ] **T2.4** `webApiSpec.md` §3.10 公式文本修正 + §3.14 反转 lastUsage 读取指引（v1.5.1→1.6）
- [ ] **T2.5** UI 手测：多轮累计单调增长、↑+⚡=总输入、空会话不报错、$ 与手算一致

## 8. 回滚策略

| Phase | 回滚 |
|-------|------|
| 1 | 还原 usageStore.py 两处公式即可（无数据迁移，无残留） |
| 2 | 还原 statusBar.js renderUsage + 契约说明段落 |

## 9. 变更记录

| 版本 | 日期 | 说明 |
|------|------|------|
| 1.0 | 2026-08-08 | 首版：P1 费用重复计费 / P2 ↑↓⚡ 口径 / P3 观感矛盾的诊断与修复方案，D1–D4 决策，两阶段计划 |
| 1.1 | 2026-08-08 | pi 审核修订：修 §0.3/Phase1 验收两处错误（M1/M2）、补 usageView tokensOf 重复计数（M3，+T1.7）、契约 §3.10 公式与 §3.14 指引同步反转（M4/M5）、版本引用修正（S1）、provider cached 兼容性风险入表（S3） |
