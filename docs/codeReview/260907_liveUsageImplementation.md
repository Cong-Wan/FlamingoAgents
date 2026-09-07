# 代码审核报告 — 状态栏逐模型调用用量更新

- Author: wilbur
- Version: 1.2
- Date: 2026-09-07
- 审核者：主代理（独立于 grok-4.6 实施代理）
- 范围：原方案 v1.6、今日执行计划对应全部 13 个业务/测试/契约文件（含未跟踪新文件及旧诊断测试fake字段补齐），基线 `90d7417`。
- 状态：**最终代码审查与自动化验收通过；本次发现 R1–R7 全部关闭，无未解决的 Critical/High/Medium/Low。真实 provider/可视双浏览器人工未执行。**

## 最终验收（主代理独立复跑，2026-09-07）

- `uv run pytest -q`：**120 passed in 3.83s**（基线79，新增/扩充41项）。
- `node --check`：statusUsage.js、statusBar.js、chatView.js逐个通过。
- `git diff --check`：通过。
- `uv run pytest -q tests/testLiveUsageUpdate.py -k RecordUsageOwnerWaiterRace`：**连续10轮，每轮8 passed**；覆盖stop/pump两个owner × 正常/DB错/sessions错/缺conversation四路径。waiterEntered门闩证明实际竞争；None哨兵晚于owner完成尝试。
- 端到端桩验收：真实Core generator与泵、长工具Event未释放期间DTO已广播、sessions已中间更新、lastUsage未改、DB无中间落账；释放后JSONL snake_case映射与sessions/usage.db累计对齐；批准/拒绝两个实际pump分别落各自delta。
- 前端Node真实脚本+轻量DOM/API/SSE桩：旧权威GET、新pending（含相同token新context/cost）、普通请求复用在途权威、stop POST先后、确认对象复用、A→B→A、attach回放/404、自然closed/断流路径通过，无恒真关键断言。
- 主代理在末次测试重排后恢复独立计价次数覆盖（临时价格桩，不读真实凭据）：直接stub下query/load各一次；真实query函数内部+显式load共两次；非零startUsage的两个DTO费用手算一致，流中writeUsageTurn=0、终态1次。冗余的预先耗尽Core对账由真正长工具流对账替代。
- API文档上游事件数校正为9；最终资源键：statusUsage 1.1、statusBar 1.6、chatView 1.21；原styles.css 1.19保持。
- 未执行真实provider调用、真实浏览器视觉/双窗口手工演练；不以桩测试冒充人工或线上验证。未提交Git、未启动部署。

| 问题 | 最终处理 |
|---|---|
| R1 普通GET抢占权威 | 普通refresh复用同会话/代际在途请求（含权威），降价校准通过 |
| R2 pending丢费用/context | 只覆写合法字段，已知费用单调保留；请求前后pending时间语义分别验证 |
| R3 恒真断言/假竞争 | 精确动作前后计数、真实未settle连接与waiterEntered门闩，8路竞争反复通过 |
| R4 测试资源 | fixture try/finally恢复previous连接，临时读连接显式close |
| R5 生产接口/新增冗余 | pump直读实际adapter；只补旧fake必要字段；删新无用helpers，新增常量小驼峰 |
| R6 百分比舍入 | toFixed+binary-exact半数点half-even，Python生成密集样例交叉验证 |
| R7 相同累计的新pending | 每条接受的pending均递增revision，相同token新context/cost不再被旧权威覆盖 |

以下保留首轮/二轮问题及复現过程以便追溯，均非当前未解决问题。

## 首轮总览（历史）

- 主代理独立复跑：`uv run pytest -q` → **109 passed in 2.64s**；三个 `node --check`、`git diff --check` 通过。
- 发现问题：Critical 0 / High 2 / Medium 2 / Low 2。
- 审核维度：Bug、资源、并发、逻辑复杂度、接口、模块融合、性能、风格。Core/codec 双映射、泵基线计价/实际模型、锁外认领等待、前端主要连接身份方向正确；测试全绿并不足以证明关键竞态正确。

## 问题清单

### R1 [High] 普通 refresh 抢占在途权威 GET，最终价格无法校准

**位置**：`statusBar.js::refresh` 单飞条件 `&& !inFlight.authoritative`。

**复现**：已显示 usage=10/0/1、cost=1 → closed 发权威 GET → 其未返回时 open 尾部或另一调用者发普通 refresh → requestId 被递增使权威响应失效 → 普通响应同 usage、cost=0.2 被 Math.max 保留为1；无后续 SSE closed，不再校准。

主代理用现有 deferred harness 注入断言实际复现：期望 `$0.2000`，实际 `$1.0000`。

**最小修复**：同 session/generation 的普通 refresh 应复用当前在途请求（包括权威），不能降低已请求的权威性。权威 refresh 仍可启动新 GET，使更早所有请求失效。

```javascript
if (!authoritative && inFlight
    && inFlight.sessionId === sessionId
    && inFlight.generation === generation) return inFlight.promise;
```

**测试**：上述交错中不新增普通 GET、复用权威 Promise、费用最终降为0.2。另覆盖无快照、权威 GET 在途时新 pending 到达，fallback 不抢占且仍按 usageRevision 合并。

### R2 [Medium] pending 替换会丢掉已经收到的费用/context

**位置**：`statusBar.js::storePending`。

**复现**：无快照 → U1 usage=10/0/1, cost=1 → U2 usage=20/0/2, cost=null（或缺失）→ fallback GET 返回旧 usage/cost=0.1 → 当前实现完全替换 pending，已知 liveCost=1 丢失，显示0.1。

主代理实际复现：期望 `$1.0000`，实际 `$0.1000`。pending 收较小 finite cost 也会丢掉单调保障；缺失 context 同理。

**最小修复**：candidate 仍经三字段支配判断；接受后只覆盖合法 context/finite cost，费用与前 pending 单调取大，缺失/null 保留既有；首帧缺 cost 不伪造账单。不要因合并 pending 破坏“请求前旧 pending 不抬回权威降价”的时间语义。

**测试**：pending 缺 cost/null/降 cost/缺 context、任一 usage 回退整帧忽略；完整 snapshot 同样保留费用；权威前后 pending 时序各自有测试。

### R3 [High] 关键验收断言恒真或未实际制造计划竞态

**位置**：`tests/testLiveUsageFrontend.py::testChatViewConnectionStopAndAttach` 及 `tests/testLiveUsageUpdate.py`。

**具体问题**：
1. 旧 confirm event 后才记录 `appliesBefore = applies.length`，然后断言 `... || applies.length === appliesBefore`，后半恒真。
2. `errorBar... || errorBar.children.length >= 0` 恒真；该段当前还有 attaching placeholder，点击 send 未必真的创建了 cut 流。
3. A→B→A 用的是早已 reject 404 的 attach Promise；无法再模拟迟到 done。`applyCount` 没断言。自然 completed 的 some(authoritative) 可由更早 stop 刷新满足。
4. confirm 前泵已 resolve/flush 后才批准，没有制造最关键的“立即批准，旧 done 后到”。
5. `testRecordUsageStopFinallyRace` 只并发调两次 `_recordUsage`，没有执行真实 `requestStop`/`_pump.finally`、managerLock、seal 和 None 哨兵顺序；time.sleep 不是确定性 barrier。缺 owner DB错/sessions错/conversation缺失下的真实收尾验证。
6. 对账测试先耗尽 Core 再伪造 pump.startUsage；未证明工具仍阻塞时 DTO 已入 history、DB仍无写，也未测 confirm 两个实际 pump 的起点。
7. 声称覆盖的显式 loadCostMap 抛错、本泵失败不重试、Core interruption/失败等场景缺失。

**最小修复**：在现有两个测试文件里写确定性的行为测试；每次 action 前快照计数，用精确等式/对象身份/phase/错误内容断言。不增加框架，不靠测试用例名称或grep代替协议。

必须覆盖：
- 同一对象 confirm 先启动新连接再 resolve 旧 done，并分别用 reject 旧连接/迟到 event；新流对象、id、phase、用量计数、refresh数均不受影响。
- 同 session S1结束未closed → S2开始结束 → S1closed；A→B→A时旧回调（尚pending）随后到达；当前自然completed/无终态断流独立计数。
- attach 成功的 streamResume/preInit/回放 DTO、未初始化404静默与旧placeholder回调；send409元信息重试仍保留。
- stop Promise 在同 session新流开始后才完成，旧stop不得refresh。
- 真正 _pump/requestStop（含 managerLock）+threading.Event/barrier 控制 owner/waiter；I/O尝试结束之前订阅队列无None，结束后唯一写账和seal、无迟到DTO，缺会话/DB错/sessions错全路径；不让失败测试遗留非daemon卡死线程。
- 长工具 Event 阻塞期间观测已广播 usage DTO/当前sessions/lastUsage未改/DB无写，释放后对账；批准/拒绝的两个真实pump只计各自增量。
- 费用显式load异常、query异常的多个事件均只尝试一次；Core modelInterrupted、无final、失败无事件。

### R4 [Medium] 新测试的资源隔离/清理不完整

**位置**：`testLiveUsageUpdate.py::isolatedStores`、`testJsonlSessionsUsageDbReconciliation`。

**问题**：fixture 保存 `previous=dbConnection`，但正常创建临时连接后只 close+设None，不恢复 previous；teardown 未用 try/finally。临时 sqlite3.connect(...).execute(...).fetchall() 创建的读连接未显式关闭。新增的 json/Path 等导入及 makeAgent.sessionId 等参数无用。

**修复**：fixture 通过 try/finally 关闭本fixture创建的连接并无条件恢复 previous；对账连接显式 close（sqlite connection 的 with 只管事务，不自动关连接）。清理本次新增的未使用导入/参数，不动旧死代码。

### R5 [Low] 为测试桩掩盖生产接口缺失，且新增 helper 过宽/命名不合约

**位置**：`agentManager.py::__init__`、`statusUsage.js`、新测试常量。

**问题**：原计划直接依赖 agent.modelAdapter.config；为旧testPumpErrorWritesJsonl fake没有modelAdapter，在生产增加嵌套getattr和unknown模型兜底，会隐瞒错误账单身份。statusUsage新增 isDangerousKey 却只检查写死的三key，恒false；usageEquals/emptyUsage 等只测不用或完全不用，多个重复包装与大写 TOKEN_KEYS/META_KEYS/ROOT/USAGE_KEYS 不符合小驼峰要求。

**修复建议**：生产按已知agent接口直接读config；只给 `tests/testModelStreamDiag.py::testPumpErrorWritesJsonl` 的旧fake补最小modelAdapter.config（这是因接口固化必需的相关测试调整，可追加到批准范围并递增该测试文件头）。删除本次无业务消费者的helpers/exports，常量小驼峰，保留必要纯计算，不造泛化防护；测试不要为了留下无用API而调用它。

### R6 [Low] context 四舍五入在半数点与 Python 后端不同

**位置**：`statusUsage.js::contextUsedPercent`（Math.round） vs `server.py::getSessionStatus`（Python round）。

**例子**：contextTokens=33,contextWindow=400 → 8.25%；Python round(...,1)=8.2，JS=8.3。不会影响token或费用，但不完全符合“百分比严格对齐后端”。

**建议**：保留问题记录；如能在不扩后端API/不引依赖的前提下简洁复现后端一位小数语义，补对照测试并修正；不要为显示小数构造大框架。不能声称现有Math.round对所有输入与Python完全等价。

## 修复优先级

1. R1 普通请求不得取消权威性；R2 pending 不能丢最近已知值。
2. R3 去掉恒真断言、用真实并发和未结束流证明端到端时机。
3. R4 资源隔离；顺手只清理本次新代码的R5冗余与测试桩接口。

## 二轮复审（主代理，2026-09-07）

- 独立复跑 **116 passed in 3.85s**，三个JS语法检查和diff检查通过。R1/R2已复现修复后行为通过；R4资源修复、R5生产接口、R6 Python对照均通过。前端R3多数真竞态已补齐。

### R7 [Medium] 相等累计的较新 pending 没增加 revision，丢掉新 context/cost

**位置**：`statusBar.js::storePending` 新增 `usageAdvances` 条件。

**问题**：原契约接受三项相等帧以更新context/cost，usageRevision必须在每个接受的事件（含pending）递增，不仅在token前进时。首版是无条件递增，R2修复无依据改成仅token前进。

**实际复现**：pending usage10/0/1 context11 cost1 → 发权威GET → 同usage新帧 context40 cost2 → 老GET同usage context11 cost0.2返回。期望 `$2.0000 / 40%`，实际 `$0.2000 / 11%`。

**最小修复**：删除 `usageAdvances` 及其条件；通过支配规则后的每一帧都执行 `pendingRevision = ++usageRevision`。这不影响请求前pending丢弃，时间由事件是否在请求后接受来确定，不能由token是否严格增加来代替。补本次真实复现测试与无新事件时仍可权威降价对照。

### R3剩余 [Medium] 竞争测试仍未证明 waiter 已进入，错误路径没有参与 stop 竞争

**位置**：`testRecordUsageStopFinallyRace` / `testPumpOwnerRequestStopWaitsForIo` / `testRecordUsageErrorPathsStillSeal`。

stop-owner 测试先完成stop写账才放开stream，pump finally实际没有与owner竞争；pump-owner测试启动stopper后未等待它进入_recordUsage waiter即release I/O，也可能只串行通过。错误/缺conversation测试只自然finally，没有requestStop/waiter。

**最小补证据**：使用 `usageRecordDone.wait` 的轻量包装发 waiterEntered Event；明确等待waiterEntered后才释放owner门闩。按owner=stop/pump × failure=none/db/sessions/missing参数化（缺conversation可在返回None前门闩拦截conversations.get，不改生产）。必须运行真实 startStream/requestStop/_pump，None哨兵在owner完成尝试后、DB只一次、两线程收口；别用线程启动等于线程已进入wait的假设。

新文件中的 ROOT/HARNESS 仍需小驼峰，只有pytest约定fixture名称tmp_path等保留。

## 已确认优点与未验证边界

- Core safePayload/合法usage门卫未改变provider原生累计；事件在工具前产生；codec双分支均保留。
- pump惰性计价无构造期I/O，使用当前累计减startUsage、实际模型入账；正常record锁外I/O且finally.set。
- statusBar已修复“旧权威响应覆盖新SSE”的主路径，chatView已加connectionId/latestBound/stop POST延迟校准。
- 真实provider/可视浏览器/双窗口人工尚未验证，不记作通过；需通过更强自动证据后再定最终代码验收。
