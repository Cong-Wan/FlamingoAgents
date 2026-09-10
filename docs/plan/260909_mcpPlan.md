# FlamingoAgents 接入 MCP 服务详细方案

> Author: wilbur  
> Version: 1.3  
> Date: 2026-09-09  
> Description: 在不重写 Agent 内核的前提下，把 Flamingo 做成 MCP host/client。v1.3 第二轮复审修订 + 第三轮通过后吸收非阻塞注记（门闩 finally、load/PUT 分层、路由层 409）。本次只出方案，不实施代码。  
> 配套：[实施 TODO 与测试](260909_mcpTasks.md) · [调研证据](260909_mcpResearch.md)

## 0. 结论

**推荐：保留当前 Agent / 工具注册 / 确认 / SSE 事件模型，增加一层 MCP runtime，把外部 MCP tools 适配成现有 `toolDefinition`。** 模型仍然只看见普通 function call；程序负责连接、命名、权限、超时、取消和密钥。

不要做这些事：

- 不要把 Flamingo 改成对外 MCP server。
- 不要用 LangGraph / 官方 Agents SDK 替换现有循环。
- 不要把 MCP 配进 `tools.yaml` 或 `models.yaml`。
- 不要让每个 Web 会话各拉一套 stdio 进程。
- 不要把 MCP Python SDK 的 async `Client` 直接塞进同步 `execute()`。
- 不要在 v1 做 OAuth、resources/prompts 注入、elicitation 表单、sampling、MCP Apps。
- 不要把现有 `askSubAgent` 默认带上全部 MCP 工具。

一期范围：**stdio + Streamable HTTP、只暴露 tools、静态 token/环境变量鉴权、进程级连接共享、Web 设置页。**

## 1. 明确假设与待确认项

### 1.1 按现有项目约束给出的默认值

| 项 | 默认 | 理由 |
|---|---|---|
| 角色 | Flamingo = MCP host/client | 用户要“加入 MCP 的服务”，是消费外部 server，不是对外提供 |
| 协议 | 官方 SDK `mcp>=2.2.0,<3`，默认 `mode=auto` | 2026-09-07 的 v2.2.0 同时覆盖 2026-07-28 与旧 initialize 时代 |
| 传输 | stdio + Streamable HTTP | 规范两种标准传输；旧 HTTP+SSE 不主动建设，仅当 SDK 对 dual-era 服务自动回退时顺带可用 |
| 能力 | 只把 **tools** 交给模型 | resources/prompts 有 prompt injection 与上下文膨胀风险 |
| 连接作用域 | **进程级 runtime**，所有 Web 会话共享 | 避免 N 个会话 = N 个 `npx` 进程 |
| 会话选择 | v1 所有启用的 server 对所有会话生效 | 现有 `sessions.json` 无工具字段；避免顺手改会话索引 |
| 子代理 | `sdkEntry` / `askSubAgent` **默认 0 个 MCP 工具** | 子进程会重新 `createAgent`，且 SDK 自动拒绝确认 |
| 鉴权 | stdio 用 `env`；HTTP 用 headers / `$ENV` | v1 不做 OAuth 2.1 / DCR / CIMD |
| 确认 | 默认 `approvalMode=always` | MCP 工具注解不可信；现有确认框可复用 |
| 配置文件 | `~/.flamingo/config/mcp.yaml` | 与 models/tools/skills 并列；仓库 `config/` 只放模板 |
| 空配置 | 首次 `ensureUserConfig()` 拷贝空模板，已存在不覆盖 | 不像 `models.yaml` 那样缺文件即失败；无密钥也可空跑 |
| Python / uv | 不新建环境、不升级 Python | 当前 3.13.13、`requires-python>=3.12`，SDK 要求 `>=3.10` |
| 平台 | 单机 POSIX，单用户，Web 单 worker | 不承诺 Windows 进程组 |
| 依赖 | 主依赖增加 `mcp>=2.2.0,<3`（不要 `mcp[cli]`） | 用官方 client，不自写 JSON-RPC |

以上默认值**待你确认后实施**。若你希望“只接 HTTP、不要 stdio”或“默认免确认”，应在开工前改表，而不是实现时再猜。

### 1.2 三种容易混掉的对象

| 对象 | 职责 | 不负责 |
|---|---|---|
| Agent | 模型循环、会话锁、确认、事件流 | 拉起 MCP 进程、OAuth、跨会话连接池 |
| MCP runtime | 读配置、建连、list/call、取消、关闭 | 改 system prompt、写 sessions.json |
| MCP server | 外部进程或 URL | Flamingo 内部 read/write/edit/bash |

Skill 仍然只是 prompt 片段。MCP 不是 skill，也不走 `SKILL.md`。

## 2. 当前 Agent 结构（接入必须面对的事实）

调研截面 2026-09-09，证据见 `260909_mcpResearch.md`。行号会变，以下按模块。

```text
浏览器  --REST/SSE Bearer-->  webApp/backend (FastAPI 单 worker)
                                  │ 每会话一个 agent 缓存
                                  ▼
askModel.py / sdkEntry.py  -->  createAgent()
                                  │
                                  ▼
                    agent + toolRegistry + 同步 driveToolBatch
                                  │
                                  ▼
              builtin read/write/edit/bash/askSubAgent
```

关键不变量：

1. **工具必须是同步 `toolDefinition.execute(arguments, context) -> toolOutput`。** MCP SDK `Client` 是 `async with`，离开即断开且不能复用。
2. **`driveToolBatch` 先批量 `toolCallStart`，再串行 execute。** 不并行调工具。MCP 调用按这个串行模型即可，不要在 v1 做并行 tools/call。
3. **每轮模型请求从 registry 现取 schema。** 进程内刷新工具列表后，下一轮 `driveModelLoop` 自动看到新工具；不必改模型适配器。
4. **待确认是内存态。** 进程重启后 pending 丢失。MCP 确认走同一套，不在 v1 持久化 MCP `request_state`。
5. **`toolOutput.content` 只是字符串。** image/audio/resource 必须先文本化，不能把 bytes 塞进现有 jsonl。
6. **`toolRuntime.validateArguments` 不是完整 JSON Schema。** 实测 `boolean`/`enum`/`anyOf`/`$ref` 全部放行。MCP 工具**禁止**再走这套校验。
7. **`createAgent` 只装内置工具；`toolNames` 只识别内置名。** MCP 要用新参数，不能塞进 `toolNames`。
8. **Web `agentCache` 丢弃实例时不 close。** MCP 连接不能挂在单个 agent 上随缓存淘汰泄漏进程。
9. **`sdkEntry` 无工具则空列表，确认一律拒绝。** 子代理默认禁止 MCP。
10. **配置源已经切到 `~/.flamingo/config/`。** 新模块必须用 `configPaths`，禁止回退扫描仓库 `config/`。

本地验证（无真实模型、无外部 MCP）：

- `uv run pytest -q` → **209 passed / 4.48s**。
- `validateArguments` 对非 object 叶子类型返回空字符串（放行）。
- 隔离安装 `mcp==2.2.0`：`Client(MCPServer, mode=auto)` 协商 `2026-07-28`；`mode=legacy` 协商 `2025-11-25`；`call_tool` 返回 `content + structuredContent + isError`。

## 3. MCP 规范与 SDK 取舍（2026-09-09 实读）

当前规范是 **2026-07-28**。它与 2025-11-25 及更早版本不是“多一个 header”的差别：

| | 2026-07-28 现代 | 2025-11-25 及更早 |
|---|---|---|
| 版本协商 | 每请求 `_meta`，无 initialize | `initialize` 握手 |
| HTTP | 仅 POST；无 GET 长流、无协议 session | POST + 可选 GET；可有 `MCP-Session-Id` |
| 取消 | HTTP 关 SSE 流；stdio 发 `notifications/cancelled` | 通知取消 + 会话语义 |
| 服务端要输入 | `InputRequiredResult` 内嵌，客户端重试原方法 | 服务端可发独立 JSON-RPC request |
| 工具变更 | `subscriptions/listen` | `notifications/tools/list_changed` 等 |

Python SDK **v2.2.0**（PyPI `mcp`，Python >=3.10）是官方 client。实测/文档要点：

- `Client(url)` = Streamable HTTP；`Client(StdioServerParameters)` = 子进程；`Client(MCPServer)` = 进程内（测试用）。
- `async with` 才连接；退出即断开，对象不可复用。
- stdio **不继承父进程环境**，只带 POSIX allow-list，密钥必须显式 `env=`。
- HTTP 自定义头/超时/代理必须自己建 `httpx2.AsyncClient` 传给 `streamable_http_client`；没有 `headers=` 快捷参数。
- 跨 origin 的 HTTP 重定向不跟随。
- `list_tools` 会丢掉非法 `x-mcp-header` 的工具。
- 若注册 `elicitation_callback`，`call_tool` 会自动帮你完成多轮输入。v1 **禁止注册**这类 callback，避免在无 UI 时自动应答或卡死。
- 已知缺口：tasks extension、DPoP、jwt-bearer。v1 不依赖它们。

**结论：用 SDK，不要手写 transport。** 本项目要自建的是：配置、命名、权限、同步桥、生命周期、Web、密钥，以及“不把 MCP 结果/密钥写进不该写的地方”。

## 4. 推荐架构

```text
~/.flamingo/config/mcp.yaml          用户配置（无/少密钥）
~/.flamingo/mcp.secrets.json         可选：HTTP token / stdio env 明文，0600
        │
        ▼
mcpConfig.load() ──► mcpRuntime  (进程内 1 个)
                      │  专用 asyncio 线程
                      │  serverId → 连接（懒建、可重连）
                      │  list_tools 缓存
                      ▼
              mcpTools.toDefinitions()
                      │  普通 toolDefinition
                      ▼
         createAgent(..., mcpRuntime, mcpServerIds)
                      │
         agent.toolRegistry = 内置 ∪ MCP
                      │
         driveToolBatch / 现有确认框 / 现有 SSE 卡片
```

Web 与 CLI 都是 host：

- Web：`agentManager` 持有唯一 runtime；`getAgent()` 把 runtime 传进 `createAgent`。
- CLI：`askModel.main()` 建 runtime，结束 `close()`。
- 子代理：不传 runtime 或 `mcpServerIds=[]`。

库继续零 Web 依赖。`flamingoAgents` 不 import `webApp`，也不对自己的 HTTP 端口发 MCP 请求。

### 4.1 模块划分（建议新建，不塞进 builtinTools.py）

| 文件 | 职责 |
|---|---|
| `flamingoAgents/utils/configPaths.py` | 增加 `userMcpPath`；`ensureUserConfig` 拷贝空模板 |
| `flamingoAgents/mcp/mcpConfig.py` | 解析/校验 `mcp.yaml` |
| `flamingoAgents/mcp/mcpSecrets.py` | 0600 密钥文件，serverId 白名单，禁止复用 `credentialStore` 的 openai-codex/xai 限制 |
| `flamingoAgents/mcp/__init__.py` | 导出 `createMcpRuntime` / 设置 dataclass |
| `flamingoAgents/mcp/mcpRuntime.py` | 线程+事件循环、长驻 Client、内存快照、list/call/cancel/close |
| `flamingoAgents/mcp/mcpTools.py` | MCP Tool → `toolDefinition`（名字、schema、preview、permissions、execute 闭包） |
| `flamingoAgents/mcp/mcpContent.py` | `CallToolResult` → `toolOutput` 文本 |
| `flamingoAgents/mcp/mcpNames.py` | serverId 校验、工具名消毒、冲突 |
| `webApp/backend/mcpConfigStore.py` | GET/PUT 脱敏、合并 `__KEEP__`、原子写 |
| `webApp/frontend/js/mcpView.js` | `#/settings/mcp` |
| `config/mcp.yaml` | 空模板 `version: 1 / servers: []` |
| `config/mcp.example.yaml` | 带注释的 stdio/HTTP 示例（不含真实密钥） |

不改 `core/agent.py` 的循环结构。允许的内核级小改动只有：`createAgent` 装配入口、必要时 `toolRuntime` 对 MCP 工具跳过本地 schema 校验（见 6.4）。

## 5. 配置设计

### 5.1 文件与权限

| 路径 | 作用 | 初始化 |
|---|---|---|
| `~/.flamingo/config/mcp.yaml` | 服务器列表、传输、非密钥字段 | 缺失则从项目模板拷贝；已存在不覆盖 |
| `~/.flamingo/mcp.secrets.json` | `{ "servers": { "<id>": { "env": {}, "headers": {} } } }` | 不自动创建；第一次写入时 `0700` 目录 + `0600` 文件 |
| `config/mcp.yaml` | 仓库模板 | 空列表 |
| `config/mcp.example.yaml` | 人读示例 | 不自动拷贝 |

密钥**优先** `$ENV` 引用（与 `models.yaml` 相同：`$` 开头原样保存、Web 回显原样）。明文只允许进 `mcp.secrets.json`，**禁止**把 token 写入会话 JSONL、SSE、GET `/api/mcp` 的非脱敏字段、调试日志。

`credentialStore.py` 写死只有 `openai-codex`/`xai`，不能拿来存 MCP。

### 5.2 `mcp.yaml` schema（version 1）

```yaml
version: 1
servers:
  - id: github          # 必填，^[A-Za-z0-9_][A-Za-z0-9_-]{0,31}$，配置内唯一
    enabled: true
    transport: stdio    # stdio | http
    command: npx        # stdio 必填；禁止经过 shell
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_TOKEN: $GITHUB_TOKEN
    cwd: null           # 若设则必须是绝对路径且存在的目录
    url: null           # http 必填
    headers: {}         # 仅 http；值可为 $ENV
    timeoutSeconds: 60  # 1..300，默认 60
    maxOutputChars: 20000  # 1..200000，默认 20000
    toolPrefix: github  # 默认等于 id；用于模型看见的名字
    approvalMode: always  # always | never | annotations
    trusted: false      # 仅当 true 时 annotations 才可作为降确认的依据
```

校验分层：**PUT 整份非法 → 400 不写盘**；**磁盘 load / GET / 进程启动按条隔离**（坏 server 变 `configError`，其余照常，不得让 lifespan/CLI 因手改坏 yaml 起不来）：

1. `version` 必须是 `1`。
2. `id`/`toolPrefix` 符合正则；`toolPrefix` 在已启用 server 间唯一。
3. `transport=stdio`：`command` 非空字符串，`args` 为字符串数组（缺省 `[]`），禁止 `command` 含空格冒充 shell（空格必须拆进 `args`）。`url`/`headers` 必须为空。
4. `transport=http`：`url` 为 `https://...`，或 host 为 `127.0.0.1`/`localhost`/`::1` 的 `http://`。禁止 `file:`、`unix:`、用户名密码写在 URL 里。`command`/`args`/`cwd` 必须为空。
5. `env`/`headers` 键值都是字符串；密钥值不出现在 debug 字符串里。
6. 启用 server 上限 **16**；每个 list_tools 最多纳入 **64** 个工具；进程内 MCP 工具总数上限 **128**。超过则拒绝该 server 并记原因。
7. yaml 层：`toolPrefix` 不得等于内置工具名。消毒后 flamingoName 冲突（如 `foo.bar` 与 `foo_bar`）在 snapshot 阶段**跳过后来者**，记 `skippedTools`；必要时该 server `status=error`。**禁止**因 MCP 撞名让 `createAgent`/`getAgent` 抛错（否则持锁失败，内置工具也起不来）。

### 5.3 模型看见的工具名

MCP 工具名允许点号，OpenAI-style function name 通常更窄。统一：

```text
flamingoName = "{toolPrefix}__{sanitize(originalName)}"
sanitize: 只留 [A-Za-z0-9_-]，其余变 _，连续 _ 压缩，最长 64（超长则失败并跳过该工具，而不是静默截断造成冲突）
```

例子：`github__list_issues`。`toolCallStart.preview` 与卡片标题用 flamingoName；`details` 里保留 `mcpServerId`、`mcpToolName`，方便排障，但 **details 不得含 URL 查询串里的 token、env、Authorization**。

### 5.4 不放进配置的东西

- 不按会话存 MCP 选择（v1）。
- 不把 `protocolVersion` / `serverInfo` 当作用户可编辑字段（只读状态）。
- 不把 resources/prompts 列表持久化进 yaml。
- 不在 yaml 里写 `mode: legacy` 作为默认；由 SDK `auto` 探测。仅在后续排障需要时再加只读覆盖，v1 不加。

## 6. 库内运行时设计

### 6.1 同步桥

`mcpRuntime` 启动一条守护线程，线程里 **唯一** asyncio loop。Agent 泵线程、FastAPI 工作线程都不得自建 loop。

```text
泵线程（agent.execute / toolContext.interruptEvent）
   runCoroutine(coro, timeout, interruptEvent)
      → asyncio.run_coroutine_threadsafe(同一 MCP loop)
      → 0.1s 切片 wait，与 bash/_runWithInterrupt 同风格
         interrupt 或超时 → 取消**这一次** request（HTTP 关该请求 SSE；stdio 发 notifications/cancelled）
            调用超时：不得升级为杀掉共享 stdio 进程树（否则所有会话一起掉线）
            仅用户停止且 cancel 无响应时，才允许杀该 server 进程；supervisor 必须视为连接丢失并有限次数 backoff 重连
            主动拆 supervisor 的入口只有 close / PUT reload / reconnect
      → 回到 toolOutput；modelInterruptedError 必须原样抛出，不能包成 tool 失败文本
```

禁止每次 `call_tool` `asyncio.run()`。  
禁止在 FastAPI 自己的事件循环上 await MCP。  
同一 `serverId` 上的 `call_tool`/`list_tools` 加 **asyncio.Lock**：两个 Web 会话共享 runtime，不对 SDK Client 的并发安全作假设。

### 6.2 连接生命周期（硬约束）

**每个已启用 server 在 loop 内必须有一个 supervisor：进入 `async with Client(...)` 后一直持有，直到 reload/close 取消。** 禁止用短生命周期 `async with` 去做 `list_tools`（否则每次列举都 spawn/杀 stdio）。HTTP 的 `httpx2.AsyncClient` 与 Client 同进同出。`Client` 退出即废，重连必须新实例。

| 事件 | 行为 |
|---|---|
| 进程启动（Web lifespan / CLI main） | 建 runtime，并对 enabled servers **并行预热**（每 server 独立 timeout，`list_tools` 翻页上限 20）。预热只更新内存快照与 status，失败隔离 |
| `createAgent` / `getAgent` | **只读内存快照**，禁止 spawn、禁止网络、禁止 `runCoroutine`。`getAgent` 今天在 `managerLock` 里调用 `createAgent`，任何阻塞 I/O 都会卡住全部会话 |
| `chat/stream` 路径 | 禁止现场建连。尚未预热成功的 server 本轮没有那些工具；不得让首包 SSE 卡在 `npx` |
| 某 server 失败 | 跳过；内置工具照常；该条 `status=error` + 中文 `lastError` |
| 配置 PUT / reconnect | 见下方「全局忙」；忙则 **409**。空闲才取消旧 supervisor、在 **managerLock 外** warmup 新快照、`invalidateAllAgents` |
| 工具 list 变更 | v1 不订 `subscriptions/listen`。手动 reconnect 或下次进程启动才刷新 |
| Web 关闭 | lifespan：`requestStop` 全部泵 → 等 inFlight==0 或最多 2s → `runtime.close()`。禁止在 FastAPI loop 上 `async with Client` |
| CLI 退出 | `try/finally` close |
| `dropAgent` / 删会话 / 切模型 | **不**关 MCP |

**全局忙 + `mcpReloading` 准入门闩（进程级）：**

只检查当下 pending/流然后放锁再 warmup，会留下窗口：PUT 已放行 → 另一会话 `startStream` → always 很快 pending → PUT 仍 `invalidateAllAgents` → `/chat/confirm` 走 `getAgent` 重建 → `crashRecovered`。因此必须用同一把 `managerLock` 做门闩，而不是看一眼就放行。

```text
with managerLock:
    if 忙 or mcpReloading: 409
    mcpReloading = True
# 锁外、仅 MCP 线程：cancel supervisor + warmup
with managerLock:
    invalidateAllAgents()
    mcpReloading = False
```

忙的定义（文案区分原因）：

1. 任意会话 `hasActiveStream`
2. runtime `inFlight > 0`
3. 任意缓存 agent 对任意会话 `hasPendingConfirmation`
4. `mcpReloading`（对 PUT/reconnect 与 **`startStream` / `chatConfirm`** 都 409）

双保险：`chatConfirm` 若目标会话 stale 且仍有 pending，禁止 `getAgent` 重建，改走 `getCachedAgent`。不要在 pending 存活时 invalidate。

`mcpReloading` 必须 `try/finally` 清闩：warmup 失败也要复位，否则全站一直 409。失败时保持已写盘配置，快照/status=error，不半套 invalidate。

`mcpReloading` 的 409 做在**路由层**（独立文案），不要复用 `startStream() is None`（那是同会话活跃流，chatStream 还有 2s 宽容闸）。

测试用进程内 `MCPServer`：辅助函数 `createMemoryMcpRuntime(server)` 走同一 supervisor 模型；yaml **不**增加 `transport: memory`。单测注入 runtime，不依赖 FastAPI lifespan。Web MCP 路由用同步 `def` 或 `async def` + `to_thread`，一律进 MCP 线程；禁止在 FastAPI loop 上 `async with Client`。

### 6.3 调用路径

```text
模型 function call
  → registry.get(flamingoName)
  → evaluateToolCall（现有确认）
  → execute 闭包
       runtime.callTool(serverId, originalName, arguments, timeout, context.interruptEvent)
       SDK client.call_tool(...)
       mcpContent.render(result, maxOutputChars)
  → toolResult 进 jsonl / SSE
```

`call_tool` 超时用 server 的 `timeoutSeconds`。进度通知 v1 忽略（不改 SSE 事件集）。

若 SDK 返回 `InputRequiredResult` 或因无 callback 抛错：`isError=true`，文案固定为「该 MCP 工具需要额外交互，当前版本不支持」。不要为了让 callback 生效去设 `mode=legacy`。

### 6.4 Schema 与本地校验

- 交给模型的 `parameters` = MCP `inputSchema`。若缺省，用 `{type:object, additionalProperties:false}`。
- **MCP 工具的 `prepareArguments` 为 None；`execute` 前不调用 `validateArguments`。** 实现上二选一，推荐改动更小的 B：
  - A. `toolDefinition` 增加 `skipLocalSchema: bool`（要改 runtime 分支）。
  - B. MCP execute 闭包自己调用 SDK，而 `toolRuntime.executeToolCall` 增加：当 `definition.prepareArguments is _mcpPassthrough` 或 `definition.parameters.get('x-flamingo-skipLocalValidation')` —— 不要用非标 JSON 字段污染发给模型的 schema。
  - **选定 A**：`toolDefinition` 加 `skipLocalSchema: bool = False`，`executeToolCall` 在 True 时跳过 `validateArguments`。这是对内核唯一允许的结构性小改，且默认 False 不影响内置工具。
- 发给模型的 schema 保持 MCP 原样。若某模型 API 因 `$schema`/`$ref` 报错，记为已知风险，v1 不预先改写 schema（避免 silently 改变工具语义）。可在 debug 日志里打出原始 schema 大小。

### 6.5 结果文本化

`mcpContent.render(result) -> toolOutput`：

1. `isError` 直接映射。
2. 按 `content[]` 顺序：
   - `text`：原文；
   - `image`/`audio`：`[image mimeType=... chars=N]`，**不**把 base64 写入 content；
   - `resource_link`：`[resource uri=... name=...]`；
   - 未知 type：`[unsupported content type=...]`。
3. 若有 `structuredContent`：在文本后追加 `--- structured ---\n` + `json.dumps(ensure_ascii=False)`。
4. 总长超过 `maxOutputChars`：截断并声明 `truncated:true`（对齐 bash `maxOutput` 精神）。
5. 空 content 且无 structured：`(empty MCP result)`。
6. 协议错误、超时、取消：取消抛 `modelInterruptedError`；其余 `isError=true` 短中文 + `details.exceptionType`。

JSONL 只存这段文本。禁止把 MCP 原始 bytes、Authorization、stdio env 写入 logger。

### 6.6 确认策略

现有 `evaluateToolCall` 对缺失字段会变成 `''`，而 `.*` 能匹配空串。因此 **always 模式不必改 policy 引擎**：

```text
permissionRule(id='mcpApproval', field='__mcp', action='requireApproval',
               reason='MCP 工具需要用户确认', patterns=[re.compile('.*')])
```

| approvalMode | trusted | 行为 |
|---|---|---|
| always | * | 每次确认 |
| never | * | 不确认 |
| annotations | false | 当作 always（注解不可信） |
| annotations | true | 仅当 MCP `annotations.destructiveHint` 为 true 时确认；读失败则 always |

CLI `askModel.py` 已有 `input()` 确认循环，MCP always 可直接用。`sdkEntry` 仍然自动拒绝——这是子代理默认不挂 MCP 的另一原因。

### 6.7 `createAgent` 合同

新增关键字参数：

```python
mcpRuntime: mcpRuntime | None = None
mcpServerIds: list[str] | None = None  # None=该 runtime 全部 enabled；[]=不要 MCP
```

规则：

- `mcpRuntime is None` → 不装 MCP 工具（保持今天单测/子代理行为）。
- Web/CLI host **必须**显式传入已预热的进程级 runtime。
- `createAgent` 只调用 `runtime.snapshotDefinitions(mcpServerIds)`（纯内存）。禁止在装配路径里 connect。
- 不要在 `createAgent` 里做模块级单例。
- `toolNames` 仍只过滤内置工具；MCP 工具不受 `toolNames` 影响，只受 `mcpServerIds` 影响。
- 装配顺序：内置 → MCP。snapshot 已去冲突；`createAgent` 若仍看到重名，**丢弃 MCP 侧那条并 debug**，不得 `RuntimeError` 让整个会话起不来。

`createMcpRuntime()` 与 `askModel.main` **先** `ensureUserConfig()`；缺 `mcp.yaml` 当空 `servers`，不得启动失败。  
`askModel.py`：ensure → createRuntime → warmup → createAgent，finally close。  
`sdkEntry.py`：不传 runtime。  
`agentManager.getAgent`：传入共享 runtime，锁内只 snapshot。

## 7. 安全边界（v1 必须写进实现，不是附录）

1. stdio 用 argv 数组 spawn，`start_new_session=True`（SDK 已如此），**禁止** `shell=True` 或把整行命令丢给 bash。
2. stdio env = SDK allow-list ∪ 配置里显式 env。**不得**传入 `FLAMINGO_WEB_TOKEN`、`~/.flamingo/auth.json` 内容、`models.yaml` 的 apiKey。
3. HTTP：证书校验开启；禁止自定义 `verify=False` 配置项。
4. HTTP URL 允许列表见 5.2（https 或本机 http；禁 file/unix/URL 内嵌凭据）。「测试连接」走同一校验。任意 https（含内网）在用户已登录前提下仍可访问——本机受信任模型可接受，**不要**写成“设置页不会变成 SSRF 代理”。
5. 重定向只信 SDK 的 same-origin 规则；配置里必须填最终 URL。
6. 工具注解、server instructions、tool description 都当不可信文本。v1 **不**把 server instructions 拼进 system prompt。
7. `x-mcp-header` 非法工具由 SDK 丢弃；不要自己实现一套更松的解析。
8. 用户停止：必须 cancel 在飞的 MCP 请求；stdio 在取消无响应后走 SDK 进程树终止。现有 `requestStop`/`interruptEvent` 是触发源。
9. 配置写入用「同目录唯一临时名 + `os.replace`」。**不要**复制 `modelConfigStore` 那种固定 `.yaml.tmp`（并发两个保存会互踩）。
10. 本方案面向本机受信任用户。不承诺防御恶意 MCP server 通过工具结果诱导模型破坏本机；`approvalMode=always` 是缓解不是沙箱。

## 8. 前后端配置与 API

### 8.1 现状可复用的模式

- 侧栏 + hash 路由：`#/settings/models`、`#/settings/skills`。新增 `#/settings/mcp`。
- `api.js` Bearer JSON；401 回登录门。
- 设置页：工作副本、dirty hint、保存/重置、`__KEEP__` 脱敏。
- 错误封套：`{ "error": "中文" }`，库 `RuntimeError` → 400。
- 单 worker；agent 缓存惰性重建。

### 8.2 后端

新文件 `webApp/backend/mcpConfigStore.py` + 在 `server.py` 加路由；`agentManager` 增加 `setMcpRuntime/getMcpRuntime`。

必须补 **FastAPI lifespan**（当前 `__main__` 无 shutdown 钩子）：启动 `ensureUserConfig` 之后创建 runtime 注入 manager；停止时 close。不要把 runtime 建在 `server.py` 模块级（pytest 收集会有副作用，`__main__.py` 对 `ensureUserConfig` 已有同样约束）。

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/mcp` | 读配置 + 每条脱敏 + `status`/`configError`/`lastError`/`toolCount`；`toolCount` 仅已连接时为整数，否则 `null`；不现场 list_tools、不 spawn |
| PUT | `/api/mcp` | 校验、合并 `__KEEP__` 与 secrets、原子写；全局忙 409；否则锁外 reload + `invalidateAllAgents` |
| POST | `/api/mcp/test` | 对**请求体里的一条**（可未保存）在 MCP 线程用**短生命周期** Client 试连；不写盘、不替换 supervisor；`finally` 关掉试连进程 |
| POST | `/api/mcp/reconnect` | 已保存配置强制重连；全局忙 409 |

PUT 请求体：

```json
{
  "servers": [
    {
      "id": "github",
      "enabled": true,
      "transport": "stdio",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_TOKEN": "__KEEP__" },
      "cwd": null,
      "url": null,
      "headers": {},
      "timeoutSeconds": 60,
      "maxOutputChars": 20000,
      "toolPrefix": "github",
      "approvalMode": "always",
      "trusted": false
    }
  ]
}
```

GET 对明文密钥回 `__KEEP__`，`$ENV` 原样。`status ∈ idle|connected|error|disabled`（`idle`=尚未预热或未启用连接，不是现场去连）。

POST `/api/mcp/test` 成功响应（无密钥）：

```json
{
  "ok": true,
  "protocolVersion": "2026-07-28",
  "serverInfo": { "name": "...", "version": "..." },
  "tools": [
    { "name": "list_issues", "flamingoName": "github__list_issues", "description": "..." }
  ],
  "skippedTools": [{ "name": "bad.name", "reason": "flamingoName 超长" }]
}
```

失败：HTTP 400 `{ "error": "..." }`。试连必须有超时上限，不得在事件循环上挂死。

**Admission：** PUT/reconnect/`startStream`/`chatConfirm` 都看 §6.2 门闩。MCP 保存期间禁止新开对话或确认。设置页写明：任一对话进行中、等待确认、或正在重载 MCP 时都不能保存/发送。

删除会话、切模型：不关 MCP runtime。

### 8.3 前端

| 改动 | 说明 |
|---|---|
| `index.html` | 侧栏「MCP 服务」；新 `section#mcpPage`；样式复用 settings |
| `main.js` | 路由 `#/settings/mcp` |
| `api.js` | `getMcp` / `putMcp` / `testMcp` / `reconnectMcp` |
| `mcpView.js` | 新 IIFE，无构建、无框架 |
| `styles.css` | 只加必要 class，不重排模型页 |

页面要素：

1. 说明：配置文件路径；**任一会话**在对话中或等待工具确认时都不能保存；子代理不会继承这些服务。
2. 服务卡片列表：id、transport、enabled 开关、status。
3. 编辑区按 transport 切换 stdio/http 字段。
4. `env`/`headers` 文本域写死为每行 `Key: Value`（复用 settingsView 的 `headersToText`/`textToHeaders`），契约不再并列 `KEY=VALUE`。
5. approvalMode 下拉；trusted 勾选旁写「勾选后才相信该服务的 destructiveHint」。
6. 「测试连接」用当前表单（含未保存修改），结果区展示工具表。
7. 底栏：脏标记、重置、保存。保存成功 toast/notice，失败用 settings-error。
8. 空状态：引导看 `config/mcp.example.yaml`。

不在聊天页做 MCP 选择器（v1）。工具卡片沿用现有 `toolCallStart/End`，无需新 SSE 事件。可选：preview 前缀 `mcp:` 仅作展示，不改 DTO。

### 8.4 契约文档

实施时更新 `docs/webApiSpec.md` 增一节 MCP，版本 +1。本方案是设计源，契约是接口源；两者字段名必须小驼峰且一致。

## 9. 与现有功能的交界（明确不顺手重构）

| 交界 | 处理 |
|---|---|
| Skills | 无关。MCP description 不是 skill |
| 图片输入 | MCP 返回的 image 不转成 `inputImage`，只占位文本 |
| 用量 | MCP 不产生 tokens；不必改 usage.db |
| askSubAgent | 默认无 MCP；其已知 stdout/超时缺陷不在本任务修 |
| 新增工具手册 | 实施时在 `docs/addCallableToolFunction.md` 加一句：外部 MCP 不走该手册，走 mcp.yaml |
| Workflow 方案 | 并行文档，不阻塞；若以后 worker 要 MCP，必须显式 allowlist |
| 模型切换 | 仍只重建 adapter；MCP runtime 不动 |
| 停止按钮 | 现有 interruptEvent 必须传到 MCP execute |

## 10. 分期

**P0 — 库（可 CLI 验证）**

- 配置解析、空模板、secrets 文件
- runtime 线程桥、**长驻** Client、内存快照、stdio/http 预热
- 工具适配、命名、截断、确认规则、跳过本地 schema
- `createAgent` 只读快照
- 单测用 `createMemoryMcpRuntime`
- `askModel` 在 main 预热并 finally close

**P1 — Web**

- lifespan 预热 + 关闭顺序（先停泵再 close runtime）
- store、API、设置页、**全局** 409
- 保存后重建 agent 即能在对话里看到 MCP 工具；chat 路径零建连

**P2 — 体验（方案不实施，仅登记）**

- `subscriptions/listen` 热更新工具列表
- 每会话 server 开关
- MCP image 落盘
- 把 server 状态露到聊天顶栏

**P3 — 明确不做，除非另开方案**

- OAuth 2.1 / 动态注册
- resources 自动注入 / prompts 库
- elicitation / sampling / roots UI
- 把 Flamingo 当 MCP server
- 为 MCP 改写模型 function-call 协议
- Windows
- 旧 HTTP+SSE 独立实现

## 11. 风险与非目标

1. 恶意/拙劣 MCP server 仍可能返回超大文本、慢调用、危险 tool 描述。靠 timeout、maxOutput、always 确认、数量上限缓解。
2. `npx -y ...` 会在本机拉包，这是用户配置的命令，不是 Flamingo 替用户安装生态。
3. SDK 与 FastAPI 都依赖 starlette/anyio/httpx2。加依赖后必须跑现有 209 测试；若冲突，停在依赖决议，不降 FastAPI 去迁就。
4. 现代协议下无 callback 则无法完成 elicitation 工具。这是 v1 有意缺口。
5. 不把“能连上 Inspector”当验收；验收是：配置 → 工具出现在 registry → 模型调用走确认 → 结果进现有卡片 → 停止能取消。

## 12. 成功标准（你确认后才开工）

1. 未配置 MCP 时，行为与现在一致（209 测试仍绿，子代理无 MCP）。
2. 配置一个进程内/stdio 测试 server 后，CLI 一轮对话能列出并调用 `prefix__tool`。
3. `approvalMode=always` 时 Web 出现现有确认框；拒绝不打到 MCP。
4. 停止按钮能在 timeout 内打断 MCP 调用。
5. GET `/api/mcp` 不含明文密钥；JSONL 不含 Authorization。
6. 一个坏 server 不影响内置 read/write。
7. Web 多会话共享同一 stdio 进程（用测试计数或 pid 断言）。
8. PUT 与新对话/确认共用 `mcpReloading` 门闩；pending 窗口不得出现 crashRecovered。
9. `getAgent` 持锁期间不发起 MCP I/O（用调用记录断言 snapshot 恰好一次，warmup/runCoroutine/callTool 为 0，不用墙钟）。
10. 单次 MCP 调用超时不得杀掉共享 stdio 进程；缺 mcp.yaml 时 CLI/Web 当空配置启动。

## 13. 建议你确认的选择题

1. 是否接受 **stdio + HTTP 都做**，还是先只做其中一种？
2. 默认确认是否为 **always**？（更顺手可改 never，但外部工具风险更高）
3. 是否同意主依赖加入 **`mcp>=2.2.0,<3`**？
4. v1 是否同意 **所有会话共享启用的 MCP**，不做每会话勾选？
5. 空 `mcp.yaml` 是否允许自动创建？
