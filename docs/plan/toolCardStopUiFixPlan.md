# 工具停止后卡片定格 running 修复方案

- Author: wilbur
- Version: 1.0
- Date: 2026-08-17
- 关联：`docs/toolCallInterruptionIncidentReport.md`（协议闭环已由 commit 426e410 修复）；本方案修复同一中断路径下的**前端卡片状态残留**问题。
- 范围：仅修复「用户停止后，running 工具卡片未收到 toolCallEnd、视觉残留执行中」的 UI 竞态。不改任何协议/后端落盘逻辑。
- v1.1：按子代理审核意见修订——① T1 关闭（`card.status` 字段已由 buildToolCard/setCardStatus 唯一维护）；② settleRunningCardsOnStop 尾部补 maybeScrollToBottom；③ 文案行加锚点注释；④ §7 补 pending 待确认与未 Start 被 close call 两条边界说明。

---

## 1. 问题复述

用户停止工具执行后，后端已正确完成协议闭环（`stopRequested` + cancellation `toolResult` 落盘，commit 426e410），但**前端 bash 工具卡片仍显示"执行中"**，不刷新。

## 2. 根因（已定位）

时序竞态：

1. 用户点停止 → `streamPump.requestStop()`（agentManager.py:160）立即：`stopFlag.set()` → `interruptActiveStreams` → `_sealStopped()`（广播 `stopped` 终态、关订阅、置 `doneEvent`）。
2. 工具线程中 `modelInterruptedError` 抛出 → `agent.closeUnfinishedToolCalls` 写 cancellation toolResult（成功）→ `yield toolCallEndEvent`。
3. 但 pump 消费循环（agentManager.py `_pump`）：

```python
for event in self.stream:
    if self.stopFlag.is_set():   # ← requestStop 已先置位
        break                     # ← 直接跳出
    self._broadcast(event)
```

`toolCallEndEvent` 在被 `_broadcast` **之前**就因 `stopFlag` 置位而 break，**没有推给前端**。前端只收到 `stopped`，`handleStreamError` 对 stopped 仅 `markInterrupted() + goIdle()`（chatView.js:810-814），**不触碰 running 卡片**，卡片于是定格"执行中"。

`toolCallEndEvent` 不在 `terminalEventTypes`（types.py:157）中，pump 不会因它收尾。

## 3. 修复目标（可验证）

1. 用户停止工具后，running 卡片**视觉定格为终态**（按 cancellation result 渲染为 error/失败态，内容显示"该工具调用因用户停止未完成…"），不再永远"执行中"。
2. 不破坏既有 stopped 语义：跨窗口 stopped 静默、连接关闭、输入框回空闲、usage 回写均不变。
3. 不重复推送：每张卡片只定格一次。

## 4. 方案选型

两个候选：

| 方案 | 位置 | 做法 | 评价 |
|---|---|---|---|
| **A（后端补推）** | `agentManager._pump` | `stopFlag` 置位后，把流中已产生的 `toolCallEndEvent` 仍 `_broadcast` 完再 break | 治本：事件本就该到前端；风险是改动 pump 临界区 |
| **B（前端收尾）** | `chatView.handleStreamError` stopped 分支 | 收到 stopped 时，把 `toolCards` 注册表里所有 running 卡片批量收尾 | 治标：不动后端；但前端拿不到 cancellation result 的 content/details，只能拍一个通用"已停止"文案，与历史渲染（error+真实取消文案）不一致 |

**推荐 A（后端补推）**：toolCallEndEvent 携带真实 cancellation result（isError/details/userStopped 文案），前端 `resolveToolCardOnEnd` 已有完整逻辑，补推后卡片定格状态与刷新后历史渲染**完全一致**。方案 B 会导致"停止时看到的卡片文案"与"刷新后看到的卡片文案"不一致。

## 5. 改动设计（方案 A）

### 5.1 pump 消费循环修改（agentManager.py `_pump`）

当前：

```python
for event in self.stream:
    if self.stopFlag.is_set():
        break
    self._broadcast(event)
    if isinstance(event, terminalEventTypes):
        break
```

改为：**`stopFlag` 置位后，仍放行 `toolCallEndEvent`（卡片配对终态），其余事件（delta/start/retry 等）继续丢弃**：

```python
for event in self.stream:
    if self.stopFlag.is_set():
        # 停止后仍补推工具配对终态（卡片定格），其余事件丢弃（stopped 已由 requestStop 广播）。
        if isinstance(event, toolCallEndEvent):
            self._broadcast(event)
        continue   # 注意：不 break——继续吃流直到流自然结束，把中断批次所有 toolCallEnd 都推完
    self._broadcast(event)
    if isinstance(event, terminalEventTypes):
        break
```

关键点：
- `stopFlag` 置位后改 `break` 为 `continue`，并对 `toolCallEndEvent` 特判补推——因为中断批次可能有**多个**未闭合 call（批量中断），会连续 yield 多个 toolCallEnd，必须全部推完，不能用 break 只推一个。
- `_broadcast` 内部已有 `doneEvent.is_set()` 守卫（agentManager.py:226-227）：`_sealStopped` 已置 `doneEvent`，补推时 `_broadcast` 会直接 return……

**⚠️ 阻塞点**：`_sealStopped` 在 `requestStop` 里已置 `doneEvent`，而 `_broadcast` 首行 `if self.doneEvent.is_set(): return`。这意味着即使 pump 调 `_broadcast(toolCallEnd)`，也会被 `doneEvent` 守卫吞掉！

### 5.2 解除 doneEvent 吞事件（关键）

必须让"停止后的 toolCallEnd"能穿透 `doneEvent` 守卫。两种做法：

- **A1**：`_broadcast` 加参数 `_broadcast(event, force=False)`，`toolCallEnd` 补推时 `force=True` 跳过 doneEvent 守卫。但 `_sealStopped` 已关订阅（`_closeSubscribersLocked` 放了哨兵 None），即使写入 history，订阅者也已关闭，**SSE 客户端收不到**——只进了 history，对已连接的当前窗口无效。
- **A2（重新排序）**：`requestStop` 不要那么早 `_sealStopped`。让 pump 先把流中 toolCallEnd 推完，再由 pump 自己 stopped 收尾。这牵动 `requestStop`/`_sealStopped`/`_pump` 三方的竞态红线（stopResponsivenessPlan G3：避免泵先关连接导致其他窗口收不到 stopped），**风险高**。

### 5.3 重新评估：方案 A 的复杂度超出预期

`_sealStopped` 关订阅 + 置 doneEvent 是为了保证 stopped 是 history 尾事件、且多窗口都收得到。若在 stopped 之后还要推 toolCallEnd，会破坏"stopped 必须是 history 最后一个事件"（`_trimHistoryIfNeeded` 注释也强调这点），且订阅已关、当前窗口收不到。

**结论：方案 A 会破坏 stopped 终态语义，不可行。改回方案 B（前端收尾），但解决文案一致性问题。**

## 6. 最终方案：前端收尾（方案 B 改进）

既然后端 stopped 已关闭事件通道，正确做法是让**前端在收到 stopped 时，主动把 running 卡片收尾**。文案一致性通过复用后端 cancellation 语义解决：

### 6.1 改动点（chatView.js）

`handleStreamError` 的 `stopped` 分支（chatView.js:810-814）：

```javascript
if (data.errorType === 'stopped') {
  settleRunningCardsOnStop();   // 新增：把所有 running 卡片定格为停止态
  markInterrupted();
  goIdle();
  return;
}
```

新增函数（放在 `resolveToolCardOnEnd` 附近，风格一致）：

```javascript
// 停止收尾：把仍 running 的卡片定格为「失败/已停止」，文案与后端 cancellation 一致
function settleRunningCardsOnStop() {
  Object.keys(toolCards).forEach(function (id) {
    var card = toolCards[id];
    if (card.status !== 'running') return;
    setCardStatus(card, 'error');
    card.resultSection.classList.remove('hidden');
    // 文案锚点：与 flamingoAgents/core/agent.py closeUnfinishedToolCalls contents['userStopped'] 保持一致，改文案需同步
    setCollapsibleText(card.resultPre, '该工具调用因用户停止未完成；停止前可能已产生文件或命令副作用。', card.resultSection);
  });
  maybeScrollToBottom(); // 审核 P1：与 resolveToolCardOnEnd 一致，定格后滚到底
}
```

文案直接复用后端 cancellation content（与刷新后历史渲染的取消文案一致），卡片定格为 error 态——与后端 `statusFromResult`（isError→error）刷新后的呈现完全一致。

### 6.2 卡片 status 字段确认（审核已关闭）

`card.status` 字段真实存在：`buildToolCard` 字面量初始化 `status: status`，`setCardStatus` 唯一维护 `card.status = status`（upsert 置 running、resolve 置终态、enterWaitingConfirm 置 pending、历史重建均走此函数，无旁路）。故 running 判定 = `card.status === 'running'`，§6.1 写法成立，原 TODO T1 关闭。

### 6.3 不改动项

- 后端 agentManager/agent/conversation 一律不动（协议闭环已完成，stopped 语义不动）。
- `_pump`/`_sealStopped`/`requestStop` 竞态红线不动。
- 卡片样式不动（复用现有 error 态样式）。

## 7. 边界与风险

1. **多窗口**：stopped 是后端广播的，每个连接窗口都会走 stopped 分支，各自收尾自己的 DOM，互不影响 ✅。
2. **无 running 卡片时**：forEach 无匹配，空操作 ✅。
3. **刷新后一致性**：刷新走 historyView（读 JSONL toolResult→statusFromResult→error+取消文案），与 stopped 时前端定格的 error+同文案一致 ✅。
4. **已定格卡片不重复收尾**：`card.status !== 'running'` 守卫 ✅。
5. **拒绝路径卡片**：rejected 态非 running，不受影响 ✅。
6. **pending 待确认态停止**：待确认时泵已因 confirmationRequired 终态退出并 unregister，`requestStop` 在无活跃泵时返回 False，前端不会收到 stopped，本函数不触发——安全（审核补述）。
7. **未 Start 的被 close call**：批次中 requiresApproval 及之后项前端本无卡（协议上未 Start 的 call 前端无感知），方案不触碰，刷新后历史渲染才出 error 卡——停止当窗与刷新后的卡片数差异属**既有协议行为**，非本方案缺陷（审核补述）。

## 8. TODO List

1. [x] **T1 确认卡片 status 存储**（审核已关闭）：`card.status` 由 buildToolCard 初始化、setCardStatus 唯一维护，running 判定 = `card.status === 'running'`。
2. [ ] **T2 chatView.js：新增 settleRunningCardsOnStop + stopped 分支调用**：按 §6.1 实现（含 maybeScrollToBottom 与文案锚点注释）。
3. [ ] **T3 文件头版本更新**：chatView.js 小版本 +1，description 追加本次改动。
4. [ ] **T4 手动验收**：起服务发 sleep 60 → 点停止 → 卡片定格"失败"且文案为取消文案、不再"执行中"；刷新页面后历史卡片呈现一致；发新消息正常。
5. [ ] **T5 commit**：按 git skill 提交（仅 chatView.js 一个文件）。

## 9. 验收红线

```text
停止工具后：running 卡片必须定格为终态（error/失败），文案=后端取消文案；
stopped 终态语义、跨窗口静默、连接关闭、输入框回空闲、usage 回写全部不回归。
```
