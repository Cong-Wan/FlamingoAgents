## 代码审核报告 — 模型流断连诊断日志补强（B1–B4）

### 总览
- 审核文件：7 个（6 个实现 + 1 个测试）
- 发现问题：🔴 0 个 / 🟠 0 个 / 🟡 2 个 / 🔵 3 个
- 整体评价：按 v1.2 方案落地，diag 在 consume 栈上、adapter 不写 jsonl、willRetry 含 chunkSeen、buildCompletion 外层补附着、sseGen 不把客户端断开写成服务端崩。无必须修复的 Critical/High。pytest 68 passed。

对照：`docs/plan/modelStreamDiagnosisPlan.md` v1.2 与 `docs/plan/modelStreamDiagnosisReview2.md` P2。

---

### 问题清单

### [Medium] Responses 成功 refresh 后的后续失败也会带 authRefresh

**位置**: `flamingoAgents/models/responsesAdapter.py` `consumeSseStream` 读循环 / `buildCompletion` 的 `except modelRequestError`

**问题**: 401 第一次失败后 `authRefresh=True`。第二次 `openRequest` 成功后，若 SSE 读失败或 `buildCompletion` 报 `streamEnd`，仍会写 `diag['authRefresh']=True`。方案原文是「Responses 401 内部 refresh：仅失败 diag 记 authRefresh=true」，更自然的理解是「这次失败发生在 refresh 重试上」（第二次 connect 失败），而不是「本尝试曾经 refresh 过」。归因时可能把「refresh 后的 streamRead」看成「refresh 本身失败」。

**修复方案**:
当前行为仍只出现在失败 diag、成功 timings 不含 authRefresh，不违反红线。若要收紧：

```python
# 仅第二次 openRequest 失败时记
except modelRequestError as refreshError:
    mergeErrorDiag(refreshError, diag, stage='connect', underlying=refreshError.__cause__)
    diag['authRefresh'] = True
    raise
# 读循环 / buildCompletion 的失败路径不要再写 authRefresh
```

建议：不改。记录「本尝试经过 refresh」对对账仍有用。

---

### [Medium] `_noteStreamYield` 在两个 adapter 各写一份

**位置**: `chatCompletions.py` / `responsesAdapter.py` 的 `_noteStreamYield`

**问题**: 计 TTFB / textChars / reasoningChars 的逻辑完全相同。方案允许复制小函数，不强制新模块，所以不是错误；后续改 TTFB 口径（usage-only 不算）时容易只改一边。

**修复方案**: 可以抽到 `chatCompletions.py` 与 `elapsedMs` 放一起再 import。非必须。

---

### [Low] `newStreamDiag` 为了 `requestBytesLen` 再 dumps 一遍 payload

**位置**: `chatCompletions.py` `newStreamDiag`

**问题**: `openRequest` 已经 `json.dumps` 出 `requestBytes`。consume 入口再 dumps 一次只为长度。热路径多一次序列化，功能正确。

**修复方案**: 维持现状（openRequest 签名未改、测试 mock 不受影响）。若以后要改，让 openRequest 把 `len(requestBytes)` 写回传入的 diag。

---

### [Low] `retryNoticeEvent.retryAfterMs` 静态类型是 `int`，传入变量是 `int | None`

**位置**: `flamingoAgents/core/agent.py` `driveModelLoop`

**问题**: `backoffMs` 标成 `None` 初值，但 yield `retryNoticeEvent` 只在 `willRetry` 为 true 之后，那时已赋 `int`。运行时安全，类型不整齐。

**修复方案**: 可写成 `retryAfterMs=int(backoffMs)` 或收窄分支内的局部变量。非必须。

---

### [Low] Chat 内嵌 error 对象的 decode 没有 exceptionName

**位置**: `chatCompletions.py` `processSseData` HTTP 200 内嵌 `error` 分支

**问题**: 该分支 `raise modelRequestError(...)` 没有 `from`，`__cause__` 为 None，diag.stage=`decode` 但没有 exceptionName。符合「不要填包装后的 modelRequestError」。G1 在无底层 OS 异常时允许缺字段。

**修复方案**: 保持空缺，或补一个与协议相关的标量（非本方案范围）。

---

### 优点记录

- diag 是 consume 栈上局部 dict，没有 `self.diag`。
- `t0 = time.monotonic()` 在第一次 `openRequest` 之前，connect 失败也有 `durationMs`。
- `exceptionName`/`errno` 取 `HTTPError`/`URLError`/`TimeoutError`/`JSONDecodeError`，不是包装类型。
- `willRetry = (not chunkSeen) and isRetryable and attempt < MODEL_RETRY_MAX_ATTEMPTS`，半截流不会标还将重试。
- Responses `buildCompletion()` 单独包 try：`terminalSeen=False` → `streamEnd`，其余协议错 → `decode`。
- `_protocolError` 未写死 stage。
- 成功 timings 含 `sawDone`，不含 headers/requestId/authRefresh。
- `pickDiagHeaders` 全包 try，headers=None 不打断主流程。
- 泵 / sseGen 只用 `conversations.get`，禁止 `getConversation()`。
- sseGen：`GeneratorExit` 再 raise；`ClientDisconnect`/写失败不记 `sseGenError`；正常 `None` 哨兵不写。
- `_resumeFromLog` 未改；新事件类型自然跳过，有测试覆盖。
- 重试谓词、超时 300s、半截不落盘均未改。

---

### 方案约束核对

| 约束 | 结果 |
|------|------|
| adapter 不写 jsonl | 通过 |
| diag 禁止 self.diag | 通过 |
| willRetry 含 chunkSeen | 通过 |
| exceptionName 取底层异常 | 通过 |
| t0 在第一次 openRequest 前 | 通过 |
| buildCompletion 外层附着 diag | 通过 |
| _protocolError 不写死 stage | 通过 |
| GeneratorExit/ClientDisconnect 不记 sseGenError | 通过 |
| 无 conversation 禁止 getConversation | 通过 |
| 成功 timings 不记 authRefresh | 通过 |
| headers 解析失败不打断主流程 | 通过 |
| 不改重试/计费/恢复语义 | 通过 |

---

### 修复优先级建议

1. 无 Critical/High，**可以实施结果直接保留**。
2. Medium 的 authRefresh 口径若产品上要区分「refresh 失败」和「refresh 后读失败」，再收紧；现在不必改。
3. `_noteStreamYield` 去重属于整洁度，不挡验收。
