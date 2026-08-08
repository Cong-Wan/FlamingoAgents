# 流式输出（Streaming）方案设计

> Author: wilbur
> Version: 2.4
> Date: 2026-08-08
> 目的：为 `flamingoAgents` 的 LLM 请求增加流式输出能力。
> v1.1：按 `docs/codeReview/260725_streamOutputPlan.md` 审核报告修订 12 处。
> v1.2：落档用户已拍板决策——思维链也要流式（独立 `onReasoning` 回调）；保留 `modelConfig.stream = False` 回退开关。
> v2.0：**用户已拍板选定方案 C（事件流）**，为后续 TUI/拓展铺路；新增 §6 方案 C 详细设计。
> v2.1：按 `docs/codeReview/260725_streamOutputPlanV2.md` 审核报告修订 11 处——**适配器层改为迭代器接口 `completeStream()`**（修复"回调无法同步透传为生成器事件"的架构级问题）、终态事件锁释放后再 yield、sessionId 预生成、前置 error 事件约定、toolCallStart 次序约定、事件改为独立 dataclass。
> v2.2：按 `docs/codeReview/260725_streamOutputPlanV2_1.md` 二次审核修订——前置校验锁位置修正（pending 检查保留在锁内，复用 terminalEvent 机制）、流式 API 的 sessionId 改为必填、清理 §3 与选定结论矛盾的残留表述。
> v2.3：三项待拍板问题全部按建议确认（1A 中断+新流 / 2A 旧 API 保留为包装 / 3A 7 种事件），方案定稿，进入实现。
> v2.4：chatUiStreamingFixPlan 实施补记（§6.7）——事件集无新增（仍 7 事件）；UI 侧 live 按模型 step 拆块呈现；reasoning 落库 assistantMessage.reasoning（仅展示，不回灌模型）。

---

## 1. 现状回顾

当前 LLM 调用链路：

```
askModel.py / 调用方
    └─ agent.runUserMessage()  →  continueModelLoop()
        └─ modelAdapter.complete(messages, tools)      ← chatCompletionsAdapter
            └─ urllib.request.urlopen() 一次性 POST，read() 读完整个响应体
                └─ json.loads() 解析 choices[0].message
```

关键事实：

- 请求体**没有** `"stream": true`，服务端一次性算完才返回；
- 调用方在拿到 `runResult` 之前**没有任何输出手段**：模型生成 500 字需要 10 秒，用户就对着空白屏幕等 10 秒；
- 现状的 `askModel.py` 演示脚本甚至还没有 `print(result)`，也没有确认流程的交互代码（这两点实现阶段需顺带补齐，否则流式效果无法演示）。

**流式输出要解决的问题**：模型边生成、用户边看到文字逐段出现（类似 ChatGPT 网页的打字机效果），同时 agent 内部的工具调用逻辑不受影响。

---

## 2. 流式协议基础知识（三方案共用前提）

无论选哪个方案，与模型服务的交互方式都一样，区别只在"内部怎么用"：

1. 请求体加 `"stream": true`；
2. 服务端改用 **SSE（Server-Sent Events）** 格式逐块返回，事件以**空行**分隔，典型数据行长这样：

   ```
   data: {"choices":[{"delta":{"content":"你"}}]}
   data: {"choices":[{"content":"好"}}]}
   ...
   data: [DONE]
   ```

3. 完整响应 vs 流式响应的结构差异：
   - 完整响应：`choices[0].message.content` 是一整段字符串；
   - 流式响应：`choices[0].delta.content` 是一小片增量文字，需客户端**自己拼接**；
4. **工具调用在流式下也是碎的**，这是实现里最大的坑：
   - `tool_calls` 按 `index` 分桶累积，最后才对拼好的 `arguments` 字符串做一次 `json.loads`；
   - 某个 tool_call 的**首个 chunk** 通常只带 `id` + `function.name`，后续 chunk 的 `function` 里**只有** `arguments` 片段（`id`/`name` 为 null），绝不能直接覆盖；
   - 首轮 chunk 的 `delta` 常只含 `role: "assistant"` 而 `content` 为 null，解析器必须容忍缺字段；
5. **网络层解析陷阱**（仅次于 tool_calls 的易错点）：
   - TCP 不保证一行一次性到达，必须自己缓冲半行；
   - 多字节 UTF-8 字符可能跨 chunk 被切断，须先按**字节**缓冲、凑满一行再 decode；
   - 部分 provider 会下发 `: keep-alive` 注释行和心跳空行，解析器要跳过；
6. **思维链是独立通道**：`modelConfig` 已支持 `thinking`，GLM 系模型开启后，流式响应里思维链走 `delta.reasoning_content`，与正文 `delta.content` 是两个通道。开启 thinking 时"首字 ~0.5 秒"的预估不成立（思维链可能先跑十几秒）；
7. 其它协议细节：可选 `stream_options: {"include_usage": true}` 让最后一个 chunk 单独下发 token 用量；`finish_reason`（`stop`/`tool_calls`）在流式下也分段到达。当前不实现 token 统计，记录备查即可；
8. **错误形态差异**：OpenAI 风格是"非 200 → HTTPError"；GLM 流式错误可能在 **HTTP 200 之后以 `data:` 事件内嵌 error** 下发，现有只 catch HTTPError/URLError 的处理覆盖不到，流式解析中必须识别并转为 `modelRequestError`。

---

## 3. 候选方案对比

### 方案 A：纯内部流式（对调用方无感）

**做法**：只改 `chatCompletionsAdapter`。请求加 `stream: true`，内部逐 chunk 读取、拼文字、拼 tool_calls，最后仍返回和现在一模一样的 `modelCompletion`（含合成的完整 responsePayload）。

**对 agent 层 / 调用方的改动**：**零**。`agent.continueModelLoop()`、`askModel.py` 一行都不用动。

**用户看到的效果**：

| 场景 | 改造前 | 改造后 |
|---|---|---|
| 终端体验 | 等 10 秒 → 一次性拿到完整回复 | 等 10 秒 → 一次性拿到完整回复（**没变化**）|

**收益**（均为稳定性收益，无体验收益）：

- 超长响应场景下，非流式整体超时/截断风险更高，流式逐块接收更稳；
- 非流式下 `read()` 期间连接可能被中间代理 idle 切断导致整请求失败，流式下持续有流量反而不易被 idle 超时杀掉；
- 为方案 B/C 打底（SSE 解析与 tool_calls 累积逻辑可完整复用）。

**工作量**：最小。改动集中在一个文件（`chatCompletions.py`），约 80–120 行新增。

**结论**：单做方案 A 用户**感知不到任何区别**。它只适合作为 B/C 的第一步，不适合作为终点。

---

### 方案 B：回调式流式（调用方注册回调，边生成边打印）

> 注：本节为选型期的候选分析，最终用户已选定方案 C（见 §5）；方案 B 的回调能力已由 §6.5 包装层保留。

**做法**：

1. `chatCompletionsAdapter.complete()` 增加两个可选参数：`onDelta: Callable[[str], None] | None = None`（正文回调）与 `onReasoning: Callable[[str], None] | None = None`（思维链回调，**已拍板：思维链也要流式**，与正文分离走独立回调，避免两个通道的文字混在一起）；
2. 每收到一个正文 chunk 调 `onDelta(片段)`，收到思维链 chunk（`delta.reasoning_content`）调 `onReasoning(片段)`，同时自己累积；
3. `agent` 侧透传：`runUserMessage(..., onDelta=..., onReasoning=...)`，且 `continueConfirmation(...)` 同样透传（模型调用有三个入口：主路径、dangling 恢复、确认续跑，三处都要覆盖）；
4. `modelConfig` 新增 `stream: bool = True` 开关（**已拍板**）：为 `False` 时强制走现有非流式路径，用于不支持 SSE 的 provider 回退；两个回调在非流式下静默不触发；
5. 调用方（`askModel.py`）这样用：

   ```python
   def printDelta(text):
       print(text, end='', flush=True)

   result = flamingo.runUserMessage(prompt, sessionId='test111', onDelta=printDelta)
   ```

**用户看到的效果**：

```
$ python askModel.py
你认为这份文档写的合理吗？
文档整体结构清晰，我先说结论：合理。具体来说……▌   ← 文字逐段实时出现
```

- 模型一开始生成，终端立刻开始出字，等待感从 10 秒降到首 chunk 延迟（thinking 关闭时 ~0.5 秒）；
- 最终 `runResult` 里的完整 message 不受影响；
- **不传 `onDelta` 时行为与现在完全一致**（向后兼容）。

**边界行为与硬性约定**：

1. **日志与会话恢复**：流式下不存在单个完整响应 JSON，适配器**必须在流式结束后合成一个与非流式结构完全一致的 responsePayload**（`choices[0].message` 形态），供 `agent.appendAssistantMessage(message, responsePayload)` 写日志——这是 v1.9 dangling tool-call 会话恢复不被破坏的前提；
2. **回调异常安全**：适配器捕获 `onDelta` / `onReasoning` 抛出的异常并静默忽略（仅记 debug 日志）。否则一个打印异常（如 BrokenPipeError）会被 `continueModelLoop` 的 `try/except` 当成"模型调用失败"，导致已打印半截的回复被丢弃、整轮 error；
3. 模型本轮返回纯 tool_calls（无正文无思维链）时，两个回调都不会被调——正常行为，工具确认/执行流程照旧；
4. 正文与 tool_calls 并存时，正文照旧流式回调，tool_calls 静默拼接；
5. agent 循环内**每一轮**模型调用（含工具执行后、确认续跑后的新一轮）都会触发回调；
6. **超时语义变化**：`urlopen(timeout=60)` 在非流式下是"整请求硬上限"，流式下变为"**相邻两次 read 之间的空闲超时**"——模型持续吐字则总时长无上限，长时间 thinking 静默超 60 秒会误判超时。暂不加总时长兜底，记录备查；
7. 流式中途的错误事件（HTTP 200 后内嵌 error）必须正确转为 `modelRequestError`（见 §2 第 8 条）。

**工作量**：中等。`chatCompletions.py`（SSE 解析 + tool_calls 累积 + payload 合成 + 错误识别）+ `ports.py`（协议加参数，落地时 grep 一遍 `complete(` 的所有实现/调用方同步签名）+ `agent.py`（三个入口透传）+ `askModel.py`（示例回调 + 补 print 与确认流程演示），总计约 200–250 行。

**结论**：**性价比最高的方案**。用最小侵入换来"打字机效果"这个最直观的体验升级，agent 仍是同步阻塞模型，不引入并发复杂度。

---

### 方案 C：异步生成器式流式（agent 层产出事件流）

**做法**：把 `runUserMessage` 改造成**生成器**，不再返回单个 `runResult`，而是逐个 yield 事件：

```python
for event in flamingo.runUserMessageStream(prompt, sessionId='test111'):
    if isinstance(event, textDeltaEvent):   print(event.text, end='')
    if isinstance(event, toolCallStartEvent): print(f'调用工具 {event.toolCall.toolName}')
    if isinstance(event, completedEvent):   ...
```

**用户看到的效果**：终端体验和方案 B 一样（逐段出字），但调用方能拿到**更丰富的事件**（工具调用开始/结束、确认请求、每轮完成等），适合未来做 TUI/GUI。

**代价**：

- agent 核心循环要从"函数一次跑完返回结果"改成"生成器暂停/恢复"，**确认流程（confirmationRequired 中断后继续）与生成器的交互会变复杂**；
- 会话锁（`RLock`）跨 yield 持有，需仔细设计避免死锁——现状 `runUserMessage` 全程在 `getSessionLock()` 内执行，生成器化确实会持锁跨 yield；
- 现有 `runUserMessage` / `continueConfirmation` 的调用契约要重新设计，属于**架构级改动**；
- 目前项目只有 CLI 调用方，没有 TUI/GUI 需求在案，属于为假想需求付架构成本。

**工作量**：大。涉及 `agent.py` 核心循环重构 + 事件类型定义 + 所有调用方改造，预计 400+ 行，且回归风险最高。

**结论**：架构级改动，成本最高，但事件流是 TUI/GUI 的正确地基。**用户已拍板选定本方案**，详细设计见 §6。

---

## 4. 横向对比表

| 维度 | 方案 A 纯内部 | 方案 B 回调式 | 方案 C 事件流 |
|---|---|---|---|
| 用户能看到打字机效果 | ❌ | ✅ | ✅ |
| 对现有代码侵入 | 极小 | 小（加参数透传） | 大（重构核心循环） |
| 架构复杂度变化 | 无 | 无（仍同步） | 高（生成器+事件） |
| 向后兼容 | ✅ | ✅（不传回调=旧行为） | ❌（调用契约改变） |
| 日志/会话恢复风险 | 低（需合成 payload） | 低（需合成 payload） | 中 |
| 工作量估计 | ~100 行 | ~200–250 行 | 400+ 行 |
| 为 TUI/GUI 铺路 | 解析层可复用 | 回调可包装成事件 | 直接到位 |
| 回归风险 | 低 | 低 | 高 |

---

## 5. 建议与决策点

**方案已选定：C（事件流）**，理由（用户拍板）：要考虑后续 TUI/GUI 拓展，需要结构化的过程事件而非仅有文本回调。详细设计见 §6。

**落地步骤**：见 §6.6。

**已拍板的决策**：

- ✅ **方案选定：C（事件流）**——为后续 TUI/GUI 拓展铺路；
- ✅ **思维链也要流式**：事件流层对应 `reasoningDeltaEvent`（适配器层对应 `reasoningChunk`）；
- ✅ **保留回退开关**：`modelConfig.stream = False` 强制走非流式路径。

---

## 6. 方案 C 详细设计（已选定）

### 6.1 分层架构

方案 C 分两层实施：

```
调用方（askModel.py / 未来 TUI）
    └─ agent 事件流 API（生成器，yield 各类 streamEvent）          ← 本方案新增
        └─ chatCompletionsAdapter.completeStream() -> Iterator[adapterChunk]  ← 迭代器接口
```

**关键架构决策（v2.1 修正）**：适配器层暴露的是**迭代器**而非回调。原因：Python 生成器只能从自己的栈帧 yield，无法从 `complete()` 嵌套调用栈内部的 `onDelta` 回调里 yield——若回调只写缓冲区、等 `complete()` 返回后再统一吐出，所有增量事件会在模型调用结束后一次性出现，流式实时性归零。因此适配器必须自己可迭代，agent 生成器直接 `for chunk in ...: yield event` 委托，实时性才成立。

适配器层的其余设计与方案 B 一致：`stream` 开关、SSE 解析（半行缓冲、UTF-8 字节级拼接、跳过注释行）、三路增量拼接、payload 合成、流内错误识别。`stream=False` 时迭代器退化为一次性产出单个 `finalChunk`，回退路径自然统一。原方案 B 的"回调异常安全"约定上移到 §6.5 的包装层（回调只在那里出现）。

### 6.2 事件模型（core/types.py 新增）

**适配器层 chunk**（3 个 dataclass）：

| chunk 类型 | 载荷字段 | 含义 |
|---|---|---|
| `textChunk` | `text: str` | 一段正文增量 |
| `reasoningChunk` | `text: str` | 一段思维链增量（`delta.reasoning_content`） |
| `finalChunk` | `completion: modelCompletion` | 本轮结束，携带拼好的完整结果与合成 payload |

**agent 层事件**（7 个独立 dataclass，`isinstance` 判别，与 types.py 现有风格一致）：

| 事件类型 | 载荷字段 | 触发时机 |
|---|---|---|
| `textDeltaEvent` | `text: str` | 适配器每产出 `textChunk` |
| `reasoningDeltaEvent` | `text: str` | 适配器每产出 `reasoningChunk` |
| `toolCallStartEvent` | `toolCall: toolCall`、`preview: str` | 某个 toolCall 进入实际执行路径时（免确认工具、已批准工具；未知工具的合成失败结果也发，见配对不变式） |
| `toolCallEndEvent` | `toolResult: toolResult` | 工具执行完毕（含 isError、未知工具、被拒绝） |
| `confirmationRequiredEvent` | `confirmationId`、`reason`、`commandPreview`、`toolCall` | 工具需用户确认，**终态事件，锁释放后 yield，流到此终止** |
| `completedEvent` | `message: str` | 模型给出最终正文回复，**终态事件，锁释放后 yield** |
| `errorEvent` | `message: str`、`errorType: str` | 前置校验失败/模型调用失败/超步数等，**终态事件，锁释放后 yield** |

设计说明：

- **toolCallStart/End 配对不变式**：每个进入批处理且实际执行的 toolCall 恰好一对 Start/End；两个例外——① 需确认工具检出时只发 `confirmationRequiredEvent`（载荷已含 toolCall + preview，不发 Start）；批准后续跑流中该工具正常走 Start→End；**拒绝路径只发 End**（isError=True，对应 buildBlockedToolResult），不发 Start；② 未知工具（definition is None）不执行，Start/End 都发，保持配对；
- **preview 行为扩围声明**：现状 `buildToolPreview` 只在需确认分支调用，新设计下每个工具执行前都会生成 preview（沿用其异常兜底，失败回退 `str(call.arguments)`）；
- `errorEvent.errorType` 取值：`emptyMessage` / `pendingConfirmationExists` / `confirmationMismatch`（前置校验）/ `maxStepsExceeded`（超步数）+ 运行期模型错误的异常类名；
- 不设 `stepFinish` 事件：`toolCallStart`/`completed` 已能区分轮次边界，避免推测性设计；后续 TUI 需要时再加；
- 事件不含 `sessionId`：流由调用方按会话发起，天然知道会话（同步包装层预生成 sessionId，见 §6.5）。

### 6.3 事件流 API（agent 新增）

```python
def runUserMessageStream(self, message: str, sessionId: str) -> Iterator: ...
def continueConfirmationStream(self, sessionId: str, confirmationId: str, approved: bool) -> Iterator: ...
```

**sessionId 为必填**（v2.2 修正）：事件刻意不带 sessionId，若允许 None 由生成器内部创建，调用方拿到 `confirmationRequiredEvent` 后将无从得知该对哪个会话调 `continueConfirmationStream`，确认链路断掉。新会话由调用方先调 `agent.createSessionId()` 再传入（同步包装层正是这样做的，见 §6.5）。

**确认流程的交互方式**：与现有状态机同构——

1. 流运行中遇到需确认工具：yield `confirmationRequiredEvent` 后**生成器正常结束**（pendingConfirm 已存入 conversation，与现状一致）；
2. 调用方拿到确认结果后，调 `continueConfirmationStream(...)` 开启**新的事件流**续跑；
3. 不用 `generator.send()` 回传确认结果：会让生成器生命周期与锁持有时间不可控，且与现有持久化状态机割裂。

**前置校验失败的契约**：三个分支都 yield **单个 `errorEvent`** 后正常结束，不发其他事件。注意锁位置（v2.2 修正）：

- 空消息 → `errorType='emptyMessage'`——现状在拿锁之前校验，流式实现同样在锁外直接产出该事件；
- 存在待确认 pendingConfirm → `errorType='pendingConfirmationExists'`——现状 `hasPendingConfirmation` **在锁内**检查，流式实现必须同样在锁内完成检查后把该事件当终态事件处理（走 §6.4 的 terminalEvent 机制，锁释放后再 yield），**不得在锁外预检**（否则两线程同会话可同过预检，产生 TOCTOU 竞态）；
- confirmationId 不匹配 → `errorType='confirmationMismatch'`——现状 `takePending`/`setPending` 放回在锁内，同样走锁内检查 + terminalEvent 机制，且必须保持现状语义——pending 不被消费（setPending 放回）。

统一契约：**消费者收到任何 `errorEvent` 时锁必然已释放**。

### 6.4 会话锁策略（核心风险点，v2.1 重写）

现状：`runUserMessage` 全程持有 `getSessionLock()` 的 RLock。生成器化后锁必然**跨 yield 持有**。`try/finally` 只能保证"继续驱动或 close() 时"释放锁，挡不住最常见的中断模式（拿到终态事件后 break、消费回调抛异常），且旧流挂起持锁时跨线程开新流会**直接死锁**（TUI 典型场景）。因此采用**结构性消除**：

1. **终态事件在锁释放之后再 yield**。生成器结构：

   ```python
   terminalEvent = None
   with self.getSessionLock(realSessionId):
       for event in self._runLoopStream(realSessionId):   # 中途事件在锁内 yield
           if 是终态事件(event):
               terminalEvent = event
               break
           yield event
   # 锁已释放
   if terminalEvent is not None:
       yield terminalEvent
   ```

   效果：**消费者看到 `completed`/`confirmationRequired`/`error` 时锁必然已释放**，"拿到确认事件后直接开新流"在任何线程都安全，最常见的消费模式（for 循环跑到终态自然结束）零风险；
2. 锁泄漏只剩一种可能：中途 delta 阶段放弃迭代且不 close。**调用方契约**：中途退出必须 `stream.close()`，askModel.py 示例示范 `try/finally` 写法；
3. 同会话并发由 RLock 串行化（同会话串行、跨会话并行），任何线程模型下语义不变。

### 6.5 向后兼容：旧同步 API 保留为薄包装

现有 `runUserMessage` / `continueConfirmation` **保留**，内部改为驱动事件流：

```python
def runUserMessage(self, message, sessionId=None, onDelta=None, onReasoning=None) -> runResult:
    realSessionId = sessionId or self.createSessionId()   # 预生成（与现状顺序一致）
    for event in self.runUserMessageStream(message, sessionId=realSessionId):
        # textDeltaEvent→onDelta、reasoningDeltaEvent→onReasoning、
        # 终态事件 → 转成同构 runResult(sessionId=realSessionId, ...)
```

- **sessionId 必须包装层预生成**：事件流刻意不带 sessionId，若由生成器内部创建则包装层耗尽事件后无从构造 `runResult.sessionId`；
- 现有调用方一行不用改，行为与现状一致（含 dangling 恢复路径：其复合事件序列 = [dangling 工具事件…] + [queued 消息触发的模型轮事件…]，终态映射无损）；
- `runResult` 结构不变，终态事件到 `runResult` 是机械映射；
- `onDelta`/`onReasoning` 可选回调在包装层把事件机械映射回函数调用，**回调异常在包装层捕获静默**（仅 debug 日志）——方案 B 的能力在同一代码路径上收敛保留。

### 6.6 落地步骤

1. **适配器层**：`modelConfig.stream` 开关 + `completeStream() -> Iterator[adapterChunk]`（SSE 半行缓冲、UTF-8 字节级拼接、跳过注释行、三路增量拼接、payload 合成、流内 error 转 modelRequestError）；`stream=False` 时退化为单发 finalChunk；`ports.py` 协议签名同步，grep 全部 `complete(` 调用/实现方 → **验证**：--debug 见逐 chunk；finalChunk 的合成 payload 与非流式同构（硬性标准，保证会话恢复不破）；`stream=False` 行为与现状一致；
2. **事件模型**：`core/types.py` 新增 §6.2 的 3 个 chunk + 7 个事件 dataclass → 验证：类型可导入、字段齐全；
3. **agent 重构**：抽出 `_runLoopStream` 生成器承载现有 `continueModelLoop`/`processToolBatch` 逻辑（适配器 chunk 直接委托为事件）；按 §6.4 结构实现 `runUserMessageStream`/`continueConfirmationStream`（终态事件锁后 yield）；旧同步 API 改为包装 → **验证**：现有同步 API 行为零变化（会话恢复、dangling 处理、确认流程全部照旧）；**迭代到 textDelta 后 `stream.close()`，同会话立即可发起新流**（验证锁释放）；
4. **askModel.py**：改为消费事件流的演示（逐字打印正文/思维链、工具事件打印、确认交互，try/finally close 示范）→ 验证：终端效果达 §3 方案 C 示例；
5. **真实模型场景验证**：① 纯文本回复；② 免确认工具调用；③ 需确认工具（拒绝/批准两条路径，验证拒绝只发 End、批准走 Start→End）；④ 确认续跑后新一轮流式；⑤ `stream=False` 回退；⑥ **拿到 confirmationRequiredEvent 后不 close 旧流直接 continueConfirmationStream，验证续跑成功**；⑦ 构造 dangling 会话日志后恢复，验证事件序列 = [dangling 工具事件…] + [正文流] + completed，且日志结构不变 → 验证：全部事件序列正确、会话日志结构不变、中断恢复正常。

**三项决策均已确认（用户拍板）**：

1. ✅ **确认交互方式：1A**——流遇确认即终止 + `continueConfirmationStream` 开新流（§6.3）；
2. ✅ **旧同步 API 保留为包装：2A**——`runUserMessage` / `continueConfirmation` 内部驱动事件流，外加可选 `onDelta`/`onReasoning` 回调（§6.5）；
3. ✅ **事件粒度：3A**——7 种事件，不加 stepFinish/耗时统计（§6.2）。

方案定稿，按 §6.6 落地步骤实施。

### 6.7 实施补记（2026-08-08，chatUiStreamingFixPlan）

- SSE 事件集无新增，仍为 §6.2 的 7 种事件；step 边界由前端隐式推断（toolCallEnd 后再来 textDelta/reasoningDelta ⇒ 新 step），未引入 stepStart 事件。
- `reasoning` 落库到 jsonl `assistantMessage.reasoning`（consumeSseStream 累积合成 responsePayload 顶层字段；非流式 `complete()` 出口从 `choices[0].message.reasoning_content` 归一化），仅用于 UI 展示与历史回放，**不进入发往模型的 messages**。
- Web UI 侧 live 按模型 step 拆 assistant 块呈现（thinking → content → 本 step 的 tools），与历史 1:1 对齐。

---

## 7. 附录：pi（生产级 coding agent）的机制对照验证

> v2.2 后补充。研究了 pi 的实际实现（`pi-ai` / `pi-agent-core` / `pi-coding-agent`，位于 `@earendil-works/pi-coding-agent/node_modules/@earendil-works/`），验证本方案架构方向与生产实践一致。

**pi 的链路**：

1. **LLM 层**（`pi-ai/dist/api/openai-completions.js`）：provider 读 SSE 时向 `EventStream` push 事件（`text_delta`/`thinking_delta`/`toolcall_delta`/`done`/`error`），每个事件携带**累积好的 partial 消息**，消费者不拼接；
2. **桥接**（`pi-ai/dist/utils/event-stream.js`，约 60 行）：队列 + 等待者 + 最终结果 Promise，生产者 push、消费者 `for await` pull——它的存在是因为 JS 里生产/消费是两个异步上下文；**Python 同步代码的等价物就是生成器直接委托**（本方案 §6.1 的 `completeStream()` 迭代器），无需队列；
3. **Agent 层**（`pi-agent-core/dist/agent-loop.js`）：`for await` 消费 LLM 流，switch 转换为 10 种 agent 事件（`agent_start/end`、`turn_start/end`、`message_start/update/end`、`tool_execution_start/update/end`）；
4. **确认机制**：`await beforeToolCall(钩子)`，钩子 Promise 不 resolve 则循环暂停，**流不中断、状态不外置**；pi 不持久化待确认状态。

**对本方案的验证结论**：

- 分层（SSE 解析层 → chunk 迭代器 → agent 事件流）与 pi 完全一致，§6.1 的迭代器委托是 pi `EventStream` 桥在同步 Python 下的正确等价物；
- 本方案 7 种事件 ≈ pi 10 种的子集（pi 多 start/end 成对事件，当前 CLI 用不上，TUI 阶段按需补）；
- 唯一实质差异在确认机制：pi 用钩子暂停（简单），本方案用"中断 + 新流"（为兼容已有的 pendingConfirm 持久化/重启恢复能力，属合理成本而非过度设计）；
- pi 无会话锁（JS 单线程），本方案的 §6.4 锁策略是 Python + 既有 v1.9 锁设计下的必要增量。
