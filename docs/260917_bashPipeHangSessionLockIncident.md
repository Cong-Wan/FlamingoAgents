# bash 管道死等导致会话锁不释放、新消息无法落盘

- Author: wilbur
- Version: 1.0
- Date: 2026-09-17
- Incident session: `260917105350-c77fffe2`
- WorkDir: `/home/wanshicong/aigc`
- jsonl: `~/.flamingo/logs/webData/~-aigc/260917105350-c77fffe2.jsonl`
- Severity: **High / P1（单会话前端可发送、后端不落盘，停止/刷新无法自愈）**
- Scope: **仅溯源落档；本文不改源码。** 相关症状里 `/api/chat/attach` 404 是无活跃泵的业务返回，不是本事故根因，文中只作对照。

---

## 0. 一句话

上一轮 `bash` 的子孙进程仍占着 `Popen` 的 stdout/stderr 管道 → 泵线程堵在无超时的 `communicate()` → `sessionLock` 不放 → 后续 `/chat/stream` 虽 200，但走不到 `appendUserMessage`，jsonl 零新增。

`timeout=20` 救不了：超时只覆盖 `poll() is None` 的循环；一旦认为子进程已退出，后续 `communicate()` 没有超时。

---

## 1. 现象（用户侧）

| 观察 | 含义 |
|------|------|
| 前端发出去后等待动画正常 | 乐观 UI：本地先插用户气泡、置 `streaming`、开 SSE |
| 对应 jsonl 完全没有新 `userMessage` | 后端还没执行 `conversation.appendUserMessage` |
| Network：`POST /api/chat/stream` 200 | SSE 连接已建立（泵已 `startStream` 登记） |
| 随后 `POST /api/chat/stop` 200 | 停止只注销**当前**泵 |
| 再进会话 `POST /api/chat/attach` 404、`content-length: 36` | 无活跃泵时的业务 404：`{"error":"该会话无活跃流。"}`，前端静默回历史态 |

这不是「消息没发出去」，是 **请求已进后端，卡在锁后面，没机会落盘**。

---

## 2. 真正发不出去：上一轮工具没结束，锁没放

### 2.1 jsonl 停在未闭合的 bash

jsonl 最后写入时间：**2026-09-17 14:14:07 +08**（UTC `2026-09-17T06:14:07.194384+00:00`）。

最后一条是 `assistantMessage`（空 `content`），带 **尚未配对 `toolResult` 的 bash**：

- call id: `call-22527dee-15fd-4e85-8d08-28f532d65684-49`
- `timeout`: 20
- 命令要点：
  - 若 8765 未监听，则 `python3 -m http.server 8765 ... &`
  - `nohup uv run python main.py ... --port 8071 > /tmp/aigc_c_test/backend.log 2>&1 &`
  - `sleep 2; head -20 /tmp/aigc_c_test/backend.log`

`bash` 不在删除类权限规则里，此命令 **不走确认框**，泵会直接 `executeToolCall`。因此不是 `waitingConfirm` / `pendingConfirmationExists`（当时 `GET .../pending` 为 `{"pending":null}`）。

### 2.2 进程证据（调查时刻约 14:31–14:37）

残留 bash 与 jsonl 中的命令 **逐字相同**：

| 字段 | 值 |
|------|-----|
| PID | 156500 |
| PPID | 1（会话首领已死后过继） |
| PGID / SID | 156477 |
| STAT / 已运行 | `S`，20+ 分钟（远超 `timeout=20`） |
| cmdline | `bash -lc` + 上述启动 8765/8071 脚本 |
| fd 1 / fd 2 | `pipe:[642551350]` / `pipe:[642551351]` |

同管道读端在 Web 进程（当时观测到的 `python -m webApp`）上仍打开。泵线程 `Thread-1 (_pump` 系统调用为 `poll`，`nfds=2`，形态就是 `Popen.communicate()` 在等 stdout+stderr EOF。

该 bash 仍拉着前台/同组子孙（调查时可见 `python3 -m http.server 8765` 的 PPID=156500）。只要这些进程不关写端，管道就不会 EOF。

### 2.3 后端锁与落盘时序

用户消息 **不是** 在 `POST /chat/stream` 返回 200 时写盘。生成器惰性，注释已写明：`appendUserMessage` 发生在泵线程 **首次迭代**。

关键路径：

```text
chatStreamSync
  → runUserMessageStream(...)          # 尚未持锁、尚未写盘
  → startStream(...)                   # 登记泵、开 SSE → HTTP 200
  → 泵线程 _pump: for event in stream
       runUserMessageStream
         with getSessionLock(sessionId):   # ← 旧 bash 泵若仍在锁内，这里永久阻塞
           driveUserMessage
             appendUserMessage             # ← 只有这里才写 jsonl
```

`runUserMessageStream` 在 **整段** `driveUserMessage`（含工具执行）期间持有 `sessionLock`。旧泵堵在 `executeToolCall` → `_runWithInterrupt` → `communicate()` 时，锁不放。

于是新发送：

1. 前端 `send()`：`appStore.stream` 为空则放行；本地 `appendUserMessage` + 等待动画。
2. `startStream` 成功（`activeStreams` 里旧泵若已被 `requestStop` 摘掉，新泵可以登记）→ SSE 200。
3. 新泵第一次 `next(stream)` 在 `getSessionLock` 上阻塞。
4. jsonl 无新行。

### 2.4 停止 / 刷新为什么不能自愈

`requestStop`（`stopResponsivenessPlan` L2）主动收尾：

1. 置 `stopFlag`、`interruptActiveStreams`
2. `_recordUsage`
3. **`unregisterStream`（从 `activeStreams` 删除）**
4. 广播 `stopped`、关订阅、置 `doneEvent`

它 **不要求** 旧泵线程已经走出 `communicate()`，也 **不释放** 仍被旧生成器 `with getSessionLock` 握着的锁。

因此：

- 点停止 → 新泵（以及被 stop 命中的那只登记泵）从 `activeStreams` 消失 → 再 `attach` 就是业务 404。
- 旧 bash / 旧泵线程若仍堵在管道上 → 锁还在。
- 再发送 → 又一只新泵 200 然后堵在锁上；调查时可见多只 `_pump` 线程（有的 `futex` 等锁，有的 `poll` 等管道）。

`attach` 404 在本事故里是 **停泵之后的伴随症状**，不是发送失败的原因。前端对 attach 404 的处理是静默 `resetToHistoryState`，composer 会重新可点，所以用户能反复「发送成功、动画转、jsonl 不写」。

---

## 3. 为什么 `timeout=20` 救不了

### 3.1 实现（`flamingoAgents/tools/builtinTools.py` `_runWithInterrupt`）

Web 路径总会注入 `interruptEvent`，因此 **不走** `subprocess.run(..., timeout=timeout)`，走 `Popen` + 轮询：

```python
process = subprocess.Popen(
    command,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    start_new_session=True,
)
deadline = time.monotonic() + timeout
while process.poll() is None:
    if context.interruptEvent.is_set():
        _killProcessGroup(process)
        raise modelInterruptedError('用户已停止')
    if time.monotonic() > deadline:
        _killProcessGroup(process)
        stdout, stderr = process.communicate()   # 超时分支：仍可能堵
        raise subprocess.TimeoutExpired(...)
    time.sleep(0.1)
stdout, stderr = process.communicate()           # poll 已返回：完全无超时
```

超时 **只** 在 `poll() is None`（Popen 那个 pid 还活着）时生效。

### 3.2 死等条件

`start_new_session=True` 后，Popen pid 是会话首领（本例 PGID `156477`）。脚本里的 `&` / `nohup` 会再拉出 **不随首领退出而关管道** 的子孙：

- 若首领 bash 已退出（`poll()` 得到返回码），循环结束，进入 **无超时** 的 `communicate()`。
- `communicate()` 等到的是 **管道 EOF**，不是「首领进程已死」。
- 任何仍继承 stdout/stderr 写端的子孙（本例：PID 156500 的 bash，fd1/fd2 仍是那对 pipe；以及它拉着的 `http.server`）都会让 `poll(nfds=2)` 永远等。

本事故现场符合这条路径：首领 PGID `156477` 已不在，工具 bash `156500` PPID=1，管道两端仍分别在 bash 与 Web 泵线程上。

超时分支同样调用 `communicate()`，且 `_killProcessGroup` 末尾的 `process.wait()` 在 SIGKILL 之后也 **没有超时**。即便 20s 到了去杀首领，杀不掉的子孙 + 无界 `wait`/`communicate` 仍可把泵钉死。

中断路径更弱：`interruptEvent` 只在 `while process.poll() is None` 里检查。已经进入 `communicate()` 之后，再点停止 **叫不醒** 这次 `communicate()`。

### 3.3 与「timeout 语义与 subprocess.run 一致」的差距

注释写的是超时语义对齐 `subprocess.run`。`subprocess.run(..., timeout=T, capture_output=True)` 会在 T 秒后杀进程并结束等待。当前实现：

- 对齐的是「首领 pid 还活着」的那一段；
- **不对齐**「首领已死、管道仍被孙子占着」；
- 也不保证 `killpg` 能清掉已经改组/过继的子孙。

所以工具参数 `timeout: 20` 对这次命令 **没有上限**。

---

## 4. 因果链（对照）

```text
14:14:07  assistantMessage + bash toolCall 落盘（尚无 toolResult）
          泵持 sessionLock，进入 _runWithInterrupt
          后台 http.server / uv run 拉起；管道写端留在子孙 bash 上
          Popen 首领退出或被视为退出 → communicate() 无限等 EOF
                 │
                 ├─ jsonl 停更（toolResult 未写）
                 ├─ sessionLock 不放
                 └─ 泵线程 poll 两根 PIPE
                          │
用户再发送 ─► startStream 200 + 前端乐观气泡
             新泵 next() 堵在 getSessionLock
             appendUserMessage 不执行 → jsonl 仍无 userMessage
                          │
用户点停止 ─► unregisterStream（attach 再来就是 404）
             communicate() 与 sessionLock 仍在
                          │
刷新会话 ─► attach 404（无活跃泵）+ 历史渲染
             composer 空闲，可再次发送，循环上述
```

---

## 5. 临时恢复（不改代码）

本会话在泵线程与残留 bash 退出前 **无法** 再落盘新消息。

1. 停掉卡住的 `python -m webApp`（会拆掉管道读端；注意其它会话的内存态也会丢）。
2. 清残留：至少 PID 156500 及其子进程（8765 `http.server`、8071 后端若不再需要一并停）。
3. 重启 Web。再进该会话时，`driveUserMessage` 的 `findUnclosedTailCallIndex` 应对这条无 `toolResult` 的 bash 做 `preflightRepair`（写取消结果，不重跑命令）。

未重启前不要指望停止按钮或刷新能恢复发送。

---

## 6. 修复方向（仅记录，本文不实施）

若后续立项，最小闭环应同时覆盖：

1. `_runWithInterrupt`：`communicate()` / 最终 `wait()` 必须受同一 `deadline` 约束；超时后关 PIPE、杀进程组、放弃读。
2. 后台任务：避免子孙继承 `Popen` 的 stdout/stderr（`close_fds` / 显式把后台 fd 指到文件或 `devnull`）。
3. `requestStop`：中断后若泵线程仍不离开工具执行，应有办法释放或降级 `sessionLock`，避免「泵表已空、锁仍被幽灵生成器占用」。
4. 可选诊断：工具超时/中断/管道死等落一条 jsonl（现状是 assistant toolCall 之后完全静默，现场只能靠 OS 进程表对命令）。

---

## 7. 明确非根因

| 项 | 说明 |
|----|------|
| `POST /api/chat/attach` 404 | 无活跃泵的约定返回；打开空闲会话就会打。本事故里它出现在 stop 之后，是伴随现象。 |
| `GET /static/vendor/chart.umd.min.js.map` 404 | sourcemap，无关。 |
| 待确认框 / `pending` | 该 bash 不需审批；调查时 `pending=null`。 |
| 前端没调 `/chat/stream` | 日志与 Network 均为 200；失败点在 200 之后的锁。 |
