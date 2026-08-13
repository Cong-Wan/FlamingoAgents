# 模型体验四项改进方案（用量图配色 / tooltip 过滤 / 顶部灯条 / 调用重试与错误展示）

Author: wilbur
Version: 1.2
Date: 2026-08-12
Description: 针对四项 UX 问题的修复方案——(1) 用量图模型撞色；(2) tooltip 列出 0 用量模型；
             (3) 模型输出时顶部穿梭灯条提醒；(4) 模型调用 502/429 重试 3 次 + 错误内联到消息下方。
             v1.1 审核修订（H1/H2/M1-M5）：重试仅限「未产出任何 chunk」的连接建立期；tooltip 同步过滤 label 回调；
             退避分片可中断、emptyMessage 特判、删除空块/TODO 冗余、retryNotice 语义统一为「进内存 history 不进 jsonl」。
             v1.2 复审修订（kimi/k3 L1/L2）：退避可中断改为「agent 分片 sleep + waiting 心跳 → 泵查 stopFlag 广播 stopped → finally close」
             机制（跨模块拿不到 stopFlag，agent 不主动查）；T4 Retry-After 挂点明确为 openRequest 的 HTTPError 分支 → modelRequestError.retryAfterSeconds。

## 0. 背景与根因（已读码确认）

| # | 问题 | 根因定位 | 关键代码 |
|---|------|----------|----------|
| 1 | 用量图两个模型同色 | `colorFor()` djb2 哈希 `% 10`，10 色调色板无去重，模型多必撞色 | `usageView.js` palette/colorFor |
| 2 | 悬浮柱子列出 0 用量模型 | index 模式下 tooltip 对该 x 所有 dataset 都触发 `label` 回调 + `afterBody` 只判 `!byModel` 不判 0 | `usageView.js` |
| 3 | 模型输出无视觉提醒 | 顶部无灯条结构；流态由 `appStore.stream.phase` 驱动 | `index.html` `.topbar` / `chatView.js` |
| 4 | 502/429 一次失败即停 + 错误在顶部弹窗 | `driveModelLoop` `except Exception` 一次失败即 `errorEvent` 终态；前端 `showError` → 顶部 `#errorBar` | `agent.py` / `chatView.js` |

## 1. 设计决策（含权衡，已选定）

### D1 模型配色：调色板扩容 + 去重分配
- 调色板由 10 → 20 色（区分度优先离散色，**不含折线色 `#1c1c1e`**）。
- `assignColors(models)`：对当前图实际 modelKey 集合，先按 djb2 哈希取偏好色，冲突线性探测下一空色，保证**同图任意两模型不同色**。
- 已知取舍：跨粒度（hour/day/month）模型集合不同，同一模型在不同粒度下可能换色——可接受，不切粒度时稳定。

### D2 tooltip 过滤：label 回调 + afterBody 双重跳过（审核 H2 修正）
- 问题不止 `afterBody`：`interaction.mode:'index'` 下 tooltip 对该 x 索引**所有 dataset** 各调一次 `label`，0 值模型照样输出「model：0 tokens」行。
- 双管齐下：`label` 回调对 `item.parsed.y === 0` 返回 `null`（Chart.js 跳过该项）；`afterBody` 内 `tokensOf(byModel) === 0` 跳过。
- 总量折线（label='总量'）恒显示。
- 全 0 极端桶回退「该桶无明细」。

### D3 顶部穿梭灯条：纯 CSS 动画 + stream.phase 驱动
- `index.html` **聊天页** `#chatPage .topbar` 内注入 `<div id="streamIndicator">`（锚定 chat 顶栏，避免命中 settings/usage 的同名 `.topbar`）。
- 3px 渐变块，CSS keyframes `translateX` 左右往返，纯 CSS 不占主线程。
- 显隐：`syncStreamIndicator()` 在 `updateComposer()` 末尾按 `phase==='streaming'||'attaching'` 判定。`updateComposer` 是所有流态切换必经点；`close()` 置 `stream=null` 不调 updateComposer，但切页后 chatPage 隐藏、回来 `open()` 重算，无害。

### D4 模型调用重试：仅「未产出 chunk」的连接建立期（审核 H1 修正）
- **核心红线**：重试只允许发生在 `completeStream` **尚未 yield 任何 textChunk/reasoningChunk/finalChunk 之前**（连接建立期 429/5xx）。一旦该 step 已产出任意 chunk 后失败（中途断流），**不重试**直接终态——否则重跑会把已上屏正文再追加一遍，造成重复错乱。
- 实现：在 `driveModelLoop` 包一层重试循环；每次 `completeStream` 用局部标志 `chunkSeen` 记录本轮是否已产 chunk。`chunkSeen=False` 且可重试 → 退避后重跑；`chunkSeen=True` → 直接 errorEvent。
- 可重试错误：`modelRequestError.statusCode in (429,500,502,503,504)` 或 `statusCode is None`（网络层连接失败）；但 `statusCode is None` 仅当 `chunkSeen=False`（连接根本没建立）才重试。
- 不可重试：4xx 业务错（400/401/403/404）直接终态。
- 次数：最多重试 3 次（最多 4 次尝试）。退避 `2^attempt` 秒（1/2/4s）封顶 8s；429 读 `Retry-After`（秒数或 HTTP-date 均解析）。
- **退避可中断（审核 M2/L1）**：停止信号在 `agentManager.streamPump.stopFlag`，重试循环在 `agent.py`，跨模块拿不到。agent 侧**不主动查停止标志**，而是把退避做成 ≤100ms 分片 sleep，每片尾 yield 一个 `retryNotice(status='waiting')` 心跳；泵 `_pump` 循环每事件先查 stopFlag，一旦停止即广播 `stopped` 终态并 break，agent 生成器在 finally 被 close，sleep 随 `GeneratorExit` 终止，**不再发真实重试请求**。stopped 终态只能由泵广播（其他订阅窗口不误报中断）。
- 每次失败 yield `retryNoticeEvent(message, attempt, retryAfterMs, status)`；终态仍败 yield `errorEvent`（含「已重试 3 次」）。waiting 心跳与前端「重试中」倒计时刷新共用同一事件流（审核 L3）。

### D5 错误内联到消息下方（审核 M3/M4 修正）
- 终态模型错误 → `handleStreamError` 在当前 `stream.currentStep.live.bodyEl` 下方 append `.msg-error-block`（红块 + ✕ 关闭）。
- **特判保留顶部 errorBar**：
  - REST 预检失败（401/409/会话不存在/无会话）：走 `onStreamFailed` → `showError`，本就不受影响。
  - `emptyMessage`：是 SSE error 事件但 `send()` 已预建空 step，会挂空块——显式特判走顶部 errorBar（纯附件发送时 text 为空会命中此路径）。
  - `confirmationMismatch`：刷新提示，顶部 errorBar。
  - **无 currentStep**：不新建空块（审核 M4 删冗余），直接回退顶部 errorBar。
- 错误块不落盘：刷新后消失（错误是本轮瞬态，非历史事实）。

### D6 新增事件 retryNotice（契约小幅扩展，审核 M1 语义统一）
- 库 `types.py` 加 `retryNoticeEvent(message, attempt, retryAfterMs)`，非终态、不进 `terminalEventTypes`。
- **语义统一：进 pump 内存 history（供 attach 回放），不进 jsonl 落盘**。`sseCodec.eventToFrame` 映射 `retryNotice`；`compactDeltas` 只合并 textDelta/reasoningDelta，其余事件天然原样透传，**无需改动**（删除审核 M5 指出的冗余 TODO）。
- 前端 case：消息下方「重试中」提示块（每次事件更新文案/倒计时）；attach 回放到的重试块随后续 textDelta/completed/error 清除；切页/终态清理倒计时定时器（审核 L7）。

## 2. 影响面与兼容
- **契约**：事件集 7 → 8（新增非终态 retryNotice），向后兼容（旧前端忽略未知事件）；webApiSpec 与 `docs/streamOutputPlan.md §4.3`、`sseCodec.py` 头注释同步注记。
- **落盘**：retryNotice 与错误块均不进 jsonl，历史语义不变；retryNotice 仅驻 pump 内存 history 供 attach 回放。
- **配置**：重试常量（次数/退避/可重试状态码集）放 `agent.py` 模块级常量（审核 L2 修正，重试循环在 agent 层，不放 adapter）；本期不进 models.yaml，避免过度设计。
- **多窗口**：retryNotice 进内存 history，attach 窗口回放可见。

## 3. 风险与缓解
| 风险 | 缓解 |
|------|------|
| 中途断流重试致正文重复（H1） | 仅 `chunkSeen=False` 才重试；产过 chunk 直接终态 |
| 退避期停止滞后（M2） | 分片可中断 sleep，命中停止立即终态，不发真实请求 |
| 退避期前端「看似卡住」 | 灯条常亮 + retryNotice 提示块明示「第N次重试，x秒后」 |
| emptyMessage 挂空块（M3） | handleStreamError 特判走顶部 errorBar |
| 0 用量模型仍入 tooltip（H2） | label 回调 + afterBody 双重过滤 |
| 灯条残留（waitingConfirm/stopping/close） | syncStreamIndicator 统一按 phase 判定；close 例外无害 |

## 4. TODO（实施顺序）——全部完成（2026-08-12，sub2api/grok-4.5 派发执行 + 主代理逐批验收）
- [x] T1 usageView.js：20 色调色板 + assignColors 去重 + 折线色排除 → 验证：多模型图任意两色不同
- [x] T2 usageView.js：label 回调对 0 值返回 null + afterBody 跳过 tokens=0 + 全 0 兜底 → 验证：悬浮只见有效模型
- [x] T3 types.py：retryNoticeEvent dataclass（非终态）
- [x] T4 chatCompletions.py：openRequest 的 `except HTTPError` 分支解析 `error.headers.get('Retry-After')`（秒数/HTTP-date 均解析），挂到 `modelRequestError.retryAfterSeconds` 字段（审核 L2 修正挂点）
- [x] T5 agent.py：重试常量 + driveModelLoop 重试循环（仅 chunkSeen=False 重试、分片可中断退避、失败 yield retryNotice、终态带次数）
- [x] T6 sseCodec.py：retryNotice → SSE 帧映射
- [x] T7 index.html/styles.css：#chatPage .topbar 注入 #streamIndicator + 穿梭 keyframes
- [x] T8 chatView.js：syncStreamIndicator() + updateComposer 末尾调用
- [x] T9 chatView.js：onStreamEvent 新增 retryNotice case → 「重试中」提示块渲染/更新/倒计时/清除（waiting 心跳即倒计时刷新源）
- [x] T10 chatView.js：handleStreamError 终态错误 → .msg-error-block 内联；emptyMessage/无 currentStep 特判走顶部 errorBar
- [x] T11 文件头版本号与 description 更新（usageView/agent/chatCompletions/chatView/types/sseCodec/index/styles）
- [x] T12 webApiSpec.md + streamOutputPlan §6.2 + sseCodec 头注释补 retryNotice 注记

## 5. 验收清单
1. 用量图 ≥3 模型时任意两柱不同色。
2. 悬浮任一柱子，tooltip 只列该桶 tokens>0 模型（label 加粗行与明细均不含 0 值模型）。
3. 发送消息后顶部灯条左右穿梭；completed/error/stop/确认弹窗时熄灭。
4. 触发连接建立期 502/429：消息下方「第N次重试，x秒后」，3 次后内联红错误块，顶部不弹模型错误窗。
5. 中途断流（已产内容）：不重试，直接内联错误块，已上屏正文不重复。
6. 重试退避期点停止：≤100ms 内响应，不再发真实重试请求。
7. 空消息（纯附件 text 为空）：错误走顶部 errorBar 而非挂空块。
8. 刷新会话后历史无错误块/重试提示（瞬态不落盘）；attach 重连可见在途重试提示。
