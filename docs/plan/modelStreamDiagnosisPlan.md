# 模型流断连诊断日志补强方案（modelStreamDiagnosisPlan）

- 日期：2026-09-01
- 状态：v1.2（grok-4.6 复审通过，可以实施）
- 范围：`flamingoAgents/models/chatCompletions.py`、`flamingoAgents/models/responsesAdapter.py`、`flamingoAgents/core/agent.py`、`webApp/backend/agentManager.py`、`webApp/backend/sseCodec.py`、`webApp/backend/server.py`
- 原则：**先观测、后处置**。本方案只做纯增量诊断（B 系列）。除已落地的读超时 60→300s 外，不改重试/计费/恢复语义。
- 变更记录：
  - 2026-09-01 用户确认先行实施：urlopen 读超时 60s→300s（`chatCompletions.py` v1.18 / `responsesAdapter.py` v1.1）。49 passed。
  - 2026-09-01 源码评审修订 v1.1：adapter 不写 jsonl；收缩 B4；删除 B5；O5 改述；stage 判定点写清。评审见 `docs/plan/modelStreamDiagnosisReview.md`。
  - 2026-09-02 `xaiSubscription/grok-4.6` 复审 **通过**（无 P0/P1）。报告 `docs/plan/modelStreamDiagnosisReview2.md`。P2 全部是写法约束，收入 §4.6，不改事件模型。

## 1. 问题现象

LLM response 经常「断」，事后无法定位：前端只看到失败或连接关闭，jsonl 分不清「没连上 / 首字前超时 / 流中静默超时 / 连接重置 / provider 主动断」。

## 2. 调研结论

### 2.1 全链路（现状）

```
浏览器 fetch(POST /api/chat/stream)
  → FastAPI chatStream → streamPump 泵线程
    → agent.driveModelLoop（重试循环）
      → adapter.completeStream
        → openRequest: urlopen(timeout=300)
        → consumeSseStream: iterSseData 循环 read1(4096)
        ← yield finalChunk
      ← appendAssistantMessage 落 jsonl
    ← 泵 _broadcast → sseGen → 浏览器
```

CLI 走同一套 agent/adapter/jsonl，不经过泵/sseGen。

### 2.2 数据实锤（~/.flamingo/logs）

- 48 个会话、56 条 modelError：54 条 modelRequestError（403 `error code: 1010`、502 upstream_error 等），2 条 RuntimeError。
- 现有 modelError 字段：`errorType/message/status/request(全量 payload)`。无 stage、耗时、TTFB、chunk 数、errno、requestId、attempt。
- 同一 502 会连续多条：因为 `driveModelLoop` **每次失败都 `logModelError`，然后再决定是否重试**。重试过程其实已经在 jsonl 里，只是分不清 attempt / 是否还将重试 / 退避多久。

### 2.3 观测断层

| # | 断层 | 位置 | 后果 |
|---|------|------|------|
| O1 | 失败无阶段/耗时/TTFB/chunk/errno/requestId | 两个 adapter 构造 `modelRequestError` 处 | 无法归因 |
| O2 | FastAPI 500 不打堆栈 | `server.py` `fallbackErrorHandler` | 建流前的后端 bug 无痕 |
| O2b | SSE 已启动后的生成器异常不进 fallbackErrorHandler | `sseCodec.py` `sseGen` | 中途崩只表现为连接断开 |
| O3 | 泵 except 只广播不落盘 | `agentManager.py` `_pump` | 泵异常不可查 |
| O4 | 浏览器关页 vs 上游断流不可区分 | 前端 + sseGen | **次要**。多窗口下一页关闭不等于流死。v1 不为此加流级事件 |
| O5 | modelError 无 attempt/willRetry/backoffMs | `agent.py` `logModelError` | 多条失败分不清重试中还是放弃 |
| O6 | `assistantMessage.timings` 永远 None | adapter 从不写 timings | 成功路径无耗时 |
| O7 | debugConsole 是 print，Web 不开 debug | `debug.py` / `agentManager.getAgent` | v1 **不做**（开 debug 才有用，与「默认能查」无关） |
| O8 | 断流半截 content 不落盘 | `consumeSseStream` | 前端已渲染 vs jsonl 不一致。v1 **不保存半截**（M2） |

### 2.4 嫌疑根因（行为层，本方案除超时外不改）

- **S1**：socket 单次 read 超时误杀长思考。已放宽 300s。`chunkSeen=True` 仍不重试，半截仍丢（M3）。
- **S2**：403/502 等 provider 错误无 requestId，无法对账。
- **S3**：前端把「无终态的连接关闭」一律当成连接中断。v1 不改前端文案。

### 2.5 设计根因

jsonl 是会话审计（恢复上下文），不是诊断日志。本方案在同一 jsonl 里 **加诊断事件/字段**，resume 忽略未知 type，不另建日志系统。

## 3. 目标与非目标

### 3.1 成功标准

- **G1**：失败后 jsonl 能读出 stage、httpStatus、durationMs、ttfbMs、chunks、textChars、reasoningChars、exceptionName/errno、attempt、willRetry、requestId（有则记）。
- **G3**：建流前 500 有 stderr 堆栈；泵异常有 `pumpError`；sseGen 意外异常有 `sseGenError`。
- **G4**：每次失败的 modelError 带 attempt/willRetry/backoffMs。
- **G5**：成功轮次 `assistantMessage.timings` 有值（durationMs≥0；textChars/reasoningChars 允许 0）。
- **G6**：不上报、不上传、不做新 UI。
- **G7**：每尝试 +1 条 `modelRequestStart`；失败增强原 modelError（不新增 modelRetry 事件）；成功 timings 并入 assistantMessage。诊断字段为标量 + 响应头白名单（值截断 256B）。

删除原 G2（流级 streamDisconnected）：见 P1-2，避免误报。

### 3.2 非目标

- 不改重试策略、不保存半截内容、不再改超时（已 300s）。
- 不动 SSE 帧格式 / attach 协议。
- 不做诊断 UI、不做 askSubAgent 子日志、不改 debugConsole。

## 4. 设计

### 4.1 谁写 jsonl（评审 P1-1）

| 写者 | 事件 | 原因 |
|------|------|------|
| **agent**（有 per-session `conversation.logger`） | `modelRequestStart`、增强 `modelError`、`pumpError`/`sseGenError` 由 Web 层经 conversation.logger | adapter 跨会话共享，不能绑 logger |
| **adapter** | 不写文件。只填 `modelRequestError.diag` 与 `responsePayload['timings']` | 采集点在 HTTP/SSE 循环内 |

库独立使用：只要走 `agent`+conversation，诊断自动进该会话 jsonl。采集失败必须吞掉，不得打断主流程。

### 4.2 事件

| type | 时机 | 字段 | volume |
|------|------|------|--------|
| `modelRequestStart` | `driveModelLoop` 即将 `completeStream` | sessionId、attempt（1 起）、messageCount、contextTokens（lastTurnTokens） | 1/尝试 |
| `modelError`（增强，兼容旧字段） | 现有 `logModelError`（每次失败，含将重试的） | 旧字段 + attempt、willRetry、backoffMs、stage、durationMs、ttfbMs、chunks、textChars、reasoningChars、exceptionName、errno、requestId、responseHeaders、requestBytesLen、api、baseUrl、authRefresh | 1/失败尝试 |
| `pumpError` | 泵 `_pump` except | sessionId、errorType、message、traceback | 异常时 1 |
| `sseGenError` | sseGen 非 GeneratorExit 异常 | sessionId、errorType、message、traceback | 异常时 1 |

`_resumeFromLog` 对未知 type 自然跳过；`modelError` 本就会 `continue`。无需改白名单。

### 4.3 stage 判定点（评审 P1-5）

在 **构造/附着 diag 时** 赋值，不要只靠最外层 OSError except。

| stage | 判定 | Chat Completions | Responses |
|-------|------|------------------|-----------|
| `connect` | `openRequest` 里 HTTPError/URLError（含 TCP/DNS/TLS） | 是 | 是（含 401 刷新前的失败；刷新成功则不落到这条） |
| `firstByte` | openRequest 已返回，尚无任何 SSE data payload 时 read 失败/超时 | 是 | 是 |
| `streamRead` | 已处理 ≥1 个 SSE data payload 后 read 失败/超时 | 是 | 是 |
| `decode` | SSE JSON 非法，或 HTTP 200 内嵌 error 对象 | `processSseData` | `parseEvent` / `response.failed` / `error` 事件 |
| `streamEnd` | 流 EOF 且协议不完整 | **不适用**（无 [DONE] 仍合成 finalChunk） | `buildCompletion` 发现 `terminalSeen=False` |

TTFB：openRequest 返回 → 第一次 yield `textChunk`/`reasoningChunk`（usage-only / [DONE] 不算）。无 delta 则 ttfbMs 为空。

响应头白名单：`x-request-id`、`cf-ray`、`x-served-by`、`retry-after`、`date`，以及所有 `x-ratelimit-*`。成功在 urlopen 返回的 `response.headers` 取；HTTPError 在 `error.headers` 取。值截断 256B。`requestId` 取 `x-request-id` 否则 `cf-ray`。

Responses 401 内部 refresh：仅失败 diag 记 `authRefresh`=true；成功 timings 不记。

### 4.4 adapter diag 形状

局部 dict（两 adapter 共用同一键名，允许复制小函数，不强制新模块）：

```
stage, t0, ttfbMs, durationMs, chunks, textChars, reasoningChars,
exceptionName, errno, requestId, responseHeaders, requestBytesLen,
api, baseUrl, authRefresh
```

- 失败：`error.diag = diag`（`modelRequestError` 增加可选属性，缺省 None）。
- 成功：`responsePayload['timings'] = {ttfbMs, durationMs, chunks, textChars, reasoningChars, sawDone}`；headers/requestId 不进 assistantMessage（避免审计日志膨胀）。成功路径以 timings 为主。

`conversation.appendAssistantMessage` 已有 `'timings': responsePayload.get('timings')`，adapter 写了就会落盘。

### 4.5 Web 异常落盘

- `fallbackErrorHandler`：`traceback.print_exc()`。只覆盖建流前 500。
- `_pump` except：stderr 堆栈 + 若 `agent.conversations.get(sessionId)` 存在则 `logger.logEvent(pumpError)`。禁止 `getConversation()`（会为无会话建 jsonl）。
- `sseGen`：`except GeneratorExit: raise`；其它 Exception 经 pump 回调写 `sseGenError` 再 re-raise。正常 `None` 哨兵 return 不写。

### 4.6 实施写法约束（v1.2 吸收复审 P2，不改事件模型）

- **diag 是 consumeSseStream 栈上局部 dict**，禁止 `self.diag`。Responses 的 `state.buildCompletion()` 在读循环 try **之外**：对该调用再包 try，catch `modelRequestError` 后附着 diag。`_protocolError` 不要写死 stage：`response.failed`/`error`/`parseEvent` → `decode`；`terminalSeen=False` → `streamEnd`。Chat 的 `processSseData` 同样在 consume 出口补 `decode`。
- **exceptionName/errno 在 except 现场取底层异常**（`HTTPError`/`URLError`/`TimeoutError`/`OSError` 的 type 与 errno），不要填包装后的 `modelRequestError`。`logModelError` 的 `errorType` 仍可以是 `modelRequestError`（兼容旧字段）。
- **willRetry** 必须与放弃谓词同一条：`(not chunkSeen) and isRetryable and attempt < MODEL_RETRY_MAX_ATTEMPTS`。漏掉 `chunkSeen` 会把半截流记成还将重试。`backoffMs` 仅 willRetry=true 时有值。`attempt` 1-based，与 `retryNoticeEvent` 的 `attempt + 1` 对齐。
- **t0 = time.monotonic()** 放在第一次 `openRequest` 之前，connect 失败也要有 durationMs。成功 headers 从 `response.headers` 取，HTTPError 从 `error.headers` 取。
- timings 增加可选 **`sawDone: bool`**（Chat：是否见到 `[DONE]`；Responses：是否 `terminalSeen`）。不改成功/失败语义，只让静默 EOF 合成 completion 事后可区分。
- sseGen：`GeneratorExit` 以及项目里已有的客户端断开类型（若 Starlette 抛 `ClientDisconnect` / 写失败）不记 `sseGenError`。无 conversation 时 `conversations.get` 为 None 则只 `print_exc`，禁止 `getConversation()`。回调走 pump 方法，sseCodec 不 import conversation。
- **authRefresh**：只在失败 diag 里保留；成功 timings 不记。§4.3 与此对齐。

## 5. 实施 TODO

### B1 adapter 采集（O1/O6）

- [ ] `modelRequestError` 增加 `diag: dict | None = None`（构造默认 None，不改现有调用点行为）
- [ ] 抽小函数 `pickDiagHeaders(headers) -> dict`（可放在 `chatCompletions.py`，Responses import）
- [ ] `chatCompletions.openRequest`：HTTPError/URLError 填 diag.stage=`connect` + headers/errno；成功返回前可把 headers 挂在 response 上供 consume 读取（或 consume 里直接读 `response.headers`）
- [ ] `chatCompletions.consumeSseStream`：t0、chunks/chars、ttfb；iterSseData 失败按 chunks 判 firstByte/streamRead；`processSseData` 的 modelRequestError 补 diag.stage=`decode`；成功写 timings
- [ ] `chatCompletions.complete`（非流）：同样 timings（ttfb 可空）+ 失败 diag
- [ ] `responsesAdapter` 对称：注意 `except modelRequestError: raise` 前补 diag；`buildCompletion` 缺 terminal → streamEnd；401 refresh 记 authRefresh
- [ ] 采集代码自身失败不得影响主流程（写 diag 用尽量朴素的赋值，不必层层套 try；headers 解析包 try）

### B2 agent 落盘（O5/G1）

- [ ] `driveModelLoop`：每次 `completeStream` 前 `logger.logEvent(modelRequestStart)`
- [ ] `logModelError`：合并 `error.diag`；增加 attempt、willRetry、backoffMs（由调用方传入，因为此时才知道是否还要重试）
- [ ] 调整 except 顺序：先算 isRetryable / backoff，再 logModelError，再 yield errorEvent 或 retryNotice

### B3 泵异常（O3）

- [ ] `_pump` except：`traceback.print_exc()` + `pumpError` 落盘

### B4 收缩后的服务端异常（O2/O2b）

- [ ] `fallbackErrorHandler`：`traceback.print_exc()`
- [ ] `sseGen`：捕获非 GeneratorExit 异常 → pump 回调 `sseGenError` → re-raise

实施顺序：B1 → B2 → B3 → B4。

## 6. M 系列（不实施）

数据跑一段时间后再立项：

- M1 分层超时（connect / firstByte / streamRead 间隔），基线现为 300s。
- M2 断流半截 assistant 落盘。
- M3 `chunkSeen=True` 仍不重试的策略。
- M4 Retry-After 作为退避主基准。

## 7. 验收

- [ ] A1 流中 RST/断网：modelError.stage 为 firstByte 或 streamRead；chunks/durationMs 合理。
- [ ] A2 HTTP 5xx/403：stage=connect，status 有值；若重试则连续 modelError 的 attempt 递增且前几条 willRetry=true。
- [ ] A3 成功轮：assistantMessage.timings 存在，durationMs≥0（纯 tool_calls 时 textChars 可为 0）。
- [ ] A4 泵临时抛错：pumpError + stderr 堆栈；测完回滚。
- [ ] A5 关一浏览器页、另一页仍 attach：jsonl **没有** 流级断连事件，泵继续。
- [ ] A6 pytest 全绿；对话/resume/stop/确认行为不变。
- [ ] A7 诊断字段缺失时主流程仍成功（人为让 headers 为 None）。

## 8. 风险

| 风险 | 缓解 |
|------|------|
| diag 干扰主流程 | 失败路径只多赋值；headers 解析 try/except |
| 日志膨胀 | 不写 modelRetry；成功路径不加 headers；值截 256B；Start 事件无 messages 原文 |
| resume 误吃新事件 | 未知 type 已跳过（已核对 `_resumeFromLog`） |
| 多窗口误报断连 | v1 不写 streamDisconnected |

## 9. 文件

| 文件 | 改动 |
|------|------|
| `flamingoAgents/models/chatCompletions.py` | B1 |
| `flamingoAgents/models/responsesAdapter.py` | B1 |
| `flamingoAgents/core/agent.py` | B2 |
| `webApp/backend/agentManager.py` | B3、B4 回调 |
| `webApp/backend/sseCodec.py` | B4 sseGen |
| `webApp/backend/server.py` | B4 print_exc |
| `flamingoAgents/core/conversation.py` | 不改 |
| `flamingoAgents/utils/debug.py` | 不改 |
