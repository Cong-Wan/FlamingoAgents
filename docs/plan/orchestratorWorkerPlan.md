# Orchestrator-Worker 多 Agent 编排（dispatchTask 工具）—— 方案计划

> Author: wilbur
> Version: 1.0
> Date: 2026-08-08
> Status: 待实施（调研与评审阶段）
> 关联：README 路线图「多 Agent 编排 / 并发作业派发」；`docs/initAgentCustomizationPlan.md`（createAgent 定制能力，本方案的地基）
> 评审：docs/codeReview/260808_orchestratorWorkerPlan.md（10 个问题已全部修复回本文档）

## 背景与目标

README 的初心是「多 Agent 并发作业，突破单个 Coding Plan 的并发限制」。当前已实现多 provider 接入与按会话绑定模型，缺的是**编排派发**：让一个主 agent（Orchestrator）把任务拆给多个 worker agent 并行执行，并汇总结果。

目标：

1. 主 agent 通过 **function calling** 调用 `dispatchTask` 工具派发子任务（模型原生能力，参数有 schema 校验，结果自动回到主 agent 上下文）；
2. 每个 worker 绑定独立 profile：**不同 provider/model + 不同 systemPrompt + 不同工具白名单**；
3. 多个 worker **真并行**执行，且受 per-provider 并发限制（防同 plan 触发 429）；
4. 零核心改动：`agent` 核心、事件流、`toolContext`、webApp 均不动；不使用 `dispatchTask` 的现有调用方（CLI / Web 会话）行为完全不变。

### 方案选型记录（已否决的替代路径）

| 路径 | 结论 | 理由 |
|---|---|---|
| B1 function call 工具派发 | ✅ 采纳 | 模型原生结构化调用；`toolRuntime` 的 schema 校验/异常包装现成；结果作为 toolResult 直接进上下文 |
| B2 主 agent 用 bash 起 worker 子进程 | ❌ 否决（实现层保留） | stdout 截断、黑盒不可观测、确认链断裂、模型要拼 shell 易错。进程隔离思想留作 dispatcher 二期演进位（接口不变） |
| B3 主 agent 调 webApp HTTP API 建会话 | ❌ 否决 | 库回调 Web 层造成依赖倒置；SSE/会话协议对模型负担过重；自调用有锁竞争风险。UI 可视化留作二期（Web 层订阅 dispatcher 事件，而非 agent 调 API） |

## 现状分析结论

- **差异化 worker 装配已就绪**：`createAgent(providerId=..., modelId=..., systemPrompt=..., toolNames=...)`（builder v1.4）正好覆盖「每个 agent 不同模型 + 不同 prompt + 不同工具」的全部需求；
- **工具扩展机制已就绪**：`defineTool` 可直接构造工具定义；execute 是同步阻塞函数——worker 的整个生命周期发生在 execute 内部，跑多久主 agent 等多久，异常由 `toolRuntime` 统一包装成 toolResult。注：内置工具（`createBuiltinTools`）是「名称映射 execute + `toolContext` 注入」模式，`toolContext` 仅含 `workDir/debugConsole`，塞不下 dispatcher——`dispatchTask` 采用**工厂闭包捕获 dispatcher**，这是本项目新引入的模式（本身合理，但无先例，勿去 builtinTools 找参考）；
- **并发原语已验证**：`streamPump` 证明「阻塞式生成器 + 线程」模式在本项目稳定；worker agent 各自独立实例、各自会话锁，无共享内存态；
- **关键障碍 1 个**：`agent.driveToolBatch` 对同一批 toolCalls **串行**执行。若模型一次返回 N 个 dispatchTask 调用会排队跑，并行失效。→ 对策：`dispatchTask` 接受 `tasks[]` 数组参数，**并行发生在工具 execute 内部**（线程池），核心零改动；
- **递归防护自然成立**：worker 由 dispatcher 内部调 `createAgent` 创建且**不传 workers 配置** → worker 的工具集里物理上不存在 `dispatchTask`，无需额外防护代码；
- **确认机制冲突**：worker 跑在工具内部，无人可点确认框。worker 触发 `requireApproval` 规则时会得到 `confirmationRequired` 终态——一期策略：**终止该 worker，把确认请求转述给主 agent**（见 §4.5）。

## 总体设计

```
用户消息 → 主 agent（createAgent 传了 workersConfigPath，多装配一个 dispatchTask 工具）
  → 模型决定拆任务，返回 toolCall: dispatchTask({tasks: [{worker, task}, ...]})
  → driveToolBatch 命中工具（现有流程，串行批次里只有这一次调用）
  → dispatchTask.execute() 内部（workerDispatcher）：
       ├─ ThreadPoolExecutor 并行起 N 个 worker（per-provider 信号量限流）
       ├─ 每个 worker = createAgent(profile...) + runUserMessage(task)（同步 API）
       ├─ 收集各 worker 终态（completed / confirmationRequired / error / 超时）
       └─ 聚合成一段结构化文本作为 toolOutput.content
  → toolResult 回到主 agent 上下文 → 主 agent 读结果，继续推理 / 汇总 / 再派发
```

## 详细设计

### 4.1 `config/workers.yaml`（新增，worker profile 配置）

```yaml
version: 1

dispatch:                        # 派发治理参数（均可缺省，取默认值）
  maxParallel: 4                 # 全局并发上限，默认 4
  taskTimeoutSeconds: 600        # 单 worker 逻辑超时，默认 600
  providerLimits:                # per-provider 并发信号量，缺省 = maxParallel（即仅受全局上限约束）
                                 # 注意：key 是 providerId；同一 plan/账号配了多个 providerId 时需分别配置
    glm: 2

workers:                         # 必填，至少 1 个 profile
  researcher:                    # profile 名 = dispatchTask 参数里的 worker 值
    providerId: glm              # 必填，必须存在于 models.yaml
    modelId: glm-5.2             # 可选，缺省 = 该 provider 第一个模型
    systemPrompt: |              # 可选，缺省 = config/systemPrompt.md
      你是调研员，只做只读分析，输出结构化结论。
    toolNames: [read, bash]      # 可选，缺省 = 全部内置工具；[] = 纯对话
    maxModelSteps: 16            # 可选，缺省 16（worker 任务聚焦，小于主 agent 的 32）
  coder:
    providerId: volcano
    modelId: doubao-seed-code
    systemPrompt: 你是实现工程师，按任务单写代码，完成后自验。
    toolNames: [read, write, edit, bash]
```

校验规则（加载时 fail-fast，全部抛 `RuntimeError`）：

- `workers` 缺失/为空/含重复语义的非法项 → 报错；
- profile 缺 `providerId`、`providerId` 在 `models.yaml` 中不存在 → 报错（加载时调用 `loadModelConfig` 预检）；
- `toolNames` 含未知内置工具 → 复用 `createAgent` 现有报错；
- `maxParallel` / `taskTimeoutSeconds` / `providerLimits` 非正整数 → 报错。

提供 `config/workers.example.yaml`，`workers.yaml` 加入 `.gitignore`（与 models.yaml 同级处理——虽无密钥，但含本地 plan 拓扑）。

### 4.2 `dispatchTask` 工具定义（schema 写死在工厂内，不进 tools.yaml）

```json
{
  "name": "dispatchTask",
  "description": "将子任务派发给指定 worker 并行执行，返回各 worker 的结果汇总。tasks 数组内多个任务真并行；worker 取值见各 profile 名。适合：可拆分的独立子任务、需要不同能力/模型处理的分支。不适合：有先后依赖的步骤（先等本轮结果再派下一轮）。",
  "parameters": {
    "type": "object",
    "properties": {
      "tasks": {
        "type": "array",
        "minItems": 1,
        "items": {
          "type": "object",
          "properties": {
            "worker": { "type": "string" },
            "task": { "type": "string" }
          },
          "required": ["worker", "task"],
          "additionalProperties": false
        }
      }
    },
    "required": ["tasks"],
    "additionalProperties": false
  }
}
```

- schema 直接写在 `defineTool` 工厂里，**不进 `tools.yaml`**：`createBuiltinTools` 的语义是「内置工具全量加载 + 名称映射 execute」，而 `dispatchTask` 的可用性由「是否传 workersConfigPath」决定，且需要闭包注入 dispatcher，两者语义不同，避免 `createBuiltinTools` 被迫认识一个没有 execute 映射的 schema；
- 现有 `validateArguments` 已支持 array/items/object/minItems 嵌套校验，schema 无需扩展校验器；注意 `validateArguments` **不支持 maxItems**，tasks 数量上限（16）改在 execute 内校验（超限整批返回 isError）；
- **无 permissions 规则**：dispatchTask 本身不需确认（worker 内部工具的权限规则在 worker 会话内各自生效）；
- `preview`：返回 `f'派发 {len(tasks)} 个子任务：{worker 名列表}'`，供 Web 工具卡片展示。

### 4.3 `workerDispatcher`（新包 `flamingoAgents/workers/`）

```
flamingoAgents/workers/
  __init__.py            # 导出 loadWorkerProfiles / workerDispatcher / createDispatchTool
  workerConfig.py        # workers.yaml 加载与校验（仿 modelConfig.py 风格）
  workerDispatcher.py    # 线程池 + 信号量 + 超时 + 结果聚合
  dispatchTool.py        # defineTool 工厂：闭包捕获 dispatcher，返回 toolDefinition
```

`workerDispatcher` 核心行为：

1. **初始化**：持有 profiles、`ThreadPoolExecutor(max_workers=maxParallel)`、per-provider 信号量、主 agent 的 workDir / logDir / debug 开关 / modelConfigPath / toolsConfigPath（透传给 worker 的 createAgent；注意 createAgent 只收 `debug: bool` 自建 debugConsole 实例，worker 继承的是**开关**而非实例）。信号量**显式构建**，防缺省值退化：
   ```python
   semaphores = {pid: threading.Semaphore(limit) for pid, limit in providerLimits.items()}
   semaphoreFor = lambda pid: semaphores.setdefault(pid, threading.Semaphore(maxParallel))
   # 未配置 providerLimits 的 provider 缺省 = maxParallel，不是 1（Semaphore() 无参构造初值为 1，有坑）
   ```
2. **生命周期（per-process 缓存单例）**：dispatcher 内含常驻线程池，若随 createAgent 每会话新建且从不 shutdown，webApp 场景 N 个编排会话 = N 个线程池泄漏。因此 dispatcher 不随 createAgent 创建：workers 包提供模块级 `getDispatcher(workersConfigPath, workDir, ...)`，按 `(解析后配置路径, 解析后 workDir)` 键缓存复用（仿 agentCache 模式），createAgent 装配 dispatchTask 时调用之。一期缓存对 workers.yaml 变更不感知（重启生效，列入 §4.10）。shutdown：CLI 进程退出由 atexit 注册一次 `shutdown(wait=False, cancel_futures=True)`；webApp 二期接入时改挂 lifespan。
3. **dispatch(tasks) -> str**（同步阻塞，execute 的唯一入口）：
   - tasks 数量 > 16 → 不进池，整批返回 isError（上限校验在 execute 内，见 §4.2）；
   - 未知 worker 名 → 该子任务直接标记失败（不抛异常，其余任务照常），聚合文本中说明可用 worker 列表；
   - 每个子任务提交线程池并记录 submitTime，执行体先 acquire 对应 provider 信号量再执行；
4. **超时语义（如实声明，不许美化）**：收集用 `concurrent.futures.as_completed` + 统一 deadline——`taskTimeoutSeconds` 从**提交时刻**起算，不是从逐个等待时刻起算（逐个 `future.result(timeout)` 会让第 k 个任务实际可跑约 k 倍超时）。超时到期：dispatcher 放弃等待并把该子任务标记为超时，但**这只是逻辑放弃**——worker 线程无法强杀，会继续跑完自身模型循环（存活上限 ≈ maxModelSteps × 单步耗时），其文件写操作在主 agent 收到结果后**仍可能生效**；executor 工作线程为**非 daemon**，进程退出会等待其结束。聚合文本如实标注「仍在后台执行直至完成」（§4.4）；
5. **单个子任务执行体**：
   - `sessionId = f'worker_{profile名}_{uuid4().hex[:8]}'`；
   - `createAgent(workDir=主agent.workDir, logDir=主agent.logDir/'workers', providerId, modelId, systemPrompt, toolNames, maxModelSteps=profile值)`——**同 workDir**（coder 类 worker 需要改主项目文件；写冲突靠主 agent 拆任务时按文件/目录划分职责，prompt 引导，见 §4.6）；
   - 调 `runUserMessage(task, sessionId)`（同步包装 API，自动耗尽事件流；worker 事件一期不转发）；
   - 按 `runResult.status` 映射结果文本（见 §4.4）；
6. **aggregator**：输出格式见 §4.4；聚合时**单段输出截断 8000 字符**（标注 `<truncated>`），**总输出上限 30000 字符**——防 N 个长结果撑爆主 agent 下一轮请求的上下文（表现为模型调用失败且根因难排查）。

### 4.4 结果聚合格式（toolOutput.content）

```text
## 子任务 1（worker=researcher）✅ 完成
<completedEvent.message 原文>

## 子任务 2（worker=coder）⚠️ 需要人工确认，已终止
worker 请求确认：删除命令需要用户确认。
预览：rm -rf ./tmp
worker 会话已落日志（sessionId=worker_coder_a1b2c3d4），可人工排查。请调整任务描述后重派，或向用户报告。

## 子任务 3（worker=reviewer）❌ 失败
模型调用失败：...

## 子任务 4（worker=coder）⏱️ 超时（600s），已放弃等待
注意：该 worker 仍在后台执行直至自身完成，其文件写操作可能在你收到本结果之后生效。
```

- 单段结果超过 8000 字符时截断并标注 `<truncated>`；全量输出上限 30000 字符（见 §4.3-6）；
- 任一子任务失败/确认/超时 → `toolOutput.isError = False`（**整体不算工具错误**，否则主 agent 拿不到其他子任务的成功结果；由各段状态标识让主 agent 自行判断如何继续）；
- 全部子任务都因参数级问题失败（如 worker 名全部未知）→ `isError = True`；
- worker 的 token 用量记录在各自 jsonl 日志中；**一期不入 webApp usage.db**（webApp 不感知 worker，二期事件桥接时一并解决）。

### 4.5 确认转述策略（一期决策）

worker 触发 `requireApproval` → `runUserMessage` 返回 `status='confirmationRequired'` → dispatcher **终止该 worker**（会话 jsonl 已落盘，可 resume 可排查），按 §4.4 格式转述给主 agent。主 agent 可选择：换描述重派、向用户报告、或自己接管执行。

一期**不做** profile 级 autoApprove（绕过权限规则的口子一旦开了难收回）；二期再评估交互式批准（把 confirmationId 透传到 Web 层）。

### 4.6 主 agent 的编排引导（systemPrompt 约定）

使用 dispatchTask 的主 agent，其 systemPrompt 需自行补充编排指引（本方案提供 `config/orchestratorPrompt.md` 示例，不进库默认值）：

- 何时拆、怎么拆（按文件/目录划分 worker 职责，避免写冲突）；
- **无依赖的独立子任务必须在同一次 dispatchTask 的 tasks 数组中一起提交**（单轮多任务并行派发）；有依赖的步骤分轮派发（先等本轮结果再派下一轮）；
- worker 返回「需要确认」时如何向用户转述。

### 4.7 `builder.py` 改动（库层唯一被修改的现有文件）

`createAgent` 新增 2 个关键字参数：

```python
workersConfigPath: str | Path | None = None   # 传入即装配 dispatchTask 工具；None = 现状不变
maxModelSteps: int = 32                       # 透传 agent 构造器（缺省保持现状 32）；
                                              # dispatcher 建 worker 时用 profile 的 maxModelSteps 值
```

处理位置：dispatchTask 定义在 toolNames 白名单过滤**之后**追加——编排能力由 workersConfigPath 独立控制，不与 toolNames 语义纠缠（主 agent 即使 toolNames 收窄也能派发；worker 创建时不传 workersConfigPath，物理上无法拿到 dispatchTask → 递归防护）。

dispatcher 不随 createAgent 新建，由 `getDispatcher()` per-process 缓存单例提供（见 §4.3-2），createAgent 仅触发获取。

装配失败（workers.yaml 缺失/校验失败）→ `createAgent` 抛 `RuntimeError`，fail-fast 不静默降级。

### 4.8 线程安全论证

- 每个 worker 是独立 `agent` 实例 + 独立 sessionId + 独立 RLock，无共享内存态；
- `createAgent` 是纯装配函数（每次读 models.yaml / tools.yaml，均为只读），可并发调用；
- dispatcher 的信号量 / 线程池为标准线程安全原语；
- 主 agent 的会话锁在 dispatchTask execute 期间由主 agent 自己持有（在 driveToolBatch 调用栈内），worker 不触碰主 agent 会话状态；
- worker 的 jsonl 日志写各自文件（`logDir/workers/{sessionId}.jsonl`），原子追加，无写竞争。

### 4.9 启用入口（一期 CLI，webApp 二期）

若没有任何调用方传 `workersConfigPath`，功能落地后不可达、端到端验证无入口。因此：

- **一期**：`askModel.py` 加 `--workers [路径]` 参数（缺省 `config/workers.yaml`），透传 createAgent；演示与端到端验证（实施步骤 4/5）均走此入口；
- **webApp 一期不接**（会话元数据加 workersConfigPath 字段 + UI 入口属二期，与 worker 事件桥接一起做）；现有 Web 会话不传该参数，行为完全不变。

### 4.10 已知限制（设计固有，明示不藏）

1. **主会话锁全程持有**：dispatchTask execute 阻塞期间（最长 taskTimeoutSeconds+），主会话锁被持有，同会话新消息排队；同步 execute 无法 yield 中途事件，Web 工具卡片最长转圈 10 分钟无反馈（二期事件桥接解决）；
2. **providerId 限流粒度 ≠ plan 粒度**：同一 plan/账号配多个 providerId 时需分别配置 providerLimits（见 §4.1 注释）；
3. **超时无法强杀 worker**：逻辑放弃后 worker 仍跑至自身终态，写操作可能滞后生效（见 §4.3-4）；
4. **dispatcher 缓存不感知 workers.yaml 变更**：重启进程生效（二期可做失效标记，仿 invalidateAllAgents）。

## 实施步骤与验证（目标导向，不引入测试框架）

前置：models.yaml 至少配 2 个不同 provider 的有效 plan。

1. **`workers/workerConfig.py`**：workers.yaml 加载 + 校验 → 验证：脚本断言各类非法配置均抛 RuntimeError（缺 providerId / provider 不存在 / 非法 maxParallel）；合法 example 配置加载成功。
2. **`workers/workerDispatcher.py` + `dispatchTool.py`** → 验证：
   - mock 级：构造 dispatcher，dispatch 未知 worker 名 → 聚合文本含失败段与可用列表，isError 符合 §4.4 规则；tasks 17 个 → 整批 isError；
   - 并行性：派发 2 个不同 provider 的耗时任务，断言墙钟时间 < 两任务串行之和（容差内），且 `logDir/workers/` 生成 2 份 jsonl；
   - 限流：同 provider 派 3 个任务、providerLimits 设 1 → 断言存在严格先后关系（jsonl 时间戳）；**未配置 providerLimits 的 provider 派 2 个任务 → 断言真并行**（防信号量缺省值退化为 1 的回归，审核 P3）；
   - 超时时钟：taskTimeoutSeconds 设小值派 2 个慢任务 → 断言两者几乎同时被标记超时（验证 deadline 从提交时刻起算，审核 P4）。
3. **`builder.py` 接线**（workersConfigPath + maxModelSteps 两个参数）→ 验证：均不传时 `createAgent` 产物与现状一致（回归）；传入 workersConfigPath 时工具集多 dispatchTask；maxModelSteps 透传 agent 构造器生效。
4. **启用入口 + 端到端**（真实 plan）：`askModel.py --workers` 启动主 agent（强模型 + orchestratorPrompt），下达可拆分任务（如「分别调研 A 模块和 B 模块并汇总」）→ 断言：事件流中出现 dispatchTask 的 Start/End 配对；toolResult 含两段 worker 结果；主 agent 最终 completed 汇总了两段内容。
5. **确认转述**：worker profile 含 bash，任务诱导删除类命令 → 断言主 agent 收到「需要人工确认」转述段，且主 agent 能向用户合理解释。
6. **递归防护**：检查 worker 会话 jsonl，断言其可用工具列表不含 dispatchTask（assistantMessage 请求的 tools 字段）。
7. **文档收尾**：README 路线图勾选「多 Agent 编排」「并发作业派发」；`config/workers.example.yaml` 注释完整。

## 二期展望（本方案不做，但留了位置）

- **worker 事件桥接 Web UI**：dispatcher 增加事件订阅回调，webApp 把 worker 流转发成虚拟会话展示（复用 streamPump，不动 agent 核心）；
- **交互式批准 worker 确认**：confirmationId 透传到 Web 层，批准后 resume worker 会话续跑；
- **profile 级 autoApprove 规则** / **子进程隔离 worker**（dispatchTask 接口不变，只换 dispatcher 实现）；
- **跨 plan 负载均衡与失败转移**（dispatcher 已持有 provider 维度状态，是自然落点）。
