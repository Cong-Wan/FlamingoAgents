'''
Author: wilbur
Version: 1.0
Date: 2026-08-17
Description: 修复「assistant 只输出 thinking 就断了」后再次请求被 provider 以 400 拒绝（assistant must not be empty）的方案。本文件仅方案，不改业务代码。v1.1：自审订正——补现场 jsonl 证据；占位符由单空格改为 '.'（防止 provider trim/strip 后仍被视为空）；澄清目标口径为 wire 而非落盘。v1.2：吸收审核意见——写明判定是「无 toolCalls 且文本空」不是「文本空就整条替换」；写明 convertMessage 仅请求构造、不落盘。
'''

# 空 assistant 再次请求 400 修复方案

- Author: wilbur
- Version: 1.2
- Date: 2026-08-17
- 上游问题：用户反馈「assistant 只输出了 thinking 内容就断了时，再次请求是有问题的」
- 现场错误：`模型调用失败（已重试0次）：模型请求失败：status=400 body={"error":{"message":"the message at position 99 with role 'assistant' must not be empty","type":"invalid_request_error"}}`
- 现场日志：`webData/sessionLogs/session_1aa01ee011af.jsonl` 第 100 行 `assistantMessage`（`content=""`、`toolCalls=[]`、`reasoning` 15411 字）对应重放后 `messages[99]`；同会话还有 4 条同形态。这是 **thinking-only 流正常走完 `finalChunk`** 后落盘，不是用户 stop。
- 相关代码：`flamingoAgents/models/chatCompletions.py`、`flamingoAgents/core/agent.py`、`flamingoAgents/core/conversation.py`
- 状态：**已实施**（v1.2 复审通过，无 P0/P1/P2；T1/T2 已完成）

---

## 0. 目标与非目标

### 0.1 目标（修复后用户应感知到）

1. **已中毒会话可继续聊**：历史里已经存在「空 content、无 toolCalls」的 assistant，下一轮请求不再 400。
2. **新写入也不再打 400**：thinking-only / 断流合成的空 assistant 仍可按现状落 jsonl（`content=""`），但发往模型时不会再以空 content 出现。
3. **reasoning 红线不变**：thinking 仍只用于 UI / jsonl 展示，**不**回灌到发往模型的 `messages[]`。

### 0.2 非目标（本期不做）

| 项 | 说明 |
|----|------|
| 把 reasoning 写进下一次请求 | 明确红线，与 `chatUiStreamingFixPlan` D2 一致 |
| 回滚已中毒 jsonl / 改写历史落盘 | 已写入的空 assistant 保留；只在发请求时消毒 |
| 追踪 `[DONE]` vs 意外 EOF 并改完成语义 | 断流判定增强另立专题；本期只堵 400 |
| 连续 user 消息合并 / 会话压缩 | 超出本 bug |
| 前端 thinking 展示改动 | 不需要 |

### 0.3 成功标准（总验收）

- [ ] 热会话中已有空 assistant（`content` 空或纯空白、无 `toolCalls`），再发一条用户消息，请求不再出现 `assistant must not be empty`
- [ ] 新发生的 thinking-only 完成（流正常结束、只有 reasoning、无正文、无工具）再次请求同样不 400
- [ ] 合法的「空正文 + 有 toolCalls」assistant 仍按现有格式发出（`content` 保持 `""`，带 `tool_calls`）
- [ ] `convertMessage` 仍不读取、不发送 `reasoning`
- [ ] 用户停止（`modelInterruptedError`）路径不因此多写一条 assistant

---

## 1. 问题复述（一句话）

模型只吐了 thinking 就结束时，本轮被落成 `assistant(content="", toolCalls=[])`；下次请求原样发出，provider 以 400 拒绝空 assistant。已中毒会话会持续失败。

---

## 2. 根因（代码证据）

### 2.1 空 assistant 如何被合成

`consumeSseStream` 把正文和工具分别累积，reasoning 只进顶层 `responsePayload['reasoning']`，**不进** `messagePayload`：

```python
messagePayload = {'role': 'assistant', 'content': ''.join(contentParts)}
# 仅当有 tool_calls 才挂 tool_calls
# reasoning 非空才写 responsePayload['reasoning']
```

thinking-only 时：`contentParts == []`、`toolCallAccum == {}` → `content=""`、无 `tool_calls`。

`parseAssistantPayload` 再把它变成：

```python
content = rawMessage.get('content') or ''
return chatMessage(role='assistant', content=content, toolCalls=[])
```

### 2.2 空 assistant 如何被持久化

`driveModelLoop` 在拿到 `finalChunk` 后无条件：

```python
currentConversation.appendAssistantMessage(assistantMessage, responsePayload)
if not assistantMessage.toolCalls:
    yield completedEvent(message=assistantMessage.content)
    return
```

于是 jsonl / 内存 messages 多了一条协议非法的空 assistant。用户停止走 `modelInterruptedError` 直接 return，**不会**走这条路径。

### 2.3 空 assistant 如何把下次请求打成 400

`convertMessage` 原样发送：

```python
converted = {'role': message.role, 'content': message.content}
# 无 toolCalls 时不带 tool_calls
```

`content=""` 且无 `tool_calls` → Volcano/Ark 等 provider：`the message at position N with role 'assistant' must not be empty`。

`position 99` 只说明长会话里某条历史已经是空 assistant，不是「只有第 99 条有问题」。

### 2.4 为什么「已重试 0 次」

`driveModelLoop` 只对「连接建立期、可重试 status」重试。400 带明确 `statusCode=400`，不在 `(429, 500, 502, 503, 504)`，且本轮请求一发出就被拒，于是 `已重试0次` 直接 error。重试策略正确，不是本 bug。

---

## 3. 方案选型

| 方案 | 位置 | 做法 | 评价 |
|------|------|------|------|
| **A. 发送期消毒（推荐）** | `convertMessage` | assistant 无 toolCalls 且 content 空/空白时，wire 上改发 `'.'` | 一行防线覆盖热会话 + resume；不改 jsonl；无连续 role 风险 |
| B. 跳过空 assistant | `buildRequestPayload` | 组装 messages 时丢掉空 assistant | 可能制造 `user, user`；部分 provider 也不吃 |
| C. 落盘期改写 content | `appendAssistantMessage` / `driveModelLoop` | 空且无工具时改写成占位正文再落盘 | 只防新写入，**治不好已经中毒的会话** |
| D. thinking-only 当错误、不落盘 | `driveModelLoop` | 不 append，yield error | 已中毒会话仍 400；且本轮 user 已落盘，下次变连续 user |

**推荐 A 作为唯一必做。**

理由：

1. 用户现场已经是 position 99，必须发送期治愈，否则改落盘也救不了当前会话。
2. 占位符只出现在 **发往模型的 payload**，不改 jsonl、不改 UI 展示、不把 reasoning 回灌。
3. 比 skip 更安全：不制造连续 user。
4. 合法「空正文 + toolCalls」走原路径，不误伤。

C/D 单独做都不够。若审核认为要防御式双写，可把 C 列为可选 L2，但 **A 已覆盖新写入**（下次发出去时同样消毒），本期不叠加，避免两处改 content 语义分叉。

占位符定死为 **单字符 `'.'`**（不用单空格）：

- 满足「must not be empty」
- 对模型几乎无语义，不引入中文说明（避免模型以为上轮在解释自己失败）
- 避免部分 provider 对 content 做 trim/strip 后把 `' '` 重新判空（Volcano 报错原文就是 empty，未说明是否 trim；用可见字符更稳）

---

## 4. 改动设计

### 4.1 `chatCompletions.convertMessage`（唯一代码改动）

当前：

```python
converted: dict[str, Any] = {
    'role': message.role,
    'content': message.content,
}
```

改为：

```python
content = message.content or ''
if message.role == 'assistant' and not message.toolCalls and not content.strip():
    content = '.'
converted: dict[str, Any] = {
    'role': message.role,
    'content': content,
}
```

判定口径：

| 条件 | 行为 |
|------|------|
| `role != assistant` | 不动 |
| assistant 且 `toolCalls` 非空 | 不动（`content` 即使 `""` 也合法） |
| assistant 且无 toolCalls 且 `content` 为 `None` / `""` / 纯空白 | wire content 改为 `'.'` |
| 其它 | 不动 |

硬约束（审核 P0 已吸收，实现时不得放松）：

1. **判定是「无 toolCalls 且文本空」，不是「文本空就整条替换」**。`message.toolCalls` 非空时，即使 `content == ""` 也原样发出 `content` + `tool_calls`，禁止改写成 `'.'`（工具循环里空正文极常见，整条替换会毁掉 function call）。
2. **`convertMessage` 只用于请求构造**。`buildRequestPayload`（流式 / 非流式）是它的唯一调用方。本改动不得写回 `chatMessage`、`conversation.messages`、jsonl；UI / GET messages 仍展示真实空正文。
3. 空 assistant 出现在历史中间（现场 `messages[99]`，后面还有 user）而不是「末条角色不对」。报错原文就是 empty content，不是 role 顺序问题。

`complete()` 非流式出口同样走 `buildRequestPayload` → `convertMessage`，一并覆盖。

### 4.2 不改的地方（明确边界）

- `consumeSseStream` 仍按现状合成空 `messagePayload`（jsonl 继续如实记录「没有正文」）。
- `driveModelLoop` 仍可 `appendAssistantMessage` + `completedEvent('')`。thinking 仍经 `responsePayload['reasoning']` 落 jsonl，历史 UI 还能回看。
- `modelInterruptedError` 仍不落 assistant。
- `findUnclosedTailCallIndex` / `closeUnfinishedToolCalls` 与本 bug 无关：空 assistant 没有 toolCalls。

---

## 5. 文件改动清单

| 文件 | 改动 |
|------|------|
| `flamingoAgents/models/chatCompletions.py` | `convertMessage` 按 §4.1 消毒；文件头 Version 1.15 → 1.16，Description 补本修复 |
| `docs/plan/emptyAssistantRequestFixPlan.md` | 本方案（已新增） |

不改 frontend、不改 conversation / agent、不改契约文档（wire 占位不属于对外 API）。

---

## 6. 验收用例

1. **已中毒热会话**：内存 messages 中插入一条 `assistant(content='', toolCalls=[])`，再 `completeStream` / 看 `buildRequestPayload`，该条 `content` 为 `'.'`，无 `tool_calls`。
2. **空白 content**：`content='   \n'` 同样被换成 `'.'`。
3. **有工具的空正文**：`assistant(content='', toolCalls=[...])` 仍发 `content=''` + `tool_calls`。
4. **正常正文**：`content='hello'` 原样发出。
5. **thinking-only 新完成**：走完一轮只有 reasoning 的流后，再发用户消息，不再 400。
6. **用户停止**：thinking 中途 stop，不新增多余 assistant；下次请求不因本改动失败。

手测即可（项目禁止测试框架）。可在本地用临时脚本调 `convertMessage` 打印 payload 验 1–4。

---

## 7. 风险与回滚

| 风险 | 等级 | 缓解 |
|------|------|------|
| 占位 `'.'` 被模型当成上轮正文 | 低 | 单字符几乎无语义；比 400 好一个数量级 |
| 某 provider 连 `'.'` 也不吃 | 极低 | 未观察到；若出现再改占位文案 |
| 误伤 toolCalls 消息 | 无 | 判定显式要求 `not message.toolCalls` |
| reasoning 被送进模型 | 无 | 只改 `content`，不读 reasoning |

回滚：还原 `convertMessage` 三行即可。

---

## 8. TODO lists

- [x] T1 按 §4.1 改 `convertMessage`，更新文件头（`chatCompletions.py` v1.16）
- [x] T2 手测 §6 用例 1–4（convertMessage 输出）：空/空白 → `'.'`；有 toolCalls 的空正文仍 `content=''` + `tool_calls`；正常正文/user/system 不动；原对象不写回
- [ ] T3 在已中毒或 thinking-only 会话上再发一条消息，确认不再 400（需用户热会话手测）
- [x] T4 确认有 toolCalls 的空正文请求形态未变（T2 已覆盖）

---

## 9. 实施顺序

T1 → T2 → T3 → T4。无依赖分叉。
