# Workflow / Subagents 实施 TODO 与验证计划

> Author: wilbur  
> Version: 1.2（方案复审通过；实施项未执行）  
> Date: 2026-09-09  
> Description: 配套 workflowPlan 的文件级任务、依赖和测试验收。v1.1 补 coordinator/暂停决策/集成返修并分离T12开发收尾和T13独立验收；v1.2仅更新复审/测试状态，无任务设计变更，实施项尚未执行。

主方案：[260909_workflowPlan.md](260909_workflowPlan.md)；调研/审核：[260909_workflowReview.md](260909_workflowReview.md)。

## 1. 交付分层与不做事项

- **M0：可靠子任务基础**：真实终态、管道排空、受控进程退出、结构化结果、显式配置/工具/预算。完成后可安全验证 subagent 执行，不宣称已有完整 workflow。
- **M1：纯库 + CLI 完整固定开发闭环**：审核修订、隔离并发、串行集成、全新验收、状态恢复、最终报告。先在临时 fixture 仓库用假模型完成端到端。
- **M2：Web 实用接线**：主 session 租约、独立 workflow API/事件、面板、幂等用量、断连/停止/重启测试。
- **M3：可选后续，不计入首版**：自动发布到用户工作区、脏快照、恢复原 worker 的交互确认、泛用批量派发工具、多模板/可视化 DAG、分布式部署、严格 OS 沙箱。

首版不通过减少隔离/终态/验收测试来“快速支持多 coder”。若必须更早上线，仅上线 M0 的只读独立子任务。

## 2. 拟新增模块（最小职责）

```text
flamingoAgents/
  subagents/
    __init__.py
    subagentTypes.py          # binding/request/outcome/role 结构和显式校验
    subagentRunner.py         # 进程、NDJSON、输出、预算、取消、终态
    workerEntry.py            # 包内子进程入口；调用现有 createAgent
    roleTools.py              # scoped 工具、search/runTests/submitResult
  workflows/
    __init__.py
    workflowTypes.py          # run/task/plan/review/acceptance 契约与转移校验
    workflowStore.py          # SQLite 事务、事件 seq、审批、用量事实
    workspaceManager.py       # repo 基线、worktree、manifest、串行集成
    workflowRunner.py         # 固定流程 + 就绪波次；不做泛用图平台
    workflowPrompts.py        # 角色输入组装；不解析/推断 Markdown TODO
    workflowCli.py            # 前台 start/resume/status 与同宿主审批/取消
    prompts/                 # 必要的固定角色 Markdown，随包发布
webApp/backend/
  workflowManager.py         # 宿主、session 租约接线、workflow DTO
webApp/frontend/js/
  workflowView.js             # 主会话 workflow 面板
```

只有真实逻辑增长到职责过重时才拆出独立 scheduler/eventBus/budgetManager；不要为了“可扩展”先建十几层接口。已有核心 agent/tools/adapter 尽量原样复用。主方案的事件、状态、权限是契约，不要求每个概念一个文件。

## 3. 编码约束（所有实现子代理必须收到）

- [ ] 每个任务开始前重读关联源码、确认自己的 writePaths，列明假设和成功标准。
- [ ] 仅改分配文件；跨范围问题返回主 agent，不能私自接线共享文件。
- [ ] 新代码文件和符号使用小驼峰（保留 Python `__init__`/入口协议等必需约定）。
- [ ] 新代码文件有 Author: wilbur、Version: 1.0、当前 Date 和准确 Description 文件头；现有代码每次变更升小版本并说明。
- [ ] uv 管理 Python 环境；已有 >=3.12 约束保持，若需要升级先询问用户。
- [ ] pytest 严格测试；用 monkeypatch/fake adapter、临时目录/仓库，禁止默认使用真实模型和凭据。
- [ ] 保留预先存在的无关代码/格式/未提交变更，只清理由自身修改产生的未使用内容。
- [ ] 完成时提交结构化结果：变更摘要、实际测试、产物、未决问题，不能仅回复“已完成”。

## 4. 详细任务列表

### T0｜冻结契约、基线与配置依赖

**依赖：无。角色：主 agent/架构。写范围：本次计划和测试 fixture 规范；不与配置迁移任务抢写。**

- [ ] 用户确认默认模型继承、maxParallel、计划后人工审批、脏工作区/私有交付策略。
- [ ] 重取 git status/HEAD/文件指纹；确认配置目录迁移实际完成到哪一步，与其负责人确定唯一 resolver 接口。
- [ ] 批准 subagentRequest/outcome、workflowPlan/task/review/acceptance、事件、取消、artifact、usageKey 的字段契约。
- [ ] 明确逻辑 coordinator 的所有节点均为受管新 attempt/session，只有 scoped read/search/submitResult；冻结权限/预算/取消/用量绑定，不复用 Web 全权限 agent 或重复记账。
- [ ] 冻结 inputRequest/respond 类型化入口及决策生效规则；澄清/权限/预算/基线/集成失败/对账都不能依赖被禁止的普通聊天。
- [ ] 冻结最小 fake adapter、假进程与临时 Git fixture 接口，测试禁网络/隔离 HOME。
- [ ] 把共享文件 owner 和 exports 集成任务写清楚；后续需要改契约必须同步审查并重跑相关测试。

**验证：** 用成功、审核拒绝、任务失败、取消、集成中崩溃五条轨迹手工走查，任何未定义转移阻塞开发。配置来源契约未稳定不能进入 T4/T9 的生产接线。

### T1｜修复现有 SDK/askSubAgent 基础语义

**依赖：T0。写范围：`sdkEntry.py`、`tools/builtinTools.py`、必要的 `config/tools.yaml`、`tests/testSdkOutcome.py`、`tests/testSubagentPipes.py`。**

- [ ] 先写复现：progress→error、空流、仅 completed.message、坏 JSON、非对象 JSON、缺 reply/error 字段、非零退出。
- [ ] runSdk 成功使用 completed.message，失败显式异常，CLI JSON error 与退出码一致；正常返回 str 的 SDK 使用方式保持。
- [ ] askSubAgent 严格校验 payload；失败携带有限诊断，不出现成功的 `None`。
- [ ] askSubAgent 工具列表硬限制在 read/write/edit/bash，拒绝嵌套派发；不把该限制宣传成 shell 安全隔离。
- [ ] 修复 `_runWithInterrupt` stdout/stderr 背压：执行中持续排空或带短 timeout 的 communicate；停止/超时后关闭管道、回收进程。
- [ ] 明确普通 chat 旧 bash 模式与新 workflow 进程组模式分离，不能把共享 helper 修改成杀调用方自己的组。
- [ ] 配对测试成功、权限拒绝、超时、中断、unicode、超长输出；禁止真实模型请求。

**验证：** 本次三个 probe 均有 pytest 回归；2MiB stdout 和 stderr 不再假超时；现有 suite 通过。长日志在旧 API 仍有边界，可靠有界流协议由 T3 实现，不宣称 T1 已完成 workflow runner。

### T2｜结构化类型与持久状态

**依赖：T0。写范围：`subagentTypes.py`、`workflowTypes.py`、`workflowStore.py`，各自测试。**

- [ ] dataclass + 显式校验最小契约；不新增大型 schema 框架，不依赖现有不完整 enum/maxItems 支持。
- [ ] 校验 DAG 唯一 ID、依赖/环、requirementIds、writePaths、测试命令、枚举/预算范围。
- [ ] 实现 planHash/sourceHash、版本审批、结构化 issue/criteria 引用；人读 Markdown 由该结构生成。
- [ ] SQLite 初始化/小版本迁移；run/task/attempt/approval/inputRequest/event/usageFact 和必要唯一索引。
- [ ] inputRequestId/responseId 幂等、类型化决策、参数/基线hash、版本检查；决策消费与状态/event同事务。
- [ ] 状态转移 + event seq + 审批消费同事务；expected stateVersion 冲突失败，不部分落状态。
- [ ] 关键 artifact 临时写入、hash、原子发布后关联 DB；定义孤立产物对账方式。
- [ ] 幂等 requestId 同内容返回原 run、异内容拒绝；重复终态/审批/cancel 不重复迁移。
- [ ] 非终态恢复 interrupted、过期批准失效、usageKey 去重/outbox 状态。
- [ ] 宿主 OS 文件锁，工作线程使用清晰的连接生命周期；模型/worker 不直接写 DB。

**验证：** 参数化非法契约、事务 rollback/磁盘写失败、并发 stateVersion、幂等请求、重启加载、坏/缺 artifact hash、schema 版本不支持全部有测试。

### T3｜受管 Subagent 进程与可信终态

**依赖：T1、T2。写范围：`subagentRunner.py`、`workerEntry.py`、`tests/testSubagentRunner.py`。**

- [ ] 使用包内 `python -m` 入口，stdin 请求/NDJSON stdout；不依赖仓库根 sdkEntry 路径。
- [ ] 每 attempt 独立进程/agent/adapter/session/日志，固定运行绑定；与 T4 工具构造接口采用 T0 契约。
- [ ] 同时持续消费 stdout/stderr，限制单条记录和内存预览，完整 artifact 不走输出截断通道。
- [ ] 精确处理 completed/error/blocked/timedOut/cancelled/interrupted；子进程退出码、终态、候选 result 三重核验。
- [ ] coordinator/reviewer/coder/acceptor 全部走受管入口；研究/修订/总结也有步数、deadline、取消、usage与独立attempt，不留未治理模型节点。
- [ ] 记录真实启动时刻、步数、deadline、usage step；没有 usage 的中断不计成零成本。
- [ ] 全局槽位派发前认领；排队不占执行线程；不为每个工作目录建独立失控线程池。
- [ ] 进程组 owner/运行身份登记、TERM/KILL、wait/reap、取消迟到消息屏蔽；先 cancelRequested 后确认 cancelled。
- [ ] 与 T4 约定 workflow 命令继承 worker 组；命令超时升级 attempt 终止，不继续复用不确定工作树。
- [ ] 父宿主退出/未知遗留 writer 进入 reconciliation；无法确认终止不返回可集成成功。
- [ ] 所有资源退出路径关闭句柄/线程/生成器，失败日志保留；Windows 未实现明确拒绝。

**验证：** 假入口按脚本化事件输出；输出饱和/坏协议/缺终态/父停止/嵌套 sleep/迟到成功/组内遗留进程均覆盖；进程退出后必须可核验没有普通受管 writer 继续改文件。

### T4｜角色工具、builder 与配置绑定

**依赖：T0、T2；与 T3 可并行，最终联合验证后可用。写范围：`roleTools.py`、`builder.py`、必要的工具装配接口及专属测试。**

- [ ] 最小 keyword-only builder 扩展：resolved model binding、显式工具定义、maxModelSteps；定义与旧路径/toolNames 的互斥或优先级，参数冲突 fail-fast。
- [ ] 复用模型 API/auth 校验；显式配置路径与迁移 resolver 对齐；默认参数缺省时普通 createAgent 行为不变。
- [ ] 流程上下文通过工具工厂闭包传递 run/attempt/role/作用域，不为 lineage 无差别修改 core/types。
- [ ] scoped read/search/write/edit 归一化路径并检查 realpath；coordinator/reviewer/acceptor 物理没有源码写工具和通用 bash；coordinator文档经submitResult由宿主导出。
- [ ] coder 的命令工具与 worker 组一致；输出有界、工作目录可核验，不声称任意 shell 是强沙箱。
- [ ] runTests 接受批准的 testId，解析为固定 argv/cwd/env/timeout，不能由模型临时改成别的命令。
- [ ] submitResult 按角色校验候选，不直接让 child 改 run 状态；最终接受结果仍由父宿主决定。
- [ ] 叶角色硬禁原生递归工具；skills 默认关闭/白名单；公共风格与流程责任分离。
- [ ] 工具确认映射 blocked + toolApproval inputRequest：持久参数 hash/基线/有效期；respond批准后宿主受控动作ledger执行/对账再新建attempt，禁止自动批准、旧confirmationId假恢复及换命令绕过。
- [ ] 凭据/secret header 不入配置快照、日志、argv 或事件；运行时 auth 刷新仍走既有解析器。

**验证：** 默认模型继承实际 binding 而非 YAML 第一项；热变更不漂移；显式覆盖记录；role 工具白名单、越界路径/符号链接、参数伪造、secret redaction、step limit、与 T3 超时取消联合测试。

### T5｜工作区与串行集成

**依赖：T0、T2（基线设计可与 T2 并行，接线等契约完成）。写范围：`workspaceManager.py`、`tests/testWorkflowWorkspace.py`。**

- [ ] preflight 查 repo/commonGitDir/HEAD/index/workingTree；拒绝未支持仓库状态。
- [ ] 脏工作区暂停与明确 HEAD 选择；不 stash/commit/reset/clean 用户内容；基线变化使相关计划失效。
- [ ] 仅豁免精确登记/hash 不变的本 run docs 导出，不能忽略全部 docs 或所有 untracked。
- [ ] 创建 integration 与 per-attempt detached worktree，登记归属、私有 ref、外部锁和日志位置。
- [ ] 准备隔离测试缓存/环境/端口；不能多个 agent 并发修改 uv.lock 或同一个 .venv。
- [ ] writer 真实退出后收集新增、删除、rename、binary、mode 变化；检查 writePaths 和敏感/越界文件。
- [ ] 完整 patch/manifest/hash 生成与验证，不遗漏 untracked，不从截断 stdout 回收产物。
- [ ] 固定顺序在候选集成树应用，冲突或测试失败进入 task=integrationBlocked/run=paused并保留证据；测试通过才前移 accepted integration ref。
- [ ] integrationResolution 后从最新 acceptedRevision 发新attempt；其它候选跨基线必须重新应用校验/测试，不能盲重放或沿用旧验证。
- [ ] 集成 intent 与 before/expectedAfter/hash 对账；崩溃后不重复 apply，候选失败不污染 accepted ref。
- [ ] 最终验证 worktree 冻结对应版本；测试修改源码则使验收证据失效。
- [ ] 输出私有 ref/patch/report，不实现自动发布；清理必须确认归属与退出，失败现场默认保留。

**验证：** 全部用临时 Git 仓库，逐项测试原 index/HEAD/用户文件不变、自己文档不死锁、冲突、binary/mode/new file、路径穿越、双 linked worktree 同 commonDir 锁、Git 生效/DB 失败恢复、基线变化、清理失败。

### T6｜固定状态机和 DAG 波次调度

**依赖：T2、T3、T4、T5；开发可先使用 fake runner/workspace。写范围：`workflowRunner.py`、`tests/testWorkflowRunner.py`。**

- [ ] 实现 preflight/research/plan/review/revise/approve/develop/accept/repair/report 固定节点，禁止任意跨阶段。
- [ ] 唯一驱动者更新状态，模型只提交候选数据。
- [ ] 审核通过条件、版本检查与可选人工审批；revisingPlan只能回到新reviewingPlan。新hash未复审通过时人工approve无效、coder启动次数为零。
- [ ] respond消费类型化暂停决策；resume仅继续已解决请求/完成对账的checkpoint，不能默认同意澄清/权限/预算。
- [ ] 就绪集合 = 依赖全部 integrated；同波相同 baseRevision，writePaths/exclusiveResources 无冲突。
- [ ] 实际启动/退出统计与最大并发；波次结束后固定顺序验证集成，再开放下游。
- [ ] task completed 与 verified/integrated 分开；failed/blocked 导致依赖暂停，不能“其余成功所以全部完成”。
- [ ] integrationBlocked正常失败回路与崩溃对账分开；收受决策后受影响任务新attempt，扩大范围回计划审核，旧未集成候选必须重验。
- [ ] 取消停止新派发、处理本波全部 attempts，禁止迟到结果修改 accepted 状态。
- [ ] 审修/返修轮次和 run 时间预算；用完进入 paused，而不是 approve/completed。
- [ ] 范围改变回计划关，原批准失效；同范围返修新 attempt，最终全部测试/独立验收重做。
- [ ] 重启先 reconcile，不自动重放未知写操作；报告阶段可独立幂等重试。

**验证：** 用事件顺序/同步屏障断言两 coder 的执行区间重叠和容量上限，不仅靠容易抖动的墙钟耗时；非法状态转移、下游提前启动、失败分支、轮次耗尽、cancel race、迟到成功、计划过期和总结失败皆覆盖。

### T7｜规划/审核/验收输入、产物与报告

**依赖：T2、T4；可与 T6 并行。写范围：`workflowPrompts.py`、`workflows/prompts/*.md`、`tests/testWorkflowArtifacts.py`。**

- [ ] 主 agent 各受管节点的输入与代码证据要求；不确定项生成 clarification inputRequest，respond正文形成新的需求版本。
- [ ] 单一结构化计划生成人读 docs/TODO，日期前缀来自启动日期且运行内固定。
- [ ] 计划审查覆盖需求/架构/接口/文件/并行/测试/失败恢复，问题关联稳定 issueId。
- [ ] 每轮 reviewer 和 acceptor 新 session；验收者不复用 plan reviewer/coder 会话。
- [ ] 修订 diff、旧问题状态、新版本 hash 传给新 reviewer；不将旧结论当可信指令。
- [ ] requirementId→taskId→testId→acceptanceItem 可追溯；缺映射不通过。
- [ ] 独立验收在冻结验证树使用 runTests，模型 verdict 不覆盖真实退出码。
- [ ] finalReport 引用固定产物、版本、实际测试与风险，不暴露凭据或整段内部推理。
- [ ] 长结果用登记 artifact 引用与有限摘要，不能静默截断关键约束；docs 手改需要导入新版本并重新审核。

**验证：** golden/结构检查覆盖 Markdown 与机器 DAG 一致、过期报告拒绝、全新 session 不变量、缺需求覆盖、伪造测试结果、修订产生新问题、末尾风格标记与 submitResult 协议共存。

### T8｜CLI 闭环与打包可达性

**依赖：T3–T7。写范围：`workflowCli.py`、两个新包 `__init__.py`（集中 owner）、CLI 测试。**

- [ ] 提供 start/status/resume；start/resume 前台运行，同一宿主里交互 approve/respond/cancel（或 Ctrl-C），不新增跨进程服务。
- [ ] 按 inputRequest 类型收集澄清、权限、预算、基线、集成处理与对账说明；resume加载请求后进入交互，不在请求未解决时启动模型/工具。
- [ ] 无交互模式遇人工关停在 awaitingApproval，退出后由显式 resume 接续，不自动批准。
- [ ] 接受显式 provider/model/config 路径；已有代码创建的 coordinator binding 可直接注入纯库。
- [ ] 宿主锁防 CLI 与 Web 同时驱动；status 只读，未持锁不能改运行状态。
- [ ] installed package 中可以启动 workerEntry；离开仓库 cwd 仍能定位包内角色模板。
- [ ] CLI 退出按真实执行状态返回可区分退出码；保留 interrupted 证据，不假报 completed。

**验证：** subprocess CLI + fake model fixture 跑研究→审核 reject→修订→approve→两个并发 coder→集成→accept reject→返修→新 accept approve→主报告。覆盖任意 cwd、包安装、Ctrl-C、断点恢复、锁冲突。

### T9｜Web 主会话 admission 与模型/用量基础接线

**依赖：T0、T2；生产启用还需 T6。写范围：`agentManager.py`、`server.py` 中已有 chat/confirm/model/delete admission 接口、对应专属回归测试。**

- [ ] 一个 admission gate 覆盖 chat/confirm/startWorkflow/model/delete，先认领再修改索引；workflow 等待审批也占用 session 租约。
- [ ] 认领/释放带 owner/generation，旧泵不能注销新 owner；managerLock 内不等待 worker/SQLite 长 I/O。
- [ ] workflow coordinator各节点由T3受管独立会话执行，原Web session只持操作租约、不在多个child中共同续写JSONL；最终报告按runId/reportId关联显示。
- [ ] 下次普通聊天按登记runId加入已完成报告摘要/证据引用，保留原始需求，不把内部worker对话复制进去。
- [ ] 活跃 workflow 中模型切换无副作用拒绝；冻结实际 adapter binding，不依赖索引里的“期望模型”。
- [ ] workflow全部主/子步骤通过自身usageFacts路径计费，禁止普通pump为相同步骤再记账。普通聊天冷恢复累计基线风险另案，不在此无关扩修。
- [ ] requestStop 的前台封流不能释放 workflow writer 的资源所有权；普通聊天 stop 体验保留。
- [ ] 对接配置迁移后的唯一 resolver；不改写既有未完成迁移文件抢占任务。

**验证：** chat/confirm/model/delete 与 run 并发、双启动、待批 session 删除、旧 owner finally、workflow主节点权限/冻结模型/步骤计费、早 stop/真实退出差异、报告关联后继续普通聊天；普通聊天现有回归不退化。

### T10｜Web 用量与 workflow DTO/API

**依赖：T2、T6、T9。写范围：`workflowManager.py`、`usageStore.py`、`server.py` 中新增 workflow 路由和测试。与 T9 不同波编辑 server.py。**

- [ ] 生命周期启动 store/锁/reconcile，退出停接新 run 并收敛活跃子进程。
- [ ] 主方案的 start/get/events/approve/respond/cancel/resume/artifact API，保留现有 Bearer 认证；所有固定inputRequest类型端到端可回应。
- [ ] workflow 专用 DTO，不经普通聊天 sseCodec 的 terminalEventTypes 处理。
- [ ] snapshot+afterSeq，一致水位、重复/过期游标、慢消费者有界处理，断连不取消。
- [ ] usageFacts outbox→usage.db 幂等 usageKey；保持旧 usage 行和普通聊天写入兼容。
- [ ] coordinator 与 child 的 actual model 用量分别归属，父级求和不再插总账；contextTokens 独立。
- [ ] workflow 失败/未知用量/缺价格如实展示；重启补投递不重复记账。
- [ ] 按 artifactId 查允许文件，不接收浏览器任意绝对路径；日志和配置脱敏。

**验证：** FastAPI 测试带假 runner：鉴权、requestId/responseId幂等、409、重复approve/respond、陈旧planHash/参数hash、未解决输入时resume拒绝执行、worker terminal不终结run、重连乱序/断档、计费重复投递/DB失败补偿、路径穿越、shutdown。

### T11｜Web 最小 Workflow 面板

**依赖：T0 接口约定即可用 fake API 开发；生产接线依赖 T10。写范围：`workflowView.js`、必要的 api/main/chatView/index.html/styles.css，前端测试。**

- [ ] 主会话“启动开发流程”入口及 options 预览，不默认把所有普通聊天变成 workflow。
- [ ] 阶段/TODO 状态、审核轮次、并发任务、需求验收、artifact/日志、用量展示。
- [ ] 明确区分 awaitingApproval/paused/cancelling/cancelled/failed/completed；stop 点击后不虚报所有 worker 已退。
- [ ] 待审批呈现计划版本、修改范围、私有 worktree/Git 元数据行为；批准绑定当前版本。
- [ ] inputRequest卡片支持澄清文本、明确权限/预算批准、基线选择、集成失败处理和对账说明，调用respond；不要求用户发送被admission禁止的普通消息。
- [ ] 连接/runId/seq 守卫，迟到事件不改别的会话；snapshot 回放幂等。
- [ ] worker 不加入普通 session 侧栏；切会话/断网后可恢复面板。
- [ ] 浏览器渲染复用现有 Markdown 安全净化，不直接插入子代理 HTML。

**验证：** 延用项目现有前端测试方案（pytest 驱动 Node/已有 DOM stub）；多窗口 attach、切会话迟到事件、断连/刷新、审批过期、cancel状态、worker completed、脚本注入有自动测试。

### T12｜全链路回归与文档开发收尾（有写权限）

**依赖：T8–T11。写范围：新增端到端测试、README/接口文档；这是开发集成任务，不是最终acceptor。**

- [ ] 运行全部 pytest/前端测试；本次初始205项、配置迁移后隔离HOME复跑209项通过，实施时重新建立基线，不能只核对总数。
- [ ] 临时仓库验证原工作区/index/HEAD 不变及 worktree/私有 ref 产物可用。
- [ ] 首次审核失败、第一次验收失败的完整闭环，验证每轮 session 都新建。
- [ ] 在启动前/模型中/工具中/审核中/集成中/总结中/计费后插入取消和进程崩溃故障。
- [ ] 本任务所有测试/文档修改完成后重新集成、运行必需回归并冻结最终源码树；随后才能启动T13。
- [ ] 更新 README 的已实现项只勾选真实交付范围，记录首版恢复/权限/平台/发布限制。
- [ ] 写迁移/启停/恢复/排障说明；禁止新增功能 import 时悄悄初始化真实 HOME。
- [ ] 只有用户明确授权才做小规模真实模型冒烟，限定需求/预算/临时仓库；默认测试全部不计费。
- [ ] 交付冻结的源码/测试manifest/hash以及所有证据给T13，不能由T12作者自验后替代独立验收。

**验证：** 测试/文档全部已集成，必需测试通过并绑定最终版本；暂不宣布独立验收完成。

### T13｜全新只读验收与主 agent 报告（无源码写权限）

**依赖：T12已完成并冻结版本。角色：全新acceptor；不能是T12或此前coder/reviewer的session。写范围：无；报告经submitResult由宿主持久化，不改变验收源码树。**

- [ ] 全新acceptor读取原需求/计划、最终集成差异与证据，对每个requirementId逐项独立验收。
- [ ] 通过runTests在冻结版本的验证worktree复跑必需命令；不提供write/edit/普通bash。
- [ ] 校验工具/进程实际终态、测试记录和版本hash，不只信实现者报告；全部不变量有测试或明确人工核验记录。
- [ ] 无未解决blocking问题后提交绑定版本的acceptance；如需任何源码/测试/README改动，返回相应开发任务，再次冻结并派另一个全新acceptor，不在验收会话修复。
- [ ] 主coordinator收到acceptance后生成最终报告（独立受管reporting节点），包括版本/测试/风险/私有交付位置；不代用户提交/推送。
- [ ] 用户未授权真实模型冒烟时，明确区分“假模型协议与端到端通过”与“真实模型任务效果尚未验证”，不得夸大。

**验证：** T12与T13身份/权限不同，最终报告版本与验收版本一致；验收后再改任意被验收源码使原通过失效。

## 5. 实施依赖与可并行分工

```text
T0
├─ T1 legacy 可靠性 ───────┐
├─ T2 类型/存储 ───────────┼─ T3 runner ───────┐
│          ├─ T4 角色/装配 ┘                 │
│          └─ T5 worktree ───────────────────┼─ T6 workflow ──┐
│                     T4 + T2 ── T7 产物 ───┘               ├─ T8 CLI
├─ T9 Web admission（等配置契约） ─── T6 + T2 ── T10 Web API ├─ T12 开发收尾 → T13 独立验收
└─ T11 UI（先 fake API） ─────────────────────── T10 接线 ────┘
```

推荐实际批次（每次最多两人，并行仅在文件所有权不冲突时）：

1. 主 agent 完成 T0。
2. worker A 做 T1，worker B 做 T2；T11 可按资源空闲在后续波次用 fake API 先做。
3. T3 与 T4 并行（共享契约已冻结，分别只写 runner 与 roleTools/builder）；之后 T5 与 T7 并行。
4. T6 与 T9 并行（库/后端分离），T9 必须跟配置迁移对齐。
5. T8 与 T10 并行；T11 独立完成或此后接线。
6. 开发agent完成T12的测试/文档修改并集成冻结；再派全新、无源码写权限的subagent执行T13独立验收，最后主agent汇总。

如果执行中需要改变共享接口，应暂停受影响任务、更新契约与审核，不能让两个 worker 互相猜测。T3/T4、T6/T7 的 fake 测试通过不代表联合接线已验收。

## 6. 测试总矩阵与退出标准

| 类别 | 必须验证 |
|---|---|
| 协议 | error/空流/非 JSON/截断/错误 ID/重复终态均不成功，completed.message 不丢 |
| 计划审核 | DAG合法、覆盖完整、新hash未复审不得人工放行、复审全新、轮次到顶暂停 |
| 暂停决策 | clarification/权限/预算/基线/集成/对账可回应，过期请求拒绝，未解决输入resume不执行 |
| 并发 | 真重叠、容量不超、依赖不提前、资源冲突不并发、不同基线不混用 |
| 隔离/集成 | 新文件不漏、二进制/mode、越界拒绝、冲突暂停、accepted ref 不污染、原工作区不变 |
| 生命周期 | 进程管道、普通孙进程退出、取消迟到结果、未知writer隔离、句柄/线程回收 |
| 持久化 | 状态/event事务、幂等启动/审批、Git已生效DB未记对账、磁盘满停派发 |
| 独立验收 | 作者与验收不同session、冻结代码/测试证据、需求逐项验证、返修后新验收 |
| Web | admission统一、子终态不终结主run、多窗口/重连/陈旧事件、停止真实语义 |
| 用量 | workflow新主/子节点只计一次、真实模型归属、outbox幂等、未知usage、主context不混worker总量 |
| 兼容性 | 普通chat/确认/模型/图片/history/SSE/旧SDK正常路径、不启用workflow行为不变 |

自动测试使用隔离 HOME、临时模型配置与假 credentials；不得直接读取本机真实 auth.json、sessions.json 或把用户当前仓库用作破坏性测试夹具。性能测试使用同步屏障/事件计数验证正确性，只在辅助指标使用墙钟。

## 7. 本次方案工作 TODO（与未来实施区分）

- [x] 定位项目/依赖/工作区变更，建立调研计划。
- [x] 核心与 Web 方向新子代理只读研究、计划初审。
- [x] 阅读真实源码/旧方案/官方框架资料并交叉核验。
- [x] 初始 uv run pytest -q：205 passed；三个本地无模型probe验证基础缺口；收尾隔离HOME复跑209 passed。
- [x] 完成主设计与详细实施 TODO 文档初稿。
- [x] 新子代理审核完整主方案 + 实施计划，落档5项问题。
- [x] 修订并用全新子代理完整复审，明确approve、无明显阻塞；不完整调用不计通过。
- [x] 检查文档一致性、链接、工作区保护，整理供用户确认的决策与结论。
