# 会话内与整体用量统计深度调研报告

- Author: wilbur
- Version: 1.1
- Date: 2026-09-20
- 调研基线：Git `3bdce31c835c1cae9763c9efe8ca2d960883d6c8` 所在工作树
- 审核状态：全新独立 subagent 已完成四项主结论事实核验，结论为“无 Blocker/High/未处置 Medium，可交付”
- 调研性质：只读代码审计 + 真实数据脱敏结构对账 + 临时目录复现实验
- 结论适用范围：当前工作树支持的 OpenAI Chat Completions / Responses 统计链路

> 本次没有修改业务代码、模型配置、真实会话日志、`sessions.json` 或 `usage.db`。调研期间工作树存在与本任务无关的本地改动，且真实会话仍可能被运行中的进程写入；真实数据部分只作为带截止点的样本证据，不替代代码结论。

---

## 1. 执行摘要

### 1.1 一句话结论

**当前实现的 token 字段计算在“标准 OpenAI usage、Agent 未被重建、无停止竞态、持久化无故障”这一正常热路径下基本合理；但 `usage.db` 的整体趋势和总费用存在已复现的历史重复记账问题，且所谓“整体用量”只覆盖 Web 父会话，不覆盖 CLI、SDK 与 `askSubAgent`，因此不能视为 FlamingoAgents 的完整、账单级用量统计。**

### 1.2 分项判定

| 统计对象 | 判定 | 可以相信的部分 | 不能据此断言的部分 |
|---|---|---|---|
| 会话 JSONL / 内存 `usageTotal` | **条件性合理** | 标准 usage 下，每个成功模型调用累计一次；多工具 step、确认续流、恢复重放均有实现 | provider 不返回/返回非标准 usage、失败/中断调用、恶意或畸形数值 |
| 会话状态栏 token | **正常热路径合理** | `↑=prompt-cached`、`↓=completion`、`⚡=cached`，三者不重复；显示会话累计 | `%` 只是最近一次调用后的上下文近似，不是累计 token；持久化失败/停止窄竞态可使其暂时或长期落后 |
| `/api/usage` 顶部卡片与会话表 | **仅对“现存 Web 会话快照”合理** | 从 `sessions.json` 汇总现存会话绝对累计值 | 不含已删会话、CLI、SDK、子代理；模型列只显示当前模型，不能代表历史累计归属 |
| `/api/usage/series` 趋势 | **当前不可靠** | 热 Agent、单泵正常结束时能按实际泵模型记录增量 | 冷恢复/Agent 重建会重复写历史；崩溃/I/O 失败无自动对账；时间记在泵结束点 |
| 总费用 | **只能称“按当前配置估算”** | 在 `cached ⊆ prompt` 时公式不重复收费 | 不是 provider 账单；历史随当前价格变化；不计 cache write；缺价格/订阅价为 0 时隐藏 |
| “整个系统用量” | **不成立** | 覆盖 Web `streamPump` 管理的调用 | CLI、`sdkEntry`、`askSubAgent` 子进程的模型调用不入 `usage.db` |

### 1.3 最高优先级问题

1. **High：冷恢复或 Agent 重建后，历史 usage 会再次写入 `usage.db`。** 已用临时 SQLite/JSONL 稳定复现；会让趋势、模型分桶和费用确定性虚高。
2. **High（若产品目标是“整个 FlamingoAgents”）：CLI、SDK、`askSubAgent` 全部在整体账本之外。** 当前页面实质是“Web 会话用量”。
3. **High：JSONL、`sessions.json`、`usage.db` 是非原子三层账，落账失败后只记诊断、没有幂等键或启动对账。** 崩溃和 I/O 错误可造成永久分叉。
4. **Medium：停止可与 `appendAssistantMessage()` 并发，读到旧值或撕裂值，并因 at-most-once 标志而永不校准。** 已通过放大现有并发窗口复现。

---

## 2. 先澄清三个容易误判的口径

### 2.1 累计 prompt 大于上下文窗口，不一定是重复计数

`conversation.usageTotal.promptTokens` 是**每一次模型 API 调用的 prompt token 之和**。一次用户消息若触发：

```text
模型调用 → 工具 → 模型调用 → 工具 → 模型调用
```

每次模型调用通常会再次发送大部分历史上下文，因此累计 prompt 可以远大于模型的 `contextWindow`。这表示累计消费，不表示当前上下文里同时存在这么多 token。

### 2.2 Cached Tokens 在当前代码口径中是 Prompt Tokens 的子集

当前 OpenAI 风格映射为：

```text
promptTokens = prompt_tokens / input_tokens
cachedTokens = prompt_tokens_details.cached_tokens / input_tokens_details.cached_tokens
completionTokens = completion_tokens / output_tokens
```

在该语义成立时：

```text
总输入 = promptTokens
非缓存输入 = max(0, promptTokens - cachedTokens)
展示总 token = promptTokens + completionTokens
```

**不能再把 cachedTokens 加到 promptTokens 上。** 当前状态栏和趋势图已经避免了这一重复；顶部三张卡虽分别展示 Prompt/Cached/Completion，但 UI 没有明确注明 Cached 是 Prompt 子集，用户手工相加时仍容易误读。

这一包含关系是当前代码对 OpenAI 风格 provider 的协议假设，不代表任意兼容网关都必然遵守。

### 2.3 状态栏百分比不是“累计用量占窗口”

`contextTokens = 最近一次模型调用的 promptTokens + completionTokens`，百分比为：

```text
clamp(contextTokens / 当前模型 contextWindow × 100, 0, 100)
```

它是**下一次请求上下文规模的近似**，而 `↑/↓/⚡` 是整个会话累计消费。二者本来就不是同一分母。工具结果、下一条用户消息、模型切换及 provider 内部裁剪都会让该百分比与下一次真实输入存在差异。

---

## 3. 当前完整数据流

```text
Provider terminal usage（单次模型调用）
  │
  ├─ Chat Completions：原样保留 OpenAI snake_case usage
  └─ Responses：input/output 字段归一化成 OpenAI snake_case
  │
  ▼
conversation.appendAssistantMessage()
  1. 写 assistantMessage + usage 到会话 JSONL
  2. _accumulateUsage() 累加 conversation.usageTotal
  3. 更新 lastTurnTokens
  │
  ▼
agent.usageUpdateEvent（每个有合法 terminal usage 的模型 step 一条）
  │
  ▼
Web streamPump._toUsageUpdateDto()
  ├─ 中间回写 sessions.json：绝对 usage + contextTokens
  ├─ 计算 db 已落账费用 + 本泵临时增量费用
  └─ SSE usageUpdate → 状态栏即时显示
  │
  ▼ 泵终止 / stop
streamPump._recordUsage()
  ├─ finalUsage - startUsage = 泵增量
  ├─ INSERT usage.db.usageTurns
  └─ sessions.json 最终回写 usage/contextTokens/lastUsage

读取端：
  GET /api/sessions/{id}/status ─ sessions.json + usage.db cost + 当前 models.yaml
  GET /api/usage               ─ sessions.json（现存会话）
  GET /api/usage/series        ─ usage.db（历史泵，含已删会话）
```

### 3.1 各层粒度并不相同

| 层 | 粒度 | 字段形态 | 是否包含已删会话 | 主要用途 |
|---|---|---|---:|---|
| `assistantMessage.usage` JSONL | 单模型调用 step | snake_case | 日志删除后否 | 历史恢复、消息详情 |
| `conversation.usageTotal` | 会话累计 | camelCase | 仅内存中的会话 | Core 累计 |
| `sessions.json.usage` | 现存会话绝对累计 | camelCase | 否 | 状态栏、顶部卡片、会话表 |
| `sessions.json.lastUsage` | 最近一个完整泵的增量 | camelCase | 否 | 契约保留，状态栏已不使用 |
| `usageTurns` | 一个 Web 泵的增量 | 三列整数 | 是 | 趋势、模型分桶、费用 |

`usageTurns` 名称容易让人以为“一次模型调用一行”或“一次用户消息一行”，实际是**一次泵流一行**；同一泵内多个工具 step 被合并。

---

## 4. Provider 字段与可计量边界

| 路径 | 获取方式 | 当前正确之处 | 限制 |
|---|---|---|---|
| `chatCompletionsAdapter` 流式 | 请求 `stream_options.include_usage=true`，保留最后一个带 usage 的 chunk | 对标准 OpenAI Chat Completions 正确 | 不归一化其他厂商字段；provider 不发 usage 时成功回复计 0；正常 EOF 即使未见 `[DONE]` 也可生成完成结果 |
| `chatCompletionsAdapter` 非流式 | 直接使用响应顶层 `usage` | 标准字段可累计 | 同样依赖兼容端点字段语义 |
| `responsesAdapter` | terminal response 的 `input_tokens/output_tokens` 经 `normalizeUsage()` 转换 | cached、reasoning 详情映射清晰；缺 total 时补 input+output | 原始空 usage 会变合法全 0；失败事件和 terminal 前中断不记 token |
| Core `_accumulateUsage()` | `int(...)` 后直接累加 | 简洁且可从 JSONL恢复 | 不拒绝负数、bool、浮点截断、`cached > prompt`；畸形嵌套对象可抛异常 |

### 4.1 当前三字段未覆盖的费用维度

- `completion_tokens_details.reasoning_tokens` 被保留在 JSONL，但不单独展示；通常已包含在 completion/output 中，因此不应再额外相加。
- 配置暴露 `cacheWrite` 单价，但运行时没有 cache-write token 计数，费用公式永远不使用它。
- 失败请求、重试失败尝试、terminal usage 到达前的用户停止，即使 provider 最终可能计费，本地也无法精确记录。

因此，当前数据最多是“provider 成功返回并被本地完整消费的 terminal usage”，不是完整账单流水。

---

## 5. 会话内统计合理性分析

### 5.1 已做对的部分

1. **每个模型 step 累加，而非每条用户消息只算一次。** 工具循环的多次 API 调用都会进入累计，这符合真实 token 消费。
2. **重试基线放在 attempt 循环外。** 成功 attempt 只产生一条 `usageUpdate`，不会因本地重试框架重复增加已成功 usage。
3. **确认续流按新泵计算增量。** 确认前后两个泵分别记各自新增量，现有测试覆盖批准和拒绝。
4. **JSONL resume 会重放全部 assistant usage。** 单纯从日志恢复 `conversation.usageTotal` 的算法是正确的。
5. **状态栏不重复展示缓存输入。** `↑=prompt-cached`，`⚡=cached`，二者相加才是总输入。
6. **费用公式已消除 cached 重复收费。** `calcTurnCost()` 的公式在 OpenAI 包含语义下正确。
7. **live usage 的前端竞态防护较完整。** session/generation/requestId、单飞 GET、权威刷新和单调合并已有大量自动测试。

### 5.2 会话内统计的条件与偏差

- 会话累计是**消费累计**，不是当前上下文大小。
- `lastTurnTokens` 只是一种近似；若最近停在工具结果、切换模型或下一条用户消息尚未调用模型，百分比不是下一次请求的最终准确值。
- 会话切过多个模型后，`usageTotal` 仍是全会话总量；`sessions.json` 只保存当前 provider/model，无法从会话表看出历史分摊。
- provider usage 缺失时不会给 UI 警告，而是静默保持旧计数。
- sessions 中间写失败不会阻断回答，但状态接口会落后；后续成功 step 的绝对值回写可以自愈，若会话不再继续则不会自动修复。

**判定：** 把它作为“标准 provider 成功调用的会话累计”是合理的；把它当作 provider 账单、当前上下文精确 tokenizer 结果或所有失败尝试的成本则不合理。

---

## 6. 整体统计范围与双数据源

### 6.1 `/api/usage` 实际统计什么

`server.getUsage()` 读取 `sessions.json`，只汇总：

- 当前索引仍存在的 Web 会话；
- 每个会话的绝对累计 usage；
- 会话表中的模型为当前索引模型。

删除会话后，这部分立即下降。

### 6.2 `/api/usage/series` 实际统计什么

`usageStore.querySeries()` 读取 `usage.db.usageTurns`，统计：

- Web `streamPump` 已终态落账的泵；
- 已删除会话仍保留；
- 以泵结束写库时间切到服务器本地时区的 hour/day/month 桶；
- 按泵创建时冻结的实际 adapter provider/model 分组。

进行中的泵已经可能更新顶部卡片，却尚未写趋势；所以同页差异不只来自“已删除会话”。当前 UI 仅注明删除差异，说明不完整。

### 6.3 CLI / SDK / 子代理均不进入整体账本

`builder.createAgent()` 在非 Web 路径默认把日志写到 `~/.flamingo/logs/cliData/...`。`sdkEntry.runSdk()` 只消费事件并返回文本；`askSubAgent` 又是启动 `sdkEntry.py` 子进程。三者都没有调用 `usageStore.writeUsageTurn()`，也没有 sessions 索引。

因此：

```text
Web 父 Agent usage         → 页面可见
askSubAgent 子 Agent usage → 只在 cliData JSONL，可从页面完全看不到
CLI / SDK usage            → 同上
```

父 Agent 后续把子代理输出作为工具结果重新发送所产生的 prompt token，不等于子代理自身调用成本，不能视为已间接记账。

**判定：** 若产品定义为“Web 现存会话 + Web 历史趋势”，当前范围有明确实现；若页面或用户把它理解为“整个 FlamingoAgents 用量”，则范围严重不足。

---

## 7. 发现的问题

## 7.1 High — Agent 冷恢复/重建后，历史累计被再次写入 `usage.db`

**证据置信度：已复现。**

### 根因

`streamPump.__init__()` 在泵启动前调用 `_currentUsage()`。此时 `runUserMessageStream()` 是惰性生成器；对于新建的 Agent，既有会话尚未执行 `getConversation()`，所以 `agent.conversations` 中没有该 session，基线被记为全 0。

随后泵线程首次迭代生成器，`getConversation()` 才从 JSONL resume，把全部历史恢复进 `usageTotal`。终态 `_recordUsage()` 计算：

```text
finalUsage - startUsage(0) = 历史累计 + 本轮新增
```

这整笔又插入 `usageTurns`。触发场景包括：

- Web 服务重启后继续旧会话；
- `/model` 在空闲时切换模型并丢弃旧 Agent；
- 保存模型配置后 `invalidateAllAgents()`，下次惰性重建；
- 其他导致 Agent cache 丢失、JSONL 仍在的路径。

### 临时目录复现

初始 JSONL 和 DB 已有：

```text
(100 prompt, 40 cached, 5 completion)
```

新调用实际新增：

```text
(10, 3, 2)
```

期望 DB 第二行是 `(10, 3, 2)`，DB 总和 `(110, 43, 7)`；实际为：

```text
rows = [(100, 40, 5), (110, 43, 7)]
DB 总和 = (210, 83, 12)
sessions/JSONL 绝对累计 = (110, 43, 7)
```

### 影响

- 趋势和总费用重复；
- 旧历史被记到恢复后的结束时间桶；
- 若因切模型重建，旧历史会被归到新模型；
- 首个 live cost 也会按“DB 历史基线 + 再算一遍历史”短暂虚高；
- 顶部卡片使用绝对 sessions 值，仍可能正确，使问题不易察觉。

---

## 7.2 High — 三层持久化不是一个可恢复事务，失败后没有 reconciliation

**证据置信度：静态可证，相关 I/O 失败测试确认异常会被吞并后正常封流。**

持久化先后可能是：

```text
JSONL assistant usage
→ sessions 中间绝对值
→ usage.db INSERT + COMMIT
→ sessions 终态绝对值/lastUsage
```

`_recordUsage()` 的 in-memory `usageRecorded` 在 I/O 前就被置为真；任一 I/O 抛错后只写 `usageRecordError`，不重试。同一 try 中 DB 写失败时连 sessions 终态写也不会尝试；DB 成功、sessions 失败时则形成另一方向分叉。

同时：

- `usageTurns` 没有稳定 `usageKey`/唯一约束；
- 启动时 `initUsageDb()` 只建表，不再从 JSONL 回填；
- JSONL 删除后更无法重新核对已删会话账；
- 无 outbox 或定期对账。

所以进程在任意两步之间崩溃，或磁盘/SQLite/索引写失败，都可能造成永久漏账或双账。当前 at-most-once 只解决同进程 stop/finally 重复执行，不等于 durable exactly-once。

---

## 7.3 High（产品范围）— “整体用量”不包含 CLI、SDK、askSubAgent

**证据置信度：静态可证 + 本机脱敏样本。**

本机样本读窗口中，`cliData` 存在 14 个 JSONL、103 条带标准 usage 的 assistant 记录，聚合约：

```text
prompt=3,302,098, cached=2,881,216, completion=75,707
```

同一时点 `usage.db` 只有 Web 记录。该样本会随正在运行的子代理增长，但足以证明这些调用存在且不进入整体页面。

若产品只承诺 Web 用量，应把页面标题、API 文档和提示明确成“Web 会话用量”；若希望统计整套系统，必须让所有入口走共同的、可幂等的 usage sink。

---

## 7.4 Medium — stop 与会话累计并发，可永久漏记或写入撕裂值

**证据置信度：已通过放大源码现存窗口复现。**

`appendAssistantMessage()` 顺序是：

```text
写 JSONL → _accumulateUsage 逐字段修改 dict → append message
```

泵线程在 session lock 内执行，但 `requestStop()` 不获取该 session lock；它立即调用 `_recordUsage()`，而 `_recordUsage()` 只用 `sessionLocksGuard` 查 conversation，不用会话锁或 `conversation.lock` 保护三个数值的一致读取。

因此 stop 可以：

- 在 JSONL 已写、usageTotal 尚未更新时记旧值；
- 在 prompt 已加、cached/completion 尚未加时记撕裂值；
- 先把 `usageRecorded=True`，泵 finally 即使看到完整值也只等待，不会重记。

隔离复现把该已有窗口人为延长后得到：

```text
JSONL/conversation = (25, 10, 4)
usage.db            = 无行
sessions            = (0, 0, 0)
```

窗口通常很窄，但 JSONL I/O 会扩大它；结果可持续到下一次成功的绝对值回写，DB 则不会自动修复。

---

## 7.5 Medium — 同一页面的“总量”不是同一数据集

**证据置信度：静态可证。**

顶部三卡/会话表来自现存 sessions；图和总费用来自历史 DB。差异来源至少包括：

- 已删除会话；
- 正在进行、已中间回写但未终态落 DB 的泵；
- H1 重复记账；
- DB/索引任一写失败；
- 迁移不完整；
- provider 无 usage 或全 0 时 `usageTurns` 不插行。

UI 目前只提示“已删除会话”，容易让用户把其他分叉也当成正常删除差异。

---

## 7.6 Medium — 会话表的“模型”不能代表该行全部 token

**证据置信度：静态可证。**

`/api/usage` 的模型列取 `sessions.json` 当前 `providerId/modelId`，usage 却是会话从创建以来的全模型累计。切过模型后，整行历史看起来都属于最后选择的模型。

趋势图本来能按泵实际模型拆分，但 H1 会在 Agent 重建时把旧模型历史重新归到当前模型。活跃流中调用切模型 API 还会先改索引再返回 409，进一步造成状态标签与实际 adapter 暂时不一致。

---

## 7.7 Medium — 趋势按“泵结束时间”而非模型调用时间

**证据置信度：静态可证。**

一个泵可包含多个模型 step 和长时间工具执行，最终只写一行，timestamp 取 `_recordUsage()` 时的 `nowIso()`。跨小时、跨天任务会整体落入结束桶；历史冷恢复重复量也落在恢复后的时间。

所以当前图适合看粗略的“Web 泵何时结束”，不适合作为精确调用时序或按小时账单核对。

---

## 7.8 Medium — provider 兼容与数值合法性不足

**证据置信度：静态可证；真实样本未发现畸形值。**

- Chat Completions 不识别某些兼容端点可能使用的顶层 cache-hit/miss 字段；这些调用会把 cached 记 0并按全价估算。
- `_accumulateUsage()` 会接受负整数、bool、浮点截断以及 `cached > prompt`；Core 可能不发 live update，但累计/JSONL 已发生变化。
- 缺 usage 的成功回复静默计 0，无“统计不完整”标志。
- 失败/中断调用即使 provider 计费，本地通常无可用 terminal usage。

真实 Web/CLI 样本中未发现负值、`cached > prompt` 或不可解析 usage，只能说明当前样本正常，不能证明所有 provider 路径安全。

---

## 7.9 Medium — 费用名称比实际精度更强

**证据置信度：静态可证。**

当前“总费用”具有以下语义：

- 用当前 `models.yaml` 单价回算全部历史；改价会追溯改变历史总额；
- 模型从 yaml 删除后，相应历史按 0；
- `cacheWrite` 永远不计；
- 订阅模型通常配置 0，只代表不估算，不代表免费；
- H1/持久化分叉会直接污染费用；
- UI 只写“总费用”，未突出“当前价估算”。

公式本身在标准三字段语义下是对的，问题在数据完整性与产品标签。

---

## 7.10 Low — 时间、性能与维护性边界

- hour/day 查询把全表读入 Python 后再丢弃窗口外数据；数据长期增长后成本线性上升。
- `usageTurns` 只有 timestamp 索引，缺 session/model/幂等键索引。
- DST 回拨时 `%Y-%m-%d %H` 标签可能把两个本地小时合桶。
- hour/day 无数据仍返回 72/90 个零桶，month 才返回空数组，空态不一致。
- `webApp.__main__` 文件头仍声称空表会从 JSONL 回填，与当前实现不符。
- `usageView.js` 注释仍称“三 token 之和”，代码实际正确地只算 prompt+completion。

### 已排除的候选误报

图表配置虽在 y 轴使用 `stacked: true`，但柱数据显式使用 `stack: 'tokens'`，未指定 stack 的 line 在当前 Chart.js 内部以 dataset type 形成独立 stack key；没有证据表明“总量线被再叠到柱顶”。本报告不把它列为确定缺陷。

---

## 8. 真实数据只读对账

### 8.1 一致性控制

一次脱敏读窗口记录了 index/DB 的 size、mtime 与 SHA-256 前缀；读取前后未变化：

```text
sessions.json: size=1180, sha256 prefix=08e4bb53ae47
usage.db:      size=16384, sha256 prefix=50a7b5ae2388
changedDuringRead=false
```

调研前后较长时间范围内文件仍有变化，说明存在活跃工作负载，因此以下只是该读窗口的截面。

### 8.2 样本结果

在该窗口：

- 2 个索引会话均有 Web JSONL；
- 两个 `sessions.json.usage` 都与对应 JSONL 的全部合法 assistant usage 精确相等；
- 聚合为 `prompt=5,710,671 / cached=5,350,656 / completion=90,719`；
- 未见缺 usage、非法 usage、负数或 `cached > prompt`；
- `usage.db` 当时有 1 行/1 个 session，该行与相应 JSONL 聚合精确相等；
- 另一会话当时没有 DB 行，但该会话处于继续增长状态，符合“进行中泵先更新 sessions、终态才写 DB”的设计，**不能据此判定漏账**；
- `cliData` 有大量标准 usage，确认不在 DB 范围内。

这组样本说明：

1. 热路径的 JSONL → sessions 绝对累计确实可正确工作；
2. 一个正常终态泵的 DB 聚合可与 JSONL 对上；
3. 样本无法推翻、也不用于证明冷恢复安全；H1 已由隔离实验确定性复现。

---

## 9. 测试覆盖评估

### 9.1 执行结果

```text
uv run pytest -q
221 passed in 10.66s
```

专项：

```text
78 passed in 1.44s
```

覆盖了 live usage Core/DTO、前端竞态、stop/finally owner 竞争、确认续流、Responses 归一化、session store 和恢复工具。

### 9.2 已有测试确实证明的部分

- 多 step 累计与 step baseline 值拷贝；
- retry 成功后只发一条 usage update；
- 合法/不合法 terminal usage 的事件门卫；
- cached 是 prompt 子集时不在 Core 提前相减；
- live cost 不对连续 update 重复累加；
- pump 实际模型写账；
- stop/finally 两个调用者对 `_recordUsage()` 的进程内 at-most-once；
- DB/索引 I/O 抛错不会卡死 SSE；
- 前端旧 GET、attach、confirm、stop 等竞态不会轻易回退显示。

### 9.3 关键未覆盖项

1. **已有 JSONL + fresh Agent + 构造 pump + 首次迭代 resume** 的真实顺序（H1）。
2. stop 与 `appendAssistantMessage()` 内部写 JSONL/逐字段累计的并发（7.4）。
3. DB 成功/索引失败、JSONL 成功/DB 失败后的下次启动 reconciliation。
4. `/api/usage` 与 `/api/usage/series` 在删除、进行中泵、切模型后的端到端一致性。
5. CLI/SDK/askSubAgent 是否应进入整体统计的契约测试。
6. querySeries 的时区、DST、跨小时/天长泵、空态和大表行为。
7. 非 OpenAI 标准 cache 字段、负数、bool、浮点、`cached > prompt` 的存储策略。
8. 历史价格变更/删模型/cacheWrite 对费用的产品语义。
9. migration merge 的幂等与两条合法同值记录边界。

全套测试通过说明现有约定被很好覆盖，但不能证明账本的冷恢复和 crash consistency 正确。

---

## 10. 建议目标口径

建议先由产品明确选择：

### 选项 A：页面只统计 Web

页面改名和说明为：

> Web 会话用量。Token 仅统计 provider 返回 terminal usage 的调用；顶部为现存会话，历史趋势含已删除会话。费用按当前配置估算，不等同服务商账单；不含 CLI、SDK 和子代理。

这是最小范围，但仍必须修 H1 与持久化可靠性。

### 选项 B：统计整个 FlamingoAgents（推荐长期目标）

统一记录 Web、CLI、SDK、subagent，至少增加：

```text
usageKey
source = web | cli | sdk | subagent
sessionId
parentSessionId（子代理可选）
providerId / modelId
modelStepId / requestId
occurredAt（模型 terminal 时间）
prompt / cachedRead / cacheWrite / completion
priceSnapshot 或明确 current-price-only
```

所有入口写同一幂等 sink，页面再按 source/session/model 筛选。

---

## 11. 修复优先级与详细 TODO List

## P0 — 先让账不重、不丢

- [ ] **P0.1 新增冷恢复回归测试**：初始 JSONL/DB 均有历史，fresh Agent 再发一轮；断言 DB 只增加本轮 step，而不是“历史+本轮”。
- [ ] **P0.2 修复 pump baseline 时序**：最小修复须在会话已 resume 后取 baseline；不得以“conversation 尚未创建=0”代表既有会话历史。
- [ ] **P0.3 关闭 stop 撕裂读**：usage 更新与终态快照使用同一 session/conversation 同步边界；UI 可先快速 seal，但持久化必须等生成器完成或明确 quiesce 后读取。
- [ ] **P0.4 引入稳定 `usageKey` 和 DB 唯一约束**：不能只靠进程内 `usageRecorded`。
- [ ] **P0.5 增加启动/显式 reconciliation**：从保留的权威事件补缺，不重复；输出补写、冲突和不可恢复报告。
- [ ] **P0.6 故障矩阵测试**：逐点模拟 JSONL、sessions、DB 写失败与进程重启，验证最终收敛。

验收：

```text
Σ canonical usage events == Σ usageTurns == sessions 绝对累计（按明确的删除范围调整）
```

在重启、切模型、保存模型配置、stop 竞态和任一单点写失败后仍成立。

## P1 — 统一产品范围和展示语义

- [ ] **P1.1 决定 Web-only 或 system-wide**；若 system-wide，把 CLI/SDK/subagent 接入共同 sink。
- [ ] **P1.2 分开“现存会话快照”和“历史总调用”**，不要在无筛选的同一“总量”概念下混用两数据集。
- [ ] **P1.3 UI 明示 Cached 是 Prompt 子集**，总 token 一律使用 prompt+completion。
- [ ] **P1.4 会话模型列显示“当前模型”或“混合模型”**，不要暗示累计量全属当前模型。
- [ ] **P1.5 `%` 标注为上下文估算**；切模型后在新模型首次成功调用前显示“待校准”。
- [ ] **P1.6 总费用改名“按当前价估算”**，显示未定价 token；订阅零价不等于免费。
- [ ] **P1.7 趋势按模型 step terminal 时间记录**，或明确标注“按泵结束时间归桶”。

## P2 — Provider、费用和可运维性

- [ ] **P2.1 建立 adapter 级 usage schema 校验**：拒绝/隔离负数、bool、浮点、cached>prompt；异常进入可观测状态，不污染累计。
- [ ] **P2.2 为不同 provider 显式映射 cache-read/cache-write/reasoning**；不支持时展示 unavailable，而不是默认为确定的 0。
- [ ] **P2.3 决定费用采用调用时价格快照还是当前价回算**；写进 UI 和 API 契约。
- [ ] **P2.4 SQL 按时间窗聚合**，补 session/model/usageKey 索引与 schema version。
- [ ] **P2.5 补 DST、迁移、删除、恢复及大数据量测试**。
- [ ] **P2.6 清理过时注释/文件头**，防止维护者误以为启动时仍会 JSONL backfill。

---

## 12. 最终判定

### 当前可以相信

- 标准 OpenAI usage 下，热 Agent 内 `conversation.usageTotal` 与 JSONL 的成功模型 step 累计；
- 状态栏 `↑/↓/⚡` 的不重叠显示公式；
- cached 成本不被 input 全价再收一次的费用公式；
- 单个正常结束且 Agent 未重建的 Web 泵，能写出正确增量；
- 现存会话顶部卡片在 sessions 与 JSONL 对齐时，可表示“现存 Web 会话累计”。

### 当前不应相信

- `usage.db` 在服务重启、切模型或 Agent invalidation 后仍无重复；
- 趋势图和总费用是精确、可恢复的历史账；
- “用量统计”覆盖 CLI、SDK、askSubAgent；
- 会话表模型列代表全部历史 token 的模型归属；
- 总费用等于 provider 发票；
- 上下文百分比是下一请求的精确 tokenizer 结果；
- provider 未返回 terminal usage 时的 0 表示真实零消耗。

**综合结论：会话 token 的基础算法基本合理，整体统计的数据工程闭环不合理。优先修复冷恢复重复记账、stop/持久化一致性和统计范围，再讨论图表与费用精度。**

---

## 13. 独立审核记录

- 首次尝试的多名宽范围审阅 subagent 因超时未形成结论，未被视作通过。
- 最终使用一个全新独立 subagent，限定只读核验本报告四项主结论及其直接源码：冷恢复 DB 重复、stop/记账竞态、CLI/SDK/subagent 统计范围、费用与 context 语义。
- 审核结论：**无 Blocker/High/未处置 Medium，可交付。**
