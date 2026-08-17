<!--
Author: wilbur
Version: 1.0
Date: 2026-08-17
Description: README v1.1 → v1.2 更新方案（补全式，非增量同步）。README v1.1（commit fc6a3b3，
             2026-08-14）当时只补了 skill 一句话，大量已落地能力漏写：askSubAgent（08-12）、工具停止
             （08-13/14）、多窗口 attach（08-11）、文件树@附件/状态栏//model（08-08）、models.json 导入
             （08-13）、流式渲染优化（08-12）等多为 v1.1 之前/同期合入；v1.1 之后实际仅 2 个提交
             （22949f3 工具结果折叠、2cdce4e 新建会话快捷键）。本次为补全 README 漏写的已落地能力，
             不改动任何代码。v1.1 修订：按 code-review 子代理审核，修正时间线归因（锚点 fc6a3b3 非
             2cdce4e）、docs/plan 数量 21、新建会话快捷键声明不收录、技能编辑 API 验证改 grep 路由。
-->

# README 更新方案（v1.1 → v1.2）

## 1. 背景与目标

**定位：补全式更新，非增量同步。** README v1.1（commit fc6a3b3，2026-08-14）当时只补了 skill
一句话，大量已落地能力从未写进 README。git 事实：README v1.1 之后仅合入 2 个提交
（22949f3 工具结果折叠内滚动、2cdce4e 新建会话弹窗 Enter/Esc 快捷键）；而下列能力多为
**v1.1 之前/同期就已合入但 README 漏写**：

- 纯库 `flamingoAgents/`：**askSubAgent 子代理工具**（08-12 合入，v1.4–v1.7，function call 形式、
  超时透传默认 600s/上限 3600s、可中断）；bash/askSubAgent 均接入 `_runWithInterrupt` 中断闭环。
- **Skill 系统**（08-14 fc6a3b3，与 README v1.1 同提交）：库侧只读加载 `config/skills/<name>/SKILL.md`
  注入 system prompt（resume 不重注）；Web 侧「技能」页（只读列表 → 按模板结构化编辑保存）+
  `/skill:` 斜杠命令 chip 交互（选中生成 chip、发送时拼正文、气泡不显示全文）。
- Web 端体验能力：工具执行**可停止**（08-13/14，fire-and-forget + 立即 abort、stopping 宽容闸）、
  **多窗口并行流式**（08-11，attach 回放式重连）、文件树 + `@` 附件（08-08；**文件夹可选**为 08-14
  后未提交改动，chip 带 type 📄/📁，后端递归展开目录附件）、`/model` 会话切模型（08-08）、
  状态栏（lastUsage + contextUsedPercent，08-08）、`models.json` 导入（08-13，POST /api/models/importPi）、
  流式渲染优化（08-12，read1 + 批量 Start + paint 合并）、工具结果卡片折叠内滚动（08-14，
  **v1.1 之后新增**）、新建会话弹窗快捷键（08-14，v1.1 之后新增）等。
- 工程：`docs/plan/` 方案目录成型（实测 21 份方案/事故报告），`docs/` 根下契约文档收敛为
  `webApiSpec.md` / `addCallableToolFunction.md` / `toolCallInterruptionIncidentReport.md`
  （`webAppPlan.md` / `streamOutputPlan.md` 已移入 `docs/plan/`）——README 的「文档索引」表
  路径需同步修正。

**目标**：让 README 的「现状能力 / 目录结构 / 文档索引 / 路线图」四节与代码现状一致，
头部版本号 v1.1 → v1.2、Date → 2026-08-17。

**非目标**：

- 不改任何 `.py` / `.js` / `.css` / `.html` 代码；
- 不重写「开发初心」「架构」「快速开始」（这些内容仍准确）；
- 不展开每个特性的细节（README 是门面，细节留在 `docs/plan/` 各方案文档）。

## 2. 成功标准（可验证）

1. `grep -n "Version: 1.2" README.md` 命中；Date 为 2026-08-17；
2. README 中提到的每个能力都能在代码中找到落点（抽查清单见 §4）；
3. 「文档索引」表中每条路径 `test -f` 存在；
4. 路线图勾选状态与代码事实一致（askSubAgent 已实现 → 对应条目更新表述）。

## 3. 变更点清单（逐节）

### 3.1 文件头

- Version: 1.1 → 1.2；Date: 2026-08-14 → 2026-08-17；
- Description 追加一句：v1.2 同步 askSubAgent 子代理、Skill 系统（含技能页编辑与 /skill: chip）、
  工具停止、多窗口流式、文件夹附件、models.json 导入等能力。

### 3.2 「现状能力 — flamingoAgents 纯库」

- 工具系统条目：内置工具列表 read/write/edit/bash → 补 **askSubAgent**；
  补一句「工具执行可中断（`_runWithInterrupt`，bash/askSubAgent 分片 poll + terminate/kill）」。
- Skill 条目：当前写的是「只读加载」，需补充 Web 侧已支持**按模板编辑保存**
  （frontmatter name/description/disable + 正文，保存仅影响下次新建会话）。

### 3.3 「现状能力 — webApp」

对话界面条目补充/修订：

- 工具调用卡片：补「结果可折叠内滚动预览」；
- 新增「**随时停止**」：停止按钮 fire-and-forget 即时 abort，跨窗口停止静默同步；
- 新增「**多窗口并行流式**」：同会话多标签页 attach 回放式重连，互不抢流；
- 新增「**文件树与 @ 附件**」：侧栏文件树浏览/读文件；输入框 `@` 唤起文件面板
  （目录可下钻也可整体选为附件，chip 📄/📁 区分，后端递归展开目录为文本块）；
- 斜杠命令：`/new` `/model`（会话内切换模型）`/skill:`（技能 chip，发送拼正文、气泡不显示全文）；
- 状态栏：模型名 / 最近一轮增量 tokens / 上下文使用率；
- 模型配置页：补「支持粘贴上传 models.json 一键转换导入（importPi，纯转换不落盘）」。

### 3.4 「目录结构」

- `config/` 行补 `skills/`（已存在但与 README 描述可再明确：`skills/<name>/SKILL.md`）；
- `docs/` 行描述修正：根下为契约/手册文档，方案文档归入 `docs/plan/`；
- `webApp/frontend/js/` 可保持一行概述，不逐文件列（README 不追求文件级索引）。

### 3.5 「文档索引」表

修正路径 + 补新文档：

| 现状（错） | 改为 |
|---|---|
| `docs/webAppPlan.md` | `docs/plan/webAppPlan.md` |
| `docs/streamOutputPlan.md` | `docs/plan/streamOutputPlan.md` |
| （无） | 补 `docs/toolCallInterruptionIncidentReport.md`（工具中断事故复盘） |
| `docs/webApiSpec.md`、`docs/addCallableToolFunction.md` | 路径不变 |

> 不列 `docs/plan/` 全部 21 份——表格只放「对外契约/手册/重要复盘」，
> 方案文档由 `docs/plan/` 目录自解释。README 正文不写死数量（避免数字随提交腐烂）。

> 取舍声明：**新建会话弹窗 Enter/Esc 快捷键**（2cdce4e）属交互细节，按 §1「README 是门面」
> 的非目标**不收录**进 README。

### 3.6 「路线图」

- 「多 Agent 编排」条目前为 `[ ]`，现状：askSubAgent 子代理工具已落地（function call 形式，
  子代理独立会话/独立模型可指定 provider/model），但 profile 化编排未做。
  → 改为：`- [x] 子代理工具 askSubAgent（function call 形式，可指定 provider/model，超时透传，可中断）`
  并保留 `- [ ] **多 Agent 编排**：profile 化管理…`（仍是未来项）。
- 其余条目不变。

## 4. 事实核查抽查清单（写完逐项验证）

| README 表述 | 验证命令 |
|---|---|
| askSubAgent 存在且可中断 | `grep -n "askSubAgent" flamingoAgents/tools/builtinTools.py` |
| Skill 加载注入 | `grep -n "def loadSkills" flamingoAgents/skills/skillStore.py` |
| 技能编辑 API | `grep -n "authedApi.put('/skills" webApp/backend/server.py`（验证路由存在，非文件头注释） |
| 文件夹附件 | `grep -n "type" webApp/frontend/js/fileMention.js`（chip 带 type） |
| models.json 导入 | `ls webApp/backend/piModelsImport.py` |
| 多窗口 attach | `grep -n "attach" webApp/frontend/js/chatView.js` |
| 文档路径 | `test -f docs/plan/webAppPlan.md && test -f docs/plan/streamOutputPlan.md && test -f docs/webApiSpec.md && echo OK` |

## 5. TODO

- [x] 1. 按 §3 改写 README.md（只动 README，不碰代码） → §4 抽查全过
- [x] 2. 子代理（code-review 风格）审核本方案 → 已按 4 条意见修订（时间线归因 / plan 数量 21 /
  快捷键取舍声明 / 技能 API 验证命令）
- [x] 3. 落地 README 修改 → §2 成功标准全过（版本号/日期 ✓、文档路径 5/5 ✓、抽查清单全中 ✓、路线图勾选与代码一致 ✓）
