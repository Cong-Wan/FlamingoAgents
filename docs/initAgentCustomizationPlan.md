# 初始化时指定 system prompt 与可用工具 —— 方案计划

## 背景与目标

当前创建 Agent 只能通过 `createAgent(workDir, systemPromptPath=..., toolsConfigPath=...)` 指定提示词文件路径，工具集固定从 `config/tools.yaml` 全量加载。目标：让调用方在初始化时可以直接指定：

1. system prompt（字符串直传，不再强制走文件）；
2. 可用工具（内置工具白名单子集）。

## 现状分析结论

- `agent.__init__` 已经是注入式设计：接受 `systemPrompt: str` 与 `toolDefinitions: list[toolDefinition]`，**核心层无需改动**。
- 瓶颈在装配层 `flamingoAgents/builder.py` 的 `createAgent`：仅支持 `systemPromptPath` 文件路径，工具由 `loadToolSettings` + `createBuiltinTools` 全量装配，无法筛选与追加。
- `toolRegistry.__init__` 已有按名查重（重复即抛错），`toolDefinition.defineTool()` 辅助函数已存在，自定义工具可直接复用。

## 方案（最小改动，不动架构）

仅修改 `flamingoAgents/builder.py` 的 `createAgent`，新增 3 个关键字参数：

```python
def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    logDir: str | Path | None = None,
    modelConfigPath: str | Path | None = None,
    toolsConfigPath: str | Path | None = None,
    systemPromptPath: str | Path | None = None,
    systemPrompt: str | None = None,             # 新增：提示词文本直传，优先级高于 systemPromptPath
    appendCurrentTime: bool = True,              # 新增：是否追加"## 当前时间"段落
    toolNames: list[str] | None = None,          # 新增：内置工具白名单，None=全部，[]=纯对话
    providerId: str = '101',
    modelId: str | None = None,
) -> agent:
```

### 处理规则

1. **system prompt 解析优先级**：`systemPrompt`（直传字符串，strip 后非空）> `systemPromptPath` > 默认 `config/systemPrompt.md`。三者来源最终统一走同一段"追加当前时间"逻辑（`appendCurrentTime=False` 时跳过）。`systemPrompt` 与 `systemPromptPath` 同时传入时打 debug 日志说明直传生效。
2. **工具筛选**：`toolNames is not None` 时，从 `createBuiltinTools` 产物中按名过滤；白名单中出现不存在的内置工具名 → 抛 `RuntimeError`，报错信息附带可用内置工具名列表（避免静默丢工具）。`toolNames=[]` 为合法输入，表示"纯对话模式"（零工具 agent）。
3. `agent` 核心、`tools.yaml` schema（version 3）、事件流 API 均不改动。

### 用法示例

```python
from flamingoAgents import createAgent

agent = createAgent(
    projectDir,
    systemPrompt='你是一个只读代码审查助手，禁止修改任何文件。',
    toolNames=['read'],  # 只保留 read
)
```

## 实施步骤与验证

前置条件：构造 agent 依赖 `config/models.yaml` 与有效 API key；验证脚本复用 `providerId='volcano'`（与 askModel.py 一致）。

1. 修改 `builder.py`：新增参数 + prompt 来源优先级逻辑 + 工具筛选/合并，文件头版本 1.3 → 1.4 并更新 Description。
2. 目标导向验证（`uv run python - <<'EOF'` 脚本断言，不引入测试框架）：
   - 直传 prompt：断言 `agent.systemPrompt` 以直传文本开头；`appendCurrentTime=True/False` 时分别断言含/不含 `## 当前时间`。
   - 白名单：`toolNames=['read']` → 断言 `[d.name for d in agent.toolRegistry.list()] == ['read']`；`toolNames=[]` → 断言为空列表（纯对话模式）。
   - 错误路径：`toolNames=['nope']` 断言抛 `RuntimeError` 且消息含可用工具名。
3. 回归：`uv run python askModel.py`（不传新参数）行为与现状一致——默认 prompt 文件 + 追加时间 + 全量内置工具，事件流正常跑通一轮对话。

## 不做的事（YAGNI）

- 不引入 agentProfile/角色配置对象（多角色场景出现时再演进，届时只是把上述参数打包成 dataclass）。
- 不支持追加自定义 callable 工具（用户已确认 1A：仅内置白名单；现有 `defineTool` 能力保留在库里，未来需要时再加 `extraTools` 参数）。
- 不改 `tools.yaml` schema、不支持按文件禁用单个工具。
- 不支持运行时（per-session）切换 prompt/工具；会话续跑（resume）时 prompt 仍以创建时注入的为准（现状如此，不改）。
