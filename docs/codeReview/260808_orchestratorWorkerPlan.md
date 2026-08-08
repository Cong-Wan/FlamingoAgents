# Orchestrator-Worker 方案审核报告

> Date: 2026-08-08
> Reviewer: pi -p（默认模型）
> 对象：docs/orchestratorWorkerPlan.md（初版）

## 一、架构断言核对结果（对照代码逐条验证）

| 方案断言 | 结论 |
|---|---|
| `createAgent(providerId/modelId/systemPrompt/toolNames)` 覆盖差异化装配 | ✅ 属实（builder.py v1.4 签名一致） |
| `driveToolBatch` 对同批 toolCalls 串行执行 | ✅ 属实（agent.py `for index in range(...)` 逐个 yield/执行） |
| `validateArguments` 支持 array/items/object/minItems 嵌套校验 | ✅ 属实（toolRuntime.py `validateValue` 递归处理 array→items→object，minItems 生效） |
| `createBuiltinTools` 是「工厂闭包注入依赖」模式 | ❌ 不属实（实为模块级函数 + 名称映射，依赖经 `toolContext` 注入，无闭包） |
| tools.yaml schema 结构（version 3 / parameters / permissions） | ✅ 属实 |
| worker 触发 requireApproval → `confirmationRequired` 终态 | ✅ 属实（且 resume 后 dangling 重放会重新过 `evaluateToolCall`，确认链无破口） |
| 超时线程是「daemon 线程，不阻塞进程退出」 | ❌ 事实错误（P1） |
| `defaultdict(threading.Semaphore)` 缺省 limit = maxParallel | ❌ 自相矛盾（P3） |
| profile 可配 `maxModelSteps` 并经 createAgent 传入 | ❌ 签名不存在（P2） |

## 二、问题列表

### P1【高】超时线程「daemon 不阻塞退出」论证错误，且 worker 超时后不会停止

`ThreadPoolExecutor` 工作线程是非 daemon，CPython atexit `_python_exit` 会 join 全部 executor 线程——进程退出会被阻塞。且超时后 worker 不是「收尾」，而是继续完整跑完任务（模型循环、bash、文件写照常），实际存活可能远超 taskTimeoutSeconds。
修复：① 文档如实改写超时语义；② executor 显式 `shutdown(wait=False, cancel_futures=True)` 生命周期管理；③ 聚合文本改为「仍在后台执行直至完成」。

### P2【高】`maxModelSteps` 无处可走：createAgent 签名不含此参数

builder.py v1.4 的 `createAgent` 签名没有 `maxModelSteps`（仅 `agent()` 构造器有）。按方案字面实现会 TypeError。§4.7「新增 1 个关键字参数」需改为 2 个。

### P3【中】`defaultdict(threading.Semaphore)` 与「缺省 = maxParallel」矛盾

`threading.Semaphore()` 无参构造初始值为 1，未配 providerLimits 的 provider 会被隐蔽地压到串行。
修复：显式构建 + 缺省 `Semaphore(maxParallel)`；补回归验证。

### P4【中】超时起算点错误

逐个 `future.result(timeout)` 时，第 k 个 future 的时钟从前 k-1 个收集完才开始走，极端情况后排任务可跑 ~k 倍超时。
修复：提交时记录 startTime 用剩余时间，或 `as_completed` + 全局 deadline。

### P5【中】dispatcher / 线程池生命周期未定义，webApp 场景会线程池泄漏

webApp 每会话懒建 agent，若 dispatcher 随 createAgent 每会话建一个且从不 shutdown，线程只增不减。
修复：per-process 缓存单例（按 `(workersConfigPath, workDir)` 键，仿 agentCache），明确 shutdown 时机。

### P6【中】功能入口缺失：没有任何调用方传 `workersConfigPath`

askModel.py 和 webApp agentManager 都不会传参——方案落地后功能不可达，端到端验证无入口。
修复：补「启用入口」一节（CLI `--workers` 参数一期，webApp 二期）。

### P7【中】聚合结果无长度上限，可撑爆主 agent 上下文

N 个 worker 各返回数万字符时主 agent 下一轮请求上下文超限。且 `validateArguments` 只支持 minItems 不支持 maxItems。
修复：① 单段截断 8000 字符 + 总量上限；② tasks 数量上限（16）在 execute 内校验。

### P8【低】「createBuiltinTools 闭包模式」类比错误

内置工具是名称映射 + toolContext 注入，闭包捕获是新引入模式（本身合理），应如实说明。同理 createAgent 只收 `debug: bool`，worker 只能继承开关而非 debugConsole 实例。

### P9【低】§4.2 schema JSON 笔误：结尾多一个 `}`

### P10【低】两个已知限制未声明

1. 主会话锁全程持有：execute 阻塞期间（最长 600s+）同会话新消息排队，Web 工具卡片长时间无反馈；
2. providerId 限流粒度 ≠ plan 粒度：同 plan 多 providerId 时按 providerId 信号量会漏，需注释说明。

## 三、论证通过、无需修改的点

- 递归防护（worker 不传 workersConfigPath → 物理无 dispatchTask）成立，与「dispatchTask 在 toolNames 过滤后追加」自洽；
- 确认转述后 resume 安全性（dangling 重放重新过权限评估）无破口；
- jsonl 日志原子追加无写竞争；createAgent 并发调用安全；
- 二期项只留接口不实现，整体克制，无过度设计。

## 四、结论

方向正确、选型论证扎实，但 P1/P2 会直接导致实施失败或线上行为与文档严重不符，另有 4 个中等级架构遗漏。需修订文档后再进入实施。

---

## 修订记录（2026-08-08，已全部落实回方案文档）

- P1 → §4.3-4 超时语义如实重写（非 daemon、继续跑完、进程退出等待）+ §4.4 聚合文本改「仍在后台执行直至完成」+ §4.3-2 补 shutdown 生命周期；
- P2 → §4.7 改为 2 个参数（workersConfigPath + maxModelSteps 透传），标题改「库层唯一被修改的现有文件」；
- P3 → §4.3-1 信号量显式构建 + 实施步骤 2 补「缺省不退化为 1」回归；
- P4 → §4.3-4 改 `as_completed` + 统一 deadline（从提交时刻起算）；
- P5 → §4.3-2 新增生命周期小节：getDispatcher per-process 缓存单例 + atexit shutdown；
- P6 → 新增 §4.9 启用入口（一期 CLI `--workers`，webApp 二期）；
- P7 → §4.3-3 tasks>16 整批 isError + §4.3-6/§4.4 单段 8000 字符截断、总量 30000 上限；
- P8 → 现状分析结论与 §4.3-1 如实改写（闭包为新引入模式；debug 开关而非实例透传）；
- P9 → §4.2 schema 修正；
- P10 → 新增 §4.10 已知限制（会话锁持有、providerId≠plan 粒度、超时不可强杀、配置变更需重启）。

> 恢复说明：本文档与 docs/orchestratorWorkerPlan.md 于 2026-08-08 被误删后按会话记录重建，内容与审核修复后的终版一致。
