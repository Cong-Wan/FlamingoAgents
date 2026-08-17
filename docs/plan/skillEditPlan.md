# 技能页「按模板编辑」功能方案（v2，已按审查意见修订）

- Author: wilbur
- Version: 1.0
- Date: 2026-08-14
- Description: 技能页按模板结构化编辑保存（frontmatter name/description/disable + 正文）；/skill: 改为 chip 钉住，发送时拼正文、气泡不显全文。
- Status: 已按 code-review 子代理审查修订（H1/H2/H3/M1-M4 全部吸收）

## 1. 背景与目标

当前「技能」页（`#/settings/skills`）只读展示 `config/skills/<dir>/SKILL.md` 扫描结果，
修改技能必须手动编辑磁盘文件，体验割裂。

**目标 A（编辑）**：在技能页为每个技能提供「编辑」入口，按标准 SKILL.md 模板结构
（frontmatter：`name` / `description` / `disable` + Markdown 正文）以**结构化表单**编辑，
保存后立即生效（**仅影响下次新建会话**；旧会话 resume 用磁盘上的对话历史，
不重新扫描技能——此隔离性为隐含验收标准，见 §6 用例 13）。

**目标 B（/skill: 交互重做，v3 新增，用户已拍板）**：
- 补全列表项 label `/skill:名` 不被长 desc 挤没（desc 限长省略、label 不收缩）；
- 选中后**不再把技能全文灌进输入框**，改为输入框上方生成蓝色加粗 chip `/skill:名`
  （带 × 可删，复用附件 chip 区位与交互范式），textarea 留空可继续输入补充；
- 发送：chip 存在时先取技能正文，拼 `正文 + \n\n + 补充` 发出；
- **气泡只显示 `/skill:名 + 补充`，不显示正文全文**；由此产生的
  「发送内容 ≠ 落盘内容 ≠ 气泡显示」三方漂移，处理见 §3.4。

**非目标**（本次不做）：

- 不编辑技能目录内附属文件的内容（如 `SKILL_cn.md`）；改名时它们**随文件夹原子迁移**（见 §3.1）；
- **不提供新建技能入口**（审查 L1：边际成本不低、模板占位噪音、附属文件缺失导致不完整。
  用户仍可手动建文件夹+SKILL.md）；
- 不提供技能删除；
- 不做版本历史 / 撤销。

## 2. 现状梳理（已核实的事实）

- 后端：`webApp/backend/skillStore.py` 薄封装库 `loadSkills`；`server.py` 有
  `GET /api/skills`、`GET /api/skills/{name}`（name 白名单 `[A-Za-z0-9_-]+`，
  路径拘禁、404/400 映射齐全，全局 `RuntimeError→400` 异常处理器已存在）。
  **无任何写接口**。
- 库：`flamingoAgents/skills/skillStore.py` 的 `loadSkills` 按文件夹扫描 `SKILL.md`：
  `name` 缺省=文件夹名，须匹配 `^[A-Za-z0-9_-]+$`；`description` 必填非空否则跳过；
  `disable: true` 不进 prompt；**`seenNames` 同名后者静默跳过**。
- **现状特例**：`config/skills/mygit/SKILL.md` 的 `name: git`——文件夹名与 name 不一致，
  是合法现状（加载靠 frontmatter name）。
- 前端：`skillsView.js` 纯只读卡片（renderSeq 守卫）；`api.js` 有 `getSkills`/`getSkillBody`；
  `index.html` `#skillsPage` notice 写着"修改技能请直接编辑磁盘文件"（需更新）。
- **slashCommand.js（已核实）**：技能列表缓存在模块私有 `cachedSkills`，
  `refreshSkillCache()` 是私有函数，`window.slashCommand` 仅导出 `isOpen`——
  **没有任何刷新钩子，必须改源码导出**（审查 H1）。
- Esc 栈约定（已核实）：弹层 open 时 `window.appStore.pushModalClose(closeFn)`，
  close 时 `removeModalClose(closeFn)`（参照 `fileExplorer.js:218/237`）。
- 没有任何代码消费 `SKILL_cn.md`，也没有任何代码按文件夹名硬编码引用技能目录（已 grep 确认）。

## 3. 方案设计

### 3.1 核心语义：name 即文件夹名（简化决策）

编辑保存时，**frontmatter `name` 与文件夹名对齐**：

- 若 `fields.name == 当前文件夹名`：只重写 SKILL.md；
- 否则：目标文件夹 `config/skills/<新name>` **已存在 → 400**；不存在 →
  `folder.rename(newFolder)`（同分区原子，附属文件如 `SKILL_cn.md` 随之迁移，不丢数据），
  再重写 `<新文件夹>/SKILL.md`；
- 对现状特例 `mygit/`+`name:git`：用户点保存（哪怕没改 name 输入框，表单回填的是
  frontmatter name `git`）→ 文件夹被修正为 `git/`。这是对脏现状的**一次性自愈**，
  且 `loadSkills` 缺省名回退逻辑保证对齐前后技能都能加载，无回归风险；
- 冲突检查因此**只看目标文件夹是否存在**，不依赖 `loadSkills` 结果——彻底绕开
  `seenNames` 同名跳过导致的漏检缝隙（审查 H3）。文件夹名与 name 同空间（都由
  `^[A-Za-z0-9_-]+$` 约束），"文件夹存在 ⇔ name 冲突"，检查完备；
- 并发：`saveSkill` 全程持模块级 `threading.Lock`（FastAPI `def` 路由跑线程池，
  防两个改名请求同时通过存在性检查后 rename 撞车）。

### 3.2 后端

**`webApp/backend/skillStore.py`（Version 1.0 → 1.1）**新增：

- 模块级 `_saveLock = threading.Lock()`；
- `getSkillForEdit(name) -> dict`：按 name 在 `loadSkills` 结果中定位（找不到 →
  `LookupError`），读 SKILL.md 原文重新解析 frontmatter，返回
  `{name, description, disabled, body}`。路径拘禁沿用现有
  `is_relative_to(defaultSkillsDir.resolve())` 模式；
- `saveSkill(originalName, fields) -> dict`：
  1. 校验（任一失败 → `RuntimeError`，全局映射 400）：
     `fields` 为 dict；`name` 匹配 `^[A-Za-z0-9_-]+$`；`description` 为非空 str
     （**单行**：含 `\n`/`\r` → 拒绝，见 M1；且含 `: ` 或 ` #` → 拒绝，提示改用
     中文冒号——避免 safe_dump 输出单引号包裹的丑 YAML，与 M1 同哲学：宁拒勿丑）；
     `body` 为 str（可空）；`disabled` 为 bool；
  2. `_saveLock` 内：`loadSkills` 定位 `originalName`（找不到 → `LookupError` → 404）；
     （`getSkillForEdit` 同样复用 `_saveLock`：读路径加锁代价为零，消掉
     "定位后读取前文件夹被并发改名"的缝隙）
  3. 对齐改名：若 `fields.name != 当前文件夹名` → 目标文件夹已存在则
     `RuntimeError('目标技能名已存在')`；否则 `folder.rename(target)`；
     **此后一律以新文件夹路径重新拼接写入目标**（禁止沿用旧路径变量）；
     显式定级中间态：rename 成功 + 写失败 ≈ tmp 写入异常（tmp 清理由步骤 5 兜底），
     此时技能以新文件夹名存活、内容仍为旧——`loadSkills` 缺省名回退保证不丢技能，
     属可接受中间态，**不回滚 rename**（回滚同样可能失败，引入更复杂状态）；
  4. 序列化：`yaml.safe_dump({'name':…,'description':…[, 'disable':True]},
     allow_unicode=True, sort_keys=False)` 生成 frontmatter（`disable` 仅 True 时写出；
     描述经步骤 1 校验后 safe_dump 必输出 plain 标量），拼
     `---\n{yaml}---\n\n{body}`（body 非空时保证文件以单个换行结尾）；
  5. 原子写：同目录 `SKILL.md.tmp` → `os.replace`；tmp 写入异常时 `try/finally`
     清理残留（M3）；
  6. 返回保存后的技能单项（重新 `loadSkills` 按新名取）。

**`server.py`（Version +1）**：

- `PUT /api/skills/{name}`：路径 `name` 过 `skillNamePattern.fullmatch`（非法 400）；
  `LookupError → 404`；`RuntimeError` 走现有全局处理器 → 400；
- `GET /api/skills/{name}`：响应**扩展** `description`、`disabled` 字段
  （原 `name/baseDir/body` 不变）。改为内部调 `getSkillForEdit` 统一取值，
  slashCommand 只消费 `body`，向后兼容。

### 3.3 前端

**`index.html`（Version +1）**：

- notice 文案改为："展示并编辑 `config/skills/` 下的技能（每个技能一个文件夹 + 一个
  `SKILL.md`）。保存后下次新建会话生效；改名会同步重命名技能文件夹（附属文件随文件夹迁移）。"；
- 新增 `#skillEditModal`（`.modal-mask.hidden` + `.modal.skill-edit-modal`）：
  - 标题 `编辑技能`；
  - 名称：`form-input`（text，hint "字母/数字/下划线/短横线；与文件夹同名，改名即重命名文件夹"）；
  - 描述：`form-input`（text **单行**，对应 M1；hint "单行；勿含英文冒号+空格，建议用中文标点"）；
  - 启用：checkbox `注入 prompt（下次新建会话生效）`，勾选 = 不停用（避免 `disable` 负逻辑）；
  - 正文：`form-input` textarea 等宽字体，min-height 320px；
  - `#skillEditError`（`.field-error.hidden`）；
  - 按钮：取消 / 保存（btn-primary）。

**`api.js`（1.4 → 1.5）**：新增

```js
saveSkill: function (originalName, fields) {
  return request('/api/skills/' + encodeURIComponent(originalName), { method: 'PUT', body: fields });
}
```

**`skillsView.js`（1.1 → 1.2）**：

- 卡片 head 右侧（badge 前）加「编辑」小按钮；
- 点击 → `api.getSkillBody(name)`（即扩字段后的 GET）→ 填表单；**404 时**提示
  "技能已不存在，正在刷新列表"并 `render()`（M2：被外部改名/删除的场景）；
- 弹层 open：`window.appStore.pushModalClose(closeSkillEditModal)`；close：
  `removeModalClose`（M4，遵循 store.js Esc 栈约定）；保存中保存按钮 disabled 防重；
- 保存失败：`field-error` 展示后端 `error` detail，**表单内容保留**；
- 保存成功：关弹层 → `render()` → `window.slashCommand && window.slashCommand.reloadSkills
  && window.slashCommand.reloadSkills()`（防御性判空）。

**`slashCommand.js`（Version +1，必做，审查 H1）**：

```js
window.slashCommand = {
  isOpen: function () { return panelOpen; },
  reloadSkills: refreshSkillCache   // 新增导出：技能页编辑保存后刷新 /skill: 补全缓存
};
```

### 3.4 /skill: 交互重做（v3 新增，用户已拍板方案 A + 气泡只显示名称）

**slashCommand.js（v1.4 → 1.5）**：
- 补全项渲染：`.command-desc` 限长省略 + `.command-label` 不收缩——label `/skill:名`
  恒可见（修复「前面没有名称」：现状 desc 用 `margin-left:auto` 顶到右侧把 label 挤没）；
- `fillSkill(name)` 重写为 `pinSkillChip(name)`：**不再把技能全文灌进 textarea**，
  改为在 `#attachmentChips` 区插入一枚 skill chip：蓝色加粗 `/skill:名` + × 删除钮
  （样式新增 `.skill-chip`，与 `.attachment-chip` 同排布局）；
- chip 状态暴露在 `window.skillChip`（内聚在 slashCommand.js 内实现）：
  `{ get, clear, pin }`，同名重复 pin 即替换；发送成功或 × 后清除；
- 会话切换时 chip 清除：挂在 chatView open 流程里 fileMention 清 chips 的同一位置
  （实施时定位该钩子，skill chip 跟随，不新造钩子）。

**chatView.js send() 改造（Version +1）**——核心原则：**`await` 之前同步定界，`await` 之后重校验**
（复审 H1：现有守卫全在函数首行同步段，插入 `await` 后双击会发两次、await 期间切会话会发错会话）：

```
非 retry 分支：
 1. var chip = window.skillChip && window.skillChip.get();
 2. 同步定界（在 await 之前，与现有 composerInput.value='' 同一段完成）：
    - chip 存在时：window.skillChip.clear()（先摘除，防双击重复取）；
    - composerInput.value=''; autoResize();  // 与现有一致
    - sendButton.disabled = true;           // 双击防护：await 期间 stream 仍 null，靠这个挡第二次进入
 3. chip 存在时：await window.api.getSkillBody(chip.name)
    - 失败（404/网络）：回滚 = 恢复 chip（重新 pin）+ 恢复 composerInput.value + autoResize
      + sendButton.disabled 还原 → toast → return（M3：补充文字不丢，用例 20 断言此项）
 4. await 之后重校验（H1b/c，复用 fillSkill 已有的会话守卫模式）：
    if (sessionId !== window.appStore.currentSessionId || window.appStore.stream) {
      回滚（同步骤 3）→ toast '会话已切换，未发送' → return; }
 5. var bodyText = (result && result.body) || '';
    wireText = bodyText ? (userText ? bodyText + '\n\n' + userText : bodyText) : userText;
    // L1：body 空时退化为纯补充，不产生前导换行
    displayText = '/skill:' + chip.name + (userText ? '\n' + userText : '');
    // L2：displayText 用单换行为预期显示（marked breaks:true 下紧邻两行），与 wireText 的 \n\n 无关
 6. 空校验放宽：if (!wireText && attachments.length === 0) return;（chip 单独可发）
 7. appendUserMessage(displayText, attachments)——气泡只显示名称与补充；
 8. lastUserSend = { text: wireText, attachments: attachments }（409 静默重试存全文，不丢正文）；
    POST body message = wireText（无标记，后端零改动）。
```

**chip 清理机制（复审 M1，钉死）**：fileMention 的 `resetForSession()` 是具名导出、
由 chatView 在 `open()`/`showEmpty()` 主动调用，且其 `clearChips()` 会 `innerHTML=''`
整个清空 #attachmentChips——若 skill chip 也渲染进该容器会被连带抹掉而 slashCommand 状态不知情。
定案：**skill chip 与 attachment chip 共用 #attachmentChips 容器**，且
在 chatView `open()`/`showEmpty()` 调 `window.fileMention.resetForSession()` 的**同一行**
追加 `window.skillChip && window.skillChip.clear()`（chatView 作唯一编排者，
不让 fileMention 反向依赖 slashCommand）。

**落盘格式定案（C 线）**：`message = wireText`（全文，无任何标记）。显式接受的后果：
- 历史 resume 重渲染该消息时 `appendUserMessage(msg.content)` 无 sentAttachments 分支，
  会对全文跑 ATTACHMENT_RE——**技能正文若含 `<attachment path="...">` 示例文本，
  会被误解析成折叠附件 chip**（M2：非 XSS，附件块走 textContent/DOMPurify 安全无虞，
  是渲染正确性问题）。接受为已知漂移的延伸，不改 renderHistory（改动面会扩大），
  §6 增加对应用例验证不崩溃；
- 历史气泡显示完整正文（与本次会话内气泡不一致）——信息完整优先，
  且与 409 静默重试自洽（lastUserSend 存全文）。

**后端**：`/api/chat/stream` 零改动（wireText 必非空，现有校验天然满足）。

**index.html**：placeholder 补充 `/skill:名 引用技能`；skill chip 复用 `#attachmentChips`
容器，无新结构。

### 3.5 样式

`styles.css`（Version +1，只新增）：

```css
.modal.skill-edit-modal { width: min(860px, 96vw); }
.skill-edit-body-input {
  font-family: "SF Mono", Menlo, Consolas, monospace;
  min-height: 320px; resize: vertical; line-height: 1.55;
}
.skill-card-edit-btn { /* 复用 .btn 基样式的小号变体 */ }
/* /skill: 交互（v3） */
.skill-chip { /* 蓝色加粗胶囊，复用 attachment-chip 布局 */ }
.command-label { flex-shrink: 0; }
.command-desc { max-width: 60%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
```

## 4. 接口契约变更

| 端点 | 变更 |
|---|---|
| `GET /api/skills/{name}` | 响应新增 `description`、`disabled`（原字段不变，向后兼容；slashCommand 只用 `body`） |
| `PUT /api/skills/{name}` | 新增。Body `{name, description, disabled, body}`；200 返回保存后技能单项；400 校验失败/目标名已存在；404 原技能不存在 |

## 5. 已关闭的范围决策（按审查结论定案）

- ~~Q1 新建技能~~ → **不做**（L1）；
- ~~Q2 改名~~ → **允许**，且语义 = 文件夹重命名对齐（H2 吸收：放弃"文件夹名跟随 name"
  的表述，改为"保存时把文件夹名对齐到 name"，显式声明附属文件随文件夹原子迁移）；
- ~~Q3 /skill: 缓存~~ → **必做** `reloadSkills` 导出（H1：现状一定没有钩子，无降级选项）。

## 6. 验证计划（无测试框架，手工 + python -c）

1. 技能页每张卡片出现「编辑」；
2. 编辑 `code-review`：改描述+正文加一行 → 保存 → 列表刷新显示新描述；磁盘
   `SKILL.md` frontmatter 合法、中文不转义（`allow_unicode`）、body 原样保留；
3. 取消勾选"注入 prompt"保存 → 徽章变"不进 prompt"，frontmatter 出现 `disable: true`；
   再勾回保存 → `disable` 字段消失；
4. **特例自愈**：编辑 `git`（mygit 文件夹）不改名直接保存 → 文件夹变为 `git/`，
   `SKILL_cn.md` 跟随迁移，列表正常；改回原名不影响（文件夹已是 `git/`，name 不变）；
5. 改名为 `code-review`（已存在）→ 400"目标技能名已存在"，表单内容不丢；
6. 名称输入中文/空格/空串 → 前端校验或 400；
7. 描述清空 / 描述输入换行 → 400，表单不丢；
8. 保存成功后 `/skill:` 前缀过滤出现新名/新描述（验证 reloadSkills 生效）；
9. 编辑保存后新建 CLI 会话（或 `python -c "from flamingoAgents.skills import
   loadSkills, defaultSkillsDir, formatSkillsXml; print(formatSkillsXml(loadSkills(defaultSkillsDir)))"`）
   输出包含编辑后的描述——回归 `loadSkills` 未被破坏；
10. 保存期间快速切走页面再切回 → 列表正常渲染（renderSeq 守卫不回归）；
11. Esc 关闭弹层；弹层打开时背景快捷键不响应（store.js 栈行为）；
12. 编辑弹层打开期间，另一终端把该技能文件夹改名/删除 → 点保存 → 404 提示与
    列表刷新正常（M2 保存侧对称场景）；
13. **会话隔离**：编辑某技能描述 → 已打开的旧会话发一条消息，其 system prompt 仍用旧技能；
    新建会话 → 用新技能（可用 `python -c` 对比两轮 loadSkills 输出确认）；
14. /skill: 补全项 label `/skill:git` 在长 description 下仍完整可见，desc 省略号截断；
15. 选中 `/skill:git` → 输入框上方出现蓝色 chip，textarea 为空；输入补充「只提交 src/」
    → 发送 → **气泡只显示 `/skill:git` 与补充，不显示正文全文**；
16. 发送后该消息落盘内容为技能全文+补充（查 `webData` jsonl）；刷新页面看历史
    → 该条气泡显示完整正文（已知漂移，确认渲染不报错）；
17. 发送撞 409 静默重试 → 重试发出的仍是全文（jsonl 不出现第二条残缺消息）；
18. chip 存在时 × 删除 → 发送只发 textarea 文字；chip 存在且 textarea 为空 → 单独可发；
19. pin chip 后切换会话 → chip 清除；
20. 技能页把 `git` 改名 `git2` 保存 → 回聊天页 `/skill:` 补全出现 `git2`（reloadSkills 生效）；
    若聊天页此时残留旧 chip `git` → 发送时 getSkillBody 404 → toast 提示**且补充文字与
    chip 均恢复**（不只 toast）；
21. chip 存在时双击发送/连按 Enter → 只发一次（H1a 双击防护）；
22. chip 存在时点发送后、getSkillBody 返回前快速切换会话 → toast '会话已切换，未发送'，
    消息不进旧会话，chip 与输入框恢复（H1b）；
23. 技能正文含 `<attachment path="a/b.txt">` 示例 → 发送 → 刷新看历史 → 气泡渲染不崩溃
    （允许被解析成附件 chip，M2 已知漂移）。

## 7. TODO

- [ ] 1. `webApp/backend/skillStore.py` v1.1：`_saveLock`（读/写路径共用）+
  `getSkillForEdit` + `saveSkill`（rename 后以新路径写入，中间态不回滚）
  - 验证：`python -c` 直调函数跑 对齐改名 / 目标存在 400 / 描述换行与英文冒号 400 / tmp 清理分支
- [ ] 2. `server.py`：`PUT /skills/{name}` + `GET /skills/{name}` 改调 `getSkillForEdit` 扩字段
  - 验证：curl 带 token 跑用例 5/6/7，确认 400/404 detail 中文透传
- [ ] 3. `api.js` v1.5：`saveSkill`
- [ ] 4. `index.html`：notice 文案 + `#skillEditModal` 结构
- [ ] 5. `skillsView.js` v1.2：编辑按钮 + 弹层（Esc 栈接入）+ 保存流程 + reloadSkills 调用
  - 验证：用例 1/2/3/5/10/11
- [ ] 6. `slashCommand.js` v1.5：导出 `reloadSkills`；补全项 label 防收缩 + desc 限长；
  `fillSkill` → `pinSkillChip` + `window.skillChip`（get/clear/pin；chip 渲染进 #attachmentChips）
  - 验证：用例 8/14/15/18/19
- [ ] 6.5 `chatView.js`：send() chip 分支（**同步定界→clear chip+清输入框+disable 发送钮→
  await getSkillBody→失败/切会话回滚→重校验→拼装 wireText/displayText→lastUserSend=wireText**）
  + open()/showEmpty() 同位置追加 window.skillChip.clear()
  - 验证：用例 15/16/17/20/21/22/23
- [ ] 7. `styles.css`：`.skill-edit-modal` / `.skill-edit-body-input` / `.skill-card-edit-btn` /
  `.skill-chip` / `.command-label` / `.command-desc`
- [ ] 8. 全量手工回归 §6（重点用例 4 特例自愈、9 loadSkills 回归、12 并发外部改名、
  13 会话隔离、15-17 chip 发送三态）
