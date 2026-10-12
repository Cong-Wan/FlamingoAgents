# 思考块独立耗时与 TTFT 实时化方案

- Author: wilbur
- Version: 1.2
- Date: 2026-10-10
- Description: 用户反馈：①块脚标 `Elapsed 3.1s · Thinking 1.4s · Output 0.9s` 不符合预期——一轮思考可能多次，应在【每个思考框下面】写各自耗时，汇总脚标只写 Elapsed；②状态栏 TTFT 等整轮结束（usageUpdate 帧流末才到）才更新，应在收到第一个输出时立即显示。
- 状态：**已实施**（2026-10-10）：初审 3M+3S + 复审 3 处文档级修订全部吸收；红测试 2 个先复现；全量 358 passed，exit 0。纯静态前端，刷新即生效。

## 1. 现状与根因

| 现象 | 根因 |
|---|---|
| 思考耗时只在块脚标汇总显示 | v1.29 把 Thinking 拼进块脚标组合（formatStepElapsedLine），未落到每个 thinking-block 下 |
| TTFT 轮末才更新 | TTFT 数据源是 usageUpdate 帧 timings（模型调用完成才发），流中无中间来源 |
| 一轮多次思考 | D6 隐式 step 边界：工具结束后新输出落新块——每个 step 有独立 thinking 块，正对应"每个思考框" |

数据可用性：
- 流式期间：前端本地可计时（首个 reasoningDelta → 最后 reasoningDelta），后端 timings 流末才到；
- 历史回放：jsonl timings 已含 reasoningStartMs/reasoningEndMs（thinkingPhaseMetricsPlan 已落盘），可按消息计算。

## 2. 目标

1. **每个思考框下方**显示该次思考耗时：`Thinking 1.4s`（**可见性：挂 thinking details 之后、同为 bodyEl 子节点的兄弟行**，折叠态仍可见——初审核 M1：放 details 内则终态折叠时不可见，违背用户诉求）
   - 流式：本地 Date.now() 计时（首个→最后一个非空 reasoningDelta），思考结束时写入，每块只写一次（thinkingPainted）；
   - **写入收口：收敛进 flushAndCollapseThinking 单一入口**（初审核 M2：textDelta 折叠/toolCallStart/三终态天然全覆盖，"思考→工具→新 step"中间块不丢），goIdle 与本窗口 stop 单独兜底；
   - 历史回放：**先 readTimings(msg.timings) 再传 thinkingMs**（初审核 M3：raw 传 undefined 会绕过校验），thinkingMs 需在 statusUsage 导出；
   - enterWaitingConfirm 手工 step 与 buildLiveFromHistory 复用壳无新字段 → paint 空守卫；
   - 无思考/无数据不显示；旧 jsonl（无打点）回退不显示。
2. **块脚标只写 `Elapsed 53s`**（保持一次性归属/纯工具轮不覆盖的现有语义；**title 改为 Elapsed-only 口径**：本次模型调用总时长，含思考与等待，不含工具执行）。删除 v1.4 引入的 formatStepElapsedLine（无引用即删，含测试断言与 chatView 两处 title 文案）。
3. **TTFT 实时**：发送/confirm 开始记基准时刻；收到首个非空 delta（text/reasoning）立即上报状态栏显示 `⏱ TTFT 1.2s`（其余段 —，流末权威帧接管完整行）；retryNotice 视为新 attempt 重置基准；终态/停止清除 live 态（无权威数据则整行隐藏）。
4. 状态栏其余段（Thinking/Output/TPS）仍由 usageUpdate 权威帧更新（流末），不在流中估算。

## 3. 非目标

- 不改后端（timings 打点/透传已就绪，历史回放数据已够）；
- 不做流中 Thinking/Output/TPS 实时（无权威 token 数与 duration，估算无意义）;
- 不改速度行两形态口径（formatSpeedLine 不动）；
- attach 回放流不启用 live TTFT（请求发出时刻在 attach 之前不可知，仅权威帧更新）。

## 4. 改动面（纯前端）

| 文件 | 改动 | 版本 |
|------|------|------|
| `chatView.js` | ①step 增 reasoningStartedAt/reasoningEndedAt/thinkingPainted；thinkingElapsedEl 为 thinking details **之后的 bodyEl 子级兄弟节点**（折叠态可见，默认 hidden）；②reasoningDelta 非空更新首末；③paintThinkingElapsed **收口于 flushAndCollapseThinking 内部**（覆盖 textDelta 折叠/toolCallStart/completed/error/confirmationRequired），**stop 与 goIdle 各自兜底**（goIdle 同时接 clearLiveTtfb 与 paint 兜底）；④历史回放 thinking 块下 readTimings→thinkingMs 写 `Thinking xs`；⑤块脚标 applyStepTimings/历史回放改回 formatElapsed only（title 同步 Elapsed-only）；⑥send/startConfirmStream 记 requestStartedAt+liveTtfbSent=false；retryNotice 重置；首非空 delta 上报 statusBar.applyLiveTtfb | 1.29→1.30 |
| `statusBar.js` | snapshot 增 liveTtfbMs/liveTtfbActive；applyLiveTtfb(ms) 设值重绘；clearLiveTtfb() 清 active；renderSpeed：active 时显示 `⏱ TTFT x.xs`（绕过 formatSpeedLine null 隐藏分支）；applyIncomingUsage **仅在接受帧且带有效 timings 时**清 active（审核 S：拒帧即清会出现真空窗） | 1.9→1.10 |
| `statusUsage.js` | 删除 formatStepElapsedLine 及导出（thinkingMs/readTimings 保留供回放与测试） | 1.4→1.5 |
| `styles.css` | `.thinking-elapsed`（思考块下方右对齐小字：bodyEl 子级、details 之后兄弟节点，折叠态可见） | 1.30→1.31 |
| `index.html` | cache-bust 提版（chatView 1.30/statusBar 1.10/statusUsage 1.5/styles 1.31） | 1.30→1.31 |
| `tests/testLatencyMetricsFrontend.py` | 红→绿：思考块耗时（流式/多次思考/终态纯思考/历史回放/旧数据回退）；块脚标仅 Elapsed；live TTFT 即时+权威接管+重试重置+终态清除；删除 formatStepElapsedLine 断言 | 2.1 |

## 5. 关键设计决策

- **D1 思考块耗时用前端本地计时（Date.now()）**：与后端 reasoningStart/EndMs 同语义（首个到最后一个思考增量），误差毫秒级；不等流末权威帧（思考块在转正文时折叠，权威帧尚未到达，届时再写反而滞后）。
- **D2 每块只写一次（thinkingPainted）**：对齐块脚标一次性归属；跨重试的思考同属一个 attempt 序列，retry 后新 delta 继续更新 endedAt（思考通常在 retry 成功后重开，块折叠时机才落笔，可接受）。
- **D3 live TTFT 独立于 lastTimings**：不污染权威快照字段（liveTtfbActive 单独开关）；权威帧到达即接管，避免两套数据打架。
- **D4 终态清 live**：goIdle 统一清（completed/error/stopped/attach 空闲都经此），防残留。
- **D5 纯工具轮的思考块**：模型只思考不发正文（少见但存在，工具调用前思考）→ 终态时 paintThinkingElapsed 兜底写入；块脚标不受影响（沿用纯工具轮不覆盖门槛）。
- **D6（审核补充）**：paint 收口 flushAndCollapseThinking（单点覆盖 textDelta/toolCallStart/completed/error/confirmationRequired）；stop 与 goIdle 各自兜底；中间 step 块（思考→工具→新 step）不丢耗时。
- **D7（审核补充）**：live TTFT 清除条件=「帧被接受 且 带有效 timings」，防拒帧真空窗；title 文案随 Elapsed-only 同步。

## 6. TODO

- [x] P0 本方案落档 + 子代理审核（初审 3M+3S + 复审 3 处文档级修订，均已吸收）。
- [x] P1 红测试（2 个新用例先失败复现）：testThinkingBlockElapsedAndLiveTtfb（流式/多次思考/live TTFT）+ testThinkingElapsedHistoryReplay（回放/旧数据回退）。
- [x] P2 实施 §4；前端 4 文件测试全绿；node --check 三文件 PASS。
- [x] P3 全量 `uv run pytest -q` → **358 passed，exit 0**。
- [ ] P4 用户刷新验证（纯静态生效）。

## 7. 审核记录

待审。
