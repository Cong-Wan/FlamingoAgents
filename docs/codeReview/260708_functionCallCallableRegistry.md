# 代码审核报告 — Function Call Callable Registry 迁移

## 总览

- **审核范围**：本次按 `docs/flare/20260708_functionCallCallableRegistry.md` 执行计划完成的全部产出
- **审核文件**：13 个
  - 新建：`toolDefinition.py`、`toolRegistry.py`、`builtinTools.py`、`docs/addCallableToolFunction.md`
  - 重写：`types.py`、`debug.py`、`toolConfig.py`、`toolRuntime.py`、`toolPolicy.py`、`toolSchema.py`、`agent.py`、`builder.py`、`manualChecks.py`、`config/tools.yaml`
- **发现问题**：🔴 0 个 / 🟠 1 个 / 🟡 0 个 / 🔵 3 个
- **验证状态**：`uv run python manualChecks.py all` 静默通过（exit 0）；`--debug` 下 9 项检查全 PASS；全量 `py_compile` 通过；迁移完整性扫描无残留旧 API
- **整体评价**：迁移干净、彻底，依赖方向正确，核心数据结构边界清晰。无生产代码 bug。唯一实质问题是 `manualChecks` 里一个验证用例**结构上不可能失败**，削弱了验证可信度。

---

## 问题清单

### 🟠 [Medium] `runAdapterParseCheck` 的"非法 arguments 拒绝"用例永远不可能失败

**位置**：`manualChecks.py` → `runAdapterParseCheck`

**问题**：
```python
for badArguments in ['[]', '"abc"', '{bad json']:
    try:
        adapter.parseAssistantPayload({...arguments: badArguments...})
        raise RuntimeError('非法 arguments 没有被拒绝')   # ← 只有上一行不抛时才执行
    except RuntimeError:
        pass                                              # ← 同时吞掉上面两种来源
```

`parseAssistantPayload` 拒绝非法 arguments 时抛 `RuntimeError`，被 `except RuntimeError` 捕获 → pass；
若它**没**拒绝（即有 bug），则下一行显式 `raise RuntimeError(...)`，**同样**被同一个 `except` 捕获 → pass。

两条路径都走到 `pass`，这个用例**无论实现对错都会"通过"**，等于零覆盖。它给项目唯一的验证入口提供了虚假的信心——违反 AGENTS.md「循环验证直到通过」的前提（验证必须真能失败）。

注：此模式是从旧版 `manualChecks` 原样继承的，并非本次新引入，但 Task 6 整体重写了该文件，宜顺手修正。

**修复方案**（用哨兵区分"被测代码抛错"与"测试自身断言"）：
```python
for badArguments in ['[]', '"abc"', '{bad json']:
    rejected = False
    try:
        adapter.parseAssistantPayload({
            'choices': [{'message': {
                'role': 'assistant', 'content': '',
                'tool_calls': [{'id': 'call_bad', 'type': 'function',
                                'function': {'name': 'read', 'arguments': badArguments}}],
            }}],
        })
    except RuntimeError:
        rejected = True
    expect(rejected, f'非法 arguments 没有被拒绝：{badArguments}')
```

---

### 🔵 [Low] readTool / bashTool 内存在被 schema 已保证不可能的防御分支

**位置**：`builtinTools.py` → `readTool`、`bashTool`

**问题**：
- `readTool`：`if offset < 1 or limit < 1` —— schema 已声明 `offset/limit` 的 `minimum: 1`，executor 在调用前已做 schema 校验，到函数体时 `offset>=1, limit>=1` 必然成立，此分支不可达。
- `bashTool`：`if timeout < 1: timeout = defaultTimeoutSeconds` —— 同理，schema `minimum: 1` 已保证，不可达。

这是无害的防御性代码（且来自 recipe §9 的参考实现，是有意为之），不强制修改。仅提示：在 callable 边界已统一由 executor 做 schema 校验后，工具内部重复校验属冗余。若希望工具函数也能脱离 executor 独立被调用，保留是合理的——这是设计取舍，**保持现状即可**。

---

### 🔵 [Low] `buildModelTools` 每个模型循环步都重建 schema 列表

**位置**：`agent.py` → `continueModelLoop`

**问题**：`modelTools = buildModelTools(self.toolRegistry.list())` 在每个 step（最多 `maxModelSteps=8` 步）都重新投影 schema。registry 内容在一次会话中不变，可缓存。

**说明**：这是从旧版 agent 原样保留的行为（旧版也是每步重建），本次未引入。4 个工具 × 最多 8 步的开销可忽略，**不建议为此改动**，仅作记录。

---

### 🔵 [Low] 两个工具检查在 `--debug` 下重复加载配置

**位置**：`manualChecks.py` → `loadDefinitions` 被 `runToolConfigCheck` / `runPermissionCheck` / `runToolRuntimeCheck` / `buildFakeAgent` 多次调用

**问题**：`all` 模式下会重复执行 `loadToolSettings()` + `createBuiltinTools()` 约 5 次。无正确性问题，仅 `--debug` 日志略显冗长。

**说明**：这是验证脚本的常见做法（每个检查保持独立、无共享状态），属可接受的取舍，**不改**。

---

## 优点记录

1. **依赖方向正确**：`permissionRule`/`permissionAction` 上移到 `toolDefinition.py`，形成 `toolConfig → toolDefinition → core.types` 的单向依赖，避免了核心数据结构反向依赖配置加载层。无循环 import。
2. **`toolOutput` / `toolResult` 职责分离干净**：工具函数只产出业务内容（`content/isError/details`），`toolCallId/toolName` 由 executor 统一包装，与 pi 的 `AgentToolResult` 思路一致。
3. **preview 函数被安全包裹**：`agent.buildToolPreview` 对 `definition.preview()` 做 try/except，且 preview 在 schema 校验**之前**就拿到原始模型参数——preview 函数内部的 `int()` 强转等因此不会导致崩溃，设计稳健。
4. **integer 校验正确排除 bool**：`validateValue` 中 `if not isinstance(value, int) or isinstance(value, bool)` 修正了旧版 `toolRuntime` 把 `True` 当整数 1 放行的隐患。
5. **executor 全面异常包装**：`prepareArguments` 异常、`execute` 异常、非 dict arguments、schema 错误四类都收敛为 `toolResult(isError=True)`，模型循环不会因单个工具崩溃而中断。
6. **迁移彻底**：源码中无任何 `loadToolConfig` / 旧 `from toolConfig import toolDefinition` / `executeTool(` / `.runtime` 残留；`config/tools.yaml` 干净升级到 `version: 2`。

---

## 修复优先级建议

1. **🟠 修正 `runAdapterParseCheck` 的坏用例** —— 这是唯一实质问题。验证入口里一个"永不失败"的用例会掩盖 adapter 解析回归，应改为哨兵式断言。建议本次顺手修掉。
2. 🔵 其余 3 项均为记录性 Low，**保持现状**即可，无需改动。

---

## 结论

本次 callable registry 迁移实现质量高、与 recipe 完全对齐、验证全部通过。生产代码无 Critical/High 问题。建议仅修复 🟠 级的验证用例缺陷后即可视为完成。

---

## 处置记录（2026-07-08 复核后）

针对上述问题逐项处置如下，确保每项 review 点都被有意识地闭环。

### 🟠 `runAdapterParseCheck` 零覆盖用例 —— 已修复

- **处置**：按本 review 建议，改为哨兵式断言（`rejected` 布尔 + 在 `try` 之外调用 `expect`），区分「被测代码抛错」与「测试自身断言」。
- **位置**：`manualChecks.py` → `runAdapterParseCheck`，文件头版本 2.1 → 2.2。
- **修复计划**：`docs/flare/20260708_fixAdapterParseCheck.md`。
- **牙齿验证**：用一个故意不抛错的桩函数模拟「实现不拒绝」的回归，确认修复后的 `expect(rejected, ...)` 能正确抛出「没有被拒绝」，即该用例现在「真能失败」。

### 🔵 readTool / bashTool 冗余防御分支 —— 保持现状

- **处置**：不修改。
- **理由**：工具函数（`readTool`、`bashTool`）被设计为可脱离 executor 独立调用——`docs/addCallableToolFunction.md` 的「写真实执行函数」一节明确把「工具函数直接接收 `arguments`」作为契约。保留内部对 `offset/limit/timeout` 的下界校验，使工具在被其他代码直接调用时也具备鲁棒性。这与 recipe §9 的参考实现一致，是有意为之的设计取舍，而非疏漏。executor 的 schema 校验与工具内部校验是两层独立保障，互不替代。

### 🔵 `buildModelTools` 每个模型步重建 —— 保持现状

- **处置**：不修改。
- **理由**：registry 在一次会话内不变，但 `buildModelTools` 的开销（4 个工具 × 最多 `maxModelSteps=8` 步的纯 dict 投影）可忽略；引入缓存会让 agent 多维护一份「schema 是否过期」的状态，增加复杂度，违反 YAGNI。该行为从旧版原样继承，本次迁移不改变其性能特征。

### 🔵 检查间重复加载配置 —— 保持现状

- **处置**：不修改。
- **理由**：`manualChecks.py` 的每个检查函数保持独立、无共享可变状态，是验证脚本的常见且健康做法（便于单独运行某个检查、避免检查间互相污染状态）。`--debug` 日志略冗长是无害代价，不值得用共享缓存换取。
