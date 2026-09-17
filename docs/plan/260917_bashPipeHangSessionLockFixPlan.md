'''
Author: wilbur
Version: 1.1
Date: 2026-09-17
Description: 修复 bash/askSubAgent 在「Popen 首领已退出、子孙仍占 stdout/stderr 管道」时 communicate()/wait() 无界阻塞，导致 sessionLock 不放、后续 /chat/stream 200 但不落 jsonl。v1.1 按审核修订：T2 改用 python 孤儿写端钉死 communicate 路径；首领已 reap 仍无条件 SIGKILL；写死 communicate 续读/禁止 close 后再 communicate；T4 握手与时间预算；D2 理由改为 CLI 用满 timeout 且非 killpg；D3 明确 cmd& 产品语义为超时杀组。仅方案，确认前不实施。
'''

# bash 管道死等 / 会话锁不释放 修复方案

- Author: wilbur
- Version: 1.1
- Date: 2026-09-17
- 上游事故：`docs/260917_bashPipeHangSessionLockIncident.md`
- 相关方案：`docs/plan/stopResponsivenessPlan.md`（L3.5 引入 `_runWithInterrupt`）、`docs/plan/toolCallTranscriptClosureFixPlan.md`（中断后补 toolResult，不改子进程等待）
- 状态：**已实施**（v1.1 方案；`builtinTools.py` 1.8 + `tests/testRunWithInterrupt.py`）

---

## 0. 目标与非目标

### 0.1 目标（修完用户应感知到）

1. `bash` / `askSubAgent` 在 **timeout 到期** 或 **首领已退但管道 0.5s 未 EOF** 后，调用方一定能在 **该时刻 + 杀进程预算（≤1.5s）** 内返回（超时 `toolResult` 或等价错误），**不会**把泵线程钉死在 `communicate()` / `wait()` 上。
2. 工具执行中点停止：即便已经是「首领已死、管道被子孙占着」，`interruptEvent` 仍能让 `_runWithInterrupt` 在置位后 **≤2.5s** 抛出 `modelInterruptedError`（既有 `closeUnfinishedToolCalls` 随后写 cancellation `toolResult`）。
3. 事故形（Popen 首领已退出、子孙仍占 stdout/stderr）不再导致本会话后续消息「前端动画转、jsonl 不写」。
4. `interruptEvent is None` 的 CLI 路径与 Web 路径走 **同一套有界 Popen**（见 D2：不是因为 `subprocess.run` 无界，而是为了短宽限 + `killpg` + 中断切片）。

产品语义（D3，必须写死）：**Popen 首领已退出，且 0.5s 内管道未 EOF = 失败（当超时）并杀进程组**。  
因此 `sleep 5 & echo started` 这类「后台占管 + 前台 echo」修好后是 **超时 tool 卡**（stdout 里可能仍有 `started`），**不是** `exitCode=0` 的成功卡。这是放锁所付的代价，不是回归。

### 0.2 非目标（本期不做）

| 项 | 说明 |
|----|------|
| 跨线程强行释放 `sessionLock` | Python `RLock` 不能从别的线程 `release`。根因是生成器不离开 `with getSessionLock`；子进程有界返回后锁会随 `with` 退出。不引入锁租约/看门狗。 |
| 改 `requestStop` / `activeStreams` 时序 | stop 摘泵导致 attach 404 是既有设计；修的是摘泵后锁仍被幽灵生成器占用。 |
| 改前端 send/attach/乐观 UI | 前端行为正确；不要为了「等锁」去阻塞发送按钮。 |
| 给后台任务自动 `nohup`/重定向 | 模型生成的 bash 脚本内容不改写；只保证等待有上限。 |
| 存量已卡死的进程/会话自愈 | 不唤醒已经堵在 `communicate()` 里的旧泵。现场仍须重启 Web 并清残留 bash（事故文 §5）。 |
| 工具超时/管道死等 jsonl 诊断事件 | 另立项。本期超时仍走 `TimeoutExpired` → 现有 bash 超时 `toolOutput`。 |
| Windows 进程组与 POSIX `killpg` 对齐 | `killpg` 失败则 `terminate`/`kill` 单进程。有界等待 + 关读端在 Windows 同样能解开死等。 |
| 执行中持续排空管道以防 64KiB 填满 | 现状 poll 循环也不读管道；大输出仍靠 timeout 杀。不改成 select 循环。 |
| 对齐旧 G7「工具中断 ≤0.2s」 | 管道死等 / 忽略 TERM 路径本期验收是 **置位后 ≤2.5s**。活首领 + SIGTERM 有效时仍应接近现状（一轮 0.1s poll + 杀组）。不把 0.2s 当本期红线。 |

### 0.3 成功标准（可验证）

时间口径：T1/T3 从调用起算 **≤3.5s**（1s timeout + 0.1s 片 + 杀组预算 1.5s + 抖动）。T2 从调用起算 **≤2.5s**（首领几乎立刻退，0.5s 宽限 + 杀组，**不得**用满用户 timeout）。T4 从 `interruptEvent.set()` 起算 **≤2.5s**。

- [ ] **T1 活首领超时**：`timeout=1` 跑 `sleep 30`（测试侧 `bash --noprofile --norc -c`，见 §5），≤3.5s 抛 `TimeoutExpired`，Popen 子进程已被 reap。
- [ ] **T2 事故形（钉 190 行那次无界 communicate）**：命令必须是 **Python 孤儿写端**（§5），**禁止** `bash -lc 'sleep 30 &'`。`timeout=10`（刻意大于 2.5s），函数仍在 **≤2.5s** 抛 `TimeoutExpired`。断言还要：宽限开始前 Popen pid 已死（`poll() is not None`），且曾有子孙仍活。这样 T2 与 T1 不可互换。
- [ ] **T3 忽略 SIGTERM（活首领）**：`timeout=1` 跑 `trap "" TERM; sleep 30`，≤3.5s 返回；`process.wait()` 不得无界。只覆盖首领仍活 + 继承 SIG_IGN，不冒充事故形。
- [ ] **T4 停止叫醒管道死等**：用 T2 同一孤儿写端；子进程先写握手文件再占管；测试线程见到文件后立刻 `set()`。从置位起 ≤2.5s **必须** `modelInterruptedError`，TimeoutExpired 算失败。
- [ ] **T5 正常短命令**：`echo ok`，1s 内成功，stdout 含 `ok`，`returncode==0`。无孤儿写端，0.5s 宽限不得误杀。
- [ ] **T6 锁**：`with sessionLock: _runWithInterrupt(T2命令, timeout=10)` 返回后，另一线程 `acquire(timeout=0.2)` 成功。
- [ ] **T7**：不另写 askSubAgent 等待；单测只打 `_runWithInterrupt`。
- [ ] **T8 CLI 有界**：`interruptEvent=None` + T2 命令 + `timeout=10`，同样 ≤2.5s 得 `TimeoutExpired`（覆盖 D2 合并路径）。
- [ ] POSIX：`uv run pytest tests/testRunWithInterrupt.py` 全过。

---

## 1. 问题复述（方案口径）

事故链（PID 证据见上游文档）：

```text
driveToolBatch
  → executeToolCall
    → _runWithInterrupt
         Popen(stdout=PIPE, stderr=PIPE, start_new_session=True)
         while poll() is None:  # 只有这里看 timeout / interrupt
             sleep 0.1
         communicate()          # 现状约 190 行：poll 已返回后无超时
```

`poll()` 只跟踪 **Popen pid**（`start_new_session=True` 时等于 pgid）。首领退出后循环结束；`communicate()` 等的是 **管道 EOF**。子孙仍握写端 → 泵永久堵。

放大成「发消息 200、jsonl 不写」：

1. `_killProcessGroup`：首领已被 reap 时 `wait(0.5)` **立刻成功并 return**，SIGKILL 根本不发；即便走到 SIGKILL，其后 `process.wait()` 无超时。超时分支里的 `communicate()` 也无超时。
2. `requestStop` 会 `unregisterStream`（attach 404），生成器仍在 `with getSessionLock`。新 `/chat/stream` 能登记新泵、SSE 200，第一次迭代堵在锁上，`appendUserMessage` 不执行。

本期只修子进程等待。锁在生成器离开 `with` 后自然释放（`runUserMessageStream` 约 117 行）。

---

## 2. 决策（实施前锁定）

### D1. 只改 `builtinTools.py` 的等待/收尾 — **选定**

不改 `agent.py` 锁协议、不改 `agentManager.requestStop`、不改前端。

### D2. 取消 `interruptEvent is None` 的 `subprocess.run` 分支 — **选定（理由已改）**

CPython 3.12 POSIX 上 `subprocess.run(..., timeout=T)` **有界**：内部是 `communicate(..., timeout=T)`，超时后 `kill()` + `wait()` 首领。CLI **不会永久钉死**，但会：

- **用满用户 timeout** 等孤儿管道（bash 默认 30、上限 120；askSubAgent 上限 3600）；
- `kill()` 不是 `killpg`，组内残留打不到。

Web 事故路径是 `executeToolCall` **必注入** `interruptEvent`，走 Popen poll，然后无超时 `communicate()`。合并为有界 Popen 是为了 **D3 短宽限 + D4 killpg + D5 中断切片**，不是因为 `run` 无界。禁止实施时把 Web 改回 `run(timeout=)`。

### D3. 首领退出后管道宽限 0.5s；未 EOF = 超时杀组 — **选定**

不要 `communicate(timeout=剩余用户 timeout)`。  
`foo & echo started` 修好后是超时失败，见 §0.1。无孤儿写端的 `echo ok` 仍成功（EOF 微秒级）。

### D4. 杀进程：TERM 后固定等 0.5s，再无条件 SIGKILL；所有 wait/communicate 有界 — **选定**

`Popen.wait()` 成功 **只表示首领已收尸，不表示进程组空**。事故形首领早已 reap：`wait(0.5)` 立刻返回，若此处 `return` 则 **永不 SIGKILL**。

杀组步骤（无论首领是否还活）：

| 步骤 | 上限 |
|------|------|
| `killpg(SIGTERM)`（失败则 `terminate`） | — |
| **固定 `time.sleep(0.5)`**（不要 wait 成功就 return） | 0.5s |
| `killpg(SIGKILL)`（失败则 `kill`），无条件执行 | — |
| `wait(timeout=0.5)` | 0.5s |
| 再 `communicate(timeout=0.3)` 续读 | 0.3s |
| 仍超时：**close 读端，禁止再 communicate**；`wait(timeout=0.2)` 收尸 | 0.2s |
| 再失败 | 用已保存的部分输出或空串，按 reason raise（允许短暂僵尸） |

`os.killpg(process.pid, sig)`：pid==pgid。首领已 reap 时仍应打到组内残留；`ESRCH` 再退化单进程。

### D5. 中断检查覆盖宽限 communicate；算法写死，不以「实测为准」— **选定**

CPython 3.12：`communicate(timeout=)` 超时后 **可以再 communicate 续读**（内部 buffer / `_fileobj2output` 保留）。宽限阶段用切片 `timeout=min(0.1, remaining)` 续读，并在每片前查 interrupt。

**禁止** close 读端后再 `communicate`：关掉的是父进程读端，不会给自己 EOF；不写数据的子孙（`sleep`）也不会 SIGPIPE；随后 `fileno()` 典型 `ValueError`。超时路径会被 `bashTool` 只吃 `TimeoutExpired` 漏成 `toolRuntime` 普通异常；中断路径到不了 `modelInterruptedError`，`closeUnfinishedToolCalls` 不跑。

close 读端 = 放弃读。输出用最后一次 `TimeoutExpired.stdout/stderr` 或空串。

### D6. 测试用 pytest + `uv run pytest` — **选定**

新文件 `tests/testRunWithInterrupt.py`。真进程，不 mock `Popen`。

---

## 3. 设计

### 3.1 对外契约（不变）

`_runWithInterrupt(command, context, timeout) -> CompletedProcess`

| 结局 | 行为 |
|------|------|
| 正常退出且管道 EOF | 返回 `CompletedProcess` |
| 用户停止 | `raise modelInterruptedError('用户已停止')` |
| 用户 timeout 或宽限未 EOF | `raise subprocess.TimeoutExpired`（尽量带已读 output/stderr） |

`bashTool` / `askSubAgentTool` 的 except 与直通 **不改**。

### 3.2 `_runWithInterrupt` 算法（唯一实现）

```plain
process = Popen(command, cwd=workDir, stdout=PIPE, stderr=PIPE,
                text=True, start_new_session=True)
deadline = monotonic() + timeout
partialOut, partialErr = None, None
try:
    while process.poll() is None:
        if interrupted(): _finishStop(process, 'interrupt', partialOut, partialErr)
        if monotonic() >= deadline: _finishStop(process, 'timeout', partialOut, partialErr)
        sleep(0.1)

    graceDeadline = monotonic() + 0.5
    while True:
        if interrupted(): _finishStop(process, 'interrupt', partialOut, partialErr)
        remaining = graceDeadline - monotonic()
        if remaining <= 0:
            _finishStop(process, 'timeout', partialOut, partialErr)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
            return CompletedProcess(command, process.returncode, stdout, stderr)
        except TimeoutExpired as error:
            partialOut, partialErr = error.stdout, error.stderr
            continue
finally:
    # 异常路径：若仍持有读端则 close（吞异常）。禁止在 close 后 communicate。
```

`interrupted()` = `context.interruptEvent is not None and context.interruptEvent.is_set()`。

### 3.3 `_finishStop(process, reason, partialOut, partialErr)`

```plain
1. _killProcessGroup(process)                 # §3.4，含固定 sleep 0.5 + 无条件 SIGKILL
2. try: out, err = process.communicate(timeout=0.3)
   except TimeoutExpired as error:
        out, err = error.stdout or partialOut, error.stderr or partialErr
        _closeReadPipes(process)              # 放弃读，不再 communicate
        try: process.wait(timeout=0.2)
        except TimeoutExpired: pass
   else:
        # communicate 成功，用 out/err
3. 若 out/err 仍空：回退 partialOut/partialErr 或 ''
4. reason==interrupt → raise modelInterruptedError
   reason==timeout    → raise TimeoutExpired(command, 原timeout, output=out, stderr=err)
```

`_closeReadPipes`：分别 close `process.stdout` / `process.stderr`，吞 `ValueError`。

### 3.4 `_killProcessGroup`

```plain
def _killProcessGroup(process):
    _signalGroup(process, SIGTERM, fallback=process.terminate)
    time.sleep(0.5)                 # 固定等；首领已 reap 也要给子孙处理 TERM 的时间
    _signalGroup(process, SIGKILL, fallback=process.kill)
    try:
        process.wait(timeout=0.5)
    except TimeoutExpired:
        pass

def _signalGroup(process, sig, fallback):
    try:
        os.killpg(process.pid, sig)
    except Exception:
        try:
            fallback()
        except Exception:
            pass
```

Windows 无 `killpg` → except → 单进程。不引入 `psutil`。

### 3.5 为什么这样能放会话锁

```text
runUserMessageStream:          # agent.py ~117
  with getSessionLock(sessionId):
      driveUserMessage → driveToolBatch → executeToolCall → _runWithInterrupt
```

- **超时**：`bashTool`/`askSubAgentTool` 吃 `TimeoutExpired`，写 `isError` toolResult，drive 继续直到本轮结束，离开 `with`。
- **停止**：`modelInterruptedError` 经 `toolRuntime` 直通 → `driveToolBatch` `closeUnfinishedToolCalls` → 生成器结束 → 离开 `with`。

不从 `requestStop` 线程碰锁。`requestStop` 本来就不放锁：摘泵 + `interruptActiveStreams`；`_pump` finally 的 `stream.close()` 在生成器卡 `communicate` 时一起堵。等待有界后 close/`with` 才能走完。

### 3.6 不采用

| 方案 | 原因 |
|------|------|
| 从 `requestStop` `sessionLock.release()` | 非持有线程 `RLock.release` → `RuntimeError`；且与仍在跑的 drive 并发写 jsonl。 |
| 给 `sessionLock` 加 timeout | 破坏整段 drive 持锁。 |
| 工具输出改走文件、不用 PIPE | 过大；关读端已能结束等待。 |
| 包装模型 bash（`disown` 等） | 管不住再 fork。 |

---

## 4. 文件改动

| 文件 | 动作 |
|------|------|
| `flamingoAgents/tools/builtinTools.py` | 重写 `_runWithInterrupt`、`_killProcessGroup`；新增 `_finishStop` / `_closeReadPipes` / `_signalGroup`（文件内私有即可）。文件头 1.7 → 1.8。调用点不改。 |
| `tests/testRunWithInterrupt.py` | **新建** 1.0。T1–T8。 |

不改：`agent.py`、`agentManager.py`、`server.py`、`chatView.js`、`toolRuntime.py`、`config/tools.yaml`。

编码：小驼峰；只碰必须碰的。

---

## 5. 测试设计

文件：`tests/testRunWithInterrupt.py`  
运行：`uv run pytest tests/testRunWithInterrupt.py -q`  
`toolContext.workDir` = `tmp_path`。

生产 bash 仍是 `['bash', '-lc', command]`。单测为隔离 profile，**T1/T3/T5 用** `bash --noprofile --norc -c`。T2/T4/T6/T8 **不经过 bash**，直接：

```text
[sys.executable, '-c', orphanPipeHolderSource]
```

`orphanPipeHolderSource` 必须同时做到：

1. 子进程继承当前 stdout/stderr（Popen 的 PIPE 写端）并 `sleep` 足够久（如 30s），**不要**把 fd 指到文件或 `devnull`；
2. 子进程先在 `workDir/handshake` 写入一个字节再 sleep（T4 用）；
3. 父进程 `os._exit(0)`（不要 `sys.exit`，避免等子进程）。

这样 Popen 首领是短命 Python，`poll()` 很快非 None，管道被 sleep 子进程占着 —— **即使旧实现 timeout=10 也会在 communicate 上挂满约 10s**；修好后 ≤2.5s 返回。若误写成 `sleep 30 &`，GNU bash 可能等 job，旧代码 1s killpg 也会绿，钉不住 190 行。

| 用例 | 断言 |
|------|------|
| `testTimeoutKillsSleep` | T1，≤3.5s，`TimeoutExpired` |
| `testTimeoutWhenLeaderExitsButChildHoldsPipes` | T2，`timeout=10` 却 ≤2.5s；调用前或宽限前可观察到首领已死 |
| `testTimeoutWhenSigtermIgnored` | T3，≤3.5s |
| `testInterruptDuringPipeHang` | T4，握手后 `set`，≤2.5s 且类型必须是 `modelInterruptedError` |
| `testShortCommandSuccess` | T5 |
| `testSessionLockReleasedAfterOrphanPipeTimeout` | T6 |
| `testInterruptEventNoneStillBounded` | T8 |

Windows：T1/T3 若组杀不稳可 skip；**T2/T4/T8「函数必须按时返回」在 Windows 也要跑**（关读端兜底）。

---

## 6. 实施 TODO

1. [x] `_signalGroup` + `_killProcessGroup`：TERM → **固定 sleep 0.5** → 无条件 SIGKILL → `wait(0.5)`。→ 验证：活首领 `trap "" TERM; sleep 30` 时函数返回且进程不在；首领已 reap 的孤儿写端也会被 SIGKILL（T2）。
2. [x] `_closeReadPipes`：close stdout/stderr，吞异常；**没有任何路径 close 后再 communicate**。
3. [x] `_finishStop`：按 §3.3。→ 验证：中断必须 raise `modelInterruptedError`，超时必须 `TimeoutExpired`。
4. [x] `_runWithInterrupt`：删 `subprocess.run` 分支；§3.2 循环 + 0.5s 切片续读。→ 验证：T1/T2/T5/T8。
5. [x] `bashTool`/`askSubAgentTool` 调用点零 diff。文件头 1.7 → 1.8。
6. [x] 新建 `tests/testRunWithInterrupt.py`（含孤儿写端脚本）。→ `uv run python -m pytest tests/testRunWithInterrupt.py -q` 全绿（7 passed）。
7. [x] 抽查：`uv run python -m pytest tests/testLiveUsageUpdate.py tests/testImageInput.py -q`（51 passed）。
8. [x] 走查：中断仍直通，不包装成普通工具失败；§0.2 未破。

---

## 7. 风险与回退

| ID | 风险 | 处置 |
|----|------|------|
| R1 | 杀组/关读端让后台 http.server 等收到 SIGPIPE/SIGKILL | 与「超时本就会杀组」同类。D3 已把 `cmd &` 定为超时失败。 |
| R2 | 0.5s 宽限误杀无孤儿写端的短命令 | EOF 微秒级；T5 挡住。 |
| R3 | SIGKILL 后 0.5s 仍不退（D 状态）留下僵尸 | 优于永卡泵。不引入收割线程。 |
| R4 | 反复 `communicate(timeout)` | CPython 3.12 文档允许续读。算法已写死；**禁止**改回「一次 communicate(0.5) 且不查 interrupt」。 |
| R5 | 关读端后再 communicate → ValueError 把停止打成工具失败 | §3.3 禁止该顺序。 |
| R6 | `killpg` 误伤 | `start_new_session=True` 保证新会话。 |

回退：还原 `builtinTools.py`。不碰协议/索引。

---

## 8. 实施后手测（不替代 pytest）

不停现网已卡死的泵。新会话：

1. `sleep 5 & echo started`：应很快出现 **超时** tool 卡（可能带 `started` 字样），然后能发下一条。不要期望 exitCode=0。
2. `sleep 60` 且 timeout=5：超时 tool 卡，随后能发下一条。
3. 工具执行中点停止：输入框按既有 L1 立刻可用；jsonl 有 cancellation 或超时结果，无「只动画不落盘」。

存量事故会话：仍按事故文 §5 重启 Web。
