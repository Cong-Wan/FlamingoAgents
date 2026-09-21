# 会话内与整体用量统计深度调研计划

- Author: wilbur
- Version: 1.3
- Date: 2026-09-20
- Status: 调研与最终文档审核均完成（无 Blocker/High/未处置 Medium）
- Scope: 只读调研与文档落档；不修改业务代码、数据库、配置或用户日志
- Deliverable: `docs/260920_usageStatisticsResearch.md`

## 1. 目标与成功标准

本次要回答两个彼此独立的问题：

1. **会话内统计是否合理**：单个会话的 prompt/cached/completion 累计、状态栏拆分、上下文使用率和费用估算，是否与 provider 返回值及持久化历史一致。
2. **整体统计是否合理**：用量页顶部汇总、会话表格、时/天/月趋势和总费用，究竟覆盖哪些调用，是否存在漏计、重复计数、错归模型、时间错桶或恢复后分叉。

成功标准：

- [x] 画清从 provider terminal usage 到 JSONL、内存累计、`sessions.json`、`usage.db`、API 和前端的完整数据流。
- [x] 明确 `promptTokens` 与 `cachedTokens` 的包含关系，验证展示与费用没有重复计数。
- [x] 覆盖普通回答、多工具 step、确认续流、重试、停止、异常、重启恢复、删除会话、切模型、CLI/SDK/askSubAgent。
- [x] 区分“精确统计”“合理近似”“产品口径选择”“确定性缺陷”，不能把已声明限制混写成 bug。
- [x] 每个问题都给出源码锚点、触发条件、影响、严重度和建议。
- [x] 对本机现有数据只读做结构对账，且不输出正文、密钥和不必要的会话标识。
- [x] 最终文档给出总判定、优先级及可验证的后续 TODO。

## 2. 关键假设与边界

- “会话内用量”暂按一个 Web session 的全部成功模型调用理解，但**不预设**本地三字段已经正确表达每一家 provider 的语义；先建立 provider/API/流式模式字段矩阵，再判断累计口径。
- `cachedTokens ⊆ promptTokens` 只是当前代码和 OpenAI 风格协议采用的假设，不作为调研先验结论。需分别核对 Chat Completions、Responses 及兼容 provider 的字段来源；无法由源码/协议确认的部分标为“代码假设、未获上游账单验证”。
- “整个用量”存在两种可能语义，调研必须分别判断：
  - A. Web 用量页当前实现的统计范围；
  - B. FlamingoAgents 进程体系内全部模型调用（含 CLI、SDK、askSubAgent）。
- provider 未下发 terminal usage、失败尝试和停止在终态 usage 前发生时，本地无法凭当前实现获得账单级精确 token；报告会明确这类天然缺口。
- 费用是按 `models.yaml` 单价计算的估算值，不预设它等同 provider 账单。
- 本次不联网核验各 provider 官方账单，只判断当前代码内部口径、协议兼容性和持久化一致性。

## 3. 调研方法

### 3.1 静态链路审计

- 先做全仓 usage/token/cost 的**生产者—转换器—持久化 sink—读取入口**搜索，形成入口覆盖矩阵；下列文件是已知重点而非“完整性”的先验保证。
- Provider 适配：`chatCompletions.py`、`responsesAdapter.py`。建立 provider/API/stream 矩阵，逐项记录 input、cache-read、cache-write、output、reasoning、total、缺省行为和证据强度。
- 会话核心：`conversation.py`、`agent.py`、事件类型。
- Web 持久化：`agentManager.py`、`sessionStore.py`、`usageStore.py`。
- API 与恢复/迁移：`server.py`、`sessionRecovery.py`、`logMigration.py`。
- 展示：`statusUsage.js`、`statusBar.js`、`usageView.js`、`sidebarView.js`。
- 非 Web 入口：`askModel.py`、`sdkEntry.py`、`askSubAgent`。
- 契约与既有测试：`webApiSpec.md`、live usage/session recovery tests。

对账前先定义规范记录粒度：`sessionId + 泵/请求边界 + model step + provider/model + terminal timestamp`。明确每个来源是单 step 增量、会话累计还是泵增量；重试失败、确认续流及重复 terminal 事件单列，不直接把各层总和作等价比较。当前 schema 若缺稳定唯一键，报告必须明确无法完成逐条一一映射的限制。

### 3.2 只读数据对账

只读取 token 元数据并聚合，不输出消息正文。先按可获得的 request/step/turn 边界逐记录分类 matched/unmatched，再做以下聚合：

1. `sessions.json usage` ↔ 对应 Web JSONL 中合法 `assistantMessage.usage` 的规范化 step 求和；
2. `usage.db` 按 session 的泵增量 ↔ 可映射 JSONL step 求和；无法由 schema 唯一映射时只做总量/时间区间校验，不伪造逐条对应；
3. 现存会话、已删除会话账、孤立日志、CLI 日志分别计数；
4. 检查负数、`cached > prompt`、非法类型、缺 usage、异常时间戳、精确重复行；
5. 检查会话当前模型与最后记账模型不一致的规模；
6. 统计 CLI/SDK JSONL 中存在但未进入 Web `usage.db` 的 token 量级。

一致性控制：调查开始/结束记录 Git commit/工作树状态、文件 mtime/size/hash；SQLite 使用只读连接并在单一读事务中取边界。若检测到期间仍在写入，则把结果标为“非同一截止点、仅线索”，不宣称严格对账；必要时只建议用户停服后复核，不擅自停止服务。聚合证据经脱敏后直接写入最终文档，不保存正文或长期保留额外原始副本。

若本机数据不存在或损坏，记录为“无法实证”，不据此推断代码正确。

### 3.3 定向实验与测试

- 运行现有相关 pytest（只使用项目既有测试框架与 uv 环境）。所有新增实验固定临时 HOME、临时缓存/数据库、mock provider，禁止联网和生产路径写入。
- 用临时目录和内存/临时 SQLite 做最小复现实验，不改真实数据：
  - JSONL 已落而 DB/索引未落的崩溃窗口；
  - DB 写成功、索引写失败；
  - provider usage 缺失/异常/兼容字段；
  - 跨时间桶的整泵归属；
  - 切模型后会话累计与模型归属。

## 4. 判定尺度

- **正确**：字段语义、累计、展示、持久化和契约一致，且关键边界有测试。
- **合理但有限**：实现与声明一致，可用于趋势/状态，但不是 provider 账单级数据。
- **不合理/缺陷**：相同输入可确定地产生漏计、重复、错归、永久分叉或明显误导。

严重度：

- **High**：常规路径可显著错账/丢账、无法恢复，或“整体”结论根本不成立。
- **Medium**：特定但现实边界下永久不一致、归属错误或显著误导。
- **Low**：边缘兼容、展示、性能或防御性问题。

每项同时标证据置信度（已用代码/测试复现、静态可证、样本线索、需上游验证）和可量化影响（例如重复/遗漏的 token 范围）；无法量化时明确原因。

## 5. 详细 TODO List

### Phase A — 建立口径与数据流

- [x] A1. 记录两类 adapter 的 usage 获取时机与字段归一化。
- [x] A2. 核对 JSONL 写入与 `usageTotal/lastTurnTokens` 更新顺序。
- [x] A3. 核对多 step、重试、确认续流和 stop 的累计边界。
- [x] A4. 核对 live `usageUpdate`、终态 `_recordUsage` 和 at-most-once 机制。
- [x] A5. 核对状态栏、侧栏、汇总卡和图表各自公式。

### Phase B — 持久化与恢复正确性

- [x] B1. 分析 JSONL、sessions 索引、SQLite 三写之间的原子性与崩溃窗口。
- [x] B2. 核对进程重启时哪些层可由 JSONL 恢复、哪些不会回填。
- [x] B3. 核对删除会话、索引恢复、旧库迁移后的统计语义。
- [x] B4. 核对并发锁、幂等、重复行与负 delta 防护。

### Phase C — 范围、归属与费用

- [x] C1. 判断 `/api/usage` 与 `/api/usage/series` 的数据集是否一致，并评估双口径 UI。
- [x] C2. 核对 CLI、SDK、askSubAgent 是否纳入“整体”。
- [x] C3. 核对会话切模型、活跃流切模型、provider/model 归属。
- [x] C4. 核对缓存拆分、cache write、当前价格回算、删模型和订阅零价格；把 token 事实与费用估算分开，记录币种、每百万单位、价格生效时间及未知价格处理。
- [x] C5. 核对时间源、时区、半开窗口、DST、迟到/恢复记录、跨桶和落账时间语义。

### Phase D — 实证与测试

- [x] D1. 对真实数据做脱敏结构对账并保存汇总证据，统一输出样本数、matched/unmatched 数、偏差条数、偏差 token 与偏差率（分母可定义时）。
- [x] D2. 运行 usage/session/recovery 相关测试并记录结果。
- [x] D3. 对未覆盖的高风险路径做临时最小实验。
- [x] D4. 列出当前测试已证明与未证明的边界。

### Phase E — 文档与审核

- [x] E1. 创建 `docs/260920_usageStatisticsResearch.md`，写明基线、范围、证据和总判定。
- [x] E2. 问题按严重度列出源码锚点、触发条件、影响和建议。
- [x] E3. 给出“当前可相信什么 / 不可相信什么”的用户视角结论。
- [x] E4. 给出分阶段改进 TODO 与验收标准，但本次不改代码。
- [x] E5. 创建全新 subagent 独立审核本计划；逐项修订（首轮结论：需修订后执行）。
- [x] E6. 修订后再创建全新 subagent 复审，直至无 Blocker/High，所有 Medium 均已修订或书面接受。
- [x] E7. 创建全新 subagent 审核最终调研文档的证据完整性；最终聚焦复审结论为无 Blocker/High/未处置 Medium。

## 6. 预期风险与控制

| 风险 | 控制 |
|---|---|
| 把 cached 重复算作额外 token | 仅对字段矩阵已确认包含关系的 provider 用 `prompt + completion` 核对，cached 作 prompt 子集展示/计价；其余按各自语义单独核对 |
| 把产品选择误报为代码 bug | 先对照 `webApiSpec.md` 和 UI 注释，再判断命名是否仍会误导 |
| 真实数据含敏感正文 | 脚本只读取事件类型、usage、timestamp 和模型元数据，不打印正文/请求；只把脱敏聚合写入最终文档 |
| 多文件读取不在同一截止点 | 记录前后 mtime/size/hash；SQLite 单只读事务；检测变化即降级为线索，不声称严格一致 |
| 测试意外联网或写生产路径 | 临时 HOME/缓存/DB + mock provider + 显式禁网；不调用真实发送接口 |
| 只看测试而漏掉崩溃窗口 | 按写入顺序逐点做故障矩阵，并核对是否存在自动 reconciliation |
| 使用过时文档代替源码 | 所有结论以当前工作树函数锚点为主，文档仅用于核对设计意图 |
| 调研期间误改用户数据 | SQLite 只读 URI；不调用写接口；业务源码零修改 |

## 7. 输出结构

最终调研文档计划包含：执行摘要、口径定义、完整数据流、正确性矩阵、真实数据对账、问题清单、测试覆盖、结论、优先级建议和后续 TODO。

## 8. 审核记录

### 8.1 首轮独立审核（subGPT/gpt-5.6-sol，2026-09-20）

结论：**需修订后执行**。主要问题及处置：

- High：不应预设 cached 必为 prompt 子集 → 已改为 provider/API/stream 字段矩阵，区分代码假设与上游验证。
- High：直接聚合 JSONL/DB 隐含粒度一致 → 已先定义规范记录粒度、增量/累计和 matched/unmatched 分类。
- High：真实文件缺共同截止点、测试隔离不够明确 → 已增加 mtime/hash/只读事务一致性控制，以及临时 HOME、mock provider、禁网约束。
- Medium：已知文件清单不能证明完整 → 已增加全仓生产者/sink 搜索和入口覆盖矩阵。
- Medium：历史费用按当前价回算、时间边界定义不足 → 已补价格事实/估算分离、单位/生效时间，以及时区/半开区间/DST/迟到记录。
- Low：证据保存和审核退出标准不清 → 已规定只在最终文档保存脱敏聚合，并写明复审退出标准。

### 8.2 全新 subagent 复审（glm/glm-5.3，2026-09-20）

结论：首轮问题全部闭环，**无 Blocker/High/未处置 Medium，计划可执行**。复审提出三条 Low 建议并已吸收：消除 cached 子集措辞张力；为对账证据增加样本数/偏差率输出格式；补记本轮审核记录。

### 8.3 最终调研文档审核（subGPT/gpt-6-astra，2026-09-20）

多名宽范围 reviewer 超时且未形成结论，均未计为通过。随后创建全新、窄范围只读 reviewer，对最终报告四项主结论及直接源码完成事实核验，结论：**无 Blocker/High/未处置 Medium，可交付**。
