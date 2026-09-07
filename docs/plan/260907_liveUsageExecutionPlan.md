# 状态栏逐模型调用用量更新：执行与验收计划

- Author: wilbur
- Version: 1.2
- Date: 2026-09-07
- 状态：**已由 grok-4.6 实施并完成两轮修复；主代理代码审查与自动化验收通过（120 passed）。真实 provider/可视双浏览器人工项未执行。**
- 原方案：`docs/plan/260902_liveUsageUpdatePlan.md` v1.6；本文是当前代码的执行补充，冲突处以本文为准。
- 代码基线：`90d7417`；工作区初始干净；`uv run pytest -q`：79 passed in 2.30s。
- 分工：全新 grok-4.6 子代理独立审核方案；通过后另派全新 `xaiSubscription/grok-4.6` 实施；主代理负责审核、复跑测试、追踪修复与验收。不自动提交 Git。

## 1. 目标、假设及不做的事

采用原方案 D：每个模型 API 调用收到完整合法 terminal usage 并 append 成功后发一次非终态 usageUpdate；工具执行期间即可看到上一模型 step 的精确累计统计。不是逐文本 chunk 估算。

Core 保持 JSONL snake_case/内存 camelCase 口径；Web 流中只写 sessions usage/context，不写 usageTurns、不覆盖 lastUsage；终态每泵至多一次按实际 adapter provider/model 落账。Core/DTO 双 codec 映射永久共存；CLI 忽略新事件。

不改 adapters、数据库 schema、usageStore API、/model 行为；不重构无关代码。沿用 uv 的现有 Python 环境和 pytest/Node assert，不引入依赖或真实模型请求。

## 2. 当前代码对照与必须补齐的边界

原方案 Core、泵、SSE、前端纯 helper 设计可沿用；原文版本表需按当前文件头递增：chatView 1.19 起、index.html 1.13 起、webApiSpec 1.18 起。保留最近已上线的工具参数折叠与订阅模型发现功能。

### E1：旧权威 GET 不能覆盖同会话新 SSE（High）

位置：原 §7.5.3 refresh 与 §7.5.4 onStreamClosed。

复现顺序：S1 closed 发权威 GET → 同 session S2 开始 → S2 usageUpdate 已应用 → S1 的 GET 返回。session/generation/requestId 均可能仍匹配，原文“authoritative 可完整替换”会把新用量与费用覆盖为旧值。

修正要求：refresh 另捕获用量修订号（usageRevision，接受有效 usageUpdate/pending 时递增）；不得用连接代际替代用量修订号（新流尚无 usage 时仍应允许前泵校准价格）。若权威请求发出后已有新用量，响应的 usage/context/cost 必须降级为单调合并，不能完整回退；普通元信息可照常刷新。无新用量时仍允许权威价格下调。pending 也带相同时间语义：请求前的 pending 不得在权威校准之后再把旧 liveCost 抬回去；请求后的 pending 仍应合并。不能通过始终 Math.max 禁掉终态价格校准。

无 snapshot 时不能因 revision 前进而丢弃 GET：先用合法响应建立含 location/model/window 的完整快照，再按上面的 revision/pending 时序合并。无新用量的权威响应不再重放 `pendingRevision <= requestRevision` 的旧 pending；`pendingRevision > requestRevision` 的新 pending 要合并。reset 清 usageRevision/pendingRevision，generation 隔离旧回调。每个实际新 GET（而非单飞复用）递增 requestId；权威新 GET 必须使所有更早普通/权威 GET 失效，旧响应/旧 finally 均不得提交或清理新请求。

验收：可控 deferred Promise 交错测试覆盖旧权威 GET 晚到、新 SSE 先到，以及无新事件时权威降低 cost；包括无 snapshot 时既存 pending/请求后 pending 的不同处理。

### E2：本地 stop 的 SSE closed 不等价于后端已落账（High）

当前 chatView.stop 先发 stopChat 再同步 abort SSE；abort 后 done 会 resolve，stop POST 此时可能仍在执行 _recordUsage。原文 closed 立即 GET 会读到旧账并向下校准，且再无刷新。

修正要求：在本流保存 stop 请求的完成 Promise。本地 stopping 收口 UI 仍立即执行，不等待网络；该流的权威状态刷新异步等待 stop POST 完成尝试后才发起，并在等待后重新检查 session/stream/connection（及下述生命周期身份），旧流不得刷新后来的连接。普通自然 closed 保持原 fire-and-forget 刷新；stop API 失败仍允许 best-effort 校准，但不得宣称持久化成功。不能 await 完 refresh 再 goIdle，不能新增轮询。

明确接点：`stream.stopRequest = api.stopChat(sessionId).catch(...返回null...)` 必须在 `stream.abort()` 前挂到捕获的 stream 对象；closed 依据 `closedStream.stopRequest` 判断是否异步等待，而非依赖迟到时的 currentStream.phase。延迟刷新检查 latestBound/session/connection 和 currentStream 为 null 或本对象，不能要求 currentStream 必须非 null，因为 UI 已 goIdle。catch 必须在等待链上消化拒绝，不能制造未处理 rejection。waitingConfirm/stopping/断流/自然终态等五条原路径不省略。

验收：模拟 SSE abort/done 早于 stop POST，POST 解决前不发权威 GET；解决后仅一次；若期间切会话/开新流则不触碰新视图。后端 requestStop/finally 的落账与关闭哨兵竞争仍按原方案覆盖。

### E3：closed 的 currentStream=null 特例必须有最近连接/视图生命周期守卫（Medium）

原文允许 completed/error 先 goIdle 后 currentStream=null 的 closed 校准，这是必要的。但 A→B→A 重开且当前空闲，或 S1 后又开始并结束 S2，旧 S1 closed 仍可能通过旧对象上的 connectionId 检查。

修正要求：chatView 保存最近绑定的连接身份（至少 latest connectionId，close/open/showEmpty 时清除失效），closed 只有该连接仍是当前视图最近连接时才允许 null 特例。新 send/confirm/attach 更新最近身份，确认复用对象仍分配新 id。event/failed 继续要求 appStore.stream 为捕获对象；attach 的未初始化 reset/streamResume/preInitBuf/404 保留；send 的 409 meta 保留。无需重构完整聊天状态机，也不借此修改无关历史异步流程。`goIdle()` 不清最近绑定身份（当前连接 completed→null→closed 仍需校准）；只有 open/close/showEmpty 使视图生命周期失效，bindConnection 更新最近身份。

验收：A→B→A、同 session 两流结束后旧 closed、confirm 复用对象的前连接 closed/event/failed 均不会刷新或结束新视图；自然 completed 的当前连接 closed 仍会最终刷新。

### E4：前端加载缓存与回归测试

index.html 当前 chatView.js 已带 `?v=1.19`，tests/testToolCardCollapse.py 对该 URL 有断言。更新 chatView 版本时同步新 URL 和该相关测试断言，不移除缓存失效机制；新增 statusUsage 先于 statusBar、statusBar 先于 chatView。必须给新增 statusUsage 和修改的 statusBar 加版本 URL（首轮分别 `?v=1.0`、`?v=1.4`），chatView 首轮改 `?v=1.20`；保留未修改的 `styles.css?v=1.19`。如后续修复递增代码头版本，同步相应资源键与相关断言。不得为通过测试保留过时 chatView 资源键或改弱无关断言。

## 3. 实施 TODO（原方案 §9、§10 的细目全部继承）

### Gate A：复审与派发
- [x] A1 核对目标代码、已有改动、当前版本及干净基线。
- [x] A2 基线全量 pytest 通过（79）。
- [x] A3 落档本计划，明确新增竞态、验证方法及原方案关系。
- [x] A4 全新 xaiSubscription/grok-4.6 对原方案+本文+当前代码独立审核，报告保存 docs/codeReview/260907_liveUsageExecutionPlanReview.md。
- [x] A5 修复审核问题，全新 grok-4.6 最终复审通过（零阻塞），见 docs/codeReview/260907_liveUsageExecutionFinalReview.md。
- [x] A6 Gate 通过后派全新 xaiSubscription/grok-4.6 实施，指定工具 read/write/edit/bash，无子代理嵌套。

### Phase B：Core 与 Web 原子事件通路
- [x] B1 types.usageUpdateEvent / agent 外层 step 基线值拷贝、safePayload/合法原生 usage 门卫、append 后 yield；不改变 adapter/conversation。
- [x] B2 同时落地 sseCodec Core 分支；再加固定在 codec 的 usageUpdateDto 与 Web 分支，保留未知对象 error 兜底。
- [x] B3 pump 固化实际模型、费用 pending/ready/unavailable；构造中无 SQLite/YAML；首事件惰性初始化、费用从累计减 startUsage 算，不逐事件累加。
- [x] B4 两次 stop/done 检查间转换 DTO，中间 sessions update 不改 lastUsage，失败不阻断；history/attach 保序 DTO。
- [x] B5 终态 lock 原子认领、锁外 wait/I/O/set，所有路径发 done，不双写；持久化异常不阻断 seal。
- [x] B6 pytest 覆盖原 §10.1/10.2：纯文本/工具/重试/批准拒绝/缺失非法 usage/双映射/历史/费用失败/无价格/实际模型/初始化次数/stop 竞争及无 conversation。

### Phase C：前端直接渲染与异步安全
- [x] C1 新建小驼峰 statusUsage.js 带规定头；纯归一化、三字段支配关系、费用/百分比/保元信息。
- [x] C2 statusBar snapshot/pending/reset session generation、单飞 fallback、requestId、非权威合并、权威校准，落实 E1 修订号/pending 时间语义。
- [x] C3 chatView 消费 usageUpdate、open/close/empty 重置；send/confirm/attach 的三重身份及 E3 最近连接，保完整原状态机。
- [x] C4 本地 stop 完成 Promise 与 E2 异步刷新；UI 收口不等待 HTTP；await 后身份再检查。
- [x] C5 index 加载顺序、修改文件头小版本/description、资产 URL 与相关测试 E4。
- [x] C6 pytest 启动 Node assert 纯函数 + 简单 DOM/api/SSE stub 行为测试（无新框架）；异步关键路径不能只靠源码字符串静态匹配。
- [x] C7 deferred 测试单飞 open/fallback、pending 陈旧事件、reset/迟到 finally、新旧权威 GET、stop 时序、三入口连接身份、confirm 对象复用、attach 未初始化404/断流、waitingConfirm/stopping/null收尾。

### Phase D：契约与主代理验收
- [x] D1 webApiSpec 逐调用事件与完整工具/确认序列、状态机非终态、流中 sessions/终态账单/liveCost vs authoritative；注明本地 abort 不是持久化完成信号。
- [x] D2 逐文件 `node --check`（statusUsage/statusBar/chatView）、`git diff --check`。
- [x] D3 实施者运行新测试及 `uv run pytest -q` 全量；记录输出，不放宽既有断言。
- [x] D4 主代理依 code-review skill 全量审阅所有本次 diff，按8维度落档 docs/codeReview/260907_liveUsageImplementation.md；首轮R1–R4须修复，R5/R6为简化与显示精度建议。
- [x] D5 缺陷派 grok-4.6 修复，主代理复审复跑；R1–R7关闭，本次无未解决审查项，120测试通过。
- [x] D6 独立复跑长工具前usage广播、连续调用累计、临时JSONL→sessions→usage.db对账、批准/拒绝两个实际pump增量与8路stop/finally竞争（连续10轮）；只用临时目录/桩。
- [x] D7 轻量DOM/API/SSE行为验收自动完成；下列真实provider/可视双浏览器人工项目明确未验证，不视为通过。
- [x] D8 汇总修改文件、测试结果、剩余限制给用户；未自动提交或启动部署。

### 尚未执行的真实环境人工验收（与自动化通过分开记录）
- [ ] 原 T4.5–T4.6：真实模型 + 长 bash/read、连续工具循环的可视状态栏与真实账单对账。
- [ ] 原 T4.7–T4.8：真实双浏览器 attach、立即批准/拒绝的 UI 演练。
- [ ] 原 T4.9–T4.11：真实网络 stop/断流、切页/连续连接、活跃流 `/model` 409 与刷新缓存检查。
- 以上核心协议已由桩/临时库自动测试覆盖，但不等价于 provider/真实浏览器手工验证。

## 4. 成功标准与范围

1. 单模型完整 usage 在下一工具执行前可经 Core→pump DTO→SSE 更新 UI，连续 step 不重复累加/不倒退。
2. 每泵 DB 至多一次、实际模型正确、lastUsage 终态才更新；stop 并发不死锁/不双写/关闭不先于落账尝试。
3. 状态栏旧帧/旧 HTTP/旧会话/旧连接不污染新用量，终态仍可按新价格向下校准；本地 stop 等后端完成尝试才做权威 GET。
4. 现有行为与全部测试通过；新增测试能覆盖上述故障时序。不以仅静态查字符串代替竞态行为测试。
5. 修改范围为原 §8 十个文件，另允许因资产 URL 更新所必需的 tests/testToolCardCollapse.py，及因pump固定读取实际adapter配置所需的 tests/testModelStreamDiag.py 旧fake最小补字段（不得用生产unknown-model回退迁就测试桩）；如确需新增专门测试文件须小驼峰/规定文件头并说明原因。审核/执行文档属于任务范围。

## 5. 执行记录

- 最终主代理验收：`uv run pytest -q` **120 passed in 3.83s**；三个 `node --check` 和 `git diff --check` 通过；stop/finally 8路实际owner/waiter竞争连续10轮全通过。报告 `docs/codeReview/260907_liveUsageImplementation.md` v1.2。
- 主代理补回测试重排时被移除的直接/真实query计价调用次数验证（两条参数化、非零泵基线），规范新usageKeys命名；契约事件数8→9校正。业务实现及缺陷修复由用户指定grok-4.6完成，主代理独立验收；本计划v1.2仅补充执行范围、结果与未测人工项。

- 2026-09-07 主代理初审：原设计方向正确，当前基线可兼容；E1/E2 为开工前必须补入的正确性要求，E3/E4 为连接安全与近期代码对接要求。
- 2026-09-07 独立首审允许联合开工，要求把 3 High/2 Medium 实施注意点写死；v1.1 已全部吸收：无 snapshot GET 不丢、usageRevision 不以连接代替、旧 pending 不抬回权威价格、stop Promise 挂对象且拒绝消化、latestBound goIdle 保留、全部更早 GET 失效、三脚本 cache-bust。最终复审已通过。
- 2026-09-07 全新独立终审确认零阻塞；实施必须联合遵守原方案 v1.6 与本文 v1.1，主代理验收不以子代理自报通过代替实际复跑。
- 2026-09-07 grok-4.6 实施记录：按 v1.6+v1.1+E1–E4 落地 Core 事件/双 codec/泵 DTO 与原子落账、statusUsage+statusBar+chatView 身份与 refresh 修订号、契约 v1.19。前端行为测试拆到 `tests/testLiveUsageFrontend.py`（规定头），因 Core/泵对账与 DOM/SSE deferred 体量都大，未引新框架。`node --check` 三个 JS 通过；`git diff --check` 通过；`uv run pytest -q` **109 passed in 2.66s**（基线 79 + 新测试 30）。未跑真实 provider/双浏览器手工项。D4–D8 留主代理。
- 2026-09-07 grok-4.6 修复记录：按审核报告 R1–R6 最小改动，不重设计、不勾 D5–D8。R1 普通 refresh 复用同 session/generation 在途权威 GET；R2 pending 合并保留已知 finite cost/context 且费用单调；R3 重写前端/泵竞态测试为 deferredPromise 与 Event barrier，attach preInit 外层身份+绑定回放；R4 fixture try/finally 恢复 dbConnection 并关闭临时读连接；R5 生产直接读 modelAdapter.config，仅给 testPumpErrorWritesJsonl 旧 fake 补字段，删除无消费者 helper/大写常量；R6 以 toFixed(1) 加 *.25 half-even 对齐 Python round，并用 Python 生成样例交叉比对。版本：statusUsage 1.1、statusBar 1.5、chatView 1.21、agentManager 1.11、index 1.15。
- 2026-09-07 grok-4.6 二轮剩余修复：先补 R7 deferred 复现（无 snapshot pending 10/0/1 context11 cost1 → 权威 GET → 同 usage pending context40 cost2 → 旧 GET context11 cost0.2；失败 `same-usage newer pending keeps cost 2`，实际被写成 $0.2000/11%）。再撤销 storePending 的 usageAdvances，接受帧一律 pendingRevision=++usageRevision。R3 换成 owner×failure 参数化真实 startStream/requestStop/_pump，wrap usageRecordDone.wait 发 waiterEntered 后才放 ownerGate；缺 conv 在构造后/streamStart 前门闩 conversations.get。新测试 ROOT/HARNESS → rootPath/nodeHarness。版本 statusBar 1.6、index 1.16。
