# 代码审核报告 — streamOutputPlan.md v2.0 §6 方案 C 详细设计

> 审核日期：2026-07-26
> 审核范围：§6.2 事件模型 / §6.3 确认流程 / §6.4 锁策略 / §6.5 向后兼容 / §6.6 落地步骤
> 对照代码：`flamingoAgents/core/agent.py`（v1.9 全部 return 分支）、`core/types.py`、`core/conversation.py`

## 总览

- 审核章节：5 个
- 发现问题：🔴 1 个 / 🟠 4 个 / 🟡 4 个 / 🔵 2 个
- 整体评价：事件模型与状态机的同构设计方向正确，7 种事件对 agent 主流程**终态分支**覆盖完整；但存在一个架构级硬伤——"适配器回调同步透传为事件"在单线程生成器中**物理上不可实现**，必须修改适配器层接口设计。另有若干分支遗漏与锁策略细节需要补强。

---

## 问题清单

### 🔴 [Critical] 适配器回调无法"同步透传"为生成器事件——§6.1/§6.2 的核心机制不可实现

**位置**：§6.1 分层架构、§6.2 设计说明第 1 条（"textDelta/reasoningDelta 由适配器回调**同步透传**为事件（生成器内部收到回调后 yield）"）

**问题**：这是方案 C 的地基，但它在 Python 单线程生成器模型下不成立。调用栈是：

```
agent 生成器 → modelAdapter.complete(..., onDelta=cb) → SSE 读取循环 → cb(片段)
```

`cb` 在 `complete()` 的**嵌套调用栈内部**被同步调用，而 Python 生成器**只能从自己的栈帧 yield**，无法从回调里 yield。这意味着：

- 若回调只往缓冲区 append，等 `complete()` 返回后生成器再统一 drain yield——所有 textDelta 在模型调用**结束后**才一次性吐出，用户看到的还是"等 10 秒然后全量出现"，**流式体验完全丧失**，方案 C 的核心价值归零；
- 唯一的单线程出路是适配器自己成为可迭代对象，让生成器 `yield from` 委托。

**修复方案**（二选一，推荐 A）：

**A. 适配器层改为迭代器接口（推荐，保持单线程）**

适配器不暴露回调，而是暴露流式迭代器；回调只在最上层包装中出现：

```python
# ports.py —— 适配器协议新增
def completeStream(self, messages, tools) -> Iterator[adapterChunk]:
    # adapterChunk = textChunk(text) | reasoningChunk(text) | finalChunk(completion)
    ...

# agent.py —— 生成器直接委托，实时性成立
def _runLoopStream(self, sessionId):
    ...
    for chunk in self.modelAdapter.completeStream(messages, modelTools):
        if isinstance(chunk, textChunk):
            yield streamEvent(type='textDelta', text=chunk.text)
        elif isinstance(chunk, reasoningChunk):
            yield streamEvent(type='reasoningDelta', text=chunk.text)
        else:  # finalChunk 携带合成好的 modelCompletion
            completion = chunk.completion
    ...

# agent.py —— §6.5 同步包装层把事件映射回回调，方案 B 能力保留
def runUserMessage(self, message, sessionId=None, onDelta=None, onReasoning=None):
    for event in self.runUserMessageStream(message, sessionId):
        if event.type == 'textDelta' and onDelta: onDelta(event.text)
        ...
```

收益：单线程、无队列、无额外线程；`stream=False` 时适配器退化为一次性 yield 单个 finalChunk，回退路径自然统一；§6.5 的 onDelta/onReasoning 回调能力不降反升（在事件流上机械映射即可）。

**B. 工作线程 + 队列**：`complete()` 在 worker 线程跑，回调推 queue，生成器从 queue 取。可行但引入线程间同步、异常跨线程传递、worker 与 session 锁的归属等复杂度，与 §6.4"单线程 CLI 无死锁风险"的论断直接冲突。不推荐。

需要在文档中修改：§6.1 分层图（适配器层职责改为"产出 chunk 迭代器"）、§6.2 设计说明第 1 条、§6.6 步骤 1（适配器验证标准相应调整）。

---

### 🟠 [High] 同步包装在 sessionId=None 时拿不到生成器内部创建的 sessionId，runResult 无法构造

**位置**：§6.2 设计说明第 3 条（"事件不含 sessionId"）+ §6.5 包装设计

**问题**：现状 `runUserMessage(message, sessionId=None)` 内部 `realSessionId = sessionId or self.createSessionId()`，`runResult.sessionId` 必须返回这个真实 ID。改造后 sessionId 在**生成器内部**创建，而事件流又刻意不带 sessionId，包装层耗尽事件后**无从得知**真实 sessionId，`runResult(sessionId=?, ...)` 构造不出来。这不是实现细节，是设计缺口。

**修复方案**：包装层**预生成** sessionId 再传入（与现状顺序完全一致）：

```python
def runUserMessage(self, message, sessionId=None, ...) -> runResult:
    realSessionId = sessionId or self.createSessionId()
    for event in self.runUserMessageStream(message, sessionId=realSessionId):
        ...  # 终态事件 → runResult(sessionId=realSessionId, ...)
```

文档需在 §6.5 明确写出这一点（同时也回答了"为什么事件可以不带 sessionId"）。

---

### 🟠 [High] §6.2 事件模型遗漏 3 个前置 error 分支的映射约定

**位置**：§6.2 事件表、§6.3 API 契约

**问题**：对照 `agent.py`，以下 return 分支在事件模型中**没有说明如何映射**：

1. `runUserMessage` 空消息 → `status='error', message='消息不能为空。'`（**在拿锁之前**返回）；
2. `runUserMessage` `hasPendingConfirmation` → error（"请先调用 continueConfirmation"）；
3. `continueConfirmation` `pending is None or confirmationId 不匹配` → error（且**要把 pending set 回去**，不能吞掉）。

这三个分支发生在"流正式开始之前"。如果生成器首次迭代时直接抛异常或静默结束，§6.5 的包装层就无法"机械映射"出同构 runResult——契约断了。

**修复方案**：在 §6.3 明确一条契约：

> 前置校验失败（空消息 / 存在 pendingConfirm / confirmationId 不匹配）时，生成器在首次迭代 yield **单个 `error` 事件**（`errorType` 分别取 `emptyMessage` / `pendingConfirmationExists` / `confirmationMismatch`）后正常结束；confirmationMismatch 分支必须保持现状语义——pending 不被消费（setPending 放回）。

同时 §6.2 的 `error` 事件说明补充"`errorType` 用于区分前置校验错误与运行期错误"。

---

### 🟠 [High] toolCallStart 与 confirmationRequired 的触发次序/重复语义未定义

**位置**：§6.2 事件表（toolCallStart 触发时机："某个工具即将执行前"）

**问题**：对照 `processToolBatch`，需确认工具在检出时**并不执行**（setPending 后 return），真正的执行发生在 `continueConfirmation` 批准之后。于是同一个 toolCall 存在两个"即将执行"的候选时点：

- 时点 1：`processToolBatch` 检出需确认 → yield confirmationRequired（此时会 yield toolCallStart 吗？）；
- 时点 2：`continueConfirmationStream` 批准后 `executeToolCall` 之前（此时 yield toolCallStart 吗？）。

若两个时点都发，TUI 会显示同一工具"开始"两次；若只发时点 1，则批准续跑的新流里该工具没有 Start 只有 End，事件配对断裂。文档未定义，落地时必然踩坑。

**修复方案**：在 §6.2 明确约定（建议）：

> `toolCallStart` **只在真正调用 `executeToolCall` 之前**触发。需确认工具检出时只发 `confirmationRequired`（其载荷已含 toolCall + commandPreview，不需要 Start）；批准后续跑流中该工具正常走 `toolCallStart` → `toolCallEnd`；拒绝路径只发 `toolCallEnd`（isError=True，对应 buildBlockedToolResult），不发 Start。

---

### 🟠 [High] §6.4 锁策略对"终态事件后放弃迭代"的场景仍泄漏锁，跨线程续跑即死锁

**位置**：§6.4 第 1–4 条

**问题**：`try/finally` 只能保证"生成器被继续驱动或 close() 时"释放锁。典型消费模式：

```python
for event in stream:
    if event.type == 'confirmationRequired':
        break   # ← 生成器挂起在 yield 点，锁仍持有！
result = agent.continueConfirmationStream(...)  # 同线程靠 RLock 可重入"侥幸"通过
```

- 同线程：RLock 可重入，表面能跑，但旧生成器直到 GC 才 close，锁计数依赖 GC 时机——脆弱；
- 跨线程（未来 TUI 的典型场景：UI 线程 break 后在另一线程调 continueConfirmationStream）：**直接死锁**——旧生成器在 UI 线程持锁挂起，新流在另一线程阻塞在 acquire 上；
- 消费者在循环体内抛异常（如 print 出错）同样导致生成器悬挂持锁。

§6.4 第 2 条把责任全推给"调用方契约必须 close"，文档级约束太弱。

**修复方案**：结构性消除——**终态事件在锁释放之后再 yield**。调整生成器结构，让 `with lock:` 块在 yield 终态事件**之前**退出：

```python
def runUserMessageStream(self, message, sessionId=None):
    ...  # 前置校验
    terminalEvent = None
    with self.getSessionLock(realSessionId):
        for event in self._runLoopStream(realSessionId):  # 中途事件在锁内 yield
            if event.type in ('completed', 'confirmationRequired', 'error'):
                terminalEvent = event
                break
            yield event
    # 锁已释放，再 yield 终态事件
    if terminalEvent is not None:
        yield terminalEvent
```

效果：**消费者看到终态事件时锁必然已释放**，"拿到 confirmationRequired 后直接开新流"在任何线程都安全，最常见的消费模式（for 循环跑到终态自然结束）零风险。锁泄漏只剩"中途 textDelta 阶段放弃迭代"一种，配合 close 契约即可接受。文档 §6.4 需重写第 1、2 条，并删除第 3 条中"两流之间不叠加"依赖 RLock 重入的论证。

---

### 🟡 [Medium] toolCallStart.preview 语义扩围未声明

**位置**：§6.2 toolCallStart 载荷含 `preview: str`

**问题**：现状 `buildToolPreview` 只在"需确认"分支调用。新设计下每个工具执行前都要生成 preview——这是行为扩围（preview 函数会在所有工具上跑一遍），且 preview 是用户自定义 callable，执行频次和异常面都变了。文档未提及。

**修复方案**：§6.2 补一句："preview 对所有工具生成（现状仅确认工具生成），沿用 `buildToolPreview` 的异常兜底（失败回退 `str(call.arguments)`）"；或直接去掉 preview 字段让 TUI 自己调 preview——二选一，需拍板。

### 🟡 [Medium] 未知工具（unknownTool）分支的事件行为未定义

**位置**：§6.2 事件表 vs `processToolBatch` 的 `definition is None → makeUnknownToolResult → continue` 分支

**问题**：未知工具**不执行**（合成 isError 的 toolResult 后跳过）。toolCallStart 发不发？toolCallEnd 发不发？事件配对是否要求 Start/End 一一对应，文档没有规则。

**修复方案**：明确约定（建议 Start/End 都发，保持配对不变式"每个进入批处理的 toolCall 恰好一对 Start/End，confirmationRequired 除外"），TUI 实现最简单。

### 🟡 [Medium] §6.6 验证标准缺两项关键回归

**位置**：§6.6 步骤 3、5

**问题**：
1. 没有"中途放弃流"的验证项——步骤 5 的 5 个场景全部假设流被消费到终态，锁泄漏/终态释放这两个 §6.4 的核心风险**没有任何验证标准**；
2. 适配器改为迭代器接口后（见 Critical 修复），`ports.py` 协议变更 + grep `complete(` 全部实现方/调用方同步签名这一步消失了（方案 B 里有，§6.6 没有继承）。

**修复方案**：
- 步骤 3 验证标准追加："迭代到 textDelta 后 `stream.close()`，同会话立即可发起新流（验证锁释放）"；
- 步骤 5 追加场景 ⑥："拿到 confirmationRequired 事件后**不 close 旧流**直接 continueConfirmationStream，验证续跑成功"（对应 High 锁问题的修复验证）；
- 步骤 1 追加："`ports.py` 协议签名同步，grep 全部 `complete(` / `completeStream(` 调用方"。

### 🟡 [Medium] dangling 恢复路径在 §6.5 未点名验证

**位置**：§6.5 "终态事件到 runResult 是机械映射"

**问题**：`runUserMessage` 的 dangling 恢复路径（takeDanglingToolCalls → processToolBatch → 可能 confirmationRequired / 继续 loop）是 v1.9 的重点功能，事件流化后它会产生"一流内先 toolCallStart/End（dangling 批）再 queued 消息入列再模型轮"的复合事件序列。映射本身无损，但 §6.6 验证标准没有单列。

**修复方案**：§6.6 步骤 5 追加场景 ⑦："构造 dangling 会话日志后恢复，验证事件序列 = [dangling 工具事件…] + [正文流] + completed，且日志结构不变"。

---

### 🔵 [Low] §6.4 "单线程 CLI 场景下无死锁风险"表述过于乐观

同线程不死锁靠的是 **RLock 可重入的侥幸**（旧生成器挂起持锁 + 新流同线程重入），不是设计保证。建议措辞改为："同会话并发由 RLock 串行化；任何线程下中途放弃迭代都必须 close()，终态事件则保证锁已释放（见结构性修复）"。

### 🔵 [Low] 事件 dataclass 判别方式建议收紧

§6.2 用 `type: str` 字符串判别，TUI 端要硬编码字符串。建议用 `Literal[...]` 或 7 个独立 dataclass + isinstance 判别（文档表格已是 7 种载荷各异的事件，独立类更自然，也与 types.py 现有风格一致）。非阻塞项。

---

## 优点记录

- **事件集合克制**：7 种事件对 `continueModelLoop`/`processToolBatch` 全部终态分支（completed / confirmationRequired / error×3 / 超步数）的映射是**完整且无冗余**的；刻意不加 stepFinish/usage 符合"反推测性设计"原则，正确；
- **确认流程"流终止 + 新流续跑"**：与 pendingConfirm 持久化状态机严格同构，拒绝 generator.send() 回传的理由（生命周期与锁不可控）成立，这是 §6 里最扎实的一节；
- **适配器与 agent 分层**、payload 合成、流内错误识别等方案 B 约定原样继承，没有重复设计；
- §6.4 提前识别了"创建时不拿锁、首次迭代才拿锁"这个正确关键点。

## 修复优先级建议

1. **🔴 适配器回调→迭代器接口**（Critical）：不改则方案 C 的实时性不成立，且影响 §6.1/§6.2/§6.6 三节，必须先修；
2. **🟠 终态事件锁后 yield + High 锁策略重写**：方案 C 被拍板的核心场景（TUI 多线程）的安全底线；
3. **🟠 sessionId 预生成 + 前置 error 分支映射 + toolCallStart 次序约定**：都是"不改文档、落地必返工"的契约缺口，修订成本低。
