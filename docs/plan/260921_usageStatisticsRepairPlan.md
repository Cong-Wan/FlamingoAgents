# 用量统计统一账本与界面重构方案

- Author: wilbur
- Version: 1.10
- Date: 2026-09-21
- Status: 业务代码已按方案 C 实施；口径冻结为 System-wide / 近30自然日 / 服务端 IANA 时区 / 现存日志准确优先 / USD 调用时估算
- Related research: `docs/260920_usageStatisticsResearch.md`
- Interface options: `docs/260921_usageStatisticsInterfaceOptions.md`
- Scope: 修复用量账本一致性、统一用量页读路径、增加六种时间筛选、按 Provider 纵向展示

## 1. 需求理解与成功标准

### 1.1 已确认意图

1. **用量统计页只能有一个展示数据源。** 顶部 Token/费用、Provider、模型以及选中方案需要的趋势，都必须来自同一份账本、同一时间窗口和同一次接口快照。
2. **顶部四项汇总保留。** 顺序和指标仍为 Prompt Tokens、Cached Tokens、Completion Tokens、总费用；数值全部跟随当前时间筛选。
3. **提供六个固定时间筛选。** 今天、昨天、近7天、近1个月、本月、上月。
4. **按 Provider 展示。** 筛选窗口内有已落账模型调用的 Provider 各一张卡，卡片纵向排列；卡内再按模型展示调用次数与用量。
5. **移除会话明细。** 用量页不再返回或展示会话标题、sessionId、会话当前模型、更新时间，也不再把会话作为统计维度。
6. **先做独立原型再实施。** 用户已选择方案 C；先交付不连接真实数据、不修改正式页面的 HTML 原型，视觉与交互确认后再实施业务代码。

### 1.2 可验证成功标准

- [ ] 用量页打开或切换时间范围时，前端只请求 **一个**统计接口。
- [ ] 该接口在一个 SQLite 只读事务中只读 `usage.db.usageEvents`；不得读取 `sessions.json`、JSONL 或 `models.yaml`。
- [ ] 同一响应中，`顶部汇总 = 所有 Provider 汇总之和 = 所有模型汇总之和`，调用数也同样守恒。
- [ ] 所有聚合严格使用响应返回的同一半开区间 `[startAt, endAt)` 和同一数据库快照。
- [ ] Cached 是 Prompt 子集；总 token 始终为 `promptTokens + completionTokens`，不得把 Cached 重复相加。
- [ ] 每个物理模型请求 attempt 使用一个在请求前创建的稳定 `usageKey`；同一 attempt 的终态处理、重试写库和补账始终复用该键。
- [ ] 冷恢复、切模型、配置保存导致 Agent 重建、终态重入、reconciliation 重跑和 stop 竞态均不能重复落账。
- [ ] 页面和接口均不再包含会话明细。
- [ ] 新记录的价格变化不反向改变历史费用；合法零价与未知价格严格区分。
- [ ] 页面明确声明只包含已落账且 Provider 返回合法 terminal usage 的调用；进行中、无 usage 与待补写记录不冒充完整账单。
- [ ] 相关后端、迁移、并发、边界和前端交互测试全部通过，再执行全量 pytest。

> 可靠性边界：本方案可覆盖“usage outbox 已成功落盘”后的重启、重放和数据库暂时失败；如果 Provider 已计费，但进程在 terminal usage 落入任何本地持久化之前崩溃，或 outbox 与 SQLite 同时永久写失败，本地无法无中生有恢复。统计故障不应把一个成功模型回答改成失败，因此必须显式诊断而不能虚假承诺绝对零漏账。

## 2. 编码前假设、必须确认项与异议

### 2.1 当前假设

- “一个地方读”限定为**用量统计页的读模型**：页面全部统计只读统一 SQLite 账本。会话 JSONL 仍用于会话恢复，并承载账本失败时的持久化 outbox，但页面请求绝不扫描 JSONL。
- Provider/模型按配置 ID 展示；本次不增加可编辑显示名。
- 总费用仍是美元估算，不等同 Provider 发票；新调用按调用时价格快照冻结。
- 调用归桶时间是收到合法 terminal usage 的时刻，不是用户发消息时间，也不是整个工具泵结束时间。
- 界面 C 已由用户确认；默认筛选近7天仍是提案，原型暂以此展示。

### 2.2 决策表（实施前必须冻结）

| 决策 | 选项 | 推荐默认 |
|---|---|---|
| 界面 | A 精确清单 / B 模型占比 / C Provider 趋势 | **C（用户已确认）** |
| 统计范围 | 仅 Web / 整个官方系统（Web、CLI、SDK、askSubAgent、library） | **整个官方系统** |
| 近1个月 | 含今天的近30个自然日 / 当前时刻向前一个日历月 | **近30个自然日** |
| 日历时区 | 服务端系统 IANA 时区 / 指定 IANA 时区 | **服务端系统时区，并在页面显示** |
| 历史策略 | 现存日志准确优先 / 旧 DB 覆盖优先 / 升级时刻清零 | **现存日志准确优先**，详见 §6 |
| 费用 | USD 调用时估算 / 不展示费用 | **USD 调用时估算** |

用户已确认方案 C，但未确认其余五项统计口径；HTML 原型可以使用模拟数据先行，接入真实业务前仍需补齐范围、近1个月、时区、历史和费用决策。

### 2.3 明确异议

只把当前顶部汇总改为读取既有 `usageTurns`，虽然改动最小，但不足以称为修复：该表已确认存在冷恢复重复、stop 漏账、泵结束时间错桶，而且一行可能合并多个模型调用。用户要求按筛选内的“模型调用情况”展示，因此推荐把记录粒度改为**一个取得合法 terminal usage 的物理模型请求 attempt 一行**，而不是继续修补泵级汇总。

## 3. 当前问题与根因

当前用量页有三条独立读路径：

```text
顶部 Prompt/Cached/Completion
  └─ GET /api/usage
      └─ sessions.json（仅现存 Web 会话绝对累计）

图表
  └─ GET /api/usage/series?granularity=...
      └─ usage.db.usageTurns（Web 泵级历史）

顶部总费用
  └─ 再请求 month 全历史 series 并在前端求和
      └─ usage.db + 查询时 models.yaml 当前价格
```

直接后果：

- 时间筛选无法同时作用于顶部和图表；
- 删除会话、进行中的泵、DB/索引写失败会让同页数字互相对不上；
- 费用需要额外请求全历史 month 数据，与当前筛选无关；
- 冷恢复会把历史累计重新写入 `usageTurns`；
- Provider/模型归属与调用时间只有泵级近似；
- CLI、SDK、askSubAgent 不进入当前账本；
- 会话表的“当前模型”无法代表历史用量归属。

## 4. 目标架构

### 4.1 单一展示账本

在 `~/.flamingo/logs/usage.db` 新建 `usageEvents`。每个取得合法 terminal usage 的物理模型请求 attempt 一行；用量页所有数据只查询该表。

建议字段：

```text
usageKey             TEXT PRIMARY KEY   # attempt 创建前生成，终态/补写始终复用
source               TEXT NOT NULL      # web/cli/sdk/subagent/library
sessionId            TEXT NOT NULL
parentSessionId      TEXT NULL          # 子代理关联，可审计，本次 UI 不展示
providerId           TEXT NOT NULL
modelId              TEXT NOT NULL
requestStartedAt     TEXT NOT NULL      # UTC
occurredAt           TEXT NOT NULL      # terminal usage 到达时 UTC
promptTokens         INTEGER NOT NULL
cachedTokens         INTEGER NOT NULL
completionTokens     INTEGER NOT NULL
costNanoUsd          INTEGER NULL       # 调用时价格快照算出的费用；未知为 NULL
pricingStatus        TEXT NOT NULL      # snapshot/migratedCurrentPrice/unknown
recordQuality        TEXT NOT NULL      # exact/legacyInferred/legacyUnverified
createdAt            TEXT NOT NULL
```

约束和实现边界：

- 三个 token 均 `CHECK >= 0`；OpenAI 风格统计要求 `cachedTokens <= promptTokens`，非法记录进入诊断而非正常聚合。
- `PRIMARY KEY(usageKey)` 配合 `INSERT ... ON CONFLICT DO NOTHING` 实现跨线程、跨进程幂等。
- 索引 `(occurredAt, providerId, modelId)`；为会话状态费用保留 `(sessionId, occurredAt)`。
- SQLite 使用 WAL、有限 `busy_timeout`；每个进程维护自己的连接，不能用进程内 Python 锁假装跨进程互斥。
- 费用使用 `Decimal` 按调用时原始价格计算，再存 nano-USD 整数。读取原始配置时必须保留“字段缺失”和“显式为 0”的差异，不能复用会把缺失归零的 `normalizeCostForRead()`。
- 当前没有 cache-write token 计数，费用仍只覆盖非缓存输入、cache-read 和输出；UI/契约需说明该限制。

### 4.2 稳定 attempt 身份与持久化顺序

每次实际调用 adapter 前创建新的随机 `usageKey`；一次本地 retry 的每个 attempt 都有不同键。先把该键随 `modelRequestStart` 写入 JSONL，terminal 处理持有同一个 attempt 上下文，禁止在回调、写库重试或补账时重新生成键。

取得合法 terminal usage 后按以下顺序：

```text
0. 请求前：生成 usageKey，冻结 source/provider/model/价格，记录 modelRequestStart
1. terminal usage 到达：生成 immutable usageRecord（复用 usageKey）
2. 立即把独立 usageRecord 事件追加到会话 JSONL（在后续 assistant 处理之前）
3. 尝试 INSERT usageEvents（主键冲突视为已成功）
4. 写 assistantMessage（引用 usageKey）并继续正常事件流
```

关键语义：

- **每个返回合法 terminal usage 的 attempt 恰记一次**，不把规则写死为“只记最终成功 attempt”；未返回 usage 的失败 attempt 不猜测 token。
- 新格式日志的 `conversation.usageTotal` 从 `usageRecord` 累计；带 `usageKey` 的 assistantMessage 不再重复累计。旧 assistantMessage 没有 usageKey 时继续按旧规则恢复。
- 步骤 2 成功、步骤 3 失败：记录进入待补账状态，稍后按原 usageKey 插入。
- 步骤 3 成功、后续 assistant 处理失败：调用仍已真实发生，账本保留该费用；会话日志问题单独诊断。
- JSONL outbox 写失败时仍尝试直接写 SQLite；两者只有一个成功即可保住记录。两者同时失败则写可观测诊断，但不把成功回答改成模型失败。
- Provider/model 来自该 attempt 的实际 adapter 配置，不从 `sessions.json` 当前选择反推。

### 4.3 Reconciliation、运行期重试与日志删除

- recorder 启动时扫描新格式 `usageRecord`，按原生 usageKey 补缺；多个进程同时扫描也由主键幂等兜底。
- DB 暂时失败后，运行进程使用有界后台重试/退避；后续 recorder 启动和正常关闭再补扫，不能只依赖 Web 服务下一次重启。
- reconciliation 的“插入缺失记录 + 更新该文件读取游标”在同一 SQLite 事务中完成；事务前崩溃可以重复扫描但不能重复入账。
- 坏 JSONL 停在坏行并报告，不静默越过坏行推进游标。
- 应用内删除会话日志前必须先核对并补齐该文件的原生 usageRecord；若数据库不可用或仍有未确认记录，删除接口失败并保留日志，不能删除唯一 outbox。
- 当前项目没有日志轮转；若未来增加，轮转器也必须遵循相同 drain/ack 规则。用户手工删除/截断未补账日志属于明确的不可恢复边界。
- 页面查询不触发 reconciliation，也不读取 outbox；因此一次响应是“查询事务时已入账记录”的一致快照，不是“此前所有 Provider 调用均已完整到齐”的水位。

### 4.4 记录点变化与泵级旧账退出

- 调用级 recorder 位于 Core 合法 terminal usage 路径，不再等整个 Web pump 结束。
- 工具循环中的多个模型 step 各自落一行；`occurredAt` 用各自 terminal 时刻。
- `streamPump._recordUsage()` 不再写 `usageTurns`，只做必要的会话状态收尾；fresh Agent resume 不会把恢复出的历史累计当成本轮新增，stop 也不能抢先把撕裂累计写入账本。
- 新账本切换后，`usageTurns` 保留为旧版本兼容/迁移源，不再作为新页面数据源，也不立即删除。

### 4.5 统计范围接入与状态栏边界

若选择 System-wide：

- `builder.createAgent()` 接受明确 `usageSource` 与可选 `parentSessionId`，并注入公共 recorder。
- Web 传 `web`；交互 CLI 传 `cli`；`sdkEntry` 默认传 `sdk`；`askSubAgent` 子进程显式传 `subagent`；直接库调用默认 `library`。
- 所有官方入口复用同一 recorder，不在各消费者复制 INSERT 逻辑。
- 页面本次只按 Provider/模型展示；`source` 只用于审计和未来能力，不新增筛选控件。

若选择 Web-only：只给 Web Agent 注入 recorder，页面标题/说明必须显式限定为 Web。

“单一展示源”针对用量统计页。聊天输入区状态栏的即时上下文/累计 token 仍可使用会话内存与 `sessions.json`，本任务不重做其 UI；但删除泵级写账后，**状态栏累计费用**必须最小化改为读取/接收 `usageEvents` 的 session 聚合结果，删除现有“DB 基线 + 本泵 delta”的重复叠加。这是账本切换的兼容依赖，不是扩大页面功能。

## 5. 统一接口契约

### 5.1 请求

保留一个路由、替换旧契约：

```text
GET /api/usage?period=today|yesterday|last7Days|last30Days|thisMonth|lastMonth
```

非法值返回 400；缺省 `last7Days`。

### 5.2 时间边界

所有范围都是服务端所选 IANA 时区中的半开区间 `[startAt, endAt)`：

| period | 建议定义 |
|---|---|
| `today` | 本地今天 00:00 → 本次查询 snapshotAt |
| `yesterday` | 本地昨天 00:00 → 本地今天 00:00 |
| `last7Days` | 本地 6 天前 00:00 → snapshotAt（共覆盖 7 个自然日期） |
| `last30Days` | 本地 29 天前 00:00 → snapshotAt（共覆盖 30 个自然日期） |
| `thisMonth` | 本地本月 1 日 00:00 → snapshotAt |
| `lastMonth` | 本地上月 1 日 00:00 → 本月 1 日 00:00 |

边界先按 IANA 时区构造，再转 UTC 查询；不得用固定 `24h × N` 替代自然日。调用按 `occurredAt`（terminal usage 到达时）归桶。

### 5.3 单响应与一致快照

A/B 建议响应：

```json
{
  "period": "last7Days",
  "timeZone": "Asia/Shanghai",
  "startAt": "2026-09-15T00:00:00+08:00",
  "endAt": "2026-09-21T08:55:46+08:00",
  "snapshotAt": "2026-09-21T08:55:46+08:00",
  "totals": {
    "callCount": 128,
    "promptTokens": 1284920,
    "cachedTokens": 972810,
    "completionTokens": 42306,
    "totalTokens": 1327226,
    "costNanoUsd": 8260000000,
    "costStatus": "complete",
    "unpricedCallCount": 0,
    "unpricedTokens": 0
  },
  "providers": [
    {
      "providerId": "subGPT",
      "totals": { "callCount": 89, "promptTokens": 1, "cachedTokens": 0, "completionTokens": 1, "totalTokens": 2, "costNanoUsd": 1, "costStatus": "complete", "unpricedCallCount": 0, "unpricedTokens": 0 },
      "models": [
        { "modelId": "gpt-6-astra", "totals": { "callCount": 89, "promptTokens": 1, "cachedTokens": 0, "completionTokens": 1, "totalTokens": 2, "costNanoUsd": 1, "costStatus": "complete", "unpricedCallCount": 0, "unpricedTokens": 0 } }
      ]
    }
  ],
  "quality": {
    "exactRecords": 120,
    "legacyInferredRecords": 8,
    "legacyUnverifiedRecords": 0,
    "migratedPriceRecords": 8,
    "unknownPriceRecords": 0
  }
}
```

示例中的嵌套 totals 为结构占位，不代表真实加总值。实现约束：

- 服务端先固定 `snapshotAt/startAt/endAt`，开启一个 SQLite 只读事务，再取得按 provider/model（C 方案再加 bucket）分组的单一结果集；Provider、全局 totals、费用覆盖和质量计数全部从该结果集在内存向上汇总。
- 同一请求不做彼此独立的全局 SUM/Provider SUM/模型 SUM，避免并发写入时跨快照。
- Provider 按 `totalTokens DESC, providerId ASC`；模型按 `totalTokens DESC, modelId ASC`。
- Provider 的 `callCount > 0` 即展示，即使合法 usage 三个 token 都是 0。
- 无记录返回四项零值和空 `providers`，文案为“当前区间暂无已落账用量”，不能解读成 Provider 实际没有调用或费用必为 0。
- `costStatus=complete` 只表示区间内价格覆盖完整（包括合法零价），不代表数据质量或 Provider 账单准确；部分未知为 `partial`，全部未知为 `unavailable`。
- 方案 B 的占比可由同一响应中的 model/provider `totalTokens` 计算，不发额外请求。
- 方案 C 的 buckets 也放在该响应，并从同一分组结果集上卷四卡与 Provider totals。
- `snapshotAt` 是数据库查询快照时刻，不是入账完整性 watermark；迟到的 reconciliation 可以在之后回填过去区间。

### 5.4 删除旧读路径

- 前端删除 `getUsageSeries()` 调用。
- `/api/usage/series` 在仓库内无消费者后删除，不保留第二套统计实现。
- `GET /api/usage` 不再读取 `sessionStore.listSessions()`，响应删除 `sessions`。
- 费用不再通过 month 全历史 buckets 在前端二次求和，也不在查询时读取当前模型价格。

## 6. 历史数据迁移

### 6.1 不能承诺无损精确修复

旧 `usageTurns` 已可能含冷恢复重复且一行是泵级聚合；旧 JSONL 有逐调用 usage 和时间，却没有稳定 usageKey，Provider、source、调用时价格也可能不完整。日志“存在”不代表覆盖完整，无法可靠地把部分 JSONL 与旧 pump 行逐条去重。

因此不再设计“有 JSONL 就整 session 替换旧 DB、没 JSONL 才补 DB”的混合算法。原生新格式记录与旧历史分开处理。

### 6.2 原生 usageRecord 永远优先

- 任何带完整原生 `usageRecord` 的日志均复用其中 `usageKey/source/provider/model/occurredAt/costNanoUsd/pricingStatus` 原样补入，绝不另造 legacy key、绝不用迁移时价格覆盖。
- 这条规则覆盖首次迁移失败、回滚后再升级、SQLite 暂时失败和重复 reconciliation。
- 只有完全没有原生 usageKey 的旧 assistant/usageTurns 才进入以下 legacy 策略。

### 6.3 三种互斥 legacy 策略

1. **现存日志准确优先（推荐）**
   - 只从当前仍存在的目标范围 JSONL 导入旧 assistant usage；不再导入同一 legacy 时期的旧 `usageTurns`。
   - deterministic key 使用版本化哈希（日志类别、相对日志路径、行号、原时间戳、模型和规范化 usage），重复执行不重复。
   - 优点：逐模型调用、时间更准确，可绕开已知旧 DB 重复；缺点：已删除/截断日志的历史会缺失。
2. **旧 DB 覆盖优先**
   - 只导入旧 `usageTurns`，key 由版本化 DB 身份与旧行 id 生成；全部标 `legacyUnverified`。
   - 优点：保留已删除会话；缺点：继承已知重复、泵级时间和模型错归风险。
3. **升级时刻清零**
   - 不导入任何 legacy 数据，仅从新 recorder 启用时开始展示；旧表和备份仍保留。

不能同时选择 1 和 2 来追求“既完整又准确”；无法证明的重叠会造成双账。迁移报告必须列出所选策略、扫描/导入/跳过/损坏记录数、token 差异和质量等级，不输出消息正文。

### 6.4 legacy 模型与价格

- Provider/model 只在可确定时推断；模型 ID 在配置中唯一对应一个 Provider时可标 `legacyInferred`，否则归入明确的 `legacyUnknown` Provider，不能套用会话当前模型。
- legacy 费用只能按迁移时原始配置估算并标 `migratedCurrentPrice`；缺少完整价格字段则为 `unknown`。
- API 的 `quality` 必须从当前筛选窗口同一快照返回 legacy/迁移价/未知价计数；A/B/C 都在有问题时显示紧凑提示。

## 7. 界面方案落地边界

详见 `docs/260921_usageStatisticsInterfaceOptions.md`：

- **A 精确清单型**：Provider 卡内模型表格，最适合核账。
- **B 模型占比型（未选）**：每模型“精确数字 + 横向占比条”。
- **C Provider 趋势型（用户已选）**：每张卡内按筛选范围采用小时或天刻度，按模型并排、按 token 类型堆叠展示柱状趋势。

### 7.1 已确认的方案 C 图表语义

- 时间筛选改为标题栏右侧的**单个下拉框**，选项仍是今天、昨天、近7天、近1个月、本月、上月。
- `今天`、`昨天`按小时聚合并以小时为横坐标；`近7天`、`近1个月`、`本月`、`上月`按自然日聚合并以日期为横坐标。`今天`只显示从 00:00 到查询时刻所在小时，不为尚未到来的小时绘制零值；`昨天`显示完整 24 小时。
- 每个时间点是一个分组；该 Provider 在当前筛选范围出现过的每个模型保留一个固定位置并排展示。某时/某天无调用时为零值，不改变模型顺序。
- 每个模型是一根垂直堆叠柱，柱内三段仍为：
  - `输入` = `max(0, promptTokens - cachedTokens)`（非缓存输入）；
  - `缓存` = `cachedTokens`；
  - `输出` = `completionTokens`。
- 输入、缓存、输出使用**同一蓝色系**的三档深浅/饱和度；模型身份不再使用另一套颜色，而由每组内固定左右顺序、模型编号/名称和 tooltip 区分。
- 每张图必须在 Provider 卡片可用宽度内完整展示，不提供横向滚动、滚动同步或“跳到最新”；图表按时间点数和模型数自适应柱宽、间距与标签密度。
- 用户可见文案只使用 `tokens`、`小时`、`天/日期`等通俗表达，不出现“数据桶”“小时桶”等实现术语，也不展示 token 计算公式说明。
- tooltip 至少显示时间、Provider、模型、调用次数、输入、缓存、输出、总 tokens 和费用状态。
- Provider 卡头保留筛选范围汇总和模型图例；卡片纵向单列。

### 7.2 HTML 原型边界

- 文件为 `docs/260921_usageStatisticsOptionCPrototype.html`，只使用内嵌模拟数据和原生 HTML/CSS/JS，可直接本地打开。
- 原型覆盖下拉筛选、顶部四卡、至少两个 Provider、每 Provider 多模型、随筛选自动采用小时/日粒度、自适应整页柱图、hover tooltip、质量提示和窄屏布局；不提供手动粒度控件或粒度说明。
- 原型不得调用后端 API、不得读取或修改真实 `usage.db/sessions.json/JSONL`，也不得修改正式 `webApp/frontend`。
- 原型确认后才把选定视觉落到正式页面；原型代码不直接复制为生产数据逻辑。

三者共同要求：

- 一个时间范围下拉框位于标题栏右侧，四张汇总仍是内容区第一组，指标和顺序不变。
- 四张卡始终存在；合法零费用显示 `$0.0000`，未知显示 `—`。
- Provider 卡只来自当前响应，纵向单列；展示调用数和模型用量，不展示会话。
- 快速切换使用 request generation 或 AbortController，旧响应不得覆盖新选择。
- 切换时四卡与 Provider 区整体加载，不能混合两个时间范围。
- 固定说明“仅含已落账且 Provider 返回 usage 的终态调用；进行中/无 usage 不包含，补账可能回填历史”。
- 当前窗口含 legacy/迁移价/未知价时，显示来自 `quality` 的提示；`costStatus=complete` 不能替代质量提示。
- 无数据统一空态；失败保留筛选并提供重试。
- Provider/model ID 使用 `textContent`；筛选原生下拉框有可访问名称并可通过键盘操作，不使用按钮态 `aria-pressed`。
- 删除会话表、会话链接及相关响应字段。

## 8. 预计代码影响范围

最终以实施前最新工作树复核为准：

| 文件/模块 | 变更 |
|---|---|
| `flamingoAgents/utils/usageLedger.py`（新） | schema、attempt 幂等写入、价格快照、reconciliation、区间聚合 |
| `flamingoAgents/core/agent.py` | attempt 前创建 usageKey；合法 terminal usage 后提交 usageRecord |
| `flamingoAgents/core/conversation.py` | 独立 usageRecord outbox、assistant 引用和新旧累计兼容 |
| `flamingoAgents/core/types.py` | 仅在 recorder 结果需随事件传递时做最小字段调整 |
| `flamingoAgents/builder.py` / 模型配置 | 注入 recorder/source/parentSessionId，并保留原始价格缺失与显式 0 |
| `sdkEntry.py`、CLI 入口、`builtinTools.py` | 仅 System-wide：传明确 source/父子关联 |
| `webApp/backend/agentManager.py` | 删除泵级落账；最小适配状态栏费用 |
| `webApp/backend/usageStore.py` | 删除或收敛为公共 ledger 薄入口，不保留第二套聚合 |
| `webApp/backend/server.py` | `/api/usage?period=...` 单接口；删除 sessions/series 统计路径 |
| `webApp/frontend/index.html` | 六筛选、四汇总、Provider 容器；删除会话表 |
| `webApp/frontend/js/api.js` | 一个 period 请求，删除 series API |
| `webApp/frontend/js/usageView.js` | 统一状态机和所选 A/B/C 渲染 |
| `webApp/frontend/styles.css` | 筛选、Provider 卡、质量提示、空/错/加载和响应式样式 |
| `docs/webApiSpec.md` | 新接口、时间/费用/覆盖/质量语义，删除双口径说明 |
| tests | 账本、迁移、API、Core、并发和前端回归测试 |

所有新代码文件使用小驼峰文件名并带标准文件头；修改既有代码文件提升小版本号，在 Description 写明本次改动。不顺手重构无关模块。

## 9. 详细 TODO List

### Phase 0 — 决策冻结与基线

- [x] **P0.1** 用户确认界面 C。
- [x] **P0.2** 已完成第一版独立 HTML 原型并取得用户逐项反馈。
- [x] **P0.3** 已按第二轮口径修订原型：下拉筛选；今天/昨天按小时，其他范围按天；完整宽度展示；同色系三段柱；删除专业说明与滚动控件。
- [ ] **P0.4** 用户确认修订后的 HTML 原型视觉与交互。
- [ ] **P0.5** 用户确认 Web-only/System-wide、近1个月定义、日历时区、legacy 策略和费用语义。
- [ ] **P0.6** 重新读取最新工作树和未提交改动，记录 Git 基线，不覆盖无关修改。
- [ ] **P0.7** 运行现有 `uv run pytest -q`，取得修改前基线。
- [ ] **P0.8** 冻结 API 示例、schema、覆盖声明和界面验收线框。

原型验证：下拉框六项均可切换；今天/昨天每小时一组，其余范围每天一组；每组模型柱并排且每柱有同色系输入/缓存/输出三段；桌面与窄屏均无横向滚动；页面不出现“数据桶/小时桶”、公式说明、滚动同步或跳转控件；无会话详情、无真实数据请求。

实施前验证：§2.2 除已确认界面外的每项均有书面决策，基线测试可复现。

### Phase 1 — 先写失败测试，建立调用级账本

- [ ] **P1.1** 新增 schema/version、约束、索引、WAL/busy_timeout 测试。
- [ ] **P1.2** 在每个物理 attempt 前生成并贯穿 usageKey；测试 retry attempt 键不同、同一 terminal 重入键不变。
- [ ] **P1.3** 新增相同 usageKey 经直接写、写库重试、reconciliation 多次投递仍只有一行的测试。
- [ ] **P1.4** 新增合法全零、负数、bool、浮点、cached>prompt、缺 terminal usage 契约测试。
- [ ] **P1.5** 实现原始价格快照；测试显式 0 与字段缺失不混淆，使用 Decimal→nano-USD。
- [ ] **P1.6** terminal 后先写独立 usageRecord outbox，再尝试 SQLite，assistant 引用 usageKey。
- [ ] **P1.7** 更新 conversation 新旧累计规则，验证 usageRecord + assistant 不双计，legacy assistant 仍可恢复。
- [ ] **P1.8** 覆盖多线程、多进程同库竞争写入。

验证：每个返回合法 usage 的 attempt 在同键任意重放后最多一行；无 usage attempt 不伪造。

### Phase 2 — 补账闭环与入口接入

- [ ] **P2.1** 实现启动扫描、同事务游标、运行期有界后台重试与正常关闭补扫。
- [ ] **P2.2** 故障注入：outbox 成功/DB 失败、DB 成功/assistant 失败、outbox 失败/DB 成功、二者均失败。
- [ ] **P2.3** 坏 JSONL 停行并告警；不得越过后推进游标。
- [ ] **P2.4** 会话删除前 drain/ack；未补齐时拒绝删除并保留日志。
- [ ] **P2.5** Web 普通回答、多工具 step、retry、确认续流分别验证调用级行数。
- [ ] **P2.6** 冷恢复、切模型、保存配置触发 Agent 重建后只记录新 attempt。
- [ ] **P2.7** stop 与 terminal/assistant 并发时不出现泵级旧值或撕裂值落账。
- [ ] **P2.8** 从 `_recordUsage()` 删除 `usageTurns` 写入；只清理因此孤立的 baseline/费用代码。
- [ ] **P2.9** 状态栏 session cost 最小改读统一账本，验证当前 step 不重复叠加；不重做状态栏其他数据源。
- [ ] **P2.10** 若选择 System-wide，接入 CLI、SDK、askSubAgent/library source，验证子进程并发。

验证：`Σ 去重后的原生 usageRecord = Σ exact usageEvents`；Agent resume 不改变历史 exact 行数。

### Phase 3 — 历史迁移与回滚安全

- [ ] **P3.1** 新表与旧 `usageTurns` 并存；迁移前做不可变备份，但不覆盖运行中的 live DB。
- [ ] **P3.2** 原生 usageRecord 始终按原 usageKey/价格导入，优先于任何 legacy 逻辑。
- [ ] **P3.3** 只实现用户选定的一种 legacy 策略，不混合 JSONL 与旧 DB 猜测去重。
- [ ] **P3.4** 实现 deterministic legacy key、provider 推断/legacyUnknown 和价格质量标记。
- [ ] **P3.5** 生成脱敏 dry-run 报告；迁移失败不切换页面读路径。
- [ ] **P3.6** 测试重复迁移、部分/坏日志、已删日志、歧义模型、迁移中断。
- [ ] **P3.7** 测试“新版本运行→回滚旧代码继续写 usageTurns→再次升级”：新 usageEvents 不丢，原生 usageKey 不重复，新增旧行按所选 legacy 策略处理。

验证：回滚绝不拿升级前备份覆盖唯一的新账；新旧表均保留，重新升级可恢复。

### Phase 4 — 一个查询、一个一致响应

- [ ] **P4.1** 实现六种 period 半开边界，注入 clock/timezone 便于测试。
- [ ] **P4.2** 覆盖月首、年初、闰年、DST 前进/回拨、恰好落边界的记录。
- [ ] **P4.3** 在一个 SQLite 只读事务中取得一个分组结果集，再派生 model/provider/global totals、费用和 quality。
- [ ] **P4.4** 实现 `GET /api/usage?period=...`；非法 400，缺省 last7Days。
- [ ] **P4.5** 断言查询路径不访问 sessions/JSONL/models 配置。
- [ ] **P4.6** 前端切换后删除 `/api/usage/series` 和旧 `sessions` 响应。
- [ ] **P4.7** 若选择 C，在同一结果集/响应加入 bucket，不增加第二请求。

验证：每个 period 的 global/provider/model token、调用数、费用覆盖与质量计数全部守恒；查询命中时间索引。

### Phase 5 — 实现用户选定界面

- [ ] **P5.1** 保留四卡指标/顺序，增加一个包含六个选项的原生时间范围下拉框和实际区间/时区。
- [ ] **P5.2** 删除会话表、session link、`renderTable()` 与相关 DOM/CSS。
- [ ] **P5.3** 只实现选定的 A/B/C Provider 卡，卡片纵向单列并展示模型调用数/用量。
- [ ] **P5.4** 一个请求整体驱动四卡、Provider、质量提示；不保留旧时间段局部数据。
- [ ] **P5.5** 实现 stale-response 防护、整体 loading、error/retry、empty 状态。
- [ ] **P5.6** 实现 complete/partial/unavailable 费用和 legacy/迁移价质量提示。
- [ ] **P5.7** 固定展示终态已落账覆盖说明；空态用“暂无已落账用量”。
- [ ] **P5.8** 适配窄屏、长 ID、零 token 调用、0/未知费用和大数字。
- [ ] **P5.9** 若 A/B 不再使用 Chart.js，删除仅因此孤立的 Chart.js 引用；不清理无关代码。

验证：每次打开/切换只有一个统计请求；旧响应不能覆盖新 period；DOM/响应均无会话明细。

### Phase 6 — 测试、文档、审核与交付

- [ ] **P6.1** 后端 pytest 覆盖账本、outbox、迁移、period、聚合、费用、故障和并发矩阵。
- [ ] **P6.2** 前端 Node/pytest 覆盖按钮、单请求、竞态、三层渲染、质量/费用、空/错态。
- [ ] **P6.3** 运行专项测试，再运行 `uv run pytest -q` 全量回归。
- [ ] **P6.4** 更新 `docs/webApiSpec.md`、文件头版本和用户可见统计口径。
- [ ] **P6.5** 用临时 HOME/SQLite/JSONL 做端到端迁移与六范围验收，不写真实用户数据。
- [ ] **P6.6** 创建全新 subagent 做实现与测试独立审核；修复后再以全新 subagent 复审至无明显问题。
- [ ] **P6.7** 真实迁移前展示 dry-run 汇总并取得用户确认。

验证：无 Blocker/High/未处置 Medium；全量测试通过；live DB 不被备份覆盖；界面与所选线框一致。

## 10. 测试矩阵

| 类别 | 必测场景 | 核心断言 |
|---|---|---|
| attempt 幂等 | terminal 重入、同键直接写与补写、retry 多 attempt | 同 attempt 1 行；不同 attempt 不撞键 |
| 恢复 | fresh Agent 读取历史后再调用 | 只新增新 attempt |
| 多 step | 模型→工具→模型 | 2 行且模型/时间/usage 各自正确 |
| retry | 无 usage 失败后成功；失败 attempt 也返回合法 usage | 每个有合法 terminal usage 的 attempt 恰一行 |
| stop/崩溃 | terminal 前、outbox 后 DB 前、DB 后 assistant 前 | 不伪造；可补写；不撕裂 |
| 删除 | pending outbox 时删会话 | 拒绝删除并保留唯一副本 |
| 并发 | Web + subagent 多进程写/补扫 | 无丢行/双行，锁失败有界 |
| 价格 | 正常、显式 0、字段缺失、改价 | 历史不变；unknown 不显示 0 |
| 范围 | 六 period、边界、月/年/闰日、DST | 严格 `[start,end)` |
| 快照守恒 | 并发写下多 provider/模型 | global = provider sum = model sum |
| 历史 | 三策略各自、原生记录、歧义模型、坏行 | 不混源猜去重，质量标记正确 |
| 回滚 | 新→旧→新 | 新账保留，原生键不重复，旧阶段可再迁移 |
| 前端竞态 | 连点两个 period，后请求先返回 | 最终仅显示后一次选择 |
| 前端状态 | 无记录/失败/partial/legacy/长 ID | 四卡稳定、说明准确、无会话信息 |
| 安全 | provider/model 含 HTML 字符 | 仅文本展示，无 DOM 注入 |

## 11. 发布与回滚

1. 先在临时 HOME 做 migration dry-run 和端到端验证。
2. 真实升级时停止官方 writer，复制不可变备份；在**原 live DB 中新增表但保留 `usageTurns`**，不删除旧表。
3. 迁移成功、报告通过后切换新 API/UI；失败则不切换读路径。
4. 回滚代码时不恢复旧备份覆盖 live DB：旧代码继续使用仍存在的 `usageTurns`，新 `usageEvents` 原样保留但旧 UI 暂不展示。
5. 回滚期间新增的旧 `usageTurns`/legacy JSONL 在再次升级时按已选策略导入；此前原生 usageRecord 始终按原键恢复。
6. 备份只用于灾难恢复/人工比对，任何恢复动作都必须先另存当前 live DB 和待补 outbox，禁止覆盖唯一新账副本。

## 12. 明确不做

- 不把本地估算费用宣传为 Provider 发票。
- 不为缺 terminal usage 的失败/中断调用猜 token。
- 不把 `snapshotAt` 宣传成入账完整水位。
- 不继续维护 sessions 汇总与历史账本两套用量页口径。
- 不同时实现 A/B/C 三套生产界面。
- 不在本任务增加任意日期、会话筛选、导出、预算告警或账单对账。
- 不重做聊天状态栏；只做账本切换所需的费用兼容。
- 不借机重构无关聊天、模型设置或日志模块。

## 13. 独立审核记录

### 13.1 无结论尝试

两次宽范围全仓 reviewer 分别在 900s/600s 超时，未形成结论，均不计为审核通过。

### 13.2 首轮聚焦审核（subGPT/gpt-6-astra，2026-09-21）

结论：**需修订**。8 项 High/Medium 已全部吸收：

1. usageKey 改为物理 attempt 前生成并贯穿终态/补写；规则改为每个取得合法 usage 的 attempt 恰记一次。
2. 补充运行期重试、启动/关闭补扫、删除前 drain/ack 和不可恢复边界。
3. 原生 usageRecord 永远复用原键和价格，补回滚再升级联合测试。
4. 删除“文件存在即整体替代 DB”的混合迁移，改为三种互斥 legacy 策略。
5. 回滚不再用旧备份覆盖 live DB；新旧表并存，保护升级后新账。
6. UI 明示只含已落账终态 usage，进行中/无 usage 不含且迟到补账会回填。
7. API/UI 增加区间质量与迁移价格来源提示，明确 complete 只代表价格覆盖。
8. 统一决策表，补时区/归桶/USD/历史语义；状态栏限定为必要费用兼容，不扩展重构。

### 13.3 无结论复审尝试

一名全新 reviewer 返回空结果，无法判定，不计为通过。

### 13.4 修订后全新 subagent 复审（glm/glm-5.3，2026-09-21）

按固定九项检查表复核：单一 SQLite 源/单事务快照、六筛选联动、四卡与 Provider 卡、attempt 幂等、outbox/删除边界、互斥 legacy 策略与回滚、费用/质量语义、用户决策一致性、TODO 可实施性，九项均为 PASS。

结论：**无 Blocker、High 或未处置 Medium；无明显问题，可交付。**

### 13.5 方案 C 细化审核（subGPT/gpt-6-astra，2026-09-21）

全新 subagent 聚焦审核固定小时桶、每小时每模型独立柱、输入/缓存/输出三色堆叠、长区间滚动与独立 HTML 原型边界。

结论：**无 Blocker、High 或未处置 Medium；无明显问题，可开始制作原型。**

### 13.6 第一版 HTML 原型验证（主代理，2026-09-21）

- 文件：`docs/260921_usageStatisticsOptionCPrototype.html`。
- Python `html.parser` 解析通过；提取内嵌脚本后 `node --check` 通过。
- Chrome headless 桌面视口 `1600×1100` 成功渲染截图，检测到输入/缓存/输出三种目标颜色均实际出现在图表区域。
- Chrome headless 窄屏视口 `430×932` 成功渲染截图。
- 静态检查确认六个 period 均存在，且原型无 `fetch`、XHR、`/api/` 或真实数据路径引用。

结论：第一版原型可供用户本地打开确认；尚未修改正式 Web 页面或真实数据。

### 13.7 用户第二轮界面反馈

用户要求：筛选改下拉框；删除“数据桶”与公式说明；图表整页展示且不滚动；删除同步滚动/跳到最新/粒度说明；今天与昨天按小时，近7天、近1个月、本月、上月按天；输入/缓存/输出改为同一色系不同深浅。上述要求已写入 §7.1 与 Phase 0。

### 13.8 第二轮 UI 增量审核（glm/glm-5.3，2026-09-21）

首轮结论为需小幅修订，提出 1 High / 3 Medium，均已修复：

1. 删除按钮时代的 `aria-pressed` 要求，改为原生可访问下拉框。
2. 将“粒度切换”改为由筛选自动采用小时/日粒度，不提供手动控件或说明。
3. 删除“默认界面建议 B”的陈旧假设，明确 C 已确认。
4. 明确今天只绘制到查询时刻所在小时，不绘制未来小时零值。

首次全新 subagent 复审又发现 2 项 Medium，均已修复：

5. 删除 §7 残留的 B 方案“推荐”定位，明确 B 未选。
6. 将 P5.1 的“六段筛选”改为一个含六项的原生下拉框。

另一全新 subagent 按八项口径做全文最终复审，确认当前要求与 TODO 全部一致；历史记录中的第一版旧口径仅为审计事实，不构成现行约束。

结论：**无 Blocker、High 或未处置 Medium；无明显问题，可修订原型。**

### 13.9 第二版 HTML 原型验证（主代理，2026-09-21）

- `docs/260921_usageStatisticsOptionCPrototype.html` 文件头升为 v1.1，正式 Web 页面和真实数据未改。
- Python HTML 解析通过；提取内嵌 JavaScript 后 `node --check` 通过。
- 静态断言通过：一个原生 select、六个 option；仅 today/yesterday 为 hour，其他四项为 day；三种颜色均属于蓝色系；无网络请求和真实数据路径；无横向滚动 CSS。
- 可见源码中未出现“数据桶”“小时桶”、公式说明、同步滚动、“跳到最新”或旧按钮控件。
- Chrome headless 对桌面 `1600×1000` 与窄屏 `430×932` 分别遍历六个筛选：全部 `data-horizontal-overflow=false`；today=9 个小时、yesterday=24 个小时、last7Days=7 天、last30Days=30 天、thisMonth=21 天、lastMonth=31 天。
- 桌面、窄屏、今天和上月截图均成功；像素检查确认输入/缓存/输出三种蓝色都实际出现在柱图中。

结论：第二版原型满足用户本轮八项反馈，可供视觉确认。
