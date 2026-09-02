Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: 订阅模型候选实现审核及代理修复复审；闭环登录异常泄漏、open 逆序覆盖和 http.client 绕过 HTTPS_PROXY，最终 49 项测试及专项复审通过。

# 订阅模型配置候选实现审核

## 首轮结论

独立只读 `pi -p` 审核发现 1 个高风险、1 个中风险阻塞项。

### H1. 任意登录异常可能泄漏 Token

`modelAuthManager._runTask()` 原本把任意异常的 `str(error)` 写入公开 task。注入异常若含 Authorization、Access/Refresh Token，会被浏览器轮询接口返回。

修复：

- 仅可信 `modelAuthError` 使用其统一脱敏文本；
- 其它异常固定映射为安全消息，不公开异常文本或类型携带内容；
- credential status 读取异常使用同一安全映射；
- 新增登录函数抛出 Access/Refresh/Authorization canary 的任务响应与 stdout/stderr 扫描测试。

### M1. 并发 open 逆序响应覆盖新工作副本

连续两次 `settingsView.open()` 时，旧 GET 可能后返回并覆盖新配置，再基于当前 revision 启动 discovery。

修复：

- 每次 open 递增并捕获独立 `openRevision`；
- Promise 返回、异常处理、自动 discovery 前均校验仍是当前 revision；
- 旧 open 只静默丢弃结果，不覆盖工作副本、错误栏或启动 discovery；
- 新增纯竞态 helper 与 Node 逆序提交断言。

## 修复后复审

再次独立只读复审确认：

- 任意登录异常不再直接公开 `str(error)`；
- `openRevision` 覆盖响应、异常和自动发现三个提交点；
- canary、Node race 及全量测试均通过；
- 最终结果为 **47 passed**；
- 未发现仍阻塞交付的高/中风险，结论为**可交付**。

## 真实环境代理缺陷与修复

用户真实 Web 同步返回 `network_error`。无 Token 对照诊断确认：

- 环境配置 `HTTPS_PROXY`；
- `http.client` 直连 `api.x.ai` 超时；
- 代理感知 `urllib` 能到达 xAI。

修复为固定 URL 的 proxy-aware urllib opener，并以自定义 redirect handler 拒绝 301/302/303/307/308；HTTPError 被转换成有界内部响应，异常文本和请求头不公开。测试通过本地双服务器断言 Location 目标零请求，并覆盖 ProxyHandler、HTTPError、响应上限和 canary。

专项独立复审结论：无高/中风险，可交付。最终全量结果为 **49 passed**。
