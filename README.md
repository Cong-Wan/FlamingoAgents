# FlamingoAgents

> Author: wilbur
> Version: 1.2
> Date: 2026-08-17
> Description: 项目自述——开发初心、现状能力、架构、快速开始与路线图。v1.1 现状能力与目录结构补 skill。
> v1.2 补全漏写的已落地能力：askSubAgent 子代理、Skill 系统（技能页编辑 + /skill: chip）、工具停止、多窗口流式、文件树与文件夹@附件、/model 切换、状态栏、models.json 导入等。

**一个为多 Agent 协同作业而生的本地 Agent 系统**：分别接入不同 Coding Plan 的模型，让多个 Agent 并发作业，突破单个 Coding Plan 的并发限制。

## 开发初心

FlamingoAgents 的思路：

1. **多 Plan 接入**：把多家 Coding Plan 的端点统一配置进 `config/models.yaml`（OpenAI 兼容协议），每个 provider 独立 key、独立限流池；
2. **多 Agent 并发**：每个 Agent 实例绑定不同的 provider/model，任务拆开并行派发，N 个 plan 就有 N 倍并发；
3. **统一管控**：一个 Web 界面管所有会话、看所有用量、配所有模型。

> 现状：多 provider 接入与按会话绑定模型已实现（地基完成）；多 Agent 编排派发在路线图上（见文末）。

## 现状能力

### `flamingoAgents/` —— 纯库（零 Web 依赖，可独立使用）

- **事件流 Agent**：`runUserMessageStream` / `continueConfirmationStream` 生成器产出 7 种事件（正文/思维链增量、工具起止、确认请求、完成、错误），调用方想怎么渲染就怎么渲染；
- **工具系统**：内置 read/write/edit/bash/**askSubAgent**，schema 驱动 + 正则权限规则（如删除类命令需人工确认），新增工具只需写函数 + factory + 注册（见 `docs/addCallableToolFunction.md`）；bash/askSubAgent 执行**可中断**（`_runWithInterrupt` 分片 poll，中断即 terminate/kill）；
- **子代理 askSubAgent**：以 function call 形式把子任务派给独立子代理会话，可指定 `provider/model`、独立 workDir 与 system prompt，超时透传（默认 600s，上限 3600s），子代理输出经 JSON stdout 回收——多 plan 并发的编排地基；
- **会话持久化与恢复**：jsonl 原子日志，进程重启后自动 resume（含 system prompt 前缀恢复，provider 缓存可命中）；
- **用量统计**：每会话累计 prompt/cached/completion tokens；
- **多 provider 模型配置**：`config/models.yaml` 集中管理多家 Coding Plan，`createAgent(providerId=..., modelId=...)` 按需装配，支持 thinking/reasoningEffort/stream 等模型能力声明。
- **Skill**：加载 `config/skills/<name>/SKILL.md`，新建 agent 时把 name/description/location 注入 system prompt（位于「当前时间」之前）；resume 不重注。Web 技能页支持**按模板结构化编辑保存**（frontmatter name/description/disable + 正文），保存仅影响下次新建会话。**入口文件名必须是大写 `SKILL.md`**（对齐 Agent Skills 规范）；小写 `skill.md` 不会被加载。

### `webApp/` —— Web 程序（单用户、局域网）

- **现代化对话界面**：流式逐字输出、思维链折叠、工具调用卡片（结果可折叠内滚动预览）、**工具确认框**（批准/拒绝续跑）；
- **随时停止**：停止按钮 fire-and-forget 即时 abort，工具执行一并中断，多窗口间停止状态静默同步；
- **多窗口并行流式**：同会话多标签页 attach 回放式重连，互不抢流；
- **文件树与 @ 附件**：侧栏文件树浏览/读文件；输入框 `@` 唤起文件面板，目录可下钻也可整体选为附件（chip 📄/📁 区分，后端递归展开目录为文本块）；
- **斜杠命令**：`/new` 新会话、`/model` 会话内切换模型、`/skill:` 技能 chip（发送时拼技能正文，气泡不显示全文）；
- **状态栏**：当前模型 / 最近一轮增量 tokens / 上下文使用率；
- **会话管理**：每会话绑定独立 workDir（不存在可探测后创建）、历史持久化、重命名/删除；
- **模型配置页**：整页表单直接编辑 `models.yaml`（与 CLI 共用同一份配置，apiKey 脱敏回显），支持粘贴 **models.json 一键转换导入**（纯转换不落盘）；
- **用量统计**：token 卡片 + 时/天/月粒度图表（每模型独立配色）+ 费用估算（按 plan 价格配置）；
- **安全**：静态 Bearer Token 认证、SSE 流式、原生 HTML/CSS/JS 前端（无框架无构建）。

## 架构

```
浏览器（原生 JS）
   │  REST + SSE（Bearer Token）
   ▼
webApp/backend（FastAPI，单 worker）
   │  每会话一个 agent 实例（绑定 workDir + provider/model）
   ▼
flamingoAgents 纯库（事件流 + 工具 + jsonl 日志）
   │
   ▼
各家 Coding Plan 端点（config/models.yaml 统一配置）
```

关键设计：**库与 Web 完全解耦**——`flamingoAgents` 不知道 Web 的存在，CLI（`askModel.py`）和 Web 共用同一个库。

## 快速开始

环境：Python ≥ 3.12 + [uv](https://docs.astral.sh/uv/)

```bash
# 1. 配置模型（复制示例，填入各家 Coding Plan 的 key）
cp config/models.example.yaml config/models.yaml

# 2. CLI 方式跑一轮对话
uv run python askModel.py

# 3. Web 方式（局域网访问）
FLAMINGO_WEB_TOKEN=你的token uv run python -m webApp
# 浏览器打开 http://<本机IP>:8787，输入 token 登录
```

## 目录结构

```
flamingoAgents/      # 纯库：core（事件流 Agent）/ models（适配器）/ tools（工具系统）
webApp/
  backend/           # FastAPI：SSE 桥接、会话索引、用量 SQLite、模型配置读写
  frontend/          # 原生 HTML/CSS/JS（vendor: marked + DOMPurify + Chart.js）
config/              # models.yaml（多 provider 密钥配置）/ tools.yaml / systemPrompt.md / skills/<name>/SKILL.md
docs/                # 契约与手册（见下）；方案与事故报告归入 docs/plan/
webData/             # 运行数据（gitignore）：会话索引、集中 jsonl 日志、usage.db
```

## 文档索引

| 文档 | 内容 |
|---|---|
| `docs/webApiSpec.md` | 前后端接口契约（REST + SSE 逐字段定义） |
| `docs/addCallableToolFunction.md` | 新增工具函数手册 |
| `docs/toolCallInterruptionIncidentReport.md` | 工具中断事故复盘 |
| `docs/plan/webAppPlan.md` | Web 程序总体方案（含迭代一：探建分离/侧栏/配置页/图表） |
| `docs/plan/streamOutputPlan.md` | 事件流架构设计（7 事件模型的由来） |
| `docs/plan/` | 全部方案/复盘文档目录（20 余份，自解释） |

## 路线图（对齐初心）

- [x] 多 provider 模型接入（models.yaml 统一配置）
- [x] 事件流 Agent 纯库 + 工具确认机制
- [x] Web 对话程序（按会话绑定模型与 workDir）
- [x] 子代理工具 askSubAgent（function call 形式，可指定 provider/model，超时透传，可中断）
- [ ] **多 Agent 编排**：profile 化管理多套 prompt/工具/模型组合
- [ ] **并发作业派发**：一个任务拆给多个不同 plan 的 Agent 并行执行，汇总结果
- [ ] 跨 plan 负载均衡与失败转移

## License

MIT
