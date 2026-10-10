# 中断后新消息 409「该会话仍在收尾」排查报告

- Author: wilbur
- Version: 1.0
- Date: 2026-10-09
- Scope: **只排查、不改代码。**
- Symptom: 中断某一轮后立刻发新消息，前端报「该会话仍在收尾，请稍后再试。」；服务端日志为 `POST /api/chat/stream HTTP/1.1 409 Conflict`。

---

## 0. 结论

这不是偶发误报，是停止路径的设计缺口被前端放大成了硬错误。

1. 点停止后，UI 立刻收成「已中断 / 可发送」。后端并没有在这一刻释放会话。
2. 会话占用要等泵线程跑完 `stream.close()` 才释放。`close()` 会等已启动的工具返回；并行池里的工具还要等 `ThreadPoolExecutor.shutdown(wait=True)`。
3. 新消息在开流前只等这个收尾 **2 秒**。超时就返回 409，文案正好是「该会话仍在收尾，请稍后再试。」
4. 前端静默重试只认另一句 409「该会话已有活跃流」。收尾这句 **不重试**，直接弹给用户。
5. 本机 `~/.flamingo/config/tools.yaml` 把 `bash` 和 `askSubAgent` 放进了并行池。停止这两类工具时，收尾经常远超 2 秒。
6. 最近一次实锤：会话 `261008175246-c3db58ab` 在 `askSubAgent` 执行中被停止。子进程日志在 `01:11:32Z` 就断了，父会话的取消结果却到 `01:12:48Z` 才落盘，空窗约 **76 秒**。这 76 秒里同会话新消息必然 409。

「已有活跃流」和「仍在收尾」是两条不同的 409。用户看到的是第二条。

---

## 1. 两条 409 不是一回事

`POST /api/chat/stream` 在真正开流前调用 `agentManager.waitForCoreIdle()`：

```text
webApp/backend/server.py chatStreamSync
  idleState = agentManager.waitForCoreIdle(sessionId)
  idleState == 'active'  → 409「该会话已有活跃流，请稍后再试。」
  idleState == 'timeout' → 409「该会话仍在收尾，请稍后再试。」
```

`chat/confirm` 走同一闸，同一对文案。异常处理器把 `HTTPException.detail` 收成 `{"error": "..."}`，所以前端 `error.message` 就是这句中文，uvicorn 访问日志只剩 `409 Conflict`。

闸的判定在 `webApp/backend/agentManager.py`：

| 泵状态 | 返回 | HTTP |
|---|---|---|
| `activeStreams` 里没有这只泵 | `idle` | 继续开流 |
| 有泵，但还没 stop / terminal / done | `active` | 立刻 409「已有活跃流」 |
| 已 stop 或已终态，且 `coreDoneEvent` 在超时内置位 | `idle` | 继续开流 |
| 已 stop 或已终态，等待超时（默认 **2.0 秒**） | `timeout` | 409「仍在收尾」 |

`CORE_IDLE_WAIT_SECONDS = 2.0`。等待在 `managerLock` 外进行，不会把整个进程锁死，但 **只等这一次**。超时后旧泵继续占着 `activeStreams`，下一次请求再等 2 秒，再超时再 409。只要收尾没结束，连点发送只会反复 409。

契约写的是同一语义：`docs/webApiSpec.md` v1.27，以及 §4.1「停止后 UI 可能已 stopped，但 Core/worker 仍在 draining；最多等 2s，超时仍 409」。README 也写了「不协作的池内工具无法硬杀，同会话新请求在 draining 期间可能短暂 409」。这次的现象是这条契约的用户可见面，不是新的未知状态码。

---

## 2. 为什么 UI 已经停了，后端还占着会话

停止被故意拆成两个完成信号。

```text
用户点停止
  → chatView.stop()
       phase = stopping
       立刻标「已中断」、定格工具卡、解锁输入框观感
       fire-and-forget POST /api/chat/stop
       立刻 abort 本窗口 SSE
  → agentManager.requestStop()
       claim stop owner
       runEvent.set() + stopFlag.set()
       adapter.interruptActiveStreams()   # 只掐模型 socket
       _sealStopped()                      # doneEvent：UI 终态
       不从 activeStreams 移除
  → 泵线程 finally
       stream.close()                      # 这里才等工具 / 线程池
       _recordUsage()
       stopDispatchDone.wait()
       finishStream()                      # 这时才 pop + coreDoneEvent.set()
```

对应代码：

- 前端：`webApp/frontend/js/chatView.js` `stop()`。`abort()` 是同步的，输入框在 stop POST 返回前就可以再发。
- 后端占位：`requestStop()` 明确 **不** `unregisterStream`。`finishStream()` 才在同一把 `managerLock` 里 identity-check、`activeStreams.pop`、`coreDoneEvent.set()`。
- 设计原文：`docs/plan/260920_parallelToolPoolPlan.md` §9。目的是避免「UI 已停、旧 worker 还在写 jsonl 时，新建另一把 session lock」。

所以时间线是：

```text
T0  点停止。UI 已空闲。doneEvent 已置位。activeStreams 仍有旧泵。
T0+ 用户发送。waitForCoreIdle 看到 draining，开始等 coreDoneEvent。
T0+2s  coreDone 仍未置位 → 409「仍在收尾」。
Tn  工具线程终于返回，stream.close() 返回，finishStream 放锁。
Tn 之后的下一次发送才能 200。
```

`POST /api/chat/stop` 自己很快返回 `{stopped: true}`。它只表示「停止已受理」，不表示「会话已可复用」。访问日志里先有一条 stop 200、紧接着 stream 409，与这条时序一致。

---

## 3. 收尾为什么会超过 2 秒

`runUserMessageStream` 在 **整段** `driveUserMessage` 期间持有 `sessionLock`。泵线程 `break` 之后调用 `stream.close()`，生成器的 `finally` 才会 `inner.close()`。`driveToolBatch` / `driveModelLoop` 的 `finally` 调 `finalizeToolBatchLedger()`。在此之前，调用栈还停在工具执行里。

两条会把 `close()` 拖过 2 秒的路径：

### 3.1 并行池：`shutdown(wait=True)` 不认中断

当前用户配置 `~/.flamingo/config/tools.yaml`：

```yaml
parallelToolPool:
  maxWorkers: 4
  toolNames:
    - read
    - write
    - edit
    - bash
    - askSubAgent
```

仓库模板 `config/tools.yaml` 的 `toolNames` 是空的，池默认关闭。本机是显式 opt-in。因此同一条 assistant 消息里连续、免确认的这些工具会进 `executeConcurrentSegment()`。

该函数在 `finally` 里：

```text
future.cancel()
executor.shutdown(wait=True, cancel_futures=True)
```

`cancel_futures=True` 只能取消 **还没开始** 的 future。已经在跑的 worker 必须自己返回，`shutdown(wait=True)` 才会返回。泵线程就堵在这里，`coreDoneEvent` 不会置位。

`runEvent` 已经 set，只对会轮询它的工具有用：

| 工具 | 停止后谁在等 | 典型耗时 |
|---|---|---|
| `read` / `write` / `edit` | 当前这次文件 IO | 通常远小于 2 秒，不易复现 |
| `bash` | `_runWithInterrupt` 每 0.1 秒看 `interruptEvent`，看到后 SIGTERM，再固定 `sleep(0.5)`，再 SIGKILL | 通常 1 秒内；忽略信号或 D 状态的进程可以更久 |
| `askSubAgent` | 同上，但杀的是子进程；子进程里的模型重试 **看不到父会话的 runEvent** | 见 §4，本次实测约 76 秒 |
| 自写且不看 `interruptEvent` 的工具 | 文档已声明无法硬杀 | 直到该函数自己返回 |

单次工具调用不进线程池（`len(segment) >= 2` 才并发），但泵线程同样堵在 `executeToolCall()` 里，2 秒闸一样会超时。并行只是让「必须等所有已启动 worker」变成硬约束。

### 3.2 模型重试退避不看停止信号的空窗

`driveModelLoop` 在一次可重试失败后会 `sleep` 最多 8 秒，循环里每 0.1 秒看 `runEvent`，这条路径本身能在约 0.1 秒内停下。

真正的洞在子进程和 socket：

- `interruptActiveStreams()` 只对 **当前 agent** 已登记的 HTTP 响应做 `socket.shutdown`。
- `askSubAgent` 拉起的是另一个 `sdkEntry.py` 进程，自己的 agent、自己的重试循环。父级 stop **不会**把 `runEvent` 传进子进程。
- 父级只能 `killpg`。若 kill 没有立刻让 `_runWithInterrupt` 的读循环退出，父泵就继续占锁。

`bash` 默认 timeout 30 秒、上限 120 秒；`askSubAgent` 默认 600 秒、上限 3600 秒。停止若没有有效打断子进程，收尾上界就是这个 timeout，相对 2 秒闸是数量级差距。

---

## 4. 实锤：最近一次停止卡在 askSubAgent

会话 `261008175246-c3db58ab`，jsonl：

`~/.flamingo/logs/webData/-tmp/261008175246-c3db58ab.jsonl`

子代理日志：

`~/.flamingo/logs/cliData/-tmp/261009091119-d9a163ab.jsonl`

| 时刻 (UTC) | 事件 | 含义 |
|---|---|---|
| 01:11:19.903 | 父会话 assistant 发出 `askSubAgent`，`timeout=300`，`model=subGPT/gpt-6-astra` | 工具已启动，父泵持 `sessionLock` |
| 01:11:20.626 | 子会话 `261009091119-d9a163ab` 开始，`parentSessionId` 指向父会话 | 独立进程 |
| 01:11:29.916 | 子会话第一次模型调用完成，并 `read` 了方案 | 子进程活着 |
| 01:11:31.047 | 子会话 `modelError`，上游 overloaded，`backoffMs=1000`，将重试 | 子进程进入自己的重试，父级 stop 信号到不了这里 |
| 01:11:32.065 | 子会话第二次 `modelRequestStart`，日志在此截断；文件 mtime 也是这一秒 | 之后子进程不再写日志。与「父级 kill 落在第二次请求进行中」吻合 |
| 01:12:48.681 | 父会话才写入 `stopRequested` + `toolResult(reason=userStopped)` | `finalizeToolBatchLedger` 这时才跑到。说明父泵在 01:11:32 之后又堵了约 76 秒才离开工具调用 |
| 01:15:41.955 | 下一条父会话 `userMessage`「这个不需要写方案」 | 用户侧的下一次成功发送，距取消落盘又过了约 3 分钟 |

76 秒这个空窗，单靠 `_killProcessGroup` 里那个固定 `sleep(0.5)` 解释不了。能确定的是：

- 取消结果用的是 `buildCancelledToolResult()`，不是子进程正常返回的 JSON。也就是父级最终走了「中断 / 关流后补一条 cancelled」，而不是等子代理跑完。
- 在 cancelled 落盘之前，`coreDoneEvent` 不可能置位。这 76 秒内任何 `POST /api/chat/stream` 都会在 2 秒后 409。
- 子日志停在「第二次模型请求已发出、没有 usageRecord、没有 error、没有 reply」。父级的 `interruptActiveStreams()` 管不到这个子进程的 socket；父级若已发出 kill，子进程仍可能停在一次阻塞读上，直到读返回或进程组真正消失，`_runWithInterrupt` 才会抛 `modelInterruptedError` 并让 ledger finalizer 跑起来。

同文件里更早还有一批 `bash` 实际耗时远大于参数 `timeout`（例如 timeout=30 的调用，jsonl 时间戳跨度约 53070 秒，中间夹着用户离开后再回来）。那是另一类「工具调用跨度异常」，**不能**直接当成这次 409 的证据：那些调用最终写了正常 `toolResult`，不是 `userStopped`。本次 409 的直接证据是上面这条 `askSubAgent`。

---

## 5. 前端为什么把「请稍后再试」变成了一次失败

`webApp/frontend/js/chatView.js` `onStreamFailed()` 只在同时满足下面条件时静默重试一次：

```text
来自 send
还没重试过
status === 409
error.message 包含「活跃流」
```

然后 `setTimeout(send, 600)`。

「该会话仍在收尾，请稍后再试。」不含「活跃流」，所以：

- 不重试；
- `showError(error.message)` 把后端原文弹出来；
- `goIdle()`，输入框恢复，但后端锁还在。

600ms 的重试即使命中，也覆盖不了几十秒的 draining。`docs/plan/stopResponsivenessPlan.md` 写这条重试时，假设的是 stop POST 与新消息乱序、窗口大约一个 RTT。并行池落地后，窗口变成「工具线程多久能死」，前端没有跟着改。

因此用户侧的完整体验是：

```text
停止按钮看起来已经成功
→ 立刻发送
→ 约 2 秒后红字「该会话仍在收尾，请稍后再试。」
→ 再发仍然 409，直到旧工具线程终于退出
```

---

## 6. 排除项

| 怀疑 | 结论 |
|---|---|
| 前端自己编了这句错误 | 否。唯一来源是 `server.py` 两处 `HTTPException(409, detail='该会话仍在收尾，请稍后再试。')` |
| 普通「会话忙 / 已有活跃流」 | 否。那句文案不同，且会静默重试一次。用户原文对得上 timeout 分支 |
| attach 404 | 否。draining 期间泵还在，attach 应 200 并回放 stopped history。本次日志是 `POST /api/chat/stream` 409 |
| 删除会话 / 切模型的 409 | 否。那些 detail 分别是「无法删除」「本轮仍跑旧模型…」，且不会打在 `/api/chat/stream` 上 |
| 09-17 那次 bash 管道死等 | 相关但不是同一次。那次是 `communicate()` 无超时，新请求能 200 但 jsonl 不增长。管道排空已改；这次是停止后闸超时，请求在预检就被 409，根本不会 200 |
| 2 秒闸偶发偏短 | 模型流被 socket shutdown 打断时，2 秒通常够。不够的是池内工具，尤其是 `askSubAgent` |

---

## 7. 复现条件

下面任一成立，停止后 2 秒内发送就会稳定打出这句 409：

1. 当前 `tools.yaml` 不变，让模型在一条消息里发出 `askSubAgent`（单次也会堵泵线程；两次及以上走线程池），执行中点停止，立刻发送。
2. 同样配置下，一条消息里并发两个及以上 `bash`，命令忽略 SIGTERM，或卡在不可中断状态，停止后 2 秒内发送。
3. 自定义工具已进 `parallelToolPool.toolNames`，且 `execute()` 不读 `context.interruptEvent`。停止不能硬杀，409 会一直持续到函数返回。

只在模型吐字、还没进工具时停止，一般不会出现这句。那条路径是 socket shutdown，收尾通常小于 2 秒，闸会放行。

---

## 8. 根因归并

```text
用户可见错误
  = 前端把 timeout 409 当最终失败，且不重试
直接原因
  = waitForCoreIdle 只等 coreDoneEvent 2 秒
占用不释放的原因
  = requestStop 只封 UI，不摘泵；泵要等 stream.close()
close() 慢的原因
  = 工具执行 / 线程池 join 不在 2 秒内返回
本机最容易触发的原因
  = bash 与 askSubAgent 在并行池内；askSubAgent 是独立进程，父级 stop 进不了它的模型重试
本次日志对得上的实例
  = 261008175246-c3db58ab 的 askSubAgent，kill 后父会话仍空等约 76 秒才写 userStopped
```

这是四层叠加，不是单点写错文案：

1. Core：停止不等于工具已返回。
2. Web 闸：用 2 秒近似「短暂 draining」。
3. 本机配置：把最长、最不可协作的两个工具放进了必须 join 的池。
4. 前端：只对「活跃流」409 重试，对「收尾」409 直接报错。

---

## 9. 建议方向（本次不实施）

按「先缩小用户可见失败，再缩短真实占用」排序。都不在本次改动范围内。

1. 前端把「仍在收尾」与「已有活跃流」同等看待：自动退避重试，或按钮进入明确的等待态，不要一次 409 就当本轮失败。2 秒闸保留也行，但 UI 不能把「请稍后再试」显示成不可恢复错误。
2. 查清 §4 那 76 秒：`_runWithInterrupt` 在 `killpg` 之后为什么没有马上离开循环。子日志已停、父 ledger 很晚才写，中间没有父会话诊断事件，现有日志不够定位是 kill 没打中、读线程没收到 EOF，还是 `shutdown(wait=True)` 还在等另一个 worker。
3. `askSubAgent` 不应只靠杀进程组来响应父会话停止。子进程要么继承可轮询的中断，要么父级在 kill 后有硬截止，不能让一次子进程退出把父 `sessionLock` 按子代理 timeout（最长 3600 秒）挂住。
4. 并行池成员若不能在短时间内响应 `interruptEvent`，就不要承诺「停止后新消息只等 2 秒」。要么把 2 秒改成与工具停止上界一致的等待，要么停止后允许新请求排队而不是 409。排队必须仍禁止第二把 session lock，这是 §9 当初拆 `doneEvent` / `coreDoneEvent` 的原因。
5. 自查 `~/.flamingo/config/tools.yaml`：若要先避开这类 409，把 `bash` / `askSubAgent` 移出 `parallelToolPool.toolNames` 只能去掉线程池 join，去不掉单次工具堵泵。它不是修复。

---

## 10. 关键代码索引

| 位置 | 作用 |
|---|---|
| `webApp/backend/server.py` `chatStreamSync` / `chatConfirm` | timeout → 409「仍在收尾」；active → 409「已有活跃流」 |
| `webApp/backend/agentManager.py` `waitForCoreIdle` | 2 秒闸 |
| `webApp/backend/agentManager.py` `requestStop` / `finishStream` / `streamPump._pump` | UI 先封口，Core 完成后才摘泵 |
| `webApp/frontend/js/chatView.js` `stop` / `onStreamFailed` | UI 立刻可发送；只重试含「活跃流」的 409 |
| `flamingoAgents/core/agent.py` `executeConcurrentSegment` | `shutdown(wait=True)` |
| `flamingoAgents/core/agent.py` `runUserMessageStream` | 整段持 `sessionLock`，`close()` 才放锁 |
| `flamingoAgents/tools/builtinTools.py` `_runWithInterrupt` / `askSubAgentTool` | 0.1 秒轮询后杀进程组；子代理 timeout 默认 600、上限 3600 |
| `flamingoAgents/models/chatCompletions.py` `interruptActiveStreams` | 只掐本 adapter 的 socket，不管子进程 |
| `~/.flamingo/config/tools.yaml` | 本机池包含 read/write/edit/bash/askSubAgent |
| `docs/webApiSpec.md` v1.27 / §4.1 / §4.4 | 契约已承认 draining 期间短暂 409 |
