# Function Call 中断后 Transcript 闭环修复方案

- Author: wilbur
- Version: 1.1
- Date: 2026-08-17
- 溯源依据：`docs/toolCallInterruptionIncidentReport.md`（事故 session_3ebfe236ecae）
- 回归提交：`e49de66`（工具执行可中断与停止收尾闭环）
- 范围：修复 `modelInterruptedError` 路径缺少 `addToolResult` 导致的 transcript 协议破坏；不动 stop 响应速度语义，不改已受损历史日志。
- v1.1：按子代理审核意见修订——修正 P0-1/P0-2 闭合范围语义（放弃 executedCallIds 集合传参，改为「闭合中断点之后整个 assistant 批次」）；补 preflight 判定伪代码；stopRequested 会话级去重；resume 两类日志形态处置表格化；取消卡片前端渲染诚实表述（error 态而非 cancelled 专用态）；调整 T2/T4 顺序并补 agent.py:183 引用处置。
- v1.2：自复核修订（复审子代理超时后由作者对照源码完成）——① 闭合起点写死代码级实现形态：step 3 prefix exec 循环改为 `enumerate(prefix)`，`groupOffset` 为组内下标，闭合起点 = 外层 `index`（prefix 组在 toolCalls 中的起始下标，step 4 的 `index += len(prefix)` 之前有效）+ `groupOffset`；② preflight 伪代码补「assistant 之后只允许 tool 消息」的显式约束与缺口后已混排 user 的说明。

---

## 1. 问题复述（一句话）

工具被 stop 后 `driveToolBatch` / `driveConfirmation` 在 `addToolResult` 之前 return，留下 `assistant(tool_calls) → user` 的非法序列，下一次请求被 provider 以 400 拒绝，且热会话持续中毒。

## 2. 修复目标（可验证成功标准）

不变式红线：

```text
任何已持久化的 assistant tool call，
在下一条非 tool 消息出现前，
必须有且只有一个对应 tool result。
```

验收目标：

1. 单 `askSubAgent`/`bash` 中断后，父 JSONL 中该 call id 恰有一条 cancellation `toolResult`；停止轮不再调用模型；同会话下一条用户消息请求成功（不再 400）。
2. 批量 N 个 tool calls 在第 k 个中断时：0..k-1 已有结果不重复写，**k..N-1 全部写 cancellation result（含尚未 Start 的 requiresApproval call）**。
3. 确认路径（approved 后执行中被 stop）闭合 `pending.toolCalls[currentIndex:]` 全部。
4. 重复 stop / stop 与结果落盘竞态：幂等，不产生第二个 result。
5. 冷恢复语义分流：日志尾部 dangling call 一律不自动重跑（消除副作用风险），统一持久化 cancellation。
6. 中断收尾正常完成后持久化 `stopRequested` 审计事件，单次 stop 全局只写一条。

## 3. 总体设计

核心语义分离（报告 §12.2）：

```text
持久化 cancellation toolResult（闭合协议）  ≠  继续模型循环
```

三个改动层面，按优先级：

| 层 | 内容 | 性质 |
|---|---|---|
| L1 根修 | 中断异常捕获点补写 cancellation toolResult，且不继续模型 | 必须 |
| L2 审计 | 持久化 stopRequested 事件，供冷恢复分流与溯源 | 必须 |
| L3 防御 | request preflight 本地校验 + 自愈兜底 | 增强 |

### 3.1 L1 根修设计

**核心方法：`agent.py` 新增私有生成器 `closeUnfinishedToolCalls`**

闭合范围语义（v1.1 修正，消除审核 P0-1/P0-2）：**不接受 executedCallIds 集合参数，而是闭合「指定起始 index 到批次末尾」的全部 call**。调用方负责给出正确起点：

```python
def closeUnfinishedToolCalls(
    self,
    currentConversation: conversation,
    toolCalls: list[toolCall],
    startIndex: int,
    reason: str,
) -> Iterator:
    # 闭合 toolCalls[startIndex:] 全部 call：每个恰写一条 cancellation toolResult + yield toolCallEndEvent。
    # 调用前提：toolCalls[:startIndex] 已全部有 result（由调用方流程保证），本方法不重复闭合。
    self.logStopRequestedOnce(currentConversation)
    content = '该工具调用因用户停止未完成；停止前可能已产生文件或命令副作用。'
    if reason == 'crashRecovered':
        content = '会话恢复时发现该工具调用未完成；为避免重复副作用未重新执行，停止前可能已产生文件或命令副作用。'
    elif reason == 'preflightRepair':
        content = '检测到该工具调用缺少结果（协议自愈补齐）；停止前可能已产生文件或命令副作用。'
    for call in toolCalls[startIndex:]:
        result = toolResult(
            toolCallId=call.id,
            toolName=call.toolName,
            isError=True,
            content=content,
            details={'cancelled': True, 'reason': reason},
        )
        currentConversation.addToolResult(result)
        yield toolCallEndEvent(toolResult=result)
```

**改动点 1：`agent.driveToolBatch`（prefix exec 循环，`except modelInterruptedError` 处）**

当前：

```python
for call, definition in prefix:
    try:
        result = self.makeUnknownToolResult(call) if definition is None else self.executeToolCall(call, sessionId)
    except modelInterruptedError:
        return True
    currentConversation.addToolResult(result)
    yield toolCallEndEvent(toolResult=result)
```

改为在循环内跟踪当前执行位置，中断时闭合「当前位置到整个批次末尾」。代码级实现形态（写死，实施者照此改）：

```python
# step 3) 前缀串行 exec + End；enumerate 维护组内下标 groupOffset
for groupOffset, (call, definition) in enumerate(prefix):
    try:
        result = self.makeUnknownToolResult(call) if definition is None else self.executeToolCall(call, sessionId)
    except modelInterruptedError:
        # 闭合起点 = 外层 index（当前 prefix 组在 toolCalls 中的起始下标，此时尚未执行 step 4 的 index += len(prefix)，值有效）
        #          + groupOffset（组内下标，即当前中断 call 本身）。
        # 闭合范围 = 当前中断 call 起到 toolCalls 末尾，含 prefix 之后尚未 Start 的 requiresApproval call（审核 P0-1）。
        yield from self.closeUnfinishedToolCalls(currentConversation, toolCalls, index + groupOffset, 'userStopped')
        return True
    currentConversation.addToolResult(result)
    yield toolCallEndEvent(toolResult=result)
```

实现要点：外层 `index` 变量在 step 3 执行期间尚未推进（`index += len(prefix)` 在 step 4），故 `index + groupOffset` 精确指向当前中断 call 在 `toolCalls` 中的位置。prefix 内当前 call 之前的 call 已 `addToolResult`，位于起点之前，不会被重复闭合；prefix 之后（`index + len(prefix)` 起）的 requiresApproval call 尚未 Start 也未 setPending，同样闭合——这是批次合法性的硬要求。注意 prefix 可能跨多个「可执行前缀组」（while 外层多轮迭代）：每组 step 3 中断时，外层 `index` 已是本组起始下标（上一组结束后已推进），语义自洽。

**改动点 2：`agent.driveConfirmation`（approved 执行 `except modelInterruptedError` 处）**

闭合范围写死（消除审核 P0-2 的歧义表述）：**闭合 `pending.toolCalls[pending.currentIndex:]` 全部**（current 正在执行 + 后续未执行）。`< currentIndex` 的 call 在此前的 driveToolBatch 前缀或更早确认轮已闭合，不触碰：

```python
try:
    result = self.executeToolCall(currentCall, sessionId)
except modelInterruptedError:
    yield from self.closeUnfinishedToolCalls(
        currentConversation, pending.toolCalls, pending.currentIndex, 'userStopped'
    )
    return
```

**改动点 3：`toolRuntime.py` 不变**

`modelInterruptedError` 直通语义保留——它仍是正确的传输机制；闭环责任在 agent 层（能拿到完整批次上下文）。

**`return True` 语义**：中断分支补上 transcript 终态后，返回值「已产出终态/调用方应结束」与实际一致，**不引入新返回枚举**（精准修改原则）。

### 3.2 L2 审计设计

**`stopRequested` 事件**（新 JSONL 事件类型）：

```json
{"type": "stopRequested", "phase": "toolExecution", "sessionId": "...", "unclosedCallIds": [...]}
```

- **写入点**：`closeUnfinishedToolCalls` 内的 `logStopRequestedOnce`（agent 捕获点一定能拿到 conversation logger；agentManager.requestStop 拿不到需透传，排除）。
- **单次 stop 全局只写一条的去重机制**（审核 P1-2）：在 `conversation` 实例上加内存标志 `_stopRequestedLogged: bool`（init False），`logStopRequestedOnce` 检查并置位。该标志只在热 conversation 生命周期内有效——同一中断无论经过 driveConfirmation 还是 driveToolBatch 哪个生成器，都只写第一条。resume 重建的 conversation 重新从日志判断：`_resumeFromLog` 见到 `stopRequested` 事件则置 True（恢复后不再重复写）。
- **对现有 JSONL 消费者的影响**（审核 P1-4，已验证，显式记录结论）：
  - `_resumeFromLog`：if/elif 链无 else，未知类型自然跳过不进 messages；本方案再加显式 `elif eventType == 'stopRequested'` 分支读取并置标志，意图清晰。
  - `historyView.loadMessages`：只处理 userMessage/assistantMessage/toolResult，stopRequested 不进 DTO，无副作用。
  - `usageStore` 回填：只挑 `type == 'assistantMessage'`，无副作用。

**冷恢复分流**（修复报告 §7.3 情形 A 的重跑风险；审核 P1-3 表格化）：

| 日志尾部形态 | 处置路径 | 是否落盘 |
|---|---|---|
| assistant→user/assistant 之间已有缺口（如事故日志 L101→L102） | 现有 `_closeOrphanToolCalls` **内存占位**（保留不动） | 否（不回写，保护原审计记录） |
| 尾部停在 assistant tool_calls（无后续非 tool 消息）+ 有 stopRequested | resume 末尾持久化 cancellation，reason=`userStopped` | 是 |
| 尾部停在 assistant tool_calls + 无 stopRequested（进程 crash） | resume 末尾持久化 cancellation，reason=`crashRecovered` | 是 |

实现：`_resumeFromLog` 结束时若 `openCallIds` 非空（即原 dangling 情形），不再填充 `self.danglingToolCalls`，而是：

1. 用 `_collectDanglingCalls(openCallIds)` 取得 dangling call 对象列表；
2. 逐个构造 cancellation `toolResult`（reason 按上表区分）调 `self.addToolResult(...)` **持久化**（写 JSONL + 内存 messages，保证消息顺序：这些 tool 消息追加在当前 messages 尾部，恰好接在最后一条 assistant 之后，序列合法）；
3. `danglingToolCalls` 恒为空。

**行为变更：删除「resume 后自动重跑 dangling 工具」逻辑**——自动重跑有副作用工具（bash/askSubAgent）永远是危险默认；模型拿到 cancellation result 后可自行决定重新发起调用，决策权交还模型/用户。

### 3.3 L3 防御层：preflight 自愈

`driveUserMessage` 在 `appendUserMessage` 之前执行 preflight。判定算法（审核 P1-5，补伪代码）：

```python
def findUnclosedTailCallIndex(self, currentConversation) -> tuple[list[toolCall], int] | None:
    # 从尾部找最近一条带 toolCalls 的 assistant；统计其后已闭合的 tool message id 集合。
    # 若该 assistant 之后、下一条非 tool 消息之前的配对不完整，返回 (该批次 toolCalls, 第一个未闭合 call 的下标)；完整则返回 None。
    messages = currentConversation.messages
    assistantIndex = None
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].role == 'assistant' and messages[i].toolCalls:
            assistantIndex = i
            break
        if messages[i].role == 'assistant':
            return None  # 尾部是纯文本 assistant，无缺口
    if assistantIndex is None:
        return None
    calls = messages[assistantIndex].toolCalls
    tail = messages[assistantIndex + 1:]
    # 显式约束：assistant 之后、下一条非 tool 消息之前应全是 tool 消息。
    # 缺口后已混排 user（事故形态）时，tail 含 user——其 toolCallId 为 None 不进 closedIds，
    # 缺口 call 仍会被检出并自愈，行为正确；此处仅注释说明，无需额外分支。
    closedIds = {
        m.toolCallId for m in tail
        if m.role == 'tool' and m.toolCallId
    }
    for position, call in enumerate(calls):
        if call.id not in closedIds:
            return (calls, position)
    return None
```

正常流程下该不变式恒成立（根修后），preflight O(尾部消息数) 开销可忽略，**不会误伤**：只有配对不完整才返回缺口，且闭合范围 = 第一个未闭合 call 起到批次末尾（与根修同语义）。发现缺口时 `yield from closeUnfinishedToolCalls(..., reason='preflightRepair')` 后再 append user——user 永远不会追加到非法序列之后，确定 400 的 payload 不会发给 provider。

多轮会话正确性说明：倒序扫描命中的是**最新一条带 toolCalls 的 assistant**；历史轮次的 assistant+tool 组在其之后必有更新的 assistant/user 消息，不会被误命中；若最新 assistant 为纯文本（无 toolCalls）则直接返回 None。

不维护增量 unresolved 集合（preflight 直接扫尾部，保持简单）；adapter 层不做硬阻断（preflight 已覆盖，避免重复防御）。

### 3.4 用户消息持久化边界（报告 §12.5）

「append user 前历史必合法」由 preflight 保证。模型请求失败时 user 保留语义不变（现状：落盘即保留，失败记 modelError）——本次不改，避免范围蔓延。

### 3.5 受影响文件清单

| 文件 | 改动 |
|---|---|
| `flamingoAgents/core/agent.py` | 新增 closeUnfinishedToolCalls / logStopRequestedOnce / findUnclosedTailCallIndex；driveToolBatch 中断分支闭环（含 prefix 外 requiresApproval）；driveConfirmation 中断分支闭合 [currentIndex:]；driveUserMessage 删 dangling 重跑分支（agent.py:135-143）改为 preflight；删除 driveConfirmation 收尾的 queued 死代码（agent.py:183-185） |
| `flamingoAgents/core/conversation.py` | `_resumeFromLog`：stopRequested 显式识别并置 `_stopRequestedLogged`；尾部 dangling 持久化 cancellation（不再填 danglingToolCalls）；删除 takeDanglingToolCalls/setQueuedUserMessage/takeQueuedUserMessage/danglingToolCalls/queuedUserMessage/_collectDanglingCalls（`_collectDanglingCalls` 逻辑内联进 resume 尾部处理）；`_closeOrphanToolCalls` 内存占位保留不动；文件头版本+说明 |
| `flamingoAgents/core/types.py` | 不改（复用 toolResult.details；stopRequested 用 dict 字面量） |
| `flamingoAgents/tools/toolRuntime.py` | 不改 |
| `webApp/backend/agentManager.py` | 不改（审计写入点收在 agent 层） |
| `webApp/frontend` | 不改 |

**前端渲染诚实说明**（审核 P1-6）：cancellation result 的 `isError=True`、`details.reason='userStopped'`，前端 `statusFromResult` 无 cancelled 专用分支，卡片渲染为 **error（失败）态**、内容显示取消文案——效果是「定格为失败样式卡片」，不是 cancelled 专用样式。补发 toolCallEndEvent 的价值是卡片不再永远 running。专用 cancelled 样式不做。

不做：adapter 层 payload 硬阻断、历史受损日志迁移工具、前端取消卡片专属样式、usage 统计口径调整。

## 4. 兼容性与风险

1. **历史受损会话**（session_3ebfe236ecae）：根修不改既有日志。冷重建走新 `_resumeFromLog`：L101→L102 缺口属「assistant→user 已有缺口」，走 `_closeOrphanToolCalls` 内存占位（不落盘），下次请求合法；三条已落盘失败 user 保留（语义影响见报告 §10.3）。
2. **SDK/子代理**：`sdkEntry.py` 走同一 runUserMessageStream/continueConfirmationStream 路径，自动受益；子会话被 kill 时进程已死，无 resume，无影响。
3. **行为变更**：resume 后不再自动重跑工具——有意的安全修复，commit message 中明确。
4. **并发/幂等**：所有闭环写入在持有会话锁的生成器内；重复 stop 由 `_sealStopped`/`doneEvent` 幂等早退；`logStopRequestedOnce` 会话级标志保证单条；resume 从日志恢复该标志防跨重启重复。
5. **hasPendingConfirmation**：中断后 `takePending` 已 pop pending，`hasPending` 为 False，下一条 user 不被 `pendingConfirmationExists` 阻挡（已验证无回归）。
6. **usage 统计**：stop 轮不调模型，cancellation 不写 usage；`_recordUsage` 幂等，无影响。
7. **token**：cancellation 文案每 call 约 40-50 token，可接受。

## 5. TODO List（执行顺序，v1.1 已修正依赖顺序）

1. [ ] **T1 agent.py：新增三个私有方法（closeUnfinishedToolCalls / logStopRequestedOnce / findUnclosedTailCallIndex）**
   - 改动：按 §3.1/§3.2/§3.3 伪代码实现；conversation 上需先有 `_stopRequestedLogged` 属性（见 T2，实现时 T1/T2 同一次编辑完成，提交粒度按 TODO 顺序编译通过即可——此处顺序调整：T1 依赖 T2 的属性，故 **T1 与 T2 同步实施、一次提交**，或 T2 先行）。
   - 验证：假 conversation 脚本调用 closeUnfinishedToolCalls：每个未闭合 id 恰一条 toolResult + toolCallEndEvent，JSONL 恰一条 stopRequested，二次调用不再写 stopRequested。

2. [ ] **T2 conversation.py：`_resumeFromLog` 分流 + `_stopRequestedLogged` 属性**
   - 改动：init 加 `_stopRequestedLogged = False`；`_resumeFromLog` 显式识别 stopRequested（置标志、不进 messages）；尾部 dangling 按 §3.2 表格持久化 cancellation；不再填充 danglingToolCalls。
   - 验证：只读重放事故日志三种截断（L101/L102/L107）：L101 截断 → 尾部持久化 cancellation(userStopped 或 crashRecovered，视是否注入 stopRequested 测试样本)、messages 序列合法、dangling 为空；L102/L107 → 内存占位在 assistant→user 处插入一次、无持久化新增、无重复。正常完整会话日志重放无任何新增事件。

3. [ ] **T3 agent.py：driveToolBatch 中断分支闭环**
   - 改动：prefix exec 循环 enumerate 维护组内偏移；`except modelInterruptedError` 处 `yield from closeUnfinishedToolCalls(currentConversation, toolCalls, index + groupOffset, 'userStopped')` 后 `return True`。
   - 验证：mock 工具在第 k 个抛 modelInterruptedError：JSONL 中 0..k-1 原结果各一条、k..N-1 cancellation 各一条（含 k 之后未 Start 的 requiresApproval call）；无重复 id；停止轮无模型请求。

4. [ ] **T4 agent.py：driveConfirmation 中断分支闭环 + 删除三处 dangling/queued 引用**
   - 改动：approved 执行中断闭合 `pending.toolCalls[currentIndex:]`；删除 driveUserMessage 的 dangling 重跑分支（agent.py:135-143，替换为 preflight 调用）；删除 driveUserMessage 与 driveConfirmation 收尾的 takeQueuedUserMessage 死代码（agent.py:141-143、183-185，共三处引用，审核 P2-1）。
   - 验证：确认后 bash 执行中 stop：pending 已 pop、current 及后续 call 全闭合；grep 全仓无 takeDanglingToolCalls/setQueuedUserMessage/takeQueuedUserMessage 引用（此时 conversation.py 方法定义仍在但已无调用方）。

5. [ ] **T5 conversation.py：删除 dangling/queued 死代码定义**
   - 改动（审核 P2-2：必须在 T4 之后执行）：删除 takeDanglingToolCalls/setQueuedUserMessage/takeQueuedUserMessage/danglingToolCalls/queuedUserMessage；`_collectDanglingCalls` 逻辑内联进 T2 的尾部处理后删除独立方法；清理 resume debug 日志中的 dangling 字段。
   - 验证：grep 全仓零残留；`uv run python -c "from flamingoAgents.core.conversation import conversation"` 导入正常；resume 正常会话日志无异常。

6. [ ] **T6 端到端验收脚本（不引入测试框架，uv run python 脚本，放 scripts/ 或临时文件）**
   - 覆盖报告 §13 用例 1/2/3/4/5/6/7/8/9/10/11/14：单 askSubAgent 中断、单 bash 中断、普通失败/timeout 对照、批量首项/中间项中断（含批次尾部 requiresApproval 未 Start 的闭合）、确认后中断、结果落盘后 stop 竞态不重复闭合、重复 stop 幂等、热会话下一条消息成功、冷恢复两情形（userStopped/crashRecovered 均不重跑且持久化闭合）、preflight 阻断非法发送（人为构造缺口后发自愈）。
   - 验证：全部断言通过；事故日志只读重放三次结果一致（确定性）。

7. [ ] **T7 文件头版本更新 + commit**
   - conversation.py 1.10→1.11、agent.py 1.17→1.18，description 写清改动；按 git skill 提交，commit message 明确「resume 不再自动重跑工具」的行为变更。

注：TODO 中所有行号引用以实施时的函数体为准（行号随前置改动漂移，审核 P2-3）。

## 6. 验收红线复述

```text
1. 任何已持久化 assistant tool call → 有且只有一个 tool result。
2. stop 轮不再产生任何模型请求。
3. resume 不自动重跑任何工具。
4. 事故 session 冷重建后第一条新消息请求合法（不再 400）。
5. 批量中断时，中断点之后整个 assistant 批次（含未 Start 的 requiresApproval call）全部闭合。
```

## 7. 已解决的开放问题（v1.0 遗留，按审核意见定案）

1. ~~`return True` 语义是否拆分~~ → 不拆，补终态后语义一致（精准修改）。
2. ~~stopRequested 写入点~~ → agent 捕获点 closeUnfinishedToolCalls 内，conversation 实例标志去重。
3. ~~crashRecovered 文案是否区分~~ → 区分 content 与 details.reason（见 §3.1）。
4. ~~preflight 是否加前端 warning 事件~~ → 不加，debugConsole + 自愈。

## 8. 审核问题处理记录（v1.1）

| 审核编号 | 等级 | 处理 |
|---|---|---|
| P0-1 executedCallIds 来源未定义、漏闭合 prefix 外 requiresApproval | 🔴 | §3.1 改为「闭合中断点之后整个批次」，enumerate 维护组内偏移，验收目标 2 与红线 5 显式覆盖 |
| P0-2 driveConfirmation 闭合范围表述矛盾 | 🔴 | §3.1 写死闭合 `[currentIndex:]`，删除「已闭合集合」表述 |
| P1-1 批量+确认混合场景闭合完整性 | 🟡 | 随 P0-1 修复，T3 验证点含「未 Start 的 requiresApproval」 |
| P1-2 stopRequested 跨生成器重复 | 🟡 | §3.2 conversation 实例级 `_stopRequestedLogged` + resume 恢复 |
| P1-3 事故日志两语义并存需表格化 | 🟡 | §3.2 三类日志形态处置表 |
| P1-4 stopRequested 对消费者影响需显式记录 | 🟡 | §3.2 三条验证结论 |
| P1-5 preflight 判定算法缺失 | 🟡 | §3.3 findUnclosedTailCallIndex 伪代码 |
| P1-6 取消卡片实为 error 态 | 🟡 | §3.5 诚实说明 |
| P2-1 agent.py:183 queued 引用遗漏 | 🔵 | §3.5 清单 + T4 验证点 |
| P2-2 T2/T4 顺序矛盾 | 🔵 | TODO 重排为 T1-T5（删除定义最后） |
| P2-3 行号漂移 | 🔵 | §5 末尾注明 |
