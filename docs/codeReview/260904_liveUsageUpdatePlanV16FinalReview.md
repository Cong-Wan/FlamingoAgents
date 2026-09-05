# 方案文档最终复审报告 — `260902_liveUsageUpdatePlan.md` v1.6

- Author: wilbur
- Version: 1.0
- Date: 2026-09-04
- 复审模型：`xaiSubscription/grok-4.6`
- 复审结论：**通过，可实施**

## 总览

- Critical：0
- High：0
- Medium：0
- 阻塞实施的问题：0

v1.6 已完成此前所有审核问题的闭环：Core usage 字段来源与 step 值拷贝、Core/DTO SSE 双映射、泵中间更新与实际模型落账、费用状态机、stop/finally 原子认领、前端单调合并和单飞 refresh、脚本加载顺序、attach/切会话竞态、confirm 复用 stream 对象时的 connectionId 隔离，以及完整 `onStreamClosed` 状态机。

## v1.5 两个 Medium 的验证

### 1. confirm 复用 stream 对象的连接竞态

已修复：每次 `streamPost` 分配独立 `connectionId`；send/confirm/attach 的 event/closed/failed 均绑定 `sessionId + streamState + connectionId`。confirm 启动新连接时覆盖同一 streamState 的旧 id，因此上一泵迟到 closed 不会清除新 confirm 泵。

### 2. `onStreamClosed` 状态机完整性

已修复：方案完整保留以下五条现有路径，并在身份校验后以 fire-and-forget 方式执行权威 refresh：

1. completed/error 已 goIdle（current stream 为 null）；
2. waitingConfirm 保持待确认；
3. stopping 收尾；
4. 未见终态的连接断流；
5. 正常终态。

旧 session、旧 stream 或旧 connectionId 均不得 refresh/goIdle 新连接。

## 核心设计回归结果

| 检查项 | 结果 |
|---|---|
| Core `safePayload` 与完整 usage 门卫 | 通过 |
| 外层 model step usage 值拷贝 | 通过 |
| sseCodec 永久保留 Core/DTO 双映射 | 通过 |
| `_pump` stop/done 检查与 DTO/history 顺序 | 通过 |
| 费用 pending/ready/unavailable 状态机 | 通过 |
| 费用 I/O 位于 managerLock 外 | 通过 |
| `_recordUsage` 锁内认领、锁外 wait/I/O/set | 通过 |
| owner 早退/异常均通知 done，异常不阻断 seal | 通过 |
| status 单调合并/reset/singleflight | 通过 |
| `statusUsage.js → statusBar.js → chatView.js` | 通过 |
| usageTurns 按泵实际 provider/model 落账 | 通过 |
| JSONL snake_case 对账映射 | 通过 |
| 前后端分步回滚无 C1/ImportError | 通过 |

## 非阻塞说明

方案 §8 的预计版本以调研基线 `2655e4b` 为准，并已明确：实施时若工作区文件头已因其他任务顺延，必须按当前版本继续 +0.1。当前工作区中 `chatView.js` / `index.html` 已有其它未提交修改，实施本方案时不得覆盖，应以届时文件头和现有改动为准精准合并。

## 最终判定

**方案无明显问题，状态可更新为“已审核，可实施”。**
