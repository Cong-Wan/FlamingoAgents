# Orchestrator-Worker 方案二次架构评审

> Author: wilbur
> Version: 1.0
> Date: 2026-08-10
> Review target: `docs/orchestratorWorkerPlan.md`
> Review scope: 递归派发边界、并行调度职责、线程与限流模型、写入安全、生命周期及现有代码融合
> Status: 待独立 `pi -p` 复核

## 一、总览

- 结论：**主链路可行，但仅适合作为“只读 worker / 最佳努力编排”的一期原型；尚不适合多个 coder 在同一工作树中并发写入。**
- 方案最正确的部分是：LLM 只通过结构化 `dispatchTask(tasks[])` 描述任务，真正的并发、限流、超时和收集由确定性 dispatcher 执行。
- 方案当前最危险的部分是：把“未给 worker 注册 `dispatchTask`”表述成完整递归防护，并让写 worker 共享 `workDir`，超时后还可能继续后台写入。
- “零核心改动”适合作为一期约束，但不应成为长期红线；会话级预算、取消、追踪和事件桥接都需要至少把 `sessionId` / lineage / cancellation 传入工具执行上下文。

### 问题统计

- 🔴 Critical：1
- 🟠 High：5
- 🟡 Medium：7
- 🔵 Low：2

---

## 二、方案做得好的地方

### 1. function calling 作为编排入口，边界清晰

`dispatchTask` 使用固定 schema，参数经过现有 `toolRuntime` 校验，结果按 `toolResult` 回到主 agent 上下文。相比让模型拼 shell 或调用 Web API，这条路径依赖方向正确、可观测、也最贴合当前代码。

### 2. `tasks[]` 正确绕开了同批工具串行问题

现有 `agent.driveToolBatch()` 对多个 tool call 逐个执行。把一批独立任务放进单次 `dispatchTask({tasks: [...]})`，再由工具内部并发，是在“不改核心”的约束下最小且有效的办法。

### 3. worker profile 与现有装配层契合

现有 `createAgent()` 已支持 provider/model/system prompt/工具白名单，worker profile 不需要侵入 core。worker 不传 `workersConfigPath` 时，模型请求中的 tools 列表确实不会出现 `dispatchTask`，能防止**原生 function call 路径上的直接递归**。

### 4. 已如实承认同步工具和线程超时的限制

文档没有把 Python 线程超时包装成“已取消”，而是明确说明运行中的线程不能强杀、主会话锁会长时间持有。这一点比隐藏限制可靠。

### 5. 部分失败仍返回成功结果的语义合理

一批任务中部分成功、部分失败时，整体 `isError=False`，让主 agent 能读取成功结果；只有整批参数级失败才标工具错误。这个聚合语义适合 fan-out/fan-in。

---

## 三、问题清单

### 🔴 Critical：共享工作树与不可取消超时组合，会产生返回后的代码破坏

**位置**：§4.3-4、§4.3-5、§4.6

**问题**：多个 coder 共享同一个 `workDir`，写冲突仅靠主 agent 的 prompt 约束；worker 被逻辑标记超时后，线程仍可能继续执行 `write/edit/bash`。因此主 agent 可能在收到“超时”后开始汇总、测试或再次派发，而旧 worker 随后覆盖文件。即使两个任务声称负责不同文件，测试、格式化、生成物、Git 索引和公共配置仍可能互相影响。

**修复方案**：

1. 一期只允许并行只读 worker；`researcher` 不应配置通用 `bash`，因为当前 bash 可写文件。
2. 写 worker 使用独立 Git worktree/临时工作目录，返回 patch/commit，由主 agent 串行审阅并合并。
3. 如果 writer 必须支持硬超时，runner 应改为 dispatcher 内部受控子进程，并在超时后终止进程组；线程只能用于允许后台完成的只读任务。
4. 在完成隔离前，不应宣称支持“多个 coder 真并行修改同一项目”。

---

### 🟠 High：当前递归防护只防直接工具调用，不能保证 worker 不间接启动 agent

**位置**：现状分析“递归防护自然成立”、§4.7、§6 验证 6

**问题**：worker 工具列表没有 `dispatchTask`，足以阻止模型原生调用该函数；但拥有通用 `bash` 的 worker 可执行 `uv run python askModel.py --workers`、导入 `createAgent()`、调用本地 HTTP 接口，或创建脚本间接启动更多 agent。当前 read/write/edit 还允许绝对路径，worker 也不是安全沙箱。

**修复方案**：明确区分两种保证：

- **功能级防递归（一期可接受）**：worker 不注册 dispatch 工具；增加不可变 `agentRole='worker'` / `dispatchDepth=1`，dispatcher 拒绝 depth > 0 的直接派发；测试模型 tools 列表不含该工具，并测试伪造 tool call 返回 unknown tool。
- **严格隔离保证**：不能给 worker 通用 bash/任意代码执行能力，或必须放进限制进程创建、本地 API、配置文件和网络出口的沙箱。只要 worker 同时拥有通用 shell、项目源码和凭据，就不能诚实声称“绝不可能间接再起 agent”。

建议把原文“递归防护自然成立”改为“原生 dispatchTask 路径的一层派发限制成立”。

---

### 🟠 High：provider 限流并非真正的进程级/plan 级限流

**位置**：§4.1、§4.3-1、§4.3-2、§4.10-2

**问题**：dispatcher 按 `(workersConfigPath, workDir)` 缓存，每个 workDir 都会持有独立信号量，因此多个 Web 会话/项目可同时突破同一 provider 的限制。主 agent 自己的模型请求也不经过 worker dispatcher 的信号量。同一账号若对应多个 providerId，同样会漏限流。

**修复方案**：

1. 抽出进程级 `providerLimiterRegistry`，不要把 limiter 绑定到 workDir dispatcher。
2. 配置显式 `concurrencyKey` / `planId`，按真实账号/plan 限流，而不是默认等同 providerId。
3. 若要求精确限制“并发 HTTP 请求数”，limiter 最终应位于 model adapter 请求边界，并同时覆盖主 agent 与 worker；一期若只限制 worker 数，应在文档中改名为“worker 生命周期粗粒度限流”。

---

### 🟠 High：在线程任务内部等待 provider 信号量会造成队头阻塞

**位置**：§4.3-1、§4.3-3

**问题**：先把所有任务提交到 `ThreadPoolExecutor(max_workers=maxParallel)`，再让执行体内部 acquire provider semaphore，会出现线程都被同一 provider 的等待任务占满、其他 provider 的可运行任务却排在 executor 队列中的情况。这样虽然不会突破上限，却会显著损失跨 provider 并行度。

示例：`maxParallel=4`，A 限制 1，任务顺序为 A/A/A/A/A/B；4 个线程中 1 个运行 A、3 个阻塞等待 A，B 可能长期排队。

**修复方案**：dispatcher 维护待运行队列和计数，只把同时满足“全局槽位 + provider 槽位”的任务提交给 executor；future 完成后释放计数并调度下一项。不要用 executor 工作线程承担排队职责。

---

### 🟠 High：dispatcher 缓存键与其持有状态不一致

**位置**：§4.3-1、§4.3-2

**问题**：缓存键只有 `(workersConfigPath, workDir)`，但 dispatcher 还持有 `logDir/debug/modelConfigPath/toolsConfigPath`。同一配置和 workDir 以不同 model config、tools config 或 logDir 创建主 agent 时，会静默复用第一个 dispatcher 的旧参数。

**修复方案**：优先让 dispatcher 只持有 profiles、scheduler 和全局 limiter；workDir/logDir/debug/各配置路径作为每次 dispatch 的 execution context 传入。若仍保持有状态缓存，则所有会影响行为的构造参数都必须进入缓存键。

---

### 🟠 High：只有单次 tasks 上限，没有会话级派发预算和 lineage

**位置**：§4.2、§4.3-3、§4.7

**问题**：每次最多 16 个任务，但主模型可在最多 32 个 model steps 中反复调用 dispatch，理论上可产生数百个 worker。当前 `toolContext` 只有 workDir/debugConsole，dispatcher拿不到 parent sessionId，也就无法做每会话总任务数、派发轮数、去重、取消和父子追踪。

**修复方案**：

- 给工具执行上下文增加 `sessionId`、`agentRole`、`dispatchDepth`、可选 cancellation token；
- 增加 `maxDispatchRounds`、`maxTasksPerRun`、`maxTotalWorkerSteps` 等硬预算；
- 每个任务要求稳定 taskId，记录 parentSessionId/dispatchId/taskId，便于幂等和审计。

这意味着“零核心改动”不应是长期硬约束；为一等多 Agent 能力增加少量上下文字段，比在闭包和全局缓存中绕过更可靠。

---

### 🟡 Medium：并行批次是否产生仍由 LLM 概率性决定

**位置**：总体设计、§4.2、§4.6

**问题**：dispatcher 能确定性并行 `tasks[]`，但它不能保证主模型一定拆出多个任务、一定放进同一次调用。模型可能只派一个任务，也可能在多轮中依次派发，或者在同一 assistant message 中产生多个 `dispatchTask` tool call，而后者仍会被 core 串行执行。

**修复方案**：采用混合模式：

- LLM 负责语义拆解、worker 选择和依赖判断；
- dispatcher 负责校验并发批次并确定性执行；
- 对“必须并行”的场景提供显式 CLI/API/UI 入口，调用方可直接提交 tasks，而不是必须等待模型自发调用；
- 只有需要复杂依赖时再演进 DAG，不应一期直接引入完整工作流引擎。

---

### 🟡 Medium：worker 名称和任务契约对模型不够可见

**位置**：§4.1、§4.2、§4.6

**问题**：schema 中 `worker` 只是任意字符串，主模型不知道动态 profile 的可选值和能力；`task` 只有自由文本，容易遗漏目标、输入、范围和输出格式。

**修复方案**：工具工厂根据配置动态生成 worker `enum` 和 profile 简介；任务至少包含 `taskId/objective/context/expectedOutput`。写任务还应有由 dispatcher 校验的 `allowedPaths`，不能只写在自然语言里。

---

### 🟡 Medium：结果只聚合为文本，不利于重试、UI 与状态判断

**位置**：§4.4

**问题**：主模型可阅读文本，但程序无法稳定取得每项 status/sessionId/duration/truncation/errorType。后续 Web 事件桥接和按失败项重试会再次解析文本。

**修复方案**：`toolOutput.content` 保留可读 Markdown，同时在 `toolOutput.details['tasks']` 放结构化结果；聚合顺序固定为输入顺序，完成顺序另记时间字段。

---

### 🟡 Medium：确认请求在一期结束后不能按现有描述直接续跑

**位置**：§4.5、二期展望

**问题**：`pendingConfirm` 只保存在 worker agent 内存，confirmation 事件本身不写 JSONL；局部 worker 实例被丢弃后，只剩 dangling tool call 日志。可通过重建会话重新评估工具，但无法仅凭旧 confirmationId 调 `continueConfirmation()`。二期不能只“透传 confirmationId”，还需要保留 worker 实例或持久化可恢复的 pending 状态。

**修复方案**：一期明确写成“记录日志并放弃该 worker，不支持直接批准续跑”；二期设计 worker session registry 与 pending 状态恢复后再承诺交互式批准。

---

### 🟡 Medium：超时同时包含排队时间，且未区分未启动与运行中任务

**位置**：§4.3-3、§4.3-4

**问题**：从 submitTime 起算会让受 provider 限流的任务在开始执行前就耗尽 timeout。对尚未启动的 future 可以 cancel；对已经占线程等待 semaphore 的 future 则无法 cancel，之后仍会真正执行。

**修复方案**：调度器应区分 `queued/running/timedOutBeforeStart/timedOutRunning`；只有取得并发槽后才进入 running。配置名称应明确是 batch deadline、queue timeout 还是 run timeout。

---

### 🟡 Medium：builder 与 workerDispatcher 存在潜在循环导入

**位置**：§4.3、§4.7

**问题**：builder 为装配 dispatch tool 需要导入 workers 包，而 workerDispatcher 又要调用 builder.createAgent。若双方模块顶层互相 import，会在实现时形成循环依赖。

**修复方案**：workerDispatcher 不在模块顶层导入 builder；由 builder 在运行时注入 `agentFactory=createAgent`，或在执行任务的方法内部延迟导入，并为该依赖方向写明确约束。

---

### 🟡 Medium：递归防护的验证步骤按当前日志结构无法执行

**位置**：实施步骤 6

**问题**：方案要求从 worker 会话 JSONL 的“assistantMessage 请求 tools 字段”断言不含 `dispatchTask`，但当前 `conversation.appendAssistantMessage()` 只记录响应中的 model/content/toolCalls/usage/timings，不记录模型请求的 tools。正常 worker JSONL 中没有该字段可供检查。

**修复方案**：直接检查创建出的 worker `toolRegistry.list()`，或给 dispatcher 注入可观测的 worker factory，在测试中捕获 worker 实例后断言；另补一条伪造 `dispatchTask` tool call 返回 `unknownTool=True` 的行为测试。不要为了测试把完整请求（可能含敏感上下文）写入常规日志。

---

### 🟡 Medium：所谓只读 researcher 配置了可任意写入的 bash

**位置**：§4.1 示例

**问题**：system prompt 的“只做只读分析”不是权限控制。当前 bash 可执行重定向、Python、Git、curl 等，删除确认规则也不等同只读沙箱。

**修复方案**：只读 profile 使用 `[read]`；如确需搜索，增加专用只读 grep/find 工具，或提供严格命令白名单，而不是通用 shell。

---

### 🔵 Low：配置校验规则还有未闭合项

**位置**：§4.1

**问题**：标准 `yaml.safe_load()` 无法自然报告重复 mapping key；`maxModelSteps`、空 profile 名、空 task、providerLimits 未知 key、精确 modelId 等校验未完整列出。“复用 createAgent 报错”也不等于 workers.yaml 加载时 fail-fast。

**修复方案**：明确 loader 的完整 schema；如确实要求重复 key 报错，使用自定义 YAML loader。工具名应直接对照 tools config 校验，不要为了校验实例化 agent。

---

### 🔵 Low：一期不统计 worker usage，与产品目标存在观测缺口

**位置**：§4.4、§4.9

**问题**：功能主目标是多 plan 并发，但一期 Web 不可见、usage.db 不记 worker 消耗，只能查散落 JSONL。CLI 原型可以接受，但不应在宣称功能完成时忽略。

**修复方案**：一期至少在结构化 dispatch result 中汇总每个 worker usage；Web 记账可继续放二期。

---

## 四、问题 1：怎么保证 subagent 不再派发 subagent？

### 当前方案能保证的范围

调用链为：

```text
builder 创建 orchestrator
  └─ 注册 dispatchTask
      └─ dispatcher 创建 worker（不传 workersConfigPath）
          └─ worker toolRegistry 中没有 dispatchTask
```

每轮模型请求的 tools 都来自 worker 自己的 `toolRegistry`，所以 worker 看不到 `dispatchTask`。即使模型幻觉生成同名 tool call，`driveToolBatch()` 也只会返回“未知工具”，不会执行 dispatcher。**对原生 function calling 的直接递归，这个机制是代码级保证，不依赖 prompt。**

### 当前方案不能保证的范围

如果 worker 有通用 bash，它可以绕过工具注册表，间接运行 CLI、Python 或 HTTP 来创建 agent。此时“不传 workersConfigPath”不再是安全边界。

### 推荐的两层防护

1. **一期功能级防护**
   - worker 不注册 `dispatchTask`；
   - 增加 `agentRole` 与 `dispatchDepth`，只允许 `role=orchestrator && depth=0` 装配/执行 dispatch；
   - dispatcher 创建 worker 时强制 depth=1，不从 profile 读取；
   - worker profile 不能配置 dispatch 能力；
   - 测试 tools 列表、伪造调用和 depth 拒绝三条路径。

2. **需要严格安全保证时**
   - 禁止通用 bash，或把 worker 放入受限 runner；
   - 限制本地 API、进程创建、配置/凭据读取和网络出口；
   - writer 使用隔离工作树/容器。

结论：**“工具不注册”足以防误用和正常模型递归，但不足以对拥有任意命令执行能力的 worker 提供安全隔离。**

---

## 五、问题 2：并行 subagent 怎么实现，是否全由 LLM 决定？

不是。应明确拆成两个职责：

### LLM 负责语义决策

- 任务是否可拆；
- 拆成哪些独立任务；
- 每项选择哪个 worker；
- 哪些有依赖、需要下一轮再派。

示例输出：

```json
{
  "tasks": [
    {"worker": "researcher", "task": "分析模型层"},
    {"worker": "reviewer", "task": "分析工具权限层"}
  ]
}
```

### dispatcher 负责确定性并发

收到数组后，程序而非 LLM 执行：

```text
validate → admission control → submit runnable tasks
         → global/provider limits → collect results → aggregate
```

只要同一次调用中有两个可运行任务且并发额度允许，它们是否并行不再由模型决定，而由 scheduler 确定。

### 三种产品模式

| 模式 | 拆解来源 | 并行保证 | 复杂度 | 适用场景 |
|---|---|---:|---:|---|
| A. 模型主导 | LLM 自己调用 dispatchTask | 最佳努力 | 低 | 自然对话、一期原型 |
| B. 混合模式（推荐） | LLM 产出结构化批次，dispatcher 校验执行；CLI/API 可直接提交 | 对已提交批次确定保证 | 中 | 当前项目目标 |
| C. DAG 工作流 | LLM/用户给 task graph，scheduler 拓扑分层 | 强 | 高 | 多轮复杂依赖，二期以后 |

推荐 B：保留自然语言体验，但把并发执行和资源治理留给代码；用户明确要求“必须并行”时，应有直接提交任务批次的入口，不能只依赖 prompt 期待模型自发拆分。

---

## 六、建议收敛后的一期架构

```text
Orchestrator LLM
  │ 只负责生成结构化、无依赖任务批次
  ▼
dispatchPolicy
  │ 校验 role/depth、任务预算、worker、只读能力、taskId
  ▼
deterministicScheduler
  │ 全局队列 + concurrencyKey 限流；只提交可运行任务
  ▼
readOnlyWorkerRunner（一期线程即可）
  │ 独立 agent/session；无 dispatch、无通用 bash、无写工具
  ▼
structuredAggregator
  └─ content: Markdown；details.tasks: 结构化状态
```

写 worker 建议作为下一步单独设计：`isolated worktree + controlled subprocess + patch/commit merge`。不要把它与只读并行原型一起宣称完成。

## 七、进入实施前需要用户确认的三项选择

1. **递归防护目标**
   - A：功能级，保证 worker 无原生 dispatch 能力（一期推荐）；
   - B：安全级，即使模型有恶意也不能通过 shell/HTTP/脚本间接启动（需要沙箱与工具收缩）。

2. **一期是否允许 worker 写代码**
   - A：一期仅并行只读调研/审核（推荐，方案可快速落地）；
   - B：允许并行写，但必须先加入 worktree/子进程隔离。

3. **编排模式**
   - A：仅让 LLM 自发调用（最简单但不保证一定拆分）；
   - B：混合模式，增加显式批次入口（推荐）；
   - C：直接做 DAG 调度（当前需求下偏重）。

## 八、最终评价

这不是一个应被推翻的方案。它的 function-call 入口、profile 装配和 `tasks[]` fan-out/fan-in 都应保留；需要修正的是边界声明和执行层：

- 不要把“工具列表里没有 dispatchTask”扩大解释成 shell 沙箱；
- 不要让不可取消的并发 writer 共享工作树；
- 不要让 LLM 负责并发机制，只让它负责语义拆解；
- 不要为了“零核心改动”放弃 session lineage、预算和取消这些一等编排能力。

完成上述收敛后，该方案可以成为可靠的一期多 Agent 基础，而不是只能演示成功路径的并发工具。
