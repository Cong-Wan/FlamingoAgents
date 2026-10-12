# 耗时展示收敛与归属修复方案

- Author: wilbur
- Version: 1.1
- Date: 2026-10-10
- Description: 按用户反馈恢复工具耗时折叠展示、统一 Elapsed 文案、去除纯工具消息的孤立模型耗时，澄清 TTFT/Output/Total/TPS；补失败复现与回归。
- 优先级：本文覆盖 `261010_latencyMetricsDisplayPlan.md` 中与本次反馈冲突的展示约定。当前工作区含上一轮未提交实现，不回退其它改动。
- 审核：company71/glm-5.3 初审（见 §5）提出 4 项 M/S，v1.1 已全部吸收；复审通过（附 2 项实施提示）。
- 状态：**已实施**（2026-10-10）：P1 红测试 4 个先复现缺陷；P2 修复后全绿；全量 352 passed，exit 0。纯静态前端生效（无后端改动），待用户刷新确认视觉。

## 1. 用户要求与边界

1. 工具卡恢复紧凑折叠态；耗时回到详情底部，展开才显示，不在卡片 root 增加常驻行。
2. 消除与工具执行耗时混淆的孤立标签：仅含 toolCalls、无正文/思考的 assistant 不显示模型耗时。
3. 正确英文是 **Elapsed**（elapsed time），不是 eclapse；所有用户可见的「耗时 xxs」改为 `Elapsed xxs`。
4. 状态栏口径（裁决，互斥两形态，删除含混的「生成」）：
   - TTFT 有效：`TTFT 1.2s · Output 7.2s · TPS 55.8 tok/s`
   - TTFT 缺失/非法：`TTFT — · Total 8.4s · TPS —`
   - **TTFT** = 适配器发起请求到首个文本/思考增量的等待（本地 time.monotonic 打点，非供应商字段）；**Output** = durationMs − ttfbMs（首增量之后至适配器收尾，含传输/解析，不是纯 GPU 生成）；**Total** = durationMs 全程；**TPS** = 本次 completionTokens ÷ Output 秒数，近似吞吐。TTFT 无效时禁止拿 Total 冒充 Output。
5. 每次模型调用的耗时只归属一次：文字块写入后锁定，后续纯工具轮 usageUpdate 绝不覆盖（用户反馈的核心现象）。

产品假设：保留有正文或非空思考内容的 assistant 模型耗时；隐藏纯 toolCalls 消息的模型脚标。工具执行耗时仍逐卡独立，后端数据不删。
非目标：不重写 Core/SSE，不新增 callId 协议，不改账本/历史日志，不修全量统计页。测试不发真实模型请求，截图原文件保留不修改。

## 2. 已确认的问题（实际代码）

- `chatView.js/buildToolCard`：elapsedEl 挂在卡片 root（detail 之后），折叠态常驻一行 → 回 detail 末尾。
- `applyStepTimings`：`!footnotePainted && !stepDirty` 只挡首次，paint 后继续被纯工具轮覆盖（用户看到的「bash 耗时跑到文字那里」根因）。
- `appendAssistantHistory`：只要有 timings 就插脚标，纯工具消息也显示孤立数字。
- `textDelta/reasoningDelta`：空字符串也置 dirty；无 timings 的 usageUpdate 未消费 dirty；toolCallStart/confirmationRequired 边界不清 dirty。
- `statusUsage.js/formatSpeedLine`：ttfb 缺失/非法回退 durationMs 冒充「生成」；ttfb===duration 走 else 分支同样是回退全长。
- `statusBar.js`：`storePending` 丢弃 timings；`applyIncomingUsage` 新帧无 timings 时保留旧 lastTimings 却更新 stepUsage → A 时长配 B tokens 的错配。

事实纠正：usageUpdate 在每次模型调用完成且 usage 合法时发出（不是整轮末尾才一条）；timings.ttfbMs 是适配器本地计时首增量；工具参数增量不在打点范围；durationMs 截止适配器收尾，不含 Core 工具执行。

## 3. 最小修改

### A. 工具卡与模型输出（chatView.js 1.27→1.28）

- elapsedEl 挂回 `detail` 末尾（resultSection 之后）；`.tool-elapsed` 样式适配 detail 内部（去掉横向 padding，避免与 `.tool-card-detail` 双重内边距），styles.css 1.28→1.29。
- 三条渲染路径统一 `Elapsed ` 文案：工具 title「本工具执行时间，不含模型等待/人工确认」；模型 title「本次模型调用总时长，含首输出等待，不含工具执行」。
- step 状态改为 `stepDirty` + `timingPainted`（删除 footnotePainted，含 enterWaitingConfirm 手工 step 同步）：
  - 非空白 text/reasoning 增量置 `stepDirty=true`；
  - 每条 usageUpdate 都消费 dirty（置 false）；
  - 仅 `timingPainted=false && stepDirty=true` 时写入 timing，写入后 `timingPainted=true` 永久锁定本 step；
  - toolCallStart / confirmationRequired 边界清 dirty（模型阶段结束，防泄露给下一纯工具调用）。
- 历史回放按 `(content||'').trim() || (reasoning||'').trim()` 决定是否插模型脚标；工具卡 elapsed 照旧从 `result.details.elapsedMs` 渲染。

### B. 速度文案与同调用数据配对（statusUsage.js 1.2→1.3 / statusBar.js 1.7→1.8）

- formatSpeedLine 按两形态输出（§1.4）；duration 无效 → 整行 null 隐藏；`0<=ttfbMs<=durationMs` 才算 Output/TPS；分母 <100ms 或无正 completionTokens → TPS `—`；TTFT 可为 0。
- statusBar 成对替换规则（写死对称）：被接受的每条 usageUpdate 同时替换 `lastTimings` 与 `stepUsage` 这组度量——**帧缺 timings 或缺 stepUsage 时两者一并置 null**，禁止旧 duration 配新 tokens；`storePending` 保留这组字段；刷新后（status 仅 lastTimings）TPS 显示 `—`，不扩后端 schema。
- renderSpeed 挂 title（最近一次模型调用；TTFT/Output/Total/TPS 定义，见 §1.4）。

### C. 文件与版本

- `chatView.js` 1.28、`statusUsage.js` 1.3、`statusBar.js` 1.8、`index.html` 1.29（三项脚本 + styles query 提版）、`styles.css` 1.29。
- `tests/testLatencyMetricsFrontend.py` 2.0：改为完整 vm 加载 chatView.js + statusUsage.js 的事件流/历史回放测试（复用 testLiveUsageFrontend 的 Node DOM harness 模式），删除 extractFunction+eval 截取与恒真断言。
- `tests/testToolCardCollapse.py`、`tests/testLiveUsageFrontend.py` 仅同步本功能 cache-bust 与文件头小版本。
- 原计划/契约文档只同步展示与口径约定，纠正「已全部手测」的表述。

## 4. TODO 与验证标准

### P0 落档与审核
- [x] 对照源码定位问题并落档初版。
- [x] 子代理初审（company71/glm-5.3），4 项 M/S 全部吸收。
- [x] 复审（company71/glm-5.3）：**通过**，附 2 项实施提示：① toolCallStart/confirmationRequired 清 dirty 需 currentStep 空守卫（attach 首事件可能无 step）；② storePending 的「缺 timings 双清」与 pendingDominance 丢帧逻辑需实施时验证旁路（缺 timings 帧不能因非支配被整帧丢弃）。另确认：buildLiveFromHistory 无 footnoteEl 时 applyStepTimings 判空返回，pending 恢复不误画；retry 轮 usageUpdate 会消费 dirty，复燃依赖后续新 delta（一般成立）。

### P1 先复现（红，新测试文件）— 全部完成，4 红先复现
- [x] 事件流：textDelta → usageUpdate(timings) → 脚标写入；连续两轮 toolCallStart/End + usageUpdate（纯工具轮 timings）→ 旧脚标不变、两张卡各显示自身 elapsedMs；随后新 step 正文 → 新块脚标正常。（testChatViewToolElapsedAndTimingOwnership）
- [x] 边界：空白 delta 不置 dirty；无 timings 的 usageUpdate 仍消费 dirty；toolCallStart 前未消费的 dirty 被边界清除；retryNotice 后新正文可重燃 dirty 并写入新块。（testChatViewDirtyBoundariesAndHistory）
- [x] 折叠：默认折叠 elapsedEl 随 detail 隐藏；header click 展开/再折叠正确派发；elapsedEl 祖先为 detail，不在卡片 root。（同上）
- [x] 历史/attach 回放：纯工具 assistant 无模型脚标；正文/思考消息保留；工具 elapsed 在各自折叠详情；孤儿 toolCallEnd 兜底卡同样显示 elapsed。（同上）
- [x] pending/确认恢复（enterWaitingConfirm 手工 step）不误画脚标。（复审确认 buildLiveFromHistory 无 footnoteEl 判空路径）
- [x] 速度格式：数值/单位/TTFT=0/TTFT>duration/TTFT===duration/分母<100ms/无 tokens/duration 无效整行隐藏，两形态互斥，核对 title。（testFormatSpeedLineTwoForms + testStatusBarPairingAndCacheBust）
- [x] statusBar 配对：无快照 pending 的速度帧不丢；timings=null 帧同时清空 lastTimings+stepUsage；不与上一帧混算。（testStatusBarPairingAndCacheBust）

### P2 最小修正（绿）— 完成
- [x] 完成 A/B/C；清除本功能引入的孤立字段与错误测试断言（v2.0 测试文件重写，删除 extractFunction+eval 与恒真断言）。
- [x] `uv run pytest tests/testLatencyMetricsFrontend.py tests/testToolCardCollapse.py tests/testLiveUsageFrontend.py -q` → 15 passed。
- [x] `node --check` 三个变更 JS → PASS。

### P3 回归与收尾
- [x] `uv run pytest -q` → **352 passed，exit 0**。
- [x] 同步计划/版本头（chatView 1.28 / statusUsage 1.3 / statusBar 1.8 / index 1.29 / styles 1.29；本文状态已实施；261010_latencyMetricsDisplayPlan 的「耗时 xxs」文案与「生成」口径由本文覆盖）。
- [x] 向用户说明 Elapsed / 模型耗时归属 / TTFT / Output / Total / TPS；纯静态前端生效只需刷新。
- [ ] 用户刷新后视觉确认（自动测试不替代）。

## 5. 审核记录

### 初审（company71/glm-5.3，2026-10-10，只读）
- 结论：需修订后可实施；定位准确。
- M1 速度口径矛盾（指令 vs 方案）→ 本版裁决为 TTFT/Output/TPS 与 TTFT—/Total/TPS— 两形态互斥（§1.4/§B）。
- M2 statusBar 成对更新缺对称清空规则 → §B 写死「缺一即双清」。
- M3 测试需重写并补 4 个遗漏点（retryNotice 复燃、confirm 恢复 step、孤儿 End、折叠 click 派发）→ §P1 全部纳入。
- S1 CSS 双重内边距 → §A 微调样式。
- S2 enterWaitingConfirm 残留 footnotePainted → §A 统一为 timingPainted。
- 其余：buildToolCard 归位/历史 guard/事件顺序（usageUpdate 先于 toolCallStart）均确认可行。

### 复审
待复审（实施前）。
