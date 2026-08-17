# Function Call 中断后会话 400 事故溯源计划

- Author: wilbur
- Version: 1.1
- Date: 2026-08-14
- Status: v1.1 已通过最终独立复审（无 H/M 问题，可执行）
- Incident session: `session_3ebfe236ecae`
- Error signature: `an assistant message with 'tool_calls' must be followed by tool messages ... askSubAgent:50`
- Scope guard: **只读日志与源码，只新增/修订调查文档；不修改任何代码、配置、会话日志或业务数据。**

## 1. 任务目标

对“function call 执行期间停止任务，随后再次发送消息即持续收到 HTTP 400”的事故进行证据驱动的深度溯源，并形成可供后续修复评审使用的事故报告。

报告必须回答：

1. `askSubAgent:50` 在本地日志中对应哪一个真实调用；
2. 中断前后，父会话、子代理进程/子会话、持久化日志和内存 conversation 分别发生了什么；
3. 为什么当次停止看起来成功，但下一条消息才暴露 400；
4. 哪一段代码直接制造了不合法消息序列，哪一项设计决策是根因；
5. 为什么已有 dangling/orphan 恢复逻辑没有在本次热会话中生效；
6. 影响范围、触发条件、竞态窗口、可恢复性及复发风险；
7. 后续修复应满足哪些协议不变式与验证用例（只给建议，本次不实施）。

## 2. 已知事实与待证假设

### 2.1 已知事实

- 目标文件：`webData/sessionLogs/session_3ebfe236ecae.jsonl`，共 107 条事件。
- 第 101 条是带 `askSubAgent` tool call 的 assistant 消息，本地 call id 为 `tool_MAIQ903IGSwq6hnU4cLS8xvF`。
- 第 101 条之后没有匹配的 `toolResult`；第 102 条直接是新 user 消息。
- 第 103/105/107 条均为相同的 provider HTTP 400，并保存了实际请求 payload。
- 第 103 条请求共有 51 个 tool calls；按请求内出现顺序零基编号时，第 50 个唯一对应最后一个 `askSubAgent`（本地 id `tool_MAIQ903IGSwq6hnU4cLS8xvF`）。因此错误中的 `askSubAgent:50` 可与该调用唯一映射；它是 provider 合成的“工具名:序号”诊断标签，不是本地原始 `tool_call_id`。
- 第 103 条请求尾部直接呈现 `assistant(tool_MAIQ...) → user(我看到子代理已经干完了)`，两者之间没有 `role=tool`，这是协议断点的一手证据。
- 对应子会话定位为 `.agentLogs/session_20337ff178c1.jsonl`；它产生了代码改动，最后一条是删除命令被确认策略拒绝的 bash `toolResult`（UTC 09:55:06），此后没有最终 assistant 完成消息。
- 当前代码在工具被 stop 时沿 `modelInterruptedError` 直接退出，不写 `toolResult`。

### 2.2 待证假设

- H1：事故行为由 2026-08-14 的“工具执行可中断”改动引入，而非 provider 偶发故障。
- H2：用户报告的 stop 触发了 `interruptEvent`/kill process group 路径，`modelInterruptedError` 经过 tool runtime 直通到 `driveToolBatch`，在 `addToolResult` 之前返回。由于项目没有 stop/access 持久化审计，此项需要按“源码与日志形态支持的高置信因果”表述，不能伪称有直接 kill 日志。
- H3：热 conversation 不维护本轮新增 dangling call；`_resumeFromLog()` 的 orphan 修复只在 agent/conversation 重建时运行，因此同一缓存实例继续发送时没有修复。
- H4：第一个 400 后，每次新消息仍会先持久化 userMessage，再调用 provider，导致会话持续失败并进一步积累未处理 user 消息。

## 3. 调查方法与证据等级

### 3.1 证据来源

1. **P0 原始证据**：目标 session JSONL、provider 400 中保存的 request payload、对应子代理 JSONL。
2. **P1 实现证据**：`agent.py`、`conversation.py`、`toolRuntime.py`、`builtinTools.py`、`agentManager.py`、`chatCompletions.py`、`server.py`、`chatView.js`。
3. **P1 变更证据**：Git commit/diff/blame，重点检查 `e49de66` 及其方案 `stopResponsivenessPlan.md`。
4. **P2 只读验证**：脚本解析消息配对、统计 tool call 序号、只读构造 resume conversation，验证热态/冷态差异；不发真实模型请求，不写 fixture，不运行测试框架。
5. **P3 用户描述**：用于确认触发动作；若缺少持久化 stop 审计，只作为时间范围证据，不伪造精确时间。

### 3.2 判定标准

- “已证实”：至少有原始日志 + 源码路径，或两份相互独立的一手证据。
- “高置信推断”：证据完全吻合，但缺少持久化 stop/access log，必须明确标注推断边界。
- “未知”：现有数据无法恢复，报告中显式列出，不用猜测补齐。

## 4. 执行步骤与详细 TODO

### Phase A：冻结与校验原始证据

- [ ] 记录目标日志路径、大小、mtime、SHA-256、事件总数，确保调查期间未改写；所有解析脚本只以只读方式打开日志，不创建/写回 fixture。
- [ ] 输出 107 条事件的精简时间线，只保留类型、时间、tool call/result 配对与错误摘要。
- [ ] 对第 103/105/107 条 `request.messages` 运行配对校验，列出第一个协议断点。
- [ ] 单独导出第 103 条请求末尾的 `role / tool_calls.id / tool_call_id` 邻接序列，把 `assistant(tool_MAIQ...) → user` 中间缺失 tool response 固化为报告主证据。
- [ ] 核对第 101 条 assistant 的真实 call id、工具名、参数和前一轮调用是否均已闭合。
- [ ] 统计请求中所有 51 个 tool calls，记录“零基序号 50 + 工具名 askSubAgent + 唯一未闭合调用”与 `askSubAgent:50` 的唯一映射；同时注明该标签不是本地原始 id。

### Phase B：还原跨进程时间线

- [ ] 关联父会话第 101 条与 `.agentLogs` 中对应子会话（模型、prompt、启动时间、workDir）。
- [ ] 对比子会话最后事件、工作树文件 mtime、父会话下一事件，区分“业务代码已写入工作树”与“子代理进程/父工具调用已正常返回”。
- [ ] 展开核对子会话末条 bash `toolResult` 的内容（删除命令因需确认而被拒绝）及其后无 assistant 终态的事实；禁止把该普通工具拒绝本身误写成 kill 证据。
- [ ] 查找 stop 请求/access log；若项目未持久化 stop 事件，记录可观测性缺口及不可恢复的精确停止时刻。若只能结合用户陈述与持久化事件，时间线可把 stop 表示为候选区间：不早于子会话最后**已持久化**事件 UTC 09:55:06、早于父会话下一条 user UTC 10:01:14；必须注明上下界包含 P3 用户动作顺序假设，找到更强证据时以强证据为准。
- [ ] 说明为什么正常完成、普通失败、timeout 都应产生 parent `toolResult`，而“用户报告 stop + 子会话无最终 assistant + 父调用无 toolResult + 当前 interrupt 直通源码”共同高置信指向中断路径；同时承认缺少直接 kill/access log。

### Phase C：定位代码级因果链

- [ ] 从 provider assistant completion 入盘开始，逐行追踪：`appendAssistantMessage` → `driveToolBatch` → `executeToolCall` → `_runWithInterrupt`。
- [ ] 追踪 stop：前端 `/chat/stop` → `agentManager.requestStop` → `interruptActiveStreams` → interrupt event → kill process group。
- [ ] 追踪异常：`modelInterruptedError` 在 `toolRuntime` 和 `agent.driveToolBatch` 的捕获/返回行为。
- [ ] 单独走查 `driveConfirmation`：用户批准后的工具执行若被 stop，是否同样在 `addToolResult` 前直接返回，并与普通批次路径对照。
- [ ] 明确指出 `addToolResult` 未执行的位置及由此破坏的 OpenAI tool-call 配对不变式。
- [ ] 追踪下一条 user 消息为何被先 append，再构建模型请求并触发 400。
- [ ] 解释 400 为何显示“已重试 0 次”（400 不属于当前重试集合），排除重试机制异常。

### Phase D：热态、冷态与影响面分析

- [ ] 验证 `danglingToolCalls` 只在 `_resumeFromLog()` 初始化，当前热 conversation 不会自动扫描新缺口。
- [ ] 只读重放目标日志，验证冷 resume 遇到“assistant(tool_calls) → user”时会插入内存占位 tool message，但不会回写 JSONL。
- [ ] 分析“日志末尾仍是 dangling assistant”时 cold resume 可能重新执行被用户中断的工具，评估副作用风险。
- [ ] 走查 `driveToolBatch` 的“整个可执行前缀先批量 Start、再串行执行”结构：当前缀中任一工具中断时，量化已执行/未执行调用各自缺失 toolResult 的放大效应。
- [ ] 枚举影响路径：`askSubAgent`、`bash`、确认后工具、批量 tool calls 中途停止；排除模型流中断和已落 toolResult 后停止。
- [ ] 判断当前 session 的故障持续性、缓存 agent 生命周期与重启/重建后的可恢复边界。

### Phase E：历史变更与设计根因

- [ ] 用 Git diff/blame 确认行为首次引入的 commit、时间和具体行。
- [ ] 对照 `stopResponsivenessPlan.md`，检查 L3.5 “中断不得包装成工具失败”决策与“assistant 消息落盘不变式”是否冲突。
- [ ] 分层归因：触发条件、直接原因、代码根因、设计根因、可观测性/测试防线缺口。
- [ ] 判断严重等级、确定性、回归属性和潜在数据一致性影响。

### Phase F：报告与建议（不实施）

- [ ] 在 `docs/toolCallInterruptionIncidentReport.md` 形成完整报告。
- [ ] 报告包含：执行摘要、影响、证据表、时间线（stop 使用证据允许的区间，不伪造精确时刻）、协议断点、调用链、五问/因果树、影响矩阵、恢复行为、修复候选方案及权衡、验收用例。
- [ ] 修复建议必须以协议不变式为中心：任何持久化 assistant tool call 在下一条非 tool 消息前，必须恰有对应 tool result；stop 可以阻止继续调用模型，但不能留下未闭合消息。
- [ ] 区分“建议立即止血”“推荐根修”“防御性自愈”“可观测性补强”，不写代码。
- [ ] 调查结束重新计算目标日志 SHA-256，并检查 `git status`，证明未修改代码、配置、session log 或业务数据。

## 5. 成功标准

1. 能把第 101→102→103 条事件和 `askSubAgent:50` 一一映射，无未解释跳步。
2. 至少给出一条从 stop 到缺失 tool result 的完整源码调用链，标注文件与行号。
3. 明确解释已有 resume 兜底“为什么存在但本次没生效”。
4. 对事故根因给出高置信、可反驳的结论，并列出证据与未知项。
5. 建议覆盖单工具、批量工具、确认工具、热会话、冷恢复、重复发送六类验证场景。
6. 除调查计划和报告文档外，`git status` 不出现由本次调查造成的源码/配置/日志变化。

## 6. 风险与边界

- 目标 session 已有后续 3 次失败消息；不能把当前文件尾误当中断瞬间原始尾部，必须按时间切片分析。
- `.agentLogs` 与 `webData/sessionLogs` 使用 UTC 时间戳，文件系统 mtime/当前时间为 `+08:00`，报告统一双标或转换后注明时区。
- provider 错误中的 `askSubAgent:50` 不是日志里的原始 `tool_call_id`；本次可由请求内“工具名 + 零基序号 + 唯一未闭合调用”精确映射到 `tool_MAIQ...`，但不把这一单次映射外推成所有 provider 都采用相同编号规则。
- 不执行真实模型请求，不手动补日志，不重启服务，不触发现有 session 的恢复，以免污染证据。
- 工作树在调查前已包含技能编辑子代理产生的未提交改动；报告必须区分 pre-existing changes 与本次新增文档。
