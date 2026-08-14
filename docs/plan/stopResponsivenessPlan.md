# 停止响应速度修复方案（stopResponsivenessPlan）

- 日期：2026-08-13
- 状态：v2.2（三轮外部审核通过；v2.1 后将「工具执行可中断」并入——验收发现 function call 期间 stop 仍卡，根因是工具 subprocess.run 无 HTTP 读/无退避循环，L3 的 socket shutdown 叫不醒它）
- 范围：`webApp/frontend/js/chatView.js`、`webApp/backend/agentManager.py`、`flamingoAgents/core/agent.py`、`flamingoAgents/models/chatCompletions.py`、`flamingoAgents/core/types.py`

## 1. 问题现象

按「停止」后，前端输入框长时间停留在「停止中」禁用态，要等 LLM 再产出内容（或连接彻底结束）才恢复可输入。用户语义：按中断 = 不要当前输出了，应立即截断、立即可输入。

## 2. 根因分析（调研结论）

完整链路：`stop()`（chatView.js:989）→ POST /api/chat/stop → `agentManager.requestStop` → `pump.stopFlag.set()`。之后两端都在"等"：

1. **后端（主因）**：`streamPump._pump` 只在「库生成器 yield 出下一个事件」之后才检查 `stopFlag`。而库生成器 `driveModelLoop` 此时正阻塞在 `chatCompletionsAdapter.iterSseData` 的 `read1(4096)` 上，等模型下一个 SSE chunk。慢模型、长思考（reasoning 半天不出字）、或出错重试退避（0.1s 睡眠片、最长 8s/片）时，下一个事件可能数秒~数十秒不到，stopFlag 就数秒~数十秒不被消费。
2. **前端（体验放大器）**：`stop()` 把 `stream.phase` 置为 `'stopping'` 后**故意不复位**（"等连接关闭后由 onStreamClosed 回空闲态"），而连接关闭依赖泵线程走到 `finally` 发哨兵 → 必须等后端。两层叠加 = 用户感知的"等半天"。
3. 次要阻塞面：工具执行中（bash 子进程最长 300s、askSubAgent）同样无法被 stop 打断——`subprocess.run` 阻塞等子进程退出，无 HTTP 读（socket shutdown 叫不醒）、无退避循环（interruptEvent 检查不到），只能等工具返回后的下一个事件。**验收实测：function call 期间点停止仍卡数秒~数分钟。**

附注：`urlopen(timeout=60)` 的 socket 读超时兜底了"连接不死"，但对秒级体验无帮助。

## 3. 目标与非目标

### 成功标准（可验证）

- G1：模型流式输出中点停止，输入框 ≤1s 恢复可输入（前端不等后端，L1 单独即可达成）。
- G2：「重试退避中」（retryNotice 等待片）点停止，≤0.2s 打断退出（退避片内查中断标志）。
- G3：两个窗口打开同一会话，A 点停止后，B 窗口即时收到广播的 `stopped` 终态 + 连接关闭，静默收尾（不误报"连接中断"）。
- G4：停止后立刻发新消息：stop 请求先于新消息到达后端时**不撞 409**（后端宽容闸）；宽容闸 `doneEvent.wait(2)` 超时 → 409 原文案，**前端 0.6s 静默重试一次后成功即视为达标**；
  极端乱序（stop 在途即发送）撞 409 时同样由前端静默重试兜底，再败才报错。
- G5：正常（未停止）路径行为零变化：终态仍是「先回写 usage 后关连接」、assistant 完整消息正常落盘、usage 账单不重不漏。
- G6：后端停止收尾 ≤1s：HTTP 连接被强制断开、会话锁即时释放（允许发新消息）、泵注销。
- G7：工具（bash/askSubAgent）执行中点停止，≤0.2s 打断子进程并退出（分片 poll + terminate）；前端输入框同样 ≤1s 可用（L1 不等后端）。

### 非目标（明确不做）

- 停止轮的 prompt token 计费：停止时 provider 的 usage chunk 未收到，`usageTotal` 无本轮数据，**停止轮不计费**（与现状一致，不劣化）。
- 不改 SSE 协议帧格式、不动 attach/回放协议（仅消费侧忽略行为变化）。
- 不引入 asyncio；保持 urllib 同步客户端。
- `modelAuth.py` 不涉及。

## 4. 方案设计（三层，逐层可独立生效、可独立回退）

### L1 前端：停止即乐观复位（chatView.js，唯一必做层）

**改动点（stop()，chatView.js:989-1002）**：

```plain
1. 发 POST /api/chat/stop，但不 await（fire-and-forget）：
     var stopDone = window.api.stopChat(sessionId).catch(function () { return null; });
   —— 保证停止指令先于任何用户后续操作到达后端（ordering 论证见 §4.1.A）
2. phase 置 'stopping' → flushLivePaint + markInterrupted（现状保留）
3. 【核心】立即 stream.abort()：同步断开本窗口 SSE fetch
   → handle.done resolve('aborted')，微任务链同步执行 onStreamClosed
   → 命中既有 stopping 早退分支：goIdle()（stream=null、解锁输入框）+ focusComposerIfReady()
   → 输入框立即可用，全程不等后端
4. await stopDone（仅消化异常；不阻塞任何 UI 状态）
5. 不做 phase 拨回（审核 M 修复：stopping 必须保持到 onStreamClosed 分支判定完成，
   拨回会破坏早退分支命中、误报"连接中断"）
```

**send() 增加 409 静默重试（G4 后半段，3 行）**：`onStreamFailed` 路径识别 `status===409 && message 含「活跃流」`→ `setTimeout(send, 600)` 一次（仅当 `window.appStore.stream === null`，即非用户新流抢占）；再败走原报错。

#### §4.1.A 时序论证（审核后修正版）

- `stream.abort()` 是**同步**的（`AbortController.abort()`），`done` 的 resolve 与 `onStreamClosed` 在微任务中执行，**早于**任何网络往返。`goIdle()` 解锁输入框的时刻，stop POST 已发出（第 1 步先建 fetch）但可能未到达后端。
- **乱序窗口**：用户手速极快，在 stop POST 在途时发出新消息 → 后端 `chatStream` 查 `activeStreams` 仍有旧泵 → 409。该窗口 ≈ 一个网络 RTT（本地≈0，远程可达数百 ms），**客观存在**，由 G4 的前端静默重试兜底。
- **顺序窗口**（常见路径）：stop POST 先达后端 → requestStop 同步执行 `interruptNow()`（L2）→ 泵立即收尾注销 → 新消息到达时 activeStreams 已清 → 不撞 409。
- **chatStream 宽容闸**（G4 前半段，兜底工具在飞场景）：若旧泵已置 stopping 且其 `agent` 会话锁此刻空闲（`lock = agentInstance.getSessionLock(sessionId)`；`if lock.acquire(blocking=False): lock.release()` 试探——**探测成功必须立即 release，只作空闲性读数，绝不持锁出临界区**，否则 RLock 计数泄漏将导致该会话永久死锁），说明收尾只差泵 finally 的毫秒级簿记 → `chatStream` 等待旧泵 `doneEvent.wait(2)` 后直接续走登记新泵，不返回 409；**`doneEvent.wait(2)` 超时 → 维持 409 原文案，由前端 0.6s 静默重试收敛**（不做超时强行登记：现 startStream 结构下「检查-等待-替换」无原子性，行为不确定；若要强行登记须下沉进 startStream 的 managerLock 临界区并承担 2s 全局阻塞，本方案不取）；锁被占用（工具在飞）→ 409 原文案（前端静默重试一次）。
- **新流并发安全**：宽容闸放行后新泵线程启动，`runUserMessageStream` 首先 `getSessionLock` 阻塞——旧泵 `stream.close()` 触发生成器链退出、`with` 释放锁后新流才推进。**不会**并发写 conversation（RLock 保护），jsonl 落盘顺序不受影响。

### L2 后端泵线程：停止即收尾（agentManager.py）

**`streamPump` 新增成员**：

```plain
self.doneEvent = threading.Event()      # finally 末尾 set，chatStream 宽容闸等待用
self.usageRecorded = False              # 【新增字段，审核 M 修复：现状不存在】
self.historyOverflowed = False          # 截尾标记，attach 提示用
```

**`requestStop(sessionId)` 从"置标志"改为"主动收尾"**（仍在 managerLock 内调用，但 interruptNow 内部动作均不拿 managerLock，无重入死锁）：

```plain
def requestStop(self):
    if self.doneEvent.is_set(): return      # 幂等
    self.stopFlag.set()
    try:
        self.agent.interruptActiveStreams() # L3：shutdown socket 叫醒 read1
    except Exception: pass                  # 收尾路径不容再挂
    self._broadcast(errorEvent('已停止。', 'stopped'))   # 立即广播终态（G3 其他窗口即时收尾）
    self._recordUsage()                     # 幂等；delta 全 0 时 writeUsageTurn 现状即不写
    unregisterStream(self.sessionId)        # 立即注销 → chatStream 不再 409
    self._closeSubscribers()                # closed=True + 各订阅队列放哨兵 → SSE 连接立即关闭
    self.doneEvent.set()
```

**`_pump` 调整**：

```plain
finally:
    self.stream.close()      # 现状保留：触发生成器 finally 落盘/释放会话锁
    self._recordUsage()      # 幂等守卫：requestStop 已记则跳过；正常路径行为不变
    if not self.doneEvent.is_set():   # 正常终态/异常路径：维持原顺序（先回写后哨兵，G5 不变式）
        unregisterStream(self.sessionId)
        self._closeSubscribers()
        self.doneEvent.set()
    # 中断路径：requestStop 已收尾，finally 不再重复广播/注销
```

- `_recordUsage` 首行加 `if self.usageRecorded: return; self.usageRecorded = True`（线程安全：requestStop 与泵 finally 均串行于各自路径，竞态仅"双记"，被守卫消除）。
- **broadcast 竞态（红线）**：requestStop 广播 stopped 时，泵线程可能正 `_broadcast(event)` 在飞。stopped 必须成为 history **最后一个事件**，否则 attach 回放会在 stopped 之后追加 delta。实现：`_broadcast` 内（subLock 内）先查 `if self.doneEvent.is_set(): return`——requestStop 先放哨兵、设 doneEvent（subLock 内），在飞的泵广播随后拿到锁时已被拦截丢弃。顺序保证：stopped 是终尾。
- **history 截尾**：`HISTORY_MAX_EVENTS = 2000`，`_broadcast` append 后超长则丢弃最旧 delta 段（保留终态尾部），并置 `historyOverflowed=True`；`subscribe` 回放首帧若为溢出副本，attach 首帧 meta 附带 `overflowed`（见 §4.4 兼容说明）。

### L3 库内停止信号传递（flamingoAgents）

目标：让 `stopFlag` 能「叫醒」阻塞在 HTTP 读/重试 sleep 的生成器。

**审核实测纠正（H 级）**：跨线程 `HTTPResponse.close()` **不可行**——CPython `http/client.py` 的 `close()` → `_close_conn()` 自旋等待 `self.fp` 归 None，而 `fp` 只有响应体读完才置 None；读者线程阻塞在 `readinto` 上永远读不完 → **互相等待的死锁**（实测卡死 ≥19s，直到 60s 读超时兜底），还会把调用方（stop 路由 worker）挂死。可行替代经实测验证：

```plain
response.fp.raw._sock.shutdown(socket.SHUT_RDWR)   # 0.0s 返回；read1 立即被唤醒
```

**改动清单**：

1. `core/types.py`：新增 `class modelInterruptedError(Exception)`（**非** modelRequestError 子类，避免被当可重试错误）。
2. `models/chatCompletions.py`（chatCompletionsAdapter）：
   - `__init__` 新增 `self.activeResponses: set = set()` + `self.activeResponsesLock = threading.Lock()`。
   - `consumeSseStream` 内：`response = self.openRequest(...)` 后登记入 set，`with` 退出后（finally）移出 set。
   - 新增 `interruptActiveStreams()`：锁内快照 set 后逐个 `try: r.fp.raw._sock.shutdown(socket.SHUT_RDWR) except Exception: pass`。
   - `consumeSseStream` 增加可选参 `stopEvent`；唤醒分流（审核 F 级修复，三路径全覆盖）：
     - a. chunked 响应被 shutdown → `read1` 抛 `http.client.IncompleteRead`（**HTTPException 子类，非 OSError**）；
     - b. 非 chunked（Content-Length）响应被 shutdown → `read1` **返回空字节** → `iterSseData` 正常 break，无异常；
     - c. 其它 OSError/URLError。
     统一处理：`iterSseData` 读循环捕获 `Exception`（含 IncompleteRead/OSError），若 `stopEvent and stopEvent.is_set()` → `raise modelInterruptedError('用户已停止')`，否则按原类型抛出；「空字节 break」路径在 `iterSseData` 返回后检查 `stopEvent.is_set()` → 同样 raise modelInterruptedError。consumeSseStream 现有 `except (URLError, HTTPException, OSError) → modelRequestError` 包装链中，modelInterruptedError 直通不包装。
   - 响应集合只覆盖 chatCompletionsAdapter（唯一适配器实现，`builder.py:51` 唯一实例化点，无其它适配器）。
3. `core/agent.py`（agent）：
   - `__init__` 新增 `self.interruptEvent = threading.Event()`；新增 `interruptActiveStreams()` 薄封装：`self.interruptEvent.set()` + `self.modelAdapter.interruptActiveStreams()`（适配器无该方法时 getattr 防御跳过）。
   - `driveModelLoop` 调 `completeStream(messages, modelTools, stopEvent=self.interruptEvent)`（签名透传；`modelAdapterPort` Protocol 声明同步更新——types.py 所在 core 不动，ports.py 加可选参声明）。
   - **中断异常处理位置（审核 H 级修复）**：在 `for attempt` 重试循环的 `try` 块上、`except Exception` **之前**单独加：
     ```python
     except modelInterruptedError:
                     return   # 直通：不经 logModelError（避免误记 modelError 日志）、不经重试判定
     ```
     （若只挂在 `except Exception` 上，`logModelError` 在重试判定前执行，会先记一条假 modelError 日志；且 `statusCode` 缺失时 `isRetryable=False` 虽恰好不重试，但会把中断当「模型调用失败」yield errorEvent 报给用户——语义全错。）
   - **「缺最终结果」分流（审核 L 级修复）**：`if completion is None` 分支先查 `self.interruptEvent.is_set()`，置位则 `return`（视为中断而非 `RuntimeError('模型流式响应缺少最终结果')`）。
   - 重试退避 0.1s 睡眠片循环：每片末尾 `if self.interruptEvent.is_set(): return`（G2，≤0.2s 打断）。

### L3.5 工具执行可中断（v2.2 新增，验收驱动）

function call 期间 stop 卡的根因：工具走 `subprocess.run`（无 HTTP 读、无退避循环），L3 两个叫醒机制都够不着。修复：

1. `core/types.py` 的 `toolContext` 新增可选字段 `interruptEvent: threading.Event | None = None`。
2. `agent.executeToolCall` 构造 `toolContext` 时传入当前会话的 `interruptEvent`（`self.getInterruptEvent(sessionId)`——executeToolCall 需加 sessionId 形参，调用点 driveToolBatch/driveConfirmation 均有 sessionId 可传）。
3. `builtinTools.py` 的 `bashTool`/`askSubAgentTool`：`subprocess.run` 改为 `Popen` + 0.1s 分片 `poll()` 轮询，每片查 `context.interruptEvent.is_set()`，置位则 `process.terminate()`（0.5s 后未退再 `kill()`）并 `raise modelInterruptedError('用户已停止')`。超时/输出截断等既有语义不变。
4. `agent.driveToolBatch` 的工具执行循环与 `toolRuntime.executeToolCall`：`except Exception` 前加 `except modelInterruptedError: raise` 直通（不能落进「工具执行异常 → toolResult(isError=True)」分支，否则中断会被当成工具失败发给模型）。
5. 中断后工具卡片状态：前端 L1 abort 后视图定格「已中断」，running 卡片不再更新（与现状 stop 一致）。

### 覆盖矩阵

| 停止时泵卡在 | 修复层 | 表现 |
|---|---|---|
| 等 LLM 下一个 chunk（read1） | L3 shutdown 唤醒 → 生成器 return → 锁释放；L2 已即时收尾 | 输入框 L1 即开；后端 ≤1s 清场（G1/G6） |
| 重试退避 sleep 片 | L3 片内检查 interruptEvent | ≤0.2s 退出（G2） |
| 工具子进程执行中（bash/askSubAgent） | L3.5 Popen 分片 poll + terminate；L1 前端立即可输入 | ≤0.2s 打断（G7） |
| 另一窗口观看中 | L2 requestStop 立即广播 stopped + 哨兵 | B 窗口 ≤RTT 静默收尾（G3） |

## 5. 不变式与副作用清单（逐条核实结论）

1. **assistant 消息落盘**：停止路径在 `appendAssistantMessage` 前 break/return → jsonl 无半截 assistant 消息。与现状一致 ✔
2. **usage 双计**：`_recordUsage` 幂等守卫为**新增** `usageRecorded` 字段（非现状，审核 M 修正）；中断路径 delta 全 0 → `writeUsageTurn` 现状"任一项>0 才写"天然跳过，停止轮不写账 ✔
3. **「先回写后哨兵」**：仅正常终态路径保持（G5）；中断路径显式打破（需求本身），statusBar 短暂滞后已声明可接受（下轮流终态自然刷新）✔
4. **subscribe 回放**：中断后 history 尾部 = stopped 终态（broadcast 竞态红线保证，§4-L2）；新 attach 订阅者拿回放 + 哨兵立即结束 → 前端 404 等价路径，行为不变 ✔
5. **queued 用户消息**：停止后 queuedUserMessage 未消费仍留 conversation 内存态——与现状 stop 行为一致，不扩大 ✔
6. **会话锁释放**：shutdown 唤醒后生成器 `return/raise` 经 `with getSessionLock` 正常退出释放锁；`_recordUsage` 只拿 `sessionLocksGuard`（读 conversations dict）不持会话锁，无死锁（审核 D 级核实成立）✔
7. **停止轮 token 不计费**：usage chunk 未收到，usageTotal 无本轮数据——与现状一致；G6 已降口径，不再自相矛盾（审核 M 修正）✔
8. **abort 后 sseGen 残留**：前端 abort → Starlette 下次 send（keep-alive ≤15s）或 RST 感知断连 → sseGen finally unsubscribe；期间泵已 closed 不再 broadcast，订阅队列不增长。与现状「用户直接关页面」行为完全一致，非劣化（审核 E 级核实）✔

9. **中断路径异常广播被吞属期望语义**：泵线程若在中断后因异常进 `except` 分支，其 errorEvent 广播会被 doneEvent 拦截吞掉——此时 stopped 已是终态，吞掉晚到的 error 恰是期望行为（复审 c 项确认）✔

## 6. TODO list

1. `core/types.py`：新增 `modelInterruptedError`。→ 验证：`python -c "from flamingoAgents.core.types import modelInterruptedError"` 通过。
2. `models/chatCompletions.py`：activeResponses 登记/注销 + `interruptActiveStreams()`（shutdown SHUT_RDWR 包装吞异常）+ stopEvent 透传与三路径唤醒分流（IncompleteRead/空字节/OSError）。→ 验证：本地起慢速 SSE mock 服务，流式中置位 stopEvent + shutdown，断言 read1 ≤0.5s 醒、抛 modelInterruptedError。
3. `core/agent.py`：interruptEvent + `interruptActiveStreams()` 薄封装；`except modelInterruptedError: return` 置于 `except Exception` 前；`completion is None` 分支先查中断标志；退避片末尾检查。→ 验证：mock 适配器分别模拟「慢流中断」「退避中断」，断言不记 modelError 日志、不重试、≤0.2s return。
4. `core/ports.py`：`completeStream` Protocol 声明补可选参 `stopEvent=None`。→ 验证：类型自检 import 通过。
5. `agentManager.py`：新增 doneEvent/usageRecorded/historyOverflowed 字段；`_recordUsage` 幂等首行；`_broadcast` subLock 内 doneEvent 拦截；`_closeSubscribers` 抽函数复用；`requestStop` 主动收尾序列；history 2000 截尾。→ 验证：起服务 + 慢模型配置实测 G1/G2/G3/G6；双窗口实测 G3。
6. `server.py`：chatStream 宽容闸（409 前：旧泵 stopFlag 已置 + `lock.acquire(blocking=False)` 探测**成功后立即 release** → `doneEvent.wait(2)` 续走登记；**超时 → 409 原文案**；锁占用 → 409 原文案）。→ 验证：停止后 100ms 内发新消息不撞 409；**放行路径执行后再次 `lock.acquire(blocking=False)` 仍可成功（证明探测未泄漏持锁）**；工具在飞场景撞 409 有友好文案；wait(2) 超时场景返回 409 且前端重试后成功。
7. `chatView.js`：stop() 改 fire-and-forget + abort（保持 stopping 至 onStreamClosed）；send() 409 静默重试一次。→ 验证：浏览器实测 G1；乱序连点实测 G4。
8. **L3.5 工具可中断**：`core/types.py` toolContext 加 interruptEvent 可选字段；`agent.executeToolCall` 加 sessionId 形参透传 interruptEvent；`builtinTools.py` bashTool/askSubAgentTool 改 Popen 分片 poll + interrupt 时 terminate/kill + raise modelInterruptedError；`toolRuntime.py` 与 `agent.driveToolBatch` 工具执行循环加 modelInterruptedError 直通。→ 验证：起服务，让模型调 bash（sleep 30），执行中点停止，断言 ≤0.2s 打断、子进程被 terminate、会话可立即发新消息（G7）。
9. 走查 §5 全部不变式（评审清单化逐条打勾）。→ 验证：checklist 全勾。
10. 版本收尾（文件头 Version +1、Description 记录变更）：
   - `core/types.py` 1.5 → 1.6
   - `models/chatCompletions.py` 1.14 → 1.15
   - `core/agent.py` 1.14 → 1.15
   - `core/ports.py` 1.1 → 1.2
   - `webApp/backend/agentManager.py` 1.5 → 1.6
   - `webApp/backend/server.py` 1.7 → 1.8
   - `webApp/frontend/js/chatView.js` **1.11 → 1.12**（复审修正：文件头实测已是 1.11，初版与 v2 均基于过时读数）
   - `flamingoAgents/tools/builtinTools.py` 1.1 → 1.2
   - `flamingoAgents/tools/toolRuntime.py` 1.1 → 1.2
   - （`modelAuth.py` 不改，从范围移除——审核 L 修正）
   - 备注：`core/ports.py` 文件头 Description 需同步记录 `completeStream` 新增 `stopEvent` 可选参。

## 7. 风险与回退

- R1：`fp.raw._sock` 属性链依赖 CPython 内部结构（`HTTPResponse.fp` → `SocketIO.raw` → `_sock`）。若某环境属性缺失，`interruptActiveStreams` 吞异常 → 退化为现状（等下一 chunk），**不劣化**；L1 前端复位仍保证输入框即时可用（兜底体验）。
- R2：shutdown 后 read1 的异常形态随响应类型不同（IncompleteRead/空字节）——已由 L3 三分支统一分流覆盖（审核实测两条均已验证）。
- R3：中断路径跳过的 pending 工具事件使卡片定格 running → 前端 abort 后视图定格"已中断"，与现状 stop 一致；不新增问题。
- 回退：三层独立。`git revert` L1（chatView.js 单文件）即恢复"等连接"旧行为；L2/L3 仅后端，不影前端协议。
