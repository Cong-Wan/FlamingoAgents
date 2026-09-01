Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: subscriptionLoginPlan v1.0 方案审核（pi -p 默认模型独立审核），高/中/低共 13 项问题；v1.1 已按审核结论全部修订。

# 方案审核报告 — `docs/plan/subscriptionLoginPlan.md`

## 总体结论

方案总体方向正确：不把订阅 Token 当静态 `apiKey`、区分 OAuth/凭据存储/动态刷新/Responses 适配器/opaque replay，和 pi 0.84.4 的实现思路一致。
但仍有几处会直接影响落地正确性，尤其是并发强制刷新、Responses SSE 事件覆盖、provider canonical 映射、OpenAI `accountId` 刷新一致性。

本次只读审核，未修改文件。

---

## 高严重度问题

### H1. 401 强制刷新缺少“失败 Token”双重检查，会并发旋转 Refresh Token

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:67` S4
- `docs/plan/subscriptionLoginPlan.md:443-449` D4
- `docs/plan/subscriptionLoginPlan.md:614` 401 刷新

**问题**:
方案只描述了“过期时加锁刷新”，但 401 场景使用 `forceRefresh=True` 时，如果多个请求同时拿到同一个旧 access token 并同时 401，后进入锁的请求仍可能再次刷新，导致 refresh token 被重复旋转，旧 refresh 覆盖/失效。

pi 的普通过期刷新通过 store `modify` 做锁内重读；但“强制刷新”也必须带上本次失败的 access token 做比较。

**修正建议**:
落档为：

```text
resolve(config, forceRefresh=False, staleAccess=None)

当 forceRefresh=True 时：
1. 进入 provider 锁；
2. 重新读取凭据；
3. 若 staleAccess 不为空且 current.access != staleAccess，说明别的请求已刷新，直接返回 current；
4. 否则执行 refresh；
5. access/refresh/expires/accountId 原子写回。
```

同时测试补充：

```text
两个并发请求使用同一个 expired/revoked access 触发 401：
- refresh HTTP 只调用一次；
- 第二个请求锁内发现 access 已变化，直接复用新凭据。
```

---

### H2. Responses SSE 事件清单不完整，工具参数和拒绝输出可能解析错误

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:307-320`
- `docs/plan/subscriptionLoginPlan.md:675-681`

**问题**:
当前事件表只列了 `response.function_call_arguments.delta`，但 pi 0.84.4 还处理：

- `response.function_call_arguments.done`
- `response.custom_tool_call_input.delta`
- `response.custom_tool_call_input.done`
- `response.refusal.delta`
- `response.reasoning_text.delta`
- `response.reasoning_summary_part.done`
- Codex 的 `response.done` → 归一成 `response.completed`

缺少 `function_call_arguments.done` 时，最后一次完整 arguments 可能无法覆盖流式半成品，工具调用参数会不完整或 JSON 解析失败。缺少 `refusal.delta` 时，拒答文本可能丢失。

**修正建议**:
在 §2.7 和 §5.5 明确增加事件处理矩阵：

```text
response.function_call_arguments.done:
  用 event.arguments 覆盖累积参数，重新解析 JSON。

response.refusal.delta:
  当作 textChunk 输出并累积到 assistant content。

response.reasoning_text.delta:
  当作 reasoningChunk。

response.reasoning_summary_part.done:
  追加段落分隔，保持摘要展示完整。

response.done:
  Codex 兼容事件，按 response.completed 处理。

response.custom_tool_call_input.*:
  首版若不支持 custom tool，必须显式忽略并报可识别错误；
  若支持，则映射到 custom_tool_call/custom_tool_call_output。
```

测试补充 `function_call_arguments.done` 覆盖半包 JSON、`refusal.delta` 输出文本、Codex `response.done` 终止。

---

### H3. OpenAI Codex `accountId` 刷新一致性没有写清，刷新后可能带旧账户头

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:133-140`
- `docs/plan/subscriptionLoginPlan.md:415-420`
- `docs/plan/subscriptionLoginPlan.md:443-449`

**问题**:
方案说明登录时从 access token JWT 提取 `accountId` 并存储，但没有明确 refresh 后必须重新从新 access token 提取并原子更新。Codex 请求头 `chatgpt-account-id` 必须和当前 access token 匹配，否则可能 401/403。

pi 的 OAuth 实现每次 refresh 后都会重新 `credentialsFromToken()` 提取 accountId。

**修正建议**:
在 §5.2 / §5.3 增加硬性规则：

```text
OpenAI login 与 refresh 成功后都必须：
1. 从新的 access token 解出 accountId；
2. 若缺失则拒绝写入；
3. access/refresh/expires/accountId 作为一个对象原子替换；
4. 请求前可校验 stored.accountId 与 current access 解出的 accountId 一致。
```

测试补充：refresh 返回不同 `chatgpt_account_id` 时，请求头使用新 accountId。

---

### H4. config providerId 与 canonical OAuth provider key 混用，回放和凭据查找边界不清

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:362` 示例 `openaiCodex`
- `docs/plan/subscriptionLoginPlan.md:415` 凭据 key `openai-codex`
- `docs/plan/subscriptionLoginPlan.md:498` providerData `"provider": "openaiCodex"`
- `docs/plan/subscriptionLoginPlan.md:506` “同 canonical Provider”

**问题**:
方案同时出现：

- YAML 配置 providerId：`openaiCodex`
- OAuth canonical key：`openai-codex`
- providerData provider：`openaiCodex`
- 回放判断：canonical Provider

这会让实现者不清楚：

- 凭据按 YAML providerId 找，还是按 canonical key 找；
- 两个 YAML provider 都指向 Codex 时是否共享同一订阅凭据；
- providerData exact replay 判断用配置 ID 还是 canonical ID。

当前 FlamingoAgents 的 session/model 配置大量使用用户自定义 providerId，因此这里必须落清楚。

**修正建议**:
文档中拆成两个字段：

```json
{
  "configProviderId": "openaiCodex",
  "authProvider": "openai-codex",
  "api": "openai-codex-responses",
  "model": "gpt-5.4",
  "responseItems": []
}
```

规则：

```text
凭据存储永远按 authProvider：openai-codex / xai。
UI/session 仍保留 configProviderId。
opaque replay exact 匹配使用：
  api + authProvider + model
必要时再记录 configProviderId 仅用于展示/排查。
```

---

## 中严重度问题

### M1. xAI `referrer` 从 `pi` 改成 `flamingo-agents` 有落地风险

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:189-198`

**问题**:
pi 0.84.4 明确发送 `referrer=pi`。方案计划改为 `flamingo-agents`，但没有证据说明 xAI OAuth 服务接受该值。该字段可能被服务端白名单或统计逻辑依赖，改动会导致设备码申请失败。

**修正建议**:
首版建议按基线保持：

```text
referrer=pi
```

并加注释：

```text
该值来自 pi 0.84.4 兼容基线；如后续实测 xAI 接受 flamingo-agents，再单独调整。
```

若坚持变更，必须把真实 xAI device code 申请作为实施前契约测试，而不是实施后兜底。

---

### M2. Device Code polling 缺少默认 interval、最小 interval、xAI 首次等待规则

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:166-176`
- `docs/plan/subscriptionLoginPlan.md:207-213`
- `docs/plan/subscriptionLoginPlan.md:573`

**问题**:
pi 的 device-code 公共逻辑包含：

- 缺省 interval = 5 秒；
- 最小 interval = 1 秒；
- `slow_down` 缺省 +5 秒；
- xAI `waitBeforeFirstPoll: true`；
- OpenAI Codex device flow 不等待首轮。

方案没有明确这些差异。xAI 如果立即轮询，可能触发 `slow_down` 或被判定过早轮询。

**修正建议**:
补充状态机：

```text
默认 interval = 5s，最小 1s。
OpenAI Codex device：拿到 device_auth_id 后可立即首次 poll。
xAI RFC8628：首次 poll 前先等待 interval。
slow_down：
  有 server interval 使用 server interval；
  否则当前 interval += 5s。
```

---

### M3. “过期刷新”应改为“少于 5 分钟有效期也刷新”

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:47`
- `docs/plan/subscriptionLoginPlan.md:443-447`

**问题**:
目标写了“到期前/到期后自动刷新”，但 D4 实施步骤只写“过期”。pi 的 `resolveStoredOAuth` 默认要求至少 5 分钟有效期，否则进入刷新。只在过期后刷新会导致长 SSE 请求中途 Token 失效。

**修正建议**:

```text
expiresSoon = now + 5min >= credential.expires
若 expiresSoon 则进入刷新锁。
xAI 写入时已有 -5min skew；OpenAI 也应在 resolve 层统一执行 min validity。
```

测试补充：`expires = now + 60s` 时会刷新。

---

### M4. encrypted reasoning 需要从 terminal `response.output` 回填

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:317-318`
- `docs/plan/subscriptionLoginPlan.md:324`
- `docs/plan/subscriptionLoginPlan.md:629`
- `docs/plan/subscriptionLoginPlan.md:682-685`

**问题**:
pi 的 Responses shared 实现会在 `response.completed.response.output` 中回填 reasoning item 的 `encrypted_content`，因为某些服务端可能不在 `response.output_item.done` 中给全。方案虽然说 `response.completed` 读取 output，但没有明确“合并/回填 reasoning item”。

**修正建议**:

```text
finalizeResponse(response):
  for item in response.output:
    if item.type == "reasoning" and item.encrypted_content:
      找到同 id 的已保存 reasoning item；
      若缺 encrypted_content，则合并回填；
  最终 providerData.responseItems 以回填后的 item 为准。
```

测试补充：`output_item.done` 缺 encrypted_content、`response.completed.response.output` 带 encrypted_content，JSONL round-trip 后第二轮仍回放完整。

---

### M5. Web 浏览器登录需要明确“同机自动回调 / 远程手工粘贴”的状态机

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:520-532`
- `docs/plan/subscriptionLoginPlan.md:642-645`

**问题**:
OpenAI redirect URI 固定是 `http://localhost:1455/auth/callback`。Web 前端如果运行在远程浏览器，`localhost` 指的是用户电脑，不是 Flamingo 后端所在机器。方案有 `manualCode` 路由，但没有明确浏览器登录在 Web 场景下只能同机自动完成，远程必须粘贴回调 URL/code。

**修正建议**:
在 D7/D9 增加状态定义：

```text
browser login:
  - 后端启动 127.0.0.1:1455 listener；
  - 前端展示 authUrl；
  - 若浏览器与后端同机，回调自动完成；
  - 若远程访问，回调通常打到用户本机，必须使用 manualCode 提交完整回调 URL/code；
  - 状态接口不得返回 code_verifier、refresh/access token。
```

---

### M6. xAI API Key fallback 到 `XAI_API_KEY` 的规则需要落到 modelConfig/modelAuth

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:397`
- `docs/plan/subscriptionLoginPlan.md:445`
- `docs/plan/subscriptionLoginPlan.md:591`

**问题**:
方案说 xAI `auth: api-key` 可从 `apiKey` / `XAI_API_KEY` 解析，但当前 FlamingoAgents 的 env fallback 默认是 `OPENAI_API_KEY` 风格。若不明确实现规则，xAI API Key 模式可能仍要求 YAML `apiKey` 或错误读环境变量。

**修正建议**:

```text
auth=api-key:
  1. provider.apiKey 存在：按现有 ${ENV}/$ENV/明文规则解析；
  2. provider.apiKey 缺失且 api=openai-responses 且 canonical=xai：读取 XAI_API_KEY；
  3. 其它 api-key provider 仍保持旧行为，不扩大隐式环境变量范围。
```

---

## 低严重度问题

### L1. Codex `originator` / User-Agent 改名应视为兼容风险

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:248-249`
- `docs/plan/subscriptionLoginPlan.md:552`

**问题**:
pi Codex SSE 请求头中 `originator` 是 `pi`，User-Agent 来自 pi UA。方案改为 `flamingo-agents` / `FlamingoAgents/<version>`。这大概率只是统计字段，但订阅后端不是公开稳定 API，首版不应引入不必要差异。

**修正建议**:
首版推荐：

```text
originator: pi
User-Agent: FlamingoAgents/<version>
```

若 `originator=flamingo-agents`，必须加入真实 Codex smoke test；失败时统一常量回退为 `pi`。

---

### L2. 示例模型额度与 pi 当前目录不完全一致，可能误导配置

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:371`
- `docs/plan/subscriptionLoginPlan.md:384-385`

**问题**:
本机 pi 0.84.4 中：

- `gpt-5.4` maxTokens 是 `128000`；
- `grok-4.6` contextWindow/maxTokens 是 `500000`。

方案示例写 `65536` / `256000`。虽然文档说“服务端账户决定”，但示例可能被直接复制，造成 UI 上下文估算偏差。

**修正建议**:
要么对齐 pi 目录，要么明确：

```yaml
# 以下 maxTokens 为 Flamingo 首版保守 UI 限制，不代表服务端真实上限。
```

---

### L3. Token 不泄漏测试还不够覆盖异常日志路径

**文档位置**:
- `docs/plan/subscriptionLoginPlan.md:58`
- `docs/plan/subscriptionLoginPlan.md:73`
- `docs/plan/subscriptionLoginPlan.md:697-702`
- `docs/plan/subscriptionLoginPlan.md:778-781`

**问题**:
测试计划覆盖了 Web 响应不含 token，但还应覆盖：

- `modelAuth.__repr__`
- `modelAuthError.__str__`
- debug 日志
- `modelError` JSONL
- HTTP error body 截断日志

**修正建议**:
新增测试：

```text
构造 access/refresh 形如 sk-test-secret / refresh-secret；
触发 auth error、refresh error、model 401；
断言 Web response、debug buffer、JSONL modelError 中均不包含这些字符串。
```

---

## 明确无问题部分

- **ChatGPT Codex OAuth 端点、client_id、scope、device auth 端点**与 pi 0.84.4 一致。
- **xAI OAuth client_id、scope、device/token endpoint**与 pi 0.84.4 一致。
- **Codex `/backend-api/codex/responses` 与 xAI `/v1/responses` 的 URL 分离**正确。
- **`store: false`、`include: ["reasoning.encrypted_content"]`、opaque item replay 的方向**正确。
- **FlamingoAgents 现状差距表**基本准确：当前代码确实仍是 `openai-completions`、静态 Bearer、无 `providerData`、无 model OAuth Web 路由。
- **不改动现有 `chatCompletionsAdapter`、新增 Responses adapter 的边界**合理，有利于保持旧 Provider 兼容。
