# FlamingoAgents Workflow 与 Subagents 详细方案

> Author: wilbur  
> Version: 1.2（方案复审通过；待用户确认后实施）  
> Date: 2026-09-09  
> Description: 基于实际代码设计固定开发闭环。v1.1 明确 coordinator 受管执行、暂停决策入口、集成失败返修、审修回边及独立验收；v1.2 仅更新复审状态与最新测试记录，无设计变更。本次不实施代码。

配套：[详细实施 TODO 与测试计划](260909_workflowTasks.md) · [调研证据与审核记录](260909_workflowReview.md)

## 1. 结论：不重写 Agent，增加受控编排层

**推荐：保留当前 Agent 内核，增加 `subagents` 执行层和一个固定的 `developmentWorkflow`。** 模型决定“方案是什么、代码怎么写、问题在哪里”；程序决定“什么时候允许继续、哪些任务可并发、哪个版本通过审核、如何停止及恢复”。

你的流程是三种模式的组合，而不是一群 agent 自由聊天：

1. **Evaluator–optimizer**：方案→独立审核→修订→重新审核。
2. **Orchestrator–workers**：主 agent 拆任务→依赖 DAG→并行开发→串行集成。
3. **独立验收关卡**：全新 agent 在最终集成版本上运行验收→通过后主 agent 汇报；不通过则返修再验。

一期不引入 LangGraph/OpenAI Agents SDK 作为新运行时，也不做通用 DAG 编辑器、无限递归代理、分布式队列或跨账号自动切换。需要的状态、进程、Git、安全边界，即使换框架也必须自己完成。

### 1.1 明确假设与待确认建议

- 保持 `flamingoAgents` 纯 Python 库与 Web 解耦；当前 Python 项目已由 uv 管理，`requires-python >=3.12`，不新建环境、不擅自升级 Python。
- 首版支持单机 POSIX（当前 macOS，可测试 Linux）、单用户、一个 workflow 宿主进程；不承诺 Windows 的进程组语义。
- 默认所有角色继承**启动 workflow 时主 agent 实际生效的 provider/model**；仅显式覆盖才换模型。换会话、全新审核不等于换模型。
- 推荐默认最大并发 2；计划审核最多自动 3 轮、验收返修最多自动 3 轮，到阈值暂停，绝不以“次数用完”当通过。
- 计划通过后的人工批准是额外开关，推荐开启。若你希望审核通过便自动开发，可在启动时关闭这一关；不能关闭独立审核或验收。
- 开发在私有 worktree 中完成。默认交付已验收的私有分支/patch/报告，**不自动改写、提交或推送用户原分支**；写回原工作区是后续单独批准的操作。
- worktree 解决协作隔离，**不是安全沙箱**。首版面向受信任的本地项目；不承诺防御恶意 shell、测试脚本、主动 `setsid` 逃逸或凭据窃取。处理不可信代码前必须增加容器/VM 等隔离。
- 本次只提出建议，不启动开发。上面的默认值和交付方式尚待用户确认。

### 1.2 三个层次，不要混淆

| 概念 | 在本项目的职责 | 不应承担的职责 |
|---|---|---|
| Agent | 读上下文、请求模型、调用工具、生成事件、维护会话 | 全项目任务调度、Git 合并、跨任务恢复 |
| Subagent | 一个有身份、独立实例/会话/预算的有限子任务执行者 | 自己绕过审核关卡、修改全局 DAG、无限派发 |
| Workflow | 持久状态机、审核门禁、任务就绪判断、并发、集成、恢复 | 替模型完成需求分析和编码 |
| Skill / prompt | 规定角色的方法、标准、输出内容 | 强制并发、权限、超时、版本一致性 |

“全新 subagent”在程序中是硬不变量：新的 `attemptId + agent 实例 + adapter + sessionId + 日志`，不恢复实现者对话；仍允许读取需求、代码、计划和历史问题等客观材料。

## 2. 当前代码到底支持什么

以下为 2026-09-09 调研截面，行号仅用于定位；旧方案不能作为已实现能力。

| 能力 | 代码证据 | 结论 |
|---|---|---|
| 装配不同 agent | `flamingoAgents/builder.py:28–111` | 已支持模型、prompt、toolNames、workDir、logDir、skillsDir；没有 workflow |
| 事件流与会话锁 | `core/agent.py:110–135,656–679` | 同实例按 session 串行；独立实例可并发，但共享 adapter 不宜复用 |
| 一批工具执行 | `core/agent.py:376–435` | 先批量发 Start，再逐个 execute；**不是并行工具执行** |
| 子代理工具 | `tools/builtinTools.py:304–366` | 用 `sys.executable sdkEntry.py --json` 启子进程，每次新会话；同步等待 |
| 模型/工具继承 | `builtinTools.py:305–338`、`sdkEntry.py:87–121` | model 必填；workDir 默认继承；模型配置、tools 配置等重新从默认路径加载；tools 为空则没有工具 |
| 深度限制 | `config/tools.yaml:98–127`、`builtinTools.py:327–330` | “不可传 askSubAgent”只写在描述里，入口没有相应硬拦截 |
| 确认 | `sdkEntry.py:59–62,114–120` | SDK 自动拒绝，不能把子代理确认转到 Web 后继续原任务 |
| 模型步数 | `core/agent.py:73–82`、`sdkEntry.py:109` | 默认无步数上限；SDK 显式设 -1；600/3600 秒不是完整成本治理 |
| 停止 | `builtinTools.py:158–215` | 有 poll/进程组终止，但管道背压和嵌套进程组边界未闭合 |
| 会话恢复 | `core/conversation.py:39–119` | 可恢复聊天；未闭合工具补取消结果，不重执行，pending 不持久恢复 |
| Web 流 | `webApp/backend/agentManager.py:119–238,322–352` | 有同会话单活跃泵和多窗口 attach；没有 run/task 状态持久化 |
| 模型步骤用量 | `core/agent.py:344–364` | 已发 usageUpdate，可复用；当前子代理结果不携带独立计费事实 |

`README.md` 的多 Agent 编排/并发派发仍为未完成项。`docs/plan/orchestratorWorkerPlan.md` 是 2026-08 的待实施方案；可借鉴分工和批量派发思想，不沿用共享工作树、线程超时后继续写、默认 32 步、“恢复确认即可续跑”等过时或不安全假设。

### 2.1 已复现的基础缺口

本次以 uv、内存假模型和短时本地子进程验证，未调用真实模型 API：

1. **错误会被当作成功文本**：`textDelta('ONLY_PROGRESS') → errorEvent`，`runSdk()` 返回 `ONLY_PROGRESS`；CLI 仍可能输出 `error: null`。`completedEvent.message` 也没有被作为最终正文使用。
2. **非法 stdout 会被当作成功**：子进程 exitCode=0、stdout 非 JSON，`askSubAgentTool()` 返回 `isError=False, content='None'`。
3. **管道饱和造成假超时**：2MiB stdout，无 interruptEvent 约 0.02s 正常完成；有未置位 Event 时约 2.09s 超时，只读到 65536 字节。原因是先等退出，后 `communicate()`。

所以不能直接把现有 `askSubAgent` 套个线程池就承诺可靠开发闭环。相关回归测试必须先落地。

静态风险另外记录：外层 SDK 与内部 bash 各自 `start_new_session=True`，杀外层组不足以证明全部 writer 退出；现有路径工具接受任意绝对路径，`bash` 也不是只读工具。

### 2.2 Web 对接必须处理、但不能顺手全面重构的问题

- Web 的 `doneEvent/stopped` 当前代表前台封流，不证明泵线程及全部 writer 已退出（`agentManager.py:176–202`）。workflow 需要独立执行终止状态。
- 普通聊天互斥只看 activeStreams，workflow 待审核/集成时可能没有聊天泵。需要统一的 session 操作租约，而不是重复利用“有没有 SSE”。
- worker completed/error 直接混入主聊天流会让主泵提前终止；需要独立事件封套。
- 当前普通聊天用量基线来自缓存 conversation，新建 agent 尚未 hydrate 时是零，恢复后可能把历史累计再次记账（`agentManager.py:170,378–428`，静态风险）。本设计所有 workflow 节点使用全新受管会话和步骤用量事实，不走这一差值算法，因此该既有缺陷另案修复，不顺手扩大本任务；若将来复用普通 pump，必须先复现并修复。
- 模型切换路由先改索引再检查 busy（`server.py:375–380`）；要保证活跃 workflow 不出现索引与实际模型脱节。
- 调研中发现配置目录迁移在并行进行，Web modelConfigStore 与 builder/modelConfig 的默认来源存在中间态。接线前必须统一显式配置来源，不能另建一套默认路径。

配置迁移补充复核：撰写v1.1时，当前 `builder.py` 已为v1.8，使用 `configPaths.userSystemPromptPath` 并调用 ensureUserConfig；modelConfig/toolConfig/skills/sdkEntry/Web store 已引用 `~/.flamingo/config/` 的统一常量。此前的“默认来源中间态”是调研过程中观察，不是对当前全部文件的最终断言。新模块应复用这套统一 resolver，仍需在实施前验证迁移和测试基线，不能再回退到仓库 config 默认查找。

以上缺口均不在本次直接修改。

## 3. 外部框架研究与取舍

官方资料于 2026-09-09 实际读取，借鉴概念，不假定其替我们解决文件副作用：

| 资料 | 适合借鉴 | 本项目取舍 |
|---|---|---|
| [Anthropic：Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | workflows/agents 区别、orchestrator-workers、evaluator-optimizer、先简单后复杂 | 用户流程高度匹配；先做固定可验证流程 |
| [LangGraph：Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents) | 状态、条件边、并行、审核循环 | 借鉴状态建模；多套可编辑 workflow 出现后再评估采用 |
| [LangGraph：Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | checkpoint 与跨线程 store 的区别、人工介入和恢复 | 对话 JSONL 与 workflow checkpoint 分开；有 checkpoint 不等于文件操作 exactly-once |
| [OpenAI Agents SDK：Agent orchestration](https://openai.github.io/openai-agents-python/multi_agent/) | 代码编排与 LLM 编排、agents-as-tools 与 handoff | 你的主 agent 必须持有最终解释权，适合 manager/agents-as-tools，不是交接用户会话的 handoff |

若以后需要多人、多机、长期任务服务、复杂动态图或大量可复用工作流，重新比较 LangGraph/专用任务平台。现在整套迁移会重复现有模型适配、工具确认、日志、Web 流桥接，且不消除 worktree/验收证据/进程取消工作量。

## 4. 推荐整体架构

```text
用户 / Web / CLI
        │ 明确创建 development run（幂等）
        ▼
workflowRunner（固定状态机；唯一状态写入者）
  ├─ workflowStore：run/task/attempt/approval/event/usage
  ├─ workspaceManager：基线、隔离工作树、验证、串行集成
  ├─ subagentRunner：独立实例/进程、结构化终态、预算与取消
  │    └─ createAgent → 现有 Agent/模型/工具/会话 JSONL
  └─ 主 agent 的规划/修订/总结节点
        │
        └─ 新 reviewer、coder、acceptor 子会话

Web workflowManager：鉴权、session 租约、API、事件 DTO、usage 桥接
（库不 import webApp，不向自己的 HTTP 服务发请求）
```

**两种使用方式：**

- 单独委派短任务：继续使用 `askSubAgent`，但修复其终态与管道问题；它仍是一问一答，不等于 workflow。
- 执行完整开发需求：调用新的 `workflowRunner.start(...)` / Web“开发流程”入口。编排器按状态和计划调度 subagentRunner，不指望模型主动连续调用正确的工具。

首版不额外增加泛用 `dispatchTasks`、`pollTask` 等一整套聊天工具。以后若需要从普通对话启动 workflow，只加很薄的 `startDevelopmentWorkflow` 工具：创建 run 并立即返回 runId，随后由宿主驱动；不能在该工具同步栈里占住主会话锁数小时。

### 4.1 主 agent 的身份与会话

- 主 agent 是用户面向的**逻辑 coordinator**，负责研究、计划、修订和最终总结；其身份由 parentSessionId、启动 binding 和持久产物连接，不要求一个活对象/长对话贯穿数小时。
- 研究、规划、修订、总结四类 coordinator 节点也统一交给 subagentRunner：每次登记 attempt、独立受管进程/adapter/session、有限步数和 deadline，全部计入 run 预算与取消树；不直接复用带全套工具的 Web agent 对象。
- Coordinator 节点仅给 scoped read/search/已登记 artifact 读取/submitResult；没有源码 write/edit、通用 bash、派发及审批工具。需要基线测试时由宿主执行批准的 testId，文档由宿主从已校验结果导出。即使主模型误想直接开发也无原生写工具。
- Web 的主 session 在 run 非终结期间由 workflow 持有操作租约，用户可查状态/批准/通过专用 respond 入口回应暂停问题/取消，但不能同时普通发送、确认旧工具、切模型或删除。涉及请求先在同一 admission gate 检查再产生副作用。
- 原 Web 会话不被多个子进程共同续写；各阶段对话归 run 日志。最终 coordinator 报告保存在 workflowStore/artifacts 并显示在主会话关联卡片，不再走普通聊天泵生成一份重复报告或账单。下次普通聊天按登记 runId 加入报告摘要/证据引用，而非拼原始 worker 对话。
- 原始需求、已确认澄清、计划与证据是各阶段的显式输入；不用自动生成的短摘要替代唯一原始约束。超过上下文预算则分页读取或暂停。恢复从产物重建新节点 attempt，不恢复 reviewer/acceptor 会话。

## 5. 固定 Workflow：阶段、关卡和循环

### 5.1 正常路径

```text
created → preflight → researching → planning → reviewingPlan（新 reviewer）
                                                  │ reject
                                                  ▼
                                             revisingPlan
                                                  │
                                                  └──→ reviewingPlan（再次全新审核）

reviewingPlan --approve--> awaitingPlanApproval（可选人工关）
                                  │
                                  ▼
 developing：就绪波次 → 并发 coder → 验证/串行集成 → 有剩余任务则下一波
                                  │
              冲突/测试失败 → paused(integrationBlocked) → 受控返修 → developing
                                  │ 全部 required tasks integrated
                                  ▼
 accepting（全新验收 agent + 独立测试）
      │ reject                          │ approve
      ▼                                 ▼
 repairing → developing          reporting（主 agent）→ completed
```

运行状态和阶段分别存储：`status = created/running/awaitingApproval/paused/cancelling/cancelled/interrupted/failed/completed`，`phase` 为上述业务节点。必须使用显式转移表，禁止随意设置组合；所有非终结阶段均可取消。`completed` 只在有效验收通过且最终报告已持久化时设置。

### 5.2 调研与计划的产物

调研要产出需求解释、代码入口/证据、既有约束、不确定项、基线测试命令与结果。需求矛盾或关键未知进入 `paused(reason=needsClarification)`，不能猜测后开发。

定稿计划必须包含：

- 原始需求与 `requirementId`，inScope/outOfScope，假设与待确认项。
- 架构选择、接口契约、文件级修改范围和不修改内容。
- 详细 TODO 的机器版 DAG，每项依赖、writePaths、测试、验收标准。
- 数据/兼容/安全/失败恢复边界。
- 风险、预算、交付方式。

人读版 `docs/plan/YYMMDD_<runId>_developmentPlan.md` 与机器版都由**同一份结构化计划**生成，不能分别让模型写两份互不对应的计划。模型更新说明和 task 数据后，程序校验并生成 `planVersion/planHash`；不能仅靠解析 Markdown 勾选框调度任务。

### 5.3 审核与修订

审核输入：原始需求、源代码基线、完整计划、任务 DAG、验收标准、前轮问题及修订差异。前一审核结果是参考材料，不是指令；不得只检查前轮问题而漏掉新改动。

审核输出：`verdict = approve/revise/blocked`、`planHash`、逐项问题（稳定 issueId、严重度、blocking、位置、修复建议）、需求覆盖结论。

通过条件由程序检查：

1. reviewer 的 sessionId 不属于计划作者/实现者，且本轮未复用已有 reviewer session。
2. 返回结果对应**当前** planHash、源码基线和需求版本；协议完整有效。
3. verdict=approve，open blocking issues=0，必需需求与测试覆盖检查通过。
4. 低优先级风格建议可以记录不阻塞；影响功能、依赖、可验收性、数据安全的明显问题不能被作者自行降级。分歧不能收敛时请求用户裁决。

修订创建新版本，前版审批立即失效。默认每轮创建新的 reviewer，减少顺从和上下文偏差。3 轮仍不通过时暂停并报告；用户可增加预算或改需求，不能自动“审核通过”。

### 5.4 暂停必须有可达的决策入口

每次暂停都持久化 `inputRequestId, kind, expectedStateVersion, relatedHashes, question, allowedDecisions`。用户通过 `respond(runId, inputRequestId, stateVersion, decision, responseId)` 回应；responseId 幂等，同 ID 不同载荷拒绝，过期请求/版本拒绝。只有宿主校验和消费决策，模型无权替用户回应。

固定的几种决策足够，不做泛用表单平台：

- clarification：用户补充需求，保存原文并产生 requirementVersion；影响方案时回 planning/reviewingPlan，旧通过失效。
- toolApproval：批准/拒绝明确参数 hash、作用域、基线与有效期；批准后宿主通过受控动作 ledger 执行/对账，旧 blocked attempt 不复活，新建后续 attempt。批准不等于立即运行不明副作用。
- budgetExtension：显式批准新的轮次/时长/步数上限，再从安全边界续跑；未批准继续暂停。
- baselineChoice：整理后的 HEAD 或显式仅用 HEAD；重新 preflight，源码基线变化使相应计划失效。
- integrationResolution：选择受影响 task 的返修/放弃，并附用户说明；按6.4节执行，不能强行把失败任务改 integrated。
- reconciliation：用户提供处置说明，宿主仍必须核验进程/工作树/副作用证据；无法确认无 writer 就不能继续。用户声明不能覆盖实际检测失败。

计划正式审批仍用 approve（绑定 planHash），resume 只请求从**已经解决决策、完成对账的安全 checkpoint**恢复，不代替回答问题、不默认批准。CLI 前台提供这些选择/文本输入，Web 面板提供对应请求卡片；cancel 始终可用。

### 5.5 验收不是“所有 agent 说完成”

进入 accepting 之前必须同时满足：

- 所有必需任务状态为 integrated；没有 failed/blocked/running/unknown writer；不是只看 future.done。
- 最终集成版本不可变，所有产物合法且工作区已经静止。
- 编排器重新执行的必需测试通过，日志、命令、环境指纹、exitCode 与版本绑定。

全新 acceptor 读取需求/计划、最终集成代码、完整基线差异、各任务结果、风险和测试证据；**不能复用 coder 或 plan reviewer 会话**，不以 coder 自报测试为唯一依据。它可以通过受控 runTests 重新执行验收项，不提供 write/edit/普通 bash。

输出包括每个 requirementId 的 pass/fail/blocked、问题定位、独立测试证据、`planHash + integrationTreeHash + testManifestHash`。程序同时检查必需项全部 pass、无 blocking 问题、版本未变化；模型一句“通过”不能覆盖测试失败。

返修：主 agent 将问题映射回任务/责任文件，创建新 attempt，在当前已集成版本上修复。验收结果全部失效，修后跑完整必需测试，并派新的 acceptor。若修复要扩大范围、改公共接口、依赖图或 writePaths，必须回到计划修订/新 reviewer 审核/人工审批，不绕过计划关卡。

最终报告包含完成/未完成需求、变更与交付路径、基线/最终版本、独立验收摘要、实际测试结果、风险、任务耗时与可得用量。总结措辞不能修改流程状态；用户拿到的是“已验收的指定版本”，不是对未来改动的保证。

## 6. 任务 DAG 与真正并行

### 6.1 Task 契约

最小字段：`taskId, title, requirementIds, dependsOn, writePaths, acceptanceCriteria, testCommands, inputArtifacts`。运行时补充 `planHash, baseRevision, attemptId, role, modelBinding, status`，不允许模型伪造运行字段。

校验必须拒绝：重复 ID、不存在依赖、自依赖/环、没有验收标准、非法/越界 writePaths、没有需求归属的扩展任务。测试命令是已经审核的 argv/cwd/timeout 结构，不是未经确认的任意 shell 文本。

### 6.2 一期采用“就绪波次”，不用过早追求动态最优调度

1. 找到全部依赖已 integrated 的 pending 任务。
2. 在同一 `integrationRevision` 上选择 writePaths/resource 不冲突的任务，最多 2 个并发。
3. 每项分配独立 attempt/worktree/agent；**派发前**取得可用槽位，不能先占线程再等信号量。
4. 等本波 writer 全部退出，校验各自产物和独立测试；失败则保留已验证产物、暂停依赖分支，不允许进入最终验收。
5. 按稳定 taskId 顺序串行集成已成功产物；集成后的必需测试成功，才标 integrated 并开放下一波。

这比“任何任务一完成就立刻开下游”少一些吞吐，但基线与验收更清晰，足以满足按依赖并发的需求。以后有瓶颈再优化。

示例：T1 定义契约 → T2 后端实现和 T3 前端实现可并行 → T4 端到端验证。T3 若只是依赖 T1 契约，可基于 fake API 写；若必须依赖 T2 新接口实际代码，应明确 dependsOn=T2，不由调度器猜。

即使不同文件也可能争用端口、测试数据库、同一生成器或依赖锁文件；为这些加少量显式 exclusiveResources。`__init__.py`、共享 schema、`pyproject.toml/uv.lock` 由一个明确任务拥有；不能让多个 agent 分别“顺手接线”。

### 6.3 调度预算

建议初值：maxParallel=2、taskTimeoutSeconds=1800、maxModelSteps=80、maxReviewRounds=3、maxRepairRounds=3、runTimeoutSeconds=7200；它们是起始建议，不是性能承诺。

- taskTimeout 从实际启动计时；runTimeout 包括排队、测试和集成，人工审批等待不消耗执行时长。所有时钟语义记录在状态。
- 宿主全局共享槽位，而非每个 workDir 各限各的。首版只是受管 worker 生命周期粗粒度限流，不宣称覆盖所有普通聊天、账号别名和 HTTP/TPS 配额。
- 保留现有请求级 429/5xx 重试；不对整个有写副作用的任务盲重试、不自动换 provider。
- 模型步数/墙钟可控；token 用量在每个已知 usage 步骤累计。未知 usage 标 unknown。费用只能估算，不能把迟到的 token 统计包装成绝对美元硬上限。
- 上述约束覆盖 coordinator/reviewer/coder/acceptor 所有模型节点，不只覆盖 coder。人工等待时间单独记账，用户通过 respond 显式扩大预算后才能再次执行。

### 6.4 正常集成失败也必须有返修回路

产物应用冲突或候选集成测试失败时：相关 task=`integrationBlocked`、run=`paused`，记录失败类型、当前 acceptedRevision、候选 patch/hash、日志和测试证据；失败候选不移动 accepted ref。发出 integrationResolution 请求，不能走 accepting，也不能仅反复执行 git apply。

用户选择继续后，主 coordinator 根据证据提出受影响 task 的新 attempt，基于**最新 acceptedRevision**修复；如果需要扩大 writePaths/接口/需求范围则先重新审计划。同波其它已验证但未集成的候选可保留，不自动当 integrated：基线没变仍需核验，基线变化则重新检查应用/测试与产物hash；不适用时也新建 attempt。原 accepted 产物不盲回滚，最终仍需完整测试和独立验收。

这是正常失败处理，与崩溃后的 intent 对账是两条不同路径；恢复必须区分“已知失败”与“是否生效未知”。

## 7. Subagent Runtime：如何落到当前代码

### 7.1 新建每个子代理的流程

`taskSpec → resolved binding → attempt 持久化 → 受控进程 → createAgent → 消费事件 → 结构化候选结果 → 明确终态/退出 → 父进程校验 → task 状态变更`。

新包内的 worker 入口使用 `python -m flamingoAgents.subagents.workerEntry`，避免依赖安装包之外的项目根 `sdkEntry.py`。开发与测试用 uv 管理环境，宿主用其已验证解释器启动子进程；不在每个任务里重新 uv init/安装依赖。

### 7.2 角色与工具

| 角色 | 能做的事情 | 默认不提供 |
|---|---|---|
| researcher / planReviewer | 受限 read/search、读取登记产物、submitResult | write/edit/bash/派发工具 |
| coder | scoped read/search/write/edit、受控命令执行、submitResult | askSubAgent、workflow 控制、用户原工作区写入 |
| acceptor | 受限 read/search、runTests、读取 diff/证据、submitResult | write/edit/普通 bash/派发工具 |
| coordinator | scoped read/search、读登记产物、submitResult；各节点同样受管 | 源码 write/edit、通用 bash、派发、DB/审批写入 |

新 `roleTools.py` 用现有 defineTool/toolRuntime 机制构建受限工具；复用 read/edit 处理逻辑，但先做路径归一化和允许目录检查。search 使用参数化本地搜索，不把查询拼成 shell。worktree 内真实路径、symlink 出界要检测。

只删除工具名不等于安全隔离：coder 的通用代码执行仍能绕过功能级限制，所以首版明确受信任执行边界。原生派发则必须硬禁止：不可变 role/depth，worker 工具表无派发工具，runtime 拒绝嵌套请求，不以 prompt 为唯一约束。

### 7.3 Prompt 与 skills

- Coordinator 继续遵守用户风格；固定角色 prompt 继承编码风格、文件头、小版本更新、测试标准及末尾标记等公共规则。
- 将“复杂任务需继续派 subagent 审核”的流程责任限定在 coordinator/engine；leaf prompt 不再复制会导致自派发的责任。每轮审核由 engine 保证。
- 默认 skills 关闭或显式白名单；code-review 方法可注入审核/验收角色，git/ssh 等不自动开放。
- 内容最小化：需求、任务契约、必要代码入口、依赖产物、约束和可验收标准；不复制整个主对话。模型可按允许路径进一步读文件。

### 7.4 配置继承必须是启动绑定，不是读取“最近会话”

- 从主 agent 实际 adapter.config 取得 `configProviderId/model/apiType/...`，宿主补上统一配置 resolver 与 credential 引用；CLI 显式传入。
- 冻结非敏感配置、角色 prompt/tools/skills 内容 hash 和预算。provider/model 默认用该 binding；覆盖必须显式且记录。
- API key、Authorization/custom secret headers、OAuth token 不进入 plan、事件、数据库或命令行。凭据运行时由既有 auth resolver 获取；重启后重新解析凭据，非敏感配置改变则暂停确认。
- 配置路径迁移完成前由宿主显式传所有路径，不从 worktree 找 config。builder 增加最小 keyword-only 装配入口以接收 resolvedModelConfig/明确工具定义/步数限制；普通调用参数缺省时行为不变，禁止从 workflow 绕过模型 API/auth 校验。

### 7.5 结构化结果不靠解析自然语言

使用角色专属的 `submitResult` 工具提交候选结果，服务端做明确 Python 校验；不要依赖模型在 final 中严格输出 JSON（这也避免末尾风格标记破坏 JSON）。现有 validateArguments 不完整支持 enum/boolean/maxItems，因此关键枚举、上限、hash/关联检查需专门校验，不能以“用了 schema”就视为安全。

worker stdout 使用版本化 NDJSON 事件，stderr/工具输出持续排空到受控日志。请求走 stdin，而非在 argv 放大量 prompt 或凭据。父进程只接受已知 schema/version、绑定正确 runId/taskId/attemptId 的消息，限制行长/总输出并标明日志截断。

父进程构造的终态字段：`status = completed/error/blocked/timedOut/cancelled/interrupted`，以及 `sessionId, finalText, result, artifactRefs, usage, errorType`。

**同时满足以下条件才成功**：合法候选结果、明确 completedEvent、子进程正常退出、未取消/超预算、结果版本对应且必需产物通过父进程检查。errorEvent/空流/坏 JSON/只有进度/缺失终态均不能成功。自然语言 finalText 仅用于阅读，不能推进关卡。

兼容修复：保留旧 `runSdk()` 正常完成返回字符串的用法；让其采用 completedEvent.message，失败显式异常；`--json` 的 reply/error 保留字段、失败非零退出；askSubAgent 严格校验结构。新 workflow 不通过旧文本协议调度，避免为旧 API 塞入整套状态机。

### 7.6 取消、管道与确认：不可伪装完成

- runner 持续消费 stdout/stderr，内存只保留受限预览；日志和完整产物分开，不能把截断 stdout 当 patch。
- 一个 worker attempt 由宿主建立独立进程组。workflow 专用命令执行不再像现有 builtin bash 一样另开脱离 worker 的 session；普通派生测试进程保持同组。该模式下命令超时升级为 attempt 终止，由宿主终止组，不在工具里误杀自己的组后继续模型。
- Stop 先持久化 cancelling、禁止新派发，再取消所有受管 attempts；协作取消→限时 TERM→KILL→wait/reap→核验进程组/产物。只有确认 writer 已退才释放工作区/进入 cancelled。迟到结果仅留诊断，不能推进状态。
- 进程主动脱组、父进程崩溃后身份无法核实等情况，标 interrupted/needsReconciliation，隔离目录并停止自动集成/清理。绝不承诺 `future.cancel()` 或 `kill(parentPid)` 等于完整停止。
- worker 要人工权限时首版**中止本 attempt 为 blocked 并上报请求**，不自动 approve，不保存一个无法恢复的 pending 让 UI 假装可续跑。用户授权需绑定 task/参数 hash/基线/有效期；将授权动作交给宿主受控执行、对账后创建新 attempt。未授权不得换一种命令偷偷绕过。透明恢复原 child 确认留待后续。

## 8. 工作区隔离、集成与交付

### 8.1 基线与脏工作区

- run preflight 获取 repoRoot、commonGitDir、HEAD、index/workingTree 指纹和忽略规则。首版要求正常 Git 仓库、已有提交、无 merge/rebase 冲突；submodule、LFS、稀疏 checkout、外链 symlink 等未支持状态明确阻塞，不悄悄丢内容。
- 用户有既有未提交/未跟踪内容时，自动开发默认暂停，用户可自行整理基线，或明确选择只基于 HEAD（说明这些内容不会进入 worker）；不自动 stash/commit/reset/clean。
- 调研可读脏工作树，但那份计划绑定 workingTree 指纹。后续换为 HEAD/新提交时必须重新核对并审核，不能把基于未提交代码的计划无条件复用。
- 引擎自己导出的 `docs/plan` 文件只在登记了精确路径/hash 且没有再被改动时作为本 run 的文档产物处理，不因自己的导出陷入“永远脏”。不得忽略整个 docs 目录或用户既有修改。
- 首版不做自动脏快照；如后续支持，必须显式清单包含 index/未暂存/授权的新文件，并保证最终差异扣除用户原修改。

### 8.2 隔离工作树

基线提交 → 私有 integration worktree；每个 attempt 从指定 integrationRevision 新建 detached worktree。数据目录示例：

```text
~/.flamingo/workflows/
  workflow.db
  runs/<runId>/
    plan/                       # 各版本机器计划/审核/正文快照
    attempts/<attemptId>/       # 会话 JSONL、进程输出、结果、测试证据
    workspaces/                 # integration 及各 attempt worktree
    artifacts/                  # 带 hash 的 patch、manifest、验收、最终报告
```

工作树是真正独立的源代码文件；Git commonDir 仍共享。创建/移除 worktree 和更新私有 refs 由 workspaceManager 串行管理，worker 不负责 Git 集成。源码运行数据不放项目 docs，docs 只存人读文档。

宿主在私有工作树封装必要的内部提交/refs，供下游 checkout 和保留产物；这些不移动用户 HEAD、不修改用户 index、不推送远端，但确实写入共享 Git 元数据，启动开发时必须说明并授权。清理只针对登记归属本 run 的 worktree/ref，默认保留失败现场。

依赖环境复用已安装运行时；项目测试使用锁定版本的独立/经验证只读环境，测试缓存/数据库/临时目录/端口按 attempt 隔离。不能共享可写 `.venv` 并让多个 coder 同时 uv sync；依赖变更由单一任务修改 lock 并重新准备环境。

### 8.3 产物和串行集成

1. writer 退出后由宿主扫描真实变更，包含新增/删除/重命名/二进制/可执行位，不能只读默认不含 untracked 的 git diff。
2. 全部变更必须落在审核的 writePaths；路径穿越、未授权敏感文件、越界符号链接、额外 generated 文件都进入阻塞，不偷偷丢掉。
3. 在受管独立 index/工作树生成完整 patch + fileManifest + hash；只处理已验证归属的文件，不碰用户原 index。
4. 对当前 integrationRevision 校验/应用候选。并发产物不等于可以无条件 cherry-pick；冲突停止，不用 ours/theirs 自动裁决。
5. 每次集成写 durable intent（beforeRevision/patchHash/expectedAfterTreeHash），Git 操作完成并验证后再提交状态/事件。失败的候选只污染临时集成候选树，不移动 accepted integration ref。
6. 最终测试通过后更新 accepted revision；依赖仅看已接受的 integrated 产物。若 Git 已生效、DB 未落盘，恢复用 before/after/hash 对账，不盲重放。

验收基于冻结的最终集成树。测试在该树的独立验证 worktree 上运行；测试若改变被跟踪源码，证据失效并阻塞，不能让 acceptor 顺手修复再声称验收通过。

### 8.4 写回原工作区不是默认结尾

默认交付 private ref、完整 patch、所需基线与报告。用户确认后才增加后续 publish 操作；它必须重新比较目标 HEAD/index/workingTree 指纹，只发布已经验收的同一差异。目标已变、patch 冲突、审批版本不符就暂停。首版无需自动发布工具，不做自动 push/PR/部署。

## 9. 持久化、恢复、事件和成本

### 9.1 最小 Store

使用标准库 SQLite，不另搭数据库服务。最小表：runs、tasks、attempts、approvals、inputRequests、events、usageFacts（兼作投递 outbox）。计划/patch/测试日志为不可变文件，DB 保存引用/hash；SQLite 状态迁移、审批消费、event seq 以及 usageFacts 插入使用事务。

- runId/taskId/attemptId 分开；每次重试、审核、返修都是新 attempt。
- `stateVersion` 乐观检查；启动请求 `requestId` 幂等且校验请求内容，重复 approve/cancel 不产生新动作。
- run 内只有宿主写 DB；worker 仅发事件/候选结果。不能让模型直接用 write 编辑运行状态。
- 一个宿主进程用 OS 文件锁占有 workflow DB；服务多个 run。repo 的集成锁按 commonGitDir，不按 workDir，以免 linked worktree 绕过。
- 磁盘满、状态提交失败进入 fail-closed，不继续派发/发布；关键输出先临时文件+fsync/原子发布再关联到状态，孤立文件可留待人工核验。

### 9.2 首版恢复承诺

**恢复状态与证据，不承诺任意代码副作用 exactly-once，也不透明续跑原 worker。**

重启时：加载 run 和租约 → 将遗留 running/cancelling 标 interrupted → 核验进程身份、登记工作树、integration intent、结果 hash → 对已证实完成的产物入账，对不确定操作暂停 → 用户确认后从安全 checkpoint 创建新 attempt。

不复用旧 confirmationId，不因 PID 存在就信任它（有 PID 重用问题），不把旧 cancelled 结果复活。审核/验收通过只在其绑定的 plan/source/tree/test hashes 不变时保留。所有无法证明安全的写操作都不自动重放。

主 agent 总结是单独可重试的受管阶段：验收已通过但模型总结失败时，保留 acceptance，通过幂等 reportId 重试新 coordinator attempt 即可，不重跑开发；报告落盘后再 completed。Web 按 runId/reportId 渲染关联报告，不向聊天 JSONL 重复插入总结。

### 9.3 Workflow 事件不复用聊天终态

封套：`runId, seq, phase, taskId?, attemptId?, kind, payload`，kind 如 phaseChanged/taskStarted/taskBlocked/taskIntegrated/reviewFinished/usageUpdated/runCompleted。

使用持久的状态快照+afterSeq 事件增量；同一水位下返回 snapshot。重复 seq 忽略，旧游标失效则重新取快照。生命周期事件持久化，token delta 可仅保留有限实时预览；慢订阅者断开重连，不允许无界队列拖住执行。

浏览器断连不取消 run。cancelRequested 立即可见，之后显示 cancelling，真实退出再 cancelled。worker 的 completed 只改变该 task，不结束主 run/SSE。

### 9.4 用量与日志

- 各模型步骤记录稳定键 `(runId, attemptId, modelStep)`、实际 provider/model、prompt/cached/completion tokens；SSE 回放不计费。
- workflowStore 保存事实，Web 通过幂等 usageKey 写 usage.db；成功投递后标 outbox，崩溃重复投递由唯一键去重。不要让子进程共享 Web 模块级 SQLite connection。
- 主 coordinator 节点也由 workflow 记步骤事实，不能再由普通 streamPump 给同一轮重复记账。Web 接线时只有一个账单写入路径。
- 主模型 contextTokens 与所有 worker 总 token 分开展示；父级汇总只查询，不再插一条总量账。中断缺失 usage 标 unknown，失败任务已发生的用量仍计入。
- 费用是基于记录时可得价格的估计；费用快照和缺失价格显式标记，不因模型删除变成零成本。
- 配置凭据和敏感 headers 不进日志；项目代码/结果日志仍视为敏感，本机目录按用户权限保护，Web 只通过登记 artifactId 读取，不开放任意路径。

## 10. Python / Web 集成入口（目标接口，尚未实现）

纯库只需几个明确操作：`start(requirement, coordinatorBinding, workDir, options, requestId)`、`getRun(runId)`、`approve(runId, stateVersion, planHash)`、`respond(runId, inputRequestId, stateVersion, decision, responseId)`、`cancel(runId)`、`resume(runId, stateVersion)`、`events(runId, afterSeq)`。返回结构化对象/事件，不依赖 FastAPI。

CLI 模块 `flamingoAgents.workflows.workflowCli` 提供 start/status/resume；在已有 uv 环境运行。首版 start/resume 宿主保持前台，approve/cancel 是该宿主内的交互操作（Ctrl-C 也触发取消）；status 仅只读。无交互时遇人工关持久化 awaitingApproval 后退出，之后用 resume 显式接续。不新增 CLI IPC/后台服务，也不允许另一个 CLI 进程直接并发驱动同一 run。

Web 由 workflowManager 共用同一 engine 与 store：

- `POST /workflows`：parentSessionId、requirement、options、requestId；返回 202 + runId。
- `GET /workflows/{runId}`：snapshot + seq。
- `POST /workflows/{runId}/events`：afterSeq，沿用鉴权 fetch 流传输习惯，独立 workflow DTO。
- `POST .../approve`、`.../respond`、`.../cancel`、`.../resume`：显式版本/审批或输入请求对象；respond 的固定类型和效力见5.4节，冲突 409。
- `GET .../artifacts/{artifactId}`：只读受管产物。

最小 UI 是主会话的一张 workflow 卡片/侧面板：当前阶段、DAG 列表、任务状态、审核轮次、待批项、测试证据、日志链接、用量、停止按钮。worker 不进入普通 sessions 侧栏，以免产生大量垃圾会话；待审批的主 session 也不能被删除。

Web 与 CLI 不能同时各自启动一个独立宿主操作同一 DB；有宿主锁时新的独立宿主启动失败，或仅做只读状态。后续再做正式跨进程控制服务；首版不会靠无限轮询代替完整服务端架构。

## 11. 不变量与成功标准

1. 未有当前版本计划的独立审核通过，coder 启动次数必须是 0。
2. 所有审核/验收 attempt 与作者/实现者 session 都不同；复审也新建。
3. 依赖未 integrated，不启动下游；同一波任务基线一致；writePaths 冲突不并行。
4. 一个 worker 出错不被包装成完成；只有自然语言“完成”没有合法结构化结果，必须失败。
5. writer 未确认退出，不能集成/清理其工作区；取消后不接受迟到成功。
6. 用户原工作树/index/HEAD 不因私有开发与验收改变；只允许已说明的 docs 导出及私有 Git 元数据。
7. 所有必需测试在明确版本上实际通过，且全新 acceptor 覆盖全部需求后，才能 reporting/completed。
8. 修改 plan/源基线/验收树使相应旧批准失效；失败重试不会重复 Git 副作用或记账。
9. 老的普通对话、模型、确认、停止、图片、history/SSE 行为有回归测试；workflow 默认不启用。
10. 测试基于 fake adapter/process + 临时 Git 仓库 + 隔离 HOME/配置，默认禁止网络，不依赖真实模型账户。

逐文件任务、依赖顺序、详细 TODO、故障注入与验证见配套实施计划。

## 12. 本次调研执行计划与工作区保护

已完成：实际代码链梳理；核心/Web 两方向独立子代理只读调研；旧方案比对；官方资料对照；初始205项测试基线与三个无模型本地probe；完整方案独立审核提出5项问题→修订v1.1→全新子代理完整读取两份文档后明确approve。未回传final或读取不完整的调用均未计为通过。

结束前针对正在更新的工作区，使用临时HOME运行 `uv run --no-sync pytest -q`：**209 passed in 3.98s**。这验证的是现有项目当时截面，不是尚未实施workflow的验收。v1.2仅补此状态/事实；详细审核与已审文档hash见审核记录。下一步应由用户确认T0决策，再开始M0。

初始已有未跟踪：`docs/plan/260908_configHomePlan.md`、`flamingoAgents/utils/configPaths.py`。调研期间配置迁移继续改变 builder/modelConfig/toolConfig/sdkEntry/Web 等多个文件，并新增配置测试；均不是本次工具编辑，不覆盖或回滚。第2节为原调研截面的定位，不代表迁移后的行号/默认路径保持不变。实施前必须重新读取最新状态和配置契约，不把本次截面当作原子冻结快照。

本次新增/修改仅 `docs/plan/260909_workflowPlan.md`、`260909_workflowTasks.md`、`260909_workflowReview.md`，不提交 Git，不清理既有内容。
