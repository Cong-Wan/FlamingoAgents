# 当前工具调用并发语义深度调研计划

- 日期：2026-09-20
- 状态：调研、实验、报告与独立复审均已完成
- 调研对象：当前工作树 `dev` 分支，基线提交 `3bdce31c835c1cae9763c9efe8ca2d960883d6c8`
- 目标问题：当前 FlamingoAgents 的工具调用到底是串行还是并行，并给出可复核的源码、历史与运行实验证据
- 最终产物：`docs/260920_toolCallConcurrencyResearch.md`

## 1. 范围与术语

“工具调用是否串行”至少有三种不同问题，必须分开回答，禁止把其中一项外推为全部：

1. **同一模型 step / 同一 assistant message 内的一批 `toolCalls`**：多个工具函数是否在时间上重叠执行。
2. **同一 session 的多个请求/流**：是否能同时进入模型循环或工具执行。
3. **不同 session 的请求/流**：Web 运行层是否允许它们在不同线程并发。

补充边界：

- provider 请求里的 `parallel_tool_calls` 只说明是否允许模型一次产出多个调用；必须继续追踪本地执行器，不能据此直接宣称执行并行。
- UI 同时出现多张 running 工具卡，只能证明 `toolCallStart` 的事件顺序，不能证明实际执行时间重叠。
- 单个 `bash` 命令自行使用 `&`、工具实现内部自行开线程/子进程，属于**工具内部并发**，不等于 Agent 对同批 tool calls 的并发调度。
- 本报告只对仓库 `3bdce31...` 当前实现下结论，不把本对话宿主平台未公开的工具调度器当成仓库实现；若提及本对话可用的外层 `multi_tool_use.parallel`，必须明确它不在本仓库代码证据范围内。

## 2. 已知工作区约束

当前有与本任务无关的既存改动：

- `D askModel.py`
- `?? docs/plan/260920_usageStatisticsResearchPlan.md`

调研不得修改、恢复、删除或纳入上述内容。除本计划、最终调研报告以及经审核要求产生的同主题审核记录外，不修改业务源码。

## 3. 证据链设计

### 3.1 主执行链（最高优先级）

逐段建立以下调用链并记录精确行号：

```text
model adapter 解析多个 tool calls
→ agent.driveModelLoop
→ agent.driveToolBatch
→ agent.executeToolCall
→ toolRuntime.executeToolCall
→ definition.execute(arguments, context)
```

要核实：

- `driveToolBatch` 是否先批量发 Start；
- 真正执行调用是否位于普通 `for` 循环；
- 每次 `executeToolCall` 是否在返回后才处理下一项；
- 执行路径中是否存在 `ThreadPoolExecutor`、`asyncio.gather`、`create_task`、`TaskGroup` 或线程创建；
- 工具结果写盘与 End 事件的顺序。

### 3.2 provider 与适配器层

核实并区分：

- Responses payload 的 `parallel_tool_calls`；
- Chat Completions payload 是否显式设置对应字段；
- adapter 如何把多个调用聚合为有序 `assistantMessage.toolCalls`；
- adapter 是否执行工具（预期只解析，不执行）。

### 3.3 会话与 Web 线程层

核实：

- `runUserMessageStream` / `continueConfirmationStream` 的 session `RLock` 持有范围；
- 同 session 是否由同一把锁串行；
- Web 的 `activeStreams` 是否拒绝同 session 第二条活跃流；
- `streamPump` 是否每条活跃流创建独立线程；
- 不同 session 是否因此具备并发条件；
- “不同 session 可并发”应表述为运行层能力，而不是保证任意 provider、工具或共享外部资源都能安全并发。

### 3.4 历史意图证据

使用 Git 只读证据核实当前行为是否是有意设计：

- `git blame` 当前 `driveToolBatch`；
- 引入批量 Start 的提交 `1250f868...`；
- 提交说明与对应方案是否明确“先全部 running，再串行 exec”；
- 旧文档只能作为设计意图/交叉证据，当前源码与可复现实验才是最终事实来源。

### 3.5 可复现实验

使用 `uv run pytest` 执行临时测试文件（放在系统临时目录，不污染仓库；文件头遵守项目规范），至少覆盖：

#### 实验 A：同一 batch 两个慢工具

- fake adapter 第一轮一次返回两个免确认 tool calls，第二轮返回最终文本；
- 两个 fake 工具各阻塞固定时长，并记录单调时钟的 enter/exit、活动执行数与线程 ID；活动计数与时间线写入都由专用 `Lock` 保护；
- 收集完整 agent 事件流；
- 断言事件顺序为 `Start1, Start2, End1, End2`（允许在前后出现 usage/completed 等非工具事件时按工具事件过滤）；
- 断言 `tool2.enter >= tool1.exit`、`maxActive == 1`、两个 execute 使用同一驱动线程；总耗时接近两次时长之和只作辅助观察，不作为唯一判据；
- 输出原始时间线，作为“同一 batch 的同步 for 调度没有执行重叠”的直接运行证据；不把该结论错误归因于 session `RLock`。

#### 实验 B：不同 session 的并发边界

- 用两条线程驱动两个不同 session、两个独立 agent 实例，以贴近 Web `agentCache[sessionId]` 的结构；
- 测试开始先断言 agent 实例不同、sessionId 不同、两者 `getSessionLock(...)` 返回的锁对象不同；
- 每条流执行一个 fake slow tool；共享活动计数与时间线由 `Lock` 保护，避免 `maxActive` 的读改写竞态；
- 用 `threading.Barrier(2)` 同步工具入口，`wait` 必须带有界 timeout；任何 `BrokenBarrierError` 都使测试明确失败，线程 `join` 也必须带 timeout 并断言无存活线程，禁止测试死锁；
- 断言观察到 `maxActive >= 2` 且两个执行区间相交，并打印线程 ID/时间区间；
- 该实验只证明“不同 session 的运行路径具备重叠执行条件”，不承诺 provider/外部资源并发安全，也不改变实验 A 的同批串行结论。

#### 实验 C：同一 session 的 Core 锁

- 只在 Core 层直接驱动同一 agent、同一 session 的两条生成器，不经过 Web；否则第二条请求会先被 `activeStreams` 映射为 409，无法单独验证 `RLock`；
- 在测试中用一个仅增加 attempt/acquired 观测事件、底层仍为真实 `threading.RLock` 的包装器预置该 session lock，区分两条命名线程的“开始尝试获取”和“已获取”；
- 第一条流进入 fake tool 后在可控 gate 内停留，此时断言第一线程已获取 session lock；启动第二线程并等待其“尝试获取”事件（有界 timeout），再断言其“已获取”事件在短探测窗口内未发生、第二条 user message 未追加、adapter 第二请求未进入且线程仍存活；
- 释放 gate 后，两条线程都须在有界 timeout 内退出，第二线程随后出现 acquired/adapter 记录，两条流依次完成；
- 另以静态源码引用 Web `activeStreams` 的 409 防重逻辑，明确 Core 是“锁等待串行”，Web 是“入口拒绝并发”，两者不是同一机制。

#### 实验 D：确认批准路径

- fake adapter 第一轮返回 `[requiresApprovalCall, freeCall]`，第二轮返回最终文本；
- 初始流断言只产生 `confirmationRequired(call1)`，两个工具均未执行；
- 批准后收集续流，记录两个 execute 的 enter/exit、活动数和工具事件；
- 断言批准路径为 `Start1, End1, Start2, End2`，且 `call2.enter >= call1.exit`、`maxActive == 1`；由此覆盖 `driveConfirmation` 当前调用和随后 `driveToolBatch` 余项，而不只覆盖免确认 batch 主路径。

实验稳定性要求：

- 不访问真实模型或网络；
- 所有共享计数/时间线由 `Lock` 保护；所有 Barrier/Event 等待与线程 join 都有超时，测试结束断言无线程遗留；
- 不依赖绝对的极窄耗时阈值作为唯一依据；
- 优先用 barrier/event、区间关系和 `maxActive` 判定；
- 用 pytest 断言，运行时输出保留到报告；
- 临时文件与临时日志位于 pytest `tmp_path` / 系统临时目录，不改生产源码。

## 4. 预期结论结构（尚待证据验证）

报告必须以如下条件式结构呈现，实验完成前不得把预期当事实：

- **同一 assistant batch：预期为串行执行。** 所有可执行前缀会先发 Start，所以 UI 可同时显示 running；随后工具函数逐个同步执行。
- **同一 session：预期为串行。** Core 有 per-session `RLock`，Web 还拒绝第二条 active stream。
- **不同 session：预期可并发。** Web 每个 stream pump 有独立线程、不同 session 使用不同锁/缓存 agent；这是允许并发，不是全局串行。
- **例外：工具内部可自行并发。** 这不代表 Agent scheduler 并行调度多个 tool calls。

## 5. 成功标准

1. 结论明确回答“是/否”，不以“看情况”回避，但按三个层次分别作答。
2. 至少提供四类可复核证据：当前源码行号、负向并发原语检索、Git 引入提交、pytest 时间线。
3. 明确解释 `parallel_tool_calls: true` 与本地串行执行并不矛盾。
4. 明确解释“批量 Start”与“并行 execute”不是同一件事。
5. 报告列出基线 commit、实验命令、测试结果、局限及复现方式。
6. 不泄露 `~/.flamingo/config/models.yaml` 中的密钥/token，不调用真实 provider。
7. 不改业务代码，不触碰既存未提交内容。

## 6. 审核修订记录

### 第一轮有效审核

结论：发现 5 类实验严谨性问题（并补充 1 项确认路径覆盖），均已修复。

审核发现并已修复：

1. 实验 B 的 Barrier 可能永久等待 → 增加不同 agent/session/lock 对象预检、Barrier timeout、BrokenBarrierError 失败与有界 join。
2. `maxActive` 有共享变量竞态 → 所有活动计数和时间线写入统一加锁。
3. 实验 C 若走 Web 会被 409 提前拒绝 → 改为 Core 直测，Web 409 只作独立静态证据。
4. 实验 C 可能没有证明第二线程真的尝试拿锁 → 增加基于真实 RLock 包装器的 attempt/acquired 观测和全部有界等待。
5. 实验 A 因果可能误归于 session lock → 明确它证明普通同步 `for` 调度，同 session 锁由实验 C 单独证明。
6. 为覆盖另一生产执行入口，新增实验 D 验证 `driveConfirmation` 批准后执行当前调用，再串行续跑剩余 batch。

### 第二轮全新审核

结论：**无明显问题，可以执行**。

### 最终报告证据审计

1. 第一名全新审核员结论：核心结论证据充分，**无明显问题**；提出 4 项 P2 边界措辞优化。
2. 已全部修订：串行限定到 scheduler 调用/结果提交；Core 锁限定到公开流入口；`parallel_tool_calls` 不越界解释 provider 协议；Web 409 限定到 active stream 已登记。
3. 第二名全新审核员复审结论：**无明显问题，可以定稿**。

### 执行结果

- 临时证据测试：`uv run pytest -q -s /tmp/testToolCallConcurrencyEvidence.py` → `4 passed in 0.53s`。
- 稳定性：同一证据测试连续重复 5 次，全部 `4 passed`。
- 全量回归：`uv run pytest -q` → `221 passed in 11.09s`。
- 临时测试 SHA-256：`2cd3c2f17d4a13e217884df50330eae0467bd245871350395248fb3ca876037e`。

## 7. TODO lists

### Phase 0：基线与范围

- [x] 记录仓库路径、分支、HEAD 与工作区既存改动。
- [x] 定义 batch / same-session / cross-session 三个并发口径。
- [x] 标明外层宿主工具调度不等于仓库 Agent 调度。
- [x] 由全新 subagent 审核本计划。
- [x] 修复审核发现并由另一全新 subagent 复审，直到无明显问题。

### Phase 1：当前源码证据

- [x] 固定 `driveModelLoop → driveToolBatch → executeToolCall → toolRuntime` 精确行号。
- [x] 固定 Responses `parallel_tool_calls` 与 Chat Completions payload 精确行号。
- [x] 固定 adapter 多调用聚合/排序代码精确行号。
- [x] 固定 session lock、Web active stream 防重、stream pump 线程精确行号。
- [x] 全仓检索并发执行原语及所有 `executeToolCall` 调用点，记录命令与结果。

### Phase 2：历史与设计意图证据

- [x] 记录 `git blame` 结果。
- [x] 检查 `1250f868...` 的提交说明和 diff。
- [x] 对照 `streamingLatencyFixPlan` 的“非目标 / D2 / TODO / 实施记录”。
- [x] 只把历史文档作为辅助证据，不替代当前实现。

### Phase 3：动态直接证据

- [x] 编写并运行实验 A：同 batch 两工具不重叠。
- [x] 编写并运行实验 B：不同 session 可重叠（锁对象预检、Barrier/join 均有界）。
- [x] 编写并运行实验 C：Core 同 session 被锁串行（与 Web 409 分开验证）。
- [x] 编写并运行实验 D：确认批准当前调用与剩余 batch 串行。
- [x] 保存 pytest 命令、通过数、原始时间线与关键断言。
- [x] 核对实验与静态结论无冲突，无需进入冲突定位分支。

### Phase 4：报告与交叉核验

- [x] 写入 `docs/260920_toolCallConcurrencyResearch.md`。
- [x] 在报告首屏给出一句话结论和三层结论表。
- [x] 为每个结论附当前源码路径/行号与实验编号。
- [x] 单列“容易误判的证据”：`parallel_tool_calls`、多张 running 卡、Web 多线程。
- [x] 核验所有引用行号仍对应当前 HEAD。
- [x] 核验 `git diff --check` 与 `git status --short`；本任务只新增两份文档，既存 `askModel.py` 删除与 usage 计划均未触碰。
- [x] 由全新 subagent 对最终报告做证据审计；修复后用另一全新 subagent 复审至无明显问题。
