# 模型流断连诊断方案评审（对照源码，v1.0 → 要求修订）

- 日期：2026-09-01
- 对象：`docs/plan/modelStreamDiagnosisPlan.md` v1.0
- 结论：**需修复后通过**（无 P0；4 个 P1 必须改方案再实施；若干 P2）

说明：两次外部子代理评审无效（一次幻觉出不存在的 asyncio/`_process_stream`，一次未产出报告）。本报告依据真实源码交叉核对。

## 各维度

| 维度 | 结论 |
|------|------|
| O1–O8 与代码吻合 | 大体吻合；**O5 陈述错误**（见 P1-4）；O2 对 SSE 已启动后的异常覆盖不足（见 P1-3） |
| B1–B6 能否归因 | B1+B2 是核心且可行；B6 的「adapter 注入 logger」不可行（P1-1）；B4 按原文会误报（P1-2）；B5 对 Web 几乎无效（P2） |
| stage 口径 | 分类合理，但必须写清「谁在哪条 except 里判定」——openRequest 已把 HTTP/URL 错误转成 modelRequestError，consumeSseStream 的 OSError except **接不到 connect 失败**（P1-5） |
| resume 兼容 | **无问题**。`conversation._resumeFromLog` 只处理 system/user/assistant/toolResult/modelError/stopRequested，未知 type 自然跳过 |
| 过度/欠设计 | B5 过度；adapter 注入 logger、streamDisconnected 语义欠设计 |

## 问题列表

### 【P1-1】adapter 不能持有 per-session logger，B6 传递路径不成立

**证据**：`builder.py` 每个 agent 只建一个 adapter；`agent.conversations` 是 `{sessionId: conversation}`，logger 在 conversation 上。adapter 的 `activeResponses` 跨会话共享。

**问题**：方案 §4.2「modelRequestStart 由 adapter openRequest 前写（经注入的 logger）」会迫使 adapter 绑定某一个 session logger，多会话并行时写错文件或需要改端口。

**修复**：adapter **不写 jsonl**。agent 在 `driveModelLoop` 调用 `completeStream` 前写 `modelRequestStart`（它已有 sessionId / attempt / messageCount / lastTurnTokens）。adapter 只做两件事：局部 diag；失败附着 `error.diag`；成功写入 `responsePayload['timings']`。`requestBytesLen`/`baseUrl`/`api`/`responseHeaders` 放在 diag/timings 里由 agent 合并进 modelError / assistantMessage。

### 【P1-2】streamDisconnected 按「sseGen finally」落盘会误报

**证据**：`sseCodec.py` `sseGen` finally **每次** unsubscribe，包括：(1) 正常 `event is None` return；(2) 客户端断开 GeneratorExit；(3) 编码异常。`agentManager` 是多订阅者广播，一窗口关页不影响泵继续跑。

**问题**：G2/A4「关页就记 actor=client」会把正常收尾和多窗口关一页都记成断连，污染归因。

**修复**：
- 正常哨兵关闭：不记。
- GeneratorExit（客户端断开）：**不记流级断连**；可选记 `sseSubscriberLeft`（remaining 计数），v1 可砍掉。
- sseGen 非 GeneratorExit 的 Exception：记 `sseGenError`（堆栈）。
- 泵 except：只走 `pumpError`（B3），不要和断连事件混写。

用户真问题是 **上游模型流断**，不是浏览器 SSE 断。O4 降为 P2，B4 收缩为「sseGen 意外异常 + fallbackErrorHandler 打堆栈」。

### 【P1-3】fallbackErrorHandler 打不到 StreamingResponse 生成器里的异常

**证据**：`server.py` `fallbackErrorHandler` 只处理路由函数抛错。`chatStream` 在返回 `StreamingResponse` 时已经成功；之后 `sseGen` 里的异常由 Starlette 直接拆连接，**不会进这个 handler**。

**修复**：`traceback.print_exc()` 仍要加（覆盖建流前的 500）。sseGen 内部自己 try/except 非 GeneratorExit 异常并经 pump 落盘。方案不得声称「只改 fallbackErrorHandler 就能看到 SSE 中途崩」。

### 【P1-4】O5「重试不落盘」与代码不符

**证据**：`agent.py` `driveModelLoop` 的 except 里 **先** `logModelError`，**再** 判断是否重试。jsonl 里同一 502 出现多条，正是重试留下的 modelError，不是「只有最终失败」。

**问题**：缺的不是事件本身，而是 attempt / willRetry / backoffMs，多条 modelError 分不清「还要试」还是「放弃」。

**修复**：改 O5 表述。B2 给现有 modelError 加 `attempt`、`willRetry`、`backoffMs`。不再单列 `modelRetry` 事件（避免与已有 modelError 重复，也更符合 G7）。

### 【P1-5】stage=connect 不在 consumeSseStream 的 OSError except 里

**证据**：`chatCompletions.openRequest` 已把 `HTTPError`/`URLError` 转成 `modelRequestError` 抛出。`consumeSseStream` 的

```
except (urllib.error.URLError, http.client.HTTPException, OSError)
```

接的是 **openRequest 成功之后** 的 read 中断。`processSseData` 的 JSON 错误直接 `raise modelRequestError`，也不走这条 except。Responses 还有 `except modelRequestError: raise`，diag 必须在 **构造 modelRequestError 的每一处** 附着，而不是只在最外层 OSError 分支。

**修复**：方案写明判定点：
- connect：`openRequest` 的 HTTPError/URLError
- firstByte / streamRead：`iterSseData` read 失败，用当时 chunks==0 区分
- decode：`processSseData` / `parseEvent` JSON 失败
- streamEnd：**仅 Responses** `buildCompletion` 在 `terminalSeen=False` 时；Chat Completions 无 [DONE] 仍会合成 finalChunk，**不是错误**

另：`openRequest` 成功时就要记下 `response.headers`（白名单），HTTPError 时记 `error.headers`，否则 requestId 采集落空。

## P2

- **B5 debugConsole 落盘**：Web `agentManager.getAgent` 调用 `createAgent` **不传 debug=True**，改 debugConsole 对「断了查不到」无帮助。v1 删除 B5。
- **A3「textChars 均 >0」**：纯 tool_calls 轮次 content 为空，会误判失败。改为 timings 对象存在且 durationMs≥0，text/reasoning 允许 0。
- **§2.1 / M1 仍写 timeout=60**：已改为 300s，文档同步。
- **Responses 401 内部 refresh 重试**：`responsesAdapter.consumeSseStream` 在 agent 重试循环之外再打一次 401→refresh→重发，jsonl 不可见。diag 加 `authRefresh=true` 即可，不单开事件。
- **A7「不传 logger 则跳过」**：agent 创建 conversation 时必有 jsonlLog。诊断应默认写入会话 jsonl；约束改为「采集失败不得打断主流程」。
- **G2b/O8 半截内容**：正确放在非目标/M2，保持。
- **M1 数字**：基线已是 300s，分层超时等数据后再定，删除过时的「替代 60s」。
