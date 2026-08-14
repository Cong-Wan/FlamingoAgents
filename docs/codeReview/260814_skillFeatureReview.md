# 代码审核报告 — Skill 能力

> Author: wilbur
> Version: 1.0
> Date: 2026-08-14
> Review target: FlamingoAgents Skill 能力改动（对照 `docs/plan/skillFeaturePlan.md` §0/§3/§4/§5/§7 硬约束）
> Review scope: 库加载/XML 注入、Web 只读 API、slash 异步填框、平级技能页；聚焦逻辑正确性，不通读无关文件
> Status: 有条件可合并（无 Critical；唯一 High 是跨平台文件名，文档 + 示例改名即可，不必改加载语义）

## 总览

- 审核文件：核心必读 8 + 为判断查阅的 builder/main/store/chatView/api/index/styles 等，合计约 16 个；tracked 10 个文件、untracked 新实现 4 个（`flamingoAgents/skills/*`、`webApp/backend/skillStore.py`、`webApp/frontend/js/skillsView.js`）
- 发现问题：🔴 0 个 / 🟠 1 个 / 🟡 3 个 / 🔵 5 个
- 整体评价：主链路按硬约束落地，注入点、resume 红线、路径拘禁、name 白名单、slash 异步回填、平级技能页、debug 门控均正确。唯一值得挡一下的是「小写 `skill.md` × 大小写不敏感 FS」的静默可移植性；其余是边界健壮性，不阻断合并。

### 硬约束核对

| # | 约束 | 结论 |
|---|---|---|
| 1 | skill 块在「## 当前时间」之前 | ✅ `builder.py` 先 `rstrip+skillsBlock`，再进 `appendCurrentTime` |
| 2 | resume 不重注，不改 `conversation.py` | ✅ `git diff` 未碰该文件；注入只发生在 `createAgent` 拼文本 |
| 3 | 只认 `config/skills/<name>/skill.md` | ✅ 一层子目录 + 字面量 `'skill.md'`；不递归、不认其它文件名（代码层） |
| 4 | `GET /api/skills/{name}` 按 loadSkills name 精确匹配 + `is_relative_to`，禁路径拼接 | ✅ `getSkillBody` 遍历匹配后再 `resolve`/`is_relative_to` |
| 5 | name 白名单 `^[A-Za-z0-9_-]+$`，库与 API 一致 | ✅ 库 `fullmatch(r'^[A-Za-z0-9_-]+$')`；API `fullmatch(r'[A-Za-z0-9_-]+')`（`fullmatch` 等价） |
| 6 | slash 异步填框、不改 `runItem` 清空语义、有会话守卫 | ✅ `runItem` 仍先清空再 `item.run()`；`fillSkill` 自行回填 |
| 7 | 技能页与模型配置平级（`#skillsPage` + `#/settings/skills`） | ✅ 独立 section + 侧栏入口 + `main.js` 路由；`settingsView` 已抽出。注：方案 §6 仍写 `#skillsSection`，实现跟的是本期硬约束而非旧挂载点 |
| 8 | 库层 debug 走 `debugConsole` 门控，不无条件 print | ✅ `_debug` 先看 `isDebug`，全文件无 `print` |

---

## 问题清单

### [🟠 High] 小写 `skill.md` 在大小写不敏感 FS 上「假命中」，Linux 会静默 0 skill

**位置**: `flamingoAgents/skills/skillStore.py` `loadSkills`（`folder / 'skill.md'`）；仓库现状 `config/skills/*/SKILL.md`

**问题**:
实现按方案只拼小写 `skill.md`。macOS APFS 默认大小写不敏感：`Path('SKILL.md')` 与 `Path('skill.md')` 是同一 inode，`is_file()` 为 True，skill 能加载。Linux ext4 大小写敏感：目录里只有 `SKILL.md` 时 `skill.md` 不存在，整夹被 `continue`，无日志（web 侧调用还不传 `debugConsole`）。

这不是实现偏离方案——方案 §3.2 / §11 已钉死「不接受 `SKILL.md`」。但它是**跨平台静默失败**：本机（Mac）验收全绿，部署到 Linux 后设置页空态、prompt 无 `<available_skills>`、`/skill:` 无项。当前 untracked 示例目录 `os.listdir` 就是 `['SKILL.md', 'SKILL_cn.md']`，开发者在 Mac 上会误以为「大写也能用」。

官方 Agent Skills 规范文件名是 `SKILL.md`，作者从生态拷过来就会踩。

**是否需在代码/文档处理**:
- **不要**在代码里兼容 `SKILL.md`（方案明确不在本期，加了反而破坏「单一约定」）。
- **必须**改仓库示例文件名 + 文档写死约定。
- **建议**加一条 debug：目录在、小写 `skill.md` 不在、但存在 `SKILL.md` 时点名提醒（Linux 上才看得到；Mac 上这条走不到，因为 `skill.md` 已经 `is_file()`）。

**修复方案**:

```python
# before
skillFile = folder / 'skill.md'
if not skillFile.is_file():
    continue

# after（语义仍只认小写；仅补可观测性）
skillFile = folder / 'skill.md'
if not skillFile.is_file():
    if (folder / 'SKILL.md').is_file():
        _debug(f'跳过 skill：只找到 SKILL.md，需要小写 skill.md（{folder}）')
    continue
```

文档（README / `docs/webApiSpec.md` §3.19）补一句：

```text
文件名必须是小写 skill.md。Linux 等大小写敏感文件系统上，SKILL.md 不会被加载。
```

示例目录把 `SKILL.md` 改名为 `skill.md`（或确认不把大写文件当正式技能提交）。

---

### [🟡 Medium] slash 前缀过滤：keyword 已 toLowerCase，skill.name 未折叠

**位置**: `webApp/frontend/js/slashCommand.js` `onInput`

**问题**: `keyword = value.slice(1).toLowerCase()`，skill 项却用原始 `'/skill:' + skill.name` 做 `indexOf`。白名单允许 `A-Za-z`。`name: CodeReview` 时用户键入 `/skill:code` → `'skill:CodeReview'.indexOf('skill:code') === -1`，面板空。`/model` `/new` 全小写所以旧逻辑没暴露。

**修复方案**:

```javascript
// before
var label = '/skill:' + skill.name;
if (label.slice(1).indexOf(keyword) === 0) {

// after
var label = '/skill:' + skill.name;
if (label.slice(1).toLowerCase().indexOf(keyword) === 0) {
```

`run` 仍传原始 `skill.name`，不影响 API 精确匹配。

---

### [🟡 Medium] `fillSkill` 回填会覆盖用户在等待期间的输入

**位置**: `webApp/frontend/js/slashCommand.js` `fillSkill`

**问题**: `runItem` 同步清空后才发起 `getSkillBody`。慢网络下用户已开始改输入框，回填无条件 `composerInput.value = body + '\n\n'`，把草稿冲掉。会话守卫只管 sid，不管输入框是否仍是「清空后等待回填」状态。

**修复方案**:

```javascript
// before
if (window.appStore.currentSessionId !== sid) {
  window.toast('会话已切换，已丢弃技能正文');
  return;
}
composerInput.value = body + '\n\n';

// after
if (window.appStore.currentSessionId !== sid) {
  window.toast('会话已切换，已丢弃技能正文');
  return;
}
if (composerInput.value.trim() !== '') {
  window.toast('输入框已有内容，已丢弃技能正文');
  return;
}
composerInput.value = body + '\n\n';
```

---

### [🟡 Medium] 技能页快速进出可能叠两份卡片

**位置**: `webApp/frontend/js/skillsView.js` `render`

**问题**: 每次 `open()` 先 `innerHTML = ''` 再 `getSkills()`。慢响应晚到时不会作废：先打开的请求在第二次清空之后才 `appendChild`，和第二次渲染叠在一起。只读页、需连点路由才触发，不是主路径。

**修复方案**:

```javascript
var renderSeq = 0;
function render() {
  var seq = ++renderSeq;
  listEl.innerHTML = '';
  window.api.getSkills().then(function (data) {
    if (seq !== renderSeq) return;
    // ... 原渲染
  }).catch(function () {
    if (seq !== renderSeq) return;
    // ... 原失败态
  });
}
```

---

### [🔵 Low] 其它（归纳）

1. **`setToken` 包裹重复加载（疑点 B）** — `slashCommand.js` 末尾保存 `originalSetToken` 再包一层。本项目是一次性 `<script>` IIFE，整页刷新会连 `store.js` 一起重跑，**生产路径不会套娃**。只有「只热替换 slashCommand.js、不重跑 store.js」才会 N 次 `getSkills`。不必改；真要防呆就加 `if (window.appStore._refreshSkillCacheHooked) return;`。
2. **`fillSkill` 守卫（疑点 C）** — 见下方专节。逻辑正确。toast 在「离开会话到设置页（sid→null）」时也说「会话已切换」，文案略宽，非必须改。
3. **`flamingoAgents/skills/skillStore.py` 未使用的 `from datetime import datetime`** — 删掉即可。
4. **`styles.css` 空规则 `.skills-list { }` + 注释仍写「设置页」**；`index.html` 文件头还留着已废弃的 `#skillsSection` 一句。不影响运行。
5. **`getSkillBody` 二次 `read_text` 未捕 `OSError`/`UnicodeDecodeError`** — 扫描后文件被删/非 UTF-8 会掉进统一 500。量小，可改成 `LookupError` 变 404。

---

## 三个必答疑点

### A. 大小写可移植性

**等级：🟠 High。**

代码认小写 `skill.md` 是方案原意，不是实现 bug。问题是 **Mac 验收无法代表 Linux 行为**，再叠加仓库示例实际是 `SKILL.md`。

| 处理层 | 要不要做 |
|---|---|
| 代码兼容 `SKILL.md` | 不要（方案 §11 明确排除） |
| 代码 debug 点名 `SKILL.md` | 建议，便宜且只在敏感 FS 上触发 |
| 文档写死小写 | **必须** |
| 示例文件改名为 `skill.md` | **必须**（否则 Linux 上自带技能全灭） |

### B. `setToken` 多次包裹

**不是实际问题。**

加载顺序是 `store.js` → `slashCommand.js`，各执行一次。没有动态 `import()`，没有只重载单文件的开发服务器。套娃需要「store 单例活着 + slashCommand IIFE 再跑一遍」，当前模型不可达。保持现状即可。

顺带：`clearToken`（401）没挂钩，登出后门内 `cachedSkills` 会脏着；登录门挡住 composer，重新 `setToken` 会再拉。可接受。

### C. `fillSkill` 会话守卫

**「无会话 (null)」和「切换会话」两条边界都正确。**

```javascript
var sid = window.appStore.currentSessionId;           // 快照
// await getSkillBody
if (window.appStore.currentSessionId !== sid) return; // 比对
```

依据：

- 面板入口 `onInput` 要求 `window.appStore.currentSessionId` 为真，UI 选中时 `sid` 一定是会话 id，不会快照到 null。
- 切到另一会话：`chatView.open` 改写 id，`!==` 丢弃，避免正文写进新会话（全局只有一个 `composerInput`）。
- 离开聊天（设置 / 技能 / 用量 / 空首页）：`main.js` `route()` 先把 `currentSessionId = null`，`null !== sid`，同样丢弃。
- A→B→A 在响应返回前绕回：守卫视为「仍是该会话」并写入。这是「仍处于该会话」的本意，不是漏洞。

`runItem` 清空语义未改：先空再异步填，最终框里是正文。与 H2 一致。

---

## 优点记录

- 注入点干净：`skillsDir == ''` / `None` / 路径三分支拆开，避开 `if skillsDir` 把 `None` 和 `''` 判成一路。
- resume 红线守得住：只改 `builder.createAgent`，conversation / agent / agentManager 装配签名都没动。
- `GET /api/skills/{name}` 按加载结果映射 + `resolve().is_relative_to`，没有 `skillsDir / name / skill.md` 拼接，穿越面收掉。
- slash 没动 `runItem`，skill 走动态 items + 异步回填，`/model` `/new` 的「先清空再跑」不受影响。
- 技能页真正平级：独立 `#skillsPage`、侧栏入口、路由，失败只影响本页。
- 库 debug 有门控；XML 五字符转义按方案做了。
- 前端卡片全 `textContent`，技能名/描述不会走 HTML 注入。

---

## 修复优先级 Top 3

1. **🟠 示例 `SKILL.md` 改名为 `skill.md` + README/契约写死小写约定**（疑点 A）— 不改加载语义也能消掉 Linux 静默空载；这是唯一会让「Mac 验过、Linux 上线全无 skill」的问题。
2. **🟡 slash 过滤对 `skill.name` 做 `toLowerCase`** — 白名单允许大写，过滤却折叠了输入，属于实现自相矛盾，改动一行。
3. **🟡 `fillSkill` 回填前检查输入框仍为空** — 保护慢网下用户已敲的草稿；和会话守卫同一层。

（技能页 renderSeq 可顺手带上，不进 Top 3。）

---

## 是否可合并

**有条件可合并。**

主路径逻辑正确，硬约束 8 条全部满足，无 Critical，无「合进去会写坏 resume / 会路径穿越 / 会改掉 `runItem`」这类红线问题。

合入前建议做完 Top 1（改名 + 文档，**可以不改 Python 加载语义**）。Top 2 / Top 3 建议同 PR 修，不修也不至于当场坏掉（现有示例 name 是 `git` / `code-review`，全小写）。

不要为了疑点 B 去「重构」`setToken` 挂钩。
