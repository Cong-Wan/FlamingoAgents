# MCP 接入：实施 TODO 与测试计划

> Author: wilbur  
> Version: 1.3  
> Date: 2026-09-09  
> Description: 对应 260909_mcpPlan.md v1.3。mcpReloading 门闩、冲突名跳过而非 createAgent 失败。确认方案前不实施。

配套：[方案](260909_mcpPlan.md) · [调研](260909_mcpResearch.md)

依赖约定：当前 Python 3.13.13、uv、pytest。实施时先 `uv add 'mcp>=2.2.0,<3'`，再跑 `uv run pytest -q` 确认原 209 项仍通过。不要 `mcp[cli]`，不要升级 Python。

## 0. 开工前（人工确认，无代码）

- [ ] 确认角色是 host/client，不是对外 MCP server
- [ ] 确认传输范围：stdio+HTTP / 仅其一
- [ ] 确认默认 `approvalMode`
- [ ] 确认可添加 `mcp` 依赖
- [ ] 确认 v1 全会话共享启用 server、子代理默认无 MCP
- [ ] 确认空 `mcp.yaml` 可自动拷贝

## P0 — 库

### T0 配置路径与模板

- [ ] `configPaths.py`：`userMcpPath = userConfigDir / 'mcp.yaml'`；`ensureUserConfig` 从 `config/mcp.yaml` `_copyIfMissing`
- [ ] 新增 `config/mcp.yaml`（version 1, servers: []）
- [ ] 新增 `config/mcp.example.yaml`（stdio/http 各一例，密钥只用 `$ENV`）
- [ ] 更新 `config/README.md`、必要时 README 路线图一句
- [ ] 测试：`testConfigPaths.py` 增补拷贝/不覆盖/不创建 secrets 文件

### T1 解析器 `mcpConfig.py` / `mcpNames.py`

- [ ] dataclass：`mcpServerSpec` / `mcpSettings`
- [ ] 校验 5.2 全部规则；中文 `RuntimeError`
- [ ] `flamingoName(prefix, original)`：消毒、64 长度、冲突检测
- [ ] 测试：合法 stdio/http；拒绝 shell 式 command；拒绝 http+command 混用；拒绝非 localhost 的 http://；拒绝与 builtin 撞名；超过 16 server

### T2 密钥 `mcpSecrets.py`

- [ ] 路径 `~/.flamingo/mcp.secrets.json`（可注入 baseDir 便于测试）
- [ ] 读缺失=空；写 0600；serverId 必须已在 yaml
- [ ] 解析 `$ENV`：未设置则该 server 连接失败，不回退空字符串静默连上
- [ ] 测试：权限位、合并、不把密钥 `repr` 进异常消息（断言 `str(error)` 不含 token）

### T3 结果文本化 `mcpContent.py`

- [ ] 覆盖 text / image / audio / resource_link / 未知 type / structuredContent / isError / 空 / 截断
- [ ] 测试用 SDK 的类型或简单 namespace，不需要真连

### T4 runtime 线程桥 `mcpRuntime.py`

- [ ] `createMcpRuntime(settings=None, configPath=None, secrets=None)` 与测试辅助 `createMemoryMcpRuntime(mcpServer)`
- [ ] 专用线程 + **唯一** loop；`runCoroutine` 0.1s poll
- [ ] 每 server 一个长驻 supervisor（`async with Client` 直到 cancel）；HTTP 的 httpx client 同生命周期
- [ ] `warmup(serverIds)` 并行预热；`snapshotDefinitions(serverIds)` **纯内存、无 I/O**
- [ ] 每 server `asyncio.Lock` 串行 list/call；失败隔离
- [ ] `callTool(...)` 使用 `context.interruptEvent`；超时只取消该 request，**不**杀共享 stdio 树；用户停止且 cancel 无响应才允许杀并 backoff 重连
- [ ] `close()` 幂等；inFlight 计数
- [ ] **禁止**注册 elicitation/sampling/roots callback
- [ ] 测试：内存 server 预热后 snapshot 不含 I/O；并发两次 snapshot 不二次 connect；close 后再 call 失败；中断；两调用串行

### T5 工具适配 `mcpTools.py` + `toolDefinition.skipLocalSchema`

- [ ] `toolDefinition` 增加 `skipLocalSchema: bool = False`；`defineTool` 透传
- [ ] `toolRuntime.executeToolCall`：True 时跳过 `validateArguments`
- [ ] MCP execute 闭包捕获 runtime/serverId/originalName
- [ ] always 模式挂 `.*` permissionRule
- [ ] preview：截断 JSON
- [ ] details：`mcpServerId`/`mcpToolName`/`truncated`，无密钥
- [ ] 测试：带 `additionalProperties: false` 的多余字段在 skipLocalSchema 下仍能调用（不要用 boolean 叶子，现校验对 boolean 本就放行）；always 会 `requiresApproval`

### T6 `createAgent`

- [ ] 参数 `mcpRuntime` / `mcpServerIds`
- [ ] None runtime = 现行为
- [ ] 只调用 `snapshotDefinitions`，装配路径无 connect
- [ ] 冲突 flamingoName：跳过 MCP 侧，内置工具仍注册，不得 RuntimeError
- [ ] `createMcpRuntime`/`askModel` 先 `ensureUserConfig()`；缺 mcp.yaml 当空 servers
- [ ] `askModel.py` 先 warmup 再 createAgent，finally close
- [ ] `sdkEntry.py` 不传 runtime
- [ ] 测试：无 runtime 时工具集 = 内置；runtime+空 servers 仍 = 内置；sdkEntry 即使存在 mcp.yaml 也不装 MCP

## P1 — Web

### T7 lifespan 与 manager

- [ ] 启动函数创建 runtime 并 **warmup**（避免 `server.py` import 副作用）
- [ ] lifespan 关闭：先停所有泵，再 `runtime.close()`
- [ ] `agentManager.setMcpRuntime`；`getAgent` 传入 runtime 且 **持锁区内零 MCP I/O**；`dropAgent` 不 close runtime
- [ ] 全局忙 = activeStream **或** inFlight **或** pending **或** `mcpReloading`
- [ ] 同一 `managerLock` 置位 `mcpReloading` 后再锁外 warmup；`startStream`/`chatConfirm`/PUT/reconnect 都看这扇门
- [ ] `chatConfirm` 对 stale+pending 走 `getCachedAgent`，禁止重建

### T8 `mcpConfigStore` + 路由

- [ ] GET 脱敏、缺文件走 ensure 后的空列表
- [ ] PUT 校验、`__KEEP__` 合并、唯一 tmp 名原子写；409 **必须调用 T7 全局忙**（禁止路由只判 activeStreams）；成功则门闩内 reload+invalidate
- [ ] POST test：不写盘、有超时、返回 tools/skipped
- [ ] POST reconnect：409 规则同 PUT
- [ ] 更新 `docs/webApiSpec.md`
- [ ] 测试：仿 `testModelConfigStore`/`testModelAuthWeb`：脱敏、KEEP、非法 URL、活跃流 409

### T9 前端

- [ ] 侧栏入口、hash、空页面壳、表单、测试连接、保存条
- [ ] `api.js` 四个方法
- [ ] 无构建；复用现有 CSS 变量
- [ ] 测试：`test*Frontend.py` 对 html/js 做字符串契约断言（项目现有风格），检查路由与 API path

## 测试矩阵（实施时必须落地的用例名建议）

| 用例 | 类型 | 期望 |
|---|---|---|
| `testEnsureUserConfigCopiesEmptyMcpYaml` | 单元 | 拷贝且不覆盖 |
| `testParseStdioAndHttpSpecs` | 单元 | 圆Trip 字段 |
| `testRejectShellCommandAndNonLocalHttp` | 单元 | 中文错误 |
| `testFlamingoNameCollisionWithBuiltin` | 单元 | 失败 |
| `testSecretsNotInErrorString` | 单元 | token 不出现 |
| `testRenderMcpContentTruncation` | 单元 | truncated |
| `testInMemoryMcpToolRoundtrip` | 单元 | skipLocalSchema + 返回 5 |
| `testMcpApprovalAlways` | 单元 | confirmationRequired |
| `testMcpInterruptRaisesModelInterrupted` | 单元 | 抛指定异常 |
| `testCreateAgentWithoutRuntimeUnchanged` | 单元 | 工具名集合 |
| `testCreateAgentUsesSnapshotOnly` | 单元 | snapshot 1 次；warmup/runCoroutine/callTool/connect 均为 0 |
| `testMcpNameCollisionDoesNotBreakBuiltins` | 单元 | 内置仍在，冲突工具被跳过 |
| `testSdkEntryHasNoMcp` | 单元 | 不连 mcp.yaml 即使存在 |
| `testWebGetMasksSecrets` | Web | `__KEEP__` |
| `testWebGetDoesNotSpawn` | Web | GET 后 connectCount 不变 |
| `testWebPut409WhenAnyStreamActive` | Web | 其他 session 在流也 409 |
| `testWebPut409WhenPendingConfirm` | Web | waitingConfirm 无泵也 409，且不写 crashRecovered |
| `testMcpReloadingBlocksNewStreamAndConfirm` | Web | PUT warmup 期间另一会话 stream/confirm 409，结束后无 crashRecovered |
| `testCallTimeoutDoesNotKillSharedStdio` | 单元 | 超时后仍能再 call |
| `testCreateMcpRuntimeMissingYamlIsEmpty` | 单元 | 不抛；无工具 |
| `testWebTestConnectionTimeout` | Web | 400 超时文案；试连进程被关掉 |
| `testTwoSessionsShareOneRuntime` | 集成 | 同 pid/同 connectCount |
| `testGetAgentLockHasNoMcpIo` | 单元 | 调用记录而非墙钟 |

不测：真实 GitHub/npx 网络、OAuth、Inspector、Windows。

## 建议实施顺序

```text
T0 → T1 → T2 → T3 → T5(skipLocalSchema) → T4 → T6 → 全量 pytest
T7 → T8 → T9 → 契约文档 → 全量 pytest
```

T4 依赖 T1/T3/T5。T6 依赖 T4 的 snapshot 合同。T8 依赖 T0/T1/T2/T7。不要先做前端。实施时顺手改 `docs/addCallableToolFunction.md` 一句与 `docs/webApiSpec.md`。

## 每步验证

1. 改配置路径 → `uv run pytest tests/testConfigPaths.py -q`
2. 每加一个 mcp 模块 → 对应 `tests/testMcp*.py`
3. 任何时候准备提交前 → `uv run pytest -q` 必须全绿
4. 手工（可选，非 CI）：settings 页保存一个 `Client(MCPServer)` 不可行，手工用本地 `mcp run` 仅在你明确要求时做
