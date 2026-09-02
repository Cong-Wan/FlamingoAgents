# ChatGPT 与 xAI 订阅登录 Python 原生接入方案

Author: wilbur
Version: 1.2
Date: 2026-09-01
Description: 基于 pi 0.84.4 的现行实现，说明 ChatGPT Plus/Pro 与 xAI SuperGrok/X Premium 的 OAuth、凭据刷新、Responses 请求及多轮回放机制，并给出 FlamingoAgents 的 Python 原生落地计划；不依赖 pi/Node 运行时。v1.2 根据实施前二次独立审核，补充 Responses item 白名单序列化、terminal output 全类型权威合并、浏览器回调全局互斥与端口降级、取消终态 CAS、凭据路径防 symlink/owner 校验及跨模型工具链成对降级。

- 状态：代码与自动化测试已实施；真实 ChatGPT/xAI 账户烟测待用户凭据环境验收
- 调研基线：`@earendil-works/pi-coding-agent 0.84.4` / 内置 `@earendil-works/pi-ai`
- 目标工程：FlamingoAgents（Python >= 3.12、FastAPI、`urllib`、原生前端）
- 独立审核：`docs/codeReview/260901_subscriptionLoginPlan.md`（v1.1）、`docs/codeReview/260901_subscriptionLoginPlanV2.md`（v1.2）；实现审核：`docs/codeReview/260901_subscriptionLoginImplementation.md`

---

## 0. 结论

pi 没有把“订阅登录”当作静态 `apiKey`，而是拆成四层：

```text
Provider 认证声明
  → OAuth 登录（浏览器 PKCE / Device Code）
  → 独立凭据存储 + 到期自动刷新
  → Provider 专用 Responses 请求与 SSE/多轮状态转换
```

FlamingoAgents 应照此机制用 Python 原生实现，不引入 pi 或 Node 桥接：

```text
subscriptionAuth.py       OAuth 登录与刷新
credentialStore.py        ~/.flamingo/auth.json 安全存储
modelAuth.py              每次请求前动态解析有效认证
responsesAdapter.py       xAI Responses + ChatGPT Codex Responses
conversation.py           保存下一轮必须回放的 opaque response items
webApp/modelAuthManager.py Web 登录任务编排与状态查询
```

**认证代码本身很直接；真正不能省略的是 Responses 协议和多轮 opaque item 回放。** 只把订阅 Access Token 填入现有 `apiKey` 会同时缺失自动刷新、Codex 账户头、Responses 输入格式及 reasoning/tool item 回放，因此不是完整实现。

---

## 1. 目标、非目标与成功标准

### 1.1 目标

1. ChatGPT Plus/Pro 用户可通过浏览器 PKCE 或设备码登录，调用 ChatGPT Codex 模型。
2. SuperGrok/X Premium 用户可通过 RFC 8628 设备码登录，调用 xAI Grok 模型。
3. Access Token 到期前/到期后自动刷新，用户不需要把 Token 手工写进 `models.yaml`。
4. 保留现有静态 API Key Provider 和 `/chat/completions`，做到向后兼容。
5. Responses 模型支持文本、reasoning 摘要、工具调用、工具结果、多轮继续、停止和用量统计。
6. Web 设置页可登录、查看脱敏状态、取消登录、退出登录；CLI/SDK 也能共用同一凭据。

### 1.2 非目标

- 不把 pi 或 `pi-ai` 作为运行时依赖。
- 不实现 OpenAI 普通 API Key 的 Responses Provider；本期只服务 ChatGPT Codex 与 xAI。
- 首版不实现 Codex WebSocket/zstd 优化；先使用 pi 同样支持的 SSE 路径保证正确性。
- 不复制 pi 的动态模型目录更新、价格目录和全部兼容 Provider。
- 不把 Access/Refresh Token 返回给浏览器或写入 `models.yaml`、日志、调试输出。

### 1.3 可验证成功标准

| 编号 | 标准 |
|---|---|
| S1 | ChatGPT 浏览器登录后凭据落盘，重启 Web/CLI 后仍可调用 |
| S2 | ChatGPT 设备码登录在无本地浏览器环境可完成 |
| S3 | xAI 显示 user code/验证地址并按 interval 轮询，成功后可调用 Grok |
| S4 | 模拟即将过期或并发 401 时只刷新一次；强刷按失败 Access Token 双重检查，不重复旋转 Refresh Token |
| S5 | Codex 请求携带 Bearer、`chatgpt-account-id`、Responses Beta 头，命中 `/backend-api/codex/responses` |
| S6 | xAI 请求命中 `/v1/responses`，而不是 `/chat/completions` |
| S7 | 工具调用完成后下一轮正确发送 `function_call_output`，不会因 call/item ID 缺失报错 |
| S8 | reasoning 的 `encrypted_content` 原样保存并在同 Provider/模型下一轮回放 |
| S9 | 老 JSONL、老 `models.yaml` 和全部 `openai-completions` Provider 行为不变 |
| S10 | 前端、后端错误和 debug 日志中均不出现 Access/Refresh Token |

---

## 2. pi 的现行实现机制

### 2.1 公共认证链路

pi 的 Provider 同时声明：

- Provider ID、Base URL、模型及 API 适配器；
- 可用认证方式（API Key、OAuth 或两者）；
- OAuth 的 `login`、`refresh`、`toAuth` 实现。

运行链路为：

1. `/login` 枚举 Provider 的 OAuth 声明；
2. 登录实现完成授权，返回 `{access, refresh, expires, ...}`；
3. 凭据独立写入 `~/.pi/agent/auth.json`；
4. 模型请求前统一解析认证；
5. 凭据过期时，在 Provider 维度加锁、重新读取最新凭据、刷新并原子写回；
6. 最终只把有效 Access Token 和必要头交给模型适配器。

FlamingoAgents 应保留同样的职责边界：配置描述模型，凭据存储描述登录状态，请求层不直接管理登录 UI。

### 2.2 ChatGPT Plus/Pro：浏览器 PKCE

pi 使用公共客户端：

```text
client_id    = app_EMoamEEZ73f0CkXaXp7hrann
authorize    = https://auth.openai.com/oauth/authorize
token        = https://auth.openai.com/oauth/token
redirect_uri = http://localhost:1455/auth/callback
scope        = openid profile email offline_access
```

流程：

1. 生成 32 随机字节并做 Base64URL，得到 `code_verifier`；
2. `BASE64URL(SHA256(code_verifier))` 得到 `code_challenge`；
3. 生成独立随机 `state`；
4. 打开授权 URL，附带：
   - `response_type=code`
   - `code_challenge_method=S256`
   - `id_token_add_organizations=true`
   - `codex_cli_simplified_flow=true`
   - `originator=pi`（按 pi 0.84.4 兼容基线）
5. 临时监听 `127.0.0.1:1455/auth/callback`；
6. 回调必须先校验 `state`，再读取 `code`；
7. 以 `application/x-www-form-urlencoded` 请求 Token：

```text
grant_type=authorization_code
client_id=<client_id>
code=<authorization code>
code_verifier=<code_verifier>
redirect_uri=http://localhost:1455/auth/callback
```

8. 保存 `access_token`、`refresh_token`、`expires_in`；
9. Base64URL 解码 Access Token JWT payload，从以下私有 claim 提取账户 ID：

```text
payload["https://api.openai.com/auth"]["chatgpt_account_id"]
```

`accountId` 是 Codex 模型请求的必需头。这里只是读取 claim，不在客户端把“解码”当作签名验证；Token 真伪最终由 OpenAI 服务端验证。

**登录和每次 Refresh 成功后都必须从新的 Access Token 重新提取 `accountId`。** 缺失时拒绝写入；`access/refresh/expires/accountId` 必须原子替换。请求使用的账户头只能来自当前 Access Token 对应的新凭据，不能复用刷新前的旧值。

浏览器回调端口占用或远程访问时，pi 还允许用户粘贴完整回调 URL、`code#state` 或裸 code。Python 版本保留此回退，并在提供 state 时强制比对。

### 2.3 ChatGPT Plus/Pro：设备码模式

pi 的 Codex 设备码不是标准 xAI 那组端点，流程为：

```text
POST https://auth.openai.com/api/accounts/deviceauth/usercode
body: {"client_id":"..."}
```

返回 `device_auth_id`、`user_code`、`interval` 后，让用户访问：

```text
https://auth.openai.com/codex/device
```

随后轮询：

```text
POST https://auth.openai.com/api/accounts/deviceauth/token
body: {"device_auth_id":"...", "user_code":"..."}
```

- 403/404、`deviceauth_authorization_pending`：继续轮询；
- `slow_down`：增加等待；
- 成功：返回 `authorization_code` 与 `code_verifier`。

最后仍走普通授权码交换，但 `redirect_uri` 改为：

```text
https://auth.openai.com/deviceauth/callback
```

总超时按 pi 设为 15 分钟。Device Code 公共轮询规则为：缺省 interval 5 秒、最小 1 秒；OpenAI Codex 获取 device auth 后允许立即第一次 poll；`slow_down` 带 interval 时采用服务端值，否则在当前间隔上增加 5 秒。

### 2.4 xAI：RFC 8628 Device Authorization Grant

pi 使用：

```text
client_id   = b1a00492-073a-47ea-816f-4c329264a828
device_code = https://auth.x.ai/oauth2/device/code
token       = https://auth.x.ai/oauth2/token
scope       = openid profile email offline_access grok-cli:access api:access
```

设备授权请求为 Form：

```text
client_id=<client_id>
scope=<scope>
referrer=pi
```

首版保持 pi 0.84.4 的兼容值 `referrer=pi`，并把该值集中为常量；在没有真实契约证据前不自行改名。

响应中的 `verification_uri` / `verification_uri_complete` 必须解析并限制为 `https:`，防止恶意响应诱导打开本地协议。轮询 Token：

```text
grant_type=urn:ietf:params:oauth:grant-type:device_code
client_id=<client_id>
device_code=<device_code>
```

状态处理：

- 首次 poll 前必须先等待 interval；interval 缺失用 5 秒，最小 1 秒；
- `authorization_pending`：按原 interval 继续；
- `slow_down`：使用服务端新 interval，缺省则在当前间隔上增加 5 秒；
- `access_denied` / `authorization_denied`：终止；
- `expired_token`：终止并提示重新登录；
- 其它错误：带 HTTP 状态和 OAuth error 描述终止。

xAI 保存过期时间时提前 5 分钟：

```text
expiresAt = now + expires_in - 300 seconds
```

刷新请求：

```text
grant_type=refresh_token
client_id=<client_id>
refresh_token=<stored refresh token>
```

xAI 刷新响应可能不返回新的 `refresh_token`，此时必须保留旧值；不可把它覆盖成空。

### 2.5 ChatGPT Codex 模型请求

Provider：

```text
baseUrl = https://chatgpt.com/backend-api
POST      https://chatgpt.com/backend-api/codex/responses
```

最低正确请求头：

```http
Authorization: Bearer <access token>
chatgpt-account-id: <accountId>
OpenAI-Beta: responses=experimental
Accept: text/event-stream
Content-Type: application/json
originator: pi
User-Agent: FlamingoAgents/<version>
session-id: <sessionId>             # 有稳定 sessionId 时
x-client-request-id: <sessionId>    # 有稳定 sessionId 时
```

请求体骨架：

```json
{
  "model": "<modelId>",
  "store": false,
  "stream": true,
  "instructions": "<system prompt>",
  "input": [],
  "text": {"verbosity": "low"},
  "include": ["reasoning.encrypted_content"],
  "tool_choice": "auto",
  "parallel_tool_calls": true,
  "prompt_cache_key": "<sessionId>",
  "tools": [],
  "reasoning": {"effort": "high", "summary": "auto"}
}
```

要点：

- System Prompt 放 `instructions`，不重复放入 `input`；为空时提供非空兜底。
- `store` 必须为 `false`。
- 首版只做 SSE；WebSocket continuation、zstd 压缩属于性能优化，不影响协议正确性。
- 401 且尚未产生任何 SSE 内容时，携带本次失败的 `staleAccess` 强制刷新并只重试一次；锁内若发现当前 Access Token 已变化，说明其它请求已经刷新，直接复用，不再次旋转 Refresh Token。产出内容后禁止重跑，避免正文/工具副作用重复。

### 2.6 xAI 模型请求

Provider：

```text
baseUrl = https://api.x.ai/v1
POST      https://api.x.ai/v1/responses
Authorization: Bearer <access token 或 XAI_API_KEY>
```

请求使用标准 OpenAI Responses 结构：

```json
{
  "model": "<modelId>",
  "input": [],
  "stream": true,
  "store": false,
  "tools": [],
  "tool_choice": "auto",
  "reasoning": {"effort": "high", "summary": "auto"},
  "include": ["reasoning.encrypted_content"]
}
```

xAI 当前 Grok 推理模型也依赖 encrypted reasoning replay；不能因为它的 URL 看起来 OpenAI-compatible，就继续走现有 Chat Completions 适配器。

### 2.7 Responses SSE 与下一轮回放

需要消费的核心事件：

| 事件 | 动作 |
|---|---|
| `response.created` | 记录 response ID（诊断用，不作为跨轮唯一状态） |
| `response.output_item.added` | 建立 reasoning/message/function_call 临时槽位 |
| `response.reasoning_summary_text.delta` / `response.reasoning_text.delta` | 产出 `reasoningChunk` 并累积展示内容 |
| `response.reasoning_summary_part.done` | 追加段落分隔，保持多段摘要完整 |
| `response.output_text.delta` / `response.refusal.delta` | 产出 `textChunk`；拒答文本不能丢失 |
| `response.function_call_arguments.delta` | 累积工具 JSON 参数 |
| `response.function_call_arguments.done` | 用完整 `arguments` 覆盖流式半成品并重新解析 JSON |
| `response.custom_tool_call_input.delta/done` | 首版不支持 custom tool：收到即以 `unsupportedResponseItem` 明确失败，不静默忽略或执行 |
| `response.output_item.done` | 更新临时槽位，并按类型白名单生成可持久化 item；不保存只允许出现在响应中的字段 |
| `response.completed` / Codex `response.done` | 统一为 completed；以 terminal `response.output` 为全部支持 item 的最终权威状态，再读取 usage、结束原因并生成 `finalChunk` |
| `response.incomplete` | 按原因映射 length/error，保留已完成输出 |
| `response.failed` / `error` | 转 `modelRequestError` |

Responses 下一轮不是简单的 role/content 回放。至少要保留：

- reasoning item 的 `id`、summary 和 `encrypted_content`；
- assistant message item 的 `id`；
- function call item 的 `id` 与 `call_id`；
- 工具结果对应的 `function_call_output.call_id`。

这些字段属于 Provider opaque data，但**不能把完整服务端 item 未经筛选直接写盘并重发**。持久化和下一轮发送统一经过类型白名单 serializer：reasoning 仅保留 `id/type/summary/encrypted_content`，message 仅保留 `id/type/role/content/phase`，function call 仅保留 `id/type/name/call_id/arguments`，function call output 仅保留 `type/call_id/output`；`status`、内部元数据和未知字段一律不进入下一轮。业务层不解析、不修改 `encrypted_content`。

terminal `response.output` 是全部支持 item 的最终权威状态。部分服务可能不发 `response.output_item.done`，或只在 terminal 给出完整 message、function call、arguments/call ID、reasoning encrypted content。finalize 时按 item ID 合并所有支持类型：terminal 的完整字段覆盖增量半成品，新增 item 补建 completion；之后再通过白名单 serializer 写入 `providerData.responseItems`。

---

## 3. FlamingoAgents 现状差距

| 位置 | 现状 | 必要修改 |
|---|---|---|
| `modelConfig.py` | 只允许 `openai-completions`，强制 `apiKey` | 增加 API/认证类型并保持旧配置默认值 |
| `modelAuth.py` | 只拼静态 Bearer | 改为每次请求动态解析、刷新 OAuth |
| `builder.py` | 固定创建 `chatCompletionsAdapter` | 按 `api` 分派适配器 |
| `chatCompletions.py` | 固定 `/chat/completions` | 保留不动；新增 Responses 适配器 |
| `types.py` | message/toolCall 无 opaque Provider 数据 | 增加向后兼容的 `providerData` |
| `conversation.py` | JSONL 不保存 reasoning item / message item ID | 保存并恢复 `providerData` |
| `ports.py` | 请求没有 sessionId | 给适配器传稳定 sessionId 供缓存/请求头使用 |
| `modelConfigStore.py` | Web 只接受一个 API 且把认证等同 apiKey | 支持 `auth` 字段及 OAuth Provider 无 apiKey |
| `settingsView.js` | 只有 apiKey 输入框 | 增加订阅登录状态及登录/退出入口 |
| `server.py` | 无模型 OAuth 路由 | 增加登录任务 API；不能复用应用自身 `/api/auth/login` |

---

## 4. 已选设计

### D1. Python 原生，不依赖 pi 运行时

只提取协议和状态机；HTTP 继续使用项目现有标准库风格。OAuth 与 SSE 都可由 `urllib`、`http.server`、`threading`、`hashlib`、`secrets` 完成，不新增生产依赖。

### D2. 模型配置与凭据彻底分离

`models.yaml` 仅描述 Provider：

```yaml
providers:
  openaiCodex:
    baseUrl: https://chatgpt.com/backend-api
    api: openai-codex-responses
    auth: oauth
    models:
      - id: gpt-5.4
        name: GPT-5.4 Codex Subscription
        input: [text, image]
        contextWindow: 272000
        maxTokens: 128000
        reasoning: true
        reasoningEffort: high
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}

  xai:
    baseUrl: https://api.x.ai/v1
    api: openai-responses
    auth: oauth
    models:
      - id: grok-4.6
        name: Grok 4.6 Subscription
        input: [text, image]
        contextWindow: 500000
        maxTokens: 500000
        reasoning: true
        reasoningEffort: high
        cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
```

兼容规则：

- `configProviderId` 是用户在 YAML 中自定义的键（如 `openaiCodex`），用于会话选择和展示。
- `authProvider` 是解析后的 canonical key，只允许 `openai-codex` / `xai`：`openai-codex-responses` 映射前者，本期 `openai-responses` 映射后者。凭据和 OAuth 锁永远按 `authProvider`，因此多个 YAML Provider 可共享同一订阅登录。
- 缺少 `auth` 时默认为 `api-key`；现有配置零修改继续工作。
- `api-key` 才要求 `apiKey`。
- `openai-codex-responses + oauth` 固定使用 OpenAI Codex 凭据。
- `openai-responses + oauth` 本期固定使用 xAI 凭据。
- xAI 若选择 `auth: api-key`：先按现有规则解析 YAML `apiKey`；缺失时只对 canonical xAI 回退 `XAI_API_KEY`，其它旧 Provider 不扩大隐式环境变量范围。
- 模型元数据已对齐 pi 0.84.4 当前目录，但最终可用模型和额度仍由服务端账户决定；本期不做在线目录同步。

### D3. 凭据文件与并发刷新

路径：

```text
~/.flamingo/auth.json
~/.flamingo/auth.lock
```

结构：

```json
{
  "version": 1,
  "providers": {
    "openai-codex": {
      "type": "oauth",
      "access": "...",
      "refresh": "...",
      "expires": 0,
      "accountId": "..."
    },
    "xai": {
      "type": "oauth",
      "access": "...",
      "refresh": "...",
      "expires": 0
    }
  }
}
```

写入要求：

1. `~/.flamingo` 权限 0700；凭据/锁文件 0600；用 `lstat` 拒绝 symlink、非当前 uid 和异常文件类型。
2. 同进程使用每 Provider `threading.Lock`；跨进程始终锁稳定的 `auth.lock`（不锁会被 replace 的 `auth.json` inode）；login/write、logout/delete、refresh 均覆盖完整 read-modify-write。
3. 刷新获得锁后必须重新读文件：即将过期场景重查有效期；401 强刷场景比较 `staleAccess`，若当前 Access 已变化则直接使用新值。
4. 临时文件在目标目录排他创建并直接设 0600，写入后 flush、`fsync`、`os.replace`，再 `fsync` 父目录；不得原地 truncate。
5. Refresh Token 轮换时，Access/Refresh/Expiry 必须作为一个原子对象写入；OpenAI 还必须把从新 Access 提取的 AccountId 放在同一原子对象中。
6. JSON 损坏时保留原文件并报明确错误，不用空对象静默覆盖。

### D4. 每次请求前动态认证

不在 `createAgent()` 时把 Access Token 固化进 adapter。`modelAuthResolver.resolve(config, forceRefresh=False, staleAccess=None)` 在每次 HTTP 请求前：

1. `api-key`：解析原有 key，返回 Bearer；
2. `oauth`：按 canonical `authProvider` 读取对应凭据；
3. `now + 5min >= expires`：进入 Provider 锁，锁内重读并再次判断，仍即将过期才刷新；
4. 401 强刷：传入本次失败的 `staleAccess`，锁内若 `current.access != staleAccess` 则复用其它请求已刷新的凭据，否则执行一次 refresh；
5. 返回 `modelAuth(authorizationHeader, extraHeaders)`；
6. Codex 的 `extraHeaders` 包含与当前 Access 匹配的 `chatgpt-account-id`。

xAI 虽在写入时已有 5 分钟 skew，仍统一走 minimum-validity 检查；重复保护优于请求中途过期。

这样 Web 登录、退出、Token 刷新和长期 agent 缓存不会互相污染。

### D5. 一个 Responses 适配器，两种请求策略

新增一个 `responsesAdapter`，共享：

- message/tool schema 转换；
- SSE 解码；
- output item 累积；
- usage 归一化；
- `modelCompletion/finalChunk` 构造。

按 `config.apiType` 只分叉：

| 项 | OpenAI Codex | xAI |
|---|---|---|
| URL | `/codex/responses` | `/responses` |
| System | `instructions` | `input` 中 system/developer message |
| 特殊头 | account ID、Beta、originator | 无 |
| `include` | encrypted reasoning | encrypted reasoning |
| Auth | OAuth only | OAuth 或 API Key |

不要改写现有 `chatCompletionsAdapter`，避免回归已有 Provider。

### D6. Opaque Provider 数据向后兼容扩展

扩展数据结构：

```python
@dataclass
class toolCall:
    id: str
    toolName: str
    arguments: dict[str, Any]
    providerData: dict[str, Any] = field(default_factory=dict)

@dataclass
class chatMessage:
    ...
    providerData: dict[str, Any] = field(default_factory=dict)
```

assistant 的 `providerData` 至少保存：

```json
{
  "api": "openai-codex-responses",
  "authProvider": "openai-codex",
  "configProviderId": "openaiCodex",
  "model": "gpt-5.4",
  "responseItems": ["按类型白名单规范化后的可回放 item"]
}
```

回放规则：

- exact replay 只比较 `api + authProvider + model`；`configProviderId` 仅用于展示/排查，不影响凭据共享与协议兼容判断。
- exact match 也必须再次经过 serializer，只允许 reasoning/message/function_call/function_call_output 的协议白名单字段；未知字段和 `status` 不发送。
- 同 Responses API 但切换模型：丢弃 reasoning opaque item和旧 item ID；function call 与其后续 output 只有在 `call_id` 可可靠配对时才成对重建。
- 不得发送没有前置 call 的孤立 `function_call_output`；缺 call、缺 result 或无法可靠配对时，将整组调用/结果转换为带明确标记的普通文本，不能只保留其中一半。
- 切回 Chat Completions：忽略 `providerData`，沿用 role/content/toolCalls。
- 老 JSONL 没有字段时默认为 `{}`。

`providerData` 只允许 JSON 类型；写盘前不放请求头、Token、未筛选的服务端 item 或整个 HTTP 响应。

### D7. Web 登录采用内存任务，不阻塞 HTTP 请求

新增模型认证 API，与 Web 应用自身登录区分：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/modelAuth` | 返回 OpenAI/xAI 的 `loggedIn/expiresAt/accountHint`，绝不返回 Token |
| POST | `/api/modelAuth/{provider}/login` | 启动登录；OpenAI body 可选 `method=browser/device_code` |
| GET | `/api/modelAuth/logins/{loginId}` | 获取 pending/authUrl/deviceCode/completed/error |
| POST | `/api/modelAuth/logins/{loginId}/manualCode` | 浏览器回调不可达时提交 URL/code |
| DELETE | `/api/modelAuth/logins/{loginId}` | 取消未完成登录 |
| DELETE | `/api/modelAuth/{provider}` | 退出并删除对应凭据 |

`modelAuthManager.py`：

- 使用后台线程执行等待回调/设备码轮询；所有 OAuth HTTP 请求设置有限超时，网络返回后、Token 交换前和凭据写入前均复查取消状态。
- 内存任务用随机 `loginId`，含取消 Event；状态更新在锁内做终态 CAS，`cancelled` 后迟到 worker 不得转 `completed` 或写凭据；worker 退出后，完成/失败任务保留 10 分钟再清理。
- 同 Provider 同时只允许一个登录任务，第二个请求返回 409；OpenAI browser 额外使用进程内全局互斥和跨进程固定 callback lock，避免多个任务/进程争抢 1455。
- 服务重启只丢失未完成任务，不影响已落盘凭据；多 worker 不受支持，浏览器回调只由单一认证协调进程承载。
- 任务状态只保存 URL、user code、错误摘要，不保存可返回前端的 Token；不得返回 `code_verifier`。
- 浏览器登录时后端尝试监听 `127.0.0.1:1455`：bind/锁失败立即降级为 `manualCodeRequired`，不让整个登录失败；成功、取消、超时、异常均统一 `shutdown/server_close`。浏览器与后端同机可自动回调；远程浏览器的 `localhost` 指向用户机器，通常必须通过 `manualCode` 提交完整回调 URL/code，或改用设备码。

### D8. CLI 共用同一实现

新增薄入口 `modelLogin.py`：

```bash
uv run python modelLogin.py login openai-codex --method browser
uv run python modelLogin.py login openai-codex --method device-code
uv run python modelLogin.py login xai
uv run python modelLogin.py status
uv run python modelLogin.py logout xai
```

CLI 只负责打印 URL/code 和读取人工输入，OAuth、存储、刷新均调用库代码，不做第二套实现。

### D9. OAuth 公共客户端不是项目私密 Secret

上述 Client ID 来自 pi 当前实现，属于 installed/public client，不能也不需要作为 Secret 隐藏。但它们及端点可能由上游调整：

- 全部常量集中在 `subscriptionAuth.py`；xAI `referrer` 与 Codex `originator` 首版均保持兼容值 `pi`，User-Agent 使用 FlamingoAgents 版本；
- 错误消息保留 HTTP 状态与 OAuth error code，不打印 Token；
- 文档标注基线版本；
- 真实登录测试作为人工/可选集成测试，不在 CI 使用真实账户。

---

## 5. 详细实施改动

### 5.1 新增 `flamingoAgents/models/credentialStore.py`

- `oauthCredential` 数据结构和严格 JSON 校验。
- `readCredential(provider)`、`writeCredential(provider, credential)`、`deleteCredential(provider)`。
- 0700/0600 权限、稳定锁文件覆盖完整 read-modify-write、排他临时文件、原子替换与父目录 fsync。
- 目录/锁/凭据用 `lstat` 校验非 symlink、归当前 uid 且文件类型正确。
- 只接受 `openai-codex`、`xai` canonical key，未知键读取可保留但不执行。

### 5.2 新增 `flamingoAgents/models/subscriptionAuth.py`

- PKCE/Base64URL/JWT claim 工具。
- OpenAI 浏览器、OpenAI device code、xAI device code。
- 两类 Refresh Token 流程；OpenAI 登录与刷新都从新 Access 重提 `accountId`，缺失即失败并禁止覆盖旧凭据。
- 可取消、超时、interval/slow_down 状态机：默认 5 秒、最小 1 秒、slow_down 缺省 +5 秒；xAI 首 poll 前等待，OpenAI device 可立即首 poll。
- 所有 OAuth HTTP 请求使用有限 timeout；网络返回后和返回凭据前复查取消，调用方写盘前再复查，消除 cancel/token-success 竞争。
- OpenAI browser callback 使用全局互斥；1455 bind 失败保留 manual-code 能力，所有出口关闭 listener。
- `resolveOAuthCredential(provider, forceRefresh=False, staleAccess=None)` 实现 minimum-validity 与 401 双重检查锁。
- 所有 HTTP 错误统一转 `modelAuthError(provider, action, statusCode, errorCode)`，字符串化时先做敏感字段剔除。

### 5.3 修改 `flamingoAgents/models/modelAuth.py`

- 保留 `createModelAuth(apiKey)` 给旧 Provider。
- 新增动态 `modelAuthResolver`。
- `modelAuth` 增加 `headers`，但不在对象的 `repr` 暴露完整 Bearer；建议 `repr=False`。
- 认证错误与模型协议错误分开，前端可提示“请重新登录”。

### 5.4 修改 `flamingoAgents/models/modelConfig.py`

- `allowedApi` 扩为：
  - `openai-completions`
  - `openai-responses`
  - `openai-codex-responses`
- 新增 `authType`，缺省 `api-key`；resolved config 同时保存用户的 `configProviderId` 与按 API 映射出的 canonical `authProvider`。
- `resolvedModelConfig.apiKey` 改为可空；只有 API Key 模式解析它。
- canonical xAI 的 API Key 解析顺序为 YAML 明文/环境变量引用 → 缺失时 `XAI_API_KEY`；不影响其它 Provider。
- `apiType` 保存 YAML 中真实协议，不再统一写 `openaiCompatible`。
- 对不合法组合给明确错误，例如 Codex + api-key、Responses OAuth 但 Base URL 非 xAI。

### 5.5 新增 `flamingoAgents/models/responsesAdapter.py`

实现：

1. `buildRequestPayload(messages, tools, sessionId)`；
2. Chat Completions tool schema → Responses function tool schema；
3. role messages → Responses input items；
4. opaque item 使用类型白名单 serializer 安全持久化/回放，并对非 exact 工具调用/result 成对降级；
5. `/responses` 与 `/codex/responses` URL/头分派；
6. SSE 增量解析和 `textChunk/reasoningChunk/finalChunk`：覆盖 summary/reasoning/refusal、function arguments delta+done、Codex `response.done` 归一化；custom tool 事件首版明确报不支持；
7. 以终态 `response.output` 为权威，按 item ID 合并 reasoning/message/function call，terminal 完整字段覆盖增量半成品并补建仅 terminal 出现的 item；随后经白名单 serializer 生成 `responseItems`;
8. usage 转成现有格式：

```text
input_tokens                     → prompt_tokens
input_tokens_details.cached_tokens → prompt_tokens_details.cached_tokens
output_tokens                    → completion_tokens
output_tokens_details.reasoning_tokens → completion_tokens_details.reasoning_tokens
```

9. HTTP 401 首字节前将失败 Token 作为 `staleAccess` 强制刷新并重试一次；锁内 Token 已变化则直接复用。其它重试继续由现有 agent 连接建立期策略处理。
10. `interruptActiveStreams()` 与现有 adapter 同样登记 response/socket，确保 Stop 可唤醒阻塞读取。

### 5.6 修改 `builder.py` 与 `ports.py`/`agent.py`

- builder 按 API 类型创建 adapter。
- `modelAdapterPort.complete/completeStream` 增加可选 `sessionId`；旧 adapter 接受但忽略。
- `agent.driveModelLoop` 把当前 `sessionId` 传给 adapter。
- Session ID 限制到 64 字符内；当前生成格式天然满足，仍在 adapter 边界夹紧。

### 5.7 修改 `types.py` 与 `conversation.py`

- `toolCall/chatMessage` 增加默认空 `providerData`。
- assistant JSONL 事件增加 `providerData`。
- resume 严格恢复 JSON 对象；错误类型降级为空对象而不是让历史崩溃。
- `reasoning` 字段继续只作为可视摘要，不注入普通 content；opaque reasoning 走 `providerData`。
- 工具结果仍以业务 `toolCall.id=call_id` 关联；服务端 item ID 放 `toolCall.providerData.itemId`。

### 5.8 修改 Web 后端

- 新增 `webApp/backend/modelAuthManager.py`。
- `server.py` 增加 §D7 路由，继续受 `authedApi` 保护。
- `modelConfigStore.py` 读写 `auth` 与三种 API，OAuth Provider 不要求 apiKey。
- 登录/退出成功后调用 `invalidateAllAgents()`；正在运行的请求不强杀，下次模型请求使用新认证。
- `GET /api/models` 只返回认证类型，不合并凭据状态；状态由 `/api/modelAuth` 单独提供。

### 5.9 修改 Web 前端

- 设置页 API 字段从只读改为三种协议下拉；认证字段显示 API Key/OAuth。
- OAuth 模式隐藏 apiKey，显示“未登录 / 已登录 / 即将过期 / 已过期”。
- OpenAI 提供“浏览器登录”“设备码登录”；xAI 提供“订阅登录”。
- 登录面板展示 URL、user code、倒计时、状态和取消；浏览器模式提供手工粘贴回调 URL。
- `api.js` 增加 modelAuth API。
- Token 永不进入 DOM、localStorage 或浏览器网络响应。

### 5.10 更新文档与示例

- `config/models.example.yaml` 增加两个订阅 Provider 示例，并说明账户权益决定模型可用性。
- README 增加登录命令和 Web 入口；实施时只追加本功能内容，不覆盖当前工作区已有 README 修改。
- 文件头按项目规则递增小版本并描述本次修改。

---

## 6. 测试计划

本项目当前没有自动化测试目录。实施时用 `uv` 增加 pytest 开发依赖，测试文件仍按项目小驼峰命名。

### 6.1 单元测试

新增：

- `tests/testCredentialStore.py`
  - 权限、原子写、损坏 JSON、不丢未知 Provider；
  - symlink/错误 owner/异常文件类型拒绝，replace 后父目录 fsync；
  - 两线程/两进程竞争刷新只保留完整凭据；login/refresh/logout 三方竞争不丢更新。
- `tests/testSubscriptionAuth.py`
  - PKCE 长度和 challenge；
  - OpenAI callback state mismatch、1455 端口占用降级、取消后再次登录、跨进程 callback lock；
  - JWT Base64URL padding/accountId 提取；
  - OpenAI device pending/slow_down/success/timeout，且允许立即首 poll；
  - xAI pending/slow_down/denied/expired，且首 poll 前等待、默认/最小 interval 正确；
  - xAI 刷新不返回 refresh_token 时保留旧值；
  - OpenAI refresh 产生新 accountId 时凭据和请求头同时换新；
  - `expires=now+60s` 触发 minimum-validity refresh；
  - 两个并发 401 使用同一 staleAccess 时 Refresh HTTP 只调用一次；
  - OAuth HTTP 均有有限 timeout；取消与 Token 成功同时发生时 cancelled 任务不能写凭据。
- `tests/testResponsesAdapter.py`
  - Codex/xAI URL、头和请求体快照；
  - 文本/reasoning/refusal/tool 参数跨 SSE chunk 拼接；
  - `function_call_arguments.done` 覆盖半包 JSON，Codex `response.done` 正常终止；
  - terminal output 覆盖完整 message/function arguments/call ID，且缺少 `output_item.done` 时补建 completion；
  - custom tool 事件返回可识别的不支持错误；
  - usage 映射；
  - response.failed/incomplete；
  - 401 刷新仅在零 chunk 时重试一次；
  - stop 中断阻塞读。
- `tests/testResponsesReplay.py`
  - encrypted reasoning 完整 round-trip；`output_item.done` 缺密文而 terminal output 带密文时可回填；
  - item 白名单过滤未知字段、`status`、空密文和跨版本字段；
  - item ID/call ID/function_call_output 配对；
  - JSONL resume 后第二轮 payload 与首次内存态一致；
  - 切模型/切 Provider 时不回放不兼容 opaque item，function call/output 成对重建；
  - 缺失 tool result、孤立 tool result 不产生孤立 `function_call_output`；
  - 老 JSONL 兼容。
- `tests/testModelConfigAuth.py`
  - 老配置默认 API Key；
  - OAuth 无 apiKey 合法；
  - 非法 API/auth 组合拒绝。

所有 OAuth/模型 HTTP 用本地假服务或 monkeypatch，CI 不访问真实服务。

### 6.2 Web API 测试

- 未带 Flamingo Web Token 访问 modelAuth 路由 → 401。
- 登录状态响应不含 `access`、`refresh`、`authorization`、`code_verifier`。
- 同 Provider 重复启动登录 → 409。
- cancel 后后台轮询停止；取消与 Token 成功竞争时终态保持 cancelled 且不写凭据。
- 浏览器 1455 端口占用时任务进入 manual-code 状态，取消后 listener/全局锁释放，可再次登录。
- logout 删除凭据，下一模型请求返回可识别的 `modelAuthError`。
- `PUT /api/models` 对旧配置和新认证类型均正确合并。
- 构造特征 Access/Refresh Secret，触发登录、刷新和模型 401 错误；断言 Web 响应、`repr`/异常字符串、debug buffer、`modelError` JSONL 均不含 Secret。

### 6.3 人工真实账户验收

真实登录不可进 CI，实施者使用自己的测试账户手工验证：

1. ChatGPT browser → 一轮文本 → 一轮工具 → 第二轮继续 → 重启后继续。
2. ChatGPT device code 重复上述最小文本调用。
3. xAI device code → 一轮文本 → 工具调用 → 第二轮继续。
4. 将测试凭据 expiry 改为 60 秒后 → 发请求 → minimum-validity 触发且只刷新一次。
5. 两个 Web 窗口使用同一失效 Access 同时触发 401 → Refresh 只发生一次，凭据文件完整。
6. 流式中点击停止 → 无后续 chunk，下一轮 transcript 仍能按现有中断闭合机制继续。
7. 退出登录 → UI 状态立即变化，日志和浏览器响应无 Token。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 上游公共 Client ID/端点变化 | 常量集中、记录 pi 基线、错误保留 OAuth code、真实烟测 |
| 订阅账户没有目标模型权益 | UI 明示“登录成功不等于模型可用”，透传服务端限额/权限错误 |
| Refresh Token 并发旋转导致旧值覆盖新值 | Provider 级线程锁 + 文件锁 + expiry/staleAccess 锁内重查 + 原子整体写 |
| Token 泄漏 | 独立 0600 文件；响应、repr、debug、JSONL 全面排除 |
| 回调 state/恶意 verification URI | 强制 state 比对；设备验证链接只允许 HTTPS |
| 固定 1455 端口/多进程竞争 | browser callback 全局线程锁 + 固定文件锁；占用时降级 manual code；所有出口关闭 listener |
| 取消时 urllib 仍在阻塞或迟到写凭据 | 有限 HTTP timeout + 网络返回/写盘前复查 + 任务终态 CAS |
| 凭据路径被 symlink 替换 | `lstat`/uid/type 校验；稳定 lock inode；排他临时文件 + 父目录 fsync |
| SSE 中途重试造成重复输出/工具副作用 | 仅零 chunk 401 刷新重试；其它沿用 chunkSeen 红线 |
| 丢失 encrypted reasoning/item ID 导致第二轮 400 | terminal output 全类型权威合并；白名单 item 落 JSONL 并做 resume round-trip 测试 |
| Provider/模型切换回放不兼容 opaque data | 仅 exact `api+authProvider+model` 白名单回放；其余工具 call/output 成对降级，禁止孤立 output |
| Codex SSE 性能不及 WebSocket | 首版正确性优先；后续独立增加 WS/zstd，不污染认证层 |
| Session JSONL 含 encrypted reasoning | 文档声明其为会话敏感数据，沿用用户目录权限；不含 Token |
| 临时 1455 端口被占 | 自动进入 manual-code 状态并释放 listener 资源，或改用设备码模式 |

---

## 8. 实施顺序与 TODO

### Phase 1：认证闭环

- [x] T1 新增 `credentialStore.py` 与并发/权限测试
- [x] T2 新增 OpenAI PKCE + device code + refresh
- [x] T3 新增 xAI device code + refresh
- [x] T4 改造动态 `modelAuthResolver`
- [x] T5 新增 `modelLogin.py`（真实账户烟测归 T21）

验收：S1–S4、S10。

### Phase 2：Responses 协议

- [x] T6 扩展 model config API/auth schema，保证旧配置测试通过
- [x] T7 新增 adapter factory 与 `responsesAdapter.py`
- [x] T8 完成 message/tool 请求转换和 Codex/xAI 头/URL
- [x] T9 完成 SSE text/reasoning/refusal/function call delta+done/terminal output/usage 解析
- [x] T10 接入 stop、零 chunk 401 refresh、现有 retry 策略

验收：S5–S7、S9。

### Phase 3：多轮持久化

- [x] T11 给 `chatMessage/toolCall` 增加 `providerData`
- [x] T12 JSONL 保存/恢复 response items
- [x] T13 exact `api+authProvider+model` 回放与跨模型降级
- [x] T14 工具结果 call ID 配对和 resume round-trip 测试

验收：S7–S9，重点 S8。

### Phase 4：Web 登录体验

- [x] T15 新增后台登录任务管理器和 modelAuth API
- [x] T16 扩展模型配置读写/API/auth 校验
- [x] T17 设置页登录状态、登录面板、manual code、取消/退出
- [x] T18 登录任务并发、持久化、取消竞态、Token 不泄漏自动化测试（真实浏览器双窗口归 T21）

验收：S1–S4、S10 的 Web 场景。

### Phase 5：文档与最终验收

- [x] T19 更新 `models.example.yaml`、README 与 Web API 契约（保留用户现有 README 内容）
- [x] T20 全量 `uv run pytest`（35 passed）
- [ ] T21 ChatGPT/xAI 各完成文本 + 工具 + 第二轮 + 重启烟测（需要用户真实订阅账户授权）
- [x] T22 检查 git diff：无 Token、无真实 auth.json、无无关格式化

---

## 9. 参考实现定位

本方案依据本机 pi 0.84.4 的以下实现提炼：

- `pi-ai/dist/auth/oauth/openai-codex.js`
- `pi-ai/dist/auth/oauth/xai.js`
- `pi-ai/dist/auth/oauth/device-code.js`
- `pi-ai/dist/auth/credential-store.js`
- `pi-ai/dist/auth/resolve.js`
- `pi-ai/dist/providers/openai-codex.js`
- `pi-ai/dist/providers/xai.js`
- `pi-ai/dist/api/openai-codex-responses.js`
- `pi-ai/dist/api/openai-responses.js`
- `pi-ai/dist/api/openai-responses-shared.js`
- `pi-coding-agent/dist/core/auth-storage.js`
- `pi-coding-agent/dist/core/model-runtime.js`

交叉确认来源：OpenAI 官方 Codex CLI OAuth/Responses 实现、xAI OIDC metadata。实现时以契约测试和真实烟测为最终判据，不机械复制 TypeScript 结构。
