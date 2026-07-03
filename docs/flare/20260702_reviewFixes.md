# FlamingoAgents 审核遗留问题修复计划

> **面向智能体工作者：** 本计划处理 `docs/codeReview/260702_pureLibraryAgentRuntime.md` 中除 🟠 High（已修复）外的 6 个遗留问题。建议使用 executing-plans 内联逐任务实现。步骤使用复选框（`- [ ]`）追踪。

**目标：** 以最小改动堵掉审核发现的校验语义漏洞、库资源回收缺口和验证脚本污染，使 `manualChecks.py all` 在新增检查项后仍全绿，且不改变既有对外行为。

**技术栈：** Python 3.12、uv、无框架手动验证（`manualChecks.py` + `expect()` + `RuntimeError`）。

---

## 问题状态核对表（对照审核报告）

| # | 等级 | 问题 | 当前状态 | 本计划处置 |
|---|------|------|----------|-----------|
| 0 | 🟠 High | 跨会话共享 dict 数据竞争 | ✅ 已修复（pending 收进会话、根表加短锁） | 不在本计划范围 |
| 1 | 🟡 Medium | bool 被当作合法 integer 通过校验 | 仍成立（`toolRuntime.validateValue`） | **Task 1 修复** |
| 2 | 🟡 Medium | 会话/会话锁表无界增长 | 部分变化（`pendingConfirms` 已删）；`conversations`/`sessionLocks` 仍无界增长 | **Task 2 修复（需确认）** |
| 3 | 🔵 Low | manualChecks 污染进程环境变量 | 仍成立（`TEST_API_KEY` 不还原） | **Task 3 修复** |
| 4 | 🔵 Low | 每个模型步重建 tools schema | 仍成立（`continueModelLoop` 循环内） | **Task 4 修复** |
| 5 | 🔵 Low | redactText 三段赋值可读性 | 仍成立 | **Task 5（可选）** |
| 6 | 🔵 Low | shell runtime 全量缓存输出 | 仍成立（`subprocess.run(capture_output=True)`） | **暂不修复，记录** |

---

## 范围决策（执行前需你拍板）

1. **Task 2 是否扩展库 API**：`closeSession` 是新增公开方法（库边界变更）。备选是「不扩 API，仅在 agent 类 docstring 注明回收责任由宿主承担」。**默认按「扩展 closeSession」执行**，因为这是无界增长的正路且改动小。
2. **Task 5（redactText）是否改**：纯风格，收益极低。**默认不改**，仅列在计划里供你选择。
3. **Task 6（shell 缓存）**：流式截断需用 `Popen` 逐块读取，复杂度高，本地工具输出通常不大。**默认本期不修**，仅记录。

下面 Task 1–4 为推荐执行项，Task 5 可选，Task 6 仅说明。

---

## 文件结构

```plain
flamingoAgents/tools/toolRuntime.py    Task 1：validateValue integer 分支排除 bool
flamingoAgents/core/agent.py           Task 2：新增 closeSession；Task 4：buildModelTools 提到循环外
manualChecks.py                        Task 2：新增 runSessionLifecycleCheck；Task 3：还原 TEST_API_KEY
flamingoAgents/utils/redaction.py      Task 5（可选）：redactText 改循环
```

任务依赖：Task 1 / 3 / 4 / 5 互相独立；Task 2 同时改 `agent.py` 与 `manualChecks.py`，与 Task 4（也改 `agent.py`）改不同函数，顺序执行即可。所有任务最终验证都依赖 `manualChecks.py all` 不回归。

---

### Task 1: integer 校验排除 bool

**目标：** 模型给 integer 字段回传 JSON `true/false` 时被校验拒绝，而不是被当成 0/1 放行。

**涉及文件：** `flamingoAgents/tools/toolRuntime.py`

#### Step 1 — 实现

定位 `validateValue` 的 integer 分支（约 L92）：

```python
    if expectedType == 'integer':
        if not isinstance(value, int):
            return f'{path} 必须是整数'
```

改为：

```python
    if expectedType == 'integer':
        if not isinstance(value, int) or isinstance(value, bool):
            return f'{path} 必须是整数'
```

> 原因：`bool` 是 `int` 子类，`isinstance(True, int)` 为 `True`。显式排除 bool 才能让 schema 校验语义严谨。

#### Step 2 — 验证

```bash
$ uv run python - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory
from flamingoAgents.core.types import toolContext
from flamingoAgents.tools.toolConfig import loadToolConfig
from flamingoAgents.tools.toolRuntime import executeTool

definitions = loadToolConfig()
bashDefinition = next(d for d in definitions if d.name == 'bash')
with TemporaryDirectory() as tempDir:
    context = toolContext(workDir=Path(tempDir))
    badResult = executeTool(bashDefinition, {'command': 'echo hi', 'timeout': True}, context, 'c_bool')
    assert badResult.isError, 'bool 应被 integer 校验拒绝'
    assert badResult.details.get('schemaError'), badResult.content
    okResult = executeTool(bashDefinition, {'command': 'echo hi', 'timeout': 5}, context, 'c_int')
    assert not okResult.isError, okResult.content
print('PASS bool rejected as integer')
PY
# 预期：输出 PASS bool rejected as integer；timeout=True 报 schemaError，timeout=5 正常执行。
```

✅ **完成标志：** 上述命令输出 `PASS bool rejected as integer`，且 `manualChecks.py all` 仍 8 项全绿。

---

### Task 2: 提供 closeSession 回收会话表

**目标：** 宿主可显式回收已结束会话的 `conversations` 与 `sessionLocks` 条目，避免长生命周期 agent 随会话数线性泄漏内存。

**涉及文件：** `flamingoAgents/core/agent.py`、`manualChecks.py`

#### 并发语义说明

- `closeSession(sid)` 先获取该 session 的锁（`getSessionLock`），确保没有正在进行的会话操作；再在 `sessionLocksGuard` 短临界区内 pop 两个表的条目。
- 锁顺序固定为「session 锁 → sessionLocksGuard」，与 `getSessionLock`（「sessionLocksGuard → 释放 → 返回锁」）不构成反向等待，无死锁。
- `closeSession` 后若宿主再用同一 `sid` 调用，`getConversation` 会重建会话（`existed=False` 路径已验证）。

#### Step 1 — 实现

在 `flamingoAgents/core/agent.py` 的 `agent` 类中（建议放在 `getConversation` 之后、`createSessionId` 之前）新增：

```python
    def closeSession(self, sessionId: str) -> bool:
        with self.getSessionLock(sessionId):
            with self.sessionLocksGuard:
                existed = sessionId in self.conversations
                self.conversations.pop(sessionId, None)
                self.sessionLocks.pop(sessionId, None)
        return existed
```

> 注：`with self.getSessionLock(sessionId)` 会先（短暂）持有 `sessionLocksGuard` 取得锁对象后释放，再 `with` 该锁；随后再次进入 `sessionLocksGuard` 执行 pop。对不存在的 sessionId，`getSessionLock` 会惰性创建一把锁随即被 pop，净效果干净，返回 `False`。

在 `manualChecks.py` 新增检查函数（建议放在 `runAgentStateCheck` 之后、`runPureLibraryApiCheck` 之前）：

```python
def runSessionLifecycleCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 session 生命周期检查')
    with TemporaryDirectory() as tempDir:
        workDir = Path(tempDir)
        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        testAgent = buildFakeAgent(workDir, debugEnabled)
        readResult = testAgent.runUserMessage('please read sample', sessionId='lifecycle')
        expect(readResult.status == 'completed', readResult.message)
        expect('lifecycle' in testAgent.conversations, '会话应已登记')
        removed = testAgent.closeSession('lifecycle')
        expect(removed is True, 'closeSession 应回报已删除')
        expect('lifecycle' not in testAgent.conversations, 'conversations 应已回收')
        expect('lifecycle' not in testAgent.sessionLocks, 'sessionLocks 应已回收')
        expect(testAgent.closeSession('never') is False, '关闭不存在的会话应返回 False')
        reborn = testAgent.runUserMessage('please read sample', sessionId='lifecycle')
        expect(reborn.status == 'completed', 'closeSession 后应能重建会话')
    printPass('session lifecycle')
```

更新 `manualChecks.py` 的 `argparse` choices 与 `main` 分发（在 `'agent'` 与 `'pureLibrary'` 之间插入 `'session'`）：

```python
    parser.add_argument('check', choices=[
        'all', 'toolConfig', 'permission', 'runtime', 'logger', 'adapter', 'modelAuth', 'agent', 'session', 'pureLibrary',
    ])
    ...
    if args.check in {'all', 'agent'}:
        runAgentStateCheck(args.debug)
    if args.check in {'all', 'session'}:
        runSessionLifecycleCheck(args.debug)
    if args.check in {'all', 'pureLibrary'}:
        runPureLibraryApiCheck(args.debug)
```

#### Step 2 — 验证

```bash
$ uv run python manualChecks.py session --debug
# 预期：输出 PASS session lifecycle。
$ uv run python manualChecks.py all
# 预期：9 项全 PASS（新增 session lifecycle）。
```

✅ **完成标志：** `manualChecks.py all` 输出 9 项 PASS（含 `session lifecycle`），运行无异常。

---

### Task 3: manualChecks 还原测试用环境变量

**目标：** `runModelAuthCheck` 设置的 `TEST_API_KEY` 在检查结束后从进程环境还原，避免污染后续检查或同进程其它逻辑。

**涉及文件：** `manualChecks.py`

#### Step 1 — 实现

定位 `runModelAuthCheck`（约 L191 起）。当前在函数中段执行：

```python
        os.environ['TEST_API_KEY'] = 'env-secret'
```

且从不还原。将该函数体用 `try/finally` 包裹，确保还原。最小改动方式：把整个函数体缩进进 `try`，在 `finally` 中 pop：

```python
def runModelAuthCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 model config / auth 检查')
    try:
        with TemporaryDirectory() as tempDir:
            ... （原有全部逻辑保持不变，仅整体缩进一级）...
        auth = createModelAuth('abc123')
        expect(auth.authorizationHeader == 'Bearer abc123', 'Authorization header 生成失败')
        sourceText = Path('flamingoAgents/models/chatCompletions.py').read_text(encoding='utf-8')
        expect('os.getenv' not in sourceText, 'adapter 不应包含 os.getenv')
        expect('jsonlLog' not in sourceText, 'adapter 不应依赖 jsonlLog')
        printPass('model config auth adapter')
    finally:
        os.environ.pop('TEST_API_KEY', None)
```

> 用 `pop(..., None)` 而非 `del`，避免变量本就未设置时抛 `KeyError`。

#### Step 2 — 验证

```bash
$ uv run python - <<'PY'
import os
assert 'TEST_API_KEY' not in os.environ
from manualChecks import runModelAuthCheck
runModelAuthCheck(False)
assert 'TEST_API_KEY' not in os.environ, 'TEST_API_KEY 未还原'
print('PASS env var restored')
PY
# 预期：输出 PASS env var restored；检查前后 TEST_API_KEY 均不存在。
```

✅ **完成标志：** 上述命令输出 `PASS env var restored`，且 `manualChecks.py all` 全绿。

---

### Task 4: tools schema 提到模型循环外

**目标：** `buildModelTools` 结果在一次 `continueModelLoop` 内不变，无需每个 step 重建。

**涉及文件：** `flamingoAgents/core/agent.py`

#### Step 1 — 实现

定位 `continueModelLoop`（约 L89）。当前：

```python
    def continueModelLoop(self, sessionId: str) -> runResult:
        currentConversation = self.getConversation(sessionId)
        for stepIndex in range(self.maxModelSteps):
            modelTools = buildModelTools(list(self.toolDefinitions.values()))
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(currentConversation.messages)} tools={len(modelTools)}'
                )
            ...
```

把 `modelTools` 提到循环外：

```python
    def continueModelLoop(self, sessionId: str) -> runResult:
        currentConversation = self.getConversation(sessionId)
        modelTools = buildModelTools(list(self.toolDefinitions.values()))
        for stepIndex in range(self.maxModelSteps):
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(currentConversation.messages)} tools={len(modelTools)}'
                )
            ...
```

> debug 日志仍引用 `len(modelTools)`，提到外面后照常可用。行为不变。

#### Step 2 — 验证

```bash
$ uv run python manualChecks.py agent
# 预期：PASS agent state machine（行为不变）。
```

✅ **完成标志：** `manualChecks.py all` 全绿，agent 状态机检查无回归。

---

### Task 5（可选）: redactText 改为规则循环

**目标（弱）：** 把三段重复的 `pattern.sub(...)` 收敛成一个 `(pattern, repl)` 循环，提升可读性。纯风格改动，**默认不改**。

**涉及文件：** `flamingoAgents/utils/redaction.py`

#### Step 1 — 实现（若选择执行）

当前 `redactText`：

```python
def redactText(text: str) -> str:
    redactedText = text
    redactedText = secretPatterns[0].sub(lambda match: f'{match.group(1)}{match.group(2)}<redacted>', redactedText)
    redactedText = secretPatterns[1].sub(lambda match: f'{match.group(1)}<redacted>', redactedText)
    redactedText = secretPatterns[2].sub('sk-<redacted>', redactedText)
    return redactedText
```

可改为：

```python
def redactText(text: str) -> str:
    redactors = [
        (secretPatterns[0], lambda match: f'{match.group(1)}{match.group(2)}<redacted>'),
        (secretPatterns[1], lambda match: f'{match.group(1)}<redacted>'),
        (secretPatterns[2], 'sk-<redacted>'),
    ]
    for pattern, repl in redactors:
        text = pattern.sub(repl, text)
    return text
```

> 说明：改动后行数基本不变、还多出一个列表字面量，收益有限。原写法已足够清晰。

#### Step 2 — 验证

`runLoggerCheck`（jsonl logger）已覆盖脱敏行为，无需新增检查。

✅ **完成标志：** `manualChecks.py logger` 通过即可（仅在选择执行本任务时）。

---

### Task 6: shell runtime 全量缓存输出 —— 暂不修复（记录）

**现状：** `executeShellRuntime` 用 `subprocess.run(capture_output=True)`，把全部 stdout/stderr 读进内存后才 `makePreview` 截断。命令输出巨大时会瞬时占用内存。

**为何本期不修：**
- 真正修复需改用 `subprocess.Popen` 逐块读取、达到预览上限后丢弃剩余（甚至 kill 进程），代码量与复杂度显著上升。
- 本地工具场景输出通常不大，实际内存风险低。
- 属工具固有局限，与 agent 正确性无关。

**若将来要修的方向（仅记录，不在本期实现）：**
- 用 `Popen` 启动，循环 `read(chunk)` 累积到 `previewLimit` 后停止读取并 `terminate()`，剩余输出丢弃；同时保留退出码语义。

---

## 自我复审

**1. 范围对照：** Task 1–4 覆盖报告中除已修复 High 外的全部 Medium 与可执行的 Low；Task 5 为可选项、Task 6 明确不修并说明理由，无遗漏。

**2. 改动幅度：** Task 1（1 行）/ Task 3（try/finally 包裹）/ Task 4（1 行上移）均为最小改动；Task 2 新增一个方法 + 一个检查函数，是必要的 API 扩展。无过度设计。

**3. 并发安全（Task 2）：** `closeSession` 锁顺序与 `getSessionLock` 一致、无反向等待；对不存在 sessionId 返回 `False` 且不留垃圾；close 后重建路径已纳入验证。

**4. 验证完整性：** 每个任务给出确切内联验证命令或 `manualChecks.py` 子命令与预期输出；全部完成后 `manualChecks.py all` 应为 9 项 PASS（新增 `session lifecycle`）。

**5. 版本号建议：**
- `toolRuntime.py` 1.0 → 1.1（Task 1）
- `agent.py` 1.4 → 1.5（Task 2 + Task 4）
- `manualChecks.py` 2.0 → 2.1（Task 2 + Task 3）
- `redaction.py` 仅 Task 5 执行时 1.0 → 1.1

---

## 执行交接

计划已保存到 `docs/flare/20260702_reviewFixes.md`。请确认上方「范围决策」三点（尤其 Task 2 是否扩 `closeSession` API、Task 5 是否执行），确认后按既定**内联执行**方式逐任务实现，每任务跑其验证命令，最后跑 `manualChecks.py all` 做全量回归。
