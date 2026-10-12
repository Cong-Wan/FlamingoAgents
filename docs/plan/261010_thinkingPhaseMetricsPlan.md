# 思考耗时拆分与指标口径统一方案

- Author: wilbur
- Version: 1.1
- Date: 2026-10-10
- Description: 用户反馈两项：①thinking（reasoning）耗时无显示；②块脚标 Elapsed 53s 与状态栏 TTFT 4.4s + Output 49s「对不上」。
             根因：ttfbMs 打点于首个 text/reasoning chunk（思考开始即触发），思考期被并入 Output、无独立指标；
             块脚标用总时长、速度行拆 TTFT/Output，两处口径未互相印证。本方案拆分思考阶段并统一三处显示。
- 状态：**已实施**（2026-10-10）：初审 1M+4S 已吸收，复审通过；全量 356 passed，exit 0。后端新打点需重启服务生效；前端静态部分刷新即生效。

## 1. 事实核对（基于当前会话 jsonl 实测）

| 项 | 实测（倒数第 2 条 assistant） | 结论 |
|---|---|---|
| jsonl timings | `ttfbMs=4386, durationMs=52976, textChars=865, reasoningChars=5052` | 数据齐全 |
| 块脚标显示 | `Elapsed 53s`（= durationMs） | 数字正确但语义孤立 |
| 速度行显示 | `TTFT 4.4s · Output 49s · TPS 60.4 tok/s`（49=52976-4386） | 数字正确但口径未拆思考 |
| thinking 耗时 | **无任何展示** | 缺失 |

打点事实（`chatCompletions._noteStreamYield` / `responsesAdapter._noteStreamYield`）：
- `ttfbMs` = 首个 **text 或 reasoning** chunk 的时点 → 有思考时它是「思考开始」，不是「正文开始」；
- 思考增量也计入 `textChars/reasoningChars` 累计，但**没有 reasoning 首末时点**，无法算出思考阶段时长；
- `durationMs` 截止流结束。

## 2. 目标

1. **新增后端打点**：`reasoningStartMs`（首个 reasoning chunk 时点，相对 t0）、`reasoningEndMs`（最后一个 reasoning chunk 时点）→ 推导 `Thinking = reasoningEndMs - reasoningStartMs`（无 reasoning 时为 null）。
2. **状态栏速度行**改为（有思考时）：
   `⏱ TTFT 4.4s · Thinking 42s · Output 7s · TPS 123.6 tok/s`
   - TTFT 含义修正为「到首个输出（思考或正文）」；
   - Output 修正为 `durationMs - reasoningEndMs`（思考结束后至收尾，即正文阶段）；
   - TPS 分母改为正文阶段时长（reasoning tokens 不再摊薄 TPS，语义更准）。
   - 无思考：维持 `TTFT x · Output y · TPS z`（Output=duration-ttfb，退化为原口径）。
3. **块脚标**（assistant 消息块右下角）改为与速度行同一口径的自解释组合（有思考时）：
   `Elapsed 53s · Thinking 42s · Output 7s`
   - 无思考：`Elapsed 53s`（维持现状，不啰嗦）；
   - 数字互相印证：53 ≈ 4.4 + 42 + 7（尾差为传输/收尾开销），用户「对不上」的困惑消除。
4. **历史回放**同口径（jsonl timings 已持久化新字段，透传即可）。

## 3. 非目标

- 不改 ttfbMs 语义本身（改为首个正文 chunk 会破坏既有口径与旧数据可比性）；
- 不新增 TTFT 单独到正文的指标（TTFT→Thinking→Output 已可推导，避免指标爆炸）；
- 不做会话级累计/平均；
- 不改工具卡 Elapsed（工具执行耗时，与本口径无关）。

### 已知限制（审核 S，验收时不视为 bug）

- 旧会话 jsonl（有 reasoning 无 reasoningStart/End 打点）回退旧口径后，Output=duration-ttfb 仍含思考段，「块脚标与速度行对不上」对旧数据依旧存在，仅新会话修复；
- **TPS 口径 v2 变更**（分母 duration-ttfb → duration-reasoningEnd），跨版本数值不直接对比，将写入契约文档提示。

## 4. 改动面

| 文件 | 改动 | 版本 |
|------|------|------|
| `flamingoAgents/models/chatCompletions.py` | `_noteStreamYield`：reasoningChunk 时记录 `reasoningStartMs`（首个）与更新 `reasoningEndMs`（每个）；`successTimings` 增加两字段 | 1.21→1.22 |
| `flamingoAgents/models/responsesAdapter.py` | 同步 `_noteStreamYield` 打点（successTimings 自 chatCompletions import 共享，仅改本文件打点处） | 1.6→1.7 |
| `flamingoAgents/core/types.py` 等 | timings 为 dict 透传，无需结构改动 | — |
| `webApp/backend/sessionStore.py` | `lastTimings` 白名单增加 `reasoningStartMs`/`reasoningEndMs` | 1.7→1.8 |
| `webApp/frontend/js/statusUsage.js` | `readTimings` 读新字段；`formatSpeedLine` 新口径（见 §2.2）；新增 `formatStepElapsedLine`（块脚标组合，§2.3） | 1.3→1.4 |
| `webApp/frontend/js/chatView.js` | 块脚标（流式 applyStepTimings + 历史回放）改用 `formatStepElapsedLine`；title 更新 | 1.28→1.29 |
| `webApp/frontend/js/statusBar.js` | renderSpeed 无结构变化（formatSpeedLine 内聚新口径）；title 同步 | 1.8→1.9 |
| `webApp/frontend/index.html` / `styles.css` | cache-bust 提版 | 1.29→1.30 |
| `tests/testLatencyMetrics.py` | 新增 reasoning 打点断言（fake 适配器产 reasoning chunk） | 1.0→1.1 |
| `tests/testLatencyMetricsFrontend.py` | 新口径断言（速度行/块脚标/历史） | 2.0→2.1 |
| `docs/webApiSpec.md` | §3.14 lastTimings 与 usageUpdate.timings 字段说明补新字段、口径与 TPS v2 提示 | 1.31→1.32 |

## 5. 关键设计决策

- **D1 Thinking = reasoningEndMs - reasoningStartMs**：思考流式增量首末时点差，含思考内部网络间隔，为近似值；无 reasoning（reasoningChars=0 或无打点）时为 null 不显示。**非流式 complete 路径**（chatCompletions L313 调 successTimings，无 chunk 事件）两键天然缺失 → 显示层走无思考形态，属预期回退非缺陷（审核 M）。
- **D2 Output = durationMs - reasoningEndMs**（有思考）；无思考时维持 durationMs - ttfbMs。思考与正文间可能穿插（Responses 思考块通常在正文前连续），若实测出现交錯，取 reasoningEnd 为最后 reasoning chunk 时点仍正确（正文首包通常其后）。
- **D3 TPS 分母 = Output**（正文阶段），completionTokens 含 reasoning token 的 provider（部分 provider 把 reasoning 计入 completion）会使 TPS 偏高——title 保留「近似吞吐」声明，不强行修正。
- **D4 块脚标数字和不必精确等于 Elapsed**（传输/收尾开销数秒），组合内数字与状态栏同源即可对得上。
- **D5 前端统一边界防御**（statusUsage）：新字段任一无效（非 int/负数）、`reasoningStartMs >= reasoningEndMs`、或 `reasoningEndMs > durationMs` → 整体按无思考回退旧口径（Thinking 不显示，Output=duration-ttfb），单点判定不散落各处（审核 S）。

## 6. TODO（实施顺序）

- [ ] T1 后端打点：chatCompletions/responsesAdapter `_noteStreamYield` + `successTimings` 新字段；`uv run pytest tests/testModelStreamDiag.py tests/testResponsesAdapter.py tests/testLatencyMetrics.py -q`。
- [ ] T2 sessionStore 白名单 + 契约文档字段说明；`uv run pytest tests/testSessionStore.py tests/testLatencyMetrics.py -q`。
- [ ] T3 前端 statusUsage（readTimings/formatSpeedLine/formatStepElapsedLine）+ chatView 脚标两路径 + title；红测试先行（新口径断言）。
- [ ] T4 cache-bust + 版本头 + 全量 `uv run pytest -q`（exit 0）。
- [ ] T5 用户刷新确认（视觉验收不替代）。

## 7. 审核记录

### 初审（company71/glm-5.3，2026-10-10，只读）
- 结论：需修订（1M + 4S），v1.0 主干成立。
- M：非流式 complete 路径 reasoning 打点天然缺失未声明 → D1 已补回退声明。
- S1：formatSpeedLine 边界防御未系统化 → 新增 D5 统一规则。
- S2：旧会话回退后仍对不上 → 已列入已知限制。
- S3：TPS 分母切换跨版本不可比 → 已列入已知限制 + 契约 TODO。
- S4：successTimings 实为全仓共享（responsesAdapter import）→ §4 表述已精简。
- 其余：打点位置/透传链路/白名单/接线点/测试锚点全部 OK。
