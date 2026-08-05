# 方案审核报告 — initAgentCustomizationPlan（初始化时指定 system prompt 与可用工具）

审核对象：`docs/initAgentCustomizationPlan.md`
对照源码：`flamingoAgents/builder.py`、`flamingoAgents/core/agent.py`、`flamingoAgents/core/conversation.py`、`flamingoAgents/tools/toolRegistry.py`、`flamingoAgents/tools/toolDefinition.py`、`flamingoAgents/tools/toolConfig.py`、`flamingoAgents/tools/builtinTools.py`、`flamingoAgents/tools/toolSchema.py`、`flamingoAgents/models/chatCompletions.py`、`flamingoAgents/__init__.py`、`askModel.py`

## 总览

- 审核文档：1 份方案 + 11 个源码文件
- 发现问题：🔴 0 / 🟠 0 / 🟡 3 / 🔵 4
- 整体评价：**方案与现状代码事实完全相符，设计方向正确（改动收敛在装配层、复用现有查重机制），无过度设计，YAGNI 清单合理**。问题集中在：用法示例不可运行、两个边界语义未定义、验证步骤断言不够具体。均为落地前需补充的小项，不影响方案通过。

## 事实核查（全部通过）

| 方案声明 | 核实结果 |
|---|---|
| `agent.__init__` 已接受 `systemPrompt: str` 与 `toolDefinitions` 注入 | ✅ 属实（agent.py:44-65），核心层确实无需改动 |
| `createAgent` 仅支持 `systemPromptPath`，工具全量装配 | ✅ 属实（builder.py:51-56） |
| `toolRegistry` 重复名抛错 | ✅ 属实（toolRegistry.py:20-22，抛 `RuntimeError`） |
| `defineTool()` 辅助函数已存在 | ✅ 属实（toolDefinition.py:37） |
| `tools.yaml` schema 为 version 3 | ✅ 属实（toolConfig.py:55 强制校验，`config/tools.yaml` 实际为 3） |
| resume 时 prompt 以创建时注入为准 | ✅ 属实（conversation.py v1.8：resume 从日志恢复 systemMessage） |
| `askModel.py` 可作为不传新参数的回归基线 | ✅ 属实 |

## 问题清单

### 🟡 用法示例缺少 `toolOutput` 导入，直接运行会 NameError

**位置**: 方案「用法示例」代码块
**问题**: 示例中 `execute=lambda args, ctx: toolOutput(...)` 使用了 `toolOutput`，但 import 只导入了 `createAgent` 和 `defineTool`。初级程序员照抄会得到 `NameError: name 'toolOutput' is not defined`。
**修复方案**: 示例补充导入：

```python
from flamingoAgents import createAgent
from flamingoAgents.core.types import toolOutput
from flamingoAgents.tools.toolDefinition import defineTool
```

### 🟡 `toolNames=[]`（空白名单）语义未定义

**位置**: 方案「处理规则」第 2 条
**问题**: 规则只定义了 `toolNames is not None` 时按名过滤。`toolNames=[]` 会合法地产出零工具 agent，此时 `buildModelTools([])` 返回 `[]`，而 `chatCompletionsAdapter.buildRequestPayload` 无条件发送 `'tools': []`（chatCompletions.py:47）——OpenAI 兼容接口通常接受空数组，但部分 provider 可能报错。方案未说明这是「允许的纯对话模式」还是应当报错。
**修复方案**: 在处理规则中补一条明确约定，二选一：
- 允许：`toolNames=[]` 表示纯对话 agent（无工具），文档注明；
- 禁止：`toolNames` 非 None 且为空列表时抛 `RuntimeError('toolNames 不能为空列表，如需全部工具请传 None')`。

推荐前者（更灵活，实现零成本），但必须写明。

### 🟡 验证步骤缺少具体断言，「冒烟」定义过泛

**位置**: 方案「实施步骤与验证」第 1、2 条
**问题**: 「`uv run python -c` 冒烟，各走一条路径」没有说明断言什么。按项目「目标驱动执行」规则，验证应写成可判定的检查。另外构造 agent 会真实执行 `loadModelConfig`/`createModelAuth`，依赖 `config/models.yaml` 与 API key 存在，这一前置条件未注明（本仓库可满足，但应写明）。
**修复方案**: 将验证细化为无需真实调用模型、仅构造 agent 后断言内部状态的检查，例如：

```bash
uv run python - <<'EOF'
from flamingoAgents import createAgent
a = createAgent('.', systemPrompt='  测试  ', toolNames=['read'])
assert a.systemPrompt.startswith('测试'), '直传 prompt 未生效'
assert '## 当前时间' in a.systemPrompt, '时间段落缺失'
assert [t.name for t in a.toolRegistry.list()] == ['read'], '白名单过滤失败'
EOF
```

错误路径同理：`toolNames=['nope']` 应抛 `RuntimeError`；`extraTools` 与内置重名应抛 `RuntimeError`；`appendCurrentTime=False` 时 prompt 不含「## 当前时间」。

### 🔵 `systemPrompt` 与 `systemPromptPath` 同时传入时静默覆盖

**位置**: 方案「处理规则」第 1 条
**问题**: 两者同传时 `systemPrompt` 静默胜出。调用方若误以为文件生效，排查困难。
**修复方案**: 实现时在两者同传的情况下打一条 debug 日志（复用现有 `printer.debug`），例如 `系统提示词来源=direct（systemPromptPath 已忽略）`。不需要抛错。

### 🔵 未知名报错的报错信息未约定内容

**位置**: 方案「处理规则」第 2 条
**问题**: 只说「抛 `RuntimeError` 明确报错」。建议约定报错信息包含未知名与当前可用名列表，否则调用方要翻 yaml 才能改正。
**修复方案**: 报错格式约定为：
`RuntimeError(f'未知内置工具：{unknown}，可用工具：{sorted(available)}')`

### 🔵 实施步骤遗漏项目规范动作

**位置**: 方案「实施步骤与验证」
**问题**: 按 AGENTS.md 代码规范，修改 `builder.py` 需将文件头 Version 1.3→1.4 并更新 Description；方案未提及。另外可考虑在 `flamingoAgents/__init__.py` 顶层导出 `defineTool`/`toolOutput`（当前 `__all__` 未包含），调用方少写两行深层 import——可选，不做也能用。
**修复方案**: 实施步骤第 1 条追加「更新 builder.py 文件头版本与描述」。

### 🔵 顺带提醒（现状问题，非本方案引入）

`defaultSystemPromptPath`（builder.py:20）与 `defaultToolsConfigPath`（toolConfig.py:29）都解析到仓库根 `config/` 目录。项目定位为「Python Agent 库」，若未来打包分发而 config 未随包安装，默认路径会失效。本方案不涉及，不阻塞；记录在案，待打包需求出现时再处理。

## 优点记录

- **改动面控制得当**：只动装配层一个函数，核心层、schema、事件流 API 均不碰，与现状注入式设计契合。
- **复用现有机制而非另起炉灶**：重名冲突交给 `toolRegistry` 现有查重，自定义工具复用 `defineTool`，没有重复实现。
- **YAGNI 清单明确**：agentProfile、按文件禁用工具、运行时切换均合理推迟。
- **错误取向正确**：白名单未知名选择显式抛错而非静默丢弃，符合健壮性原则。

## 修复优先级建议

1. **🟡 示例导入修复** — 文档示例是调用方的第一触点，必须可运行；
2. **🟡 `toolNames=[]` 语义** — 落地前一句话约定即可，不约定则行为靠运气；
3. **🟡 验证断言具体化** — 把「冒烟」落成可判定断言，保证实施后可独立验收。

三个 🟡 均为文档补充性质，修改成本极低。**结论：方案通过，补齐上述 3 个中等级问题后即可实施。**
