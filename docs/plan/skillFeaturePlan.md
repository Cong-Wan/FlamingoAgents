# FlamingoAgents 引入 Skill 能力 —— 方案与计划

> Author: wilbur
> Version: 1.2
> Date: 2026-08-13
> Description: 为 flamingo 增加 Agent Skills 能力（对齐 pi / Agent Skills 规范）：agent 只读加载 `config/skills/` 下的 skill（每 skill 一个文件夹 + 一个 `skill.md`），新建 agent 时把 name/description/location 注入 system prompt 且位于「当前时间」注入之前；resume 绝不重注（保 prompt cache）；前端设置页只读展示 skill；输入框用 `/skill:名` 选中后把该 skill 全文填入输入框（补参数后再发，不自动发送）。
> v1.1 评审修订（H1 H2 / M1–M6 / L1–L9）：钉死 `GET /api/skills/{name}` 按加载结果映射查找 + 路径拘禁、统一 name 白名单；slash 不改 `runItem` 清空语义、异步填框契约；设置页技能区挂 `#skillsSection` 且只在 `open()` 渲染；库导出并复用 `defaultSkillsDir`；`skillsDir=''` 分支写死；契约改 §3.19/§3.20；slash 走同一套前缀过滤；补验收与遗漏文件（index.html/styles.css）。
> v1.2 复审补强（L1'–L4'）：slash 缓存时机写死「页面加载/登录后拉取一次并常驻，路由切换不重拉」；设置页 `getSkills()` 失败不拖累主流程；slash 异步回填加「仍处于该会话」守卫；声明顶层 `flamingoAgents/__init__.py` 不动、web 从子包导入。两轮评审均通过，可实施。

---

## 0. 需求确认（已定）

| 项 | 决定 | 备注 |
|---|---|---|
| skill 存放 | `config/skills/<name>/skill.md`，每 skill 一个文件夹 | 库默认路径即此处 |
| agent 侧 | **只读**，不写 | 库返回路径，模型自行 `read` |
| system prompt 注入 | 新建 agent 时注入 `<skill><name/><description/><location/></skill>` 块，**在「当前时间」注入之前** | 见 D3 |
| resume | **不**重注 skill；即使 skill 更新也不强行注入 | 现状即满足（D3） |
| 前端配置 | **A：只读展示** skill 列表（名称/描述/路径/是否进 prompt） | 不做在线增删改 |
| 输入框 slash | `/skill:<名>` 选中后**把指令填进输入框**让你补参数再发，**不自动发送** | 见 D5 |

---

## 1. 现状结论（已读码核对）

### 1.1 system prompt 组装（`flamingoAgents/builder.py::createAgent`）

- 文本来源：`systemPrompt`（直传）> `systemPromptPath` > 默认 `config/systemPrompt.md`。
- 末尾：`if appendCurrentTime:` 追加 `\n\n## 当前时间\n\n当前日期为：{iso}。\n`。
- **注入点**：skill 块加在「rstrip 后、时间块前」，即 `text + skillBlock + timeBlock`（D3）。

### 1.2 resume 语义（`flamingoAgents/core/conversation.py`）

- `agent.getConversation`：`resume=logPath.exists()`。
- resume 时 `_resumeFromLog()` **只取 jsonl 里第一条 `systemMessage`**（含创建时注入的时间），**完全不读**传入的新 `systemPrompt`。
- 所以 skill 只在 `appendSystemMessage(systemPrompt)`（新建分支）出现；resume 天然不注入，prompt cache 前缀不被破坏。**无需改 conversation / agent / agentManager**。

### 1.3 装配点

- web 侧唯一调用：`webApp/backend/agentManager.py::getAgent` 懒建 `createAgent(workDir/logDir/providerId/modelId)`，**未传 systemPrompt/skillsDir** → 吃默认注入。
- 库内注入后，**CLI（`askModel.py`）与 SDK（`sdkEntry.py`）也会带 skill**——可接受，无需改它们，仅记录。
- 模型配置变更 `invalidateAllAgents` 惰性重建：新会话带新 skill；旧会话 resume 仍用日志旧 system。

### 1.4 前端骨架

- `slashCommand.js`：`commandRegistry`（`/model` `/new`）；`onInput` 用 `command.name.slice(1).indexOf(keyword)===0` 前缀过滤；`runItem` **先 `closePanel()` + `composerInput.value=''` + dispatch input，再 `item.run()`（不 await）**。
- `settingsView.js`：`render()`=`renderTabs`+`renderForm`；`renderForm` 开头 `formEl.innerHTML=''`；`open()` 清 `tabsEl`/`formEl`。HTML 结构：`#settingsError` → `#providerTabs` → `#providerForm`。
- 路由：`authedApi = APIRouter(prefix='/api')`；`..` 路径穿越中间件只挡一层，不作主防线。
- `pyproject.toml` 已含 `pyyaml`（复用解析 frontmatter）。

---

## 2. 总体设计

```
config/skills/<name>/skill.md
        │ 只读扫描（库）
        ▼
flamingoAgents/skills/skillStore.py   加载 + 解析 + XML 片段；导出 defaultSkillsDir
        │
flamingoAgents/builder.py::createAgent  注入 system prompt（时间之前；resume 由 conversation 现状保证）
        │
webApp/backend/skillStore.py            复用库 loadSkills + defaultSkillsDir
        │
webApp/backend/server.py                GET /api/skills（列表）/ GET /api/skills/{name}（正文）
        │
webApp/frontend/js/settingsView.js      「技能」只读区块（#skillsSection，仅 open() 渲染）
webApp/frontend/js/slashCommand.js      /skill:名 → 面板 → 异步拉正文填进输入框（不发送）
```

---

## 3. 库侧：`flamingoAgents/skills/`

### 3.1 新文件 `flamingoAgents/skills/__init__.py`

文件头（小驼峰目录 `skills`，对齐 `tools`/`models`）。导出 `Skill` / `loadSkills` / `formatSkillsXml` / `defaultSkillsDir`。**顶层 `flamingoAgents/__init__.py` 不动**（仍只导出 `createAgent`），web 侧从子包导入（`from flamingoAgents.skills import loadSkills, defaultSkillsDir`）。

### 3.2 新文件 `flamingoAgents/skills/skillStore.py`

纯只读，无网络无写盘。

```python
@dataclass
class Skill:
    name: str          # frontmatter name，缺省取文件夹名；必须 ^[A-Za-z0-9_-]+$
    description: str   # 必填；空则跳过该 skill
    filePath: str      # skill.md 绝对路径
    baseDir: str       # 所在文件夹（相对路径解析基准）
    disabled: bool     # frontmatter disable 为 YAML bool true → 不进 prompt，但 /skill: 可用
```

**`defaultSkillsDir`**：`Path(__file__).resolve().parents[2] / 'config' / 'skills'`（`skills/skillStore.py` 上两级到包根，再同级 `config/skills`；与 `builder.py` 的 `defaultSystemPromptPath` 指向同一 config 目录）。

**`loadSkills(skillsDir) -> list[Skill]`**

- 目录不存在 → `[]`。
- 只扫 `skillsDir` **一层子目录**：子文件夹含 `skill.md`（**小写**，单一约定，不接受 `SKILL.md`）即为一个 skill。
- **排序：按子文件夹名字典序**（保证 prompt 稳定可缓存；注意是文件夹名不是 `Skill.name`）。
- frontmatter 用 `yaml.safe_load` 解析文件头 `---\n...\n---` 块（内联进本文件，**不单独建 frontmatter.py**）；块非 dict 或解析失败按「无 frontmatter」处理。
- 字段：
  - `name`：缺省 = 文件夹名；**必须匹配 `^[A-Za-z0-9_-]+$`，否则跳过该 skill 并 debug**（H1：与 API charset 统一，杜绝「改名后 404 / 中文名 slash 拉不到正文」）。
  - `description`：去空白后为空 → **整条跳过**（不进列表、不进 slash）。**先判 description 再判 disable**（L2）。
  - `disable`：`parsed.get('disable') is True`（仅 YAML bool true；`'yes'`/`'true'`/`1` 不算）（L1）。
  - 其余字段忽略。
- 同名碰撞：按文件夹字典序**先出现的赢**，后者跳过 + debug。
- 不做：glob、`.gitignore`、嵌套递归、`.agents`、64/1024 长度强校验（简洁优先，作者自律）。

**`formatSkillsXml(skills) -> str`**

- 过滤 `disabled`，为空返回 `''`（不输出裸 `<available_skills>`）。
- 输出：

```xml

## 可用技能

以下技能提供特定任务的专门指令。当任务与某个技能的描述匹配时，用 read 工具读取其 location 指向的文件，按其中步骤执行；技能内相对路径相对该文件所在目录解析。

<available_skills>
  <skill>
    <name>{name}</name>
    <description>{description}</description>
    <location>{filePath}</location>
  </skill>
</available_skills>
```

- `name`/`description`/`location` 用 `html.escape(s, quote=True).replace("'", "&apos;")` 转义五者（L4）。
- **缓存友好**：`location` 为绝对路径（新建即定，resume 用旧值，不跨会话漂移）。

---

## 4. 库侧：`builder.py` 注入（D3）

`createAgent` 在算出 `systemPromptText` 之后、`appendCurrentTime` 之前插入。

新增关键字参数 `skillsDir: str | Path | None = None`：

```python
if skillsDir == '':
    skills = []                      # 显式禁用（测试/纯对话逃生口）
elif skillsDir is None:
    skills = loadSkills(defaultSkillsDir)
else:
    skills = loadSkills(Path(skillsDir))
skillsBlock = formatSkillsXml(skills)
if skillsBlock:
    systemPromptText = systemPromptText.rstrip() + '\n' + skillsBlock
if appendCurrentTime:
    ... 现有时间注入 ...
```

- **`skillsDir == ''` 必须单独判**，不能用 `if skillsDir`（`None` 与 `''` 真值同为 False，会误判）（M3）。
- **顺序保证**：skill 块在时间块之前 → 满足「放在时间注入之前」。
- resume 红线：builder 只在新建路径拼文本；`conversation` resume 走 `_resumeFromLog` 用日志旧 system，不经过 builder 新 prompt。**改 builder 不影响 resume**。
- `builder.py` 文件头版本 +0.1，description 记此改动。

---

## 5. Web 后端

### 5.1 新文件 `webApp/backend/skillStore.py`

薄封装，**import 库的 `loadSkills` 与 `defaultSkillsDir`，禁止自己推 `parents`**（M2：否则 `webApp/backend/skillStore.py` 会错推成 `webApp/config/skills`）。

```python
def listSkills() -> dict:
    skills = loadSkills(defaultSkillsDir)
    return {'skills': [
        {'name': s.name, 'description': s.description, 'filePath': s.filePath,
         'baseDir': s.baseDir, 'disabled': s.disabled}
        for s in skills
    ]}

def getSkillBody(name: str) -> dict:
    # H1：只做「loadSkills 结果按 name 精确匹配」，再读 filePath；绝不拼 skillsDir/name/...
    for s in loadSkills(defaultSkillsDir):
        if s.name == name:
            resolved = Path(s.filePath).resolve()
            if not resolved.is_relative_to(Path(defaultSkillsDir).resolve()):
                raise RuntimeError(f'skill 路径越界：{name}')
            body = _stripFrontmatter(resolved.read_text(encoding='utf-8'))
            return {'name': s.name, 'baseDir': s.baseDir, 'body': body}
    raise LookupError(f'技能不存在：{name}')
```

- 每次请求现读现扫（量小，无需缓存）。
- `disabled` 也返回正文（slash 要用）。
- `_stripFrontmatter`：去掉 `---...---` 头，返回正文 markdown。

### 5.2 `server.py` 新增路由

```python
skillNamePattern = re.compile(r'[A-Za-z0-9_-]+')

@authedApi.get('/skills')
def getSkills():
    return skillStore.listSkills()

@authedApi.get('/skills/{name}')
def getSkillBody(name: str):
    if not skillNamePattern.fullmatch(name):
        raise HTTPException(status_code=400, detail='skill 名非法。')
    try:
        return skillStore.getSkillBody(name)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))
    # RuntimeError（越界）由统一 runtimeErrorHandler 转 400
```

- 挂 `authedApi` 自带鉴权；name charset 与库加载白名单一致（H1）。
- **不新增 PUT/POST/DELETE**：本期只读（需求 A）。
- `server.py` 文件头版本 +0.1。

### 5.3 契约文档

`docs/webApiSpec.md` 新增（**现有 §3.13–§3.18 已占用**，往后顺移）（M4）：

- **§3.19 `GET /api/skills`**：响应 `{skills:[{name,description,filePath,baseDir,disabled}]}`；只读、鉴权、`filePath` 为服务器绝对路径、`disabled` 语义（不进 prompt 但 `/skill:` 可用）。
- **§3.20 `GET /api/skills/{name}`**：响应 `{name,baseDir,body}`，`body` 为剥 frontmatter 的正文；400 名非法、404 不存在、400 路径越界。

---

## 6. 前端：设置页只读区块（需求 2）

**挂载点（M1）**：技能区**不能**塞进 `formEl` 或每次 `render()`（切 tab/改名会重建表单把它冲掉）。

- `webApp/frontend/index.html`：在 `#settingsError` 与 `#providerTabs` 之间加 `<div id="skillsSection">`（文件头 +0.1）。
- `settingsView.js`：
  - `open()` 时并行 `api.getSkills()`，**只在 `open()` 渲染一次**技能区，**不进** `render()`。
  - **`getSkills()` 失败不拖累主流程**：单独 catch，技能区显示「加载失败」或置空态；`getModels()` 主流程不受影响（不与 `getSkills()` 绑同一个 `Promise.all` 失败分支）。
  - 卡片用 `textContent` 渲染（不 `innerHTML`）：名称、描述、路径、徽标（`已启用` / `不进 prompt`）。
  - 空态：一行「`config/skills/` 下暂无技能」。
  - 加一句说明：「是否进 prompt 指**下次新建会话**会注入；已打开的旧会话 resume 不受影响」（L5）。
  - 文件头版本 +0.1。
- `webApp/frontend/styles.css`：只读技能卡片样式（文件头 +0.1）。
- `api.js`：`getSkills: function () { return request('/api/skills'); }`。

---

## 7. 前端：输入框 `/skill:` 强制使用（需求 3，D5）

`slashCommand.js`：

1. **页面加载/登录成功后拉取一次 `api.getSkills()` 并常驻内存**，路由切换（`location.hash` 换会话）不重拉；失败静默按空数组（L6：无文件监听，新增 skill 需刷新页面才出现，本期可接受）。
2. **同一套前缀过滤（M5）**：skill 项 `label='/skill:'+name`、`desc=description`，与 `/model` `/new` 一起进 `onInput` 的前缀匹配。打 `/` 列出全部；打 `/s`、`/skill:my` 自然收窄。**不单独设 `/s` 分支**。
3. **选中行为（D5：填进输入框，不发送）**：
   - skill 项的 `run` 是**异步**：选中时先快照 `sid = window.appStore.currentSessionId`；`api.getSkillBody(encodeURIComponent(name))` 成功后**先比对 `window.appStore.currentSessionId === sid`，不符则丢弃并 toast**（防慢网络下切会话后正文写进新会话输入框，L3'）；符合则 `composerInput.value = body + '\n\n'`、`selectionStart = selectionEnd = value.length`（L7）、`focus()`、dispatch `input`（触发 `chatView` autoResize）；失败 `toast`。
   - **H2 硬契约：禁止改 `runItem` 的全局清空语义**（`/model` `/new` 依赖「先清空再 run」）。`runItem` 不 await `run`，skill 的异步 `run` 在清空之后自行回填正文——时序上「先清空 → 再异步填正文」，最终输入框为正文，正确。
   - 填入正文末尾的 `\n\n` 让 `onInput` 的 `/^\/ 且无空白/` 判断不命中，**不会误再弹 slash 面板**。
   - skill 项**不**写进静态 `commandRegistry` 的同步 `run`，走动态 items。
4. 发送后 skill 正文作为普通 user 消息进对话（透明可见可编辑，符合「补参数再发」）。
5. `api.js`：`getSkillBody: function (name) { return request('/api/skills/' + name); }`（调用处 `encodeURIComponent`）。
6. `slashCommand.js` / `api.js` 文件头版本各 +0.1。

---

## 8. 契约/文档增量

| 文档 | 增量 |
|---|---|
| `docs/webApiSpec.md` | §3.19 `GET /api/skills`、§3.20 `GET /api/skills/{name}`；slash 行为说明 |
| `README.md` | 现状能力加一条 skill；目录结构 `config/` 加 `skills/` |
| `docs/addCallableToolFunction.md` | 不动 |

---

## 9. TODO lists

### 库（flamingoAgents）
- [ ] L1 新建 `flamingoAgents/skills/__init__.py`（文件头 + 导出 `Skill`/`loadSkills`/`formatSkillsXml`/`defaultSkillsDir`）
- [ ] L2 新建 `flamingoAgents/skills/skillStore.py`：`Skill`、`defaultSkillsDir`、`loadSkills`（一层扫描/小写 skill.md/文件夹名排序/name 白名单 `^[A-Za-z0-9_-]+$`/缺 description 跳过/disable 仅 bool true/同名先赢）、`formatSkillsXml`（五字符转义、空返回 `''`）、内联 frontmatter 解析（`yaml.safe_load`）
- [ ] L3 `builder.py`：新增 `skillsDir=None`；`==''` 禁用 / `None` 默认 / 路径覆盖 三分支；时间注入前拼 `formatSkillsXml`；文件头 +0.1
- [ ] L4 `uv run python -c` 手验：造 `config/skills/demo/skill.md` → 新建 agent 的 system prompt 含 `<available_skills>` 且在 `## 当前时间` 之前；`skillsDir=''` 时不含；name 含中文/点号被跳过

### Web 后端
- [ ] B1 新建 `webApp/backend/skillStore.py`：import 库 `loadSkills`+`defaultSkillsDir`；`listSkills()`、`getSkillBody(name)`（映射查找 + `is_relative_to` 拘禁 + `_stripFrontmatter`）
- [ ] B2 `server.py`：`GET /api/skills`、`GET /api/skills/{name}`（name 白名单 400、未知 404、越界 400）；文件头 +0.1
- [ ] B3 `webApiSpec.md` §3.19/§3.20

### 前端
- [ ] F1 `api.js`：`getSkills`、`getSkillBody(name)`（`encodeURIComponent`）
- [ ] F2 `index.html` 加 `#skillsSection`；`settingsView.js` 在 `open()` 渲染只读技能区（textContent、空态、徽标、说明文案）；`styles.css` 加卡片样式；三者文件头各 +0.1
- [ ] F3 `slashCommand.js`：缓存 skills、skill 项并入同一套前缀过滤、异步 `run` 填全文进输入框（selectionStart/End 置尾、focus、dispatch input、不发送、失败 toast）；不改 `runItem`；文件头 +0.1

### 验收（非测试框架，走真实 UI + 脚本）
- [ ] V1 `config/skills/mygit/skill.md` 就位 → 新建会话 system prompt 含该 skill 且在 `## 当前时间` 之前
- [ ] V2 resume 旧会话 → systemMessage 为创建时旧值，不因 skill 变动而改
- [ ] V3 设置页「技能」区列出 mygit，路径正确；切 provider tab 不闪丢
- [ ] V4 输入 `/` → 面板含 `/skill:mygit`；选中 → 输入框被填入 skill 全文、光标在末尾、可继续打字 → 回车发出
- [ ] V5 description 含 `<` `&` → prompt XML 不破坏
- [ ] V6 `config/skills/` 为空 → prompt 无 `<available_skills>`、设置页空态、`/` 不补 skill 项
- [ ] V7 `disable: true` 的 skill → 设置页列表有（标「不进 prompt」）、prompt 无、`/skill:` 仍能拉正文
- [ ] V8 `GET /api/skills/{name}` 未知名 404；name 带 `.`/中文 400
- [ ] V9 `skillsDir=''`（脚本验证）：prompt 不含 `<available_skills>`

---

## 10. 风险与取舍

| 风险 | 取舍 |
|---|---|
| frontmatter 解析 | 用 pyyaml（已有依赖）内联解析，不手写、不单独建文件 |
| `location` 绝对路径进 prompt 影响缓存 | 有意保留；新建即定，resume 用旧值，不漂 |
| name 白名单 `^[A-Za-z0-9_-]+$` | 库加载即过滤，与 API charset 统一，杜绝「改名 404 / 中文名 slash 拉不到」 |
| skill 正文经输入框发送 = 一条长 user 消息 | 透明可编辑，符合「补参数再发」；计入会话与 token，正常 |
| 只读展示无法满足「在线改 skill」 | 本期刻意不做；磁盘改文件 + 新建会话生效 |
| 同名 skill | 文件夹字典序先赢 + debug；无多来源，不做复杂优先级 |
| CLI/SDK 被动带 skill | 可接受，不改 `askModel.py`/`sdkEntry.py` |
| slash 缓存无失效 | 刷新页面才见新 skill；本期不做文件监听 |

---

## 11. 不在本期范围

- skill 的在线新建/编辑/删除/启停（写 `config/skills/`）
- `allowed-tools`、多来源（`~/.agents`、packages）、glob、嵌套递归
- description 长度强校验、`SKILL.md` 大写兼容
- `/skill:` 直接发送（本期填输入框）
