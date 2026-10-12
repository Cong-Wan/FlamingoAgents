# 耗时展示 Demo 界面方案

- Author: wilbur
- Version: 1.3
- Date: 2026-10-12
- Description: 根据用户对当前聊天界面耗时信息层级的描述，新增一个独立、可直接打开的静态 demo 供视觉确认；不修改生产聊天逻辑、不接入真实数据。根据两轮审核意见补充原生折叠语义、半开时间区间与限定范围的静态验收契约；已完成 demo 实施与静态验证。
- 状态：Demo 已实施，等待用户视觉确认

## 1. 用户目标与本次交付边界

用户希望先确认展示效果，再决定是否修改真实耗时计算与持久化逻辑。本次只交付一个静态 demo，模拟一轮包含 thinking、工具调用和最终回答的对话。

### 1.1 必须在 Demo 中体现的视觉规则

1. 现有用量行的耗时区域只显示 `TTFT` 与 `TPS`，并放在 `↑ … · ↓ … · ⚡ … cached · $… · … / …` 之后。
2. 耗时行不显示 `Thinking`、`Output`。
3. 顶部用一个完整的静态示例展示已完成状态，例如：`TTFT 1.9s · TPS 42.6 tok/s`；不展示 `--`，避免复现当前用户不想要的残留状态。
4. thinking 块下方只显示本块 thinking 耗时，例如 `Thinking 1.5s`；不在同一位置重复放 `Elapsed`。
5. 工具卡内部保留工具自身执行时间，例如 `Tool execution 1.2s`。
6. 工具卡片下方另放一行工具阶段总耗时，例如 `Tool call 2.9s`，语义为“LLM 开始产出该次工具调用参数 → 工具执行完成”。它是工具阶段的包含性总耗时，包含参数生成/传输和工具实际执行；工具卡内部的 `Tool execution 1.2s` 只是其中的实际执行区间，不与 `Tool call` 相加。demo 不放人工确认停顿，后续真实实现需明确确认等待是否纳入同一边界。
7. 最后一轮回答内容下方另放一行总体耗时，例如 `Total 9.2s`，语义为“用户发消息 → 最后一个 token 输出”，是端到端总耗时，不等同于当前单次模型调用的 `Elapsed`，也不与工具阶段耗时直接相加。
8. demo 展示多个 thinking 块，证明耗时是“每个 thinking 块独立展示”，而不是把所有 thinking 合并成一行。
9. demo 的工具卡初始为展开态，并支持点击折叠/展开；折叠后工具执行时间随详情隐藏，而卡片外的工具阶段总耗时仍可见。无需在同一静态画面同时展示两种状态。

### 1.2 明确非目标

- 不修改 `webApp/frontend` 生产页面、SSE、后端 timings、历史数据或状态栏逻辑。
- 不在 demo 中模拟真实计时、会话切换、刷新或数据持久化。
- 不决定最终英文/中文文案，只用短英文标签让信息层级清楚；视觉确认后再定稿。
- 不新增第三方依赖、不创建前端构建流程、不启动开发服务器。
- 不处理当前工作区已有的未提交改动。

## 2. 现状依据

当前生产界面已有以下结构，可作为视觉参考但不直接复用生产代码。需要特别说明：用户要求的目标状态是把 TTFT/TPS 接到现有用量信息同一行末尾；当前生产页面仍有独立的 `#statusSpeed` 行。本 demo **有意展示目标状态而不是复制当前实现**，以便用户先确认最终层级。

当前生产界面已有以下结构：

- `webApp/frontend/index.html`：浅色侧栏、聊天消息流、composer 下方状态栏。
- `webApp/frontend/styles.css`：`.msg-*`、`.thinking-block`、`.tool-card`、`.composer-status` 等样式。
- `webApp/frontend/js/chatView.js`：已有 thinking、工具卡和 `Elapsed` 相关渲染，但用户希望调整信息归属与层级。
- `webApp/frontend/js/statusBar.js`：当前速度行仍可能包含 `Thinking`、`Output`，不作为 demo 的最终展示依据。

## 3. Demo 设计

### 3.1 文件位置

新增：`docs/261012_latencyDisplayDemo.html`

选择独立 HTML 的原因：用户可以直接用浏览器打开确认；不污染生产页面；静态资源全部内联，不依赖服务端、登录态或 cache-bust。

### 3.2 页面结构

1. 页面背景与主容器：复用当前产品的浅色、细边框、圆角、紧凑字号。
2. 左侧 sidebar：保留会话列表视觉参照，标记一个当前会话；不实现交互。
3. 顶部标题栏：显示 `FlamingoAgents` 与模型标签。
4. 状态行：
   - 第一行显示工作目录。
   - 第二行显示 `↑ 2.9k · ↓ 443 · ⚡ 2.0k cached · $0.0013 · 0.3% / 1.0M · TTFT 1.9s · TPS 42.6 tok/s`。
   - TTFT/TPS 与用量数据使用同一行，并以分隔点区分；**耗时子区段**不出现 `Thinking`、`Output` 或 `--`。这是对用户目标的刻意展示，虽然当前生产 DOM 仍把速度信息放在独立行；页面其它说明文字可以出现 `Thinking`。
5. 消息区：
   - 用户消息气泡。
   - 助手第一个思考块：展开内容 + `Thinking 1.5s`。
   - 第一个工具卡：默认展开以便同时看到工具入参、工具结果、工具自身 `Tool execution 1.2s`；卡片下方显示 `Tool call 2.9s`。点击卡片标题后详情折叠，但卡片外的 `Tool call` 保持可见。
   - 助手第二个思考块：折叠内容 + `Thinking 0.8s`，验证每块独立耗时。
   - 最终回答正文；正文下方显示 `Total 9.2s`。
   - 在消息区顶部附一条低对比度“示意时间线”说明：`request → first thinking token: TTFT 1.9s | first → last thinking token: Thinking 1.5s | Tool call 2.9s（含 Tool execution 1.2s）| Thinking 0.8s | Final output 2.1s | Total 9.2s`，并注明“半开区间，边界只计一次；非生产文案”，避免示例数字被误解为当前后端已计算的同一指标。
6. 底部 composer：保持当前界面输入框外观，仅作为完整界面背景，不发送消息。

### 3.3 语义与视觉层级

- `Thinking x.xs`：紧邻对应 thinking details 下方，使用低对比度小号文字；每个块单独一行。
- `Tool execution x.xxs`：位于工具详情内部结果区域末尾，表示工具真正开始执行到执行完成的时长。
- `Tool call x.xxs`：位于整个工具卡外侧下方，使用与 thinking/total 同级的小号文字；表示从 LLM 开始生成本次工具参数，到该工具执行完成的包含性时长，包含 `Tool execution`，不与其相加。
- `Total x.xxs`：位于整轮 assistant 内容末尾，使用稍高对比度但不抢正文的样式；表示用户消息发出到最后一个回答 token 输出的端到端时长，包含等待、thinking、工具阶段和最终正文输出。
- 顶部 `TTFT/TPS`：与 token、费用、窗口占用处于同一信息行；不额外产生速度行，也不出现 `Thinking/Output`。
- 示意时间线采用不重叠的半开区间：`request → first thinking token = TTFT 1.9s`，`first → last thinking token = Thinking 1.5s`，随后为 `Tool call 2.9s`、`Thinking 0.8s`、`Final output 2.1s`，所以 `1.9 + 1.5 + 2.9 + 0.8 + 2.1 = Total 9.2s`；相邻边界只计一次。这只是 demo 数值关系，不规定后端最终字段名。

### 3.4 交互

工具卡使用浏览器原生 `<details open>` + `<summary>` 实现展开/折叠，不写自定义脚本：初始为展开态；折叠时浏览器只隐藏详情内的入参/结果/`Tool execution`，卡片外的 `Tool call` 仍可见。其余按钮、会话项、发送按钮均为视觉占位，不触发真实行为。

## 4. 实施步骤与 TODO

### P0 方案与审核

- [x] P0.1 创建本方案文档。
- [x] P0.2 使用全新的 subagent 审核方案，重点检查是否误改生产代码、指标语义是否混淆、demo 是否覆盖用户描述。
- [x] P0.3 根据审核意见修订方案并再次审核，直到没有明显问题。

### P1 Demo 实现

- [x] P1.1 新建 `docs/261012_latencyDisplayDemo.html`，添加文件头并使用当前日期/版本号。
- [x] P1.2 写入完整静态页面骨架：sidebar、topbar、status row、message list、composer。
- [x] P1.3 实现顶部用量与 `TTFT/TPS` 同行展示，确认不含 `Thinking/Output/--`。
- [x] P1.4 实现至少两个 thinking 块，每个块下方各自显示 `Thinking` 耗时。
- [x] P1.5 实现工具卡内部 `Tool execution` 与卡片外部 `Tool call` 两个不同层级的耗时。
- [x] P1.6 实现最终回答下方 `Total` 耗时，并避免把它放进工具卡或 thinking 块。
- [x] P1.7 实现工具卡折叠/展开，不引入第三方脚本。
- [x] P1.8 添加窄屏响应式规则，避免 demo 在普通笔记本窗口横向溢出。

### P2 验证

- [x] P2.1 运行静态 HTML 结构检查，确认文件可解析、标签成对、关键文案位于预期容器。
- [x] P2.2 只新增一个与 demo 配套的静态检查测试（不修改现有生产测试），使用 Python 标准库解析 DOM，至少断言：①目标状态行包含 TTFT/TPS 且 TTFT/TPS 位于用量文本之后；②**仅耗时子区段**不存在 `Thinking`、`Output`、`--`；③两个 thinking 块各自紧邻一个 `Thinking` 耗时；④`Tool execution` 在工具详情内部；⑤`Tool call` 是工具卡外部的兄弟节点，且工具卡使用原生 `<details open>`；⑥`Total` 位于回答正文之后并使用 9.2s 示意值；⑦不存在旧的 assistant `.step-elapsed`。
- [x] P2.3 不添加自定义脚本，因此无需 `node --check`；若实现时意外加入内联脚本，再使用 `node --check` 或最小 Node 执行检查。
- [x] P2.4 检查 `git diff --stat` 和 `git status`，确认本次只新增 demo、方案与配套静态检查文件，不改生产文件。
- [ ] P2.5 向用户报告 demo 路径、直接打开方式和视觉验收点，等待确认后再做生产改动。

## 5. 成功标准

- 直接浏览器打开 `docs/261012_latencyDisplayDemo.html` 能看到完整聊天界面 demo。
- 状态栏用量行中，TTFT/TPS 位于费用与窗口信息之后，且耗时子区段不存在 `Thinking`、`Output`、`--`。
- 页面可同时区分并定位四种耗时：thinking 块耗时、工具执行耗时、工具阶段总耗时、整轮总耗时。
- 至少两个 thinking 块各有自己的耗时，不产生重复的 `Elapsed` 行。
- 工具卡展开/折叠时，工具执行时间只在详情内显示，工具阶段总耗时始终在卡片外部显示。
- demo 中的示意时间线数值自洽：1.9 + 1.5 + 2.9 + 0.8 + 2.1 = 9.2；`Tool execution 1.2s` 是 `Tool call 2.9s` 的子区间，不重复计入总和。
- 不影响当前生产前端和后端行为；用户确认之前不实施真实逻辑改造。

## 6. 风险与取舍

| 风险/不确定点 | 处理方式 |
|---|---|
| 用户说“工具调用卡片的下方”可能指卡片外部紧邻位置 | demo 明确把 `Tool call` 放在 `.tool-card` 外部；让用户直接确认层级 |
| `Tool execution` 与 `Tool call` 的最终文案未指定语言 | 先用短英文标签保证语义可辨；确认后再统一文案 |
| `Tool call` 是否包含人工确认等待尚未确定 | demo 不放确认弹窗并标注为“无确认停顿的示意值”；真实实现前单独确定边界 |
| 状态栏用量行在窄屏可能过长 | demo 使用状态行内部横向滚动/可收缩布局，不折成第二个速度行 |
| 当前生产代码有大量未提交修改 | demo 独立于生产文件，只做新增文件，并在验证时检查 diff |

## 7. 审核记录

- 初审：subGROK/grok-4.7；指出需补充工具时间边界、Total 数值自洽、静态验收断言，并要求澄清顶部合并行与当前生产结构的差异。
- 修订：已将顶部合并行明确为用户目标而非生产现状；补充 9.2s 端到端示意时间线、Tool call 包含 Tool execution 的关系、折叠行为和具体静态断言。
- 复审：subGROK/grok-4.7；指出需修正静态测试对折叠交互的表述、明确 TTFT/Thinking 的半开区间，并限定禁止词检查只作用于耗时子区段。
- 二次修订：改用原生 `<details>` 语义，移除自定义脚本要求；补充半开时间线和限定范围断言。
- 复审结论：subGROK/grok-4.7 明确给出“无必须修复问题，可以实施”；仅建议将记录状态与文首同步，已在本次版本更新中完成。
