Author: wilbur
Version: 1.2
Date: 2026-09-01
Description: subscriptionModelDiscoveryPlan 三轮审核；前两轮闭环安全/竞态风险，代理实测后 v1.3 改用 ProxyHandler + no-redirect opener，专项审核确认无高/中风险。

# 订阅模型发现方案审核

## 结论

v1.0 存在阻塞项，不直接实施。独立 `pi -p --no-context-files --no-session --thinking low` 审核发现：高风险 4 项、中风险 5 项、低风险 2 项。

## 高风险

1. **重定向 Token 泄漏**：固定初始 URL 仍不足以阻止 HTTP 客户端携带 Authorization 跟随跨域 30x；实现必须禁止所有重定向。
2. **401 并发刷新**：必须把收到 401 的实际 Access Token 作为 `staleAccess`，锁内复用其它请求已刷新的凭据，只重放一次。
3. **候选模型误导**：实时 `/models` 不证明 Responses 兼容，本地目录也不证明账户权益；仅 live ID 与已知 Responses 目录交集可自动合并，fallback/未知模型需要显式确认或只报告。
4. **异步覆盖工作副本**：发现返回后必须针对最新工作副本做增量合并，并用 revision 丢弃请求期间发生编辑、重载、保存、退出登录后的旧结果。

## 中风险

1. Provider 匹配需包含 canonical API/auth/规范化 base URL；多个匹配项不能随机选择。
2. 登录完成和页面打开需按 Provider single-flight；logout/重新登录使旧结果失效。
3. 401/403、429、5xx/网络、坏 JSON/schema/超大响应必须分类处理，不能统一静默 fallback。
4. discovery 边界不得透传底层异常文本；使用 canary Token 扫描响应、异常和日志。
5. JS 合并补充深层字段保留、无共享引用、危险对象键、幂等和失败零修改测试。

## 低风险

1. `cost: 0` 只能表示“不进行按 Token 成本估算”，不能表述为订阅零成本。
2. 外部发现改用鉴权 POST，避免 GET 预取/缓存；文档说明可能触发 OAuth refresh。

## 第一轮修订

计划 v1.1 落实：无重定向固定主机 HTTP、stale-token CAS、配置候选语义、仅已知 live 交集自动合并、结构化错误分类、revision/generation/single-flight、严格 Provider 匹配以及扩充测试。

## 第二轮复审

复审确认首轮 4 个高风险和其中 4 个中风险已闭环，但发现仅按 Provider keyed 的 single-flight 可能在旧请求未结束时被 logout→re-login 的新账户复用。计划 v1.2 改为 `provider + accountEpoch` 复合 key，并规定登录/logout 开始先递增 epoch、旧 flight 只能清理自己的 key；测试必须证明新登录产生新请求且旧结果不覆盖新结果。

## 代理兼容专项审核

真实 Web 烟测发现运行环境依赖 `HTTPS_PROXY`，而 v1.2 的 `http.client` 固定主机直连会超时。v1.3 改为 `ProxyHandler()` + 自定义 `HTTPRedirectHandler`：遵循标准代理环境，同时 `redirect_request()` 对所有 3xx 返回 `None`；普通响应与 HTTPError body 均有界读取。

专项独立审核确认该设计不会向 Location 创建携带 Authorization 的请求，401/429/3xx 分类和 1 MiB 上限保持成立，无高/中风险阻塞。

## 最终结论

v1.3 已闭环安全、竞态和代理兼容风险，可以实施。
