# 当前工具调用并发语义深度调研报告

- 日期：2026-09-20
- 仓库：`/home/wilburwan/FlamingoAgents`
- 分支：`dev`
- 代码基线：`3bdce31c835c1cae9763c9efe8ca2d960883d6c8`
- 调研计划：[`plan/260920_toolCallConcurrencyResearchPlan.md`](plan/260920_toolCallConcurrencyResearchPlan.md)
- 调研方式：当前源码调用链、并发原语反向检索、Git blame/引入提交、fake adapter/tool 的 pytest 时序实验、全量回归

## 1. 直接结论

> **是：当前 FlamingoAgents 调度器对同一个 assistant message 中一批 tool calls 的 `executeToolCall` 调用与结果提交是严格串行的。**
>
> 它会先把连续可执行工具的 `toolCallStart` 全部发出去，所以 UI 可以同时显示多张 `running` 卡；但随后只在一个普通 `for` 循环里同步调用工具，前一个 `execute` 返回并写入结果后，才执行下一个。**“批量 Start”不是“并行 Execute”。** 若某个工具在返回前自行启动未等待的后台线程/外部作业，其内部活动之后可与下一工具重叠；那是工具内部并发，不是 Agent batch 调度并行。

需要按范围区分：

| 问题范围 | 当前结论 | 直接机制 |
|---|---|---|
| 同一 assistant batch 中多个 tool calls | **调度器串行调用并提交结果** | `driveToolBatch` 普通 `for` + 同步 `executeToolCall` |
| 批准一个待确认调用后，继续执行同批余项 | **调度器串行调用并提交结果** | 先同步执行当前 approved call，再调用 `driveToolBatch` 处理余项 |
| 同一 session 经公开流入口发起的多条 Core 流 | **串行等待** | 整条非终态流持有 per-session `RLock` |
| 已登记 active stream 的同一 session 再发 Web 流 | **不并发，也不排队；通常直接 409** | `activeStreams[sessionId]` 防重 |
| 不同 session 的 Web 流/工具 | **具备并发执行条件** | 每 session 独立 agent，且每个 stream pump 启动独立线程 |
| 单个工具实现内部自行开线程、子进程或 shell 后台任务 | **可以内部并发** | 属于工具自己的实现，不是 Agent 并行调度多个 tool calls |

因此，笼统地说“整个系统所有工具全局串行”是错的；准确说法是：

> **同批串行、同会话串行/拒绝重入、跨会话可并发。**

## 2. 最硬的当前源码证据

### 2.1 模型返回一批调用后，统一交给 `driveToolBatch`

`flamingoAgents/core/agent.py:344–374`：

1. 取出 `assistantMessage = completion.message`；
2. 没有 `toolCalls` 就完成；
3. 有调用则执行：

```python
terminated = yield from self.driveToolBatch(sessionId, assistantMessage.toolCalls, 0)
```

也就是说，adapter 可以交回一个包含多个调用的 list，但真正如何执行由 `driveToolBatch` 决定。

### 2.2 Start 事件先批量发，执行却是另一个串行循环

决定性代码在 `flamingoAgents/core/agent.py:376–408`：

```python
# 2) 前缀全部 Start
for call, definition in prefix:
    ...
    yield toolCallStartEvent(...)

# 3) 前缀串行 exec + End
for groupOffset, (call, definition) in enumerate(prefix):
    result = ... self.executeToolCall(call, sessionId)
    currentConversation.addToolResult(result)
    yield toolCallEndEvent(toolResult=result)
```

源码自己的注释在 `agent.py:379` 和 `agent.py:399` 分别明确写着：

- “先全部 yield Start，再**串行 exec + End**”；
- “前缀**串行 exec + End**”。

执行时序不是 `S1 → E1 → S2 → E2`，而是：

```text
S1 → S2 → ... → execute1 → E1 → execute2 → E2 → ...
```

所以前端可能同时看到多个 running 状态，但 `execute1` 与 `execute2` 没有因此重叠。

### 2.3 每次工具调用是同步函数调用，没有 Future/Task

`agent.executeToolCall` 在 `flamingoAgents/core/agent.py:516–526` 直接返回 runtime 的结果：

```python
return executeCallableToolCall(definition, call, context)
```

runtime 在 `flamingoAgents/tools/toolRuntime.py:44–57` 又直接同步调用：

```python
output = definition.execute(arguments, context)
...
return result
```

工具签名本身也是同步签名，见 `flamingoAgents/tools/toolDefinition.py:27,32–40`：

```python
toolExecuteFunction = Callable[[dict[str, Any], toolContext], toolOutput]
```

它不是 coroutine，也不返回 future。全仓生产代码检索还确认：

- `definition.execute(arguments, context)` 只有 `toolRuntime.py:47` 这一个调用点；
- `agent.executeToolCall(...)` 的生产调用点只有：
  - `agent.py:207`：批准确认的当前调用；
  - `agent.py:402`：batch 串行循环；
- `flamingoAgents/` 工具执行链中没有 `ThreadPoolExecutor`、`ProcessPoolExecutor`、`asyncio.gather`、`asyncio.create_task` 或 `TaskGroup`。

这是比注释更直接的结构性证据：**本地调度器没有创建并行执行单元。**

### 2.4 工具结果也在进入下一项前同步写入

仍在 `agent.py:400–408`：当前工具返回后先 `addToolResult`，再 yield End，循环才进入下一项。`conversation.addToolResult` 在 `flamingoAgents/core/conversation.py:190–204` 同步写 JSONL 并追加 conversation message。

因此当前落盘顺序同样是工具 1 结果在工具 2 结果之前；没有“并行执行、最后按原顺序汇总”的隐藏层。

## 3. 确认路径同样没有并行

批准路径位于 `flamingoAgents/core/agent.py:184–219`：

1. `agent.py:205`：发当前调用 Start；
2. `agent.py:207`：同步执行当前调用；
3. `agent.py:214–215`：写结果并发 End；
4. `agent.py:216`：当前调用结束后，才用 `driveToolBatch(... currentIndex + 1)` 处理余项。

因此若 batch 是 `[需批准 call1, 免确认 call2]`，批准后的工具事件顺序是：

```text
S1 → execute1 → E1 → S2 → execute2 → E2
```

拒绝路径则不执行当前工具，只合成一个 blocked End，再继续余项；它也不会触发并行。

## 4. `parallel_tool_calls: true` 为什么不是反证

这是最容易误判的地方。

### 4.1 Responses adapter 确实向 provider 开启该字段

`flamingoAgents/models/responsesAdapter.py:77–86` 的请求 payload 包含：

```python
'parallel_tool_calls': True,
```

在本实现里，这个字段是发给 provider 的**生成侧选项**，至多影响/允许上游在同一次响应中给出多个 function calls；adapter 没有据此创建本地并行执行单元，因此它不为本地 Python 工具并行提供任何保证。

### 4.2 adapter 只负责收集和排序，不执行工具

Responses adapter 在 `responsesAdapter.py:754–805` 按 `output_index` 对 slot 排序，并构造 `parsedToolCalls`。Chat Completions adapter：

- 在 `chatCompletions.py:548–561` 按 provider 给出的 `index` 聚合流式参数；
- 在 `chatCompletions.py:409–419` 按 index 排序合成 `tool_calls`；
- 在 `chatCompletions.py:599–630` 解析为 `list[toolCall]`。

两种 adapter 都止步于“得到有序调用列表”。随后仍进入第 2 节的同步 `driveToolBatch`。

### 4.3 Chat Completions 路径没有显式设置该字段

`chatCompletions.py:206–222` 的 payload 有 `tools` 与 `tool_choice: auto`，但没有显式写 `parallel_tool_calls`。即使某 provider 默认一次返回多个 calls，本地执行仍由同一个串行 batch 循环处理。

所以以下两句话可以同时为真，完全不矛盾：

1. provider 被允许一次生成多个 tool calls；
2. FlamingoAgents 收到后逐个执行。

## 5. 会话层的并发边界

### 5.1 Core：同 session 持同一把 `RLock`

`flamingoAgents/core/agent.py:110–136` 显示：

- `runUserMessageStream` 在 `with self.getSessionLock(sessionId)` 内驱动流；
- `continueConfirmationStream` 也持同一把锁；
- 仅终态事件在退出 `with`、释放锁后才 yield。

锁由 `agent.py:656–662` 按 `sessionId` 创建并缓存。因此，经 `runUserMessageStream` / `continueConfirmationStream` 这些受保护入口并发驱动同一 session 时，后一线程会等待前一流释放锁；该结论不外推到绕过这些入口、直接调用内部 `drive*` 方法的代码。

### 5.2 Web：同 session 第二条活跃流通常直接被拒绝

`webApp/backend/agentManager.py:119–128` 在 `managerLock` 内检查已登记的 active stream：

```python
if sessionId in activeStreams:
    return None
```

`webApp/backend/server.py:694–707` 将无法启动第二个 pump 映射为 HTTP 409（停止收尾有一次受控重试）。所以在同 session 已登记 active stream 的前提下，Web 的实际语义不是“两个工具线程在锁前排队”，而是**入口层先拒绝第二条活跃流**。

### 5.3 不同 session：运行层允许并发

`agentManager.py:43–60` 的缓存键是 `sessionId`，每个 session 懒建自己的 agent。`agentManager.py:145–174` 又为每个 stream pump 创建：

```python
threading.Thread(target=self._pump, daemon=True)
```

`agentManager.py:322–353` 在该线程中驱动整个 stream。`managerLock` 只保护登记/缓存等短临界区，不包围完整工具执行。因此不同 session 的泵线程可以同时进入各自的工具函数。

这只说明运行层**具备并发条件**，不保证任意 provider、文件、数据库或外部命令天然具备并发安全性。

## 6. 动态时序证据

### 6.1 方法

使用仓库外临时 pytest：`/tmp/testToolCallConcurrencyEvidence.py`。

- 文件 SHA-256：`2cd3c2f17d4a13e217884df50330eae0467bd245871350395248fb3ca876037e`；
- fake adapter：第一轮返回指定 tool calls，后续返回最终文本；
- fake tools：记录 `time.monotonic()`、线程名、enter/exit 和加锁保护的 `active/maxActive`；
- 无网络、无真实 provider、无生产源码改动；
- Barrier/Event/join 均有 timeout，避免测试挂死；
- 命令：

```bash
uv run pytest -q -s /tmp/testToolCallConcurrencyEvidence.py
```

### 6.2 实验 A：同 batch 两个慢工具

事件与时间线原始输出：

```text
EVIDENCE_A elapsed=0.1216s maxActive=1 events=['S:callA1', 'S:callA2', 'E:callA1', 'E:callA2']
EVIDENCE_A timeline=enter:batch1@0.0000s thread=MainThread active=1 |
exit:batch1@0.0601s thread=MainThread active=1 |
enter:batch2@0.0604s thread=MainThread active=1 |
exit:batch2@0.1205s thread=MainThread active=1
```

直接观察：

- 两张 Start 卡先出现；
- `batch2.enter` 晚于 `batch1.exit`；
- 两次执行均在同一驱动线程；
- `maxActive == 1`；
- 两个约 60ms 的工具总耗时约 122ms。

这直接证实当前 batch 执行没有时间重叠。

### 6.3 实验 B：不同 session 可重叠

两个不同 agent、不同 session、不同 session lock，通过 Barrier 同步进入工具：

```text
EVIDENCE_B maxActive=2 overlap=True
EVIDENCE_B timeline=enter:session1@0.0000s thread=crossSessionOne active=1 |
enter:session2@0.0001s thread=crossSessionTwo active=2 |
exit:session2@0.0602s thread=crossSessionTwo active=2 |
exit:session1@0.0604s thread=crossSessionOne active=1
```

两个区间明确相交，`maxActive == 2`。所以不存在覆盖所有 session 的全局工具执行锁。

### 6.4 实验 C：同 session 确实被 Core 锁挡住

测试用一个包装真实 `threading.RLock` 的观测锁区分 `attempted` 与 `acquired`。第一条流在工具 gate 内持锁时，第二线程已尝试获取但未获得：

```text
EVIDENCE_C beforeRelease secondAttempted=True secondAcquired=False adapterCalls=1 userMessages=1
EVIDENCE_C afterRelease secondAcquired=True adapterCalls=3 userMessages=2
```

释放第一条流后，第二线程才获得锁并进入 adapter。该实验直接验证 Core 锁；Web 409 是另一个更上层的限制，二者没有混为一个因果。

### 6.5 实验 D：批准确认后仍串行

```text
EVIDENCE_D maxActive=1 events=['S:callD1', 'E:callD1', 'S:callD2', 'E:callD2']
EVIDENCE_D timeline=enter:approvedCurrent@0.0000s thread=MainThread active=1 |
exit:approvedCurrent@0.0601s thread=MainThread active=1 |
enter:remainingFree@0.0604s thread=MainThread active=1 |
exit:remainingFree@0.1204s thread=MainThread active=1
```

这覆盖了另一条生产入口：`driveConfirmation` 先执行获批调用，结束后才续跑余项。

### 6.6 稳定性与回归

临时证据测试结果：

```text
4 passed in 0.53s
```

随后无输出模式连续重复 5 次：

```text
4 passed in 0.52s
4 passed in 0.52s
4 passed in 0.53s
4 passed in 0.52s
4 passed in 0.52s
```

项目全量回归：

```bash
uv run pytest -q
```

结果：

```text
221 passed in 11.09s
```

## 7. Git 历史证明这是有意设计，不是偶然现象

当前批量 Start 结构由提交 `1250f868d1fcc64878018480ca74347906aea5dc` 引入。提交标题是：

```text
修复流式展示迟钝：read1 + 批量 Start + 前端 paint 合并
```

其提交正文明确写：

```text
driveToolBatch 可执行前缀批量 Start：同批工具先全部 running 再串行 exec
```

`git blame` 显示 `agent.py:377–399` 的前缀收集与批量 Start 主要来自该提交；后续提交只在执行循环加入中断闭环，没有把它改为并行 executor。

对应设计文档也明确：

- `docs/plan/streamingLatencyFixPlan.md:38–46`：工具 batch 并行执行是非目标，仍串行；
- 同文件 `224–230`：选定“先全部 Start，再串行 exec”，明确否决“并行 exec”；
- 同文件 `615–620`：实施项勾选“前缀串行 exec + End”。

README 当前路线图 `README.md:164–172` 仍将“并发作业派发”标记为未完成，也与现状一致。

历史不能替代当前源码与运行实验，但它证明当前结构是明确选择，而非遗漏了一个不易看见的并行层。

## 8. 容易造成误判的现象

| 现象 | 能证明什么 | 不能证明什么 |
|---|---|---|
| Responses payload 有 `parallel_tool_calls: true` | 本实现向 provider 声明生成侧可接受并行/多个 calls | 本地工具同时执行 |
| UI 同时出现多张 running 卡 | 多个 Start 被预先广播 | 多个 execute 时间区间重叠 |
| Web 使用线程 | 不同 stream 可在不同线程驱动 | 同一 batch 自动拆成多线程 |
| `bash` 运行一个内部含 `&` 的命令 | 该 shell 命令内部可能并发 | Agent 并发调度两个 `bash` tool calls |
| `askSubAgent` 启动子进程 | 单个工具内部运行一个子进程 | 同一 batch 的多个 `askSubAgent` 同时启动 |
| 本对话宿主提供 `multi_tool_use.parallel` | 宿主包装器可并行发多个开发工具调用 | FlamingoAgents 仓库已实现并行 batch executor |

特别是最后一项：`multi_tool_use.parallel` 是当前对话 API 提供的外层工具，不在本仓库 `config/tools.yaml` / `driveToolBatch` 实现内。本报告结论针对 FlamingoAgents 当前代码。

## 9. 最终判断

如果问题指的是“模型一轮同时给出两个 `read` / `bash` / `askSubAgent` 调用，FlamingoAgents 会不会同时跑它们”，答案是：

> **不会由 Agent batch 调度器同时启动。当前调度器一定逐个同步调用并提交结果。**

直接因果链是：

```text
provider 一次产出多个 calls
→ adapter 按 index/output_index 组装有序 list
→ driveModelLoop 把整批交给 driveToolBatch
→ driveToolBatch 先广播所有可执行 Start
→ 普通 for 循环同步 execute 第 1 个并写结果
→ 再 execute 第 2 个
→ ...
```

把任务放在**不同 session 的不同 stream pump**时可出现执行重叠；某一个工具实现也可自行创建后台并发，但这不改变 batch scheduler 的串行调用语义。当前核心没有“同批 tool calls 并行执行器”。
