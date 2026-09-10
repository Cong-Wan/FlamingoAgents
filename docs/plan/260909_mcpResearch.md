# MCP 接入调研、证据与审核记录

> Author: wilbur  
> Version: 1.1  
> Date: 2026-09-09  
> Description: MCP 调研执行计划、实际代码与官方资料索引、验证和独立方案审核记录。本次仅文档，不实施业务功能。

配套：[方案](260909_mcpPlan.md) · [TODO](260909_mcpTasks.md)

## 调研范围与假设

- 用户需要当前 FlamingoAgents agent 架构如何接入外部 MCP 服务的详细方案，包括前后端配置；按 **Flamingo 作为 MCP host/client** 设计，不擅自改为对外提供 MCP server。
- 当前单用户、单机、FastAPI 单 worker、纯 Python agent 库与原生无构建前端保持不变；本项目已有 uv 与 Python >=3.12，不创建/升级 Python 环境。
- 推荐同时覆盖 stdio 与 Streamable HTTP；OAuth、resources/prompts、子代理继承列为分期/待确认。
- 不启动第三方 MCP 服务，不提交/推送 Git，不修改私人配置和密钥，不覆盖既存 workflow 方案文件。

## 详细调研 TODO

- [x] 确认工作目录、仓库状态、项目入口、配置迁移现状。
- [x] 阅读 builder / agent / registry / runtime / policy / core types，核对同步与确认机制。
- [x] 阅读 Web agentManager / server / sessionStore / SSE 与配置读写链路。
- [x] 阅读原生 JS 路由、设置页、API 与状态管理。
- [x] 核对 SDK/CLI/askSubAgent 的配置来源、权限与资源生命周期。
- [x] 从当前会话索引核实实际 provider/model 为 openaiCodex/gpt-6-astra；独立审核沿用该配置。
- [x] 访问 MCP 官方规范索引和 Python SDK 最新发布：规范 2026-07-28，SDK 2.2.0。
- [x] 深读新旧版本兼容、SDK 实际 API、transport/cancellation、schemas/content、安全与授权。
- [x] 验证当前工具参数校验边界与现有测试基线；隔离安装 mcp 2.2.0 做进程内 Client 探测。不调用真实模型/外部 MCP 服务。
- [x] 落档架构、配置、密钥、API、UI、缓存/确认/停止/恢复和分期边界。
- [x] 落档文件级 TODO、依赖顺序、测试矩阵与验收标准。
- [x] 创建全新子代理审核方案（只读）；修复全部明显问题后以全新子代理复审，直到无明显问题。
- [x] 校验文档链接、版本/状态及 git diff，向用户总结方案和待确认决策。

## 当前代码证据（调研截面）

- `flamingoAgents/builder.py`：createAgent 只装配内置工具，toolNames 只认内置名；调用 ensureUserConfig。
- `flamingoAgents/core/agent.py`：runUserMessageStream/continueConfirmationStream 是同步生成器；driveToolBatch 批量 start 后串行执行；modelTools 每模型 step 从 registry 构建；pending 为内存态。
- `flamingoAgents/tools/toolDefinition.py`：同步 execute(arguments, context) → toolOutput；目前无动态权限 callback、来源元数据和生命周期管理。
- `flamingoAgents/tools/toolRuntime.py`：轻量 schema 校验不是完整 JSON Schema；异常字符串会成为模型可见 toolResult。
- `flamingoAgents/core/types.py`：toolOutput/content 为 str；toolContext 有 workDir、interruptEvent，没有 sessionId；images 目前为 user 输入。
- `flamingoAgents/utils/configPaths.py`：真实运行配置在 ~/.flamingo/config，仓库 config/ 是模板；不能回退到项目默认扫描。
- `webApp/backend/agentManager.py`：managerLock 内 createAgent；缓存失效/删除未 close；activeStreams/stopped 不等于执行线程实际退出。
- `webApp/backend/server.py`：所有 /api 除登录外 Bearer 鉴权；现有错误封套为 {error: string}；当前无 FastAPI lifespan。
- `webApp/backend/modelConfigStore.py`：可借鉴配置表单/脱敏/原子写；固定 `.yaml.tmp` 不能直接照抄。
- `webApp/backend/sessionStore.py`：会话索引暂无工具/MCP 选择字段。
- `webApp/frontend/js/main.js` / `api.js` / `settingsView.js`：hash 路由、IIFE 原生 JS、Bearer fetch；无 package.json/构建框架。
- `flamingoAgents/models/credentialStore.py`：仅接受 openai-codex/xai；可借鉴安全写入，不可直接塞 MCP serverId。
- `flamingoAgents/tools/builtinTools.py` / `sdkEntry.py`：子代理独立进程、model 必填、tools 为空则无工具、SDK 自动拒绝待确认；不得默认获得全局 MCP。
- `flamingoAgents/tools/toolPolicy.py`：按 arguments[field] 的 regex；缺失字段为 `''`，`.*` 可实现 always 确认。
- `askModel.py`：已有确认 input 循环；应在 main 持有 runtime。
- 当前 Web 会话（本任务）：`openaiCodex` / `gpt-6-astra`，workDir 为本仓库。

## 官方资料（2026-09-09 实际访问）

1. 索引：https://modelcontextprotocol.io/llms.txt
2. 当前规范：https://modelcontextprotocol.io/specification/2026-07-28/index.md
3. 版本兼容：https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning.md
4. transports：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/index.md
5. Streamable HTTP：https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http.md
6. 取消：https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/cancellation.md
7. tools：https://modelcontextprotocol.io/specification/2026-07-28/server/tools.md
8. 授权：https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization/index.md
9. 安全实践：https://modelcontextprotocol.io/docs/2026-07-28/tutorials/security/security_best_practices.md
10. Python SDK README：https://github.com/modelcontextprotocol/python-sdk/blob/v2.2.0/README.md
11. SDK 文档：https://py.sdk.modelcontextprotocol.io/llms.txt 及 client/transports/callbacks/subscriptions/pagination
12. 源码：`src/mcp/client/client.py`、`session.py`、`stdio.py`（tag v2.2.0）
13. PyPI：mcp 2.2.0，requires_python >=3.10，依赖 httpx2/jsonschema/anyio/pydantic/mcp-types==2.2.0

要点：

- 2026-07-28 无 initialize 握手；HTTP 无协议 session、无 GET 长流；服务端输入走 MRTR。
- SDK Client 是 async context manager；stdio 环境 allow-list；HTTP 头必须放在调用方的 httpx2.AsyncClient。
- 注册 elicitation_callback 会使 `call_tool` 自动完成多轮；v1 禁止注册。
- `list_tools` 丢弃非法 x-mcp-header；outputSchema 用 jsonschema 校验。
- stdio 关闭：关 stdin、grace、杀进程树；stdout 按行解析。
- 授权规范是 OAuth 2.1 + PRM；stdio 明确不应走该规范、改从环境取凭据。

## 验证

命令与结果（均未打真实模型、未连外部 MCP）：

1. `uv run pytest -q` → 209 passed in 4.48s。
2. `validateArguments` 对 `{type:boolean}`、`enum`、`anyOf`、`$ref` 均返回 `''`（放行）。
3. `uv run --isolated --no-project --with mcp==2.2.0 python` 进程内探测：
   - mcpVersion=2.2.0
   - `Client` 签名含 `mode='auto'`、`read_timeout_seconds`、`elicitation_callback`、`input_required_max_rounds=10`
   - `streamable_http_client(url, http_client=, terminate_on_close=True)`
   - `StdioServerParameters` 字段：command/args/env/cwd/encoding/encoding_error_handler
   - mode=auto → protocol 2026-07-28；mode=legacy → 2025-11-25
   - `call_tool('sumNumbers', {left:2,right:3})` → content text `'5'`，structuredContent `{result:5}`，isError false
4. 本机 Python：3.13.13。仓库 git `f835765`，方案文档当时为未跟踪文件。
5. `httpx2.AsyncClient` 已在本项目环境中（dev 依赖）；mcp 2.2.0 将其列为正式依赖。

限制：未跑 stdio 真子进程、未跑 Streamable HTTP 端口、未验证 FastAPI 与 mcp 的 starlette 版本冲突（实施 T0 依赖决议时必须跑全量测试）。

## 审核

三轮独立只读审核记录见 `260909_mcpReview.md`。第三轮结论：无明显阻塞问题。方案现行版本 1.3。
