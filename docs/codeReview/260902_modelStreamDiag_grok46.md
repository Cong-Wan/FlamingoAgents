## 代码审核报告 — 模型流断连诊断日志补强

### 总览
- 审核文件：9 个（6 实现 + 1 测试 + 2 方案/复审）
- 发现问题：🔴 0 / 🟠 2 / 🟡 3 / 🔵 3
- 整体评价：B1–B4 事件模型和 P2 写法约束基本落地（栈上 diag、adapter 不写 jsonl、willRetry 含 chunkSeen、buildCompletion 外层附着、sseGen 区分客户端断开）。主要风险不在语义，而在「采集失败不得打断主流程」：两处 except 里诊断代码一旦再抛，会盖掉真正的模型/SSE 异常。

对照：`docs/plan/modelStreamDiagnosisPlan.md` v1.2 §3–§5 / §4.6，`docs/plan/modelStreamDiagnosisReview2.md` P2。

---

### 问题清单

### [High] Responses 401 失败路径上 mergeErrorDiag 再抛会吞掉真正的 modelRequestError

**位置**: `flamingoAgents/models/responsesAdapter.py` `consumeSseStream`

**问题**: 非 401、以及 refresh 后第二次 `openRequest` 失败时，都是先 `mergeErrorDiag(...)` 再 `raise`。`mergeErrorDiag` 会改 dict、读 `__cause__`、写 `error.diag`，内部无 try。方案 §4.1 / B1：「采集失败必须吞掉，不得打断主流程」。这里一旦 diag 赋值失败，except 里抛出的就不再是 403/502/401，agent 重试谓词（`statusCode` / `retryable`）会看错对象，主流程被诊断代码带偏。

同函数里 401 之后的 `authResolver.resolve(forceRefresh=True)` 也不在 try 中：refresh 自己抛错时，原 401 的 connect diag 不会挂上。

**修复方案**:
diag 必须包 try；原错误无条件再 raise。401 先附着再 refresh 更稳妥，但 refresh 抛错属既有行为，不挡 High 的最小补丁。

```python
except modelRequestError as error:
    if error.statusCode != 401 or self.config.authType != 'oauth':
        try:
            mergeErrorDiag(error, diag, stage='connect', underlying=error.__cause__)
        except Exception:
            pass
        raise
    authRefresh = True
    try:
        auth = self.authResolver.resolve(forceRefresh=True, staleAccess=auth.accessToken)
        response = self.openRequest(requestPayload, auth, sessionId=sessionId)
    except modelRequestError as refreshError:
        try:
            mergeErrorDiag(refreshError, diag, stage='connect', underlying=refreshError.__cause__)
            diag['authRefresh'] = True
            refreshError.diag = diag
        except Exception:
            pass
        raise
```

更干净的做法是让 `mergeErrorDiag` 自身吞掉内部异常，所有调用点一起受益（Chat `complete` / `consumeSseStream` 同样裸调）。

---

### [High] sseGen 记录 sseGenError 时若回调抛错，会盖掉真正的生成器异常

**位置**: `webApp/backend/sseCodec.py` `sseGen`

**问题**:

```python
except Exception as error:
    if not isClientDisconnect(error) and pump is not None:
        pump.logSseGenError(error)
    raise
```

`logSseGenError` 会 `format_exc` / `print_exc` / `_logDiagEvent`。`_logDiagEvent` 内部有 try，但 `print_exc()` 没有。诊断回调再抛时，Starlette 看到的不再是原来的编码/队列异常，G3「sseGen 意外异常」归因错位，也违反采集失败不得影响主流程。

**修复方案**:

```python
except GeneratorExit:
    raise
except Exception as error:
    if not isClientDisconnect(error) and pump is not None:
        try:
            pump.logSseGenError(error)
        except Exception:
            pass
    raise
```

---

### [Medium] Chat `complete()` 把所有 modelRequestError 都标成 stage=connect

**位置**: `flamingoAgents/models/chatCompletions.py` `complete`

**问题**: `with self.openRequest(...) as response:` 成功后 `response.read()` 理论上仍可能（当前没有）转成 `modelRequestError`。except 无条件 `stage='connect'`。流式路径用了 `opened` 区分 connect / 后续，非流没有。现在 read 失败走 OSError → `firstByte`，实际还对；接口不对称，以后容易标错。

**修复方案**: 与 consume 一样用 `opened`，或只在 `not getattr(error, 'diag', None)` 时补 connect。非必须。

---

### [Medium] 成功 refresh 后的读失败仍带 authRefresh=true

**位置**: `responsesAdapter.py` `consumeSseStream` 读循环 / `buildCompletion` 的 except

**问题**: 第一次 401 后 `authRefresh=True`，第二次 open 成功后若 `streamRead`/`streamEnd`，失败 diag 仍写 `authRefresh=true`。方案更自然的口径是「这次失败发生在 refresh 重试的 connect 上」。成功 timings 不含该字段，红线没破。

**修复方案**: 只在第二次 `openRequest` 失败时写 `authRefresh`。可以改，不挡验收。

---

### [Medium] mergeErrorDiag 用 underlying 覆盖已采集的 exceptionName

**位置**: `chatCompletions.py` `mergeErrorDiag`

**问题**: 先把 `error.diag` 拷进栈上 diag，再 `applyExceptionDiag(diag, underlying)`。connect 路径 `underlying=error.__cause__` 一般仍是 HTTPError/URLError，与 openRequest 写入一致。若 `__cause__` 缺失或被换成包装类型，会把已经采对的底层名盖掉，部分违反 P2「取底层异常」。

**修复方案**: 仅当栈上还没有 `exceptionName` 时才 apply underlying；或 connect 路径不再传 underlying。非必须。

---

### [Low] `_noteStreamYield` 两 adapter 各一份

**位置**: `chatCompletions.py` / `responsesAdapter.py`

**问题**: TTFB / chars 口径相同。方案允许复制。以后改「usage-only 不算 TTFB」容易只改一边。

**修复方案**: 抽到 `chatCompletions.py` 与 `elapsedMs` 一起再 import。非必须。

---

### [Low] `newStreamDiag` 为 requestBytesLen 再 dumps 一遍 payload

**位置**: `chatCompletions.py` `newStreamDiag`

**问题**: `openRequest` 已 dumps。多一次序列化，功能正确。为保持 openRequest 签名、兼容 mock，可接受。

**修复方案**: 维持现状。

---

### [Low] Chat 内嵌 error 对象的 decode 没有 exceptionName

**位置**: `chatCompletions.py` `processSseData` HTTP 200 内嵌 `error`

**问题**: `raise modelRequestError(...)` 无 `from`，`__cause__` 为 None，stage=`decode` 但无 exceptionName。符合「不要填包装后的 modelRequestError」。G1 允许缺字段。

**修复方案**: 保持空缺。

---

### 优点记录

- diag 是 consume 栈上局部 dict，没有 `self.diag`；adapter 不写 jsonl。
- `t0` 在第一次 `openRequest` 之前（含 Responses 401 refresh），connect 失败也有 `durationMs`。
- `exceptionName`/`errno` 在 HTTPError/URLError/TimeoutError/JSONDecodeError 现场取，不是包装类型。
- `willRetry = (not chunkSeen) and isRetryable and attempt < MODEL_RETRY_MAX_ATTEMPTS`，半截流不会标还将重试；`backoffMs` 仅 willRetry 时有值；attempt 1-based。
- Responses `buildCompletion()` 单独包 try：`terminalSeen=False` → `streamEnd`，其余协议错 → `decode`；`_protocolError` 未写死 stage。
- 成功 timings 含 `sawDone`，不含 headers/requestId/authRefresh。
- `pickDiagHeaders` 全包 try，headers=None 不打断主流程。
- 泵 / sseGen 只用 `conversations.get`，禁止 `getConversation()`。
- sseGen：`GeneratorExit` 再 raise；`ClientDisconnect`/BrokenPipe/ConnectionReset/Aborted 不记 `sseGenError`；正常 `None` 哨兵不写。
- `_resumeFromLog` 未改；`modelRequestStart`/`pumpError`/`sseGenError` 自然跳过，测试覆盖。
- 重试谓词、300s 超时、半截不落盘、SSE 帧格式均未改。

---

### 方案约束核对

| 约束 | 结果 |
|------|------|
| adapter 不写 jsonl | 通过 |
| diag 禁止 self.diag | 通过 |
| willRetry 含 chunkSeen | 通过 |
| exceptionName 取底层异常 | 通过（见 Medium：underlying 可能覆盖） |
| t0 在第一次 openRequest 前 | 通过 |
| buildCompletion 外层附着 diag | 通过 |
| _protocolError 不写死 stage | 通过 |
| GeneratorExit/ClientDisconnect 不记 sseGenError | 通过 |
| 无 conversation 禁止 getConversation | 通过 |
| 成功 timings 不记 authRefresh | 通过 |
| headers 解析失败不打断主流程 | 通过 |
| 采集失败不得打断主流程 | **未完全**：见两条 High |
| 不改重试/计费/恢复语义 | 通过 |

---

### 修复优先级建议

1. **必须修**：`mergeErrorDiag`（或所有调用点）包 try，避免诊断代码盖掉 modelRequestError。
2. **必须修**：`sseGen` 里 `logSseGenError` 包 try，再 re-raise 原异常。
3. Medium 的 authRefresh 口径、complete() stage、underlying 覆盖可随后收紧，不挡 v1 验收。

**有必须修复的 Critical/High：2 条 High，无 Critical。**
