# Function Call 中断后会话持续 400 事故溯源报告

- Author: wilbur
- Version: 1.1
- Date: 2026-08-14
- Incident session: `session_3ebfe236ecae`
- Severity: **High / P1（单会话持续不可用，且存在取消工具被冷恢复重执行的副作用风险）**
- Regression commit: `e49de66dee9d9c27de8f12efe93c1827295ce627`（2026-08-14 12:10:10 +08:00，`工具执行可中断与停止收尾闭环`）
- Investigation status: **协议缺口与代码根因已确认；本次由 stop 触发为高置信推断（无 stop/access 持久化审计）**
- Scope: **仅溯源与报告；未修改任何源码、配置、目标 session 日志或业务数据。**

---

## 1. 执行摘要

这不是模型/provider 的偶发故障，也不是 `askSubAgent` 正常失败或超时处理错误，而是一次确定的**本地会话协议状态破坏**。

事故主链路如下：

1. 父模型返回一条带 `askSubAgent` function call 的 assistant 消息；系统先把这条 assistant 消息持久化到父会话日志（目标日志第 101 条）。
2. 据用户陈述，用户在 `askSubAgent` 子进程仍处于运行态时点击停止；项目 JSONL 没有持久化该 stop 请求。
3. 结合当前实现与事故日志形态，高置信判断：2026-08-14 新增的工具中断链路终止子进程并抛出 `modelInterruptedError`；项目没有直接持久化 kill/异常事件。
4. 该异常在 `toolRuntime` 中被特意直通，在 `driveToolBatch` 中又被捕获后直接 `return True`；因此代码在 `currentConversation.addToolResult(...)` **之前退出**。
5. 父会话由此留下：

   ```text
   assistant(tool_calls=[tool_MAIQ...])
   # 缺少 role=tool、tool_call_id=tool_MAIQ... 的响应
   ```

6. 停止本身不会再请求模型，所以不合法状态当时没有报错；下一条用户消息先被追加到会话，序列变成：

   ```text
   assistant(tool_calls=[tool_MAIQ...])
   user("我看到子代理已经干完了")
   ```

7. 下一次模型请求携带该非法序列，provider 按 OpenAI tool-call 协议拒绝并返回 HTTP 400。
8. 当前热会话没有自动补齐 dangling tool result；后续每次消息都重复携带同一缺口，所以连续三次得到完全相同的 400。

**一句话根因：工具 stop 只完成了“进程/界面终止”，没有完成“会话 transcript 的 tool-call 闭环”。**

---

## 2. 结论与确定性

### 2.1 已直接证实

- 目标日志第 101 条存在 `askSubAgent` assistant tool call：
  - 本地 call id：`tool_MAIQ903IGSwq6hnU4cLS8xvF`
  - model：`sub2api_grok/grok-4.6`
- 第 101 条之后没有任何匹配该 id 的 `toolResult`。
- 第 102 条直接是新 `userMessage`。
- 第 103 条真实 request payload 的尾部直接呈现：

  ```text
  assistant(tool_call id=tool_MAIQ903IGSwq6hnU4cLS8xvF, name=askSubAgent)
  user("我看到子代理已经干完了")
  ```

- 第 103 条请求共有 51 个 tool calls。按请求中出现顺序从 0 编号，序号 50 正是上述最后一个 `askSubAgent`，且它是唯一未闭合调用。因此错误文案中的 `askSubAgent:50` 可唯一映射到本地 `tool_MAIQ...`。
- 当前源码确实在工具中断时于 `addToolResult` 前返回。
- 第 103/105/107 条均为相同 400，证明非法状态没有被后续失败路径修复。

### 2.2 高置信因果（缺少直接 stop 审计）

用户明确描述在 function call 期间点击了停止；源码、父子日志形态与该动作完整吻合，因此可以高置信判定此次缺口由 interrupt 路径产生。

但项目没有把 `/chat/stop`、kill PID/进程组或 `modelInterruptedError` 持久化到 session JSONL，故以下内容不能伪称为日志直接记录：

- 点击停止的精确时刻；
- 子进程收到的是 SIGTERM 还是 0.5 秒后升级为 SIGKILL；
- 父 `_runWithInterrupt` 观察到 interrupt event 的精确毫秒。

若结合用户动作顺序与现存持久化事件，stop 的候选区间为：

```text
不早于 2026-08-14 17:55:06.594 +08:00（子会话最后已持久化事件）
早于   2026-08-14 18:01:14.574 +08:00（父会话下一条用户消息）
```

该区间包含“用户是在子会话最后落盘后点击停止”的 P3 用户动作顺序假设；若后续找到 access log，应以 access log 为准。

---

## 3. 原始证据完整性

### 3.1 目标文件快照

| 项目 | 值 |
|---|---|
| 父会话日志 | `webData/sessionLogs/session_3ebfe236ecae.jsonl` |
| 事件数 | 107 |
| 文件大小 | 1,018,841 bytes |
| 事故尾事件时间 | 2026-08-14 18:02:06 +08:00 |
| 调查开始 SHA-256 | `04e1d96a64dfd02286e228477a0e9bb2d8b757a1168dedca729dc567775363cc` |
| 子会话日志 | `.agentLogs/session_20337ff178c1.jsonl` |
| 子会话事件数 | 64 |

所有调查脚本均只读打开目标日志，没有写回、补行、截断、重放到真实会话或发送真实模型请求。

### 3.2 工作树基线

调查开始前，工作树已经包含被中断子代理留下的技能编辑改动：

- `webApp/backend/server.py`
- `webApp/backend/skillStore.py`
- `webApp/frontend/index.html`
- `webApp/frontend/js/api.js`
- `webApp/frontend/js/chatView.js`
- `webApp/frontend/js/fileMention.js`
- `webApp/frontend/js/skillsView.js`
- `webApp/frontend/js/slashCommand.js`
- `webApp/frontend/styles.css`
- `docs/plan/skillEditPlan.md`
- `docs/plan/fileMentionFixPlan.md`

本次调查只新增/修订：

- `docs/plan/toolCallInterruptionIncidentPlan.md`
- `docs/toolCallInterruptionIncidentReport.md`

子代理计划审核会按系统正常机制产生新的 `.agentLogs` 审核日志；它们不是目标 session，也没有改变目标证据。

---

## 4. 关键时间线

> 下表统一使用 `+08:00`；括号内为目标 JSONL / 子 JSONL 行号。

| 时间 | 会话 | 事件 | 结论 |
|---|---|---|---|
| 17:35:19.839 | 父（L96） | 用户要求派发 `sup2api_grok/grok-4.6` 实施 | provider 名有笔误 |
| 17:36:27.045 | 父（L97） | assistant 发出第一个 `askSubAgent` | call id=`tool_qTx...` |
| 17:36:27.271 | 父（L98） | `toolResult(isError=True)`：provider 不存在 | **正常失败仍完整闭环**，是本事故的对照样本 |
| 17:36:37.156–17:36:37.272 | 父（L99–100） | bash 查询正确 provider，随后正常 toolResult | 闭环正常 |
| 17:37:23.375 | 父（L101） | assistant 重新发出 `askSubAgent` | call id=`tool_MAIQ...`；assistant 已持久化 |
| 17:37:23.482 | 子（L1–2） | 子会话创建并收到实施 prompt | 与父 call 的 model/prompt/workDir/时间唯一匹配 |
| 17:44:21–17:53:32 | 子 / 文件系统 | 子代理写入技能编辑相关代码 | 用户看到的“已经干完”主要指文件副作用已出现 |
| 17:54:27.075–17:54:27.493 | 子（L61–62） | Python AST、JS `node --check` 通过 | 子任务大部分实施与静态自验完成 |
| 17:55:06.587 | 子（L63） | 子模型尝试最后一个 bash 删除/清理命令 | SDK 工具需要确认 |
| 17:55:06.594 | 子（L64） | `toolResult(isError=True)`：`userRejectedApproval` | 这是 SDK 自动拒绝危险命令，**不是 kill 证据**；子会话此后无最终 assistant |
| 17:55:06.594–18:01:14.574 | — | 用户报告在 function call 期间停止 | 无持久化 stop/access log，精确时刻未知 |
| 18:01:14.574 | 父（L102） | 用户：“我看到子代理已经干完了” | 热 conversation 直接追加 user，未补 tool result |
| 18:01:15.088 | 父（L103） | provider HTTP 400 | 第一次暴露非法 transcript |
| 18:01:34.291 / 18:01:34.842 | 父（L104–105） | 用户“？”→ 同一 400 | 状态未修复 |
| 18:02:06.181 / 18:02:06.715 | 父（L106–107） | 用户“？”→ 同一 400 | 会话持续中毒 |

### 4.1 为什么“代码看起来做完了”，父 function call 却仍未完成

子代理已经把文件写入并完成一部分自验，但子会话最后处于：

1. 删除命令被 SDK 自动拒绝；
2. `continueConfirmation(... approved=False)` 将拒绝结果写入子 conversation；
3. 子代理还需要再调用一次模型，生成最终总结和 `--json` 输出；
4. 父 `askSubAgentTool` 只有等 `sdkEntry.py` 进程退出、解析 stdout 最后一行 JSON 后，才能生成父 `toolResult`。

因此，“文件改动已出现”不等于“子代理进程已退出”，也不等于“父 assistant 的 tool call 已闭环”。停止发生在这个差异窗口内。

---

## 5. 协议断点

### 5.1 必须维持的不变式

对于任意 assistant 消息中的 tool call 集合 `C`：

```text
assistant(tool_calls=C)
→ 在下一条 user/assistant/system 消息之前
→ 每个 call id 都必须恰有一个匹配的 role=tool 响应
```

本次 `C` 只有：

```text
C = { tool_MAIQ903IGSwq6hnU4cLS8xvF }
```

实际后继响应集合为空：

```text
R = ∅
```

所以 `C - R` 非空，provider 在真正执行模型推理之前就以 400 拒绝请求。

### 5.2 同一 session 内的 A/B 对照

```text
L97 assistant askSubAgent(tool_qTx...)
L98 toolResult(tool_qTx..., isError=True)        ✅ 普通失败闭环

L99 assistant bash(tool_j3...)
L100 toolResult(tool_j3..., isError=False)       ✅ 正常成功闭环

L101 assistant askSubAgent(tool_MAIQ...)
L102 userMessage                                  ❌ 中断路径缺少 toolResult
```

这组对照排除了“askSubAgent 失败天然不写 toolResult”的可能性。普通失败、非零退出和 timeout 均会返回 `toolOutput`，再由 runtime 包成 `toolResult`；只有 `modelInterruptedError` 被特意绕开。

### 5.3 `askSubAgent:50` 如何映射到本地 id

第 103 条 request 中：

- tool call 总数：51；
- 从 0 开始的最后一个序号：50；
- 序号 50 的工具：`askSubAgent`；
- 其本地 request id：`tool_MAIQ903IGSwq6hnU4cLS8xvF`；
- 它是唯一缺少 tool response 的调用。

request payload 中不存在字面 id `askSubAgent:50`。在本事故中，该错误标签精确等于“工具名 + 全局零基序号”，可唯一定位到 `tool_MAIQ...`；本报告不把这一观察外推成所有 provider 的通用编号规范。

---

## 6. 代码级完整因果链

### 6.1 assistant tool call 先持久化

`flamingoAgents/core/agent.py:269-283`：

```python
currentConversation.appendAssistantMessage(...)
...
terminated = yield from self.driveToolBatch(...)
```

`appendAssistantMessage` 在工具执行前发生。`conversation.py:144+` 会同时：

1. 写父 JSONL `assistantMessage`；
2. 把 assistant 消息加入内存 `messages`。

这个顺序本身合理：tool result 必须引用一个已经存在的 assistant call。问题在于取消路径没有做补偿闭环。

### 6.2 stop 将中断传给工具进程

前端 `webApp/frontend/js/chatView.js:1064-1075`：

```javascript
window.api.stopChat(sessionId)
stream.abort()
```

后端 `webApp/backend/agentManager.py:160-173`：

```python
self.stopFlag.set()
self.agent.interruptActiveStreams(self.sessionId)
unregisterStream(...)
self._sealStopped()
```

`flamingoAgents/core/agent.py:85-93` 将当前 session 的 `interruptEvent` 置位。

`flamingoAgents/tools/builtinTools.py:159-190` 中，父进程每 0.1 秒检查一次：

```python
if context.interruptEvent.is_set():
    _killProcessGroup(process)
    raise modelInterruptedError('用户已停止')
```

因此停止能够快速终止 `askSubAgent`，满足响应速度目标。

### 6.3 中断异常绕过 toolResult

`flamingoAgents/tools/toolRuntime.py:44-66`：

```python
except modelInterruptedError:
    raise
except Exception as error:
    return toolResult(...)
```

普通异常会被包装成 tool result；中断异常被有意直通。

随后 `flamingoAgents/core/agent.py:310-316`：

```python
try:
    result = self.executeToolCall(call, sessionId)
except modelInterruptedError:
    return True
currentConversation.addToolResult(result)
yield toolCallEndEvent(toolResult=result)
```

`return True` 位于 `addToolResult` 之前，直接造成缺口。这里的 `True` 注释语义原本是“已经产生终态/调用方应结束”，但中断分支实际上没有在 conversation 中生成 protocol 终态，只依赖 pump 对前端广播 `stopped`。

### 6.4 pump 的 stopped 终态不能代替 role=tool

`agentManager.streamPump._sealStopped()` 写入的是泵内 SSE history 的 `errorEvent(errorType='stopped')`，并关闭订阅者；它不会调用 `conversation.addToolResult`，也不会写父 session JSONL。

因此系统同时出现两种“终态”：

- **UI/SSE 层**：已停止、连接关闭，表面成功；
- **模型 transcript 层**：assistant tool call 仍未闭合。

这正是错误延迟到下一次模型请求才出现的原因。

### 6.5 下一条用户消息先落盘，再触发 400

`flamingoAgents/core/agent.py:124-147`：

```python
dangling = currentConversation.takeDanglingToolCalls()
if dangling:
    ...
currentConversation.appendUserMessage(cleanMessage)
yield from self.driveModelLoop(sessionId)
```

当前热 conversation 的 `danglingToolCalls` 是空列表，所以第 102 条 user 先被写入 JSONL/内存，随后 adapter 才构建 request。

`chatCompletions.py:330-350` 忠实转换内存消息：

- assistant → 带原始 `tool_calls`；
- tool → 带 `tool_call_id`；
- user → 普通 user。

由于内存中没有 tool 消息，provider 收到非法邻接并拒绝。

### 6.6 为什么显示“已重试 0 次”

`agent.py:229-241` 只重试：

```text
429, 500, 502, 503, 504，或连接建立期无 status 的可重试错误
```

本次 `statusCode=400`，`isRetryable=False`，第一次 attempt 直接产出：

```text
模型调用失败（已重试0次）
```

这是当前重试策略的正确行为。重试同一个非法 payload 不可能修复该问题。

---

## 7. 为什么已有 dangling/orphan 兜底没有救本次热会话

### 7.1 兜底只在 conversation 冷恢复时运行

`conversation.py:29-33` 仅在 `resume=True` 时调用 `_resumeFromLog()`。

`_resumeFromLog()` 扫描日志并维护 `openCallIds`：

- 遇到 assistant tool call：加入 `openCallIds`；
- 遇到匹配 toolResult：移除；
- 遇到下一条 user/assistant：调用 `_closeOrphanToolCalls()`，在**内存**插入占位 tool message。

但热会话中：

- `appendAssistantMessage()` 没有同步更新 `danglingToolCalls`；
- `appendUserMessage()` 不检查当前 `messages` 尾部是否有未闭合 call；
- stop 也没有写 cancellation result。

所以同一个缓存 agent 继续运行时，恢复逻辑根本不会触发。

### 7.2 目标日志的只读冷重放结果

按现有 `_resumeFromLog()` 逻辑只读模拟：

| 截止日志行 | 重放结果 |
|---|---|
| L101（仅到中断后的 assistant） | `dangling=[tool_MAIQ...]`，无占位 tool |
| L102（已有下一条 user） | 在 assistant 与 user 之间插入内存占位 tool；`dangling=[]` |
| L107（当前完整日志） | 同样插入一个内存占位 tool；三个失败 user 都保留；`dangling=[]` |

占位内容为：

```text
该工具调用因会话中断未完成。
```

该占位只追加到 `self.messages`，不会回写 JSONL。

### 7.3 冷恢复存在两种截然不同的语义

#### 情形 A：日志末尾停在 L101，没有后续 user

冷恢复会把 `tool_MAIQ...` 放进 `danglingToolCalls`。下一条用户消息到来时，`driveUserMessage()` 会：

1. 暂存用户消息；
2. **重新执行 dangling askSubAgent**；
3. 工具完成后再追加用户消息。

这意味着一个被用户主动停止、且可能已经产生文件副作用的工具，可能在进程重启后被自动重跑。对 `bash`/`askSubAgent` 都有重复副作用风险。

#### 情形 B：像目标 session 一样，日志已有 L102 user

冷恢复会在内存插入 cancellation 占位，不再重跑工具；下一次 request 的协议序列会变合法。

因此：

- **对本目标 session**，agent 真正重建后，现有代码大概率能在内存层自愈协议；
- **对通用 stop 场景**，不能把“stop 后直接 drop/rebuild agent”当成根修，因为若还没有后继 user，它反而可能重跑已取消工具。

浏览器刷新/重新打开页面不一定会重建后端 `agentCache`，所以不能解释为可靠恢复手段。

---

## 8. 根因分层

### 8.1 触发条件

用户在 assistant tool call 已持久化、但匹配 tool result 尚未持久化的窗口内，对可中断工具执行 stop。

### 8.2 直接原因

下一次模型请求中出现：

```text
assistant(tool_calls) → user
```

缺少 provider 要求的对应 `role=tool` 消息。

### 8.3 代码根因

`driveToolBatch` 和 `driveConfirmation` 捕获 `modelInterruptedError` 后，在 `currentConversation.addToolResult(...)` 前直接返回。

确认路径同样存在：`agent.py:167-179` 中，批准后的工具若被 stop，`except modelInterruptedError: return` 也会留下同类缺口。

### 8.4 设计根因

`stopResponsivenessPlan.md` L3.5 明确要求中断异常不得落入“工具执行异常 → toolResult”分支，理由是避免把用户停止当普通失败继续发给模型。这个目标只完成了一半：

- **正确部分**：停止后不应继续执行剩余工具或立即继续模型循环；
- **遗漏部分**：即使不继续模型，也必须为已经持久化的每个 tool call 写一个 cancellation tool result，保持 transcript 合法。

方案把以下两件事错误地绑定在一起：

```text
“写 toolResult” ≈ “继续让模型处理工具失败”
```

实际上可以、也应该分离为：

```text
写 cancellation toolResult（闭合协议）
+ 终止本轮（不继续调用模型）
```

### 8.5 不完整的不变式审查

`stopResponsivenessPlan.md:170` 声称：

> assistant 消息落盘：停止路径在 appendAssistantMessage 前 break/return → jsonl 无半截 assistant 消息。

这只覆盖“模型流式输出期间 stop”，不覆盖“模型已经返回 tool_calls、assistant 已落盘、工具执行期间 stop”。L3.5 后新增了第二种停止窗口，但没有重新审查该不变式。

方案只记录了“中断后工具卡片定格 running”的 UI 副作用，没有审查 OpenAI transcript 配对不变式。

### 8.6 防线缺口

1. 没有针对“工具 stop 后再发一条真实消息”的协议回归验证。
2. G7 虽写了“会话可立即发新消息”，验收重点是进程能被杀、锁能释放、输入框可用，没有验证下一次 request 的 `messages` 合法性。
3. 当前仓库未发现覆盖该路径的自动化测试文件。
4. provider request 前没有本地 tool-call 配对校验。
5. stop/interrupt 没有持久化审计事件，导致精确时序只能推断。
6. parent askSubAgent start 记录里没有直接持久化 child session id/PID，父子关联只能靠 model、prompt、workDir 和时间完成。

---

## 9. 回归来源

Git blame 显示以下关键行均由 `e49de66` 引入：

- `builtinTools.py:159-183`：轮询 interrupt，kill 后抛 `modelInterruptedError`；
- `toolRuntime.py:58-59`：中断异常直通；
- `agent.py:311-314`：中断后在 `addToolResult` 前 `return True`；
- `agentManager.py:160-173`：stop 主动触发 `interruptActiveStreams` 并立即收尾。

该 commit 之前，`askSubAgent`/`bash` 使用阻塞式 `subprocess.run`：stop 虽然响应慢，但工具最终返回后仍会先 `addToolResult`，然后泵观察到 `stopFlag` 再结束。因此旧行为在响应速度上差，但 transcript 通常完整。

`e49de66` 修复了“function call 期间停止不响应”，同时引入了“快速停止后 transcript 未闭合”的回归。

批量工具的“整个可执行前缀先 Start、再串行执行”由更早的 `1250f868` 引入；它不是本次单调用事故的直接原因，但会放大中断缺口。

---

## 10. 影响面

### 10.1 路径矩阵

| 停止位置/工具 | 是否可能产生本缺口 | 原因 |
|---|---:|---|
| 模型还在流式输出，尚未形成 final assistant | 否 | stop 在 `appendAssistantMessage` 前退出 |
| 模型重试退避中 | 否 | 尚无新的 assistant tool call 落盘 |
| `read/write/edit` 执行中 | 当前通常否 | 这些同步工具不检查 interruptEvent，会执行完并先写 toolResult，再由泵停流 |
| `bash` 运行中 | **是** | `_runWithInterrupt` 可抛中断异常 |
| `askSubAgent` 运行中 | **是** | 与本事故完全一致 |
| 已批准的长 bash/工具执行中 | **是** | `driveConfirmation` 同样在 addToolResult 前 return |
| 工具结果已经持久化后再 stop | 否 | tool-call 已闭合 |
| 未来任何抛 `modelInterruptedError` 的工具 | **是** | runtime/agent 通用直通路径 |

### 10.2 批量 tool calls 放大效应

`driveToolBatch` 会先对整个可执行前缀 yield `toolCallStart`，再串行执行。

若 assistant 一次返回 `N` 个 calls，并在第 `k` 个可中断工具执行时 stop：

- `0..k-1`：若已完成，则已有 toolResult；
- `k`：当前调用缺少 toolResult；
- `k+1..N-1`：尚未执行，也缺少 toolResult；
- 若后面还有 requiresApproval 的 call，它们同样属于原 assistant 的 tool call 集合，也必须闭合。

所以一次 stop 可能制造多个未响应 id，不限于一个 `askSubAgent`。

### 10.3 用户与数据影响

- 同一热 agent 生命周期内，该 session 后续模型调用持续 400，表现为“会话坏掉”。
- 每次失败前 userMessage 已经持久化；本事故额外积累了三条未得到 assistant 回答的用户消息。
- 工具停止不是事务回滚：子代理在 stop 前写入的源码改动全部保留。
- 父工具没有最终 toolResult，主代理拿不到子代理最终报告，无法正常进入验收阶段。
- 冷恢复时可能自动重跑日志尾部 dangling 工具，存在重复写文件、重复命令等副作用风险。
- 没有证据显示 provider 服务、模型推理内容或 token 上限导致本事故。

---

## 11. 为什么重复发消息不能恢复

第一个 400 后：

1. assistant tool call 仍在热 `messages` 中；
2. 没有补 tool result；
3. 模型错误只写 `modelError` 审计事件，不修改 `messages`；
4. userMessage 不回滚；
5. 下一次发送再追加一个 user，仍携带最早的协议缺口。

所以 L104/L106 的“？”不是新的根因，只是让同一非法 prefix 再请求两次。继续重试只会继续追加无人回答的 user 消息。

---

## 12. 修复建议（本次不实施）

### 12.1 立即止血原则

1. 发现 provider 返回“assistant tool_calls 缺少 tool response”后，不要让用户继续盲目重试；先本地标记 session transcript 非法。
2. 不建议直接手工编辑原 JSONL；错误 call id、顺序或重复结果都可能进一步破坏审计与恢复。
3. 对本目标 session，因 L102 已存在，**后端 agent 真正重建**后现有冷恢复逻辑会在内存插入占位 tool，通常可恢复协议；但要保留三条已落盘 user 的语义影响。
4. 不要把“stop 后统一 dropAgent”当通用止血：若日志还停在 assistant tool call 尾部，冷恢复会把它当 dangling 并重新执行。
5. 风险最低的用户级临时绕行是新建会话；旧会话保留用于审计，待正式恢复工具处理。

### 12.2 推荐根修：取消也必须闭合 transcript，但不得继续模型

在捕获 `modelInterruptedError` 时：

1. 对当前 assistant 批次中所有尚未拥有结果的 call，逐个构造 cancellation `toolResult`；
2. 调用 `currentConversation.addToolResult(...)` 持久化；
3. 内容明确：工具因用户停止未完成，**停止前可能已有副作用**；
4. 可在 details 中记录 `cancelled/userStopped`；
5. 不再执行剩余工具；
6. 不在当前停止轮继续调用模型；
7. 返回/结束生成器，让 pump 完成 stopped UI 收尾；
8. cancellation 必须经 `addToolResult` 真正落入父 conversation/JSONL，不能只作为 SSE `stopped` 或内存临时占位；
9. resume 遇到已有正常/失败/cancelled result 的 id 时不得重跑工具、不得再次插入占位；对日志尾部无 result 的 crash-dangling 与有明确 user-cancel 审计的调用必须分流；
10. interrupt 收尾、request preflight 自愈、重复 stop 三条路径共享“仅补 unresolved id”的幂等规则，保证每个 call id **有且只有一个** result。

核心语义应是：

```text
持久化 cancellation toolResult  ≠  继续模型循环
```

这同时满足：快速停止、无后续模型输出、消息协议合法。

### 12.3 批量与确认路径必须一起修

- `driveToolBatch`：从当前 call 开始，闭合该 assistant 批次里所有 unresolved ids，而不只当前一个；不要继续沿用语义含混的 `return True` 来同时表示“confirmation 终态”和“interrupt 已收尾”，至少应让调用方能区分二者。
- `driveConfirmation`：批准后执行被中断时，闭合 current 与后续 unresolved calls。
- 已完成的 calls 不得重复写结果。
- 二次 stop/竞态收尾必须幂等，确保每个 id 恰好一个 toolResult。

### 12.4 防御性自愈

根修之外，建议在发送 provider request 前增加 sequence preflight：校验动作本身只读；只有识别出“明确 user-cancelled 且仍 unresolved”的 id 时，才进入下述受幂等规则约束的自愈写入：

- 检查 assistant tool calls 是否在下一条非 tool 消息前全部闭合；
- 发现非法时在本地阻断，不把确定会 400 的 payload 发给 provider；
- 对明确的 user-cancelled 状态可补 cancellation result，但只能补当前仍 unresolved 的 id，并与 interrupt 收尾共用幂等账本，禁止产生第二个 `role=tool`；
- 对未知崩溃状态不要静默重跑有副作用工具，应走显式恢复策略。

同时可让热 conversation 维护 unresolved call 集合，避免 dangling 只在 cold resume 时可见。

### 12.5 用户消息持久化边界

当前 userMessage 在模型请求前落盘。建议至少保证：

1. 在 append 新 user 前先验证历史 prefix 合法；
2. 若历史已非法，先恢复/拒绝本地发送，不继续污染日志；
3. 明确模型请求失败时 user 是否保留为待重试 turn，避免每次点击都增加独立未回答消息。

### 12.6 可观测性

建议持久化以下审计字段/事件：

- `stopRequested`：sessionId、时间、活跃阶段；
- `toolCancelled`：toolCallId、toolName、原因、是否已启动、PID/PGID；
- askSubAgent parent call id ↔ child session id；
- process exit signal/return code；
- cancellation cleanup 写入了哪些 tool results；
- 本地 sequence validator 的诊断结果。

这样下次无需依赖文件 mtime 和 prompt 相似性做父子关联。

---

## 13. 后续验收用例

后续修复至少覆盖：

1. **单 askSubAgent 中断**：assistant call 后恰有一个 cancelled tool result；停止轮不继续模型；下一条用户消息可成功请求。
2. **单 bash 中断**：进程组被终止，结果闭环，下一条消息成功。
3. **普通失败对照**：无效 provider、非零退出仍各写一个 isError tool result。
4. **timeout 对照**：timeout 写 tool result，不与 user stop 混淆。
5. **批量首项中断**：当前及所有后续 unresolved calls 均被取消闭合；已完成项不重复。
6. **批量中间项中断**：前序 success 结果保留，后序 cancellation 结果完整。
7. **确认后中断**：approved 工具执行中 stop，pending 状态和所有 call ids 正确收尾。
8. **结果落盘后 stop 竞态**：不能再补第二个 cancellation result。
9. **重复 stop**：幂等，无重复 tool result。
10. **热会话下一条消息**：无需重启即可成功。
11. **冷恢复（日志尾部 assistant）**：明确区分 user cancel 与进程 crash；被取消工具不得自动重跑。
12. **冷恢复（assistant→user 历史缺口）**：旧受损日志可确定性修复，且不会永久修改原审计记录，除非有显式迁移流程。
13. **副作用文案**：取消结果不得声称工具“完全未执行”；应提示停止前可能已改文件/执行命令。
14. **request preflight**：构造非法序列时在本地失败，禁止发真实 provider 请求。

验收红线：

```text
任何已持久化的 assistant tool call，
在下一条非 tool 消息出现前，
必须有且只有一个对应 tool result。
```

---

## 14. 五问归纳

1. **为什么报 400？**  
   provider 收到 assistant tool call 后直接跟 user，缺少 tool response。

2. **为什么缺少 tool response？**  
   工具被 stop 后抛 `modelInterruptedError`，agent 在 `addToolResult` 前返回。

3. **为什么要这样返回？**  
   为了避免把用户停止包装成普通工具失败并继续模型，实施选择了异常直通。

4. **为什么没有另行写 cancellation result？**  
   停止方案只把 UI/进程终止视作终态，没有把 transcript 闭环作为独立不变式；并错误地认为停止路径总在 assistant 落盘前结束。

5. **为什么测试没发现？**  
   验收验证了 stop 响应速度、进程终止、锁释放和输入框可用，没有验证 stop 后下一次真实 request 的消息协议。

---

## 15. 最终判定

| 维度 | 判定 |
|---|---|
| 故障性质 | 本地 runtime 会话一致性 bug |
| 是否 provider 偶发 | 否 |
| 是否可稳定复现 | 是：可中断工具运行中 stop，再发消息 |
| 直接责任路径 | `modelInterruptedError` → `driveToolBatch/driveConfirmation` 提前 return → 无 `addToolResult` |
| 根因提交 | `e49de66` |
| 当前目标 session | 事故时热缓存持续中毒；冷重建因已有 L102 user 可在内存自愈，但失败 user 会保留 |
| 数据回滚 | 无；工具停止前的文件副作用保留 |
| 推荐优先级 | P1：先保证 cancellation toolResult 闭环，再补本地 validator 与审计 |
| 本次是否改代码 | **否** |

最终结论：**工具中断功能修复了“停不下来”，却遗漏了 function-call 协议的持久化收尾。错误并非发生在下一条用户消息本身；下一条消息只是第一次把此前已被 stop 破坏的 transcript 重新提交给 provider，从而暴露 400。**
