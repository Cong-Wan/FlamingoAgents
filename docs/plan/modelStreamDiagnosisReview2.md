# 模型流断连诊断方案复审（对照 v1.1 + 源码）

- 日期：2026-09-02
- 对象：`docs/plan/modelStreamDiagnosisPlan.md` v1.1
- 对照：`docs/plan/modelStreamDiagnosisReview.md`（v1.0 的 5 条 P1）
- 结论：**通过**
- 可以实施

说明：本报告按真实源码交叉核对，未引用不存在的 asyncio / `_process_stream` / `__probe__` / `SSEStreamStartEvent` / `agent.context`。本项目是线程模型（`streamPump` + `queue.Queue`）。

## 各维度

| 维度 | 结论 |
|------|------|
| v1.0 的 A–E（原 P1-1～P1-5）是否被 v1.1 消化 | **全部消化**，见下节 |
| O1–O8 与代码吻合 | 吻合。O5 已改成「每次失败都落 modelError，缺的是 attempt/willRetry/backoffMs」；O2/O2b 已拆开 |
| B1–B4 能否归因 | B1+B2 是核心且与端口/线程模型匹配；B3/B4 收缩后不再误报流级断连 |
| stage 口径 | 判定点已写到 openRequest / iterSseData / processSseData·parseEvent / Responses `buildCompletion`。实施时注意 `buildCompletion()` 在 consume 的 try **之外**（P2） |
| resume 兼容 | **无问题**。未知 type 自然跳过；`modelError` 本就 `continue`；`conversation.py` 不改是对的 |
| 过度/欠设计 | 已删 B5、streamDisconnected、modelRetry，不过度。欠的是实施注意事项（P2），不挡开工 |
| 字段能否回答「response 为什么断了」 | **失败路径够用**（stage + status + duration/ttfb/chunks + exceptionName/errno + requestId + attempt/willRetry）。Chat Completions 对端静默 EOF 仍走成功合成，不记错误——这是既有语义，v1 已标明非目标；P2 建议 timings 加 `sawDone` |

## A–E 消化核对（原 P1-1～P1-5）

### A. adapter 跨会话共享，不能注入 per-session logger → **已消化**

**代码证据**：

```53:67:flamingoAgents/builder.py
    if resolved.config.apiType == 'openai-completions':
        auth = createModelAuth(resolved.apiKey or '')
        adapter = chatCompletionsAdapter(resolved.config, auth, debugConsole=printer)
    else:
        adapter = responsesAdapter(
            resolved.config,
            modelAuthResolver(resolved),
            debugConsole=printer,
        )
```

`createAgent` 每个 agent **只建一个** adapter。`agent.conversations: dict[str, conversation] = {}`（`agent.py:75`），logger 在 conversation 上。adapter 的 `activeResponses` 是实例集合（`chatCompletions.py:47-48`），跨该 agent 下所有 session 共享。

**v1.1**：§4.1 明确 adapter **不写 jsonl**，只填 `error.diag` / `responsePayload['timings']`；`modelRequestStart` 改由 `driveModelLoop` 在 `completeStream` 前写。不再有「注入 logger」路径。

### B. sseGen finally 每次都跑，不能在此记流级断连 → **已消化**

**代码证据**：

```90:107:webApp/backend/sseCodec.py
def sseGen(eventQueue, meta=None, pump=None):
    try:
        ...
            if event is None:
                return
            yield encodeSse(event)
    finally:
        if pump is not None:
            pump.unsubscribe(eventQueue)
```

`finally` 覆盖：正常 `None` 哨兵 return、客户端断开（GeneratorExit）、编码异常。`streamPump` 是多订阅者广播（`agentManager.py:190-201`），一页关闭只 `unsubscribe`，泵继续。

**v1.1**：删除 G2/`streamDisconnected`；B4 收缩为「非 GeneratorExit 的 sseGenError + fallback 打堆栈」；A5 断言关一页 **没有** 流级断连事件。

### C. fallbackErrorHandler 打不到 StreamingResponse 生成器异常 → **已消化**

**代码证据**：

```59:61:webApp/backend/server.py
async def fallbackErrorHandler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={'error': f'服务器内部错误（{type(exc).__name__}）。'})
```

```85:87:webApp/backend/server.py
def sseResponse(pump, meta=None) -> StreamingResponse:
    return StreamingResponse(sseGen(pump.subscribe(), meta=meta, pump=pump), media_type='text/event-stream', headers=sseHeaders)
```

`chatStream`（`server.py:556`）在返回 `StreamingResponse` 时路由函数已成功；之后 `sseGen` 异常由 Starlette 拆连接，**不会**进 `fallbackErrorHandler`。

**v1.1**：G3/§4.5 写明 fallback 只覆盖建流前 500；sseGen 自己 catch 非 GeneratorExit → pump 回调 `sseGenError` → re-raise。不再声称「只改 fallback 就能看到 SSE 中途崩」。

### D. driveModelLoop 每次失败都已 logModelError（O5 不是「重试完全不落盘」） → **已消化**

**代码证据**：

```226:258:flamingoAgents/core/agent.py
                except Exception as error:
                    self.logModelError(currentConversation, error)
                    statusCode = getattr(error, 'statusCode', None)
                    ...
                    isRetryable = (
                        retryableOverride
                        if isinstance(retryableOverride, bool)
                        else hasStatusAttr and (statusCode in MODEL_RETRYABLE_STATUS_CODES or statusCode is None)
                    )
                    if chunkSeen or not isRetryable or attempt >= MODEL_RETRY_MAX_ATTEMPTS:
                        yield errorEvent(...)
                        return
                    backoff = min(...)
                    yield retryNoticeEvent(...)
```

先 `logModelError`，再决定是否重试。同一 502 多条 modelError 就是重试留下的。

**v1.1**：O5 改述；B2 给现有 modelError 加 `attempt`/`willRetry`/`backoffMs`；不单列 `modelRetry`。B2 还要求先算 isRetryable/backoff 再 log（否则 willRetry 还不知道）——这是对现状顺序的必要调整，不是推翻「每次失败都落盘」。

### E. openRequest 已把 HTTPError 转成 modelRequestError，connect 不在 consume 的 OSError except → **已消化**

**代码证据（Chat Completions）**：

```102:131:flamingoAgents/models/chatCompletions.py
            return urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as error:
            ...
            raise modelRequestError(...) from error
        except urllib.error.URLError as error:
            raise modelRequestError(...) from error
```

```194:201:flamingoAgents/models/chatCompletions.py
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            raise modelRequestError(
                message=f'模型流式响应中断：{error}',
                ...
            ) from error
```

`openRequest` 成功之后，这条 except 才接 read 中断。`processSseData` 的 JSON/`error` 对象直接 `raise modelRequestError`（`chatCompletions.py:286-301`），也不走 OSError 分支。

Responses 对称：`openRequest` 265-278 行转 `modelRequestError`；consume 里先 `except modelRequestError: raise`，OSError 在其后（`responsesAdapter.py:344-354`）。401 refresh 的第一次失败在 consume 入口被吞掉，第二次 `openRequest` 仍走 connect。

**v1.1**：§4.3 表把 connect / firstByte / streamRead / decode / streamEnd 写到具体函数；并写明 Chat Completions 无 `[DONE]` 仍合成 finalChunk，**不是** streamEnd。

## 问题列表

无 P0、无 P1。

### 【P2】Responses `buildCompletion()` 在 consume 的 try/except **之外**，局部 diag 不会自动附着

**描述**：v1.1 正确把 streamEnd 定在 `terminalSeen=False`。但源码里 `state.buildCompletion()` 在读循环 try 块之后调用。若实施时只在 `except modelRequestError / OSError` 里 `error.diag = diag`，streamEnd（以及 `buildCompletion` 里 function_call 协议错）会丢掉 t0/chunks/ttfb。

**代码证据**：

```308:356:flamingoAgents/models/responsesAdapter.py
    def consumeSseStream(...) -> Iterator:
        ...
        try:
            with response:
                ...
                    for dataPayload in self.iterSseData(...):
                        ...
        except modelInterruptedError:
            raise
        except modelRequestError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            ...
            raise modelRequestError(...) from error

        completion = state.buildCompletion()
```

`buildCompletion`（654-656 行）`if not self.terminalSeen: raise self._protocolError(...)`。`_protocolError`（743-749 行）还被 `response.failed` / `error` 事件（decode）和 function_call 校验共用，不能在该方法里写死 `stage=streamEnd`。

**修复建议**：diag 放在 `consumeSseStream` **栈上**局部 dict（禁止 `self.diag`）。对 `buildCompletion()` 再包一层 try，或调用后 catch `modelRequestError` 再附着。`_protocolError` 的 stage 在调用点设：`response.failed`/`error`/`parseEvent` → `decode`；`terminalSeen=False` → `streamEnd`。Chat Completions 的 `processSseData` 同样：要么传入 diag，要么在 consume 出口 catch 后补 `decode`。

### 【P2】exceptionName / errno 必须在 except 现场取底层异常，不能用包装后的 `modelRequestError`

**描述**：G1 靠 exceptionName/errno 区分静默超时 vs 连接重置 vs broken pipe。现状所有 HTTP/SSE 失败出 adapter 后都是 `modelRequestError`（`errorType` 现已如此）。若 diag.exceptionName 也填 `modelRequestError`，streamRead 内部原因仍分不清。

**代码证据**：`openRequest` / consume 均 `raise modelRequestError(...) from error`（`chatCompletions.py:117-131, 194-201`）。底层是 `HTTPError` / `URLError` / `TimeoutError` / `OSError`，errno 在 `error.reason` 或 `error.errno`，不在包装类型上。

**修复建议**：在 **except 现场**写 `diag['exceptionName']=type(error).__name__`、`diag['errno']=getattr(error, 'errno', None) or getattr(getattr(error, 'reason', None), 'errno', None)`。`logModelError` 的 `errorType` 可继续是 `modelRequestError`（兼容旧字段）。

### 【P2】willRetry 必须与现有放弃谓词同一条，含 `chunkSeen`

**描述**：B2 要求调用方传入 willRetry。现状放弃条件是 `chunkSeen or not isRetryable or attempt >= MODEL_RETRY_MAX_ATTEMPTS`（`agent.py:236`）。若写成 `isRetryable and attempt < MAX` 而漏掉 `chunkSeen`，半截流失败会记 willRetry=true，实际立刻 `yield errorEvent` 返回。

**修复建议**：方案/实施注释写明：`willRetry = (not chunkSeen) and isRetryable and attempt < MODEL_RETRY_MAX_ATTEMPTS`。`backoffMs` 仅 willRetry=true 时有值，否则可空。`attempt` 与 `retryNoticeEvent` 一致用 1-based（现状 `attempt + 1`）。

### 【P2】t0 应在 `openRequest` 之前，connect 失败也要有 durationMs

**描述**：B1 把 t0 写在 consumeSseStream，openRequest 的 connect 失败若发生在计时前，G1 的 durationMs 在 403/502 上会缺。

**修复建议**：`t0 = time.monotonic()` 放在第一次 `openRequest` 前（Chat 的 consume 入口 / Responses 第一次 openRequest 前）。headers 在 urlopen 成功后立刻写入局部 diag（`response.headers`）；HTTPError 用 `error.headers`。

### 【P2】Chat Completions 对端静默 EOF 仍成功，timings 无法表达「有没有 [DONE]」

**描述**：用户问题是「response 为什么断了」。Chat 路径在无 `[DONE]` 时仍合成 finalChunk（`chatCompletions.py:194` 之后的合成逻辑，方案 §4.3 已承认「不是错误」）。结果 jsonl 只有带 timings 的 `assistantMessage`，看起来像正常说完。这不改语义（非目标/M2），但成功路径缺一个标量就无法事后区分「正常结束」和「对端 EOF 截断」。

**修复建议**：timings 增加可选 `sawDone: bool`（Chat：是否见到 `[DONE]`；Responses：是否 `terminalSeen`）。不加事件、不改重试、不保存半截。非必须，实施 B1 时顺手加最便宜。

### 【P2】sseGenError 不要把客户端断开写成服务端崩

**描述**：B4 已正确：`GeneratorExit` 再 raise、不记；其它 Exception 记 sseGenError。Starlette 在客户端断开时有时抛的是 `ClientDisconnect` / 连接写失败，而不是 GeneratorExit。若一律落 `sseGenError`，会轻微污染「服务端 SSE 崩」归因（比原 streamDisconnected 轻得多，且不是模型流断）。

**修复建议**：与 GeneratorExit 同等对待常见断开类型（按项目已有依赖捕捉，不要引入新框架）。无 conversation 时不要 `getConversation()`；`conversations.get` 为 None 则只 `print_exc`，与 B3 一致。sseGen 回调必须走 pump 方法（pump 已有 `self.agent` / `self.sessionId`，`agentManager.py:144-145`），不要在 sseCodec 里 import conversation。

### 【P2】成功 timings 与 §4.3 的 authRefresh 不完全对齐

**描述**：§4.3 说 401 refresh 的第二次 openRequest 成功/失败都记 `authRefresh=true`。§4.4 成功 timings 只有 `{ttfbMs, durationMs, chunks, textChars, reasoningChars}`，不含 authRefresh。成功 refresh 对「为什么断了」不是必须，但两次表述不一致。

**修复建议**：失败 diag 保留 `authRefresh`；成功 timings 可带同样可选标量，或在 §4.4 写明「成功路径不记 authRefresh」。

## 字段是否够回答「response 为什么断了」

对照 §1 的五种事后分不清：

| 现象 | v1.1 如何回答 | 够不够 |
|------|----------------|--------|
| 没连上 | stage=`connect` + status + exceptionName + durationMs | 够 |
| 首字前超时 | stage=`firstByte`，chunks=0，ttfb 空，exceptionName≈TimeoutError，durationMs≈300s | 够（前提：P2 exceptionName 取底层） |
| 流中静默超时 | stage=`streamRead`，chunks≥1，durationMs≈300s | 够 |
| 连接重置 | stage=`firstByte`/`streamRead` + exceptionName/errno（ECONNRESET） | 够（同 P2） |
| provider 主动断（HTTP 403/502） | stage=`connect` + status + requestId/cf-ray + 连续 modelError 的 attempt/willRetry | 够 |
| Responses 无 terminal 就 EOF | stage=`streamEnd` | 够（前提：P2 在 buildCompletion 处附着 diag） |
| Chat 无 [DONE] 当成功 | 不记错误；仅 timings | 弱信号，见 P2 `sawDone`；符合既有非目标 |
| 浏览器关一页 | 不写流级断连；另一页仍 attach | 够（A5） |
| 泵/SSE 编码崩 | pumpError / sseGenError + traceback | 够，且与模型断流分开 |

成功轮：`appendAssistantMessage` 已有 `'timings': responsePayload.get('timings')`（`conversation.py:173`），adapter 写入即可落盘，无需改 conversation。

## resume

```66:94:flamingoAgents/core/conversation.py
            if eventType == 'systemMessage':
                ...
                continue
            if eventType == 'modelError':
                continue
            if eventType == 'stopRequested':
                ...
                continue
            if eventType == 'userMessage':
                ...
            elif eventType == 'assistantMessage':
                ...
            elif eventType == 'toolResult':
                ...
```

`modelRequestStart` / `pumpError` / `sseGenError` 对上述分支都不是命中，循环自然进入下一 event。`assistantMessage.timings` 多一个键，resume 不读 timings，不影响 messages。无需改白名单，§4.2「conversation.py 不改」正确。

## 过度 / 欠设计

- 已砍：adapter 注入 logger、streamDisconnected、modelRetry、B5 debugConsole、半截落盘、改重试/超时。合适。
- 允许两 adapter 复制小函数、不强制新模块：匹配「最少代码」。
- `pickDiagHeaders` 放 `chatCompletions.py`、Responses 已 import `modelRequestError`：无环风险。
- 欠的只是上面 P2 实施陷阱，不需要改事件模型、不需要新日志系统。

## 实施顺序（方案 §5 仍适用）

B1 → B2 → B3 → B4。P2 全部是 B1/B2/B4 的写法约束，不新增 TODO 项；实施 B1 时把 diag 附着点按本报告 P2 覆盖 `buildCompletion` / `processSseData` 即可。

## 可以实施
