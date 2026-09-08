# FlamingoAgents

<!--
Author: wilbur
Version: 1.1
Date: 2026-09-07
Description: Documents centralized session storage under ~/.flamingo and explicit recovery of missing session indexes from existing history. v1.1：Web @ 改为仅路径引用，不预读文件内容。
-->

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
- **文件树与 @ 路径引用**：侧栏文件树浏览/读文件；输入框 `@` 唤起文件面板，目录可下钻也可整体选为引用（chip 📄/📁 区分）。发送时只把校验后的绝对路径交给模型，不预读内容、不解包；读取由 Agent 按需使用工具完成。能 `@` 不等于能在预览里打开该文件；
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

> **已有旧数据必读（时序约束）**：session jsonl 与 `usage.db` 已迁到 `~/.flamingo/logs/`。启用新代码的 webApp/CLI **之前**，先跑一次性迁移脚本，否则旧历史不会被自动搬迁（webApp 看空历史、CLI 旧日志被冻结）。脚本幂等，可重复跑。
>
> ```bash
> uv run python -m flamingoAgents.utils.logMigration
> ```
>
> 全新环境无旧数据也可跑（空转后写 `~/.flamingo/logs/.migrationDone`）。迁完后 `webData/sessionLogs/` 与各 workDir 的 `.agentLogs/` 残留由你手动删。
>
> **会话索引也统一存入家目录**：Web 运行时读写 `~/.flamingo/logs/webData/sessions.json`，不再向仓库 `webData/` 写数据。新索引不存在时，首次读取会校验并复制尚存的仓库 `webData/sessions.json`，旧文件保留；新索引存在（包括空列表）时始终以新索引为准，不自动合并旧数据。确认新位置的索引和历史均可读取后，才清理旧目录。已删除的旧索引不会仅凭 JSONL 自动重建；损坏/不可读的索引会报错，不当空索引覆盖。升级后需重启 Web 服务，同一用户家目录只运行一个 Web 服务实例。

```bash
# 1. 配置模型（复制示例，填入各家 Coding Plan 的 key）
cp config/models.example.yaml config/models.yaml

# 2. CLI 方式跑一轮对话
uv run python askModel.py

# 3. Web 方式（局域网访问）
FLAMINGO_WEB_TOKEN=你的token uv run python -m webApp
# 浏览器打开 http://<本机IP>:8787，输入 token 登录
```

## 索引丢失后的显式历史恢复

如果 `sessions.json` 已删除但家目录日志仍在，改路径不会自动让旧历史出现。先停止会话写入、备份日志，并通过 SQLite backup API 获取一致的 `usage.db` 副本，再运行恢复工具（`--work-dir` 可重复；必须填写真实目录，不能把日志文件夹名中的 `-` 反推成 `/`）：

```bash
uv run python -m webApp.backend.sessionRecovery \
  --logs-root /path/to/backup/logs \
  --work-dir /absolute/work/dir \
  --output ~/.flamingo/logs/webData/sessions.json
```

工具严格校验日志、只读数据库，生成恢复索引并原子发布；目标索引已存在则报错，不覆盖或合并。恢复标题取首条消息前 20 字，模型取最后记账记录；原自定义标题、未发送消息的空会话、未发请求的模型切换不能保证还原。历史读取同时支持 JSONL、旧 JSON 事件数组，以及数组后追加的 JSONL，不会为兼容而重写正文。升级读取兼容代码后需重启 Web 服务。

## ChatGPT / xAI 订阅登录

FlamingoAgents 原生支持 ChatGPT Plus/Pro 的 Codex Responses 与 SuperGrok/X Premium 的 xAI Responses，不依赖 pi/Node 运行时。CLI 登录与模型配置相互独立；可登录后复制 `config/models.example.yaml` 的订阅 Provider。Web 用户无需预先创建 Provider，可直接在模型设置页顶部登录并生成模型配置候选：

```bash
# ChatGPT：本机浏览器 PKCE（远程环境可粘贴回调 URL/code）
uv run python modelLogin.py login openai-codex --method browser

# ChatGPT：无浏览器/远程环境设备码
uv run python modelLogin.py login openai-codex --method device-code

# xAI：SuperGrok / X Premium 设备码
uv run python modelLogin.py login xai

# 脱敏状态与退出
uv run python modelLogin.py status
uv run python modelLogin.py logout xai
```

Web 模型设置页顶部始终显示独立的“订阅账户”。ChatGPT 登录后只读请求固定的 `https://chatgpt.com/backend-api/codex/models?client_version=0.153.4`，同步当前账户中官方标记为可见的 Codex 模型（包括 GPT-6）；隐藏或缺少必要元数据的模型会带原因跳过。xAI 登录后只读请求固定的 `https://api.x.ai/v1/models`，并把实时 ID 与内置 Responses 元数据取交集。候选只加入浏览器编辑区，用户点击“保存”后才写 `models.yaml`；目录结果不保证每次调用一定成功。

OAuth 凭据仅写入 `~/.flamingo/auth.json`（目录 0700、文件 0600），不会进入 `models.yaml`、浏览器响应或会话 JSONL；Access Token 到期前自动刷新。ChatGPT 与 xAI 模型发现均遵循标准 `HTTPS_PROXY/NO_PROXY`、禁止全部 HTTP 重定向、限制响应大小；401 只进行一次带 stale-token 并发保护的刷新重试。

Responses 会把多轮继续所需的加密 reasoning/item ID 以白名单字段写入会话 JSONL；它们不是 Access/Refresh Token，但会话日志仍应按敏感数据保护。

## 目录结构

```
flamingoAgents/      # 纯库：core（事件流 Agent）/ models（适配器）/ tools（工具系统）
webApp/
  backend/           # FastAPI：SSE 桥接、会话索引、用量 SQLite、模型配置读写
  frontend/          # 原生 HTML/CSS/JS（vendor: marked + DOMPurify + Chart.js）
config/              # models.yaml（多 provider 密钥配置）/ tools.yaml / systemPrompt.md / skills/<name>/SKILL.md
docs/                # 契约与手册（见下）；方案与事故报告归入 docs/plan/
~/.flamingo/logs/    # 全部会话运行数据（不依赖仓库 webData/）
  usage.db           # 用量统计
  webData/           # sessions.json 索引 + <workDir映射目录>/*.jsonl 正文
  cliData/           # <workDir映射目录>/*.jsonl
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
