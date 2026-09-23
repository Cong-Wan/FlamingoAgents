# askSubAgent“卡住”故障调研报告

- 日期：2026-09-23
- 调研仓库：`/home/wilburwan/FlamingoAgents`
- 当前 commit：`db2d163ae88f7368491299c0b37c8a9e643e6f46`
- 调研对象：用户粘贴的另一台机器子代理 JSONL；本机当前实现；本轮受控复现
- 结论强度：**实现缺陷已证实；用户那一条事件的唯一根因仍待源文件/父日志补证**

---

## 1. 执行摘要

### 1.1 最重要的结论

当前“派发子代理会卡住”不是一个单点问题，而是三层叠加：

1. **已证实的核心实现缺陷：父进程在子进程仍存活时完全不读取 stdout/stderr 两根 PIPE。**
   - `_runWithInterrupt()` 在 `while process.poll() is None` 中只检查中断、deadline、然后 `sleep(0.1)`；直到 child 退出后才调用 `communicate()`。
   - 本机 PIPE 容量为 **65,536B**。
   - 受控实验：写 stderr 60,000B 时 0.101s 完成；写 70,000B/200,000B 时都卡到 timeout，捕获量恰为 65,536B；同一 200,000B 改写普通文件只需 0.016s。
   - 真实 `askSubAgentTool → fakeSdk` 路径同样复现：60,000B 成功，70,000B 超时。
   - stdout 也一样：最终 JSON reply 超过 PIPE 容量同样会死锁。

2. **已证实的“假死放大器”：askSubAgent 是同步黑盒等待。**
   - 默认父级 timeout 是 **600s**，上限 3600s；等待期间父代理没有 child heartbeat、step、sessionId 或阶段进度。
   - child 内部网络 I/O timeout 是 **300s**，agent 单个逻辑 step 最多可做 4 个物理 attempts；父级总 deadline 与内部预算不协调。
   - `sdkEntry.py` 又明确设置 `maxModelSteps = -1`，复杂任务可以无限“读文件 → 再问模型 → 再读文件”，直到最终回复或父级 timeout。

3. **超时后诊断被主动丢弃。**
   - `_finishStop()` 已经把 partial stdout/stderr 放入 `TimeoutExpired`；但 `askSubAgentTool()` 捕获异常时不读取这些字段，只返回“子代理超时被终止（Ns）”。
   - 父级 toolResult 也不包含 child sessionId、child JSONL 路径、最后模型阶段或最后 activity 时间。
   - 因此同一个用户现象既可能是 provider/socket 长静默，也可能是 child 输出写满 PIPE，父日志表面都只显示“卡住”。

### 1.2 对用户给出的那份 JSONL，能确定什么

能确定：

- 子代理并非一启动就卡住；前 3 个逻辑 model step 和 12 个工具调用都完成了。
- 第 4 条 `modelRequestStart` 是一个新的逻辑 step 的 **`attempt=1`**，不是同一请求的第 4 次 retry。
- 粘贴片段在该 start 后没有同 `usageKey` 的 `usageRecord`、`assistantMessage` 或 `modelError`；所以直接断点位于 `modelAdapter.completeStream(...)` 调用窗口内。
- 卡点不是那一轮 read/bash 工具执行，也不是“usage 已返回后写 assistant 卡住”。

不能确定：

- 用户贴出的只是片段，不是源 JSONL 的真实 EOF；没有父会话 toolResult，也没有卡住持续时长。
- 仅凭 `modelRequestStart` 无法进一步区分 connect、firstByte、streamRead、provider 推理，或 child 在收到大量增量后被 stderr PIPE 反压。
- 用户样本在第 4 次请求前，按现有日志可计算的 stderr 严格上界约 **5,927B**，远低于 65,536B。因此“前三轮累计输出已经把 PIPE 填满”可以排除；但第 4 次尚未落盘的推理输出可能继续写满 PIPE，因其未形成 assistantMessage，现有片段无法测量。

### 1.3 当前最准确的根因表述

> **产品级根因已经证实：askSubAgent 的父进程等待器不排空活 child 的 stdout/stderr，且同步等待最长 600 秒、不透传进度、超时丢诊断；复杂/长推理子代理因此既可能真的被 64KiB PIPE 反压锁死，也会把正常的长网络静默表现成不可诊断的“卡住”。**
>
> **对用户粘贴的单次事件，直接证据只足以定位到第 4 个 `completeStream` 窗口；PIPE 与 provider/socket 静默仍是竞争解释，不能在缺源 EOF、父 toolResult、两管字节和进程栈时二选一。**

### 1.4 Git 历史核查：旧修复存在，但修的是另一条 PIPE 路径

用户记忆正确。仓库中确有提交：

```text
3bdce31c835c1cae9763c9efe8ca2d960883d6c8
2026-09-17 16:37:27 +08:00
修复 bash 管道死等卡住会话
```

该提交仍被当前 `dev`、`origin/dev` 包含，没有被回滚。它修改的是 bash 与 askSubAgent 共用的 `_runWithInterrupt()`，所以标题虽写 bash，修复范围也覆盖 askSubAgent。

但它修复的事故条件是：

```text
Popen 首领已经退出
→ 子孙仍持有 stdout/stderr 写端
→ 父进程进入无界 communicate() 等 EOF
```

本次新发现的机制是：

```text
Popen 首领仍然存活
→ 首领自己向 stdout/stderr 写满 65,536B
→ 首领阻塞，无法退出
→ 父进程只 poll、不读 PIPE，直到工具 deadline
```

旧方案对这个边界有明确文字，并非后来才遗漏：提交内的
`docs/plan/260917_bashPipeHangSessionLockFixPlan.md:42` 把以下内容列为“非目标”：

> 执行中持续排空管道以防 64KiB 填满；现状 poll 循环也不读管道；大输出仍靠 timeout 杀。不改成 select 循环。

对应测试也只覆盖首领超时、首领退出后孤儿占管、忽略 SIGTERM、中断、短命令与锁释放；没有“首领存活并写入 >64KiB”的用例。当前 7 项旧测试全部通过，说明旧修复仍然有效，但不能覆盖新路径。

`git diff 3bdce31..HEAD -- flamingoAgents/tools/builtinTools.py` 只显示后续提交 `790f126` 更新文件头，并给 askSubAgent 增加 `--usage-source subagent` 与 `--parent-session-id`；`_runWithInterrupt()` 等待逻辑没有被后续提交改回去。因此结论是：

- **不是旧修复被回滚或发生代码回归；**
- **是旧修复有意只处理“首领已退出后的无界等待”，而“活首领写满 PIPE”当时明确留作非目标；**
- 旧修复保证后者最终会在工具 timeout 后返回，因此当前通常是默认等待约 600 秒的“有界长卡”，不是旧事故那种永久卡在无界 `communicate()`。

相关提交时间线：

| commit | 日期 | 与问题的关系 |
|---|---|---|
| `cdfc872` | 2026-08-12 | 引入 askSubAgent 与 sdkEntry |
| `b9719bc` | 2026-08-13 | timeout 可配置（默认 600s、上限 3600s），并把 maxModelSteps 设为 -1 |
| `e49de66` | 2026-08-14 | 引入 Popen + poll 的可中断等待；活 child 阶段不读 PIPE 的结构从此存在 |
| `3bdce31` | 2026-09-17 | 修复首领退出后子孙占 PIPE 导致的无界 communicate/wait |
| `790f126` | 2026-09-21 | 只增加子代理用量来源与父 session 关联；未改等待逻辑 |

---

## 2. 用户样本：逐事件证据

用户消息内共粘贴了 24 条 JSONL 事件。以下是可复核的状态机摘要。

| 逻辑 step | `modelRequestStart` UTC | attempt | messageCount | contextTokens | 终态 usage | assistant | 工具结果 | 耗时/结果 |
|---|---:|---:|---:|---:|---|---|---|---|
| 1 | 10:01:42.322985 | 1 | 2 | 0 | prompt 1,886 / completion 250 | 有，3 calls | read/read/bash 全返回 | 模型 7.823s |
| 2 | 10:01:50.478461 | 1 | 6 | 2,136 | prompt 19,586 / completion 267 | 有，4 calls | 4 项全返回 | 模型 6.852s |
| 3 | 10:01:57.558756 | 1 | 11 | 19,853 | prompt 31,747 / completion 194 | 有，5 calls | 5 项全返回 | 模型 8.193s |
| 4 | 10:02:05.781023 | **1** | 17 | 31,941 | **无** | **无** | 尚未产生新 call | 粘贴片段在此结束 |

### 2.1 直接证据 1：不是启动失败，也不是首轮工具卡住

前三条成功链都完整：

```text
modelRequestStart
→ usageRecord（同 usageKey）
→ assistantMessage（带 toolCalls）
→ 所有 toolResult
→ 下一条 modelRequestStart
```

这说明：

- sdkEntry 子进程成功启动；
- GLM 认证与至少前三次 HTTP/SSE 请求成功；
- read/bash 工具执行和 JSONL 写入均成功；
- 父子会话至少运行了约 23.46 秒。

因此“子代理根本没启动”“第一轮工具没返回”“模型配置立即报错”都与日志不符。

### 2.2 直接证据 2：第 4 条是新 step 的首个 attempt

四条 start 的 `attempt` 都是 1，且 `messageCount` 依次是 2、6、11、17。`agent.py:403-425` 显示 attempt 在每个逻辑 step 内从 1 重新计数；工具跑完后 `stepIndex += 1` 再进入下一轮（`agent.py:544-562`）。

所以准确说法是：

- **第 4 个逻辑 model step 的 attempt 1 未闭合**；
- 不是“一个请求连续 retry 到 attempt 4”；
- `attempt` 是一个逻辑 step 内的物理调用计数。只有 child 内模型调用先返回可重试异常、且尚未见任何 text/reasoning chunk，才会出现 attempt 2（`agent.py:458-510`）。本例没有后续 attempt，说明片段结束/父级杀进程前异常尚未回到该 retry 分支；父级 askSubAgent timeout 本身在 child 进程之外，直接杀 child，不会替 child 写 attempt 2。

### 2.3 直接证据 3：断点位于 completeStream 窗口

代码顺序：

```text
agent.py:415-425  先写 modelRequestStart
agent.py:433-447  调 completeStream 并消费 chunk
agent.py:479-485  只有异常被捕获后才写 modelError
agent.py:515-520  正常 final 后才写 usageRecord
agent.py:530/564  再写 assistantMessage
```

用户片段最后只有 start，没有 `modelError` / usage / assistant，因此断点只能定位为：

```text
modelRequestStart 已落盘
→ completeStream 尚未形成可持久化异常或 finalChunk
```

**不能仅靠这一点继续细分阶段。** `modelError.stage` 只在异常返回后才落盘；当请求仍阻塞或父进程直接杀 child 时，它不会出现。

### 2.4 可限定排除的分支

| 分支 | 判定 | 依据 |
|---|---|---|
| 第 3 轮 read/bash 仍未完成 | 排除 | 第 19–23 条五个 toolResult 均已落盘，随后才有第 24 条 start |
| usage 已到达、卡在 usage/assistant 落盘 | 排除 | 第 4 usageKey 没有 usageRecord |
| provider 返回可识别 HTTP/SSE 错误且异常处理已运行 | 片段内排除 | 若异常被捕获，`logModelError()` 会写 modelError（`agent.py:458-485, 1186-1223`） |
| 源文件之后最终恢复/报错 | **不能排除** | 用户给的是拷贝片段，不是经 `tail`/`wc` 证明的 EOF |

---

## 3. 已证实缺陷一：活 child 的 PIPE 回压死锁

### 3.1 源码证据

`flamingoAgents/tools/builtinTools.py:232-266`：

```python
process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    ...
)
while process.poll() is None:
    # 只查 interrupt/deadline
    time.sleep(0.1)
# child 退出之后才 communicate()
stdout, stderr = process.communicate(...)
```

这里有两根独立 PIPE。只要 child 在退出前向任一管写满，写调用就阻塞；child 不能退出，父进程的 `poll()` 就永远仍是 `None`；父进程又要等 child 退出才开始读，形成闭环：

```text
child 写满 PIPE → child 等父读 → child 不能退出
父 poll 等 child 退出 → 父不读 PIPE
```

这不是猜测。历史修复计划自己明确把它列为未解决项：

- `docs/plan/260917_bashPipeHangSessionLockFixPlan.md:42`：
  “**执行中持续排空管道以防 64KiB 填满**……现状 poll 循环也不读管道；大输出仍靠 timeout 杀。”

### 3.2 确定性边界实验

本机：

```text
F_GETPIPE_SZ = 65536
```

同一 `_runWithInterrupt(timeout=1)`：

| producer | 结果 | 耗时 | 父捕获量 |
|---|---|---:|---:|
| stderr 写 60,000B | completed | 0.101s | 60,000B |
| stderr 写 70,000B | TimeoutExpired | 1.502s | **65,536B** |
| stderr 写 200,000B | TimeoutExpired | 1.502s | **65,536B** |
| 200,000B 写普通文件 | completed | **0.016s** | 文件 200,000B |

双管并发实验：

| producer | 当前实现 | 持续 `Popen.communicate()` 对照 |
|---|---|---|
| stdout 70,000B + stderr 70,000B | 1.502s timeout；两管各捕获 65,536B | 0.017s 完成；两管各 70,000B |

这组 A/B 唯一变化是父进程是否持续读取，足以证明回压机制。

### 3.3 真实 askSubAgentTool 路径实验

将 `sdkEntryPath` 临时指向本地 fakeSdk（不改仓库文件），fakeSdk 先写 stderr，再输出最终 JSON：

| fakeSdk stderr | askSubAgentTool 结果 |
|---:|---|
| 60,000B | 0.101s，成功返回 `reply=ok` |
| 70,000B | 1.502s，`子代理超时被终止（1s）` |

stdout 最终回复也同样受限：

| JSON reply 大小 | 结果 |
|---:|---|
| 60,000B | 成功 |
| 70,000B | timeout，父只返回 13 字通用错误 |

因此不仅 reasoning stderr 会触发；**最终正文超过约 64KiB，也可能在 child 打印最终 JSON 时卡死。** 这与模型配置中 `maxTokens` 很大却没有在请求 payload 下发输出上限共同放大风险：`modelConfig` 甚至没有 `maxTokens` 字段，而 `chatCompletions.buildRequestPayload()` 也不发送 `max_tokens`。

### 3.4 为什么用户样本不能直接归因给 PIPE

根据用户粘贴事件，前三轮 child stderr 的可计算严格上界：

- reasoning UTF-8：1,130B；
- text：272B；
- 12 个 tool start（含完整 preview）：1,230B；
- 12 个 tool end：276B；
- 每轮 completed 换行：3B；
- 即使把前三轮所有 377 个 SSE data chunk 都错误地当作 reasoning delta，每个 ANSI 包装按 8B，额外也仅 3,016B。

合计上界约 **5,927B**，远小于 65,536B。这个上界适用于所示源码的 `--json + quiet=True + debug=False` 路径：`sdkEntry.py:43-60` 在 child 运行期间写 stderr 的项目只有 reasoning/text、tool start/end、error/completed 行；read/bash 的真实内容作为 toolResult 进对话，不直接打印。`sdkEntry.py:153-154` 只有在 `runSdk()` 已返回后才向 stdout 打最终 JSON，因此第 4 次请求开始前 **stdout 为 0B**。Python 本身无 banner，当前调用也未开启 debug；若另一机器改过入口或依赖向标准流额外写日志，此排除需按实际版本重算。

故：

- 在用户所贴版本与事件范围内，“前三轮累计 stdout/stderr 已经填满 PIPE”可排除；
- 第 4 轮若产生了超过约 59.6KiB 的尚未落盘 reasoning/text，则仍可触发；
- 但这些增量因 child 没有形成 assistantMessage，单靠 child JSONL 无法恢复。

这就是为什么报告把 PIPE 定为**已证实产品缺陷/本次竞争根因**，而不是武断写成用户单次事件的唯一根因。

---

## 4. 已证实缺陷二：同步黑盒等待与期限不协调

### 4.1 父级最长可静默 600 秒

- `builtinTools.py`：`defaultSubAgentTimeoutSeconds = 600`，最大 3600。
- `config/tools.yaml:102-127` 也向模型声明默认 600 秒。
- `askSubAgentTool()` 同步调用 `_runWithInterrupt()`；返回前不会产生 toolResult。
- `agent.runUserMessageStream()` 在整个 drive 期间持有 session `RLock`（`agent.py:158-190`）。

受控锁实验：70KiB fakeSdk 触发 1s timeout 时，owner 持锁 1.502s，另一线程实际阻塞 1.402s。生产默认值下，这种不可用窗口可达约 600s。

### 4.2 child 内部不是 600 秒内必然结束

- `chatCompletions.py:242-244` 使用 `urlopen(..., timeout=300)`。
- 这个 timeout 是 socket 阻塞 I/O timeout，不是整条 SSE 的 wall-clock deadline；若周期性收到数据，流可持续超过 300 秒。
- 无 chunk 的可重试错误最多 4 attempts（`agent.py:403, 458-510`），基础退避 1/2/4 秒；provider `Retry-After` 还可拉长。
- 父级 600 秒是整个 child 的硬期限，可能在 child 内部完成 retry 诊断之前先杀进程。

### 4.3 sdkEntry 允许无限模型步骤

`sdkEntry.py:108`：

```python
flamingo.maxModelSteps = -1
```

而 `agent.py:378-388` 将 `None` 或 `<=0` 解释为“不限制”。所以复杂审核任务会持续：

```text
模型 → 工具 → 模型 → 工具 → ...
```

不存在 step 数、工具结果总字节或上下文预算的程序级上限，仅有父进程 wall-clock timeout。

同机首个计划审核样本提供了直接证据：

- 240.214s 总期限内启动 10 个 model step，前 9 个完成，最后一个未闭合；
- 最后 attempt 只运行了 126.474s 就被父级总 deadline 杀掉；
- 这不是单次 300 秒 socket timeout，而是前面多轮消费掉父级预算。

### 4.4 上下文快速膨胀是放大因素，不是充分根因

用户样本 prompt tokens：

```text
1,886 → 19,586 → 31,747 →（下一次 start 的 contextTokens=31,941）
```

首轮工具直接读入 18,347B 方案和 27,573B 探针；后续又读多个整文件。工具结果会作为 tool message 原样进入下一次请求（`conversation.addToolResult()`），所以 prompt 快速变大是源码必然结果。

但 31,747 token 远低于该机器日志中声明的百万上下文；仅凭 token 数不能证明 provider 卡死。它合理解释：

- 请求体更大、首字节/推理可能更慢；
- 多轮执行更容易耗尽父 deadline；
- 模型有更多 reasoning/正文可输出，更容易触发 PIPE。

所以它是风险放大器，不替代 PIPE 或网络根因。

---

## 5. 已证实缺陷三：超时诊断丢失

### 5.1 partial stdout/stderr 已经取得，却被 askSubAgent 丢掉

`_finishStop()` 在 `builtinTools.py:204-229` 把 partial output 写入 `TimeoutExpired.output/stderr`。

但 `askSubAgentTool()` 在 `builtinTools.py:391-400`：

```python
except subprocess.TimeoutExpired:
    return toolOutput(
        content=f'子代理超时被终止（{timeout}s）。',
        details={'timeout': ..., 'model': ...},
    )
```

没有读取：

- `error.stdout`；
- `error.stderr`；
- child sessionId；
- child JSONL path；
- 最后 activity/model stage。

fakeSdk 70KiB 实验中父进程底层已捕获 65,536B，但最终 toolResult 只剩通用 13–15 字错误，直接验证该丢失。

### 5.2 modelRequestStart 不是足够的阶段诊断

`modelRequestStart` 写在 `completeStream()` 前。只有请求抛异常并回到 agent 后，才会落 `modelError`，其中才可能有 `stage=connect/firstByte/streamRead/decode`。

若发生：

- socket 仍阻塞；
- provider 持续推理；
- child 已在写 stderr PIPE 时阻塞；
- 父级 deadline 直接 SIGTERM/SIGKILL；

则都不会产生 `modelError`。因此 JSONL 最后一条 start 只是“请求窗口未闭合”，不是网络阶段证明。

---

## 6. 本轮同机子代理样本说明了什么

本轮按规则做计划审核时产生了额外样本：

| provider/model | 父 timeout | child 轨迹 | 结果 |
|---|---:|---|---|
| glm/glm-5.3 | 240s | 9 个 step 完成，第 10 start 后 126.5s 被杀 | timeout |
| subGPT/gpt-5.6-sol | 240s | 第 1 step+read 完成，第 2 start 后 236.3s 被杀 | timeout |
| volcano/glm-5.3 | 180s | 单次无工具审核在 136.4s 完成，reasoning 13,885 chars | 成功 |
| DS_Offical/deepseek-flash | 180s | 单次无工具审核 7.4s 完成 | 成功 |
| volcano/glm-5.3 | 120s | 仅留 modelRequestStart，父 deadline 前未完成 | timeout |
| volcano/glm-5.3 | 180s | 后续同类终审 88.9s 完成 | 成功 |

这些样本说明：

1. 长 reasoning/长首轮在多个 provider 上都可能超过 120–240 秒，固定短 timeout 本身会制造失败；
2. 多轮工具任务更容易先耗尽父总预算；
3. 不同 provider 的成功/失败波动大，不能只归咎 GLM；
4. 当前日志仍无法区分失败样本中有多少是网络静默、有多少是 PIPE——正是可观测性缺口。

注意：这些是真实模型样本，不是严格对照实验；不能用来计算 provider 可靠性。

---

## 7. 假设判定矩阵

| 假设 | 判定 | 最直接证据 | 尚缺什么 |
|---|---|---|---|
| 用户样本停在第 4 step 的 completeStream 窗口 | **已证实（片段范围）** | 第 24 条 start；无同 key 后继；代码写入顺序 | 源 JSONL EOF/后续事件 |
| 父同步等待、默认 600s、无 heartbeat | **已证实** | `builtinTools.py:363-400`；schema 默认；父日志只有最终 toolResult | 无 |
| 活 child PIPE 回压缺陷 | **已证实** | 源码 poll；65,536B A/B；fakeSdk askSubAgent 实验 | 无（机制层） |
| 用户单次事件就是 PIPE | **待补证** | 第 4 轮输出未知；前三轮上界仅 5,927B | timeout 前两管字节、wchan/strace、持续排空 A/B |
| provider/网络在第 4 轮长静默 | **待补证** | start 后无终态与此相容 | modelError diag、socket/strace、完整持续时长 |
| 第 3 轮工具卡住 | **已排除（片段范围）** | 五个 toolResult 均已落盘，随后才 start | 无 |
| usage/assistant 持久化卡住 | **已排除（片段范围）** | 第 4 key 连 usage 都没有 | 无 |
| 无限 model steps 放大超时 | **已证实** | `maxModelSteps=-1`；同机 10-step 样本 | 无 |
| 上下文膨胀是唯一根因 | **不成立/仅放大因素** | token 增长真实，但仍在配置上下文内 | provider 侧延迟数据 |
| 历史“首领退出、孤儿占管”旧 bug复发 | **当前版本限定排除** | `testRunWithInterrupt.py` 7 passed；现有有界收尾 | 另一机器 commit 是否含修复 |

---

## 8. 对另一台机器的最小补证清单

由于用户样本不是完整源文件，以下证据能直接把竞争根因拆开。

### 8.1 静态证据（进程结束后仍可取）

```bash
# 1. 确认源文件真实 EOF、大小、mtime（替换路径）
wc -l -c /path/to/child.jsonl
stat /path/to/child.jsonl
tail -n 20 /path/to/child.jsonl

# 2. 找父会话中对应 askSubAgent 的开始与 toolResult
# 注意脱敏 prompt/API key
python - <<'PY'
import json
from pathlib import Path
p = Path('/path/to/parent.jsonl')
for i, line in enumerate(p.open(), 1):
    e = json.loads(line)
    if e.get('type') in ('assistantMessage', 'toolResult'):
        print(i, e.get('timestamp'), e.get('type'), e.get('toolName'), e.get('isError'), e.get('details'))
PY

# 3. 记录实际版本
git rev-parse HEAD
python --version
```

### 8.2 卡住当时的现场（最有判别力）

```bash
ps -eo pid,ppid,pgid,sid,stat,etimes,wchan:32,args | grep -E 'sdkEntry.py|webApp' | grep -v grep

# child PID 替换 $PID
cat /proc/$PID/wchan
ls -l /proc/$PID/fd/1 /proc/$PID/fd/2
cat /proc/$PID/fdinfo/1 /proc/$PID/fdinfo/2
ss -tpn | grep "pid=$PID," || true

# 若允许短时 attach（5 秒即可）
timeout 5 strace -f -tt -p $PID -e trace=write,sendto,recvfrom,poll,ppoll,select,pselect6,read
```

判据预注册：

- **支持 PIPE**：child 在 `write(1|2, ...)` 阻塞；fd 指向 pipe；任一管已接近容量；改为持续排空后同任务完成。
- **反对 PIPE、支持网络读等待**：stdout/stderr 远低于容量，child 阻塞 `recv/read/poll` 在模型 socket；之后约 I/O timeout 出现 `modelError.stage=firstByte/streamRead`。
- **支持父总 deadline**：父 toolResult 的 timeout 时间与 askSubAgent 开始严格接近配置 deadline，child 最后一 attempt 年龄小于内部 I/O timeout；本轮首个审核样本即是这种形态。

### 8.3 一个关键限制

Linux 不提供“过去某时刻 PIPE 中写了多少字节”的通用事后日志。当前 `askSubAgentTool` 又丢 partial output，所以如果进程已被杀、现场未抓，用户那一次事件可能无法事后唯一归因。这不是再分析 JSONL 就能弥补的，需要先修可观测性再复现。

---

## 9. 修复建议（按优先级）

> 本报告只调研，不修改生产代码。以下是后续实施建议。

### P0：持续排空 stdout/stderr，消除确定性死锁

不要继续 `poll()` 到退出后才 `communicate()`。可选最小方案：

- 在 child 存活阶段用 `selectors` 非阻塞读取两管；或
- 两个 reader thread 持续 drain 到有界 spool 文件/ring buffer；或
- stdout 只保留最终结构化结果，stderr 直接写 child 日志文件，父只监控进度文件/IPC。

必须同时限制内存：不能简单无界 `communicate` 累积所有输出。

验收：

1. stdout、stderr 各写 10MiB（单独及并发）都完成，不因 64KiB 卡住；
2. timeout/interrupt 仍在既有时间预算内杀进程组；
3. 最终 stdout JSON >64KiB 可成功返回，或明确由协议级大小上限拒绝，而不是 timeout；
4. 现有 `testRunWithInterrupt.py` 全过。

### P0：超时必须返回 child 身份与 partial 诊断

askSubAgent toolResult 至少应包含：

```text
childSessionId
childLogPath
startedAt / lastActivityAt
lastModelStage
modelStep / attempt
stdoutBytes / stderrBytes
partialStderrTail（有界、脱敏）
timeoutSource = parentDeadline | modelIo | pipeDrain
```

`askSubAgentTool` 捕获 `TimeoutExpired as error`，不能丢 `error.output/error.stderr`。

验收：任意 timeout 后，不打开进程表也能从父 toolResult 唯一找到 child JSONL，并判断最后一条事件和输出字节。

### P1：建立父总 deadline、attempt deadline 与 step 预算

建议分离：

- task 总 deadline；
- 单 model attempt deadline；
- 最大 model steps；
- 最大累计工具结果字节；
- 最大上下文 token。

不要在 sdkEntry 无条件 `maxModelSteps=-1`。审核类任务可以显式给较大值，但必须有限。

验收：构造模型持续调用工具时，在指定 step/总预算处返回明确的 `maxStepsExceeded` / `deadlineExceeded`，而不是笼统“子代理超时”。

### P1：缩小 session 锁覆盖范围

当前 `runUserMessageStream()` 在完整模型循环和工具执行期间持有同一 session 的 `RLock`。因此 askSubAgent 等 600 秒时，同会话后续请求也会等待。后续设计应把长时 child 执行移出会话互斥区，使用持久化的 in-flight tool ledger / 状态机，在重新入锁后原子提交唯一结果；不能从其他线程强行释放 `RLock`，也不能允许同一 tool call 被重复执行。

验收：一个 askSubAgent 正在运行时，同会话的 stop/状态查询能及时响应；第二条用户消息得到明确“任务运行中”或排队状态，而不是无可见状态地阻塞；取消、timeout、正常完成都只落一条 toolResult。

### P1：父级透传 heartbeat/进度

每次 child 的以下活动都应更新父级：

- modelRequestStart；
- 首 chunk；
- tool start/end；
- usage/assistant terminal；
- retry/backoff。

父 UI 至少显示“第 N 步、最后活动距今、当前阶段”，避免正常长推理被理解为冻结。heartbeat **只改善观测，不负责恢复**；真正终止静默任务仍依赖父总 deadline、单 attempt deadline 和可中断 I/O。

### P1：收敛 child 输出协议

当前 `--json` 名义上“stdout 仅一行 JSON”，但最终 JSON 可以任意大；stderr 又承载完整 reasoning 增量与工具 preview。建议：

- child stdout 只输出小型 envelope（状态、sessionId、结果文件路径）；
- 大正文写结果文件，父按大小上限读取；
- reasoning 默认不逐 token 写 PIPE，可写 child log 或仅做节流进度；
- 结果协议严格校验 reply 类型，非法/缺失 JSON 不得返回字符串 `None`。

### P2：减少无界整文件读取

- read 用 offset/limit 分段；
- 先 grep/索引再读取目标区间；
- 给单 toolResult 和累计 toolResult 设字节预算；
- 超预算时让模型摘要或引用文件路径。

这能降低上下文、模型时延与输出量，但**不能替代 P0 PIPE 根修**。

---

## 10. 验证记录

| 检查 | 结果 |
|---|---|
| `uv run python -m pytest tests/testRunWithInterrupt.py -q` | **7 passed in 7.05s** |
| `uv run python -m pytest tests/testModelStreamDiag.py -q` | **20 passed in 0.23s** |
| PIPE 容量 | **65,536B** |
| 60KB/70KB/200KB 边界 | 60KB 成功；70/200KB timeout，捕获 65,536B |
| 文件 A/B | 200KB 文件写 0.016s 完成 |
| 双 PIPE A/B | 当前 timeout；持续 communicate 0.017s 完成 |
| askSubAgentTool fakeSdk | 60KB 成功；70KB timeout且父级丢 partial stderr |
| 会话锁实验 | owner 持 1.502s；waiter 阻塞 1.402s |

---

## 11. 最终回答“为什么现在会卡住”

简明回答：

1. **代码里确实有一个可稳定复现的卡死条件**：父进程等子进程退出时不读它的 stdout/stderr；任一输出超过约 64KiB，子进程就卡在写管道，父进程又卡在等子进程，只能熬到 askSubAgent timeout。
2. **即使没写满管道，当前设计也会看起来像卡死**：父进程同步等待最多 600 秒，内部模型单次网络阻塞可到 300 秒，并允许无限 model steps，期间父级没有心跳。
3. **超时后系统把最关键证据丢了**：partial stderr、child sessionId、child 日志路径都没返回，所以所有原因在父界面都退化成同一句“子代理超时”。
4. **你贴的那一例**能确定是第 4 个 GLM `completeStream` 尚未闭合；前三轮输出远未填满管道。第 4 轮究竟是 provider/socket 静默，还是它在未落盘的大量 reasoning 中填满 PIPE，必须用完整 child EOF + 父 toolResult + 卡住时进程/两管现场才能定案。
