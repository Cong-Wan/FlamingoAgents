Author: wilbur
Version: 1.0
Date: 2026-09-01
Description: subscriptionLoginPlan v1.1 实施前独立复审（pi -p 默认模型、低推理档、无项目上下文递归）；记录 2 个高风险和 4 个中风险阻塞项，计划 v1.2 已据此修订。

# 订阅登录方案实施前复审

## 结论

v1.1 方向正确，但需先修订以下阻塞项；`docs/plan/subscriptionLoginPlan.md` v1.2 已落实。

## 高风险

### H1. Responses item 不能未经筛选直接持久化并回放

服务端 output item 可能含只允许出现在响应中的 `status`、内部字段或未来扩展字段。持久化与下一轮发送必须使用类型白名单序列化：

- reasoning：`id/type/summary/encrypted_content`
- message：`id/type/role/content/phase`（仅协议允许字段）
- function_call：`id/type/name/call_id/arguments`
- function_call_output：`type/call_id/output`

未知字段不能进入下一轮请求；增加带未知字段、`status`、空密文的测试。

### H2. terminal `response.output` 应是全部 item 的最终权威状态

不能只回填 reasoning 密文。finalize 应按 item ID 合并 message、reasoning、function call；终态 `arguments/call_id/name` 覆盖增量状态，终态新增 item 也必须补建 completion。增加缺失 `output_item.done`、缺失 arguments done、仅 terminal 存在 function call 的测试。

## 中风险

### M1. 固定 1455 回调端口需要全局互斥和降级

浏览器登录使用进程内全局互斥；bind 失败转为 manual-code 状态而非登录失败；成功、取消、超时、异常均关闭 server。多进程场景依靠固定锁文件协调，只允许一个回调监听者。

### M2. 取消不能直接打断阻塞中的 urllib

所有 OAuth HTTP 请求设置有限超时；网络返回后、token 交换前、凭据写入前复查取消。任务状态通过锁内 CAS 保证 cancelled 后不能 completed；任务清理不得允许迟到 worker 写凭据。

### M3. 凭据路径需防 symlink/错误 owner

所有 write/delete/refresh 使用稳定 `auth.lock` 覆盖完整 read-modify-write；目录、锁文件、凭据文件用 `lstat` 校验非 symlink、归当前 uid；临时文件排他创建为 0600，replace 后 fsync 父目录。

### M4. Provider/模型切换时工具调用与结果必须成对处理

exact match 才回放 opaque Responses item。非 exact 时只成对重建 function call/output；孤立 output 不得发送。无法可靠配对的组整体转成带明确标记的普通文本或省略，并测试切模型、切 API、缺失结果与孤立结果。
