# 并行工具调用池方案与实施计划

- 日期：2026-09-20
- 版本：1.2
- 状态：方案已通过独立终审，待用户确认产品取舍后实施
- 代码基线：`dev` / `3bdce31c835c1cae9763c9efe8ca2d960883d6c8`
- 关联调研：[`../260920_toolCallConcurrencyResearch.md`](../260920_toolCallConcurrencyResearch.md)
- 目标：允许显式加入池中的同批工具调用并发执行，同时保持 function-calling transcript、权限确认、停止、恢复、Agent 生命周期与 JSONL 顺序正确

## 1. 结论

**可行。** 当前工具接口是同步 `execute(arguments, context) -> toolOutput`，内置工具主要是文件 I/O、等待子进程或等待子代理，适合用有界线程池并发；无需把 Agent、urllib adapter 和 Web pump 全部重写为 `asyncio`。

本方案中“异步执行”的精确定义是：

> 同一个 assistant message 内，连续的池内调用由批次级 `ThreadPoolExecutor` 并发执行；Agent 在屏障处等待全部 worker 静止，由协调线程按原始 call 顺序持久化结果，然后才进入下一执行段或下一轮模型请求。

这是**并发执行 + 批末汇合**，不是 fire-and-forget。模型永远不会在 function call 尚无 output 时继续下一轮。

### 1.1 一句话语义

```text
池内连续调用并发跑；池外调用与确认是屏障；结果原序提交；stop 先收口再放锁；下一模型轮等全部结果。
```

### 1.2 三个必须同时落地的层次

并行 executor 单独落地并不安全，本方案必须同时实现：

1. **批次事务账本**：覆盖整个 assistant tool batch，从任意 Start/End yield 被关闭时都能同步补齐所有未持久化结果。
2. **运行级取消令牌**：每条流使用独立 `Event`，不在模型 step 中复用并 `clear()`；外部 stop 能被协调线程轮询观察。
3. **Web draining 栅栏**：UI 已 stopped 与 Core/worker 已退出分离；旧 Core 真正退出前不注销执行占用、不替换 agent、不创建独立 session lock。

缺少任意一层，都可能出现后台副作用、新旧 Agent 并行写同一 JSONL、或悬空/重复 tool result。

### 1.3 “工具池”的两层含义

本需求可能有两种“池”：

1. **资格池**：哪些工具名允许在同批并发；本方案用 `toolNames` allowlist 实现。
2. **worker 资源池**：线程是否跨 batch / agent 全局复用；本方案一期选择批次级 executor，不做进程级共享池。

这样能先实现用户要的并发能力，又避免在当前没有 agent `close()` / FastAPI lifespan scheduler 的前提下引入持久线程泄漏。代价是不同 session 的总线程数只受各批上限约束；若用户真正需要全局总配额，应另立应用级 scheduler 方案，不能把 `maxWorkers` 误解为进程全局上限。

### 1.4 为什么选择线程池

| 方案 | 判断 | 原因 |
|---|---|---|
| 批次级 `ThreadPoolExecutor` | **选定** | 当前工具是同步 callable；改动集中；I/O/子进程等待可获益；可共享运行级 interrupt event |
| 全链路改 `asyncio` | 不选 | Agent/adapter/生成器/urllib/Web pump 都是同步线程模型，改造面远超需求 |
| `ProcessPoolExecutor` | 不选 | callable/context/debug/Event 不适合跨进程序列化；停止、日志和子进程树更复杂 |
| fire-and-forget 后立即请求模型 | 明确禁止 | 会破坏 provider call/output 配对、权限、恢复与会话事务边界 |

CPU 密集型纯 Python 工具受 GIL 影响，不保证线程并发带来吞吐提升，不是一期优化目标。

## 2. 当前源码证据与既有约束

### 2.1 当前执行链

```text
agent.driveModelLoop
  → driveToolBatch(toolCalls)
    → 批量 yield toolCallStart
    → 普通 for 循环逐个 executeToolCall
      → toolRuntime.executeToolCall
        → definition.execute(arguments, context)
    → addToolResult + toolCallEnd
```

直接证据：

- `flamingoAgents/core/agent.py:376–408`：先批量 Start，再串行 execute/End；
- `flamingoAgents/core/agent.py:516–526`：创建 `toolContext` 后同步调用 runtime；
- `flamingoAgents/tools/toolRuntime.py:24–38,44–67`：prepare/execute 的异常处理并不完全相同；execute 阶段才显式直通 `modelInterruptedError`；
- `flamingoAgents/core/conversation.py:190–204`：`addToolResult` 同步写 JSONL 与 messages，当前没有去重；
- `flamingoAgents/core/agent.py:110–136`：公开流在非终态阶段持有 per-session `RLock`；
- `flamingoAgents/core/agent.py:224–225`：当前每次进入 `driveModelLoop` 都 `interruptEvent.clear()`，确认续跑存在清掉 stop 的窗口；
- `webApp/backend/agentManager.py:176–202`：当前 stop 会立即 unregister + UI seal，Core 可能仍未退出；
- `agentManager.py:43–59,69–88`：active 状态消失后 cache 可被丢弃/替换，而新 Agent 拥有独立 session lock。

### 2.2 可复用能力

- 每个工具调用可创建独立 `toolContext`。
- `bash` / `askSubAgent` 已轮询 interrupt event 并终止子进程。
- `toolRuntime` 已把普通 execute 异常隔离为单个错误 `toolResult`。
- JSONL 有按路径写锁；但本方案仍禁止 worker 写 conversation，所有结果由协调线程提交。

### 2.3 不能直接复用的旧假设

- 旧 `closeUnfinishedToolCalls(startIndex)` 假定“第一个失败 index 之后都没有真实结果”；并发后该假设不成立，后序 future 可能已先成功。
- `ThreadPoolExecutor` 的 context manager 只能等待线程，不能强杀不协作的 Python callable。
- session `RLock` 只保护同一个 agent 实例；Web 若在旧 worker 退出前重建 agent，新旧锁互不相干。
- `FIRST_EXCEPTION` 不会因普通 `threading.Event.set()` 自动醒来，必须超时轮询。

## 3. 目标、非目标与假设

### 3.1 目标

1. 同一模型 step 内，连续且至少两个池内、免确认调用真正时间重叠。
2. 空池或 `maxWorkers=1` 时走原协调线程串行路径，不创建 executor。
3. 并发度严格有界，不按调用数无限建线程。
4. 权限判断先于 submit；池成员不绕过 `requireApproval`。
5. 普通工具失败只影响自身；每个 call 最终恰有一个结果。
6. stop 后等待所有已启动 worker 静止，再释放 session lock 与 Web Core 占用。
7. JSONL、messages 与发给 provider 的 tool outputs 始终按原 call 顺序提交。
8. 任意 Start/End yield 后关闭生成器，transcript 仍完整。
9. v3 用户配置继续可用且保持串行；v4 显式 opt-in。

### 3.2 非目标

- 不做跨模型 step 的后台 job / fire-and-forget。
- 不新增 taskId、任务查询 API、持久化队列或 Web 任务中心。
- 不自动推断两个文件路径、shell 命令或子代理是否资源冲突。
- 不提供跨所有 session 的进程级总线程额度；一期是每活动 batch 上限。
- 不保证 CPU 密集型工具加速。
- 不改变 provider payload 或 SSE 事件类型。
- 不按 future 完成顺序提交结果/End。
- 不宣称线程池可以硬杀任意自定义 Python 工具。

### 3.3 假设

- “工具调用池”是显式工具名 allowlist，不在列表中的工具维持串行。
- “异步”是用户可观察到执行区间重叠，不要求 public API 变成 `async def`。
- 用户把副作用工具加入池，即承担共享文件、端口和外部系统冲突责任；系统无法可靠分析任意 shell。

## 4. 配置设计与兼容策略

### 4.1 `tools.yaml` v4

```yaml
version: 4

parallelToolPool:
  maxWorkers: 4
  toolNames: []       # 默认空池，不偷偷改变行为

tools:
  # 保持现有 schema/permissions
```

严格规则：

- `parallelToolPool` 整块可省略；省略等价 disabled。
- 块存在时只允许 `maxWorkers`、`toolNames` 两个键，且两者必填。
- `maxWorkers` 必须是非 bool 的整数 `1..32`。
- `toolNames` 必须是无重复的字符串数组，每个名称存在于 `tools[].name`。
- `maxWorkers=1` 或空 `toolNames` 都是真禁用：不创建 executor，执行线程/顺序与现状一致。
- `createAgent(toolNames=[...])` 的工具白名单生效后，池成员取交集。

### 4.2 v3/v4 兼容

- `version: 3`：按当前格式解析，pool 强制 disabled；若出现 `parallelToolPool`，明确提示升级到 v4，禁止静默忽略。
- `version: 4`：解析新块；无块仍 disabled。
- 其它版本：拒绝。

项目模板升 v4 但默认空池。运行时配置在 `~/.flamingo/config/tools.yaml`，`ensureUserConfig()` 不覆盖已有文件，因此老用户保持 v3/串行，必须手工升级并加入名称才启用。

### 4.3 内存结构与直接构造兼容

```python
@dataclass(frozen=True)
class parallelToolPoolSettings:
    maxWorkers: int = 1
    toolNames: frozenset[str] = frozenset()


@dataclass
class toolSettings:
    toolSchemas: list[toolSchemaSpec]
    parallelToolPool: parallelToolPoolSettings = field(
        default_factory=parallelToolPoolSettings
    )
```

`agent.__init__` 新参数放在末尾并有安全默认：

```python
parallelToolNames: frozenset[str] | None = None
maxParallelTools: int = 1
```

构造时复制为 `frozenset` 并严格验证 `maxParallelTools`。不修改 `toolDefinition`：池资格是部署策略，不是工具永久属性。

## 5. 调度语义

### 5.1 权限边界保持

仍从 `startIndex` 收集连续的“未知或免确认”可执行前缀，遇到第一个 `requiresApproval` 停止。池配置不参与权限决策。

```text
[P, P, Confirm, P]
→ P1/P2 并发并完成落盘
→ confirmationRequired(call3)
→ 批次安全挂起
→ 用户批准/拒绝后才处理 call4
```

### 5.2 池外调用是顺序屏障

执行前缀按原始顺序切段：

- 最大连续池内段：长度 ≥2 且 `maxWorkers>1` 时并发；
- 长度 1 的池内段：协调线程同步执行；
- 池外调用：单项串行屏障；
- 未知工具：单项有序错误屏障。

```text
[P1,P2,S3,P4,P5]
→ concurrent(P1,P2)
→ S3
→ concurrent(P4,P5)
```

不允许 P4/P5 越过 S3，模型可能隐含顺序依赖。

### 5.3 Start、结果和 End

保留现有“整个可执行前缀先发 Start”的 UI 契约，但在**第一个 Start 之前**已经创建整个 assistant batch 的事务账本。

正常执行段：

1. worker 只返回内存 `toolResult`，不得写 conversation/JSONL；
2. executor 全部收口；
3. 协调线程按原 call index 批量 `addToolResult`；
4. 本段全部结果已落盘后，才按原序 yield End；
5. 进入下一段。

结果不按完成顺序提交。一个较快的后序调用可能等同组最慢调用后才显示 End，这是确定性和 transcript 安全的有意取舍。

## 6. 批次事务账本（核心 P0）

### 6.1 账本范围与创建时机

账本不能等进入 `driveToolBatch` 才创建。当前 `driveModelLoop` 在 `appendAssistantMessage` 之后会先 yield `usageUpdateEvent`，再调用 `driveToolBatch`；若消费者在 usage yield 处 close，assistant tool calls 已落盘但批次尚无 guard。

因此生产路径必须按以下顺序处理：

```text
校验 tool call IDs
→ 若有 calls，先创建 ledger（尚未持久化）
→ 进入覆盖 append assistant / usageUpdate yield / 整个 batch 的 try/finally scope
→ appendAssistantMessage，并标记 assistantPersisted
→ yield usageUpdate（若有）
→ driveToolBatch(..., ledger, runEvent)
→ 正常完成或安全挂起
→ finally 在 assistant 已持久化且批次非完成/非挂起时收口
```

若 `appendAssistantMessage` 自身失败，`assistantPersisted=False`，finalizer 不写没有前置 assistant 的 tool results，原异常直接传播。

在 `agent.py` 内新增私有、小驼峰结构（不新建通用框架），覆盖一个 assistant message 的全部 calls：

```python
@dataclass
class toolBatchEntry:
    index: int
    call: toolCall
    state: str                    # notStarted/running/completed/persisted
    result: toolResult | None = None


@dataclass
class toolBatchLedger:
    toolCalls: list[toolCall]
    startIndex: int
    entries: list[toolBatchEntry]
    suspendedForConfirmation: bool = False
    assistantPersisted: bool = False
    finished: bool = False
```

账本在 assistant 落盘及其后任何 usage/Start/End yield 之前创建。它按 index 跟踪状态，不用单一 `startIndex` 推断并发完成情况。直接单测 `driveToolBatch` 时若未传 ledger，内部可创建 fallback ledger；生产路径必须使用 `driveModelLoop` 外层 ledger，禁止双重 finalizer。

### 6.2 唯一 call ID 前置条件

在 `driveModelLoop` 将 assistant message 写入 conversation **之前**验证该 message 内：

- call ID 是非空字符串；
- call ID 不重复。

失败则记录协议错误并产生 error 终态，不落盘该非法 assistant message，也不执行工具。旧日志恢复若发现同一 assistant 内重复 ID，明确报完整性错误，禁止猜测配对。

### 6.3 原序且幂等的持久化 helper

将旧 suffix helper 重构为基于完整状态表/精确缺口：

```text
persisted result
  > 本轮已完成的真实 toolResult
  > interrupted/cancelled/notStarted 的合成结果
```

规则：

1. 根据目标 assistant 后紧随的 tool messages 建立已闭合 ID/count；
2. 正常可恢复形态只能是“按原序已闭合前缀 + 缺失后缀”；已闭合前缀不重复写；
3. 发现重复 ID、多条同 ID result、乱序 result，或“低 index 缺失但高 index 已持久化”的非前缀缺口，立即报会话完整性错误并 fail closed；append-only JSONL 无法靠尾部追加恢复原序，禁止假修复后继续请求模型；
4. 对**尚未持久化**但本轮 future 已完成的高 index 调用保留真实结果；finalizer 仍可从最低未闭合 index 起，按原序写取消/真实结果；
5. 其余未闭合 call 按 reason 合成取消/失败结果；
6. 一次调用中先把所有 ordered results `addToolResult` 完，再返回待 emit 的 End 列表。

`findUnclosedTailCallIndex` 改为返回“合法闭合前缀长度 + 缺失后缀”；若观察到精确缺口不是 suffix，则返回/抛完整性错误，而不是重写或乱序补齐。

### 6.4 整批 scope guard

生产路径的最外层 `try/finally` 位于 `driveModelLoop`，从 assistant append 前覆盖 usage yield 和整个 batch；`driveToolBatch` 只操作传入 ledger。直接调用 fallback 路径才由 `driveToolBatch` 自己持有同构 guard：

```text
创建 ledger
try:
  append assistant，并标记 assistantPersisted（生产路径）
  yield usageUpdate（生产路径）
  yield Starts
  执行/提交各段
  遇 confirmation：先 setPending，再 ledger.suspended=True，再 yield terminal
  正常结束：ledger.finished=True
finally:
  若 assistantPersisted 且非 finished 且非 suspended：
    等全部已启动 worker 静止
    同步持久化所有未闭合结果（finally 内绝不 yield）
```

公开包装器 `runUserMessageStream` / `continueConfirmationStream` 还必须显式保存内层生成器，并在自身 `finally` 调用 `inner.close()`；不能依赖 `for` 循环临时 iterator 的引用计数/析构来触发 ledger guard。`driveUserMessage` 的 `yield from driveModelLoop` 会继续把 close 向内传播。

这保证：

- assistant 落盘后的 usage yield 后 close；
- 第一个 Start 后 close；
- 任意正常 End 后 close，后面仍有执行段；
- 普通异常；
- `GeneratorExit`；
- stop 导致 pump close；

都不会留下悬空 call。

### 6.5 confirmation 使用同一账本

`driveConfirmation` 不能在 `takePending()` 后无保护地 yield。批准/拒绝续跑流程：

1. `takePending` 后立即建立覆盖 `currentIndex:` 的 ledger；
2. approved：Start current → execute → persist current → End；
3. rejected：persist blocked current → End；
4. 后续调用传入**同一个 ledger**给内部批次驱动方法，不嵌套创建会重复 finalization 的 ledger；
5. 任意 Start/End 后 close，outer finally 闭合 current 及所有后续；
6. 若后续遇新的 confirmation，先 setPending 并标记 suspended，保留合法挂起。

`setPending + suspended` 在 Web 中属于**暂定挂起**，最终取决于 confirmation terminal 与 stop 的 manager-level claim：

- terminal claim 成功：stop 不能再 claim/runEvent 不置位，pending 合法保留；
- stop 先 claim：runEvent 置位，pump 丢弃随后到达的 confirmation terminal 并 close outer stream；outer wrapper 的 finally 看到“本次准备过 confirmation + runEvent 已置位”，重新取得 session lock，按 confirmationId compare-and-take pending，取消 `suspended` 并以 `userStopped` 精确闭合 current+rest；
- compare 不匹配不得清除其它/更新后的 pending；
- 纯库调用方正常消费 confirmation 并 close（event 未置位）时 pending 仍保留；若显式 interrupt 后 close，则走同一撤销路径。

由此，迟到 confirm 不会在 stopped/rebuilt context 上复活已经取消的 batch。

### 6.6 preflight 与旧 helper

`closeUnfinishedToolCalls` 改为：

```text
校验现有 results 是合法原序前缀
→ 计算 missing suffix
→ 一次性按原序持久化全部缺失结果
→ 再依次 yield End
```

即使消费者在第一个 End 后 close，JSONL 已完整。`preflightRepair`、串行 stop、并行 stop、确认 stop 均复用同一“先 persist、后 emit”底层 helper。

## 7. 并发执行器

### 7.1 启用条件

```python
poolEnabled = self.maxParallelTools > 1 and bool(self.parallelToolNames)
```

只有 `poolEnabled` 且连续池内段长度 ≥2 才创建 executor。`maxWorkers=1` 必须走现有协调线程路径，不得创建单 worker executor。

### 7.2 worker 规则

```python
def runCall(entry, runEvent):
    if runEvent.is_set():
        raise modelInterruptedError('用户已停止')
    entry.state = 'running'       # 状态写入需由协调锁保护或仅返回状态，不裸共享
    return self.executeToolCall(
        entry.call,
        sessionId,
        interruptEvent=runEvent,
    )
```

更推荐 worker 返回 `(index, result/exception)`，由协调线程独占 ledger 写入，避免 worker 并发改账本。

### 7.3 外部 stop 轮询

不能只用 `FIRST_EXCEPTION`。协调线程采用短超时循环：

```python
while pending:
    done, pending = wait(
        pending,
        timeout=0.05,
        return_when=FIRST_COMPLETED,
    )
    harvest(done)
    if runEvent.is_set() or sawInterrupted:
        for future in pending:
            future.cancel()
        interrupted = True
```

随后仍等待所有 running futures 真正退出。executor join 后若 `runEvent.is_set()` 或本组见到中断，**禁止进入正常段的 persist/emit/下一段路径**；协调器把所有已收集结果留在 ledger，统一转入 batch finalizer。finalizer 保留真实完成结果、取消其余项，并在任何 End 前完成整批持久化。

结果分类：

- future 成功返回（即使 stop 后才返回）：保留真实 `toolResult`；真实副作用已发生，丢弃结果会让审计失真；
- future 抛 `modelInterruptedError`：该 call 取消，并触发组中断；
- `future.cancel()` 成功：该 call 取消；
- toolRuntime 已包装的普通错误：保留 `isError=True` 结果，不取消同组；
- wrapper 意外普通 Exception：转为该 call 的错误 `toolResult`，其它调用继续；
- `KeyboardInterrupt` / `SystemExit`：等待 worker、finalize transcript 后重新抛出。

### 7.4 executor 生命周期

executor 显式管理，且 executor 存活期间**不 yield**：

```python
executor = ThreadPoolExecutor(...)
try:
    submit/monitor/harvest
finally:
    for future in futures:
        future.cancel()
    executor.shutdown(wait=True, cancel_futures=True)
```

批次级 executor 只避免空闲持久池泄漏；它不能强杀永不返回的 running thread。加入池的自定义工具必须有界或协作取消，否则 stop/进程退出可能一直等待。这是线程模型硬限制。

### 7.5 `toolRuntime` 中断直通补齐

`prepareArguments` 当前会把 `modelInterruptedError` 包成普通错误。需改为：

```python
except modelInterruptedError:
    raise
except Exception as error:
    ...
```

execute 阶段现有直通保持。

## 8. 运行级取消令牌

### 8.1 不再在 model step 中 clear 共享 Event

删除 `driveModelLoop` 的 `interruptEvent.clear()`。每条用户流/确认流拥有一个全新的 `runEvent`，一次运行中只允许从 unset → set，不复用、不回清。

### 8.2 Core API 形态

公开方法保持返回 Iterator，但新增可选 keyword-only `runEvent`（主要给 Web 内部）：

```python
runUserMessageStream(..., runEvent: threading.Event | None = None)
continueConfirmationStream(..., runEvent: threading.Event | None = None)
```

- 调用方不传：为该流创建新 Event；
- Web 传入：pump 与 Core 持有同一个 Event；
- 流获取 session lock 后按 identity 登记为当前 run event；finally identity-safe 注销；
- `driveUserMessage`、`driveModelLoop`、`driveToolBatch`、`driveConfirmation` 和 `executeToolCall` 显式透传同一 event；
- `interruptActiveStreams(sessionId)` 保留给纯库调用，设置当前登记 event 并 shutdown adapter；
- Web pump 即使在 Core 尚未登记前 stop，也能直接 set 自己持有的 event，消除“注册后、首个 next 前”的丢 stop 窗口。

### 8.3 检查点

run event 在以下位置检查：

- 每次模型请求前；
- 每个串行工具开始前/结束后；
- 并发 submit 前、worker 入口、wait 轮询、组结束后；
- confirmation approved current 结束后；
- 正常结果 persist/End 之前；
- 开始下一执行段/下一模型 step 前。

串行工具或 approved current 若在 stop 后正常返回，真实结果只先放入 ledger，不走正常 persist/End/后续段，统一进入整批 finalizer。event 一旦 set，本轮不再执行新工具、不再请求模型。

## 9. Web stopped 与 Core draining 生命周期（核心 P0）

### 9.1 两个完成信号

`streamPump` 区分：

- `doneEvent`：UI 已收到 terminal/stopped，订阅已封口；保持现有前端即时体验。
- `stopDispatchDone`：stop owner 已完成 adapter shutdown 等唤醒动作；用于阻止旧 pump 先注销、新 pump 启动后又被旧 stop 误 shutdown。
- `coreDoneEvent`：`stream.close()` 已返回、所有 executor 已 join、transcript finalization 与 usage 收尾完成、Core session lock 已释放/即将退出。
- `terminalSeen`：正常 terminal 已广播；此后迟到 stop 不得再追加 stopped 或 shutdown 下一轮。

UI done 不能再代表可丢 agent 或可启动独立新 agent。

### 9.2 stop 顺序

`requestStop()` 改为：

```text
managerLock 内取得当前 pump，并原子 claim stop owner
→ claim 阶段只做：检查 terminalSeen/幂等、stopFlag.set、runEvent.set、stopDispatchDone.clear
→ 释放 managerLock
→ stop owner 在锁外以彼此隔离的 try 块执行 adapter shutdown、usage claim、UI seal stopped
→ 任一步失败都记录诊断且不阻断后续 stop 步骤
→ outer finally 无条件 stopDispatchDone.set
→ 不 unregister activeStreams
```

泵线程 finally 使用嵌套 `try/finally`，任何 close/finalizer/usage 异常都不能跳过生命周期清理：

```text
try:
  try stream.close()            # 触发 batch guard，可能等待 worker
  finally usage finalization    # close 抛错也必须尝试，二者错误分别记录
finally:
  若 stopFlag：wait stopDispatchDone（不持 managerLock）
  finishStream(sessionId, expectedPump)
```

`finishStream` 在同一个 `managerLock` 临界区中 identity-check、从 `activeStreams` pop，并 `coreDoneEvent.set()`；不得先 pop 再锁外 set event，避免 gate 看到假 idle 的 TOCTOU 窗口。

adapter shutdown、usage、UI seal 和 `stream.close()` 任一失败都要分别记录诊断；stop owner 的 outer finally 必须 set `stopDispatchDone`，泵 cleanup 的 outer finally 必须 `finishStream`。不得静默伪装 transcript 成功，但生命周期信号永远不能因诊断路径失败而遗失。

绝不在 `requestStop` 中提前从 `activeStreams` 移除。`stopDispatchDone` 保证旧 adapter shutdown 完成前旧 pump 不能注销，因而新 pump 不会被迟到 shutdown 误伤。

### 9.3 active/draining 对 cache 的约束

Core 未完成期间，`activeStreams` 仍保留原 pump，因此：

- `hasActiveStream`、删除会话、`dropAgentIfIdle` 继续视为 active；
- `getAgent` 若存在 pump，必须返回 `pump.agent`，即使 session 被标 stale；
- `dropAgent` 对 active/draining session 不得直接 pop，可返回 False 或只标 stale；
- `invalidateAllAgents` 可标 stale，但下次重建必须等旧 pump unregister；
- `finishStream` 必须比较 expected pump identity，并在同一 managerLock 内 pop + set coreDone，防旧 finally 删除后来注册的新 pump或暴露假 idle。

这关闭“旧 worker + 新 Agent 独立 session lock”竞态。

### 9.4 新请求的宽容闸

在 Web 创建 agent/generator 之前调用 manager gate：

```text
idle                                      → 继续
active 且无 stop/terminal claim           → 立即 409
stopFlag / terminalSeen / doneEvent 任一置位 → 视为 draining，锁外 wait coreDoneEvent 最多 2s
  完成                       → 重新 getAgent（此时 stale 可安全重建）并创建新 runEvent/stream
  超时                       → 409，请稍后重试
```

等待期间不得持 `managerLock`。正常 terminal 与 stop 的归属也必须原子竞争：泵准备广播 terminal 时，在 managerLock 内执行 `claimTerminal(expectedPump)`（仅当 identity 匹配且 stop 尚未 claim 时置 `terminalSeen`）；`requestStop` 在同一锁内 claim stop。谁先 claim 决定唯一终态，锁外才 broadcast/seal/shutdown。

即使 gate 返回 idle，最终 `startStream` 仍在 managerLock 内完成 **claim-before-start**：先复查无 active、构造 pump、注册 identity，随后才启动线程；绝不先启动再 claim。两个新请求竞争时只有一个能注册。

`chat/stream` 与 `chat/confirm` 都走该 gate。若 claim 失败，调用方关闭尚未开始的 generator；此时没有 pump 线程。若 `thread.start()` 抛异常，`startStream` 必须在 managerLock 内 identity rollback + set coreDone，锁外 close generator 并重新抛出；缓存中的无活动 agent 可保留，不能留下 active/pump/thread/runEvent 泄漏。

### 9.5 attach / delete / model switch

- attach 在 draining 时可回放 stopped history 并立即收哨兵；Core 未结束不代表 UI 仍活跃。
- delete 在 draining 时继续 409，禁止删除仍可能被旧 Core 写入的 JSONL/images。
- session model switch 在 draining 时继续 409；端点当前会先更新索引再调用 `dropAgentIfIdle`，因此 active 分支必须同时将 session 标 stale，确保 Core 完成后下一次 `getAgent` 重建，而不是继续复用旧模型 agent。
- 全局 model config invalidate 不替换 draining agent。

## 10. stop、部分完成与 transcript 闭环

stop 发生时可能是：

```text
call1 已成功
call2 运行中后响应中断
call3 已先成功（高 index）
call4 尚未开始
call5 属于后续串行段
call6 尚待确认
```

账本 finalization 必须得到：

- call1/call3：真实结果；
- call2/call4/call5/call6：各一个 `cancelled: userStopped` 结果；
- 按 call1..call6 原序一次性落盘；
- 每个 ID 恰一条 result；
- 不再请求模型；
- worker 全部退出后才让 Web Core occupancy 消失。

不能用“第一个中断 index 到末尾全部取消”，否则会覆盖 call3 的真实结果。

### 10.1 finally 只持久化，不 yield

发生 `GeneratorExit` 时，Python 禁止 generator 在 finally 继续 yield。scope guard 必须只同步等待、构造和落盘；可见 End 丢失由 UI stopped/连接关闭语义承担，历史刷新从 JSONL 恢复完整终态。

### 10.2 reason

保留现有：

- `userStopped`
- `preflightRepair`
- `crashRecovered`

新增内部关闭原因：

- `streamClosed`：无 stop event 的中途 close；
- `batchFailed`：非预期批次异常。

所有 reason 都生成明确 `details.cancelled=True` / `details.reason`；普通工具错误仍保留自己的真实 error result，不伪装成取消。

## 11. 权限确认语义

| 调用序列 | 行为 |
|---|---|
| `[P,P]` | 两个并发 |
| `[P,P,Confirm]` | P/P 完成并落盘，再安全挂起 Confirm |
| `[Confirm,P,P]` | 先 Confirm；批准 current 后，余下 P/P 并发 |
| `[P,Confirm,P]` | P 完成；Confirm；批准/拒绝后才处理最后 P |
| 确认调用名也在 pool | 单次 approved current 不为制造线程而入池；后续连续池成员正常分组 |

拒绝仍写 blocked result，不执行真实函数。setPending 必须先于 confirmation terminal yield，ledger 随后标记 suspended，scope guard 才不会把合法挂起误取消。

## 12. 工具安全分级

| 工具 | 建议 | 原因 |
|---|---|---|
| `read` | **唯一默认推荐候选** | 只读、I/O 型；仍需接受读到并发外部写的普通文件系统语义 |
| `write` | 默认禁止建议 | 同路径最后写覆盖，不同路径才相对安全 |
| `edit` | 默认禁止建议 | 多次 edit 可能都基于旧内容，产生丢更新/匹配失败 |
| `bash` | 默认禁止建议 | 可任意读写、占端口、启服务，无法静态推断冲突 |
| `askSubAgent` | **高级风险 opt-in，不列推荐示例** | 默认继承相同 workDir，tools 也不是硬只读沙箱；并发写与 provider 限流风险高 |

推荐起步仅为：

```yaml
parallelToolPool:
  maxWorkers: 4
  toolNames:
    - read
```

若未来要安全推荐 `askSubAgent`，应先实现至少一项可执行约束：每 call 独立 worktree/workDir、硬只读工具策略、或资源锁。仅靠文档提示不足以提供隔离保证。

## 13. Fire-and-forget 为什么不是本方案

若“异步”意为提交工具后立即请求模型、以后再补结果，当前架构不能靠线程池安全实现：

1. provider 下一请求要求 function call/output 配对；
2. conversation replay/preflight 以成对 transcript 为基础；
3. 后台任务跨越 session lock，会与后续消息写同一工作区；
4. 权限确认必须在执行前暂停；
5. 崩溃恢复需要持久 job store、租约、幂等和取消；当前均不存在；
6. SSE 终态后没有协议向原 turn 回填迟到结果。

真正 fire-and-forget 是独立的“持久任务系统 + taskId + 状态 API/SSE + callback/resume + 工作区隔离”项目。

## 14. 影响文件与精准改动

| 文件 | 计划改动 |
|---|---|
| `flamingoAgents/tools/toolConfig.py` | v3/v4；pool dataclass/default factory；严格校验 |
| `flamingoAgents/builder.py` | 白名单过滤后的 pool 配置注入 agent |
| `flamingoAgents/core/agent.py` | run event 透传；ID 校验；ledger/scope guard；执行段；executor；原序持久化；精确缺口修复 |
| `flamingoAgents/tools/toolRuntime.py` | `prepareArguments` 的 `modelInterruptedError` 直通 |
| `flamingoAgents/core/conversation.py` | 恢复时同 assistant call ID 完整性校验；必要的精确 closed-call 查询辅助（保持 JSONL schema 不变） |
| `webApp/backend/agentManager.py` | runEvent、UI done/Core done 分离、draining 保留、identity unregister、cache 栅栏 |
| `webApp/backend/server.py` | Core idle gate；完成后重新 getAgent/建 stream；user/confirm 均使用 run event |
| `config/tools.yaml` | 模板升 v4、默认空池 |
| `config/README.md` | v3/v4、opt-in、用户配置不自动覆盖 |
| `README.md` | 屏障并发、停止和安全限制 |
| `docs/addCallableToolFunction.md` | 池内工具线程安全/有界/协作取消契约；修正相关旧配置示例 |
| `docs/webApiSpec.md` | stopped UI 终态与 Core draining/409 窗口说明 |
| `tests/testParallelToolPool.py` | 新建配置、并发、事务、stop、确认、线程回收测试 |
| `tests/testLiveUsageUpdate.py` | Web pump draining/cache/usage 竞态回归 |
| `tests/testAdapterFactory.py` | builder 透传与白名单交集（若公共路径测试需要） |

不改：

- `toolDefinition.py` callable 协议；
- model adapters/provider payload；
- SSE 事件类型和前端事件协议；
- conversation JSONL 事件 schema；
- 内置工具业务实现。

所有修改代码文件提升小版本、更新文件头 Description。新建测试文件采用小驼峰 `testParallelToolPool.py` 并带规定文件头。

## 15. 测试矩阵与成功标准

全部使用 `uv run pytest`、fake adapter/tool，不访问真实模型或用户 HOME。

### 15.1 配置与构造

1. v3 正常解析，pool disabled。
2. v3 带 pool 报升级提示。
3. v4 无块、空 names 均 disabled。
4. v4 合法配置解析。
5. pool 缺字段、多余字段拒绝。
6. `maxWorkers` 为 bool/0/33/string 拒绝。
7. names 非数组、非字符串、重复、未知名拒绝。
8. `toolSettings(toolSchemas=...)` 旧式直接构造仍成功。
9. `agent(...)` 旧参数直接构造仍成功。
10. builder 工具白名单与 pool 取交集；SDK `validTools=[]` 后池为空。
11. 非空 names + `maxWorkers=1` 不构造 executor，线程与事件序同空池。

### 15.2 真并发、上限与屏障

1. 两 pooled fake 工具用 Barrier 断言区间重叠、`maxActive=2`。
2. 四调用、workers=2，峰值不超过 2 且全部完成。
3. 单项池段不创建 executor。
4. `[P1,P2,S3,P4,P5]`：两组各自重叠，S3 前后均为硬屏障。
5. unknown 是错误屏障，不 submit。
6. 普通错误只影响自身。
7. worker 不调用 `addToolResult`；所有落盘发生在协调线程。
8. P2 先完成但 JSONL/messages/End 仍 P1、P2。

### 15.3 run event 与 executor

1. stop 只 `event.set()`、没有 future 抛异常，wait 轮询仍取消 queued futures。
2. worker 入口遇已 set event 不执行副作用。
3. `prepareArguments` 抛 `modelInterruptedError` 触发组停止。
4. running future 在 stop 后正常返回：保留真实结果，但不启动后续段/模型。
5. high-index 先成功、low-index 后中断：高 index 真实结果保留。
6. normal/interrupt/submit error/GeneratorExit 后均无残留 `flamingoTool*` 线程。
7. 不协作但有界 fake 工具：UI 可先 stopped，Core 等自然返回，不提前释放占用。
8. 明确不写“永不返回”自动测试；契约说明其会永久阻塞，线程池无法硬杀。

### 15.4 任意 yield 边界关闭

参数化覆盖：

1. assistant 落盘后的 `usageUpdateEvent` 后立即 close；
2. 每一个 Start 后立刻 `stream.close()`，并断言公开 wrapper 显式关闭内层 generator；
3. 每一个正常 End 后 close，且后面仍有串行/并发段；
4. approved Start 后 close；
5. approved End 后 close；
6. rejected End 后 close；
7. 第一个取消 End 后 close；
8. confirmationRequired 后 close，pending 必须保留而非取消。

除合法 pending 外，逐条读取原始 JSONL，断言该 assistant 每个 call ID 恰一条 result。

### 15.5 stop、部分完成与精确修复

1. 已完成/运行中断/queued/后续屏障/待确认混合批次得到真实/取消结果矩阵。
2. finalizer 重入两次不重复写。
3. 合法部分持久化为 call1 已有、call2+ 缺失：只按原序补 suffix。
4. 非前缀状态 call1 缺失、call2 已有，以及乱序/重复 result：明确完整性错误，禁止继续模型，不向 JSONL 追加伪修复。
5. stop 轮不请求模型。
6. executor 全部退出后下一线程才能取得同 Agent session lock。
7. `bash`/`askSubAgent` 既有中断测试继续通过。
8. malformed duplicate call IDs 在执行/落盘前失败；历史重复 ID 明确报完整性错误。

### 15.6 confirmation

1. `[P,P,Confirm]`：P/P 并发完成后才挂起。
2. `[Confirm,P,P]`：确认前无执行；批准 current 后 P/P 并发。
3. 拒绝 current 后 P/P 按现有语义执行。
4. pool 工具某次参数命中 permission，不提前 submit。
5. approved current 正常返回同时 stop，不进入余项或模型请求。

### 15.7 Web draining 与 cache（P0）

1. running pool → stop：`doneEvent` 可先 set，但 `coreDoneEvent` 在 worker/stream close 后才 set。
2. draining 期间 `hasActiveStream=True`、delete 409、dropAgentIfIdle=False。
3. stop → invalidateAllAgents → 立即消息：旧 worker 退出前不创建新 agent；完成后重建 stale agent。
4. stop → model switch/drop 尝试：不替换旧 agent，不产生两把并行 session lock。
5. gate wait 不持 managerLock；另一 session 可正常 start/stop。
6. `finishStream` 的 identity-check + pop + coreDone 同锁原子；旧 pump finally 不删除新 pump，也不存在 pop/set 之间假 idle。
7. stop owner 在 adapter shutdown 前被 gate；构造延迟 shutdown 竞态，断言新 pump 绝不在旧 `stopDispatchDone` 前启动，也不被误中断。
8. adapter shutdown/usage/UI seal/`stream.close()`/finalizer 分别注入异常时，stopDispatchDone 与 coreDone 仍必达，active 占用不泄漏并记录诊断。
9. terminal/stop 在 managerLock 内竞态，断言恰一个 claim 成功；正常 terminal claim 后迟到 stop 不追加 stopped、不调用 adapter shutdown；stop 先赢且 terminal 是 confirmation 时 pending 被 compare-and-take 并闭合。
10. `startStream` claim 失败与 `thread.start()` 异常都完成 generator/pump/runEvent 清理。
11. 新旧执行区间不重叠；JSONL 无重复 result。
12. draining 超过 2 秒返回 409；稍后重试成功。
13. attach draining pump 可回放 stopped + sentinel。
14. usage at-most-once 现有竞争测试继续通过。

### 15.8 回归与手测

- `uv run pytest -q tests/testParallelToolPool.py`
- 定向运行 `testLiveUsageUpdate.py`、`testRunWithInterrupt.py`、builder/config 测试。
- 并发测试连续至少 5 轮。
- `uv run pytest -q` 全量通过。
- `git diff --check`。
- Web 手测：两个慢工具先 running，墙钟接近最慢者；stop UI 即时、Core 清场后新消息成功、刷新历史无悬空卡。

### 15.9 验收红线

```text
R1. 空池/maxWorkers=1 时不创建 executor，行为与现状一致。
R2. 连续池内调用确实重叠，峰值不超过 maxWorkers。
R3. 池外工具和 confirmation 不被跨越。
R4. 成功持久化路径中每个 assistant call ID 恰一个原序 result；部分真实结果不被取消覆盖；存储 I/O 或历史完整性异常时 fail closed，不虚假承诺 exactly-once。
R5. usage/任意 Start/End/异常/stop/GeneratorExit 后 transcript 都闭合或合法 pending；不依赖 iterator 析构。
R6. worker 全部静止、stream close 与 stop 唤醒动作完成前，Web 不注销 Core 占用、不替换 agent；cleanup 异常也不得泄漏占用。
R7. event 一旦 set，本轮不再执行新工具或请求模型。
R8. 不宣称解决共享资源冲突、全局限流、CPU 并行或强杀任意线程。
```

## 16. 分阶段实施 TODO lists

### Phase 0：基线与契约冻结

- [ ] 记录实施时 HEAD、`git status --short`、全量 pytest 基线，避开既存未提交文件。
- [ ] 冻结语义：连续池段、屏障汇合、原序提交、整批 ledger。
- [ ] 冻结生命周期：run event 单调置位；UI done ≠ Core done；draining 阻止 agent 替换。
- [ ] 冻结配置：v3 串行兼容、v4 opt-in、模板空池、每批 `1..32`。
- [ ] 确认不做 fire-and-forget、完成序 End、全局线程池、资源冲突推断。

### Phase 1：配置层（先红后绿）

- [ ] `toolConfig.py` 增加 disabled default factory 与 v3/v4 parser。
- [ ] 实现 pool 精确键、类型、范围、重复/未知名校验。
- [ ] `config/tools.yaml` 升 v4，默认空池。
- [ ] 完成 §15.1 配置单测。
- [ ] 更新文件头版本与 Description。

### Phase 2：run event 与 Core 签名

- [ ] 为每条 user/confirmation stream 创建或接收独立 run event。
- [ ] 公开流 wrapper 显式持有 inner generator，并在 finally close，结构性传播 GeneratorExit。
- [ ] 将 event 显式透传到 model loop、batch、confirmation、execute context。
- [ ] 删除 `driveModelLoop` 中的 `.clear()`；新增模型前/段前后检查。
- [ ] current run event 的登记/注销使用 identity guard，保留纯库 stop API。
- [ ] `toolRuntime.prepareArguments` 增加中断直通。
- [ ] 完成 event 丢失、确认后 stop、不再请求模型测试。

### Phase 3：批次账本与串行闭环（先不并发）

- [ ] 实现 call ID 非空/唯一校验，落 assistant 前失败。
- [ ] 实现 `toolBatchLedger` 和原序幂等 persist helper。
- [ ] `findUnclosedTail...` 校验原序闭合前缀并返回 missing suffix；非前缀/乱序/重复形态 fail closed，不做 append-only 假修复。
- [ ] `closeUnfinishedToolCalls` 改先全部 persist、后 emit。
- [ ] `driveModelLoop` 在 assistant append 前创建 ledger，以外层 try/finally 覆盖 append 后的 usage yield 与整个 batch。
- [ ] `driveToolBatch` 接收外层 ledger；仅直接调用 fallback 自建 guard，覆盖所有 Start/End close且禁止双重 finalizer。
- [ ] `driveConfirmation` 用同一 ledger 覆盖 current + rest。
- [ ] 先在完全串行模式完成 §15.4/§15.5 transcript 测试。

### Phase 4：并发调度器

- [ ] 实现最大连续池内段；池外/unknown 屏障。
- [ ] `maxWorkers<=1`、空池、单项段全部内联。
- [ ] 长度 ≥2 时显式 executor；worker 只返回结果，不写 ledger/conversation。
- [ ] 0.05s wait 轮询 event，cancel queued，join running。
- [ ] 真实结果/普通错误/interrupt/cancel 的优先级收集。
- [ ] explicit `shutdown(wait=True, cancel_futures=True)`；executor 存活期间不 yield。
- [ ] 协调线程原序 persist 后原序 emit。
- [ ] 完成 §15.2/§15.3 测试并重复 5 轮。

### Phase 5：Web draining 栅栏

- [ ] `streamPump` 接收 run event，新增 `terminalSeen`、`stopDispatchDone`、`coreDoneEvent`。
- [ ] managerLock 内原子 claim stop owner并置 event；adapter/usage/UI 操作锁外执行；finally set stopDispatchDone。
- [ ] requestStop 只 UI seal，不提前 unregister；正常 terminal 后迟到 stop 幂等早退。
- [ ] `_pump finally` 用嵌套 try/finally：close/usage 异常也必须等待 stopDispatchDone；`finishStream` 同锁完成 identity-check + pop + coreDone并记诊断。
- [ ] terminal 与 stop 都在 managerLock 内原子 claim，锁外 broadcast/seal/shutdown，确保唯一终态。
- [ ] active/draining 期间 get/drop/invalidate/cache 行为按 §9.3 收紧。
- [ ] 实现不持 managerLock 的 Core idle gate。
- [ ] `chat/stream`、`chat/confirm` 在建 agent/stream 前走 gate；stopFlag/terminalSeen/doneEvent 都视为 draining，完成后重新 getAgent。
- [ ] delete/model switch/attach 行为覆盖。
- [ ] 完成 §15.7 竞态测试并维护现有 usage at-most-once 测试。

### Phase 6：自动化总验收

- [ ] 新建 `tests/testParallelToolPool.py`，文件头完整。
- [ ] 完成配置、并发、上限、屏障、顺序、普通错误矩阵。
- [ ] 完成任意 yield close、GeneratorExit、部分完成、精确缺口矩阵。
- [ ] 完成 confirmation 与 run event 矩阵。
- [ ] 完成 Web draining/cache replacement 矩阵。
- [ ] 定向测试、连续 5 轮并发测试、全量 pytest 全绿。

### Phase 7：文档与手测

- [ ] 更新 `config/README.md`：v3/v4、opt-in、用户配置不自动升级。
- [ ] 更新 README：并行池是屏障并发，不是后台 job。
- [ ] 更新 `docs/addCallableToolFunction.md`：线程安全、有界、协作取消、共享资源责任，并修正相关旧配置示例。
- [ ] 更新 `docs/webApiSpec.md`：UI stopped/Core draining 与短暂 409。
- [ ] 手测 read 并发、stop、确认、历史刷新与立即新消息。
- [ ] `askSubAgent` 只做高级风险实验，不写成默认推荐；若测试，必须显式不同 workDir。

### Phase 8：代码审核与收尾

- [ ] 全新 subagent 做实现审核：ledger、GeneratorExit、stop、draining、唯一结果、配置兼容。
- [ ] 修复后由另一全新 subagent 复审至无 P0/P1。
- [ ] 检查所有代码文件小版本/Description、`git diff --check`、工作区边界。
- [ ] 输出实施报告，记录测试、手测、限制与推荐配置。

## 17. 独立审核修订记录

### 17.1 第一轮独立审核

第一轮结论：方向可行，但原 v1.0 有 2 个 P0，不能直接实施。v1.1 已逐项修复设计：

| 审核问题 | 等级 | v1.1 处理 |
|---|---|---|
| stop 提前 unregister，stale/drop 可创建新 Agent/新锁 | P0 | 增加 UI done/Core done、draining 保留、cache 栅栏、identity unregister、Core idle gate |
| assistant 落盘后的 usage/任意 Start/End close 可留下未闭合 calls | P0 | ledger 在 assistant append 前创建；`driveModelLoop` 外层 guard 覆盖 usage + 整个 batch；finally 同步完整 persist、绝不 yield；confirmation 共用 ledger |
| FIRST_EXCEPTION 不响应外部 Event | P1 | 改 0.05s `FIRST_COMPLETED` 轮询 + event 检查 + cancel queued + join running |
| `driveModelLoop.clear()` 可吞 stop | P1 | 改每流独立 run event，单调置位，删除 step clear，显式透传 |
| prepareArguments 吞中断 | P1 | `toolRuntime.py` 纳入范围并增加中断直通 |
| suffix 取消会覆盖高 index 真实结果 | P1 | 完整 index 状态表、真实结果优先、精确 missing、幂等原序 persist |
| maxWorkers=1 仍建单线程 executor | P1 | 真禁用条件，走原协调线程；专门测试 executor 未构造 |
| executor 无法强杀 running thread | P2 | 明确硬限制；有界/协作取消契约；explicit cancel + wait；线程回收测试 |
| askSubAgent 默认共享 workDir，不应推荐 | P2 | 移出推荐，只列高级风险 opt-in；安全推荐仅 read |
| 直接构造兼容不完整 | P2 | dataclass default factory、agent 末尾默认参数、完整构造/未知键测试 |

### 17.2 第二轮有效复审

第二轮结论：无 P0，仍有 4 个 P1；v1.2 已处理：

| 审核问题 | 等级 | v1.2 处理 |
|---|---|---|
| stop 锁外步骤异常可能让生命周期信号不达 | P1 | shutdown/usage/UI 分离 try；outer finally 必达 `stopDispatchDone`；pump cleanup 必达 `coreDone` |
| start/claim 失败可能泄漏 pump/generator | P1 | 改 claim-before-start；claim 失败无线程，start 异常 identity rollback + close generator |
| stop 与 confirmation terminal 竞态可能遗留 pending | P1 | manager-level terminal/stop claim；stop 先赢时 compare-and-take pending 并精确闭合 |
| stop 后正常返回的 worker 结果处置不清 | P1 | 禁止走正常段；统一进入 finalizer，保留真实结果、取消其余 calls，不再执行后续段/模型 |

主线程额外走查并补齐：ledger 创建提前到 assistant append 前，覆盖 usage yield；公开 wrapper 显式 close inner；`stopDispatchDone` 防迟到 shutdown；`finishStream` 同锁 pop + coreDone；append-only JSONL 遇非前缀缺口 fail closed。

### 17.3 第三轮全新终审

终审结论：**无 P0/P1，可以定稿**。

## 18. 风险、缓解与回滚

| 风险 | 缓解 |
|---|---|
| 同路径 write/edit 竞态 | 模板空池；唯一推荐 read；不自动推断安全性 |
| 子代理写同一 workDir | 不列默认推荐；需独立 workDir/worktree 等可执行隔离 |
| provider 限流 | 每 batch 小上限；普通错误隔离；不承诺全局配额 |
| stop 无法强杀不协作线程 | run event + join；池内自定义工具必须有界/协作；如需硬杀改子进程模型 |
| 跨 session 总线程数增长 | 明确一期每批上限；压测后另立进程级 scheduler |
| 结果完成顺序抖动 | 协调线程按原 index 持久化与 emit |
| 新旧 Agent 并行 | draining 保留 active 占用，Core done 前禁止 cache 替换 |
| 旧配置破坏 | v3 继续串行；v4 手工 opt-in；模板不覆盖用户文件 |

回滚：

1. 将 `toolNames: []` 或 `maxWorkers: 1`，立即走原串行路径；
2. 回滚并发代码时可暂留 v4 parser 并强制 disabled，避免已升级配置无法启动；
3. JSONL schema/provider payload 不变，无历史迁移；
4. Web draining 栅栏属于独立正确性修复，若并发执行回滚也建议保留。

## 19. 实施前产品取舍

本方案推荐并默认以下选择：

1. **屏障汇合**，不是后台 job；
2. End/结果按原 call 顺序，同组较快调用等待最慢调用；
3. 模板默认空池，实际首选仅 `read`；
4. `maxWorkers` 是每 batch 上限，不是全局上限；
5. stop UI 可即时完成，但若工具不协作，Core 会继续 draining，期间同 session 新请求可能短暂 409。

若产品不接受任一项，应先重新设计，不应直接编码。
