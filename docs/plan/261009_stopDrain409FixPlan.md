# 终止即中止本轮、立即释放会话的修复方案

- Author: wilbur
- Version: 3.0
- Date: 2026-10-09
- 状态：按用户最终确认重写，停止自动复审，待用户确认后实施
- 关联报告：[`../261009_stopDrain409Incident.md`](../261009_stopDrain409Incident.md)
- 关联旧方案：本文件 v1.4 / v2.2 均已作废，**不得实施**
- 说明：v3.0 已完成针对 A/B/C 进程边界、终止时序、会话所有权和前端排队的方案重写；最后一次 subagent 复审调用被环境中断，因此不宣称“终审通过”。用户已明确要求停止继续复审。
- 范围：只落方案。用户确认前不改产品代码。

## 0. 结论与用户已确认的语义

用户要求的最终语义只有一句：

```text
点击“终止” = 真中止本轮，立刻能进下一轮。
```

用户已确认的进程边界：

- **A 类（上一轮或更早启动、仍在运行的程序）**：不在本轮进程组内，终止不触碰。
- **B 类（用 `setsid` 等另开会话的守护进程）**：不在本轮进程组内，终止不触碰。
- **C 类（被终止那一轮里用 `xxx &` 启动的后台子孙）**：在本轮进程组内，跟随本轮一起被杀。

这三条不需要额外开关，正好是 `start_new_session=True` + `killpg(本轮 pgid)` 的天然行为。方案的价值在于把它做对、做快、做安全，而不是加一层“永不杀”的拦截。

## 1. 要修什么

中断一轮后立刻发送，`POST /api/chat/stream` 返回 409，前端显示“该会话仍在收尾，请稍后再试。”

根因（已由报告确认）：

- 点击终止后 UI 立即空闲，但会话占用要等泵线程离开 `stream.close()` 才释放；
- `close()` 要等工具线程返回，并行池还要 `shutdown(wait=True)`；
- 新请求只等 `coreDoneEvent` 2 秒，超时即 409；
- 前端只静默重试“活跃流”409，收尾这条直接报错；
- 本机并行池包含 `bash` / `askSubAgent`，收尾经常超过 2 秒。

最近一次实锤：会话 `261008175246-c3db58ab` 停在 `askSubAgent`，子日志 `01:11:32Z` 截断，父会话 `userStopped` 到 `01:12:48Z` 才落盘，空窗约 76 秒。

## 2. 目标与不可违反的不变量

### 2.1 成功标准

1. 点击终止后，协作工具（bash / askSubAgent / 文件工具）在 **1.5 秒内** 离开工具函数，`coreDoneEvent` 在 **2 秒内** 置位。
2. 同会话新消息在终止后 **2.5 秒内** 能成功开流，不再收到“仍在收尾”的 409。
3. A / B 类后台程序在终止后仍存活（测试必须证明）。
4. C 类后台子孙随本轮被杀（测试必须证明）。
5. 每个 tool call 只落盘一条最终结果：真实结果或 cancelled，二选一，禁止两条。
6. 会话锁与 `activeStreams` 的所有权协议不变：旧泵 `finishStream` 后才允许新轮进入。
7. 终止后 transcript 仍闭合：顺序正确、pending confirmation 语义不变。

### 2.2 不变量

- 老泵未 `finishStream` 前，不创建同会话新 pump、不新建第二把 `sessionLock`。
- 手动终止**必须**能终止本轮进程；但只针对本轮进程组，绝不扩大到 A / B。
- 不采用“`shutdown(wait=False)` 后提前 `finishStream`，让旧 worker 在后台继续写会话”的半脱离方案。
- 不在工具返回前抢写 cancelled 结果。
- `RLock` 只由持有线程释放。
- 后端 409 保留为兜底，不删除。

## 3. 终止时序

```text
用户点击终止
  → 前端进入 stopping/stopped（UI 立即可交互）
  → POST /api/chat/stop
  → requestStop：runEvent.set + stopFlag.set + 关闭当前模型 HTTP socket
  → 当前工具（bash/askSubAgent）看到 interruptEvent：
       SIGTERM 本轮进程组
       → 短 grace（≤0.3s）
       → SIGKILL 本轮进程组
       → 有界等待 poll()/EOF，最多再 0.5s
       → 抛 modelInterruptedError
  → 并行池：cancel 未开始 future；已开始的 worker 因上面的杀组快速返回
       → 有界 join（STOP_JOIN_BUDGET，见下）
  → finalizer 闭合 transcript：未返回结果的 call 补唯一 cancelled
  → stream.close() 返回
  → finishStream：activeStreams.pop + coreDoneEvent.set()
  → 会话空闲

新消息：
  终止后 2 秒内到达 → waitForCoreIdle 等到 coreDone → 200
  极窄竞态仍 409 → 前端短退避，不弹错
```

关键点：**先杀组，再有界等待**。杀组让 `poll()` 变非空、reader 收到 EOF，工具因此能返回；有界等待只兜底“杀了但还没完全落地”的毫秒级窗口，不去等一个不存在的超时。

## 4. 后端方案

### 4.1 进程组终止：只杀本轮，A/B 不受影响

文件：`flamingoAgents/tools/builtinTools.py`。

现状问题：

- `_runWithInterrupt` 的主循环只在顶部看 `_isInterrupted`；
- `_finishStop` 固定 `TERM → sleep(0.5) → KILL → 等 EOF 0.3s`；
- 首领已退出、正在等 EOF 的半段不再看中断；
- reader 若不 EOF，只能回落到工具 timeout（bash 默认 30/上限 120，askSubAgent 默认 600/上限 3600）。

这在 `askSubAgent` 上表现为长时间占锁（事故里的 76 秒），并让 2 秒闸超时。

改法：

1. 中断置位后，**立即**对 `process.pid` 所在进程组发 `SIGTERM`，短 grace（`STOP_TERM_GRACE_SECONDS = 0.3`）后发 `SIGKILL`。两次都走现有 `_signalGroup`（`killpg` 失败再 `terminate`/`kill`）。信号必须在等待预算之前发出。
2. 信号后最多再等 `STOP_PROCESS_BUDGET_SECONDS = 0.5`，等 `poll() is not None`；到点仍未退出也照常抛 `modelInterruptedError`（进程组已被 KILL，不会变成无人管理的孤儿）。
3. 主循环每次睡眠前、以及“首领已退出、等待 EOF”的半段，都检查中断。手动终止不再回落工具 timeout。
4. reader 保持 daemon，继续排空到有界 spool；`_closeReadPipes` 仍在 `finally`，失败不得吞掉待抛异常。
5. 正常 timeout 路径保持现有语义（TERM → 0.5s → KILL），不受本次改动影响。
6. 手动终止与正常 timeout 在日志中区分 `manualStop` / `normalTimeout`，便于诊断。

为什么 A / B 安全：

- 进程组等于本轮 `bash` / `sdkEntry.py` 的会话组（`start_new_session=True`）；
- A 类进程属于更早的、已结束的调用，其 pgid 不是本轮的 `process.pid`；
- B 类由 `setsid` 另开会话，pgid 已改变，不在本轮组内。

为什么 C 会被杀：它就是本轮 shell 的后台子孙，仍在同一进程组内。这与用户确认一致。

### 4.2 并行池：有界 join，不提前释放

文件：`flamingoAgents/core/agent.py` `executeConcurrentSegment`。

现状：中断后只 `future.cancel()`，随后仍 `executor.shutdown(wait=True, cancel_futures=True)`；已开始的 worker 不返回就永久堵住协调线程。

改法：

1. 停止时先 `cancel()` 未开始的 future。
2. 已开始的 worker 依赖 4.1 的杀组自行返回（协作工具 < 1s）。
3. 协调线程最多再等 `STOP_JOIN_BUDGET_SECONDS = 2.0`（`wait(pending, timeout=0.05)` 循环，不新增依赖）。
4. 预算内全部结束：保持现有 harvest / ledger 语义。
5. 预算耗尽仍有未结束 worker：**不** `shutdown(wait=False)` 提前释放，**不**放新轮进入。继续按现有语义等待并记录 `stopJoinOverrun` 诊断；此时会话保持 draining。
   - 触发条件仅限“自定义工具、无子进程、且不看 `interruptEvent`”这类无法安全终止的情况。
   - 这种情况下 UI 明确显示“后台工具仍在收尾”，不伪装为 idle。
6. 正常完成路径（未置位 `runEvent`）仍 `shutdown(wait=True)`，批末屏障与原序落盘不变。

说明：第 5 步是唯一“需要等待”的例外，且不适用于本机 `tools.yaml` 里的任何内置工具。内置工具全部能在预算内被杀组终止。它存在只为防止“为了立刻释放而制造并发写会话”的退步。

### 4.3 唯一终结点

现有 `finalizeToolBatchLedger` / `persistUnclosedResults` 已实现“有结果用结果、无结果补 cancelled”。规则写死：

1. 工具函数返回真实 `toolResult` → 只落这一条，不再补 cancelled、不改标 stopped。
2. 工具未返回结果（被杀组后抛 `modelInterruptedError`、异常、被丢弃）→ 补唯一 cancelled/stopped。
3. 手动终止不会在阻塞调用进行中投递 `GeneratorExit`，因此协调线程不得抢在工具返回前写 cancelled。
4. 迟到结果不得二次落盘。

不需要新增 `sealed` / `writeLock` 大改：本轮不提前释放，协调线程始终是唯一会话写入者，旧 worker 不会在会话已释放后继续写。

### 4.4 会话探针

新增独立只读接口，避免前端盲重试：

```json
GET /api/sessions/{sessionId}/stream-state
{
  "state": "idle" | "active" | "draining",
  "canAcceptMessage": true | false
}
```

- 只读 `activeStreams` / pump 状态，不创建 Agent，不读写 JSONL；
- `idle` 只在 `managerLock` 内确认无当前 pump 且 `finishStream` 完成后返回；
- `finishStream` 必须 identity-check；
- 不返回 PID / 命令行 / token；
- 不复用会跑 git、读取 usage 的状态栏接口，旧 `getSessionStatus` 行为不变。

## 5. 前端方案

文件：`webApp/frontend/js/chatView.js`。

终止后释放通常 < 1s，前端只需覆盖极窄窗口和跨窗口场景，不需要长等待。

1. `stopping` / `draining` 时，`send()` 不直接 POST；进入 per-session FIFO `pendingSend` 队列并提示“上一轮正在收尾，随后发送”。
2. `confirm()` 同样排队为 `pendingConfirm`。confirm 必须先于队列 send 执行：Core 在存在未决确认时对 `runUserMessageStream` 回 `pendingConfirmationExists`；这不是插队。
3. 通过 `stream-state` 探测到 `idle` 后：先校验并处理 pendingConfirm（失效则丢弃并提示），再从队首取一条发送，只发一次。
4. 短退避兜底：若仍 409，按 300/600/1200ms 最多重试 3 次，且只在“payload 从未被接受”时重试；不并发、不重放已成功请求。
5. 切换会话按 `sessionId` 分桶；其他窗口是否占用以服务端 `stream-state` 为准，不能从本地 `goIdle()` 推断。
6. 刷新默认丢弃未发送的 pending；不静默重放。
7. 收到 `stopped` 广播只表示 UI 终态，仍需以 `stream-state` 判断可否发送。

## 6. 边界与例外

| 场景 | 行为 |
|---|---|
| 当前工具是 bash / askSubAgent | 杀本轮进程组，快速返回，立即释放 |
| A 类：更早轮次遗留进程 | 不在本轮组，存活 |
| B 类：`setsid` 守护进程 | 不在本轮组，存活 |
| C 类：本轮 `&` 后台子孙 | 在同一组，随本轮被杀（符合确认） |
| 仅模型输出、无工具 | 关闭模型 socket，正常收尾 |
| 自定义工具、无子进程、不看 `interruptEvent` | 无法安全终止，`STOP_JOIN_BUDGET` 后保持 draining 并记录诊断 |
| 正常 timeout（未手动终止） | 保持既有 timeout 语义 |
| 多窗口 / attach | 以服务端 `stream-state` 为准，409 兜底 |
| 删除 / 切模型 | draining 期间继续 409 |

## 7. 风险

| 风险 | 处理 |
|---|---|
| kill 误伤 A / B | 只杀当前 `process.pid` 进程组；A/B 不同组。测试覆盖 `setsid` 与上一轮遗留进程 |
| C 被误认为“不该杀” | 用户已确认 C 跟随本轮；文档显式声明 |
| 杀组后进程组仍残留（D 状态等） | 有界等待后照常返回；进程组已收 KILL，不做无限等待；记录诊断 |
| 提前释放导致并发写 | 不采用 `shutdown(wait=False)`；预算耗尽保持 draining |
| 重复 tool result | 4.3 唯一终结点；测试断言真实/取消各一条 |
| 前端重复发送 | 只在未接受时重试；FIFO + sessionId 分桶；刷新不静默重放 |
| 旧客户端仍 409 | 保留 409 兜底；`stream-state` 只服务新客户端，非回归 |

## 8. TODO

- [ ] T1. `_runWithInterrupt` 中断路径改为：立即 TERM → 短 grace → KILL 本轮进程组 → 有界等待 → 抛 `modelInterruptedError`。信号先于等待发出。
- [ ] T2. 主循环与 EOF 等待半段都检查中断；手动终止不回落工具 timeout。正常 timeout 语义不变。
- [ ] T3. 日志区分 `manualStop` / `normalTimeout`，新增 `stopJoinOverrun`、`stopProcessOverrun` 诊断。
- [ ] T4. `executeConcurrentSegment` 停止时取消未开始 future，已开始 worker 依赖杀组返回；有界 join 2s；预算耗尽保持 draining，不 `shutdown(wait=False)`、不提前 `finishStream`。
- [ ] T5. 固化唯一终结点规则（4.3），补真实结果/取消结果不重复的断言。
- [ ] T6. 新增 `GET /api/sessions/{sessionId}/stream-state`，锁内 identity-check；旧 status 不变。
- [ ] T7. 前端 per-session FIFO `pendingSend` + `pendingConfirm`；confirm 优先；短退避兜底；切会话分桶；刷新不静默重放；`stopped` 不直接 `goIdle`。
- [ ] T8. 更新 `docs/webApiSpec.md`（终止语义、进程组边界、stream-state）与 README 停止说明。
- [ ] T9. 改动文件按项目规则递增文件头 version / Description。

## 9. 验证矩阵

### 9.1 终止即刻释放

1. 工具为 bash，启动长时间前台命令。点击终止。
2. 断言工具 1.5s 内返回、`coreDoneEvent` 2s 内置位、会话 2.5s 内可开新流。
3. 断言新轮 userMessage 只落一条。

### 9.2 A / B / C 边界

1. C：本轮 `bash -lc "sleep 300 & echo $!"` 记录 PID；终止后该 PID 应消失。
2. B：`setsid sleep 300 &` 记录 PID；终止后该 PID 应存活。
3. A：上一轮遗留的后台进程 PID；本轮终止后应存活。
4. 断言终止调用链只对当前 `process.pid` 进程组发信号。

### 9.3 askSubAgent 停止

1. 复现 `261008175246-c3db58ab` 场景：`askSubAgent` 执行中终止。
2. 断言子进程组被杀、父工具快速返回、不再出现 76s 空窗。
3. 断言父会话落一条 cancelled 或真实结果（二选一）。

### 9.4 并行池

1. 两个池内 bash，其中一个忽略 SIGTERM。
2. 终止后断言 2s 内 `coreDoneEvent` 置位（KILL 兜底）。
3. 断言结果原序、每个 call 一条结果。

### 9.5 不协作工具例外

1. 自定义工具 `release.wait()`，不看 `interruptEvent`。
2. 终止后断言会话保持 draining、不提前释放、无并发写；`release.set()` 后恢复 idle。

### 9.6 前端

1. draining 时发送进入队列，不发 POST；idle 后只发一次。
2. pendingConfirm 先于 send，失效确认被丢弃并提示。
3. 切换 A/B 队列不串；刷新不静默重放；无 status 异常不触发退避。

### 9.7 回归

- `tests/testRunWithInterrupt.py`
- `tests/testParallelToolPool.py`
- `tests/testLiveUsageFrontend.py`
- 会话恢复 / attach / usage / 模型请求既有用例

## 10. 实施顺序

1. T1–T3：先让终止快速、有界、可诊断。验证 9.1 与 9.3。
2. T4：并行池有界 join + 不提前释放。验证 9.4、9.5。
3. T5：唯一终结点断言。
4. T6：stream-state 探针。
5. T7：前端队列与退避。
6. T8–T9：文档与文件头。

任一步验证失败就停在该步，不跳过安全不变量。
