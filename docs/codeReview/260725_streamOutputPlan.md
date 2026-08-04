# 文档审核报告 — docs/streamOutputPlan.md（流式输出方案）

### 总览
- 审核文档：1 份（对照代码 5 个文件）
- 发现问题：🔴 0 个 / 🟠 4 个 / 🟡 5 个 / 🔵 3 个
- 整体评价：方案结构清晰、三方案的成本/收益分析方向正确，**方案 B 的推荐站得住脚**；SSE 与 tool_calls 增量拼接的描述大方向准确，但存在若干与现状不符的事实性偏差，以及 4 个落地时一定会撞上的技术遗漏（响应日志格式、回调异常安全、reasoning 增量、SSE 行缓冲）。

---

## 一、协议描述准确性（审核点 1）

**结论：基本准确，4 处需要补充/修正。**

### 🟡 1. SSE 事件边界描述不完整
**位置**: 文档 §2 第 2 条
**问题**: 文档说"每一行长这样"，容易让实现者按行解析。SSE 规范中事件以**空行**分隔，一个事件可含多行 `data:`；且 TCP 层不保证一行一次性到达——用 `urllib` 实现时必须处理**半行缓冲**和 **UTF-8 多字节字符跨 chunk 被切断**的情况。这是流式实现里仅次于 tool_calls 拼接的易错点，文档却只字未提。
**修复建议**: 在 §2 增加一条：
> 5. 实现注意：网络层不保证按行到达，需自己缓冲半行；多字节 UTF-8 字符可能跨 chunk 切断，须先按字节缓冲、凑满一行再 decode；部分 provider 会发 `: keep-alive` 注释行和心跳空行，解析器要跳过。

### 🟡 2. tool_calls 增量细节缺失
**位置**: 文档 §2 第 4 条
**问题**: "按 index 分桶累积"是对的，但漏了两个实现要点：① 首个 chunk 通常只带 `id` + `function.name`，后续 chunk 的 `function` 里**只有** `arguments` 片段，`id`/`name` 为 None，不能直接覆盖；② 首个 chunk 的 `delta` 常只含 `role: "assistant"`，`content` 为 None，解析器必须容忍缺字段。
**修复建议**: 补充这两条，否则实现者按"每个 chunk 都有完整字段"写必踩坑。

### 🟠 3. 未提及 reasoning/thinking 内容的流式形态
**位置**: 文档 §2
**问题**: `modelConfig` 现有 `thinking` 与 `reasoningEffort` 字段（`chatCompletions.py` 会注入请求体），GLM 系模型开启 thinking 后，流式响应里思维链走 `delta.reasoning_content`，与 `delta.content` 是**两个通道**。文档完全没讨论：onDelta 要不要回调 reasoning？首 chunk 延迟会被思维链拉长（"~0.5 秒首字"的预估在 thinking 开启时不成立）。
**修复建议**: §2 增加 reasoning_content 说明；§5 决策点加一问："思维链内容是否也流式展示（建议：默认不展示，或另设 onReasoning 回调）"。

### 🔵 4. `data: [DONE]` 之外的终止信号
**位置**: 文档 §2
**问题**: 未提 `stream_options: {"include_usage": true}`（流式下 usage 在最后一个 chunk 单独下发）。如果后续要统计 token，现在不留口子就得返工。另外 `finish_reason` 在流式下也是分段到达（`"tool_calls"` / `"stop"`）。
**修复建议**: 一句话提及即可，不必展开。

---

## 二、三方案成本/收益与方案 B 推荐（审核点 2）

**结论：分析合理，推荐成立。1 处收益描述需修正。**

- 方案 A"用户感知为零"的判断准确；方案 C 指出的两个痛点（确认流程与生成器交互、RLock 跨 yield 持有）与 `agent.py` 现状完全吻合——`runUserMessage` 全程在 `getSessionLock()` 内执行，生成器化确实会持锁跨 yield。方案 B"不引入并发复杂度"也属实，回调在调用方线程同步触发。
- 工作量估算（A≈100 行 / B≈200 行 / C≈400+ 行）量级合理。

### 🟠 5. 方案 B 声称"日志、会话恢复逻辑完全不变"——不成立
**位置**: 文档 §3 方案 B"用户看到的效果"第 2 条
**问题**: 现状 `agent.continueModelLoop()` 把 `completion.responsePayload` 原样写进会话日志（`appendAssistantMessage(message, responsePayload)`）。流式下**不存在单个完整响应 JSON**——只有一堆 chunk。适配器要么自行合成一个与非流式同构的伪 payload，要么日志格式改变、影响 v1.9 刚做好的会话恢复（dangling tool-call 恢复依赖日志里的 assistantMessage 结构）。
**修复建议**: 在方案 B 增加一行边界行为："适配器需在流式结束后合成与非流式结构一致的 responsePayload 供日志使用，保证会话恢复逻辑不变"，并把这点列入 §5 落地步骤 1 的验证标准。

---

## 三、与现状代码的吻合度（审核点 3）

### 🟠 6. 遗漏：onDelta 回调的异常安全
**位置**: 文档 §3 方案 B（缺失项）
**问题**: 回调在 `complete()` 内部、且 `continueModelLoop` 的 `try/except Exception` 范围内被调用。若调用方的打印回调抛异常（如终端 BrokenPipeError），会被 agent 当作"模型调用失败"处理：已打印一半的回复被丢弃、整轮返回 error、会话里不写 assistantMessage——用户看到半截文字然后报错，且**无法恢复**。
**修复建议**: 方案 B 明确约定："适配器应捕获 onDelta 异常并静默忽略（仅 debug 日志），回调异常不阻断流式拼接"；或显式声明"回调异常会中断本轮请求"由调用方自担。建议前者。

### 🟠 7. 遗漏：确认流程中断后的"新一轮流式"细节
**位置**: 文档 §3 方案 B 边界行为 / §5 决策点 2
**问题**: 文档只说了"agent 循环每一轮都会触发回调"，但现状里模型调用有**三个入口**：`runUserMessage` 主路径、`runUserMessage` 的 dangling tool-call 恢复路径、`continueConfirmation` 确认后路径。决策点 2 只问了"工具执行后再问模型要不要流式"，没问"**确认流程（continueConfirmation）触发的续跑要不要流式**"——而 askModel.py 目前根本没有确认交互代码，continueConfirmation 未来由谁调用、onDelta 从哪传入，文档没交代。
**修复建议**: 决策点 2 扩写为："工具执行后（含 continueConfirmation 确认续跑）产生的新一轮正文也流式打印，需要 `continueConfirmation` 同样透传 onDelta——是否接受签名变更？"

### 🟡 8. 超时语义变化未讨论
**位置**: 文档全篇（缺失项）
**问题**: 现状 `urlopen(timeout=60)`。非流式下这 60 秒是"连接+读完整个 body"的硬上限；流式下 socket timeout 变成"**相邻两次 read 之间**"的超时——模型只要持续吐字，总时长可以无限长（长时间 thinking 后静默 >60s 也会误判超时）。这不一定是坏事，但语义变了，文档应说明，并决定是否需要总时长兜底。
**修复建议**: 在方案 B 边界行为中加一条："timeout=60 在流式下变为 chunk 间空闲超时；是否需要额外的总时长上限？"（建议：暂不需要，记录即可）。

### 🟡 9. provider 兼容性分析偏薄
**位置**: 文档 §5 决策点 3
**问题**: 只提了"保留 stream=False 开关"（这个建议本身很好），但没核对在案 provider。现状 `config/models.yaml` 走 `openai-completions`，askModel.py 用 `providerId='glm'`——GLM 兼容 SSE 流式没问题，但其流式错误是在 **HTTP 200 之后以 `data:` 事件内嵌 error** 下发的，与 OpenAI 的"非 200 直接 HTTPError"不同，现有错误处理（只 catch HTTPError/URLError）覆盖不到。
**修复建议**: §5 落地步骤 1 的验证标准加一条："流式中途的错误事件（HTTP 200 后内嵌 error）能正确转为 modelRequestError"。

### 🟡 10. 现状描述与 askModel.py 不符（两处）
**位置**: 文档 §1
**问题**: 
- ① "调用方只能等 runUserMessage() 返回后**一次性打印完整回复**"——实际 `askModel.py` 的 loop 2 **根本没有 print(result)**，现状是连打印都没有。不影响方案结论，但"现状回顾"作为决策依据应准确。
- ② §3 方案 B 示例 `flamingo.runUserMessage(prompt, sessionId='test111', onDelta=printDelta)` —— 可行，但注意 askModel.py 目前也没有 confirmationRequired 分支，文档说"工具确认流程照旧"，实际**没有旧的确认流程可照旧**。
**修复建议**: §1 改为中性描述（"调用方在拿到 runResult 前无任何输出手段"）；方案 B 的验证步骤里补一句"askModel.py 需顺带补 print 与确认流程示例，否则流式效果无法演示"。

### 🔵 11. ports.py 改动被低估一行
**位置**: 文档 §3 方案 B 工作量
**问题**: `modelAdapterPort` 是 Protocol，`complete` 加 `onDelta` 参数后，所有实现该协议的类（当前只有 chatCompletionsAdapter，但测试/mock 里可能有）签名都要同步。文档已列 ports.py，无实质问题，仅提示落地时 grep 一遍 `complete(` 的调用方与实现方。

### 🔵 12. 方案 A 的"唯一收益"表述可再补一条
**位置**: 文档 §3 方案 A
**问题**: "非流式整体超时风险"属实，但还可补一条更实在的：非流式下 `read()` 期间连接被中间代理 idle 切断会整请求失败，流式下持续有流量反而更不容易被 idle 超时杀掉。可不改，供参考。

---

## 四、优点记录

1. 三方案分层（A 打底 → B 体验 → C 架构）的演进叙事非常清晰，"B 的回调未来可包装成事件流"的演进路径判断正确；
2. tool_calls"按 index 分桶拼接、最后 json.loads"点到了流式实现的最大坑；
3. 方案 C 准确识别了 RLock 跨 yield 与确认流程两个真实架构障碍，没有为了显得全面而硬推；
4. §5 三个待拍板问题都问在点子上（尤其 stream 开关），决策成本低。

---

## 五、修复优先级建议（Top 3）

1. **🟠 #5 响应日志合成**——直接决定 v1.9 会话恢复是否被破坏，必须在落地步骤里写成硬性验证标准；
2. **🟠 #6 回调异常安全**——不改则一个打印异常就能废掉整轮对话，且现象（半截文字+报错）极具迷惑性；
3. **🟠 #3 + #7 reasoning 通道与确认续跑流式**——两者都是"现在不决定，实现到一半必然返工"的决策缺口，建议在拍板阶段一并确认。

**总结论：方案 B 的推荐成立，文档可作为决策依据；建议按上述 12 条修订至 v1.1 后再进入实现。**
