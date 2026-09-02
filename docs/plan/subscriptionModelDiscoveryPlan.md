Author: wilbur
Version: 1.3
Date: 2026-09-01
Description: 订阅模型候选发现方案的代理兼容修订；实测确认固定主机 http.client 绕过 HTTPS_PROXY 导致超时，改为 ProxyHandler + 拒绝全部重定向的 urllib opener，同时保持 stale-token 和 Token 防泄漏边界。

# 订阅登录后模型配置候选发现方案

- 状态：v1.3 代理兼容修复完成；自动化、无 Token 连通性烟测及独立复审通过
- 方案审核：`docs/codeReview/260901_subscriptionModelDiscoveryPlan.md`
- 实现审核：`docs/codeReview/260901_subscriptionModelDiscoveryImplementation.md`（复审结论：可交付）

## 1. 背景、决策与审核

当前订阅 OAuth 只负责 `~/.flamingo/auth.json`，登录按钮却依附于已有 `models.yaml` Provider，导致用户必须先按 API 方式手工创建 Provider。凭据与模型配置分离的底层边界正确，但 Web 流程不完整。

用户确认采用：

1. 模型设置页始终显示独立“订阅账户”区域，不要求先建 Provider；
2. 登录成功后自动发现并合并**安全确认过的模型配置候选**到浏览器内存工作副本；
3. 用户点击“保存”前不得写 `config/models.yaml`；
4. 允许使用现有 xAI OAuth 凭据发起只读模型列表请求。

独立审核见 `docs/codeReview/260901_subscriptionModelDiscoveryPlan.md`。v1.2 已纳入首轮 4 个高风险/5 个中风险修订，以及复审发现的“旧账户 single-flight 被新登录复用”中风险项。

## 2. 实测证据与边界

2026-09-01 使用现有凭据只读请求：

```text
GET https://api.x.ai/v1/models
HTTP 200
顶层字段：data/object
候选模型：12 个
模型字段：id/object/owned_by/created
```

列表同时包含 Grok 文本模型和 `grok-imagine-*` 图像/视频生成模型。响应不提供 FlamingoAgents 配置必需的 `contextWindow/maxTokens/reasoning/input/cost`，也不证明 Responses 兼容或最终账户调用权益。

因此 UI/API 统一使用“模型配置候选”，禁止宣称“账户可用模型”。上游 pi 0.84.4 同样使用生成时模型目录；本实现不引入 pi/Node 运行时依赖，只把调研后的明确元数据固化为 Python 本地目录。

## 3. 成功标准

- S1：即使 `models.yaml` 没有订阅 Provider，Web 设置页也能登录 ChatGPT/xAI。
- S2：xAI 登录成功后请求固定 HTTPS `/v1/models`；禁止重定向，Token 不进入浏览器、日志、错误或模型配置。
- S3：只有“live 返回 ID ∩ 本地已知 Responses 目录”默认自动合并；图像/视频、Completions-only、未知模型只进入带原因报告。
- S4：ChatGPT 本地目录、xAI 离线 fallback 均标为未验证候选，必须由用户显式点击确认，不能自动合并。
- S5：发现只针对前端最新工作副本做增量合并；保存前不 PUT、不落盘。
- S6：已有 Provider/model/headers/未知扩展字段及顺序不被覆盖或删除；合并幂等且无共享可变引用。
- S7：Provider 必须严格匹配 canonical API/auth/base URL；多匹配时只有当前选中项可作为显式目标，否则拒绝随机选择。
- S8：401 使用 `staleAccess` 并发保护后只重放一次；第二次 401/403 返回 `reauth_required`。429、协议异常不 fallback；网络/5xx 仅返回需显式确认的离线候选。
- S9：按 Provider single-flight；工作副本 revision 或登录 generation 变化时丢弃旧结果。
- S10：全量测试、编译、JS 语法、DOM 引用、lock/diff 检查通过；响应、异常及捕获日志无 canary Token。

## 4. 后端设计

### 4.1 `subscriptionModels.py`

新增 `flamingoAgents/models/subscriptionModels.py`：

- `discoverSubscriptionModels(provider, store, requestFn)` 只允许 `xai/openai-codex`；
- xAI URL 固定为 `https://api.x.ai/v1/models`，不接受调用方 URL；
- 生产请求使用 `urllib.request.ProxyHandler()` 遵循标准 `HTTPS_PROXY/NO_PROXY`，并注册自定义 `HTTPRedirectHandler`，其 `redirect_request()` 对全部 3xx 返回 `None`；不得使用默认重定向 handler；
- 301/302/303/307/308 由 `HTTPError` 转为受限内部响应并统一 `redirect_forbidden`，绝不请求 Location 目标；
- TLS 使用系统校验，连接/读取设置有限超时；响应（包括 HTTPError body）上限 1 MiB；
- 只读取 `data[].id`，校验字符/长度、去重并限制最多 200 项；不透传其它上游字段；
- 所有底层异常映射为枚举 code，不使用异常文本，不记录 Request/Authorization/响应体。

401 流程固定为：

1. `resolveOAuthCredential('xai')`，保存本次仅在内存使用的 `usedAccess`；
2. 首次 401 后调用 `resolveOAuthCredential('xai', forceRefresh=True, staleAccess=usedAccess)`；
3. 凭据锁内若 Access 已变化则复用新值，否则刷新；
4. 仅重放一次；第二次 401 或任意 403 返回 `reauth_required`；
5. `usedAccess` 永不进入异常、返回值或日志。

结构化失败分类：

| 类别 | 行为 |
|---|---|
| 未登录 | `not_logged_in`，失败 |
| 第二次 401 / 403 | `reauth_required`，失败且不 fallback |
| 429 | `rate_limited`，附安全的 retryAfter（若可解析），不 fallback |
| 3xx | `redirect_forbidden`，不 fallback |
| 坏 JSON/schema/超大响应 | `invalid_upstream_response`，不 fallback |
| 网络/超时/5xx | 返回 `source=local-fallback`，`autoApplicable=false` 和 `liveFailureCode`，只供显式确认 |

### 4.2 本地目录与候选策略

- xAI 已知 Responses：使用调研目录中的明确元数据；只有实时 ID 命中时 `source=live-catalog-match`、可自动合并；
- `grok-imagine-*`、已知 Completions-only、未知 ID：只进 `skippedModels`，绝不靠名字推断配置；
- xAI 网络/5xx fallback：只返回已知 Responses 目录，`autoApplicable=false`；
- ChatGPT 没有可靠账户枚举端点：返回 `source=local-only`、`autoApplicable=false` 的 Codex 目录；UI 必须由用户点击“应用内置候选”；
- 模型 `cost` 使用 0 仅表示“FlamingoAgents 不做订阅按 Token 成本估算”，报告必须明确，不表述为零成本。

安全返回：

```json
{
  "provider": "xai",
  "source": "live-catalog-match",
  "autoApplicable": true,
  "credentialGeneration": 1,
  "providerTemplate": {
    "suggestedId": "xaiSubscription",
    "baseUrl": "https://api.x.ai/v1",
    "api": "openai-responses",
    "auth": "oauth",
    "headers": {},
    "models": []
  },
  "report": {
    "discoveredModelIds": [],
    "includedModelIds": [],
    "skippedModels": [{"id":"...","reason":"..."}],
    "warnings": [],
    "liveFailureCode": null
  }
}
```

### 4.3 Web manager generation 与 API

`modelAuthManager` 为每个 canonical Provider 维护非敏感整数 generation：成功登录和退出时递增；状态、任务完成和 discovery 响应携带 generation。它只用于丢弃本进程旧异步结果，不作为跨进程账户身份保证。

新增鉴权、禁止缓存的路由：

```text
POST /api/modelAuth/{provider}/discover
Cache-Control: no-store
```

路由使用 Web manager 当前 credential store。该 POST 会访问 xAI，并可能在 Token 临近过期或 401 时刷新 `auth.json`，但不会改 `models.yaml`。

## 5. 前端设计

### 5.1 独立订阅账户区

`index.html` 在 Provider tabs 前新增：

```text
订阅账户
[ChatGPT Plus/Pro：状态、登录、应用内置候选、退出]
[xAI SuperGrok/X Premium：状态、登录、同步模型候选、退出]
[发现/过滤/合并报告与“未验证权益”提示]
```

登录按钮不再依赖 Provider。Provider OAuth 字段只显示“认证在上方订阅账户管理”和状态。

### 5.2 纯 JS 非覆盖合并

新增可独立 Node 测试的 `webApp/frontend/js/subscriptionModels.js`：

- 只接受普通对象与安全 suggested ID，拒绝 `__proto__/prototype/constructor`；
- 严格匹配规范化后的 canonical `baseUrl + api + auth`；
- 0 个匹配：安全创建建议 ID；建议 ID 冲突则生成数字后缀；
- 1 个匹配：增量合并；
- 多个匹配：只有调用方明确传入、且属于匹配集合的 current Provider 才合并，否则返回 ambiguity，不修改输入；
- 返回新工作副本或对当前最新副本进行最小增量操作；不得使用请求启动时快照整体覆盖；
- 已有同 ID 模型对象和 Provider 全部字段保持原值，缺失模型深拷贝追加，旧模型不删除；
- 失败零修改、重复合并幂等、无共享引用。

### 5.3 revision / generation / single-flight

- `workingRevision` 在任意表单编辑、保存、重置、重新 open 时递增；
- `accountEpoch[provider]` 在登录开始、logout 开始时递增；
- 每次发现捕获 revision、account epoch、后端 credentialGeneration；
- 返回时重新读取**当前**工作副本；只在 revision/epoch/generation 未变时合并，否则显示“配置或账户已变化，请重新同步”；
- single-flight key 必须是 `provider + accountEpoch`，只允许同一账户 epoch 的自动/手工触发复用 Promise；
- 登录或 logout **开始时**先递增 `accountEpoch[provider]`，因此旧请求即使仍未结束，新登录也会建立新 flight；旧 flight 完成时只清理自己的复合 key，不得删除新 flight；
- 页面重新 open 会使旧结果失效；不要求取消底层请求，但旧回调不得合并。

触发规则：

- xAI 登录 completed：自动发现；只有 `autoApplicable=true` 才自动合并；
- 打开设置页：仅当 xAI 已登录且没有严格匹配 Provider 时自动一次；
- 已有 xAI Provider：用户点击“同步模型候选”；
- ChatGPT/local fallback：始终先展示报告，再由用户点击确认应用；
- 所有成功合并只标记 dirty、切到目标 Provider，仍由用户点击原“保存”落盘。

## 6. 影响文件

- 新增 `flamingoAgents/models/subscriptionModels.py`
- 新增 `webApp/frontend/js/subscriptionModels.js`
- 修改 `webApp/backend/modelAuthManager.py`、`webApp/backend/server.py`
- 修改 `webApp/frontend/index.html`、`js/api.js`、`js/settingsView.js`、`styles.css`
- 新增 `tests/testSubscriptionModels.py`、`tests/testSubscriptionModelsJs.py`，扩展 `tests/testModelAuthWeb.py`
- 更新 `README.md`、`docs/webApiSpec.md`、本计划

不修改凭据 JSON 格式、Responses 请求协议和 `models.yaml` 保存语义。

## 7. 测试计划

1. 固定 HTTPS 请求不跟随 301/302/307/308，未建立目标域连接；
2. live 合法/重复/恶意字段/图像模型/Completions-only/未知 ID 的筛选与报告；
3. 401 使用准确 `staleAccess`；多发现并发 401 只刷新一次并各只重放一次；
4. 第二次 401、403、429、3xx、坏 JSON/schema、超大响应分别返回规定安全 code；网络/5xx 仅返回不可自动应用 fallback；
5. canary access/refresh 出现在注入异常时，响应、异常和捕获日志均不得包含；
6. ChatGPT local-only 不可自动应用并有权益/成本警告；
7. Web 路由鉴权、no-store、generation 和无 Token；
8. JS：新建、严格匹配、多匹配歧义、同 ID 深层保留、模型顺序/扩展字段、ID 冲突、危险键、无共享引用、幂等、失败零修改；
9. JS orchestration：同 epoch single-flight；请求期间编辑/reload 使旧结果丢弃；旧 flight 未完成时 logout→login 必须产生新请求且旧结果不得清除/覆盖新 flight；
10. 代理 transport：注入 `HTTPS_PROXY` 时 opener 包含 ProxyHandler；301/302/303/307/308 的 redirect handler 均拒绝生成新请求；HTTPError 状态仍能进入 401/429/3xx 分类且错误无 Token；
11. 全量 pytest、全部 JS `node --check`、DOM ID 引用、`compileall`、`uv lock --check`、`git diff --check`。

## 8. 任务清单

- [x] T1 独立审核方案并升级 v1.1
- [x] T2 实现安全模型发现、本地目录及 HTTP/刷新/失败分类测试
- [x] T3 新增 generation、鉴权 discovery API 与 Token 不泄漏测试
- [x] T4 实现独立订阅账户 UI
- [x] T5 实现纯 JS 非覆盖合并与 revision/generation/single-flight
- [x] T6 更新文档与 API 契约
- [x] T7 全量自动化、编译、语法、安全 diff 验收（49 passed）
- [x] T8 使用当前 xAI 凭据完成用户授权的单次真实上游探测：HTTP 200、返回 12 个候选且无 Token 输出
- [x] T9 用户真实 Web 交互发现 `network_error`；无 Token 对照测试确认 direct `http.client` 超时、代理感知 `urllib` 可达 401
- [x] T10 实现代理感知且禁止重定向的 transport；49 项测试通过，无 Token 连通性请求经代理成功到达 xAI（HTTP 400），独立复审结论可交付
