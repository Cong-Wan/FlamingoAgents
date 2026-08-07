# FlamingoAgents Web 程序 —— 方案与计划

> Author: wilbur
> Version: 1.3
> Date: 2026-08-05
> 目的：为 FlamingoAgents 纯库增加一个单用户、局域网可访问的 Web 对话程序（界面参照 Kimi 官网），支持流式对话、历史保留、用量统计、工具调用确认框、模型配置，前后端 API 对接 + Token 认证。
> v1.1：按 pi 审核报告修订——H1 停止机制改为「泵线程+队列+停止标志」（跨线程 generator.close() 会抛 ValueError，原设计不可行）；M1 sessionId 格式校验；M2 models.yaml 改合并式更新；M3 XSS 防线收敛为二选一；M4 清缓存与挂起确认的边界；M5 统一异常映射；L1-L6 如实修订语义/补 TODO。
> v1.2：用户拍板渲染方案——vendor marked + DOMPurify（§4.6）；接口契约已独立落档 docs/webApiSpec.md v1.1。
> v1.3：用户拍板包结构调整——`flamingoWeb/` 改名 `webApp/`，内部拆分 `backend/`（FastAPI）与 `frontend/`（原生静态文件，原 static/），前端仍由后端启动时托管；启动命令变为 `uv run python -m webApp`。

---

## 1. 已拍板决策

| 编号 | 问题 | 决策 |
|---|---|---|
| Q1 | 编排 agent | **本期不做**（未想好），不预留 profile 表结构，仅保证未来可扩展（见 §10） |
| Q2 | 前端技术栈 | **只允许原生 HTML + CSS + JS**，无框架、无构建工具、无 npm |
| Q3 | Token 认证 | **静态 Bearer Token**：环境变量 `FLAMINGO_WEB_TOKEN`；前端登录页填 token 存 localStorage |
| Q4 | 会话历史存储 | 对话内容复用现有 **jsonl 会话日志**；另加轻量索引 `webData/sessions.json`（id/title/workDir/model/createdAt/updatedAt/usage） |
| Q5 | 模型配置 | 提供配置界面，**与 CLI 共用 `config/models.yaml`**，字段严格按当前 yaml 已有字段 |
| Q6 | workDir | **一个会话绑定一个 workDir**，建会话时指定，之后不可改 |
| Q7 | 交付 | 先落档本方案 + TODO List，审核通过后实施 |

## 2. 现状能力映射（零库改动结论）

| Web 需求 | 库现有能力 | 结论 |
|---|---|---|
| 流式对话 | `agent.runUserMessageStream(message, sessionId)` 生成器，7 种事件 dataclass | 后端 SSE 1:1 透传 |
| 工具确认框 | `confirmationRequiredEvent` + `continueConfirmationStream(sessionId, confirmationId, approved)` | 前端弹窗 → 调续跑接口 |
| 历史回放 | `.jsonl` 日志（systemMessage/userMessage/assistantMessage/toolResult）+ `jsonlLog.readEvents()` + resume | Web 层读 jsonl 转 UI 消息 |
| 用量统计 | `conversation.usageTotal`（promptTokens/cachedTokens/completionTokens，resume 后仍正确累计） | 每轮终态后写入 sessions 索引 |
| 会话绑定 workDir | `createAgent(workDir, logDir=..., providerId=..., modelId=...)` 全部支持注入 | **每会话一个 agent 实例**，集中 logDir |
| 模型配置界面 | `config/models.yaml` + `loadModelConfigFromYaml` 解析规则 | Web 层读写同一 yaml，改后清 agent 缓存 |

**核心架构结论：`flamingoAgents` 库一行不改。** Web 是薄壳，新增独立包 `webApp/`（v1.3：内含 `backend/` 与 `frontend/` 两个子目录）。

### 2.1 关键机制说明（来自库源码，方案依赖的事实）

1. `createAgent(workDir, *, logDir, providerId, modelId, systemPrompt, toolNames, ...)`：每次调用重新读 `config/models.yaml`，成本极低（yaml + 构建 tool definitions），按会话创建 agent 实例无性能问题；
2. `agent.getConversation(sessionId)`：`logDir/{sessionId}.jsonl` 存在即自动 resume（含 system 恢复，前缀一致、缓存可命中）；
3. 事件流契约（docs/streamOutputPlan.md §6）：终态事件（completed/confirmationRequired/error）在**会话锁释放后**才 yield；中途放弃迭代必须 `stream.close()`；
4. `conversation.usageTotal` 在 resume 时从日志重放累计，-live turn 也累计——**直接作为会话总用量**；
5. 服务器重启后若有未完成确认：pendingConfirm 只在内存，丢失；但 resume 会重建 danglingToolCalls，**下一条用户消息会按库语义重放未闭合工具调用（含重新触发权限确认）**。Web 层如实呈现，不做额外状态机；
6. `jsonlLog.readEvents()` 容忍末行写一半的损坏行（跳过），直接可读；
7. `models.yaml` 解析规则（modelConfig.py）：`api` 仅支持 `openai-completions`；`apiKey` 支持 `$ENV` / `${ENV}` 环境变量引用；`stream` 字段解析器支持但当前 yaml 未使用（本期 UI 不出该字段，遵循 Q5「按当前 yaml 已有字段」）。

## 3. 总体架构

```
浏览器（原生 HTML/CSS/JS，Kimi 布局，无框架无构建）
  左侧栏：新建会话 / 会话列表(按日期分组) / 底部入口(模型配置、用量)
  主区域：标题栏(会话名+模型) / 消息流(markdown、思维链折叠、工具卡片)
  底部：多行输入框 + 发送/停止按钮
  弹层：工具确认框（批准/拒绝）
  登录门：token 输入页（localStorage 记忆）
        │  REST + SSE（Authorization: Bearer <token>，同源部署无 CORS）
        ▼
FastAPI（uvicorn，host 0.0.0.0，默认端口 8787）
  ├─ auth 依赖：校验 Bearer Token
  ├─ /api/auth/*      登录/校验
  ├─ /api/sessions/*  会话 CRUD + 历史消息
  ├─ /api/chat/*      SSE 对话流 / 确认续跑 / 停止
  ├─ /api/usage       用量汇总
  ├─ /api/models      模型配置读写（脱敏）
  └─ /static、/       原生前端静态文件
        │
        ▼
flamingoAgents 纯库（零改动）
  createAgent + 事件流 + jsonl resume + usageTotal
        │
        ▼
webData/（新增，gitignore）
  ├─ sessions.json          会话索引
  └─ sessionLogs/{sessionId}.jsonl   集中日志（= 各 agent 的统一 logDir）
```

**部署形态**：`uv run python -m webApp` 一条命令启动；同源托管前端，无需 CORS、无需 nginx。

## 4. 后端设计（`webApp/` 新包，v1.3 拆分 backend/frontend）

### 4.1 文件结构（全部小驼峰命名 + 文件头，遵循 AGENTS.md）

```
webApp/
  __init__.py            # 包说明
  __main__.py            # uvicorn 启动入口（读环境变量）
  backend/               # FastAPI 后端
    __init__.py          # 子包说明
    server.py              # FastAPI app、路由注册、静态文件托管（托管 frontend/）
    auth.py                # Bearer Token 校验依赖
    sessionStore.py        # sessions.json 索引 CRUD（锁 + 原子写）
    agentManager.py        # sessionId → agent 实例缓存、活跃流登记、停止
    sseCodec.py            # 库事件 dataclass → SSE 文本帧
    historyView.py         # jsonl 事件 → UI 消息 DTO
    modelConfigStore.py    # models.yaml 读写 + apiKey 脱敏 + 结构校验
  frontend/              # 原生 HTML/CSS/JS 前端（无框架无构建）
    index.html
    styles.css
    vendor/marked.min.js      # v1.2 拍板 vendor
    vendor/dompurify.min.js
    js/api.js            # fetch 封装（自动带 token、401 跳登录）
    js/sse.js            # fetch ReadableStream 解析 SSE 帧
    js/store.js          # 极简状态（token、当前会话、流式缓冲）
    js/sidebarView.js    # 会话列表/新建/重命名/删除
    js/chatView.js       # 消息渲染、流式增量、工具卡片、确认框
    js/settingsView.js   # 模型配置编辑
    js/usageView.js      # 用量统计
    js/main.js           # hash 路由 + 启动引导
```

### 4.2 会话与 agent 实例管理（Q6 落地）

- **每会话一个 agent 实例**：`agentManager.getAgent(sessionId)` 从索引取该会话的 `workDir/providerId/modelId`，调 `createAgent(workDir=..., logDir=webData/sessionLogs, providerId=..., modelId=...)`，缓存于 `dict[sessionId, agent]`；
- 所有会话日志集中在 `webData/sessionLogs/`，与 workDir 解耦（删除会话=删索引条目+删 jsonl+清缓存实例）；
- 新建会话参数：`workDir`（必填，默认项目根目录，校验存在且为目录、resolve 绝对路径）、`providerId`（必填，下拉自 models.yaml）、`modelId`（可空=该 provider 首个模型）；标题默认「新会话」，首条用户消息发出后自动改为消息前 20 字；
- **模型配置变更后**：`PUT /api/models` 成功 → 清空 agent 缓存（新会话/下次发消息时按新配置重建；进行中的流不受影响）；
- 单用户规模下不做缓存淘汰（几十个 agent 实例仅持有 adapter + registry，内存可忽略），记录为已知取舍。

### 4.3 SSE 桥接（核心链路，v1.1 按审核 H1/L4 重写）

**关键约束（审核 H1）**：SSE 生成器在 threadpool 中执行时，其生命周期内绝大部分时间**阻塞在库生成器 `next()` 内部**（模型 HTTP 流式读取，非挂起在 yield 点）。CPython 语义：对正在执行的生成器跨线程调 `close()` 抛 `ValueError: generator already executing`。因此「stop 接口直接 close 活跃流」不可行，必须改为**泵线程 + 队列 + 停止标志**结构：

```python
class streamPump:
    # 每个对话流一个泵：专用线程从库生成器 next() 泵事件进队列；
    # SSE 生成器只从队列取（带 timeout 轮询），stop() 只置标志位，不跨线程 close。
    def __init__(self, agent, sessionId, stream):
        self.queue = queue.Queue()
        self.stopFlag = threading.Event()
        self.thread = threading.Thread(target=self._pump, daemon=True)

    def _pump(self):
        terminal = None
        try:
            for event in self.stream:
                if self.stopFlag.is_set():
                    break                      # 停止请求：跳出后由本线程 close
                self.queue.put(event)
                if isinstance(event, terminalEventTypes):
                    terminal = event
                    break
        except Exception as error:
            self.queue.put(errorEvent(message=str(error), errorType=type(error).__name__))
        finally:
            self.stream.close()                # 同线程 close 挂起的生成器，合法
            sessionStore.updateUsage(self.sessionId, self.agent.getConversation(self.sessionId).usageTotal)
            agentManager.unregisterStream(self.sessionId)
            self.queue.put(None)               # 哨兵：通知 SSE 生成器结束

def sseGen(pump):
    while True:
        try:
            event = pump.queue.get(timeout=15)
        except queue.Empty:
            yield ': keep-alive\n\n'           # 心跳注释行，防代理 idle 切断
            continue
        if event is None:
            return
        yield encodeSse(event)
```

- 对话与确认接口均为 **FastAPI 同步 `def` 端点**（自动进 threadpool），返回 `StreamingResponse(sseGen(pump), media_type='text/event-stream')`；
- **usage 回写时机（审核 L4）**：在**泵线程结束时**执行（而非 SSE 生成器 finally）——客户端早断时 SSE 生成器先结束，但泵线程仍跑到终态，回写值才是完整的；
- **客户端断开**：SSE 生成器不再消费队列 → 泵线程继续跑到当前终态后自行 close 释放锁（库契约 §2.1-3）。队列不设上限的消费积压风险：泵在 stopFlag 之外不阻塞 put（Queue 无界），单用户场景可接受；

- **事件映射**（与库 7 事件同构，data 为 JSON）：

| SSE event | data 字段 | 来源 |
|---|---|---|
| `textDelta` | `{text}` | textDeltaEvent |
| `reasoningDelta` | `{text}` | reasoningDeltaEvent |
| `toolCallStart` | `{toolCall:{id,toolName,arguments}, preview}` | toolCallStartEvent |
| `toolCallEnd` | `{toolResult:{toolCallId,toolName,isError,content,details}}` | toolCallEndEvent |
| `confirmationRequired` | `{confirmationId, reason, commandPreview, toolCall}` | confirmationRequiredEvent（终态，流结束） |
| `completed` | `{message}` | completedEvent（终态） |
| `error` | `{message, errorType}` | errorEvent（终态） |

- **确认链路**：前端收到 `confirmationRequired`（流自然结束）→ 弹确认框 → 用户点批准/拒绝 → `POST /api/chat/confirm` 开新 SSE 流，续跑事件**继续渲染进同一个 assistant 消息块**；
- **并发约束**：同一 sessionId 已有活跃流时，新流请求返回 **409**（前端发送期间也禁用输入）；库级 RLock 是第二道保障；
- **停止（v1.1 修订语义）**：`POST /api/chat/stop {sessionId}` → 泵线程置停止标志。**停止不是即时的**：最坏延迟到当前模型 step 产出下一个事件或 HTTP 超时（60s）；泵线程跳出循环后由它自己 close 库生成器并释放会话锁。前端点停止后立即停止渲染并给半截消息加「已中断」标记；
- **半截消息不落盘（审核 L1 修订）**：`appendAssistantMessage` 只在 `finalChunk` 到达后执行，模型响应中途停止时**已渲染的半截文本不会进 jsonl**，刷新后消失（已完成的 toolResult 会落盘）——前端「已中断」标记同时承担告知义务，不做伪持久化。

### 4.4 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | `{token}` → `{ok:true}`，错则 401（前端提交 token 即验证；与 me 同源，保 login 砍 me——审核 L6） |
| GET | `/api/sessions` | 索引列表，按 updatedAt 倒序 |
| POST | `/api/sessions` | `{workDir, providerId, modelId?}` → 会话对象 |
| PATCH | `/api/sessions/{id}` | `{title}` 重命名 |
| DELETE | `/api/sessions/{id}` | 删索引+jsonl+缓存实例（活跃流拒绝） |
| GET | `/api/sessions/{id}/messages` | jsonl → UI 消息数组（§4.5） |
| POST | `/api/chat/stream` | `{sessionId, message}` → SSE（§4.3） |
| POST | `/api/chat/confirm` | `{sessionId, confirmationId, approved}` → SSE |
| POST | `/api/chat/stop` | `{sessionId}` → `{ok:true}` |
| GET | `/api/usage` | `{total:{promptTokens,cachedTokens,completionTokens}, sessions:[{sessionId,title,modelId,usage...}]}` |
| GET | `/api/models` | 解析 models.yaml 返回 JSON，apiKey 脱敏（§4.7） |
| PUT | `/api/models` | 全量替换 models.yaml（校验+原子写+清 agent 缓存） |

除 `/api/auth/login` 外全部需要 `Authorization: Bearer <token>`，失败统一 401 JSON。

**统一异常映射（审核 M5）**：server.py 注册 exception handler，库抛出的 `RuntimeError`（provider 不存在、apiKey 环境变量缺失、模型无匹配、toolNames 未知等，错误消息本是中文）→ 400 JSON `{error: 原始消息}`；未预期异常 → 500 JSON。不出现裸 traceback 响应。

**sessionId 校验（审核 M1）**：所有收 sessionId 的端点入口统一 `re.fullmatch(r'[A-Za-z0-9_-]+', sessionId)`，不合规直接 400——堵住 `../../` 路径遍历删/读任意 `.jsonl` 的纵深防御缺口。

### 4.5 历史消息视图（historyView）

读 `webData/sessionLogs/{sessionId}.jsonl`（复用 `jsonlLog.readEvents()`），转为 UI DTO 数组：

- `systemMessage` → 不下发；
- `userMessage` → `{kind:'user', content, timestamp}`；
- `assistantMessage` → `{kind:'assistant', content, toolCalls:[{id,toolName,arguments}], usage, model, timestamp}`；
- `toolResult` → `{kind:'tool', toolCallId, toolName, isError, content, timestamp}`（content 原样下发，前端折叠展示超长内容）。

前端按序渲染：assistant 的 toolCalls 与后续 toolResult 按 id 配对成工具卡片；末尾未配对的 toolCalls（dangling，重启恢复场景）渲染为「中断未完成」置灰卡片，不重放不报警。

### 4.6 前端设计（原生 HTML/CSS/JS，Q2）

**XSS 防线（审核 M3 收敛，v1.2 用户已拍板选项①）**：assistant 内容与工具结果均不可信（读文件/bash 输出），渲染 markdown→HTML 若无消毒，注入脚本可窃取 localStorage 中的 token → 获得本机文件读写能力。

**已定稿：vendor `marked.min.js` + `dompurify.min.js` 进 `static/vendor/`**——marked 转 markdown→HTML，DOMPurify 消毒后 innerHTML；无框架无构建，仍属原生前端。

**明确禁止**：自写 HTML 拼接 + 无消毒（若未来移除 vendor，唯一合法降级是纯 textContent 拼接）。

**布局参照 Kimi 官网**：

- 左侧深色侧栏：顶部「+ 新建会话」按钮；中部会话列表按日期分组（今天/昨天/更早），hover 显示重命名/删除；底部两个入口：模型配置、用量统计；
- 主区顶栏：会话标题 + 当前模型名；
- 消息流：user 右侧浅色气泡；assistant 左侧通栏（头像+正文 markdown）；思维链为可折叠灰色块（默认折叠，标题「已思考」）；工具调用为卡片（图标+工具名+preview 一行+状态：执行中/完成/失败/被拒绝，点击展开入参与结果）；
- 底部 composer：自适应多行 textarea，Enter 发送 / Shift+Enter 换行；流式期间发送按钮变为「停止」；
- 确认框：居中模态，展示工具名、原因（reason）、命令预览（commandPreview）、入参 JSON，两个按钮：批准 / 拒绝；
- 登录门：无 token 或 401 时全屏遮罩，仅一个 token 输入框；
- hash 路由：`#/chat`（首页/新会话）、`#/chat/{sessionId}`、`#/settings/models`、`#/usage`；
- 流式渲染：fetch + ReadableStream 自解析 SSE（EventSource 不支持 POST/Header，不可用），textDelta 追加进当前 assistant 块并重渲染 markdown；reasoningDelta 追加进思考块。

**Markdown 渲染决策（v1.2 已拍板）**：vendor 两个单文件库 `marked.min.js` + `dompurify.min.js` 进 `static/vendor/`——无框架、无构建、无运行时依赖安装，仍属原生前端范畴。

**孤儿 End 渲染（审核 L2）**：拒绝路径只发 `toolCallEndEvent` 无 Start——前端在收到 `confirmationRequired` 时即用事件中的 toolCall 建好「待确认」卡片，用户拒绝后 End 到达置「被拒绝」状态；历史回放中未配对的 toolCalls（dangling）渲染为「中断未完成」置灰卡片。

### 4.7 模型配置（Q5 落地，严格按现有 yaml 字段）

- `GET /api/models` 返回结构与 yaml 一致：`providers: {id: {baseUrl, api, apiKey, models:[{id,name,input,contextWindow,maxTokens,reasoning,thinking:{type},reasoningEffort,cost:{input,output,cacheRead,cacheWrite}}]}}`；
- **apiKey 脱敏**：非空且非 `$` 开头（环境变量引用不算秘密，原样返回）→ 返回 `"__KEEP__"`；PUT 时 `"__KEEP__"` 表示保留原值，空串表示删除，其它值表示更新；
- **合并式更新（审核 M2 修订，替代全量替换）**：PUT 收到 UI schema 内的 JSON → 读原 yaml → **仅覆盖 UI 字段**，解析器支持但 UI 未覆盖的字段（如 `stream`）、未知字段、注释之外的结构性内容原样保留（pyyaml 读写，注释丢失接受，字段不丢是硬要求）→ 写前备份 `models.yaml.bak` → 临时文件 + rename 原子替换 → 成功后清 agent 缓存；
- `PUT /api/models` 校验（对齐 modelConfig.py 解析规则）：providers 必须是 dict；每 provider `baseUrl` 非空、`api` 仅允许 `openai-completions`、`apiKey` 非空或 `__KEEP__`、`models` 非空数组且每项 `id` 非空；数字字段必须是数；`thinking.type ∈ {enabled, disabled}`；校验失败 400 附中文原因；
- **清缓存与挂起确认的边界（审核 M4）**：pendingConfirm 只在 agent 实例内存中，清缓存后旧确认框再点批准会收到 `confirmationMismatch`。**接受该语义**（成本最低）：前端 confirm 收到此错误时刷新消息区，未闭合工具按 dangling 卡片呈现，下一条消息按库语义自愈。清缓存采用「置失效标记、下次 getAgent 惰性重建」而非立即销毁，缩小窗口；
- UI：provider 卡片列表（可新增/删除 provider），卡片内模型表格行编辑，保存按钮触发 PUT；页面顶部提示「与 CLI 共用 config/models.yaml」。

### 4.8 认证与启动（Q3 落地）

- token 来源：环境变量 `FLAMINGO_WEB_TOKEN`；未设置时启动报错退出（强制显式配置，避免弱默认）；
- `FLAMINGO_WEB_HOST`（默认 `0.0.0.0`）、`FLAMINGO_WEB_PORT`（默认 `8787`）；
- FastAPI 依赖 `requireToken`：比对 `Authorization: Bearer <token>`（`secrets.compare_digest`），挂在所有 `/api/*`（login 除外）；
- 静态文件不鉴权（页面本身无数据），所有数据接口都有 token 门；
- **单 worker 约束（审核 L3）**：agent 缓存、活跃流登记、索引文件锁全是进程内内存态，`__main__.py` 写死 `uvicorn.run(app, workers=1)` 并注释原因，禁止多 worker 启动；
- 启动命令：`FLAMINGO_WEB_TOKEN=xxx uv run python -m webApp`。

### 4.9 数据与安全边界

- 新增 `webData/`（索引+集中日志），加入 `.gitignore`；
- `config/models.yaml` 已在 gitignore，PUT 接口原子写不破坏该约束；
- 单用户、局域网：不防 CSRF（无 cookie，纯 Bearer header 天然免疫），不防暴力破解（token 足够长即可），workDir 允许任意本机路径（等同 CLI 能力，不额外收紧）。

## 5. TODO List（按实施顺序，目标驱动验证）

> 统一约定：新文件带文件头（小版本号规则）；python 用 uv 管理；不引入测试框架，用 curl/脚本断言 + 真实模型跑通验证。

### P0 后端骨架

- [ ] T0.1 `uv add fastapi uvicorn`；
- [ ] T0.2 `webApp/__init__.py`、`__main__.py`、`webApp/backend/server.py`（挂静态目录 + `/api/health`）；
  - 验证：`FLAMINGO_WEB_TOKEN=t uv run python -m webApp` 启动；`curl localhost:8787/` 返回 index.html；`curl localhost:8787/api/health` 200。
- [ ] T0.3 `auth.py` + 全 API 挂依赖 + 统一异常映射（RuntimeError → 400 中文消息）+ sessionId 格式校验（`[A-Za-z0-9_-]+`，审核 M1/M5）；
  - 验证：无 token `curl /api/health` → 401；带错 token → 401；正确 token → 200；`curl /api/sessions/..%2F..%2Fetc/messages` → 400 而非 404/500。

### P1 会话域

- [ ] T1.1 `sessionStore.py`（索引 CRUD、锁、原子写）；
- [ ] T1.2 `agentManager.py`（getAgent 懒建缓存、活跃流登记、stop）；
- [ ] T1.3 会话路由：create/list/rename/delete；首条用户消息发出后标题自动改为前 20 字（审核 L5 补）；
  - 验证：curl 建会话（指定 workDir+provider）→ 索引文件出现条目、字段正确；list 倒序；rename 生效；delete 后 jsonl 与索引同步删除；workDir 不存在 → 400；不存在的 providerId 建会话 → 400 中文错误（M5）；发首条消息后标题自动更新。

### P2 对话流（核心）

- [ ] T2.1 `sseCodec.py` 7 事件映射；
- [ ] T2.2 `streamPump`（泵线程+队列+停止标志+心跳，审核 H1）+ `POST /api/chat/stream` + `POST /api/chat/confirm` + `POST /api/chat/stop`；
- [ ] T2.3 泵线程结束时 usage 回写索引（审核 L4）；
  - 验证（curl + 真实模型，对齐 streamOutputPlan §6.6-5 的 7 场景）：① 纯文本流式逐段到达；② 免确认工具 Start/End 成对；③ 需确认工具 → confirmationRequired 终态 → confirm(批准) 续流 Start→End→completed；④ confirm(拒绝) 只见 End(isError)（孤儿 End，前端渲染策略见 §4.6「孤儿 End 渲染」）；⑤ 流式中 stop → **当前 step 结束后**锁释放（非即时，审核 H1 语义），随后可发新消息；⑥ 同会话活跃流中再发 → 409；⑦ 重启服务后发消息 → resume 正常、usage 累计正确。

### P3 历史与用量接口

- [ ] T3.1 `historyView.py` + `GET /api/sessions/{id}/messages`；
- [ ] T3.2 `GET /api/usage`（total + per session）；
  - 验证：多轮+工具调用的会话，messages 接口顺序/配对正确（assistant toolCalls 与 toolResult 按 id 对上）；usage 总数 = 索引累计 = jsonl 重放值。

### P4 模型配置接口

- [ ] T4.1 `modelConfigStore.py`：GET（脱敏）/ PUT（合并式更新+校验+`.bak` 备份+原子写+惰性清缓存，审核 M2/M4/L5）；
  - 验证：GET 中 apiKey 显示 `__KEEP__`、环境变量引用原样；PUT 改 baseUrl 后 yaml 生效且原 apiKey 未被覆盖；**手工加过 `stream` 字段的 yaml 经 PUT 后该字段仍在**（M2 硬指标）；PUT 非法结构 400；`.bak` 备份生成；改配置后新会话用新配置；挂着确认框时 PUT 后点批准 → 前端收到 confirmationMismatch 并刷新呈现 dangling（M4）。

### P5 前端

- [ ] T5.1 登录门 + api.js（token 存储、401 兜底）；
- [ ] T5.2 侧栏（新建会话弹窗：workDir/provider/model；列表分组；重命名/删除）；
- [ ] T5.3 聊天区：历史渲染 + markdown + 思维链折叠 + 工具卡片；
- [ ] T5.4 流式：sse.js + 增量渲染 + 停止按钮 + 发送禁用；
- [ ] T5.5 确认框：批准/拒绝续流渲染（含孤儿 End 处理：拒绝路径用 confirmationRequired 事件中的 toolCall 建卡片，End 到达置「被拒绝」，审核 L2）；
- [ ] T5.6 模型配置页 + 用量页；
  - 验证（浏览器手工）：Kimi 式布局；流式逐字；确认框两条路径；重启后历史完整；用量数字与接口一致；模型配置保存后新会话生效；局域网站另一台设备用 token 可访问。

### P6 收尾

- [ ] T6.1 `.gitignore` 加 `webData/`；
- [ ] T6.2 README 或 docs 补启动说明（环境变量、端口、token）；
- [ ] T6.3 全链路回归：CLI（askModel.py）不受影响（库零改动，跑一次确认）。

## 6. 不做的事（YAGNI）

- 不做 agent 编排/profile（Q1 未定，未来扩展点见 §10）；
- 不做多用户、不做数据库（jsonl+json 索引足够）；
- 不做图片上传/多模态输入（模型配了 image input，但本期 UI 不出）；
- 不做消息编辑/重生成/分支；
- 不做移动端适配（桌面优先，CSS 保持基本响应式即可）；
- 不改 `flamingoAgents` 库任何代码；
- 不出 `stream` 字段编辑（当前 yaml 未用，遵循 Q5）。

## 7. 风险与对策

| 风险 | 对策 |
|---|---|
| SSE 经 fetch 流式在浏览器兼容性 | fetch+ReadableStream 主流浏览器均支持；局域网自用足够 |
| 客户端断连/停止的锁释放（审核 H1） | 泵线程结构：stop 只置标志位，泵线程自己 close 生成器释放锁；停止非即时（最坏延迟到当前 step 结束或 60s HTTP 超时），T2.3 场景⑤按修订语义验证 |
| 重启丢失内存 pendingConfirm | 如实呈现 dangling 卡片；下一条消息按库语义恢复（§2.1-5），文档告知用户 |
| 清模型配置缓存使挂起确认失效（审核 M4） | 接受语义：前端收 confirmationMismatch 刷新消息区，dangling 自愈 |
| 自写 SSE 解析出错 | sse.js 按「空行分帧、event:/data: 行解析」最简实现，P5 验证覆盖 |
| XSS 窃取 token（审核 M3，v1.2 已拍板） | vendor marked+DOMPurify 消毒后 innerHTML；禁止无消毒 innerHTML |
| models.yaml 写坏导致 CLI 也无法启动 | 合并式更新 + PUT 强校验 + 原子写 + `.bak` 备份 |

## 8. 验收标准汇总

1. `FLAMINGO_WEB_TOKEN=xxx uv run python -m webApp` 启动后，局域网另一设备可访问；
2. 无 token / 错 token 调任何数据 API 均 401；非法 sessionId 一律 400；
3. 完整走通：登录 → 新建会话（选 workDir+模型）→ 流式对话 → 停止生成（含锁释放验证）→ 工具确认（批准+拒绝各一次）→ 历史刷新仍在 → 用量页数字正确 → 模型配置页改 baseUrl 保存（含字段保留/备份验证）→ 新会话生效；
4. `uv run python askModel.py` 行为与现状一致（库零改动证明）。

## 9. 实施量预估

后端约 600 行（8 个小文件），前端约 1200 行（1 html + 1 css + 8 js），无框架无构建，单用户规模下 P0–P6 全部完成即可交付。

## 10. 未来扩展点（不在本期）

- Q1 编排落地时：sessions 索引加 `profileId` 字段、agentManager 缓存 key 扩展、新建会话弹窗加 profile 下拉——索引结构已按 dict 存储，加字段不破坏兼容；
- 多 agent 协作、插件工具市场、WebSocket 替代 SSE：出现时再评估。
