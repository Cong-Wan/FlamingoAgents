# Workflow 方案调研证据与独立审核记录

> Author: wilbur  
> Version: 1.2  
> Date: 2026-09-09  
> Description: 记录调研、验证与独立审核。v1.1记录完整方案5项问题与修订；v1.2记录全新复审approve、已审hash及209项最新隔离测试。不完整调用不计通过，不是实现验收报告。

关联：[主方案](260909_workflowPlan.md) · [实施 TODO](260909_workflowTasks.md)

## 1. 范围与工作区保护

用户要求深入研究当前 `flamingoAgents/` 框架，并按“计划审修→依赖并发开发→全新子代理验收→主 agent 总结”设计详细方案。本次没有实施业务代码，也没有提交/推送/清理 Git。

初始工作区已有：

```text
?? docs/plan/260908_configHomePlan.md
?? flamingoAgents/utils/configPaths.py
```

调研期间配置迁移继续改变 builder/modelConfig/toolConfig/sdkEntry/Web 等多个文件，并新增 testConfigPaths.py；本次工具没有编辑它们，不覆盖、不回滚。只读调研不是原子源码快照；未来实施须重新建立基线。

本次子代理使用 `openaiCodex/gpt-6-astra`，依据是启动时该项目最近活动 session 元数据（仅查询 providerId/modelId/workDir/时间，未读取对话正文/密钥）。这是本次调用的环境线索，不是产品应采用的模型继承算法；正式 workflow 必须显式使用启动主 agent 的实际 binding，不能查询最近会话来猜。

## 2. 实际阅读与交叉核验

- `README.md`、`pyproject.toml`、`builder.py`。
- `core/agent.py`、`core/types.py`、`core/conversation.py`、工具执行/权限/schema 及 `sdkEntry.py`。
- `modelConfig.py` 与配置路径迁移边界，只读取非敏感模型标识；未输出模型配置密钥。
- Web `agentManager/server/sessionStore/sseCodec/usageStore` 等，由独立子代理只读研究，主 agent 重点复核停止、主会话互斥、用量与事件边界。
- 历史 `orchestratorWorkerPlan.md`、2026-08 两轮审核；明确是待实施设想，未作为代码事实。
- Anthropic、LangGraph、OpenAI Agents SDK 官方资料（主方案第3节有可追溯链接）；实际读取仅支撑对应模式/概念，不声称做了所有框架的全面跑分。

## 3. 实际测试与 probe

### 3.1 现有测试基线

命令：`uv run pytest -q`。

结果：`205 passed in 4.46s`。测试是在当时工作区截面运行，不代表尚未实施的 workflow 已被测试，也不代表后续外部修改被该次运行覆盖。

### 3.2 无模型本地 probe（主 agent 复核）

方法：`uv run python` 内存假 agent、unittest.mock 替换 createAgent/子进程返回；管道测试使用仅写 stdout 的本地 Python 短进程。没有新建项目代码文件，不调用模型 API、不编辑真实仓库源代码。

输出：

```text
sdk error event returns: 'ONLY_PROGRESS'
sdk completed message collected: ''
invalid child stdout: {'isError': False, 'content': 'None'}
pipe probe: {'interruptEvent': False, 'elapsed': 0.02, 'status': 'completed', 'bytes': 2097152}
pipe probe: {'interruptEvent': True, 'elapsed': 2.09, 'status': 'timedOut', 'bytes': 65536}
```

这证明当前文本协议不能作为任务完成关卡、PIPE 排空不能等到进程结束。未来 T1 必须补正式 pytest 回归，不把本次一次性 probe 当长期测试覆盖。

## 4. 初始调研计划独立审核

### 4.1 核心方向

第一位独立子代理只回传中途进度，没有收到最终审核结论；**不计为审核通过**。随后创建全新子代理做短审（最多6次工具），收到最终报告：

- 调研可继续，原研究骨架尚不是可实施规格。
- 补齐终态契约、审批重启语义、取消树、预算与验收证据。
- 确认串行工具、SDK 自动拒绝确认、新会话但共享默认 workDir。
- 以本地假模型/进程验证终态误判、管道饱和；静态指出嵌套进程组无法完整证明退出。
- 会话 pending 恢复不等于 workflow 任务恢复，需独立状态/副作用对账。

采纳：主方案2/5/7/9节及 T1–T7 覆盖；使用实际 binding 的规则已明确，不把最近会话元数据查询变成产品逻辑。

### 4.2 Web/持久化/工作区方向

另一全新子代理给出独立静态研究报告；未运行测试，不将其静态推演误标为已复现。

主要发现与方案处理：

| 发现 | 纳入位置 |
|---|---|
| 旧 dispatchTask 方案未落地，不能直接套线程池共享工作树 | 主方案2/3/6/8节 |
| 没有聊天泵不等于主 session 空闲；stop 封流不等于真实退出 | 4.1/7.6/9.3节；T9 |
| worker 终态会提前结束主聊天流 | 9.3/10节；T10/T11 |
| 冷恢复累计用量潜在重复、模型期望值与实际值可能脱节 | 2.2/7.4/9.4节；T9/T10 |
| worker 不应塞入平面 Web sessions 索引；恢复不是重放写操作 | 9/10节 |
| worktree 不包含脏工作区；独立工作树也不是安全沙箱 | 1.1/7.2/8节 |
| patch 漏新文件、并发依赖基线、集成中崩溃需单独处理 | 6/8/9节；T5 |
| 配置目录正在迁移，不能新增另一套默认查找路径 | 2.2/7.4节；T0/T4/T9 |

## 5. 完整方案独立审核：第一轮

首先创建全新子代理长审，两份文档均被读取，但调用只回传进度，未收到 final；该调用**不计通过**。随后再创建全新子代理，限定单次只读两份完整文档、简短输出，收到明确最终结论 **revise**。

| ID | 等级 | 审核发现 | v1.1 修订 |
|---|---|---|---|
| R1 | High | coordinator复用主会话时，权限/模型步数/deadline/取消未闭合 | 主方案4.1/6.3/7.2：逻辑主agent各节点也走统一受管进程，新attempt/session，只有read/search/submitResult；原Web session只持租约，报告独立关联显示；T0/T3/T4/T9对应补齐 |
| R2 | High | 普通聊天被禁止后，澄清/权限/追加预算没有可达输入通道 | 新增5.4：inputRequest/respond类型化决策，版本/hash/responseId约束；CLI/Web都有对应入口；T2/T4/T6/T8/T10/T11补契约与测试 |
| R3 | High | T12既写测试/README又被安排为最终独立验收，最终树和角色混淆 | T12明确开发收尾并冻结版本；新增T13由全新、无源码写权限的acceptor验收；之后再修改必须重新冻结/重新验收 |
| R4 | Medium | 已知集成冲突/测试失败缺返修转移，仅有验收拒绝回路 | 新增6.4 integrationBlocked→paused→显式处理→最新acceptedRevision新attempt，旧候选跨基线重验；T5/T6补测试 |
| R5 | Medium | 图中revisingPlan错误直达人工审批，与正文复审要求矛盾 | 5.1改回新reviewingPlan；T6增加新planHash未复审时人工批准/coder启动都不得生效的断言 |

另在R1收敛过程中减少无关修改：workflow不再复用普通聊天累计差值计费，旧冷恢复usage静态风险单独记录，不强行纳入本次实现范围。

## 6. v1.1 全新子代理复审：approve

第一次复审用一次bash cat两份文档，超过现有管道容量后超时，任务文档后半段未完整返回。该子代理明确给出证据不完整的revise，而不是发现新的架构阻塞；**未计通过**。这再次说明不能把工具有部分正文当作完整完成。

随后再创建全新子代理，工具仅read，分别完整读取主方案444行与任务计划332行（读取limit分别600/500），不再用bash合并输出，收到明确最终结论：

> approve。已完整读取两份v1.1文档，未发现明显阻塞或方案与任务的实质冲突。五项修订均有设计、实施任务和验证承接；DAG/文件所有权/产物/串行集成/冻结验收衔接；取消、崩溃对账、Web租约、幂等计费有对应任务。可作为实施计划，但实现尚未验收。开工先完成T0，最终仍需T12实际测试与T13独立验收。

审核模型：`openaiCodex/gpt-6-astra`；每次调用均新会话，未复用原方案作者或此前审核会话。

已审v1.1文档SHA-256（更新收尾元数据之前记录）：

```text
0a57cfc79707da7b4524efa8cc7dc442518525b1425346c1e36e50f2a6e36ba1  260909_workflowPlan.md
0cb613921d80ebd5637cbfa7a08d2abc5ea118af7107e49c31be1a678c62eada  260909_workflowTasks.md
```

最后v1.2仅修改文档状态、测试事实及本次完成勾选，没有更改上述已审设计或未来任务逻辑。

## 7. 收尾验证与边界

- 2026-09-09收尾：在临时HOME下子进程执行 `uv run --no-sync pytest -q`，**209 passed in 3.98s**；避免初始化/读取真实用户配置。新增4项来自同时进行的配置迁移测试，不是本次编写的workflow测试。
- 重新只读核验：builder v1.8/modelConfig/toolConfig/skills/sdkEntry/Web store已接统一 `~/.flamingo/config` 常量；旧截面来源分歧已在主方案加更新说明。SDK终态等核心缺口仍在，不将配置迁移误称为workflow实现。
- 三份文档本地相对链接有效、代码围栏成对；实施任务T0–T13具备明确分工、依赖与验证。
- 本次编辑仅三个260909_workflow Markdown；未编辑并行配置迁移源码，未提交/推送/清理工作区。
- 复审通过只说明方案无明显阻塞；模型真实任务效果、未来实现质量与性能均不能由该结论代替测试和验收。

