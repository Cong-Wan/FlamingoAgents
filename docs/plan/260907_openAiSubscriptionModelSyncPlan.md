Author: wilbur
Version: 1.3
Date: 2026-09-07
Description: 修复 ChatGPT 订阅登录后只能返回过期内置候选、无法同步 GPT-6 的方案。v1.1 完成方案审核；v1.2 按实现审核分流 ChatGPT 403；v1.3 回填全部实施、实测与复审结果。

# OpenAI 订阅模型实时同步修复方案

- 状态：已完成；自动化、真实账户验收与独立实现复审均通过
- 目标：让模型设置页通过当前 ChatGPT OAuth 账户实时同步官方 Codex 可见模型，并能把 `gpt-6-astra` 安全增量加入编辑区。
- 关联旧方案：`docs/plan/subscriptionModelDiscoveryPlan.md`
- 方案审核：`docs/codeReview/260907_openAiSubscriptionModelSyncPlan.md`
- 实现审核：`docs/codeReview/260907_openAiSubscriptionModelSyncImplementation.md`
- 主要实现：`flamingoAgents/models/subscriptionModels.py`

## 1. 问题与证据

### 1.1 当前根因

`discoverSubscriptionModels('openai-codex')` 只验证 OAuth 凭据，随后直接调用 `localDiscovery(..., source='local-only')`。它没有发起模型目录请求，因此 UI 中“应用内置候选”永远受 `openAiCodexCatalog` 的发布时间限制。当前内置目录最高只有 `gpt-5.6-*`，无法发现后续账户模型。

这不是缓存或登录失败，而是 OpenAI 分支从未实现在线同步。

### 1.2 2026-09-07 官方协议证据

核对 OpenAI Codex 主仓库最新提交 `121f91fd5d9dc66017866ce9bdc49f1e182721df` 与正式版本 `0.153.4`：

- Codex 使用 `GET {providerBaseUrl}/models?client_version={version}`；
- ChatGPT provider base URL 为 `https://chatgpt.com/backend-api/codex`；
- 请求使用 `Authorization: Bearer ...` 与 `ChatGPT-Account-ID`；
- 响应结构为 `{ "models": [ModelInfo...] }`；
- picker 按 `priority` 排序，`visibility=list` 表示显示给用户；
- 远端字段含 `slug`、`display_name`、`input_modalities`、`context_window`、`default_reasoning_level`、`supported_reasoning_levels` 等。

使用本机当前 FlamingoAgents OAuth 凭据对固定官方端点进行只读探测：

```text
GET https://chatgpt.com/backend-api/codex/models?client_version=0.153.4
HTTP 200
```

当前账户返回的首个可见模型：

```text
slug=gpt-6-astra
name=GPT-6-Astra
visibility=list
context_window=272000
max_context_window=872000
default_reasoning_level=low
reasoning levels=low/medium/high/xhigh/max/ultra
priority=1
```

同一请求还返回 `gpt-reserve`、`codex-auto-review` 等隐藏模型，它们不应进入普通模型选择列表。

对照实验：`client_version=0.135.0` 只返回旧模型；缺少 `client_version` 返回 HTTP 400。故版本参数是必要协议字段。

兼容性实测：使用当前未修改的 `responsesAdapter`、临时内存配置 `model=gpt-6-astra`，携带一个无副作用 function tool schema 并要求只回复固定文本。真实请求返回 `OK`、0 次工具调用且包含 usage。由此确认本次无需引入 Responses Lite/code mode 改造，改动范围锁定在 discovery、UI 文案、测试与文档。

## 2. 假设、范围与成功标准

### 2.1 明确假设

1. “同步最新模型”沿用现有交互：候选先合并到浏览器编辑工作副本，用户点击页面底部“保存”后才写 `models.yaml`。
2. 账户端点返回的 `visibility=list` 是当前账户 picker 可见性的权威信号；`hide/none` 不加入配置。
3. `0.153.4` 是本次验证过 GPT-6 的协议兼容基线。项目不发送虚假的超高版本，以免取回要求 FlamingoAgents 尚未实现的未来协议模型。
4. OpenAI 目录没有独立的最大输出 Token 字段，而本地 schema 强制 `maxTokens` 为正整数。本次使用 `min(contextWindow, 128000)` 作为保守兼容占位；该字段当前不进入 Responses 请求，仅用于配置展示/兼容。报告中明确它不是上游声明的输出上限。
5. 本地 schema 同样强制 cost 四字段为非负数，故延续订阅候选 `cost=0` 口径，并明确其含义是“不做按 Token 成本估算”，不是免费或上游价格。
6. 真实 GPT-6 带 tools 烟测已通过。本次不修改 `responsesAdapter`、工具协议或会话执行路径；后续回归若推翻该实证，则停止交付并另行立项，而不在本修复中无界扩张。

### 2.2 方案取舍

| 方案 | 优点 | 缺点 | 决策 |
|---|---|---|---|
| 仅给 `openAiCodexCatalog` 补 `gpt-6-astra` | 改动最少 | 仍是假同步，下次模型发布再次失效，也不能反映账户可见性 | 不采用 |
| 调用本机 Codex CLI 或读取其缓存 | 可复用 Codex 行为 | 引入外部可执行文件/版本/另一份登录态依赖，Web 服务环境不稳定 | 不采用 |
| 直接实现官方固定 `/codex/models` 协议 | 与当前 OAuth store 一致，可测试、安全边界清楚 | 需维护已验证的兼容版本常量 | 采用 |
| 动态从 npm/GitHub获取“最新版本”再冒充该版本 | 看似免维护 | 第三方依赖增加，且无法证明 FlamingoAgents 支持未来模型协议 | 不采用 |

### 2.3 成功标准

- S1：有效 ChatGPT 登录调用 discovery 时真实访问固定官方 HTTPS 模型端点，返回 `source=live-account-catalog`、`autoApplicable=true`。
- S2：当前账户结果包含 `gpt-6-astra`，并正确映射名称、输入模态、272000 context、默认 reasoning `low`。
- S3：`visibility=hide/none` 不加入候选，报告给出 `hidden_by_provider`；非法、重复或缺元数据条目不会污染配置。
- S4：401 使用发起请求时的 stale Access，在凭据锁内最多刷新一次并只重放一次；第二次 401 要求重新登录。ChatGPT 403 按上游拒绝处理，xAI 403 保持既有重登语义。
- S5：3xx、429、坏 JSON/schema、超大响应按安全错误处理；网络/超时/5xx 才返回不可自动应用的本地 fallback。
- S6：固定 URL、代理感知、拒绝所有重定向、有限超时/响应大小；Token、账户 ID、响应正文不进入浏览器、异常或日志。
- S7：前端按钮和说明不再宣称“无可靠端点/仅内置候选”；在线结果自动增量合并，但仍不自动保存。
- S8：已有同 ID 模型及自定义字段保持不变，新增 GPT-6 幂等；隐藏模型不会出现在 `/model` 与新会话下拉中。
- S9：通过目标测试、全量 pytest、Python 编译、JS 语法、uv lock 与 diff 检查。
- S10：真实 GPT-6 带 tools 最小调用烟测已在编码前通过；实现后再做真实 discovery，输出仅含 source、模型 ID 和过滤原因且无凭据。

## 3. 后端设计

### 3.1 固定端点与请求

在 `subscriptionModels.py` 增加：

```text
openAiCodexModelsBaseUrl = https://chatgpt.com/backend-api/codex/models
openAiCodexModelsClientVersion = 0.153.4
openAiCodexModelsUrl = {base}?client_version={urlencoded version}
```

请求规则：

- URL 完全由代码常量构造，调用方不能传 URL；
- `GET`，仅发送 `Authorization`、`ChatGPT-Account-ID`、`Accept` 与 FlamingoAgents User-Agent；不伪装 Codex CLI originator；
- 使用现有代理感知 `urllib.request.ProxyHandler()` 和拒绝重定向 handler；
- TLS 保持系统校验；timeout 20 秒；正常及 HTTPError body 均最多读取 1 MiB + 1；一旦读到上限之外的第 1 byte，立即把整个响应判为 `invalid_upstream_response`，绝不尝试解析截断体；
- 生产函数只返回受限 `modelListHttpResponse`，不返回 Request 或凭据；
- OpenAI 测试注入回调签名固定为 `(accessToken, accountId)`，xAI 的既有 `(accessToken)` 签名不变，避免测试接口含糊。

将 opener 命名泛化为订阅模型 transport；xAI 与 OpenAI 共用同一安全 transport，不改变 xAI URL 和响应语义。

### 3.2 OpenAI 认证与一次刷新

流程：

1. `resolveOAuthCredential('openai-codex')` 获取 Access 与 accountId；
2. 调用 `requestOpenAiCodexModels(access, accountId)`；
3. 首次 401：调用 `resolveOAuthCredential(..., forceRefresh=True, staleAccess=usedAccess)`；
4. 使用刷新后 Access 与同一原子凭据中的 accountId 重放一次；
5. 并发请求若同时拿旧 Access 收到 401，仅首个锁持有者实际刷新；等待者在锁内发现 Access 已变化后复用新凭据，每个 discovery 各自最多重放一次；
6. 新 Access 再返回 401 -> `reauth_required`，不得再次刷新；ChatGPT 403 -> `upstream_rejected`，xAI 403 -> `reauth_required`；
7. 凭据读写异常 -> `credential_error`；任何错误文本都不拼接底层异常。

保留现有 Web `credentialGeneration` 前后检查，避免同步期间切换账户后应用旧结果。

### 3.3 响应解析与本地模型映射

只解析顶层 `models` 数组，最多 200 项；每项仅读取允许字段，不透传指令、计划、服务层或其它上游内容。

候选条件：

- `slug` 满足现有 model ID 正则并去重；
- `visibility == 'list'`；
- `context_window` 为正整数；
- `input_modalities` 是数组，取 `text/image` 交集且必须包含 `text`；
- `display_name` 为合理长度非空字符串，否则明确回退到 slug；该降级不依赖硬编码模型 ID 白名单，因此 `gpt-6-astra` 等新合法 slug 可直接进入候选；
- reasoning level 只从格式合法的远端字符串读取；优先 `default_reasoning_level`，缺失时取 supported 列表首项；
- 按 `(priority, 原始顺序)` 稳定排序；缺失、布尔值或非整数 priority 放末尾；
- 缺 `visibility/context_window/input_modalities` 等关键字段时不猜测，跳过并写入具体 report reason。

映射：

```yaml
id: slug
name: display_name
input: 远端 input_modalities 与 [text, image] 的交集
contextWindow: context_window
maxTokens: min(context_window, 128000)
reasoning: supported_reasoning_levels 非空或 default_reasoning_level 非空
reasoningEffort: default_reasoning_level（存在时）
cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}
```

报告：

- `discoveredModelIds`：格式合法且去重后的所有远端 slug；
- `includedModelIds`：最终可见候选；
- `skippedModels`：`hidden_by_provider`、`missing_model_metadata`、`unsupported_input_modality`；
- warnings：账户目录仅代表当前返回结果；`maxTokens=min(contextWindow,128000)` 是本地 schema 兼容占位且不进入 Responses 请求；cost=0 仅表示不估算订阅 Token 成本。

远端 payload 不存在“模型 ID 白名单”：任何满足上述 schema 与可见性条件的新 slug 都能同步。白名单仅作用于读取字段，防止把 `base_instructions`、计划、账户或其它上游内容透传到浏览器。

### 3.4 失败与 fallback

| 情况 | 行为 |
|---|---|
| 200 + 合法目录 | 在线候选，可自动应用 |
| 网络/超时 | `local-fallback`，不可自动应用 |
| 5xx | `local-fallback`，不可自动应用 |
| 首次 401 | 刷新并重放一次 |
| 第二次 401 | `reauth_required` |
| ChatGPT 403 | `upstream_rejected`（可能为权限/风控，不误报凭据失效） |
| xAI 403 | 保持既有 `reauth_required` |
| 429 | `rate_limited`，安全解析 Retry-After |
| 3xx | `redirect_forbidden` |
| 其它非 2xx（含版本参数相关 4xx） | `upstream_rejected`，显式提示上游拒绝，不得伪装成空目录 |
| 超大/坏 JSON/schema | `invalid_upstream_response` |

更新 OpenAI fallback 至至少包含已验证的 `gpt-6-astra`；fallback 继续要求用户显式确认，不宣称账户权益。

## 4. 前端与文档

`settingsView.js` 精准修改：

- ChatGPT 账户说明改为“登录后读取当前账户的 Codex 实时模型目录”；
- 已登录按钮统一显示“同步模型候选”；
- 增加 `live-account-catalog` 来源名称；
- 增加隐藏/缺元数据等过滤原因中文说明；
- 保持 revision、generation、single-flight、非覆盖增量合并和手工保存逻辑不变。

同步更新 `README.md` 与 `docs/webApiSpec.md` 中“ChatGPT 仅本地目录”的旧描述及 discovery source/过滤语义。

## 5. 测试与验证

### 5.1 pytest 回归

扩展 `tests/testSubscriptionModels.py`：

1. OpenAI 200 目录映射 GPT-6，验证顺序、context、reasoning、input、默认 maxTokens；
2. 隐藏/非法/重复/缺字段模型过滤及 report；
3. 固定 URL、query、GET、必要鉴权头、代理 opener、拒绝重定向；
4. OpenAI 首次 401 刷新后成功、第二次 401、403 独立拒绝语义、429、3xx、坏 schema、超大响应；
5. 网络/5xx fallback 不可自动应用且包含 GPT-6；
6. Access/refresh/account canary 不出现在返回、异常和捕获输出；
7. 保持所有现有 xAI discovery 测试通过；
8. 并发 OpenAI 401：两请求仅触发一次实际 refresh、各自最多重放一次；新凭据仍 401 时均终止；
9. 明确覆盖 0/1/200/201 项边界、缺失/非法 priority、缺 visibility/input/context、timeout、301/302/303/307/308、body 恰好上限与上限 + 1；
10. 验证网络/5xx fallback 的 `autoApplicable=false`，版本类 4xx 不 fallback，且所有超限响应都未调用 JSON parser；
11. 验证 `context_window<=0` 被跳过，并固化 Responses 请求体不读取 discovery 的 `maxTokens` 兼容占位。

按需要扩展 `tests/testSubscriptionModelsJs.py`，确认 OpenAI 在线候选对已有 Provider 只追加 `gpt-6-astra`，重复同步幂等且旧模型不覆盖。

### 5.2 静态与全量验证

```text
uv run pytest -q tests/testSubscriptionModels.py tests/testSubscriptionModelsJs.py tests/testModelAuthWeb.py
uv run pytest -q
uv run python -m compileall -q flamingoAgents webApp tests
node --check webApp/frontend/js/subscriptionModels.js
node --check webApp/frontend/js/settingsView.js
uv lock --check
git diff --check
```

### 5.3 真实烟测

1. 编码前 GPT-6 调用兼容性烟测已完成：临时内存配置、不改 `models.yaml`，携带无副作用 tool schema，返回固定文本 `OK`、0 tool calls、usage 存在；输出未包含原始响应/header/凭据。
2. 实现后只需做一次真实 discovery：仅打印 source、included IDs 与 skipped reason，确认包含 `gpt-6-astra` 且不含隐藏模型或凭据。
3. 若自动化回归与既有 GPT-6 实测冲突，停止交付并记录独立问题；本计划不临时扩展 Responses Lite/code mode。

## 6. 风险与回滚

- 版本门槛：未来模型要求高于 0.153.4 时不会出现，这是刻意的兼容保护；验证新协议后再提升常量。
- 远端 schema 演进：只读字段白名单；缺关键字段的条目跳过，不凭名字猜元数据；合法新 slug 不需要预置 ID 白名单。
- 账户 rollout：实时目录可能因账户/计划不同；返回结果只代表本次账户响应。
- 网络重试：本次有意不在 discovery 内自动重试网络/5xx，避免一次用户操作产生不可控请求；fallback 会明确不可自动应用，用户可再次点击同步。
- 配置覆盖：现有 JS 合并保留同 ID 对象，因此在线元数据不会覆盖用户自定义值。
- 回滚：恢复 OpenAI local-only 分支即可；xAI 路径及 OAuth 文件格式不变。

## 7. 影响文件

- 修改 `flamingoAgents/models/subscriptionModels.py`
- 修改 `tests/testSubscriptionModels.py`
- 视覆盖需要修改 `tests/testSubscriptionModelsJs.py`
- 修改 `webApp/frontend/js/settingsView.js`
- 修改 `config/models.example.yaml`
- 修改 `README.md`
- 修改 `docs/webApiSpec.md`
- 更新本计划状态与实测结果

不修改 `config/models.yaml`、凭据格式、登录流程、Provider 合并策略或会话数据。

## 8. TODO lists

### Phase 0：方案审核

- [x] T0.1 创建全新 subagent 独立审核本方案
- [x] T0.2 按高/中问题修订方案并提升至 v1.1
- [x] T0.3 独立 subagent 复审通过：无阻断、高或未处置中等级问题

### Phase 1：测试先行

- [x] T1.1 添加 OpenAI 在线目录 GPT-6 映射失败测试
- [x] T1.2 添加 visibility/非法/重复/缺元数据/排序失败测试
- [x] T1.3 添加固定 URL、query、方法、鉴权头与 no-redirect transport 测试
- [x] T1.4 添加 OpenAI 401 单次刷新、并发折叠、第二次 401 与 403 分流测试
- [x] T1.5 添加 3xx/429/4xx/5xx/network/timeout/body 上限/schema 边界测试
- [x] T1.6 添加 canary Token/accountId 不泄漏测试
- [x] T1.7 新测试在旧实现上产生 11 个预期失败，既有 xAI 基线正常

### Phase 2：后端实现

- [x] T2.1 增加固定 OpenAI models URL 与兼容版本常量
- [x] T2.2 泛化安全 opener 并实现 OpenAI 模型 GET transport
- [x] T2.3 实现 OpenAI 凭据解析、stale 401 单次刷新与失败分类
- [x] T2.4 实现 `models` 字段白名单解析、visibility 过滤、排序及字段映射
- [x] T2.5 更新 OpenAI 本地 fallback，加入经验证的 GPT-6 候选
- [x] T2.6 更新代码文件头 Version/Description
- [x] T2.7 目标测试全部通过

### Phase 3：前端与文档

- [x] T3.1 更新 ChatGPT 账户说明、同步按钮、来源与过滤原因文案
- [x] T3.2 保持现有增量非覆盖、revision/generation/single-flight、手工保存语义
- [x] T3.3 补充 OpenAI 在线候选 JS 幂等合并测试
- [x] T3.4 更新模型示例、README 与 Web API discovery 契约
- [x] T3.5 更新所有修改代码/配置文件头的小版本与 Description

### Phase 4：验收与实现审核

- [x] T4.1 目标测试 31 passed；最终全量 pytest 79 passed
- [x] T4.2 Python compileall、全部 JS `node --check`、`uv lock --check`、`git diff --check` 通过
- [x] T4.3 真实 discovery 返回 `gpt-6-astra` 等 6 个可见模型，正确过滤 2 个隐藏模型
- [x] T4.4 `config/models.yaml`、凭据和会话数据无工作区修改
- [x] T4.5 回填本计划 TODO、测试数字和实测结果
- [x] T4.6 全新 subagent 完成最终实现审核
- [x] T4.7 修复 OpenAI 403 误报问题；独立复审结论“无明显问题，可交付”
