# 已确认耗时 Demo 的生产落地方案

- Author: wilbur
- Version: 1.5
- Date: 2026-10-12
- Description: v1.3 定稿（第四轮确认可实施）后完成实施：P1 红测试 14 个→全绿；P2 最小实施全量落地；全量 373 passed。v1.4 更新 TODO 勾选与实测结果。
- 状态：已实施并通过最终审查复核（A1/A2 修复后全量 374 passed），等待用户验收。

## 1. 用户目标与边界

- composer 状态栏用量行末尾只追加 `TTFT … · TPS … tok/s`，删除独立速度行（#statusSpeed）与 Thinking/Output。
- 每个 thinking 块下只放其自己的 `Thinking …`；删除 assistant 模型级 `Elapsed` 脚标；历史与流式顺序一致（thinking 块 → 紧邻 Thinking 行 → 正文）。
- 工具卡内保留实际执行时间（文案 `Tool execution …`）；卡片外紧邻下方放 `Tool call …`（LLM 开始产出该工具参数 → 工具执行完成）。
- 每个完整用户回答末尾放一次 `Total …`（发消息 → 最终 completed），包含所有模型步、工具、重试和人工确认等待；停止/失败/待确认不冒充完成。
- TPS 有效值不能因普通重绘、权威 GET、切回会话、服务端重启或无 timings 的 usage 帧而丢失。
- 不修改已确认 demo；不清理无关代码；不提交 Git。

## 2. 根因（代码已核对）

1. `statusBar.cloneStatus` 将 `stepUsage` 固定置 null；`server.getSessionStatus` 与 `sessionStore.updateUsage` 只存 lastTimings，没有与之同源的单次用量（stepUsage），TPS 分子重开会话后缺失。
2. `statusBar.applyIncomingUsage` 的「timings 与 stepUsage 成对替换、缺一即双置 null」会在无 timings 帧到达时把已显示的 TTFT/TPS 清成 `—`。
3. `appendAssistantHistory` 渲染顺序为 thinking / content / Elapsed / Thinking，模型级 Elapsed 违背用户诉求；live 侧同样存在。
4. 工具 elapsedMs 只覆盖执行过程；适配器累积参数片段但没有记录首个非空参数片段到达时点，toolCallStart 事件已是参数组装完成后才发出，不能当参数生成起点。
5. durationMs 是单次模型请求时长；整轮端到端无任何计时；且工具可并行，多步不能简单相加。
6. attach 回放压缩 reasoning 增量，回放时本地 Date.now 间隔无意义。

## 3. 精确定时口径

### 3.1 TTFT / TPS（含前端规则与旧数据 fallback）

- 口径不变：TTFT = 请求发出到首个文本/思考增量；TPS = 本次 completionTokens ÷ 思考结束（无思考则首输出）到模型调用收尾的秒数（近似吞吐）。本次只改展示与持久化，不改语义。
- **后端配对**：`lastTimings` 与 `lastStepUsage` 必须来自同一个 usageUpdate 帧的同源字段（pump 缓存后原子回写索引）。后端收到只有其中之一的帧仍可更新累计 usage，但**不替换**索引中的最近有效配对（缺一不双清、不覆盖）。
- **前端规则（必改，对应根因 2）**：`statusBar.applyIncomingUsage` 废止「成对替换、缺一双置 null」。新规则（同样适用于 `commitStatusResponse` 权威 GET 路径，不得只改 applyIncomingUsage）：
  - 累计 usage / context / cost 照现有守卫更新；
  - 仅当帧同时携带**有效 timings**（readTimings 非 null **且 durationMs 有效**，半截 timings 不算完整对子）**且同源 stepUsage**（三 token 字段合法非负且 completionTokens>0 时才可更新 TPS 显示；仅累计用量更新不受此限）时，才替换 `snapshot.lastTimings / snapshot.stepUsage`；
  - 否则保留上一对，已显示的 TTFT/TPS 不闪成 `—`；
  - `commitStatusResponse` 的 authoritative GET 同样只在响应携带完整对子时替换快照配对（`cloneStatus` 保留服务端 lastStepUsage，不再置 null）。
- 状态栏 title：最近一次有完整计时和用量的模型调用（非会话累计）。
- **旧会话只读 fallback 执行细则**（仅索引缺 `lastStepUsage` 时触发）：
  - 数据源：该会话 JSONL，自后向前找**最近一条 `timings` 与 `usage` 同时存在于同一 `assistantMessage` 事件**的记录；
  - usage 形状转换：复用 `historyView.normalizeUsage`（provider snake_case → camelCase stepUsage 口径），禁止拿 usageRecord / 会话累计 / 索引里旧 lastTimings 与别的 usage 拼对；
  - 合法性：timings 需 readTimings 非 null 且 durationMs 有效；usage 在归一化前先拒绝 bool/非整数/负数（True 是 int 子类必须显式拒绝）、归一化（normalizeUsage）异常当非法跳过；归一后三字段非负且 `completionTokens > 0` 才构成可显示 TPS 对子（0 分子不配对）；任一非法即不配对、不显示 TPS（真实未知，不编造）；
  - 缓存：结果按 sessionId 存进程内只读缓存；失效条件 = 索引已写入 `lastStepUsage`，或该会话 jsonl 新增了 assistantMessage（按日志 mtime/事件水位判断）；每次 GET 不全量重扫；
  - fallback 只读不写：不修改旧 JSONL，不把结果写回索引（避免把日志值固化）；新轮 usage 帧会写入完整对子，自然接管。
- live TTFT 仅首输出实时展示（`…` 占位本次 TPS，不得拿上一调用的 TPS 配本次 TTFT）；有效配对帧到达后接管；切会话（resetForSession）/close/goIdle 清理 live 临时态。

### 3.2 Thinking（不新增事件）

- 保留每个模型 step 的 thinking 块 + 紧邻耗时兄弟行结构。
- 流中即时值：本窗口本地首/末 reasoningDelta 计时（现有逻辑保留）。
- 权威值：沿用既有链路 `usageUpdate.timings.reasoningStartMs/reasoningEndMs`（与 assistantMessage.timings 同源）在帧到达时覆盖本地值；无 usage 的调用不补权威值（宁缺勿造）。
- **不新增 modelTimingsEvent**（两轮审核一致结论）：sseCodec 对未知类型会编成 error 帧、前端把它当失败；attach 压缩也不该本地推测计时。attach 回放未到权威帧前不显示 Thinking。
- 无思考、旧数据缺打点、非法区间不伪造；首/末同刻可为 0.0s。

### 3.3 Tool call / Tool execution

- **透传链（必改，否则字段到不了前端）**：`argumentsStartedAtMs` 需同时进 ①`historyView.loadMessages` 的 toolCall DTO（现有只保留 id/toolName/arguments，需加该字段，缺省 None）；②`sseCodec.toolCallToDict`（attach 回放复用同一帧）。`callElapsedMs` 走 details 原样透传。convertMessage 不读该字段（不回发模型）。
- `core/types.toolCall` 新增字段 `argumentsStartedAtMs: int | None = None`（服务端 UNIX 毫秒）。JSONL 落盘经现有 `toJsonable(asdict)` 自然携带；`conversation._resumeFromLog` 恢复该字段（旧记录缺省 None）。该字段不发送回模型（convertMessage 不读它）。透传链见本节「透传链（必改）」。
- **Chat Completions**：processSseData 累积每个工具 index 的 arguments 片段时，首个**非空**片段（空白不算）到达即记录 UNIX 毫秒存入 accum；合成 toolCalls 时赋给 `toolCall.argumentsStartedAtMs`。
- **Responses**：responseState 每个 function_call slot 首个非空 arguments（delta 或带完整 arguments 的 item，含 `function_call_arguments.done` 首现分支）记录时点；buildCompletion 时赋给 toolCall。多工具各自独立。
- **执行完成计算**：工具 worker 内（`executeToolCall` 返回处）计算 `details.callElapsedMs = 结束 UNIX 毫秒 − argumentsStartedAtMs`；`details.elapsedMs` 仍为 monotonic 实际执行时长，两者并存。callElapsedMs 包含参数生成/传输、排队（顺序或并行池）、确认等待；并行工具在各自 worker 返回时计算，不用协调线程按序落盘时刻。
- **确认等待与起点**：内存 pendingConfirm 持有原 toolCall 对象（现有 `setPending(pendingConfirm(toolCalls=toolCalls))` 引用成立），driveConfirmation 从该对象取起点——**仅进程内有效**；服务重启后 pending 本身不在内存（现有语义），确认后的工具无起点则不画外部脚标，不伪造。
- 未知工具、拒绝（userRejectedApproval）、崩溃占位/取消结果不新增 callElapsedMs；执行出错算执行结束；无起点或负差不画。
- **前端历史挂载点（精确）**：`appendAssistantHistory` 每张卡先 `shell.body.appendChild(card.el)`，随后 `shell.body.insertBefore(footnote, card.el.nextSibling)` 插外部脚标（卡片已挂截，nextSibling 语义正确）；按 toolCallId 去重（重复 id 只更新文本）；pending 卡（无 callElapsedMs）不插脚标，不挤位。流式侧在 `resolveToolCardOnEnd` 写入卡片注册表持有的脚标引用。
- 折叠工具详情只隐藏 `.tool-card-detail`，外部脚标是卡片兄弟节点始终可见。更新同一 toolCallId 不新增重复脚标。

### 3.4 Total（发消息 → 最终 completed）

- **起点**：`chatStreamSync` 入口（进入函数即记 `time.monotonic()`，验证/附件拼接/idle 等待全计入），作为参数传入 `runUserMessageStream(turnStartedAt=…)`；生成器在持锁进入 `driveUserMessage` 时写入 `conversation.turnStartedAt`（float | None）。库直接调用（无参数）时首次迭代时取当前 monotonic。**线程边界**：起点值在请求线程捕获、经生成器闭包传给泵线程写入 conversation，不依赖请求线程存活；agent 在确认等待期间被 drop 不影响已落盘轮次的 Total（重启后的确认续流无 conversation 内存起点则不画，不伪造）。
- **终点**：最终 completed 的可观测时刻——即最后一个模型步无工具、产出 `completedEvent` 之前，Core 在该处计算 `totalElapsedMs = time.monotonic() − conversation.turnStartedAt`。该时刻必然晚于最后一个 toolCallEnd 的 yield（工具全部结束后才会进入最终模型步）。非流式（completeStream 单 finalChunk）无逐 chunk，以该最终 assistant 组装完成时刻为终点。
- **统一表述**：本方案全文与测试断言只用「起点=chatStreamSync 入口 monotonic、终点=最终 completed 产出前」这一口径；不使用「最后 token 到达 Core」的表述（避免与并行收尾顺序产生歧义）。usage 尾包/落盘与 completed 的先后按现有代码自然发生，不作承诺。
- **计算位置**：`driveModelLoop` 与 `driveConfirmation` 之后的最终无工具 assistant 路径统一收口（两处都产出 completedEvent 的位置）；confirm 续流**不重置** `conversation.turnStartedAt`，直到新用户轮覆盖。
- **透传链（必改）**：`completedEvent.totalElapsedMs` 需同时进 ①`sseCodec.eventToFrame` completed 帧（现有只发 message）；②`historyView` 最终 assistant DTO（现有白名单不含消息级 totalElapsedMs）。P1.5「三处相等」依赖这两处。totalElapsedMs 同时写入最终 assistantMessage 额外字段（JSONL 持久化，历史只读展示）；中间模型消息无 Total；停止/错误/待确认不产出 completed，不画 Total。

## 4. 改动范围与责任

| 文件 | 必需修改 |
|---|---|
| core/types.py | toolCall.argumentsStartedAtMs、completedEvent.totalElapsedMs（默认 None，兼容旧构造） |
| models/chatCompletions.py | 首个非空 arguments 片段时点 → 合成 toolCall 赋值 |
| models/responsesAdapter.py | function_call slot 首个非空 arguments 时点 → buildCompletion 赋值 |
| core/conversation.py | turnStartedAt（运行时）、_resumeFromLog 恢复 argumentsStartedAtMs、appendAssistantMessage 落 totalElapsedMs（可选参数） |
| core/agent.py | 最终 completed 处计算 Total；executeToolCall 结束处 details.callElapsedMs；runUserMessageStream/driveUserMessage 写 turnStartedAt；driveConfirmation 续流不重置 |
| backend/agentManager.py | pump 缓存 lastTimings+lastStepUsage 同源对（缓存条件与 lastTimings 现有条件不同：需两者同时有效才覆盖配对）；有效配对帧原子回写索引（缺一不覆盖） |
| backend/sessionStore.py | updateUsage 增可选 lastStepUsage；仅完整对子写入；防布尔/负数/非法 |
| backend/server.py | chatStreamSync 入口 monotonic 起点传参；GET status 返回 lastStepUsage + 旧日志只读 fallback（含缓存与失效、严格 usage 校验） |
| backend/sseCodec.py | completed 帧携带 totalElapsedMs（缺省 None）；toolCallToDict 增 argumentsStartedAtMs（缺省 None） |
| backend/historyView.py | 最终 assistant 透传 totalElapsedMs；toolCall DTO 增 argumentsStartedAtMs；工具 details 原样（callElapsedMs 随 details） |
| frontend/statusUsage.js | formatSpeedLine 收窄为 TTFT/TPS-only（或等价纯函数）；成对校验 helper |
| frontend/statusBar.js | 删速度行/独立渲染；用量行末尾追加 TTFT/TPS；applyIncomingUsage 新配对规则；cloneStatus 保留 lastStepUsage；live TTFT 生命周期 |
| frontend/chatView.js | 删模型级 Elapsed 与 stepDirty/timingPainted/footnoteEl；thinking 紧邻（历史顺序修正）；工具外部脚标（紧邻卡片、按 id 去重）；最终 completed 后画 Total |
| frontend/index.html、styles.css | 删 #statusSpeed；.tool-call-total/.total-elapsed 样式；用量行窄屏内部横向滚动；提 cache-bust 版本 |
| tests | 新红测试；改与新契约冲突的旧耗时断言（Elapsed→删除、speedLine 两形态→TTFT/TPS-only）；不删无关覆盖 |
| docs/webApiSpec.md | 同步 status 配对、completed/history Total、tool details.callElapsedMs 口径 |

（**不新增任何事件类型**；sseCodec 除 completed 增字段外不动事件集。）

每个修改的代码文件更新小版本号、日期、description；lowerCamelCase；不做无关重构。

## 5. 可验证目标与详细 TODO

### P0 计划审核与基线
- [x] P0.1 阅读前后端/适配器/存储/测试，确定数据缺口（见 §2）。
- [x] P0.2 基线 `uv run pytest -q` = 359 passed。
- [x] P0.3 初审（subGROK）5 项 + 复审 5 项意见已吸收进 v1.2 正文。
- [x] P0.4 复审通过后保存相关文件修改前快照用于隔离 diff（/tmp/latencySnapshot）。

### P1 红测试（实现前必须失败）
- [x] P1.1 状态栏：GET 完整对子显示 TPS；流中有效值遇 authoritative refresh / 无 timings 帧后**保持**；A→B→A 与冷启动仅从 API 重建同值。
- [x] P1.2 GET/流竞态、pending 合并、跨会话旧回调不串配对；无有效数据不显示虚构 TPS。
- [x] P1.3 pump→index→status 同源 stepUsage；旧日志 fallback（同事件、normalizeUsage、非法拒绝、缓存失效；不用会话累计、不误配、新轮接管）。
- [x] P1.4 两协议多工具首个非空参数起点（含空白片段、Responses arguments.done 首现）；callElapsedMs 并行/顺序/确认等待/拒绝与取消缺省；JSONL 恢复 argumentsStartedAtMs；重启后确认无起点不画。
- [x] P1.5 多步骤与确认续流 Total：起点=入口、终点=最终 completed 产出前、晚于最后 toolCallEnd；最终 assistant / completed / history 三处相等；停止/错误/待确认不画。
- [x] P1.6 thinking 紧邻且无模型级 Elapsed（live+历史）；attach 压缩回放不本地计时；外部脚标紧邻对应卡片、按 id 去重、折叠详情不隐藏、pending 卡不挤位；无数据不画。
- [x] P1.7 前端三脚本版本号与 index.html cache-bust 同步。

### P2 最小实施
- [x] P2.1 类型/事件字段扩展（默认值兼容旧构造）。
- [x] P2.2 适配器参数起点 → toolCall → JSONL/resume。
- [x] P2.3 入口起点 → conversation → 最终 completed Total → completed 帧/history。
- [x] P2.4 executeToolCall callElapsedMs（含确认路径与并行 worker）。
- [x] P2.5 后端同源配对（pump/index/status）+ 旧日志只读 fallback。
- [x] P2.6 前端状态栏（TTFT/TPS-only、新配对规则、live 清理）。
- [x] P2.7 前端消息区（删 Elapsed、thinking 顺序、工具外部脚标、Total）。
- [x] P2.8 HTML/CSS/cache-bust/接口文档/测试版本断言同步；删除因此孤立的旧 helper/state/style。

### P3 验证与交付
- [x] P3.1 定向 pytest（含 Node 真模块 VM harness）通过。
- [x] P3.2 前端脚本 `node --check` 全通过。
- [x] P3.3 全量 `uv run pytest -q` = 373 passed（基线 359 + 新增 14）。
- [x] P3.4 新 subagent（subGROK）独立审查：范围无越界；指出 A1 fallback 缓存失效死代码（已修：日志 size+mtime 水位 + invalidate 接线到泵/删除会话 + 回归测试）、A2 用量行窄屏滚动缺失（已修：#statusUsage 行内 overflow-x）；另修 sessionStore 日期、resetForSession 清 live、半截 timings 不清 live。
- [x] P3.5 回报根因、改动、实测结果与生效方式（backend 变更需重启服务后强刷）。

## 6. 风险与取舍

- Total 起点是服务端可观测入口（含 idle 等待），非浏览器敲 Enter；不声称客户端网络端到端，不做遥测。
- UNIX 参数起点仅同一服务端时钟内计算；跨重启的确认链路无起点则不画，不伪造。实际执行与 Total 用 monotonic。
- 旧会话缺参数起点/整轮起点不还原 Tool call/Total；TPS 仅在旧日志同事件成对时可只读恢复。
- 前端配对规则不得弱化 generation/requestId/usageRevision 守卫；usage/费用合并规则保持。
- 并行工具终点=各 worker 返回时刻，与按序落盘分离，不用协调线程时间。
- 不新增事件类型（避免 sseCodec 未知事件兜底/前端误判错误）；思考权威值复用 usageUpdate.timings。
- 配对替换失败不得提前 return 掉 usage 合并；pump 配对缓存条件与 lastTimings 现有条件需同步改（有效 timings 即覆盖 lastTimings 的旧逻辑同时收窄）。

## 7. 审核记录

- 初审（subGROK/grok-4.7，第 1 轮）：①前端成对双清丢 TPS；②旧日志 fallback 无执行细则；③modelTimingsEvent 多余且事件流接不住；④Total 终点漏确认续流与并行收尾；⑤确认等待后 toolCall 起点丢失。→ 已吸收。
- 复审（subGROK/grok-4.7，第 2 轮）：指出 v1.1 正文未同步（§3.2/§4/§6 仍留 modelTimingsEvent）、前端规则与 fallback 细则未落正文、Total 起点线程边界未指定、types.toolCall 字段未点名、历史脚标挂载点不精确。→ v1.2 全文重写吸收：§3.1 前端规则+fallback 细则、§3.2 撤销事件、§3.3 字段+进程内语义+insertBefore 挂载、§3.4 线程边界+统一口径、§4 表删除事件行、§6 删除事件行。
- 终审（subGROK/grok-4.7，第 3 轮）：指出 A1 argumentsStartedAtMs 与 A2 totalElapsedMs 的透传链断点（historyView/sseCodec 白名单未列）、A3 fallback usage 严格校验规则（bool/非整数/负数/0 分子）、卡片 append 顺序。→ v1.3 吸收：§3.1 合法性细则与「同样适用权威 GET 路径」、§3.3 透传链+先 append 再 insertBefore+pending 不插脚标、§3.4 透传链、§4 对应行更新、§6 补同步条件。
- 终审结论：第四轮明确“无必须修复问题，可以实施”（v1.3 定稿）。
- 实施记录：P1 红测试先失败（14 个）后实现转绿；P2 后端（types/adapters/conversation/agent/sseCodec/agentManager/sessionStore/server/historyView）+ 前端（statusUsage/statusBar/chatView/index/styles）落地；与新契约冲突的旧断言已改为新契约；全量 373 passed。
