# 配置目录迁移至 ~/.flamingo/config 方案与实施计划

<!--
Author: wilbur
Version: 1.0
Date: 2026-09-08
Description: 运行时配置（models.yaml / tools.yaml / systemPrompt.md / skills/）从项目 config/ 迁到 ~/.flamingo/config/；项目 config/ 只保留模板与说明文件；新机器首次使用自动初始化用户配置目录。不做迁移代码（用户已手动拷贝）。
-->

## 1. 需求与目标

- 读取与保存配置的代码全部指向 `~/.flamingo/config/`；
- 项目 `config/` 只保留**模板文件**（models.example.yaml、tools.yaml、systemPrompt.md）与**说明文件**（config/README.md）；
- 在一台新机器上使用 flamingo 时，自动在 `~/.flamingo/` 下创建配置目录结构并读取，零手工迁移；
- 用户已手动把现有 config 拷贝到 `~/.flamingo/config/`，**不需要写任何数据迁移代码**。

## 2. 关键决策

### 2.1 已确认（用户需求直接推导）

| # | 决策 | 说明 |
|---|---|---|
| A1 | 运行时配置根目录为 `~/.flamingo/config/` | 与现有 `~/.flamingo/logs/`、`~/.flamingo/auth.json` 的家目录布局对齐 |
| A2 | 项目 `config/` 保留：models.example.yaml、tools.yaml、systemPrompt.md、README.md | tools.yaml 与 systemPrompt.md 即"程序默认模板"；models.yaml（含密钥）不属于模板 |
| A3 | 删除项目内用户私有文件：config/models.yaml、config/models.yaml.bak、config/skills/ | 用户已拷贝到 ~/.flamingo；"只保留模板和说明文件"的直接要求；同步清理 .gitignore 对应条目 |
| A4 | skills 目录只 mkdir 空目录，不拷贝项目技能 | config/skills/ 本就被 gitignore（私人内容），模板源里不存在 |

### 2.2 用户已拍板（2026-09-08 第二轮确认）

| # | 决策点 | 结论 | 说明 |
|---|---|---|---|
| D1 | 新机器上 models.yaml 如何处理？ | **不自动拷贝、不自动初始化**；用户参照 example 模板手动配置（只拷需要的 key 对应内容）；未配置就发请求 → 直接报错「无可用模型」 | 模板占位密钥拷过去毫无意义且误导；models.yaml 是唯一含密钥的用户私有文件，不属于程序默认值范畴。tools.yaml / systemPrompt.md / skills/ 目录仍自动初始化（程序默认值，缺了程序崩） |
| D2 | models.yaml.bak 的位置 | 跟随 models.yaml 落在 ~/.flamingo/config/ | modelConfigStore.backupPath = modelsYamlPath.with_suffix('.yaml.bak')，改路径后自动跟随，无需额外代码 |
| D3 | 未配置 models 时的报错行为 | 统一报「无可用模型」类错误，不静默回退环境变量、不自动拷模板 | 覆盖三个入口：库 loadModelConfig（文件不存在时）、Web 设置页 GET /models（返回空 providers 而非报错，前端展示引导文案）、Web 会话预检（400 报错） |

**D1/D3 与旧版 v1.0 方案的差异（重要）**：
- ensureUserConfig() 不再拷贝 models.yaml（连模板都不拷）；
- 原设计的「占位密钥检测」失去意义 → 删除该设计；
- 原 loadModelConfig 的「文件不存在 → 回退环境变量」逻辑与 D3 冲突 → 文件不存在时改为报错（环境变量回退代码删除，符合简洁原则：无调用方的死代码不留）。

**待用户确认的推斷**：tools.yaml / systemPrompt.md / skills/ 目录仍自动从项目模板拷贝/创建（它们是程序默认值，不含密钥，缺了程序直接崩）。若你认为这三个也不该自动初始化，方案需再调。

## 3. 现状核查（全部读/写点）

| 文件 | 现状 | 操作 |
|---|---|---|
| `flamingoAgents/models/modelConfig.py:54` | `defaultModelConfigPath = parents[2]/config/models.yaml`（读） | 改指向用户目录 |
| `flamingoAgents/tools/toolConfig.py:33` | `defaultToolsConfigPath = parents[2]/config/tools.yaml`（读） | 改指向用户目录 |
| `flamingoAgents/builder.py:25` | `defaultSystemPromptPath = parents[1]/config/systemPrompt.md`（读） | 改指向用户目录 |
| `flamingoAgents/skills/skillStore.py:31` | `defaultSkillsDir = parents[2]/config/skills`（读，目录缺失返回空列表） | 改指向用户目录 |
| `sdkEntry.py`（模块级 defaultSystemPromptPath + `--system` help 文案） | `projectDir/config/systemPrompt.md`（读） | 改用库统一常量 + 更新 help 文案 |
| `webApp/backend/modelConfigStore.py:27-28` | `modelsYamlPath` / `backupPath`（读写 + 备份） | 改指向用户目录 |
| `webApp/backend/skillStore.py` | 复用库 `defaultSkillsDir`（读写，Web 技能编辑保存） | 自动跟随，无需改动 |
| `webApp/backend/server.py:272,374` | 预检 `modelsYamlPath.exists()` + 400 文案 `config/models.yaml 不存在。` | 路径自动跟随；文案更新 |
| `webApp/backend/agentManager.py` | 经 `createAgent` 默认参数 | 自动跟随，无需改动 |
| `modelLogin.py` | 只依赖 credentialStore（~/.flamingo/auth.json）与 subscriptionAuth，**不读任何 config/ 文件**（已核实源码 import 区） | 无需改动、无需 ensure 调用点 |
| `webApp/backend/sessionRecovery.py` | 只做会话索引恢复，不读配置（已核实） | 无需改动 |
| 前端文案 3 处 | settingsView.js:889 保存提示、skillsView.js:34 空提示、index.html:115,153 说明 | 更新为 `~/.flamingo/config/...` |
| `flamingoAgents/utils/logPaths.py` | `flamingoHome = Path.home()/'.flamingo'`（日志） | 不动（职责不同，避免耦合） |
| `flamingoAgents/models/credentialStore.py` | 独立定义 `Path.home()/'.flamingo'` | 不动 |

关键时序问题（核查发现）：
- `sdkEntry.runSdk` 中 `systemPrompt=resolveSystemPrompt(systemPrompt)` 作为参数在 `createAgent(...)` **之前求值**，若 ensure 只放 createAgent 开头，新机器首跑会 FileNotFoundError → ensure 必须覆盖 `resolveSystemPrompt` 的 None 分支；
- Web 设置页 GET /models 不经 createAgent，ensure 必须在 Web 启动链执行。**不放在 server.py 模块级**（审核 B：tests/ 下 7 个测试文件 import server，模块级副作用会在 pytest 收集阶段真实写入 ~/.flamingo/config，污染测试环境且 monkeypatch 拦不住 import 时副作用），而是放 `webApp/__main__.py` 的 main() 中、`from webApp.backend.server import app` 之前——它是 Web 的唯一启动入口（README 快速开始即 `uv run python -m webApp`），行为等价且测试零污染。

## 4. 总体设计

### 4.1 新建 `flamingoAgents/utils/configPaths.py`（v1.0）

```python
flamingoHome = Path.home() / '.flamingo'
userConfigDir = flamingoHome / 'config'
userModelsPath = userConfigDir / 'models.yaml'
userToolsPath = userConfigDir / 'tools.yaml'
userSystemPromptPath = userConfigDir / 'systemPrompt.md'
userSkillsDir = userConfigDir / 'skills'

projectConfigDir = Path(__file__).resolve().parents[2] / 'config'   # 模板源（repo 内）

modelsExamplePath = projectConfigDir / 'models.example.yaml'        # 仅作手动配置参照，ensure 不拷贝

def ensureUserConfig() -> None:
    """幂等初始化：mkdir ~/.flamingo/config 与 skills/；
    tools.yaml / systemPrompt.md 缺失则从项目模板 copy2 拷贝；
    models.yaml 不拷贝、不创建（D1/D3：用户参照 models.example.yaml 手动配置，未配置时报无可用模型）；
    已存在一律不覆盖（用户侧可自由修改）。"""
```

- 拷贝用 `shutil.copy2`；已存在文件**永不覆盖**（保留用户修改，不做模板版本同步）；
- **显式约束：configPaths.py 保持零库内依赖**（只 import pathlib/shutil），防止未来反向引用 modelConfig/builder 成环；
- 模板源缺失（如 pip 安装到 site-packages 的场景）→ RuntimeError 报错说明模板位置（现有代码同样假定 repo 布局，不新增问题）；
- 并发首次初始化：copy2 内容幂等，Web 单 worker + CLI 偶发，不加锁。

### 4.2 ensureUserConfig() 调用点矩阵

| 入口 | 调用点 | 覆盖场景 |
|---|---|---|
| 库 | `builder.createAgent` 开头 | askModel / sdkEntry / askSubAgent / Web agentManager |
| SDK CLI | `sdkEntry.resolveSystemPrompt` 的 None 分支 | 修复参数先于 createAgent 求值的时序问题 |
| Web | `webApp/__main__.py` 的 main()，`import app` 之前 | Web 唯一启动入口；覆盖设置页 GET/PUT /models、技能页等不经 createAgent 的路径；不在 server.py 模块级调用（测试隔离，见 §3） |
| 登录 CLI | 不需要 | modelLogin.py 只碰 ~/.flamingo/auth.json，不读 config（已核实） |

### 4.3 未配置 models 的报错行为（D3）

`modelConfig.loadModelConfig`（入口函数）当前逻辑：文件存在 → YAML 加载；不存在 → 回退环境变量。按 D3 改为：

```python
def loadModelConfig(...):
    path = Path(configPath) if configPath is not None else defaultModelConfigPath
    if not path.exists():
        raise RuntimeError(
            f'无可用模型：{path} 不存在。请参照模板 {modelsExamplePath} 手动创建，'
            f'或在 Web 设置页配置后保存。'
        )
    return loadModelConfigFromYaml(...)
```

- **删除 loadModelConfigFromEnv 及其环境变量回退分支**（与 D3 冲突，且删除后无调用方，符合"不留死代码"原则）；同步检查并清理测试中若引用了 env 回退的用例（待实施时核实）；
- Web 会话预检（server.py 两处）改为同一报错语义的 400，文案含模板路径指引；
- Web 设置页 GET /models：文件不存在时**返回空 providers 结构**（不报错），前端设置页本来就是引导用户配置的地方；保存时写入完整 YAML（writeModelsConfig 已支持"yaml 缺失时以空文档为基底创建"，天然兼容）；
- 占位密钥检测设计**取消**（原依赖自动拷模板，现已不拷，检测对象不存在）。

### 4.4 修改后项目 config/ 结构

```
config/
  README.md            # 新增：说明本目录仅模板，运行时配置在 ~/.flamingo/config
  models.example.yaml  # 模板（原有）
  tools.yaml           # 模板（程序默认工具配置）
  systemPrompt.md      # 模板（程序默认系统提示词）
```

## 5. TODO Lists

### P1 新建 configPaths.py → 验证：空 HOME 下 ensureUserConfig() 后 ~/.flamingo/config 含 tools.yaml、systemPrompt.md、skills/（无 models.yaml）且重复执行不覆盖

- [ ] 新建 `flamingoAgents/utils/configPaths.py`（v1.0）：路径常量 + ensureUserConfig()（§4.1，不含 models.yaml）
- [ ] `flamingoAgents/utils/__init__.py`：无需导出（按需 import，最小暴露）

### P2 库侧 5 个默认路径切换 → 验证：uv run pytest tests/ 全绿 + CLI 冒烟

- [ ] `flamingoAgents/models/modelConfig.py`（1.5→1.6）：defaultModelConfigPath = userModelsPath；loadModelConfig 文件不存在时报"无可用模型"（§4.3）；**删除 loadModelConfigFromEnv**；核实并清理测试中对 env 回退的引用
- [ ] `flamingoAgents/tools/toolConfig.py`（1.2→1.3）：defaultToolsConfigPath = userToolsPath
- [ ] `flamingoAgents/builder.py`（1.7→1.8）：defaultSystemPromptPath = userSystemPromptPath；createAgent 开头调用 ensureUserConfig()
- [ ] `flamingoAgents/skills/skillStore.py`（1.4→1.5）：defaultSkillsDir = userSkillsDir
- [ ] `sdkEntry.py`（1.5→1.6）：defaultSystemPromptPath 改用库常量（删本地重复定义）；resolveSystemPrompt None 分支先 ensureUserConfig() 再读；`--system` help 文案更新

### P3 Web 侧切换 → 验证：uv run pytest tests/ 全绿（重点 testModelConfigStore / testModelAuthWeb / testImageWeb）+ pytest 收集阶段不触碰真实 ~/.flamingo

- [ ] `webApp/backend/modelConfigStore.py`（1.3→1.4）：modelsYamlPath = userModelsPath（backupPath 自动跟随）；readRawYaml 文件不存在时返回空文档基底（GET 空 providers，§4.3）而非报错
- [ ] `webApp/__main__.py`（1.2→1.3）：main() 中 `from webApp.backend.server import app` **之前**调用 ensureUserConfig()
- [ ] `webApp/backend/server.py`（1.17→1.18）：**不做模块级 ensure**（测试隔离，见 §3）；两处会话预检 400 文案更新（无可用模型 + 模板路径指引，§4.3）
- [ ] `webApp/backend/skillStore.py`、`agentManager.py`：确认零改动（自动跟随）

### P4 前端文案 → 验证：grep 无残留 `config/models.yaml`、`config/skills` 旧文案

- [ ] `webApp/frontend/js/settingsView.js`（1.11→1.12）：保存提示改为 ~/.flamingo/config/models.yaml
- [ ] `webApp/frontend/js/skillsView.js`（1.2→1.3）：空提示改为 ~/.flamingo/config/skills/
- [ ] `webApp/frontend/index.html`：两处 settings-notice 的 code 标签更新

### P5 文档 → 验证：人工通读

- [ ] 新增 `config/README.md`（v1.0）：目录用途（仅模板）、运行时位置、新机器自动初始化行为、如何重置（删 ~/.flamingo/config 重跑）
- [ ] `README.md`（1.2→1.3）：所有 `config/...` 表述改为 `~/.flamingo/config/...`；快速开始改为"运行自动初始化 tools/systemPrompt/skills；models.yaml 参照 `config/models.example.yaml` 手动配置到 `~/.flamingo/config/`（或用 Web 设置页）"
- [ ] `docs/addCallableToolFunction.md`：config/tools.yaml 表述更新（说明文件，保持准确）

### P6 测试 → 验证：uv run pytest 全绿

- [ ] 新增 `tests/testConfigPaths.py`（v1.0）：
  - 空临时 HOME 下 ensureUserConfig() 建齐目录 + 拷贝 tools.yaml / systemPrompt.md + 建 skills/ 空目录，**不产生 models.yaml**（monkeypatch Path.home 或注入 baseDir）
  - 已存在文件不覆盖（预写不同内容，ensure 后内容不变）
  - 模板源缺失时 RuntimeError 消息含模板路径
  - models.yaml 缺失时 loadModelConfig 报"无可用模型"错误（含模板路径指引）
- [ ] 新增/修改模型配置相关用例：env 回退删除后，原依赖 FLAMINGO_AGENTS_MODEL 环境变量的用例（若有）改为报错断言（实施时 grep 核实）
- [ ] 回归：现有测试全量通过（testModelConfigStore 的 monkeypatch.setattr 方式不受影响；testAdapterFactory 显式传 configPath 不受影响）

### P7 项目清理（按 A3，用户已拷贝） → 验证：ls config/ 仅剩 4 个模板/说明文件

- [ ] 删除 `config/models.yaml`、`config/models.yaml.bak`、`config/skills/`
- [ ] `.gitignore`：删除 `config/models.yaml`、`config/models.yaml.bak`、`config/skills/` 三条

## 6. 测试计划

- 单测：testConfigPaths.py 覆盖 ensure 幂等性、不覆盖语义、模板缺失报错、models.yaml 缺失时报"无可用模型"；
- 回归：`uv run pytest`（现有 23 个测试文件全量）；
- 冒烟（人工）：
  1. 临时改 HOME 跑 `uv run python askModel.py --debug` 验证新机器初始化链路（应报无可用模型，tools/systemPrompt 已就位）；
  2. 正常 HOME 跑 sdkEntry 子代理调用（builtinTools.askSubAgent 链路）；
  3. Web 启动 → 设置页读写 models.yaml → 技能页列表 → 新建会话发消息。

## 7. 范围外 / 已知限制

- 不写数据迁移代码（用户已手动拷贝 ~/.flamingo/config）；
- 不加 FLAMINGO_HOME 环境变量覆盖（未要求，保持简单）；
- models.yaml 永不自动创建/拷贝（D1）：用户参照 example 手动配置或 Web 设置页配置；环境变量回退（loadModelConfigFromEnv）随 D3 删除；
- 不做模板版本升级同步：用户侧已存在的 tools.yaml/systemPrompt.md 不随项目模板更新，重置手段 = 删文件后重跑（写入 config/README.md 说明）；
- pip 安装到 site-packages 的场景模板源不可达（parents[2] 不再是 repo 根）：现状即如此，本次仅把报错变清晰。

## 8. 审核记录

### 首轮（2026-09-08，subagent volcano/glm-5.3，多轮分任务）

- **A（矩阵遗漏 modelLogin.py）**：子代理指出调用点矩阵未提及 modelLogin.py → 主线程核实源码：该入口只依赖 credentialStore（~/.flamingo/auth.json），不读任何 config/ 文件 → 无需调用点，已在 §3 现状核查表与 §4.2 矩阵中明确说明；
- **B（server.py 模块级 ensure 污染测试环境）**：子代理指出 tests/ 下多个文件 import server，模块级副作用会在 pytest 收集阶段真实写入 ~/.flamingo/config → 已采纳：调用点挪到 webApp/__main__.py 的 main()（Web 唯一启动入口，行为等价），P3 验证补"pytest 收集阶段不触碰真实 HOME"；
- **C（占位密钥检测三连问）**：子代理确认 (a) 只比对展开后值正确、${ENV} 不误伤；(b) 真实密钥撞占位串属极小概率误杀，建议文案加逃生口 → 已在 §4.3 报错文案中补逃生口提示；(c) 检测放 api-key 分支正确 → 维持原设计。【注：此条为首轮历史结论，占位密钥检测已随 D1 取消，见第二轮修订】
- **D（configPaths 循环导入）**：子代理确认零循环导入风险，但建议把"configPaths 保持零库内依赖"写成显式约束 → 已写入 §4.1。

### 第二轮（2026-09-08，用户拍板 D1/D3，方案重大修订）

用户明确：新环境配置 model 只需参照 example 模板拷贝需要的 key，用户侧 models.yaml 不预置模板内容；未配置就请求 → 报"无可用模型"。据此修订：
- ensureUserConfig() 不再拷贝 models.yaml（原 v1.0 设计自动拷贝+占位密钥检测，全部取消）；
- loadModelConfig 文件不存在时报错而非回退环境变量，loadModelConfigFromEnv 删除；
- Web GET /models 文件不存在时返回空 providers（设置页作为配置引导入口）；
- 保留推断（待确认）：tools.yaml / systemPrompt.md / skills/ 仍自动初始化。

### 复审（2026-09-08，subagent volcano/glm-5.3）

- 修复项 A–D 落回文档后提交复审，逐项确认：
  1. §4.2 矩阵（ensure 放 __main__.py 的 main()）与 P3（server.py 不做模块级 ensure）自洽无矛盾，互为同一决策的两面；
  2. 测试不经 __main__.py、不执行 ensure 不构成风险：现有测试全量 monkeypatch/显式传 configPath，从不依赖默认路径背后"ensure 已跑过"的前提，隔离效果正是审核 B 的目标；
  3. 全文无前后矛盾。
- **结论：复审通过，方案可进入实施。**（针对 v1.0 首轮；D1/D3 修订后需重新审核，见下）

### 待办：D1/D3 修订后的再审核（已完成，2026-09-08）

- 第二轮修订提交 subagent 再审核：设计性章节（§2.2、§4.1、§4.3、§7、P1/P6/P7）全部与 D1 一致无矛盾；发现两处非设计性残留均已修复：① §8 首轮记录 C 补注"已被 D1 取消"防误读；② P5 README 快速开始文案改为明确区分"自动初始化 tools/systemPrompt/skills"与"models.yaml 手动配置"。
- **结论：再审核通过，方案可进入实施。**
