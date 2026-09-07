Author: wilbur
Version: 1.1
Date: 2026-09-07
Description: OpenAI 订阅模型实时同步实现的独立 subagent 代码审核、问题修复与复审结论。v1.1 补记 GPT-6 示例配置的最终窄范围复审。

# OpenAI 订阅模型实时同步实现审核

## 1. 总览

- 执行计划：`docs/plan/260907_openAiSubscriptionModelSyncPlan.md`
- 审核范围：`subscriptionModels.py`、模型设置页文案、GPT-6 示例配置、Python/Node 测试、README 与 Web API 契约
- 审核维度：Bug、资源、并发、逻辑简洁、接口、模块融合、性能、安全与风格
- 首轮结论：无 Critical/High；2 个 Medium 建议、4 个 Low 观察
- 修复后复审：**无 Critical、无 High、无未处置 Medium；无明显问题，可交付**

## 2. 首轮问题与处置

### 🟡 Medium：ChatGPT 403 被误报为凭据失效

**位置**：`flamingoAgents/models/subscriptionModels.py` 的 discovery 终态认证判断和公共响应分类。

**问题**：ChatGPT models 端点的 403 也可能表示账户权限、组织策略或风控；统一映射 `reauth_required` 会诱导用户无意义退出重登。

**修复**：

- OpenAI 仅 401 触发 stale-token 刷新及 `reauth_required`；
- OpenAI 403 不刷新，映射 `upstream_rejected`；
- xAI 403 保持原有 `reauth_required` 契约；
- 新增测试断言 OpenAI 403 分流，并更新计划/API 文档。

**验证**：目标 24 项通过；最终全量 79 项通过。

### 🟡 Medium：20 秒 timeout 不是严格总 deadline

**位置**：`_openModelListRequest`。

**问题**：urllib timeout 是连接/单次读空闲超时；理论上的慢滴流可让总耗时超过 20 秒。

**处置**：接受为已知取舍，不引入 socket 层 deadline。理由：端点固定为官方 HTTPS、操作由用户低频触发、响应最多读取 1 MiB + 1、完全失速仍会超时；侵入底层 socket 会显著增加复杂度并破坏 transport 可测试性。独立复审认可该取舍不构成交付问题。

## 3. Low 观察核验

1. **响应资源关闭**：正常 response 与 HTTPError 均在 `finally` 显式 close，已闭环，无需修改。
2. **未知 visibility**：已进入 `skippedModels`，reason 为 `missing_model_metadata`，不是静默丢弃。
3. **client_version**：已集中为 `openAiCodexModelsClientVersion` 常量；未来协议验证后再更新。
4. **maxTokens**：项目模型 schema 要求正整数且前端会消费；使用 `min(contextWindow, 128000)` 兼容值，warning 明确其不是上游输出上限，Responses 请求测试确认不透传。

## 4. 八维审核结果

| 维度 | 结论 |
|---|---|
| Crash & Bug | HTTP/JSON/schema/边界失败均结构化处理；无明显问题 |
| 资源管理 | response 与 HTTPError 均显式关闭 |
| 并发安全 | stale Access CAS 锁折叠并发刷新；双请求只刷新一次，各自最多重放一次 |
| 逻辑简洁 | OpenAI/xAI 共用认证请求、transport 与状态分类；Provider parser 仅保留 schema 差异 |
| 接口设计 | OpenAI 测试回调 `(access, accountId)`；xAI 既有 `(access)` 保持兼容 |
| 模块融合 | 保持 Web generation/revision/single-flight、非覆盖增量合并与手工保存 |
| 性能 | 低频请求、1 MiB + 1 上限、最多 200 模型；无热路径影响 |
| 安全与风格 | 固定 URL、TLS、代理感知、拒绝重定向、字段白名单、canary 不泄漏；文件头已升级 |

## 5. 最终验证证据

- `uv run pytest -q`：**79 passed**
- Python `compileall`：通过
- 全部 `webApp/frontend/js/*.js` 的 `node --check`：通过
- `uv lock --check`：通过
- `git diff --check`：通过
- 真实账户 discovery：`source=live-account-catalog`，可见模型首项 `gpt-6-astra`；隐藏 `gpt-reserve` / `codex-auto-review` 被过滤
- 真实 GPT-6 带无副作用 tool schema 调用：返回 `OK`、0 tool calls、usage 存在
- `config/models.example.yaml`：项目解析器成功加载 `openaiCodex/gpt-6-astra`，reasoning effort 为 `low`
- `config/models.yaml`、凭据和会话数据：无工作区修改

## 6. 最终结论

最终新增的 GPT-6 示例配置经另一全新 subagent 窄范围复审，结论同为“无明显问题，可交付”。

**无明显问题，可交付。**
