# 代码审核报告 — streamOutputPlan.md v2.1 二次审核

> 审核日期：2026-07-26
> 审核范围：v2.1 相对 v2.0 的 11 处修订（§6.1/§6.2/§6.3/§6.4/§6.5/§6.6），逐条核对 `docs/codeReview/260725_streamOutputPlanV2.md` 的 🔴1/🟠4/🟡4/🔵2
> 对照代码：`flamingoAgents/core/agent.py`（v1.9）

## 总览

- 审核章节：6 个（§6.1–§6.6）+ §3/§5 一致性抽查
- 上一报告问题处置：🔴 1/1 已修复、🟠 4/4 已落实、🟡 4/4 已落实、🔵 2/2 已落实
- 本次新发现问题：🟠 1 个 / 🟡 1 个 / 🔵 3 个（均为修订本身引入或新暴露的残留，🔴 无）
- 整体评价：v2.1 正确消除了"适配器回调无法透传为生成器事件"的架构级硬伤，迭代器委托结构在单线程模型下成立；11 处修订方向全部正确。但 §6.3 在落实"前置 error 事件约定"时写错了一个关键事实——三个前置校验分支并非都在拿锁之前，照文档字面落地会引入 pending 状态竞态。另有若干低危残留需顺手清理。

---

## 逐条核对结论（v2.0 问题处置情况）

| v2.0 问题 | 结论 | 说明 |
|---|---|---|
| 🔴 适配器回调无法透传 | ✅ 已消除 | §6.1 改为 `completeStream() -> Iterator[adapterChunk]` 并写明了修正理由；§6.2 定义 3 个 chunk dataclass；§6.6 步骤 1 继承方案 B 的适配器全部约定 + `ports.py` grep 签名同步；§6.5 回调只在包装层出现。agent 生成器 `for chunk in ...: yield event` 的委托结构在单线程下成立：生成器在自己的栈帧内迭代适配器迭代器并逐 chunk yield，实时性成立；`stream=False` 退化为单发 finalChunk 的回退路径也自洽 |
| 🟠 sessionId 预生成 | ✅ 已落实 | §6.5 明确 `realSessionId = sessionId or self.createSessionId()` 在包装层预生成，并解释了"事件为何可以不带 sessionId"。但见下方 🟡 NEW-2 残留 |
| 🟠 3 个前置 error 分支 | ⚠️ 落实但引入新错误 | errorType 三个取值 + confirmationMismatch "pending 不被消费"语义均已写入 §6.2/§6.3；但"以下分支发生在拿锁之前"与现状代码矛盾，见 🟠 NEW-1 |
| 🟠 toolCallStart 次序 | ✅ 已落实 | §6.2 配对不变式例外①：检出时只发 confirmationRequired、批准走 Start→End、拒绝只发 End，与 `processToolBatch`/`continueConfirmation` 现状严格同构 |
| 🟠 终态事件锁后 yield | ✅ 已落实 | §6.4 结构性重写正确：terminalEvent 在 `with` 块退出后 yield，"消费者看到终态事件时锁必然已释放"成立；删掉了依赖 RLock 重入侥幸的旧论证；close 契约收缩到"中途 delta 放弃迭代"一种场景 |
| 🟡 preview 扩围 | ✅ 已落实 | §6.2 明确声明扩围 + 沿用异常兜底 |
| 🟡 unknownTool 配对 | ✅ 已落实 | §6.2 例外② Start/End 都发（轻微措辞瑕疵见 🔵 NEW-4） |
| 🟡 §6.6 验证标准缺项 | ✅ 已落实 | 步骤 1 补 ports.py grep；步骤 3 补 close 后锁释放验证；步骤 5 补场景⑥⑦ |
| 🟡 dangling 恢复验证 | ✅ 已落实 | 场景⑦ + §6.5 复合事件序列说明 |
| 🔵 §6.4 措辞乐观 | ✅ 已落实 | §6.4 第 2、3 条重写 |
| 🔵 事件判别方式 | ✅ 已落实 | §6.2 改为 7 个独立 dataclass + isinstance |

---

## 问题清单（仅仍存在的问题）

### 🟠 [High] NEW-1 §6.3"三个前置校验分支发生在拿锁之前"与现状代码矛盾，照字面落地会引入 pending 竞态

**位置**：§6.3"前置校验失败的契约"（"以下分支发生在拿锁之前，生成器在首次迭代 yield 单个 errorEvent 后正常结束"）

**问题**：对照 `agent.py` v1.9，三个分支的实际位置是：

- `runUserMessage` 空消息校验 —— 确实在 `with self.getSessionLock(...)` **之前** ✅；
- `runUserMessage` `hasPendingConfirmation` 校验 —— 在锁**之内**（拿锁后第一件事）❌；
- `continueConfirmation` `takePending` + confirmationId 匹配 + `setPending` 放回 —— 整个方法体都在锁**之内** ❌。

v2.0 报告原文只说这三个分支"发生在流正式开始之前"，并未说"拿锁之前"；v2.1 修订时把它写成了"拿锁之前"，属于修订引入的事实性错误。后果不止是文档不准：

1. **pendingConfirmationExists 竞态（TOCTOU）**：若按文档把该校验移到锁外，两个线程对同一会话并发发起流，都在锁外通过 `hasPendingConfirmation` 检查 → 在会话锁上串行 → 第一个流设置 pending 并终止，第二个流照样进入 `_runLoopStream`，在 pending 存续期间追加 userMessage 并发起新一轮模型调用，状态机被污染。现状代码靠"锁内检查"天然关闭了此窗口；
2. **confirmationMismatch 竞态**：`takePending`（消费）→ 不匹配 → `setPending`（放回）若移到锁外，另一线程的批准续跑可插入 take 与 set 之间，导致 pending 丢失或被双重消费。

**修复方案**：把契约文案与锁结构对齐——区分两类校验：

> - `emptyMessage` 不触碰会话状态，可在拿锁之前直接 yield 单个 `errorEvent`；
> - `pendingConfirmationExists` / `confirmationMismatch` **必须在会话锁内执行**，失败时在锁内构造 `errorEvent`，复用 §6.4 的终态事件机制（置为 terminalEvent → 退出 `with` → 锁释放后 yield），随后正常结束。

即契约的正确表述是"前置校验失败时流只产出单个 errorEvent，且消费者收到它时锁必然已释放"，而不是"发生在拿锁之前"。§6.4 的结构 sketch 可顺带补两行展示该分支。

---

### 🟡 [Medium] NEW-2 原始流式 API 下 `sessionId=None` 时确认续跑断链

**位置**：§6.3 `runUserMessageStream(self, message, sessionId: str | None = None)` + §6.2 "事件不含 sessionId"

**问题**：sessionId 预生成修复只覆盖了 §6.5 同步包装层。但方案 C 的**目标消费者恰恰是直接调流式 API 的未来 TUI**：若 TUI 以 `sessionId=None` 调 `runUserMessageStream`（签名允许且是默认值），sessionId 由生成器内部创建，事件流又不带 sessionId——拿到 `confirmationRequiredEvent` 后，TUI **无从得知该向 `continueConfirmationStream(sessionId=?, ...)` 传什么**，确认流程断链。v2.0 报告未点出此组合场景，本次补上。

**修复方案**（二选一，建议 A）：

- **A. 流式 API 的 sessionId 改为必填**：`runUserMessageStream(self, message: str, sessionId: str)`，调用方用现有 `agent.createSessionId()` 自行生成——与包装层"预生成"模式统一，一行签名改动；
- **B. `confirmationRequiredEvent` 载荷补 `sessionId` 字段**——破坏"事件不含 sessionId"的克制设计，不推荐。

---

### 🔵 [Low] NEW-3 §3 残留结论与示例代码同 v2.1 决策矛盾

**位置**：§3 方案 B 标题"⭐ 推荐"、§3 方案 C"结论：现阶段不推荐"、§3 方案 C 示例代码 `if event.type == 'textDelta'`

**问题**：§5 已拍板选定方案 C、§6.2 已改为 7 个独立 dataclass + isinstance 判别，但 §3 仍保留"方案 B ⭐ 推荐 / 方案 C 现阶段不推荐"的旧结论，且方案 C 的示例代码还在用 `event.type == 'textDelta'` 字符串判别——与 §6.2 的 isinstance 模型直接矛盾，读者按 §3 写调用代码就会写错。同理 §6.1 分层图"yield 各类 streamEvent"中的 `streamEvent` 命名在独立 dataclass 模型下已不存在（可作非正式统称，但建议改为"各类事件 dataclass"以免误以为存在基类）。

**修复方案**：§3 两个结论行加"v2.0 已拍板选定 C，本节保留作对比记录"标注；§3 方案 C 示例改为 isinstance 写法或直接注明"以 §6.2 事件模型为准"；§6.1 措辞微调。均为文档级改动。

### 🔵 [Low] NEW-4 `errorType` 对"超步数"等运行期错误取值未定义

**位置**：§6.2 `errorEvent` 说明（"errorType 取值：emptyMessage / pendingConfirmationExists / confirmationMismatch（前置校验）+ 运行期错误的异常类名"）

**问题**：errorEvent 触发时机列了"前置校验失败/模型调用失败/超步数等"，但"运行期错误的异常类名"覆盖不了超步数——`continueModelLoop` 超步数分支不经过任何异常，没有异常类名可用。TUI 若要区分"可重试的模型错误"与"超步数"，目前无判别依据。

**修复方案**：§6.2 补一个固定取值，如 `maxStepsExceeded`（模型调用失败仍用异常类名）。

### 🔵 [Low] NEW-5 toolCallStart 触发时机措辞与未知工具例外自相矛盾

**位置**：§6.2 事件表（toolCallStartEvent 触发时机"真正调用 `executeToolCall` 之前"）vs 设计说明例外②（"未知工具不执行，Start/End 都发"）

**问题**：未知工具在 `processToolBatch` 里 `definition is None → addToolResult → continue`，**从不到达** `executeToolCall`——它的 Start 不可能发生在"真正调用 executeToolCall 之前"。配对不变式本身（每个进入批处理的 toolCall 恰好一对 Start/End）是对的、值得保留，只是触发时机的措辞与例外②打架。

**修复方案**：触发时机改为"工具即将被处理（executeToolCall 或合成 unknownTool 结果）之前"，或在例外②补一句"未知工具的 Start 在批处理检出时发出"。

---

## 优点记录

- 🔴 修复干净利落：§6.1 把"为什么不能是回调"的论证直接写进了文档（生成器只能从自己的栈帧 yield），后续任何人回看都不会再走回回调方案；`stream=False` 退化为单发 finalChunk 让回退路径与流式路径共用同一迭代接口，没有引入第二条代码路径；
- §6.4 的 terminalEvent 结构同时消化了"锁后 yield"与"前置 error 事件"两类终态（一旦 NEW-1 修正，两类终态共用同一机制，无特例分支）；
- 配对不变式用"两个例外"的写法把 confirmationRequired / 未知工具 / 拒绝路径全部收敛进一条规则，TUI 端可以实现零特例的事件配对逻辑；
- 11 处修订未触碰 §6 之外的既定决策，§6.5 包装层与 §6.4 锁结构的衔接（包装层必然消费到终态 → 锁必已释放）闭环自洽。

## 修复优先级建议

1. **🟠 NEW-1（前置校验锁位置）**：唯一会影响功能正确性的问题，改动量小（文案 + §6.4 sketch 补两行），但必须在本方案落地前修掉，否则实现者按字面写代码就埋了竞态；
2. **🟡 NEW-2（sessionId 必填）**：一行签名 + 一句说明，建议顺手在 NEW-1 同一轮修订中拍板；
3. **🔵 三条**：文档级清理，可合并进下一次版本号（v2.2）。
