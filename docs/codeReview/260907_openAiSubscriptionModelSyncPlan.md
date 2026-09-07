Author: wilbur
Version: 1.1
Date: 2026-09-07
Description: OpenAI 订阅模型实时同步方案的独立 subagent 审核结论及逐项处置。v1.1 记录修订后复审通过结论。

# OpenAI 订阅模型实时同步方案审核

## 1. 审核结论

独立 subagent 结论：方案方向正确、无阻断问题，但初稿需补强超限响应语义、并发 401、字段降级和边界测试后再放行。

## 2. 审核问题

### 高

1. **1 MiB 截断语义需明确**：读取到 `limit + 1` 后必须把整个响应判为失败，禁止解析截断内容。

### 中

1. **并发 401 语义需明确并测试**：多个请求使用同一 stale Access 时只刷新一次；等待者复用新凭据，各自最多重放一次。
2. **字段降级需明确**：合法 slug 不应因 display name 等非关键字段异常而无理由消失，需定义回退或报告。
3. **`maxTokens=128000` 与 `cost=0` 不是上游权威值**：需避免将兼容占位误表述成账户元数据。
4. **固定 `client_version` 的兼容边界需明确**：版本相关 4xx 不能静默当成空目录。
5. **测试缺口**：重定向、超时、body 超限、200 条边界、priority/visibility 缺失、并发刷新、fallback 不自动应用。

### 低

1. 缺失 `input_modalities` 应进入有原因的过滤报告。
2. 网络/5xx 不重试需确认是有意策略。
3. “烟测失败后最小协议修复”的范围需锁定，避免任务无界扩张。

## 3. 处置

- 采纳：明确 body 超限整体失败且不解析；补充并发 401 精确语义和全部边界测试；明确 display name 回退及缺元数据报告；版本 4xx 按 `upstream_rejected` 显式失败；网络/5xx 本次有意不重试，由用户再次同步；补齐 fallback 不自动应用测试。
- 调整后采纳：本地 schema 强制 `maxTokens` 为正整数、cost 四项为非负数，不能使用 `null`。`maxTokens` 改为 `min(contextWindow, 128000)`，且该字段当前不进入 Responses 请求；`cost=0` 延续现有订阅口径，两者均在 warning 明确是本地兼容/不估算值，不冒充上游元数据。
- 已消除风险：2026-09-07 已用当前 adapter 对 `gpt-6-astra` 做带无副作用 tool schema 的真实调用，返回 `OK`、0 tool calls、含 usage。因此无需扩大到 Responses Lite/code mode 实现，方案将锁定不修改 adapter。

## 4. 复审状态

- v1.0：有 1 个高、5 个中、3 个低等级修订点。
- v1.1：独立 subagent 复审结论为“无阻断、无高、无未处置的中等问题；方案无明显问题，可以实施”。
- 两项非阻断建议：补 `context_window<=0` 过滤断言与 Responses 请求体不使用 `maxTokens` 的断言，已纳入测试计划。
