# 子代理派发卡住问题调研计划

- 日期：2026-09-23
- 状态：调研完成，报告终审通过
- 类型：故障调研（本轮不修改生产代码）
- 输入证据：用户粘贴的子代理 `cliData` JSONL；当前工作区 FlamingoAgents 源码、测试、历史事故文档；本轮自然产生的同机审核子代理样本
- 目标产物：`docs/260923_subAgentHangResearch.md`
- v1.1 修订说明：首个审核子代理在 240 秒父级期限内被终止，但其子 JSONL 留下完整过程证据。根据该过程已修正 request/attempt 表述，补充“活子进程 PIPE 回压”、无限 model step、父级诊断丢失及 A/B 对照；随后交由全新子代理正式复审。

## 1. 目标与成功标准

目标是解释“派发子代理后卡住”发生在哪一层、为什么父代理表现为长时间无返回，并把已证实事实与推断严格分开。

成功标准：

1. 还原用户 JSONL 的逻辑 step、物理 attempt、响应和工具调用时间线，按 `usageKey + attempt + messageCount` 配对，精确指出最后一个已完成事件和第一个未闭合 attempt。
2. 每个结论至少附一类直接证据：用户日志原文、当前源码位置、现有测试/历史事故记录，或本机可重复命令与实测输出。
3. 至少区分：子进程创建、子代理模型 HTTP/SSE、子代理工具执行、父进程等待/PIPE、会话锁、账本持久化。
4. 明确标注：
   - **已证实**：现有证据可唯一推出；
   - **高概率**：证据吻合，但缺另一台机器的进程/网络终态证据；
   - **待补证**：存在竞争解释；
   - **已排除（限定范围）**：证据仅能排除明确分支，不扩大解释。
5. 给出按优先级排序、可直接执行的补证命令和修复建议；不得把可能性写成确定根因。
6. 计划及最终报告均经全新子代理审核；修订后再由另一全新子代理复审，直至无明显问题。

## 2. 当前假设与限制

### 2.1 明确假设

- 当前仓库是用户所述 CLI/子代理实现的同源或近似版本。
- 用户粘贴的 JSONL 在 `2026-09-22T10:02:05.781023+00:00` 后是否还有事件未知；报告必须把“粘贴片段末尾”与“源文件真实 EOF”分开。
- 另一台机器实际 commit、Python 版本、`models.yaml`、网络代理和父会话 JSONL 尚未知，不能默认与本机一致。

### 2.2 必须避免的误判

- 尾部为 `modelRequestStart` 只能定位到“start 已落盘，尚无后继终态”，不能单独证明 GLM 服务端故障，也不能区分 connect / firstByte / streamRead。
- 父代理“卡住”不等于进程死锁；`askSubAgent` 默认可同步等待 600 秒。
- `_runWithInterrupt` 在 `process.poll() is None` 阶段只轮询/休眠、不读取 stdout/stderr。这是源码事实，且本轮读取大日志已触发过 >64 KiB 输出导致的 30 秒 timeout；但用户样本前三轮实际可见输出很小，不能把通用 PIPE 缺陷直接定为该样本根因。
- 历史“首领退出、孤儿仍占 PIPE”路径已修复，现有 7 项测试通过；这不覆盖“首领仍活、写满 PIPE”。
- `urlopen(timeout=300)` 是 socket 阻塞 I/O timeout，不是严格的整请求总 deadline；有周期性字节/心跳时可持续更久。
- 同机审核子代理也停在 `attempt=1 modelRequestStart` 并被父级 240 秒杀掉，只证明症状可复现；由于其前序 reasoning 很多、可能逼近 PIPE 容量，不能用它单独证明 provider 是根因。

## 3. 待检验假设

| ID | 假设 | 可观察预测 | 证据方法 |
|---|---|---|---|
| H1 | 用户样本停在第 4 个逻辑 step、`attempt=1` 的 GLM `completeStream` 窗口 | 前 3 个 start 均有同 key usage+assistant+tool results；最后 start 无同 key 后继 | 按 `usageKey/attempt/messageCount` 配对；核对 `agent.py` 写入顺序 |
| H2 | 父进程同步等待 child，默认 600 秒且不转发进度，造成“假死” | `driveToolBatch` 同步执行；父日志在 tool start 与 tool result 间无心跳 | 调用链、父 JSONL、本次 240 秒实测 |
| H3 | adapter 单次阻塞 I/O 可静默 300 秒，agent 最多 4 attempts；父总 deadline 可能先杀 child | `urlopen(timeout=300)`、retry 常量；240 秒样本未等到首 attempt 的 modelError | 源码时间预算上下界；禁止把 300 秒写成总请求期限 |
| H4 | 父进程在 child 活着时不排空 stdout/stderr，复杂子代理可能 PIPE 回压阻塞 | PIPE=65,536B；>64KiB producer 走当前实现 timeout，文件重定向/持续排空则快速完成 | 源码 + 最小确定性 A/B |
| H5 | 大量整文件读取与 prompt 膨胀增加延迟/失败概率，但不是卡死充分条件 | 用户 prompt 1,886→19,586→31,747；最后一轮还加入多个整文件结果 | 用户日志 token 和结果字节统计；只判放大因素 |
| H6 | 卡在 read/bash 工具、usage 持久化或 assistant 持久化 | 卡工具应有 assistant tool call 缺 result；usage 后持久化卡应有 usage 缺 assistant；片段尾部均不符合 | 事件状态机限定排除，不排除 model call 内底层锁 |
| H7 | `sdkEntry` 把 `maxModelSteps=-1`，复杂审核可无限查阅，缺 step/time/context 预算 | 同机审核 114 秒内完成 10 个 model steps 仍未最终回复；仅父 timeout 收口 | `sdkEntry.py` + 同机审核 JSONL |
| H8 | GLM/provider 或本机到 GLM 的链路有长静默 | 用户样本与同机独立样本都出现 start 后长时间无终态 | 两份 JSONL；需无 PIPE 干扰 A/B、diag/socket 现场才能再细分 |

## 4. 调研步骤与 TODO Lists

### A. 证据保全与时间线

- [x] A1. 已按 `usageKey + attempt + messageCount` 建立用户样本逻辑 step / 物理 attempt 表。
- [x] A2. 已计算完成 attempt 时延；确认四条 start 全是 `attempt=1`，属于四个逻辑 step。
- [x] A3. 已统计 token、工具结果、reasoning/text/chunk，并计算前三轮标准流上界；已核对两根独立 PIPE。
- [x] A4. 已明确粘贴片段不等于源 EOF，并在报告 §8 列出另一机器必须补取的证据；现场数据缺失被保留为结论边界。
- [x] A5. 已纳入同机复现并计算父总期限和最后 attempt 年龄；报告按父 deadline / socket 窗口区分。

### B. 源码调用链核对

- [x] B1. 已核对完整调用链。
- [x] B2. 已记录父 timeout、同步等待和 child 存活阶段不读 PIPE 的控制流。
- [x] B3. 已记录 socket I/O timeout、retry/退避和各 JSONL 落点，并说明预算不一致。
- [x] B4. 已核对 stderr 流式输出与最终 stdout JSON，量化前三轮上界。
- [x] B5. 已核对 session 锁范围，并用受控锁实验验证等待效应；未声称锁循环。
- [x] B6. 已用历史文档与 7 项回归区分旧孤儿 PIPE 路径和活 child 回压路径。
- [x] B7. 已核对无限 model steps 和超时诊断丢失。

### C. 可控实验

- [x] C1. `tests/testRunWithInterrupt.py`：7 passed，只作为旧路径回归证据。
- [x] C2. 本机 PIPE=65,536B；小输出对照已完成。
- [x] C3. 已做确定性边界实验：`_runWithInterrupt(timeout=1)` 下 stderr=60,000B 时 0.101s 完成；70,000B/200,000B 均 1.502s TimeoutExpired，捕获字节均恰为 65,536B。
- [x] C4. 已做文件 A/B：相同 200,000B producer 改写普通文件后 0.016s 完成；另经真实 `askSubAgentTool→fakeSdk` 验证 60,000B 成功、70,000B 在 1.502s 返回通用超时且丢弃 partial stderr。后续报告补 stdout/stderr 并发写的次要边界。
- [x] C5. 已通过 adapter 源码和现有 firstByte/streamRead timeout mock 测试验证阶段与诊断落点；未再做真实 provider 压测。
- [x] C6. `tests/testModelStreamDiag.py`：20 passed。
- [x] C7. 已完成 fakeSdk 边界、partial stderr 丢失、session 锁等待、文件重定向及持续 `communicate()` 对照。
- [x] C8. 未再执行不受控真实子代理压测；审核所自然产生的短预算样本仅作观察证据。

### D. 根因分级与建议

- [x] D1. 已精确表述用户样本断点与可判定边界。
- [x] D2. 已完成 H1–H8 分级，并预注册 PIPE 确认/否决签名。
- [x] D3. 已解释同步等待、父期限、socket 窗口与无 heartbeat 的用户体感。
- [x] D4. 已给最小补证包和配对消歧规则。
- [x] D5. 已给出分阶段建议：
  1. 父级转发 child 进度并保留 child session/log path、partial stderr、最后 stage；
  2. 统一父总 deadline、单 attempt deadline、model-step/context 预算；
  3. 持续排空 PIPE或改文件/结构化 IPC；
  4. 限制整文件读取和上下文增长，明确作用链是降低模型延迟/输出量与触发 PIPE 的概率，而非替代 PIPE 根修。
  每项附可判定验收标准。

### E. 文档与审核

- [x] E1. 已生成 `docs/260923_subAgentHangResearch.md`；问题均包含直接证据、推导、置信边界和建议。
- [x] E2. v1.0 审核尝试已执行：子代理未在 240 秒内产出最终文本；保全过程证据，未把其视作通过。
- [x] E3. v1.1 修订后由全新 `volcano/glm-5.3` 复审：无阻断，提出真实样本双 PIPE 字节、时长分箱、mock 全链路三项重要补强；已全部并入 A/C/D。
- [x] E3.1. 新审指出“真实样本 PIPE 字节未知”和“持续排空对照待完成”；前者被明确保留为不可伪造的证据缺口，不再作为计划缺陷，后者已有普通文件 A/B 与 fakeSdk 边界实测，报告中不得越级宣称用户事件已证实由 PIPE 导致。
- [x] E3.2. 全新 `volcano/glm-5.3` 终审：无阻断/重要，可进入调研；建议预注册 PIPE 可证伪签名、可选 fd/syscall trace，已纳入 D2/补证建议。
- [x] E4. 报告完成后由全新 `volcano/glm-5.3` 做证据审计：无阻断；两项重要意见为补齐前三轮 stdout/stderr 上界和 session 锁修复建议，已修订。
- [x] E5. 全新 `DS_Offical/deepseek-flash` 终审修订：阻断/重要/次要均无，结论“可交付”。

## 5. 预期报告结构

1. 执行摘要：能确定什么、不能确定什么。
2. 用户日志逐事件时间线与状态机。
3. 同机复现及其与用户样本的差异。
4. 调用链、双层 timeout、无限 step 与 PIPE。
5. H1–H8 判定矩阵。
6. 已证实产品问题：同步无心跳、超时诊断丢失、活 child PIPE 回压、无限 model step；上下文仅列放大因素。
7. 本次最可能解释和竞争解释。
8. 立即补证命令。
9. 修复建议与验收标准。
10. 附录：源码行、实验命令/输出、版本限制。

## 6. 不在本轮范围

- 不修改生产源码、provider 配置或另一台机器文件。
- 不再用真实密钥做压力/长超时测试；本次计划审核自然产生的 240 秒失败样本仅作证据。
- 不承诺仅凭截断 JSONL 唯一识别远端服务、网络、socket reader 或 PIPE 中的具体根因。
