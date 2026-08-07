# FlamingoAgents Web —— 前后端接口契约

> Author: wilbur
> Version: 1.1
> Date: 2026-08-05
> 目的：定义 Web 程序前后端对接的全部接口（REST + SSE），作为 `docs/webAppPlan.md` v1.1 的接口层细化。前端/后端各自独立开发时以本文档为唯一契约。
> 上游约束：事件模型对齐 `flamingoAgents/core/types.py` 7 事件；会话日志结构对齐 `core/conversation.py` jsonl 事件；模型配置结构对齐 `config/models.yaml` 与 `models/modelConfig.py` 解析规则。
> v1.1：按 pi 审核报告修订——H1 新增 pending 查询端点修复「待确认刷新后死锁」；H2 tool DTO 补 details（区分被拒绝/失败）；M1 usage 嵌套字段映射表；M2 modelError/timings 口径；M3 GET models 不用库解析器；M4 建会话预检实现路径；M5 dangling 重放渲染归位；L1-L6 标注不可达项/幂等/初值等。

---

## 1. 通用约定

### 1.1 基础

- Base URL：`http://{host}:{port}`（默认 `8787`，局域网部署），前后端同源，无 CORS；
- 静态页面 `/`、`/static/*` 不鉴权；**`/api/*` 全部需要认证**（唯一例外：`POST /api/auth/login`）；
- 认证头：`Authorization: Bearer <token>`（token 由用户启动服务时通过 `FLAMINGO_WEB_TOKEN` 设置）；
- 请求/响应均为 `application/json; charset=utf-8`；SSE 接口响应为 `text/event-stream`；
- 时间戳：ISO 8601 UTC 字符串（与 jsonl 日志一致），如 `2026-08-05T02:17:55.158614+00:00`；
- sessionId 合法字符集：`^[A-Za-z0-9_-]+$`，不满足一律 **400**。

### 1.2 错误响应

所有非 2xx 响应统一结构：

```json
{ "error": "中文错误描述" }
```

| 状态码 | 含义 | 典型场景 |
|---|---|---|
| 400 | 参数/校验失败 | sessionId 非法、workDir 不存在、models 结构非法、message 为空（仅 REST 层预检；流内错误走 SSE error 帧） |
| 401 | 未认证或 token 错误 | 缺/错 Authorization 头 |
| 404 | 资源不存在 | sessionId 不在索引中 |
| 409 | 会话忙 | 该 sessionId 已有活跃流 |
| 500 | 未预期服务端错误 | 兜底 |

**库的 RuntimeError**（provider 不存在、apiKey 环境变量缺失、模型无匹配等）→ 统一映射 **400**，`error` 透传库的中文消息。

**状态码补充约定**：资源创建（POST /api/sessions）亦返回 200，本契约不使用 201（审核 L2）。

### 1.3 SSE 帧格式

- 每个事件一帧：`event: <事件名>\ndata: <单行JSON>\n\n`，帧间空行分隔；
- JSON 序列化 `ensure_ascii=False`（中文不转义）、单行；
- 空闲保活：约 15s 无事件时下发注释帧 `: keep-alive\n\n`（前端忽略）；
- **流结束 = 服务端关闭连接**。终态事件（`completed`/`confirmationRequired`/`error`）发出后连接随即关闭；前端若在未收到任何终态事件时连接断开，按「中断」处理。

## 2. 数据模型

### 2.1 session（会话对象）

```json
{
  "sessionId": "session_0bcd11873ded",
  "title": "新会话",
  "workDir": "/Users/wilbur/project/FlamingoAgents",
  "providerId": "volcano",
  "modelId": "deepseek-v4-flash",
  "createdAt": "2026-08-05T02:17:55.158614+00:00",
  "updatedAt": "2026-08-05T02:18:05.347175+00:00",
  "usage": { "promptTokens": 5532, "cachedTokens": 1024, "completionTokens": 635 }
}
```

- `title`：默认「新会话」；**首条用户消息发出后后端自动改为消息前 20 字**；可经 PATCH 改名；
- `modelId`：建会话时未指定则为该 provider 首个模型（与库 `selectModel` 行为一致），此处记录的是**实际生效值**；
- `usage`：会话累计 token（来源 `conversation.usageTotal`，泵线程每轮结束后回写）；初始值 `{ "promptTokens": 0, "cachedTokens": 0, "completionTokens": 0 }`（审核 L6）；
- `updatedAt`：建会话、发消息、改名、用量回写时刷新。

### 2.2 message（历史消息 DTO，`GET /api/sessions/{id}/messages` 元素）

三种 `kind`，按 jsonl 顺序下发（systemMessage 不下发）：

```json
{ "kind": "user", "content": "阅读 @/xx 文件并总结", "timestamp": "..." }
```

```json
{
  "kind": "assistant",
  "content": "## 总结\n...",
  "toolCalls": [ { "id": "call_e7b93b...", "toolName": "read", "arguments": { "path": "/xx" } } ],
  "usage": { "promptTokens": 3948, "cachedTokens": 1024, "completionTokens": 558 },
  "model": "deepseek-v4-flash",
  "timestamp": "..."
}
```

```json
{
  "kind": "tool",
  "toolCallId": "call_e7b93b...",
  "toolName": "read",
  "isError": false,
  "content": "工具返回原文（可能很长，前端折叠）",
  "details": { },
  "timestamp": "..."
}
```

- `assistant.toolCalls` 可能为空数组（纯文本回复）；
- **`assistant.usage` 归一化映射表（审核 M1，嵌套取值，前后端必须一致）**：`promptTokens ← usage.prompt_tokens`；`cachedTokens ← usage.prompt_tokens_details.cached_tokens`（**嵌套字段**，缺省 0）；`completionTokens ← usage.completion_tokens`；usage 缺失/非对象 → 整个字段为 `null`；
- **`tool.details` 原样透传**（审核 H2，与 SSE `toolCallEnd.toolResult.details` 同口径）。渲染规则：`details.reason == "userRejectedApproval"`（即 `blocked: true`）→ 呈现「**被拒绝**」；其余 `isError=true` → 呈现「失败」。不得靠匹配 content 文案判别；
- **jsonl 事件过滤口径（审核 M2）**：`systemMessage`、`modelError` 不下发；`assistantMessage.timings` 不下发；
- 前端配对规则：`assistant.toolCalls[].id` ↔ 后续 `tool.toolCallId`；**末尾未配对的 toolCalls = dangling（中断未完成），渲染置灰卡片**——但需先经 §3.7 pending 接口识别：**pending 中的 toolCall 不按 dangling 渲染，而是重弹确认框**（审核 H1）。

### 2.3 usage（用量汇总，`GET /api/usage` 响应）

```json
{
  "total": { "promptTokens": 12000, "cachedTokens": 3000, "completionTokens": 2400 },
  "sessions": [
    {
      "sessionId": "session_0bcd11873ded",
      "title": "阅读文档并总结",
      "providerId": "volcano",
      "modelId": "deepseek-v4-flash",
      "usage": { "promptTokens": 5532, "cachedTokens": 1024, "completionTokens": 635 },
      "updatedAt": "..."
    }
  ]
}
```

`sessions` 按 `updatedAt` 倒序；`total` 为全部会话 usage 求和。

### 2.4 modelConfig（模型配置文档，对齐 `config/models.yaml`）

`GET /api/models` 响应 / `PUT /api/models` 请求体同构：

```json
{
  "providers": {
    "volcano": {
      "baseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
      "api": "openai-completions",
      "apiKey": "__KEEP__",
      "models": [
        {
          "id": "deepseek-v4-flash",
          "name": "",
          "input": ["text", "image"],
          "contextWindow": 1048576,
          "maxTokens": 65536,
          "reasoning": true,
          "thinking": { "type": "enabled" },
          "reasoningEffort": "max",
          "cost": { "input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0 }
        }
      ]
    }
  }
}
```

**apiKey 脱敏与回写规则**：

| yaml 实际值 | GET 返回 | PUT 传回该值时的行为 |
|---|---|---|
| 明文 key（如 `ark-f6f4...`） | `"__KEEP__"` | 保留 yaml 原值 |
| 环境变量引用（`$FOO` / `${FOO}`） | 原样返回 | 写回该引用串 |
| 缺失/空 | `""` | 删除该字段（校验：该 provider 将因缺 key 无法使用，允许保存但建会话时报 400） |
| — | 任何其它新字符串 | 覆盖为新值 |

**`$` 前缀约定（审核 L3）**：任何以 `$` 开头的 apiKey 值一律视为环境变量引用（GET 原样返回不脱敏、PUT 按引用写回）；因此**明文 key 不得以 `$` 开头**，UI 输入校验需拦截并提示。

**PUT 合并语义（webAppPlan §4.7）**：仅覆盖本文档 schema 内字段；yaml 中 schema 外字段（如 `stream`）原样保留；写前备份 `models.yaml.bak`。

**PUT 校验规则**（失败 400，消息中文）：

- `providers` 必须是对象且非空；
- 每 provider：`baseUrl` 非空字符串；`api` 仅允许 `"openai-completions"`；`models` 非空数组；
- 每 model：`id` 非空字符串；`name` 可空字符串；`input` 为字符串数组（元素限 `"text"`/`"image"`）；`contextWindow`/`maxTokens` 为正整数；`reasoning` 为布尔；`thinking` 可缺省，存在时 `type ∈ {"enabled","disabled"}`；`reasoningEffort` 可缺省，存在时为字符串；`cost` 四字段为数值（≥0）。

## 3. REST 接口明细

### 3.1 POST /api/auth/login —— 登录（唯一免认证接口）

请求：

```json
{ "token": "用户输入的token" }
```

- 200：`{ "ok": true }`（token 正确；前端随后把它存入 localStorage 作为 Bearer 头）
- 401：`{ "error": "token 不正确。" }`

### 3.2 GET /api/sessions —— 会话列表

- 200：`{ "sessions": [ session, ... ] }`，按 `updatedAt` 倒序；无会话时空数组。

### 3.3 POST /api/sessions —— 新建会话

请求：

```json
{ "workDir": "/abs/path 可缺省", "providerId": "volcano", "modelId": "可缺省" }
```

- `workDir` 缺省 = 项目根目录；必须已存在且为目录，否则 400（`workDir 不存在或不是目录：...`）；
- `providerId` 必填，必须在 models.yaml 中存在，否则 400；`modelId` 可缺省（= provider 首个模型）；指定但不存在 → 400；
- **校验实现路径（审核 M4）**：Web 层调库 `loadModelConfigFromYaml(providerId, modelId)` 做预检（仅解析 yaml，无网络开销，不建 agent），`RuntimeError` 消息透传为 400。**不得依赖库的默认回退**：yaml 缺失时库会静默回退环境变量配置（providerId 被忽略），故 yaml 缺失时本接口一律 400 `config/models.yaml 不存在。`（审核 M3）；
- 200：创建好的 session 对象（§2.1）。此时**不创建 agent 实例、不写 jsonl**（惰性：首发消息时才建）；
- **非幂等（审核 L4）**：契约不提供幂等键，前端创建按钮需防重（点击后禁用至响应返回）。

### 3.4 PATCH /api/sessions/{sessionId} —— 重命名

请求：`{ "title": "新标题" }`（trim 后 1–60 字，否则 400）

- 200：更新后的 session 对象；404：会话不存在。

### 3.5 DELETE /api/sessions/{sessionId} —— 删除会话

- 200：`{ "ok": true }`；404：不存在；**409：该会话有活跃流，拒绝删除**；
- 副作用：删索引条目 + 删 `webData/sessionLogs/{sessionId}.jsonl` + 清 agent 缓存实例。

### 3.6 GET /api/sessions/{sessionId}/messages —— 历史消息

- 200：`{ "messages": [ message, ... ] }`（§2.2）；会话存在但无 jsonl（从未发消息）→ 空数组；404：会话不存在。

### 3.7 GET /api/sessions/{sessionId}/pending —— 查询挂起的工具确认（审核 H1，防待确认死锁）

**存在理由**：`pendingConfirm` 只在 agent 内存。挂起确认时 assistantMessage（含 toolCalls）已落 jsonl 而 toolResult 未落盘——刷新页面后历史回放只看到「未配对 toolCall」，前端无从知道服务端挂着 pending；此时发新消息只会收到 `error(pendingConfirmationExists)`，且 error 帧不含 confirmationId（库零改动约束，不能加字段），会话将永久卡死。本端点提供契约内恢复途径。

- 200：无挂起 → `{ "pending": null }`；有挂起 → `{ "pending": { "confirmationId": "confirm_...", "reason": "...", "commandPreview": "...", "toolCall": {"id","toolName","arguments"} } }`（与 SSE `confirmationRequired` 帧 data 同构；`commandPreview` 由 Web 层用 `agent.buildToolPreview` 重建）；
- 404：会话不存在；
- 数据源：agent 缓存实例的 `conversation.pending`（取 `toolCalls[currentIndex]` 为当前待确认调用）；
- **前端调用时机**：① 进入/刷新会话页时（与 GET messages 并行）；② 任何流收到 `errorType == "pendingConfirmationExists"` 时。

### 3.8 GET /api/usage —— 用量汇总

- 200：§2.3 结构。无副作用。

### 3.9 GET /api/models —— 读模型配置

- 200：§2.4 结构（apiKey 已脱敏）；
- **实现口径（审核 M3）**：原始 yaml 读取 + §2.4 宽松结构校验（apiKey 允许为空），**不使用** `loadModelConfigFromYaml`——库解析器必须指定单个 providerId、apiKey 缺失直接 raise、yaml 缺失时回退环境变量而非报错，三者都与本端点语义冲突；
- 400：`config/models.yaml 不存在。`（缺失时）；yaml 语法错误 → 400 透传 yaml 解析消息。

### 3.10 PUT /api/models —— 写模型配置

请求体：§2.4 结构（apiKey 按脱敏规则回传）。

- 200：`{ "ok": true }`；
- 400：校验失败（§2.4 规则，消息中文指明具体字段）；
- 副作用：备份 `models.yaml.bak` → 合并式写回 → 原子替换 → agent 缓存标记失效（下次 getAgent 惰性重建）；
- **yaml 缺失时（审核 L5）**：以空文档为基底创建（等价于全新配置）。

### 3.11 GET /api/health —— 探活

- 200：`{ "ok": true, "version": "0.1.0" }`（需认证）。

## 4. SSE 流式接口

### 4.1 POST /api/chat/stream —— 发起对话

请求：

```json
{ "sessionId": "session_0bcd11873ded", "message": "用户消息" }
```

预检（失败走 REST 错误，不开流）：sessionId 非法 → 400；会话不存在 → 404；`message` trim 后为空 → 400；该会话有活跃流 → 409。

通过后返回 `text/event-stream`，事件序列见 §4.3。

### 4.2 POST /api/chat/confirm —— 工具确认续跑

请求：

```json
{ "sessionId": "session_0bcd11873ded", "confirmationId": "confirm_a1b2c3d4e5f6", "approved": true }
```

预检同 §4.1（message 校验换成：`confirmationId` 非空字符串、`approved` 为布尔，否则 400）。

通过后返回 `text/event-stream`，事件序列同 §4.3。

**confirmationId 不匹配**：库语义为「pending 不被消费、发 error 事件」——前端收到 `error` 帧（`errorType: "confirmationMismatch"`）后应**重新拉取 messages 刷新**（挂起的确认可能已因模型配置变更清缓存而失效，按 dangling 呈现，webAppPlan §4.7-M4）。

### 4.3 SSE 事件集（两个流接口共用）

| event | data（JSON） | 说明 |
|---|---|---|
| `textDelta` | `{ "text": "你" }` | 正文增量，多次到达，前端拼接 |
| `reasoningDelta` | `{ "text": "先分析..." }` | 思维链增量，渲染进折叠思考块 |
| `toolCallStart` | `{ "toolCall": {"id","toolName","arguments"}, "preview": "path=/xx" }` | 工具进入执行 |
| `toolCallEnd` | `{ "toolResult": {"toolCallId","toolName","isError","content","details"} }` | 工具完成；**拒绝路径会出现无配对 Start 的孤儿 End**（isError=true） |
| `confirmationRequired` | `{ "confirmationId": "confirm_...", "reason": "删除类命令需确认", "commandPreview": "rm -rf /tmp/x", "toolCall": {"id","toolName","arguments"} }` | **终态**。弹确认框；用 toolCall 先建「待确认」卡片 |
| `completed` | `{ "message": "完整回复全文" }` | **终态**。message 与已拼接的 textDelta 全文一致（前端可直接用拼接结果，不必替换） |
| `error` | `{ "message": "...", "errorType": "..." }` | **终态**。errorType 取值见下 |

`errorType` 取值（对齐库契约）：`pendingConfirmationExists` / `confirmationMismatch` / `maxStepsExceeded` / 模型调用异常类名（如 `modelRequestError`、`HTTPError`）/ 泵线程兜底异常类名。`emptyMessage` 仅库层兜底——REST 预检已拦截空消息，流内不可达，**前端无需为它写处理分支**（审核 L1）。

**典型事件序列**：

```
纯文本：      textDelta* → completed
免确认工具：  textDelta* → toolCallStart → toolCallEnd → textDelta* → completed
需确认批准：  textDelta* → confirmationRequired ‖（新流）toolCallStart → toolCallEnd → textDelta* → completed
需确认拒绝：  textDelta* → confirmationRequired ‖（新流）toolCallEnd(孤儿,isError=true) → textDelta* → completed
模型错误：    textDelta* → error
```

（`‖` 表示前一流结束、前端调 confirm 开新流；一轮内可能出现多次工具调用与多次确认。）

### 4.4 POST /api/chat/stop —— 停止生成

请求：`{ "sessionId": "session_0bcd11873ded" }`

- 200：`{ "stopped": true }`（置停止标志成功）或 `{ "stopped": false }`（该会话无活跃流，幂等不报错）；
- 404：会话不存在；
- **语义（webAppPlan §4.3-H1）**：非即时。SSE 连接在泵线程跑到下一个事件/当前 step 结束后关闭；停止后前端立即停渲染并给半截消息加「已中断」标记；半截文本不落 jsonl，刷新即消失。

## 5. 前端交互状态机（契约的消费侧约定）

```
[空闲] --发消息 POST stream--> [流式中]（输入禁用，按钮变停止）
[流式中] --textDelta/reasoningDelta--> 增量渲染
[流式中] --toolCallStart/End--> 工具卡片更新（含 dangling 归位，见下）
[流式中] --completed--> [空闲]（刷新会话列表：title/usage/updatedAt 已变）
[流式中] --error(pendingConfirmationExists)--> GET pending --> [待确认]（重弹框）
[流式中] --error(其它)--> [空闲] + 错误提示条
[流式中] --confirmationRequired--> [待确认]（弹框，输入框禁用）
[待确认] --批准/拒绝 POST confirm--> [流式中]（续流渲染进同一 assistant 块）
[待确认] --confirm 收到 confirmationMismatch--> 拉 messages 刷新 --> [空闲]
[流式中] --点停止 POST stop--> [停止中]（按钮禁用，等连接关闭）--> [空闲]
进入/刷新会话页 --> GET messages + GET pending（并行）
  -- pending 非 null --> [待确认]（重弹确认框，对应 toolCall 渲染「待确认」卡片而非 dangling 灰卡）
  -- pending null --> 按 §2.2 渲染历史
任意请求 401 --> 清 localStorage token --> [登录门]
```

- **dangling 重放归位（审核 M5）**：dangling 灰卡存在时发新消息，库会重放这些 toolCall，新流中出现**与灰卡同 id** 的 `toolCallStart/toolCallEnd`——前端命中同 id 卡片时**更新该卡片状态**（灰卡→执行中→完成/失败），不得新建重复卡片；重放触发的新 `confirmationRequired` 按正常待确认流程处理；
- 刷新/重进页面：`GET /api/sessions` 进侧栏；`#/chat/{id}` 时 `GET messages` + `GET pending` 恢复现场；
- 发送期间刷新页面：活跃流丢失但后端泵线程跑到终态；重进后拉 messages 即为最新完整状态（自愈，无需断线重连机制——本期不做 SSE 重连）。

## 6. 明确不在契约内（防前端误依赖）

- 无分页：sessions/messages 均全量返回（单用户规模）；
- 无 WebSocket、无 SSE 重连/断点续传；
- 无消息编辑/删除/重生成接口；
- 无 provider 连通性测试接口（保存配置后靠实际发消息验证）；
- `completed.message` 与 textDelta 拼接结果的一致性由库保证，前端不做 diff 校验。
