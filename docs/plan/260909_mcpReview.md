# MCP 方案独立审核记录

> Author: wilbur  
> Version: 1.0  
> Date: 2026-09-09  
> Description: 独立子代理只读审核记录。openaiCodex/volcano 两次空返回后，用 xaiSubscription/grok-4.6 完成第一轮；方案已修到 v1.2，待第二轮复审。

## 第一轮（xaiSubscription/grok-4.6，只读）

**结论：有明显问题（1 严重，若干中/轻）。无致命。**

已吸收进 `260909_mcpPlan.md` / `260909_mcpTasks.md` v1.2：

| 级 | 问题 | v1.2 处理 |
|---|---|---|
| 严重 | 全局 409 不含 pending，always 确认主路径会 crashRecovered | §6.2 全局忙第 3 条 + 测试 `testWebPut409WhenPendingConfirm` |
| 中 | 调用超时杀共享 stdio | 超时只取消该 request；杀进程仅用户停止且 cancel 无响应，随后 backoff 重连 |
| 中 | PUT/lifespan/test 可能占 FastAPI loop 或持 managerLock 做 I/O | 锁外 warmup；路由 to_thread；禁止 FastAPI loop 上 async with Client |
| 中 | CLI 先 runtime 后 ensure，缺 mcp.yaml | createMcpRuntime/askModel 先 ensure；缺文件当空 |
| 轻 | SSRF 表述过满 | 改成实际允许列表 |
| 轻 | 关闭等待无上限 | inFlight==0 或最多 2s |
| 轻 | env 文本格式未写死 | 写死 `Key: Value` |

审核员确认未误述：getAgent 持锁、validateArguments 放行、sdkEntry 拒确认、无 lifespan、credentialStore 限制、driveToolBatch 串行。

应保留要点见该次输出：snapshot 纯内存、长驻 Client、每 server Lock、skipLocalSchema 选 A、子代理无 MCP、密钥不走 credentialStore。

## 第二轮（xaiSubscription/grok-4.6，只读，针对 v1.2）

**结论：有明显问题（2 严重）。无致命。** 第一轮已修项未重复开单。

已吸收进 v1.3：

| 级 | 问题 | v1.3 处理 |
|---|---|---|
| 严重 | 409 与 reload 非原子，warmup 窗口可绕过 pending 保护 | `mcpReloading` 门闩；startStream/chatConfirm 也 409；chatConfirm stale+pending 走 getCachedAgent |
| 严重 | 冲突名 RuntimeError 让内置工具起不来 | snapshot 跳过后来者；createAgent 不得因 MCP 撞名抛错 |
| 中 | T8 只写活跃流 | 必须调用 T7 全局忙 |
| 中 | snapshot 测试可假绿 | 调用记录而非墙钟 |
| 轻 | 拆 supervisor 漏 reconnect | close/PUT/reconnect |

## 第三轮（xaiSubscription/grok-4.6，只读，针对 v1.3）

**结论：无明显阻塞问题。** 致命 0 / 严重 0。

非阻塞注记已写回方案正文：load/PUT 分层、门闩 `finally`、路由层 409 不复用 `startStream is None`、skipLocalSchema 测试改用 `additionalProperties: false`。
