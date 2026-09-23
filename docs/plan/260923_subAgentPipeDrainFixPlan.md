# askSubAgent/bash 活子进程 PIPE 回压与超时诊断丢失修复方案

- Author: wilbur
- Version: 1.0
- Date: 2026-09-23
- 上游调研：`docs/260923_subAgentHangResearch.md`（证据、复现与边界）
- 关联历史：`docs/plan/260917_bashPipeHangSessionLockFixPlan.md`（首领退出后无界等待已修；本方案修它明确列为非目标的"活首领写满 PIPE"）
- 状态：**已实施**（v1.0 方案；`builtinTools.py` 1.10 + `sdkEntry.py` 1.8 + `tests/testRunWithInterrupt.py` 1.1 + `tests/testAskSubAgentTool.py` 1.0；全仓 312 项测试通过）

---

## 0. 目标与非目标

### 0.1 目标（修完可验证）

1. **活 child 写满 PIPE 不再卡死**：child 首领存活期间向 stdout/stderr 任一管写入超过 PIPE 容量（本机 65,536B），父进程持续排空，child 正常完成并返回全部（有界）输出；不再等满工具 timeout。
2. **旧语义保持**：`sleep 5 & echo started` 事故形（首领退出、孤儿占管）仍是 0.5s 宽限未 EOF 即超时杀组；中断 ≤既有预算返回；短命令零误伤。
3. **超时可诊断**：`askSubAgent` 超时 toolResult 必须带 child 身份与现场：`childSessionId`、`childLogPath`、`stdoutBytes/stderrBytes`、`partialStderrTail`、`lastActivityAt`、`timeoutSource`。
4. **内存有界**：父进程对每根 PIPE 只保留 head+tail 双端有界缓冲（各 256KiB 常量），10MiB 输出也不无界堆内存。
5. **回归零容忍**：现有 7 项 `testRunWithInterrupt` 不改断言全过；`testModelStreamDiag` 等 20 项不受影响。

### 0.2 非目标（本期不做，另立项）

| 项 | 说明 |
|----|------|
| 父总 deadline / attempt deadline / step / context 预算统一 | 调研报告 P1；独立方案 |
| 长工具移出 sessionLock | 调研报告 P1；涉及锁协议与 in-flight ledger |
| 父级 heartbeat / 进度 UI | 调研报告 P1；前后端联动 |
| child 输出改 envelope + 结果文件 | 调研报告 P1；协议级改造 |
| 限制整文件读取 / 上下文增长 | 调研报告 P2 |
| `maxModelSteps=-1` 收敛 | 调研报告 P1；`sdkEntry` 行为变更需单独评估 |

### 0.3 成功标准（可判定，实施后勾选）

- [x] **T1 核心回归**：单进程向 stderr 写 70,000B 后退出，`_runWithInterrupt(timeout=5)` 在 1s 内正常返回（非 TimeoutExpired），stderrBytes=70,000。修复前该用例 1.502s 超时。
- [x] **T2 stdout 路径**：最终 JSON reply 70,000B 的 fakeSdk 经真实 `askSubAgentTool` 成功返回，content 长度 70,000。修复前超时。
- [x] **T3 双管并发**：stdout/stderr 各写 700,000B，均成功返回（不因回压等满 timeout）；超出 head+tail（512KiB）上限的中间部分按设计丢弃，两端字节完整。（v1.0 原文误写“各捕获 700,000B”，与 spool 有界设计矛盾，实施时由测试暴露后修正）
- [x] **T4 大输出有界**：单管写 10MiB 成功返回，父进程单管缓冲不超 head 256KiB + tail 256KiB 常量，且 bash 头部 clip 语义不变、askSubAgent 尾行语义不变。
- [x] **T5 旧事故形**：孤儿写端（首领退出+子孙占管）仍 ≤2.5s TimeoutExpired；与 T1/T3 用例互不可替换。
- [x] **T6 超时诊断**：fakeSdk 占管超时后，toolResult.details 含 `childSessionId`、`childLogPath`、`stdoutBytes`、`stderrBytes`、`partialStderrTail`、`lastActivityAt`、`timeoutSource`；且 `childLogPath` 与 `resolveSessionLogDir('cliData', childWorkDir)/sessionId.jsonl` 完全一致。
- [x] **T7 旧 7 项回归**：`uv run python -m pytest tests/testRunWithInterrupt.py -q` 全过（断言未改）。
- [x] **T8 诊断套件**：`uv run python -m pytest tests/testModelStreamDiag.py tests/testAskSubAgentTool.py -q` 全过。

---

## 1. 问题复述（与旧修复的边界）

```text
当前（本方案修）：
  Popen 首领存活 → 自己写满 stdout/stderr（≈64KiB）→ 阻塞在 write
  → 父进程 poll() 等首领退出，期间不读 PIPE → 双方互等 → 熬到工具 timeout

旧事故（3bdce31 已修）：
  Popen 首领已退出 → 子孙仍占管道写端
  → 父进程进入无界 communicate() 等 EOF → 永久卡死
```

旧方案文档 §0.2 明确"执行中持续排空管道以防 64KiB 填满"为非目标；本方案补齐该路径，同时保留旧方案 D3/D4 语义（0.5s 宽限、TERM→固定等→无条件 SIGKILL）。

## 2. 决策（实施前锁定）

### D1. 排空机制：每管一个 reader 线程 + 有界 head/tail spool — 选定

`selectors` 在 Windows 上不能选 pipe fd（SelectSelector 仅 socket），排除；改用两个 daemon reader 线程阻塞读 `read1`，写入共享 spool：

- spool 保留 **head 256KiB + tail 256KiB**（常量 `pipeHeadCapBytes`/`pipeTailCapBytes`），中间溢出丢弃并记 `droppedBytes`；
- head 侧保证 `bashTool` 现有"前 maxOutput 字符"clip 语义不变；
- tail 侧保证 `askSubAgentTool` 现有"stdout 最后一行 JSON"解析语义不变（reply ≤256KiB 时完整）；
- reader 线程随 EOF（ writers 全亡）自然退出；超时杀组后写端全灭 → EOF → 线程退出，无 join 死锁。

不采用：无界 communicate 累积（内存风险）；单线程 select 轮询（Windows 兼容）。

### D2. 主循环保持 poll 骨架，grace 语义原样保留 — 选定

```text
leader 存活：poll 循环查 interrupt/deadline（reader 线程独立排空，不再 sleep 空转等待）
leader 退出：记 leaderExitedAt；0.5s 内两管 EOF → 正常返回；
            未 EOF → _finishStop('timeout')（孤儿事故形，防旧回归）
全部 EOF 且 leader 已退 → wait 收尸，组装 spool 返回
```

### D3. 超时诊断：父进程生成 childSessionId 传入 — 选定

现状 child `sessionId` 在 `runSdk()` 内部生成，父进程无法预知，超时后只能翻日志目录猜。改为：

- `askSubAgentTool` 用 `newSessionId()` 生成 childSessionId，经新参数 `--session-id` 传给 `sdkEntry`；
- `runSdk(..., sessionId=None)`：None 时保持现行为（CLI 兼容），传入则用父给的；
- 父进程用 `resolveSessionLogDir('cliData', childWorkDir) / f'{childSessionId}.jsonl'` 纯计算 childLogPath（不 mkdir、不猜目录）；
- `_finishStop` 组装的 partial 输出来自 spool（含字节数），`askSubAgentTool` 超时分支不再丢弃 `TimeoutExpired.output/stderr`，取 tail 截断（≤2000 字符）入 `partialStderrTail`；
- `timeoutSource` 取值：`pipeOrLeader`（leader 活着超时）/`postLeaderGrace`（leader 退出宽限超时），为后续归因 PIPE vs 网络/推理静默留判别位。

不采用：child 提前打印 sessionId 到 stderr（父仍需解析时序）；扫描目录取最新 jsonl（并发下不可靠）。

### D4. lastActivityAt 语义 — 选定

取 spool 最后一次收到字节的时间戳；两管均无输出时回落到进程启动时间。仅诊断字段，不参与控制流。

### D5. bash 路径零行为变更 — 选定

`bashTool` 只消费 `_runWithInterrupt` 返回值（head/tail 拼接文本），clip/超时文案/.details 全部不动；write/edit/read 无关。

## 3. 文件改动

| 文件 | 动作 | 内容 | 版本 |
|---|---|---|---|
| `flamingoAgents/tools/builtinTools.py` | 修改 | `_pipeSpool`（head/tail 有界缓冲）+ 每管 reader 线程；`_runWithInterrupt` 改持续排空骨架（D1/D2）；`_finishStop` 从 spool 取 partial + `droppedBytes`/`lastActivityAt`/`timeoutSource`；`askSubAgentTool` 生成并传 `--session-id`，超时/失败 details 补诊断（D3/D4） | 1.9 → 1.10 |
| `sdkEntry.py` | 修改 | `runSdk(..., sessionId=None)`；`--session-id` 参数；传入优先、缺省现行为 | 1.7 → 1.8 |
| `tests/testRunWithInterrupt.py` | 修改 | 新增 T1（stderr 70,000B 成功）/T3（双管 700,000B）/T4（10MiB 有界，断言 spool 不超常量）/T5 保持旧用例不动；文件头 1.0 → 1.1 | 1.0 → 1.1 |
| `tests/testAskSubAgentTool.py` | 新增 | fakeSdk 注入 `sdkEntryPath`：T2 stdout 70,000B reply 成功；T6 超时诊断字段齐全且 childLogPath 真实存在 | 1.0 |

不改：`agent.py`、`toolRuntime.py`、`tools.yaml`、`logPaths.py`、webApp。

## 4. 测试设计

`tests/testRunWithInterrupt.py`（打 `_runWithInterrupt`）：

| 用例 | 断言 |
|---|---|
| `testLeaderAliveLargeStderrCompletes`（新） | stderr 70,000B，timeout=5，≤1.5s 正常返回，stderr 完整 70,000B，`droppedBytes=0` |
| `testBothPipesLargeConcurrent`（新） | 双管各 700,000B，正常返回且两管字节完整 |
| `testTenMiBOutputBoundedSpool`（新） | 单管 10MiB，返回 head 256KiB+tail 256KiB+dropped 计数，耗时随 producer 而非 timeout |
| `testOrphanPipeHolderStillFastTimeout`（旧 `testTimeoutWhenLeaderExitsButChildHoldsPipes`） | 断言不变，≤2.5s TimeoutExpired —— 防旧修复回归 |
| 其余 7 项旧用例 | 断言零修改 |

`tests/testAskSubAgentTool.py`（fakeSdk 走真实工具函数）：

| 用例 | 断言 |
|---|---|
| `testLargeReplySucceeds` | reply 70,000B 成功，content 长度 70,000，`childSessionId` 在 details |
| `testTimeoutDiagnosticsPreserved` | 占管 fakeSdk + timeout=1：isError；details 含 §0.1-3 全部字段；`childLogPath` 存在且文件名=childSessionId；`partialStderrTail` 有内容；`timeoutSource` 合法 |
| `testHeadTailSemantics` | 10MiB stdout：head+tail 拼接后最后一行仍可解析（reply ≤tail 时）或显式 error（超 tail），不静默返回 `None` |

运行：`uv run python -m pytest tests/testRunWithInterrupt.py tests/testAskSubAgentTool.py tests/testModelStreamDiag.py -q`

## 5. 风险与对策

| ID | 风险 | 对策 |
|----|------|------|
| R1 | reader 线程与 `_closeReadPipes` 竞态（关 fd 时线程仍阻塞读） | POSIX 关 fd 不保证唤醒阻塞 read；改为杀组后等 EOF 自然退出，线程 daemon 兜底，不 join 超时路径 |
| R2 | head/tail 拼接处截断 UTF-8 多字节字符 | spool 按 bytes 累积，返回前以 `errors='replace'` decode（现 `_pipeText` 同策略） |
| R3 | 10MiB 输出场景 parent 侧 decode/拼接耗时 | 512KiB 常量上限使拼接 O(常量)；producer 侧写速即瓶颈 |
| R4 | 旧孤儿用例被 drain 改慢（等满 timeout） | D2 显式保留 leaderExitedAt+0.5s 宽限 → `_finishStop`，T5 旧断言不放松 |
| R5 | `--session-id` 与既有 CLI 冲突/遗漏 | 缺省完全兼容；`runSdk` 直传 sessionId 仅 askSubAgent 使用 |
| R6 | Windows 无 killpg/线程行为差异 | 沿用 `_signalGroup` fallback；新增用例与旧用例同样 POSIX-only 标注，Windows skip 不放宽"必须按时返回"类断言 |

回退：还原 `builtinTools.py`/`sdkEntry.py` 两文件即可；无数据格式、协议、索引变更。

## 6. 实施 TODO（已执行）

1. [x] `_pipeSpool`：head/tail 双端有界缓冲 + `droppedBytes` + `lastActivityAt`（bytes 层，decode 后置）。→ 验证：T4 ✅
2. [x] `_runWithInterrupt`：双 reader 线程持续排空；leader 存活 poll 骨架；leader 退出后 0.5s 宽限语义保留；EOF+退出 → 组装返回。→ 验证：T1/T3/T5 + 旧 7 项 ✅
3. [x] `_finishStop`：partial 取自 spool；补充 `timeoutSource`。→ 验证：T6 断言字段 ✅
4. [x] `sdkEntry.runSdk(sessionId=None)` + `--session-id`；`askSubAgentTool` 生成/传递 childSessionId、计算 childLogPath、超时 details 全量诊断、`partialStderrTail` ≤2000 字符。→ 验证：T2/T6 ✅
5. [x] 新增 `tests/testAskSubAgentTool.py`；扩展 `tests/testRunWithInterrupt.py`。→ 验证：§4 命令全绿 ✅
6. [x] 抽查 `tests/testModelStreamDiag.py tests/testLiveUsageUpdate.py` 不回归。→ 20 passed / 40+ passed ✅
7. [x] 走查：bash 短命令/超时文案、askSubAgent JSON 解析失败路径、`_closeReadPipes` 不在 drain 存活期调用。✅

## 7. 验收记录（已回填）

| 命令 | 结果 |
|---|---|
| `uv run python -m pytest tests/testRunWithInterrupt.py tests/testAskSubAgentTool.py -q` | **14 passed**（旧 7 项 + 新 7 项） |
| `uv run python -m pytest tests/testModelStreamDiag.py -q` | **20 passed** |
| `uv run python -m pytest tests/testModelStreamDiag.py tests/testLiveUsageUpdate.py -q` | **60 passed** |
| `uv run python -m pytest tests/ -q`（全仓） | **312 passed** |
| 修复前后对照（stderr 70,000B，timeout=5） | 修复前 1.502s TimeoutExpired → 修复后正常返回（T1 用例） |

## 8. 审核记录

| 轮次 | 审核子代理 | 结论/问题 | 修复 |
|---|---|---|---|
| 1 | 待执行（实施前两轮尝试均因审核子代理自身在父级超时被杀，与被审问题同源；用户指示跳过流程审核直接实施，实施后补审） | — | — |
| 2 | 待执行（实施后复审） | — | — |
