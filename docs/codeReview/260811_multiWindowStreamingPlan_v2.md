'''
Author: wilbur
Version: 1.0
Date: 2026-08-11
Description: multiWindowStreamingPlan v1.1 复审报告：逐项核对上一轮审核（260811_multiWindowStreamingPlan.md）问题修复情况
             （H1-H4/M1-M3/§2.3），并检查修订引入的新矛盾。结论 = 8 项中 6 项已修复、1 项部分修复（M1 残留时序窗口）、
             H4 核心闭合但弃用了非 404 提示建议；新发现问题 2 项（N1 同会话快速重进的占位态身份竞态、N2 reloadSession 改造点未列）；
             上轮 🔵 L1/L2/L3 文档级问题均未处理。
'''

# 复审报告 — multiWindowStreamingPlan v1.1

核实源码：`webApp/frontend/js/chatView.js` (v1.5)、`webApp/backend/agentManager.py` (v1.4)、`sseCodec.py` (v1.0)、
`server.py` (v1.5)、`sse.js` (v1.0)、`flamingoAgents/core/types.py`、`agent.py`。

## 一、上轮问题逐项核对

### 1. H1 attach 在途未登记 → 【已修复】

- §5.1 发起 attach **同步**登记 `phase:'attaching'` 占位流态并同步赋 `stream.abort = handle.abort`（中间无 await，
  JS 单线程内无交错窗口）→ `close()` 经既有 `stream.abort()` 即可终止在途 attach ✓。
- streamResume 处理与 `renderFullHistoryFallback` 均带 `sessionId !== currentSessionId` 守卫；未初始化回调经
  `!initialized` 拦截 ✓。E11 已记录。
- 附带收益：M4（初始化前可发送撞 409）由占位态 + `updateComposer` attaching 分支（输入/按钮禁用）闭合 ✓；
  `send()` 的 `if (!sessionId || stream) return` 与 Enter 键路径均被占位 stream 拦截 ✓。
- 残余竞态见「新发现 N1」（身份守卫缺失，不影响本项定性）。

### 2. H2 stop 不广播终态 → 【已修复】

- §4.1 stop 分支广播 `errorEvent(message='已停止。', errorType='stopped')`；已核实 `errorEvent ∈ terminalEventTypes`
  （types.py:139），终态语义成立 ✓。
- 前端 §5.3 stopped 分支 `markInterrupted() + goIdle()` 静默不弹条 ✓。
- 双向一致性核实（本轮新增核查）：
  - 发起停止的窗口处于 `stopping` 态：onStreamEvent stopping 分支对 `error` 事件记 `terminalSeen=true` 后直接返回，
    不经 handleStreamError → onStreamClosed stopping 分支 goIdle，不弹条 ✓；
  - `markInterrupted` 有 `live.interrupted` 幂等标记，stop() 已打标后 stopped 分支重复调用无副作用 ✓；
  - stopped 事件进 history 后被迟到 attach 回放 → 按终态走 §5.3 静默收尾，语义正确（等价 E8），
    **不存在「stopped 被历史回放重复下发」问题**（泵 finally 即 unregister，replay 窗口仅 getActivePump→closed 竞态，
    且回放结果行为正确）。

### 3. §5.2 守卫清单纠正 → 【已修复】

- v1.1 清单 = toolCallStart / confirmationRequired / completed / error 四处 + `if (stream.currentStep)` 守卫。
  逐一核实 chatView.js：该四处确有 `collapseThinkingIfOpen(stream.currentStep.live)` 调用 ✓；
  `toolCallEnd` 无此调用、经 `liveBodyEl()` 短路，不改正确 ✓；
  textDelta/reasoningDelta 先行 `beginNewStepIfNeeded` 天然安全 ✓。
- 附注：实现采用四处点射守卫而非审核建议的 `currentLive()` 收敛，功能等价；代价是未来新增 case 仍需各自记得加守卫
  （点射方案的固有弱点，属风格取舍，不构成问题）。

### 4. H4 attach 初始化前失败统一兜底 → 【已修复 · 一处建议被弃用】

- done/catch 双链均以 `!initialized` 统一回落 `renderFullHistoryFallback()`；fallback = 全量历史 + pending +
  `enterWaitingConfirm` + updateComposer，含 sessionId 守卫 → E3/E12 落地 ✓。
- **偏差**：上轮修复建议中「非 404 失败应 `showError` 提示但仍渲染历史」未采纳——v1.1 对一切初始化前失败
  （含 500/网络错误）一律**静默**回落。此时若流仍在跑，用户无感知地退回静态历史 + 「发送」按钮，
  重发撞 409（原始 bug 的弱化重现）。建议补一句非 404 提示（一行改动），或在 §6 显式声明该取舍。

### 5. M1 confirm 流 queued 用户消息 → 【部分修复】

- v1.1 改采**前端方案**：`initAttachedStream` 对 `userMessage=null` 的 confirm 流，从 `messages.slice(baseCount)`
  补渲染 `kind==='user'` 消息（已声明位置近似的视觉偏差）。
- **覆盖的时序**：queued 消息已落盘、且 attach 的 getMessages 晚于落盘（如 confirm 流已进入模型循环阶段才切回）✓。
- **残留窗口**：queued 消息在 confirm 流**中途**才落盘（agent.py driveConfirmation：执行完整个工具批 →
  `takeQueuedUserMessage` + `appendUserMessage` → 才进 driveModelLoop）。若用户在**工具执行阶段**切回，
  getMessages 先于落盘 → 该气泡既不在 messages 也不在事件流，两头落空直至下次全量 reload。
  工具执行可耗时数十秒，此窗口不算极端；上轮建议的后端方案（chatConfirm 采样时 peek
  `conv.queuedUserMessage` 进 meta，confirm 流启动时 queued 必在队列中）才能完全闭合。
- 最低要求：在 §6 边界表显式声明该残留窗口为已知边界。

### 6. M2 订阅者反注册 → 【已修复】

- `unsubscribe`（subLock 内 remove）+ `sseGen` finally 反注册 ✓；§4.1 内存口径同步更新「死订阅由 unsubscribe 清理」✓。
- 小缺口（🔵）：§4.2/T1.3 未写明原 stream/confirm 的订阅者 0 是否也传 `pump` 给 sseGen。不传则与现状一致
  （死队列滞留至泵结束，无害），但文档应明确，避免实现时歧义。

### 7. M3 回放 delta 压缩 → 【已修复】

- `compactDeltas(history)` 仅合并**相邻同型** textDelta/reasoningDelta（text 拼接），不跨事件类型、不跨工具事件，
  保序 ✓；仅作用于 subscribe 回放副本、不改 history 本体 ✓。
- 效果核实：前端 textDelta case 每事件全量 `renderMarkdown(textBuf)`，压缩后回放渲染次数从 O(delta 数) 降为
  O(连续段数)，O(n²) 卡顿消除 ✓；合并后单帧语义与逐 delta 到达完全等价（同 step 内 textBuf 累加）✓。

### 8. §2.3 替代方案取舍 → 【已修复】

- A/B/C 三方案对照表与上轮建议一致，明确 G2（多浏览器窗口同会话同步观看）是选 C 的决定性理由 ✓。

## 二、上轮 🔵 项遗留（本轮未处理）

| 项 | 状态 | 说明 |
|---|---|---|
| L1 status 端点 git 子进程进 open 关键路径 | 未修复 | v1.1 仍 `Promise.all` 加 `getSessionStatus`（server.py:197 同步 subprocess，超时 2s），未记录延迟代价、未拆轻量端点 |
| L2 §4.1 内存论证错误 | 未修复 | 仍写「受 maxModelSteps=32 限制」——maxModelSteps 限步数不限事件数，应为「事件数 ∝ 回合输出长度」 |
| L3 §4.1 history 注释误导 | 未修复 | 仍写「泵结束后保留供迟到 attach 回放」；实际泵注销后 attach 恒 404，closed 分支仅覆盖 getActivePump→subscribe 竞态 |

三项均为文档措辞/记录级，不阻塞实施，建议随 T3.2 一并改。

## 三、新发现问题（v1.1 修订引入）

### 🟡 N1. 占位态缺身份守卫：同会话快速重进（A→B→A）时旧 attach 回调可清掉新占位态

**位置**: §5.1 `attachStream` 的 done/catch 链与 `renderFullHistoryFallback`
**问题**: 守卫只比 sessionId。时序：A attach#1 在途 → 切 B（close abort attach#1）→ 极快切回 A
（open 启动 attach#2，新占位态入 `appStore.stream`）→ attach#1 的 `done`（'aborted'）若迟至此时才 settle，
`sessionId === currentSessionId`（都是 A）通过守卫 → `renderFullHistoryFallback` 执行
`window.appStore.stream = null` **清掉 attach#2 的占位** → attach#2 的 streamResume 到达时
`initAttachedStream` 读 `window.appStore.stream` 为 null → `stream.phase = 'streaming'` 抛 TypeError，
视图停留在 fallback 渲染（无停止按钮、后续增量丢失）。
实际触发概率低（abort 的 done 通常在首个路由宏任务后即 settle，彼时 currentSessionId=B 被守卫拦截），
但这是 H1 修复引入的残余竞态，且「快速来回切换」恰是本方案主场景。
**修复**: 占位态加身份守卫——fallback/清理前比较 `window.appStore.stream === placeholder`（占位对象引用），
不等则静默退出；一行改动，同时覆盖 done/catch 两条链。

### 🔵 N2. E5「confirmationMismatch → 自愈刷新并 reattach」依赖 reloadSession 接入 streaming 分支，但实施清单未列

**位置**: §5.1（标题含 reloadSession，伪代码只写 open）/ §8 T2.1
**问题**: `handleStreamError` 的 confirmationMismatch 分支调用 `reloadSession(sessionId)`（chatView.js），
E5 预期其「自愈刷新并 reattach」要求 reloadSession 同样走 status.streaming 分支；§5.1 标题写了
「open / reloadSession」但伪代码与 T2.1 均未列出 reloadSession 的改造点，实现时易漏。
**修复**: §5.1 明确 open 与 reloadSession 共用同一入口逻辑（Promise.all + streaming 分支），T2.1 补一句。

## 四、其余新矛盾核查（均无问题）

- attaching 占位态 × onStreamEvent：phase!=='streaming' 事件被丢弃，但 pre-init 事件由 attach 处理器缓冲、
  待 init 迁移 phase='streaming' 后才回放 ✓；preInitBuf 仅防御（服务端保证 streamResume 首帧）✓。
- attaching 占位态 × stop()/confirm()/send()：stop 要求 streaming、confirm 要求 waitingConfirm、send 要求
  stream 为空，占位态下三者全部安全短路 ✓；updateComposer attaching 分支禁用按钮 ✓。
- stopped 终态 × 历史回放重复下发：不成立（见 §一-2 核实）✓。
- subscribe 回放 × closed 竞态：subLock 内回放+登记与 _broadcast/closed 互斥，不丢不重 ✓（上轮已确认，本轮无回归）。

## 五、结论

v1.1 修复质量高：4 个 🟠 全部闭合，M2/M3/§2.3 落地。放行前建议处理：
1. **M1 残留窗口**——至少写入 §6 已知边界，或改采上轮后端 peek 方案彻底闭合；
2. **N1 身份守卫**——一行改动，建议并入 T2.1；
3. H4 的非 404 静默取舍补提示或声明；N2 与 L1/L2/L3 随文档收尾（T3.x）一并修订。
