# 方案最终复审报告 — 状态栏逐模型用量：执行计划 v1.1

- Author: wilbur
- Version: 1.0
- Date: 2026-09-07
- 审核对象：`docs/plan/260907_liveUsageExecutionPlan.md` v1.1；对照 `docs/plan/260902_liveUsageUpdatePlan.md` v1.6、`docs/codeReview/260907_liveUsageExecutionPlanReview.md` v1.0、基线 `90d7417` 关键代码
- 审核范围：首审 3 High / 2 Medium 是否已写死；E1–E4 有无明显新洞；不改业务代码、不扩范围、不重开已吸收约束
- 复审结论：**通过。可以按 v1.6 + 执行计划 v1.1 联合开工。零阻塞。**

## 总览

- 审核文件：执行计划 v1.1、首审报告、原方案 v1.6；对照 `chatView.js` / `statusBar.js` / `index.html` / `sse.js` / `agentManager.py` / `tests/testToolCardCollapse.py`
- 发现问题：🔴 0 / 🟠 0 / 🟡 0 / 🔵 0
- 阻塞开工：无
- 整体评价：v1.1 已把首审 H1–H3、M1–M2 收成可执行接点，没有用“加几个 if”的含糊表述替代提交语义。现网 `stop()` 先 POST 再同步 abort、`done` abort 后 resolve、`onStreamClosed` 立即 `refresh()`、`statusBar` 只比 sessionId、`statusBar.js` 无 cache-bust，四处仍与首审坐实的竞态一致。无需再改计划。

## 首审收敛核对

| 首审 | 要点 | v1.1 | 结论 |
|---|---|---|---|
| High-1 | 无 snapshot 不得因 revision 前进而丢 GET；先建快照再按 revision/pending 合并 | E1 写死：先建含 location/model/window 的快照，再按时序合并；旧 pending 不重放、新 pending 要合并 | 已收敛 |
| High-2 | 按 `closedStream` 合并；`stopRequest` 先于 abort；拒绝消化；延迟刷新允许 `currentStream=null` | E2 写死挂接点、`closedStream.stopRequest`、等待链 catch、null 或本对象、五条 closed 路径 | 已收敛 |
| High-3 | `statusUsage.js?v=1.0`、`statusBar.js?v=1.4`、`chatView.js?v=1.20` | E4 首轮版本键写死；保留 `styles.css?v=1.19`；不得改弱折叠断言 | 已收敛 |
| Medium-1 | `goIdle` 不清 latestBound；仅 open/close/showEmpty 清除 | E3 写死；自然 completed→null→closed 仍校准 | 已收敛 |
| Medium-2 | 权威 GET 使所有更早普通/权威 GET 失效 | E1：每个实际新 GET 递增 requestId；旧响应/旧 finally 不得提交或清理新请求 | 已收敛 |

首审 Low（`#statusUsage` 与 `window.statusUsage` 同名）维持原结论：不改名、不扩范围，statusBar 只走 `getElementById`。不升格、不重开。

## E1–E4 复审

### E1 usageRevision 与 pending

- 用量钟与 HTTP 钟分离：`usageRevision` 在接受有效 usageUpdate/pending 时递增；`requestId` 只标记实际新 GET。不得用 connection 代际替代用量修订号，新流尚无 usage 时前泵权威降价仍合法。
- 提交语义：发出后有新用量 → usage/context/cost 单调合并、不能完整回退；无新用量 → 允许权威降 cost。禁止全程 `Math.max` 堵死终态校准。
- pending 时间语义：`pendingRevision <= requestRevision`（即该次 GET 发出时捕获的 `usageRevision`，**不是** `requestId`）的旧 pending，在无新用量的权威校准后不得把 liveCost 抬回；`pendingRevision > requestRevision` 仍合并。
- 无 snapshot 先建快照再合并，与 High-1 四条 deferred 验收一致。reset 清 usageRevision/pendingRevision，generation 隔离旧回调。

与原 §7.5.3 冲突处已声明以执行计划为准。无新逻辑洞。

### E2 stopRequest 拒绝消化 / 延迟刷新

现网时序未变：`stop()` 先 `stopChat` 再 `abort()`；`sse.js` abort 后 `done` 成 `'aborted'` 并进入 `onStreamClosed`；后端 `requestStop()` 才 `_recordUsage()`。本地 abort ≠ 落账。

v1.1 接点足够且可落在现状态机上：

- `stream.stopRequest = api.stopChat(sessionId).catch(...返回 null...)` 必须在 `abort()` 前挂到**捕获的** stream 对象。
- closed 看 `closedStream.stopRequest`，不看迟到 `currentStream.phase`。
- 等待链必须消化拒绝（挂接 catch + `Promise.resolve(...).finally(...)`），失败仍一次 best-effort GET，不宣称持久化成功。
- 延迟刷新再验 latestBound/session/connection，且 `currentStream` 为 null 或本对象；UI 已 `goIdle` 时不得因 null 丢掉校准。
- 自然 closed 仍 fire-and-forget；禁止 await refresh 再 `goIdle`；不新增轮询。
- waitingConfirm / stopping / 断流 / 自然终态 / null 五条路径不省略。

后端 requestStop/finally 仍走原文 §7.3.2 原子认领。无新洞。

### E3 latestBound 与 null 自然收口

null 特例仍必要（completed/error/stopped 先 `goIdle`）。v1.1 收口正确：

- `bindConnection` 写入最近身份；send/confirm/attach 都走它；confirm 复用对象仍分配新 id。
- `goIdle()` 不清；仅 open/close/showEmpty 使视图生命周期失效。
- closed 的 null 特例额外要求该连接仍是当前视图最近连接。
- event/failed 继续三重身份（session + stream 对象 + connectionId），不用 latestBound。
- attach 未初始化 reset/streamResume/preInitBuf/404、send 409 meta 保留。

A→B→A 空闲、同 session 两流结束后的旧 closed、confirm 前连接 closed/event/failed 都不会碰新视图；当前连接自然 completed 的 closed 仍最终刷新。不需重构状态机。

### E4 版本 URL

现网：`statusBar.js` 无 query；`chatView.js?v=1.19`；`styles.css?v=1.19`；折叠测试锁死后两处。本任务必改 statusBar 并新增 helper，只 bump chatView 会留下旧 statusBar。

v1.1 已取消“必要时”：`statusUsage.js?v=1.0` → `statusBar.js?v=1.4` → `chatView.js?v=1.20`；折叠测试只改 chatView 键；后续热修同步文件头与资源键。与 C5 一致。

## 问题清单

无。Critical / High / Medium / Low 均为 0。

## 基线与范围（无回归）

| 项 | 现状 | 实施口径 |
|---|---|---|
| commit | `90d7417` | 工作区仅计划/审核文档 |
| 文件头 | chatView 1.19、statusBar 1.3、index 1.13、webApiSpec 1.18、agent 1.21、types 1.8、agentManager 1.9、sseCodec 1.4 | 当前头 +0.1，禁止按原文 §8 过时数字改 |
| 范围 | 原文 §8 十个文件 | 另允许 `tests/testToolCardCollapse.py`；不改 adapters/schema/usageStore API/`/model` |
| 不变量 | JSONL snake_case、每泵一条账单、流中不写 DB、Core/DTO 双 codec、CLI 忽略新事件 | 与 E1–E4 无冲突 |

## 最终判定

**可以开工。零阻塞。不必再改执行计划。**

派实施子代理时指定：

1. 主方案 v1.6，执行补充 v1.1；冲突处以 v1.1 为准。首审伪代码是实施必遵，不是新需求。
2. 工具：read/write/edit/bash；禁止嵌套子代理；不自动 git commit；不碰真实用户会话/费用库。
3. `requestRevision` = 该次 GET 发出时的 `usageRevision` 快照，与 `requestId` 不是同一计数器。
4. 异步关键路径必须用 deferred Promise 行为测试，不能只静态匹配字符串。
5. 全量 `uv run pytest -q` 不得放宽既有断言。
