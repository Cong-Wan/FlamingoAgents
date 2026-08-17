# /skill: 注入正文被模型重复 read 的修复方案（v3，grok 二轮审核修订）

## 背景

session_1aa01ee011af log 复现：用户用 `/skill:git` chip 发送提交请求后——

- **[189] userMessage**：前端 `chatView.js` 把 `mygit/SKILL.md` 全文（1700 字符）拼进 wireText 发出；
- **[190] assistantMessage**：模型按 system prompt 中 `<available_skills>` 的指引（"任务匹配时用 read 读取 location"）**又 read 了一遍 SKILL.md**。

同一份技能正文进了上下文两次：浪费 token + 多一次工具往返。

## 根因

1. `flamingoAgents/skills/skillStore.py::formatSkillsXml` 注入 system prompt 的指引只有"去 read"一种路径，不知道"正文可能已被前端注入"；
2. `webApp/frontend/js/chatView.js` 发送时（v1.14）把正文裸拼进 wireText，**没有任何标记**告诉模型"这是已注入的技能正文"，模型无从区分，按 system prompt 指引重复 read。

## 修复方案（方向 A：保留注入，加定界标记）

保留"发送时注入全文"的设计（保证模型必看到、UI 气泡只显示 `/skill:名`），给注入正文加**独特定界标签** `<injected_skill>` + 强禁止句，并在 system prompt 指引中改成 if/else 规则。

### 1. 前端 `webApp/frontend/js/chatView.js`（v1.17 -> v1.18）

**1a. send() 中拼 wireText 处（约 L1019-1021）**：

现状：
```js
var bodyText = (skillResult && skillResult.body) || '';
wireText = bodyText ? (userText ? bodyText + '\n\n' + userText : bodyText) : userText;
```

改为：
```js
var bodyText = (skillResult && skillResult.body) || '';
var baseDir = ((skillResult && skillResult.baseDir) || '').replace(/"/g, '&quot;');
var skillBlock = bodyText
  ? '<injected_skill name="' + chip.name + '" dir="' + baseDir + '">\n' + bodyText + '\n</injected_skill>'
  : '';
wireText = skillBlock
  ? ('/skill:' + chip.name
    + ' 的完整指令已作为 <injected_skill> 注入本消息；'
    + '禁止再调用 read 读取该技能的 location 或 SKILL.md，直接按 <injected_skill> 内步骤执行：\n\n'
    + skillBlock
    + (userText ? '\n\n' + userText : ''))
  : userText;
```

要点：
- `displayText` 不变（气泡仍只显示 `/skill:名 + 补充文字`）；
- bodyText 为空时行为不变（退化为纯 userText）；
- retry 路径复用 `lastUserSend.text`（即 wireText），包裹格式随之复用，无需额外处理；
- skill name 已被后端 `skillNamePattern = ^[A-Za-z0-9_-]+$` 约束，直接进 XML 属性无注入风险；`baseDir` 引号已转义 `&quot;`；
- `dir` 带技能目录绝对路径，正文相对引用（如 `references/xxx.md`）可解析，避免模型为找路径去 read location；始终输出 `dir` 属性（含空值），正则 `dir="[^"]*"` 兼容。

**1b. 历史/attach 气泡折叠（修既有泄漏）**：

`renderHistory`（L592）和 `initAttachedStream`（L1172）传给 `appendUserMessage` 的是落盘 wireText（含注入块全文），今天已在泄漏。在 `appendUserMessage`（L341）无附件分支前做窄折叠：

```js
var INJECTED_SKILL_RE = /^\/skill:([A-Za-z0-9_-]+) [^\n]*\n\n<injected_skill name="\1" dir="[^"]*">\n[\s\S]*?\n<\/injected_skill>(?:\n\n([\s\S]*))?$/;

function userBubbleText(content) {
  var match = INJECTED_SKILL_RE.exec(content);
  if (!match) return content;
  return '/skill:' + match[1] + (match[2] ? '\n' + match[2] : '');
}
```

`appendUserMessage` 无附件分支执行顺序（**写死，防实施反序**）：
```
content = userBubbleText(content);  // 先折注入块，附件块（若有）留在返回值 match[2] 里
再跑现有 ATTACHMENT_RE
```

### 2. 框架 `flamingoAgents/skills/skillStore.py`（v1.3 -> v1.4）

`formatSkillsXml` 的指引改为**先例外后 fallback**，if/else 各管各的相对路径解析根：

现状：
```
以下技能提供特定任务的专门指令。当任务与某个技能的描述匹配时，用 read 工具读取其 location 指向的文件，按其中步骤执行；技能内相对路径相对该文件所在目录解析。
```

改为：
```
以下技能提供特定任务的专门指令。当任务与某个技能的描述匹配时：
- 若上下文中已有该技能的 <injected_skill> 正文块，直接按其内容执行，禁止再 read 其 location；
  块内相对路径相对该标签的 dir 属性解析；
- 否则用 read 读取其 location，按其中步骤执行；
  技能内相对路径相对 location 所在目录解析。
```

**重要生效范围**：`conversation.py` v1.8 明确 resume 用落盘 systemMessage 保 prompt cache，**`formatSkillsXml` 修改仅新会话生效**。旧会话（含复现的 session_1aa01ee011af）只能靠 1a 的 user 侧禁止句压住 read。不为指引修改改 resume 策略（会打穿缓存）。

## 标签设计说明

不用 `<skill>` 而用 `<injected_skill>`：目录块 `<available_skills>` 里已有 `<skill>` 子标签（schema 不同），同 tag 会让模型混淆"目录里的 skill" vs "消息里的正文块"，既可能仍 read，也可能把无 chip 的普通触发误判成已注入。独特标签两端一致，信号明确。

## TODO Lists

- [ ] T1：`chatView.js` wireText 拼接改为 `<injected_skill>` 包裹 + 强禁止句（生效范围：部署后所有会话的新消息）
- [ ] T2：`chatView.js` 新增 `INJECTED_SKILL_RE` + `userBubbleText`，`appendUserMessage` 无附件分支接入（生效范围：所有匹配新格式的历史/attach，新旧会话都生效）
- [ ] T3：`skillStore.py` `formatSkillsXml` 指引改 if/else（生效范围：仅新会话，resume 不回放是预期）
- [ ] T4：验证
  - [ ] T4.1 `node --check chatView.js` 语法通过
  - [ ] T4.2 python 侧 `formatSkillsXml` 输出 diff 核对（仅指引行改，`<available_skills>` 结构不变）
  - [ ] T4.3 **无 chip 回归**：直接说"帮我提交"，模型仍应 read 对应 SKILL.md（防 T3 把目录 `<skill>` 误伤）
  - [ ] T4.4 chip-only 无补充文字：wireText 有注入块，气泡只显示 `/skill:git`
  - [ ] T4.5 `bodyText === ''` 退化：退化为纯 userText，不产出空 `<injected_skill>`
  - [ ] T4.6 409 retry：不重打 getSkillBody，`lastUserSend.text` 带同一包裹
  - [ ] T4.7 刷新历史 / attach：气泡按 userBubbleText 折叠，不显示 XML 全文
  - [ ] T4.8 生效矩阵：
    - 新会话 + chip：T1 禁止句 + T3 if/else 双保险，不应再 read location
    - 旧会话 + 部署后新 chip 消息：只靠 T1 禁止句（T3 不回放是预期）；气泡应被 T2 折叠（新格式，T2 必须生效）
    - 旧会话 + v1.14 已落盘裸正文：T2 不匹配，气泡保持泄漏（见下方「不做的事」）
  - [ ] T4.9 chip + 附件：刷新/attach 后气泡为「/skill:名 + 补充文字 + 附件折叠块」，无 `<injected_skill>` 全文

**版本号规则**：T1 + T2 同改 `chatView.js`，Version 1.17 -> 1.18（一次升级），Description 同时写「wireText 加 `<injected_skill>`」和「历史/attach 走 `userBubbleText`」。T3 改 `skillStore.py`，Version 1.3 -> 1.4。

## 不做的事（明确排除）

- 不改 `/skill:` chip 的 UI / pin 逻辑 / 错误恢复路径（v1.3-v1.5 既有行为保持）；
- 不为 bodyText 中理论出现 `</injected_skill>` 做转义（实际技能正文不含，属过度设计）；
- **不折叠 v1.14 已落盘的无标签裸正文**（无法与用户粘贴的 markdown 无损区分）；那些气泡保持泄漏。T2 只保证 T1 之后发出的消息在历史/attach 中折叠；
- 不动后端 API（getSkillBody 返回结构不变）；
- 不处理"方向 B：只发引用不发全文"（保留注入设计，是 v1.14 既定决策）；
- 不为指引修改改 resume 策略（会打穿 prompt cache）；
- 不处理「上下文已有旧注入块、后文无 chip 再提」的读取策略（按 S1 采纳"上下文已有则勿再 read"，已在 T3 指引中体现）。
