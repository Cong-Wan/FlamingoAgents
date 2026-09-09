<!--
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: configHomePlan P1–P6 实现审核。对照 docs/plan/260908_configHomePlan.md。
-->

## 代码审核报告 — configHomePlan P1–P6

### 总览
- 审核文件：16 个（含新建 configPaths.py / testConfigPaths.py / config/README.md）
- 发现问题：🔴 0 个 / 🟠 0 个 / 🟡 3 个 / 🔵 3 个
- 整体评价：路径切换与「models.yaml 不自动初始化 / 缺失报无可用模型」与方案一致，生产链路正确；问题集中在测试副作用、过时注释、改动留下的死变量，没有功能错误。

对照方案关键决策：
- D1：ensureUserConfig 不拷 models.yaml — **落实**
- D3：loadModelConfig 缺失报「无可用模型」，删除 env 回退 — **落实**
- Web GET 缺失返回空 providers、会话预检 400 — **落实**
- ensure 调用点：createAgent / sdkEntry None 分支 / webApp.__main__（不在 server 模块级）— **落实**
- 测试：209 passed（含新增 4 条）

---

### 问题清单

### [🟡 Medium] sdkEntry.py 改动后留下未使用的 projectDir

**位置**: `sdkEntry.py:75`
**问题**: 默认系统提示词改为 `userSystemPromptPath` 后，`projectDir` 不再被引用。本项目编码规范要求「删除因你的修改而变得未使用的变量」。
**修复方案**:
```python
# 删除
projectDir = Path(__file__).resolve().parent
```
`runSdk` 的 workDir 默认已用 `Path(__file__).resolve().parent`，互不影响。

### [🟡 Medium] createAgent 无条件 ensureUserConfig，测试会写真实 ~/.flamingo

**位置**: `flamingoAgents/builder.py` `createAgent` 开头
**问题**: 方案要求生产入口这么做，正确。副作用是 `tests/testAdapterFactory.py` 等调用 `createAgent` 的测试会在真实 HOME 下 mkdir/拷贝（已存在不覆盖，所以 209 全绿）。这不是功能 bug，但是测试隔离缺口：CI/他人机器跑测试会创建 `~/.flamingo/config/{tools.yaml,systemPrompt.md,skills}`。
**修复方案**: 不必改生产逻辑。若要隔离，让 testAdapterFactory 在调用前 monkeypatch `configPaths.user*`（注意还要同步 patch `builder.defaultSystemPromptPath` 等 import 时拷贝的常量，见下条）。当前幂等不覆盖，风险低，可维持现状。

### [🟡 Medium] 模块级路径常量是 import 时绑定，monkeypatch configPaths 不会传导到 builder/skillStore

**位置**: `builder.defaultSystemPromptPath`、`skillStore.defaultSkillsDir`、`toolConfig.defaultToolsConfigPath`、`modelConfig.defaultModelConfigPath`
**问题**: 这些是 `from configPaths import userXxx; defaultXxx = userXxx` 的快照。`monkeypatch.setattr(configPaths, 'userSkillsDir', ...)` 只改 configPaths 命名空间，createAgent 读的仍是旧 Path。testConfigPaths 测的是 ensureUserConfig（函数体内查模块全局，monkeypatch 有效），所以测试本身是对的；但以后若有人用同样手法测 createAgent 默认路径会误判。
**修复方案**: 维持现状即可（与改前 `Path(__file__)/config/...` 同一模式）。文档/后续测试约定：测默认路径请 patch 各模块自己的 `defaultXxx`，或让 createAgent 运行时读 `configPaths.userXxx` 而不是本地快照。后者更干净但超出本任务最小改动。

### [🔵 Low] server.py 预检注释仍写「库会静默回退环境变量」

**位置**: `webApp/backend/server.py` createSession 预检上方
**问题**: D3 已删除 `loadModelConfigFromEnv`。注释过时，后续维护者可能误以为去掉 exists 检查会静默走 env。实际 `loadModelConfigFromYaml` 仍会报「模型配置文件不存在」。
**修复方案**: 改成「yaml 缺失时 400 无可用模型，不创建会话」。

### [🔵 Low] 缺失 models.yaml 的两套文案

**位置**: `loadModelConfig` vs `loadModelConfigFromYaml`
**问题**: 库入口是「无可用模型：…请参照模板…」；FromYaml 仍是「模型配置文件不存在：{path}」。Web 会话预检自己写了 400，不依赖 FromYaml 这条。直接调 FromYaml 的调用方看不到新文案。
**修复方案**: 可把 FromYaml 的不存在分支改成同一句；非必须，Web 已拦截。

### [🔵 Low] builtinTools.py 注释仍写 config/systemPrompt.md

**位置**: `flamingoAgents/tools/builtinTools.py` 文件头与 askSubAgent 注释
**问题**: 运行时已改路径，注释未跟。不影响行为。
**修复方案**: 随下次改该文件时一并更新。

---

### 优点记录

- models.yaml 与 tools/systemPrompt 的职责切开，符合「密钥不自动生成」。
- ensure 不放 server 模块级，pytest 收集阶段不写 HOME。
- GET /models 空 providers 时前端 `renderForm` 在 `!provider` 直接 return，新机器打开设置页不会崩，可点「新增 provider」。
- `_copyIfMissing` 已存在跳过，用户改过的 tools.yaml 不会被模板覆盖。
- `loadModelConfigFromEnv` 已确认无外部引用后删除，没有留死代码。

---

### 修复优先级建议

1. **sdkEntry 删除 projectDir** — 一行，规范问题，建议改。
2. **server.py 过时注释** — 一行，防误导。
3. **测试写 HOME** — 已知可接受，不阻塞。

P7（删项目内 models.yaml / skills、去 gitignore）按你的要求未做，不列入缺陷。
