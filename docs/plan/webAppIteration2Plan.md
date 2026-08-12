# webApp 迭代二方案：会话状态栏 + 快捷指令(/、@) + 右侧文件浏览器

- Author: wilbur
- Version: 1.3
- Date: 2026-08-07
- 范围：`webApp/`（前后端）+ `flamingoAgents/core/conversation.py`（一处小改）
- 上游文档：`docs/webAppPlan.md`、`docs/webApiSpec.md`
- 评审记录：`docs/codeReview/260807_webAppIteration2Plan.md`（v1.1 已落实全部 🟠/🟡 与 🔵 修复）
- v1.2（用户评审反馈）：新增图标需求（§1.4）；D1 按 turn 计价用户已确认；D6 改为代码高亮 + Markdown 渲染；files 端点不再屏蔽任何文件（含 dotfiles 与 .git，用户明示接受凭证暴露风险）；D8 保留（message 与 attachments 不同时为空）
- v1.3（增量评审修复）：补 hljs 主题 CSS（M1）与未知语言降级预判（M2）；favicon/script 引入点写明（L1）；空文本时附件名标题截断 20 字（L3）

> **实施状态（2026-08-08）：已全部实施并通过 Playwright 端到端验证**（验证计划 §6 全部 9 项）。实施中额外修复两处方案外问题：
> 1. 面板键盘拦截需用 `stopImmediatePropagation`（textarea 为事件目标本身时 `stopPropagation` 挡不住同节点先注册的 chatView Enter→send）；
> 2. 状态栏刷新时机由「completed 事件」改为「SSE 连接关闭后」（泵线程先回写索引后放哨兵，事件到达时索引可能尚未回写）。
> 3. `/new` 增加 `findSession` 未命中时先 `sidebarView.refresh()` 再查的兑底（原逻辑静默无反应）。

---

## 1. 需求概述

1. **会话状态栏**：输入框（composer）底部新增两行信息——
   - 第一行：当前会话 `workDir` + git 分支（workDir 是 git 仓库时显示，否则不显示分支）；
   - 第二行：当前会话的输入 tokens、输出 tokens、缓存命中 tokens、费用估算、会话窗口剩余百分比。
2. **快捷指令**：
   - 输入框行首输入 `/` 弹出指令面板，本期实现 `/model`（切换当前会话模型）、`/new`（同 workDir 新开会话）；指令注册表可扩展；
   - 输入 `@` 弹出当前 workDir 的文件/文件夹选择面板；选中文件后，发送时把**整个文件内容**随本次请求发给模型。
3. **右侧文件浏览器**：聊天页右侧常驻（可折叠）当前 workDir 的文件树；点击文件弹出预览。
4. **品牌图标**：`webApp/flamingo.png` 作为浏览器 favicon；`webApp/flamingo2.png` 作为对话机器人头像（替换 chatView 现有的 🦩 emoji 与空态 logo）。

---

## 2. 决策点（需用户确认，附推荐项）

### D1. 费用统计口径（✅ 用户已确认 A）
- **A（已选定）**：会话**累计**费用，按 `usageTurns` 表逐 turn 的当时 `providerId/modelId` 分别计价（每次模型调用返回 usage 后由泵线程记一行，费用 = 每行 × 该行当时模型单价再求和；复用 `usageStore.loadCostMap()` 当前价），与用量页口径天然一致；跨模型会话（/model 切换后）历史 turn 仍按原模型价计。

### D2. 「会话窗口剩余百分比」的计算依据
- **A（推荐）**：`1 − lastTurnTokens / contextWindow`，其中 `lastTurnTokens = 最近一次模型调用的 promptTokens + completionTokens`（对下一次请求上下文量的最佳已知估计），**clamp 到 [0, 100]**；`contextWindow` 取当前会话模型在 `models.yaml` 中的配置。会话尚无调用时显示 100%；模型未配置 contextWindow 时该段显示 `-`。
- B：本地 tokenizer 精确计算。需引入分词器依赖，过重，不推荐。

### D3. `/model` 的作用范围
- **A（推荐）**：只切**当前会话**——改写 sessions 索引的 `providerId/modelId` 并废弃 agent 缓存（下次发消息按新模型重建），不动 `models.yaml`，不影响其它会话。
- B：切全局默认。与现有「每会话独立模型」的架构冲突，不推荐。

### D4. `@文件` 内容注入方式
- **A（推荐）**：前端只把 `@路径` 作为 chip 留在输入框上方，发送时在 `POST /api/chat/stream` 请求体新增 `attachments: [{path}]`；**后端**读取文件、拼接进发给模型的 message（并落 jsonl，保证 resume 后上下文一致）。前端气泡只显示用户原文 + 附件 chip；历史回放时按约定的标记语法把附件块渲染成折叠 chip。
- B：前端取内容后直接拼进 message 字符串。气泡/历史里充斥大段文件内容，且路径校验分散到前端，不推荐。

### D5. 文件大小 / 二进制 / 总量限制
- **A（推荐）**：单文件 ≤ 512KB 且为文本（NUL 字节嗅探）；**附件合计 ≤ 1MB**（附件落 jsonl 后每轮都会重复计入 prompt，无总量上限时一次超窗会拖死整个会话）；单次附件 ≤ 8 个。目录列举每层上限 500 条。
- B：不做总量限制。有费用与可用性风险，不推荐。

### D6. 文件预览呈现（✅ 用户已选定：高亮 + Markdown）
- **代码文件**：highlight.js（新增 vendor `highlight.min.js` common 语言包 ~120KB + **主题样式 `highlight-theme.min.css`**，两者都在 index.html `<head>`/script 中引入）按扩展名映射语言做语法高亮；**渲染前先 `window.hljs && hljs.getLanguage(lang)` 预判，未注册语言（如 .vue/.toml）或 hljs 缺失时降级为纯文本**（不裸调 `hljs.highlight`，避免 Unknown language 异常）；hljs 输出本身转义 HTML，稳妥起见再过一道已 vendor 的 DOMPurify 后才 innerHTML；保留行号列。
- **Markdown 文件**（`.md`/`.markdown`）：默认用已 vendor 的 marked + DOMPurify 渲染，弹层头部提供「渲染 / 源码」切换（源码态同样走高亮）。
- 其余文本文件：等宽 `<pre>` + 行号（无高亮语言命中时同此）。

### D7. 状态栏刷新时机
- **A（推荐）**：进入会话、每次流终态（completed/error）、`/model` 切换后主动拉一次 `GET status`；不做轮询。该时机与泵线程「先回写索引、后放哨兵」的时序匹配，索引数据必然够新。
- B：定时轮询。浪费请求，不推荐。

### D8. 纯附件（无文本）发送
- **A（推荐）**：允许——用户只 @ 了文件、未输入文本也可发送：`attachments` 非空时 `message` 可为空，后端校验放宽为「message 与 attachments 不能同时为空」；后端拼上文件内容后发给模型的文本必然非空，不违反库层语义。
- B：强制带文本。交互上多一步，不推荐。

> 命名说明：「附件 / attachments」指 @ 选中的文件引用（输入框上方的 chip），因 textarea 无法内嵌文件原文，发送时由请求体把路径带给后端拼接。若觉得命名别扭，实现时请求体字段可改为 `mentionFiles`，仅命名差异。

---

## 3. 后端设计

### 3.1 新增端点一览（均挂在 `authedApi`，沿用统一异常映射与 sessionId 校验）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sessions/{sessionId}/status` | 状态栏聚合数据（workDir/gitBranch/usage/cost/context） |
| PATCH | `/api/sessions/{sessionId}/model` | 切换当前会话模型（`/model` 指令） |
| GET | `/api/sessions/{sessionId}/files?path=` | 列目录（@面板 + 右侧文件树共用） |
| GET | `/api/sessions/{sessionId}/fileContent?path=` | 读文件内容（预览 + @附件共用） |
| POST | `/api/chat/stream`（扩展） | 请求体新增可选 `attachments: [{path}]` |

### 3.2 `GET /api/sessions/{sessionId}/status`

响应：

```json
{
  "workDir": "/abs/path",
  "gitBranch": "main",            // 非 git 仓库 / workDir 已删 / 无 git → null
  "providerId": "volcano",
  "modelId": "deepseek-v4-flash",
  "usage": { "promptTokens": 123, "cachedTokens": 45, "completionTokens": 67 },
  "cost": 0.000123,               // 美元；usageTurns 按 sessionId 聚合（D1-A）
  "contextWindow": 1048576,       // 模型未配置或 yaml 异常 → null
  "contextRemainingPercent": 97.5 // contextWindow 为 null → null；否则 clamp [0,100]
}
```

实现要点：

- **usage / contextTokens 单一数据源：sessions 索引**（评审 M4：删掉「读内存 conversation」分支——D7 的刷新时机下索引必然够新，且避免对 `usageTotal` 的无锁撕裂读）。老索引无 `contextTokens` 字段 → 按 0 → 剩余 100%。
- **cost**：`usageStore` 新增 `querySessionCost(sessionId)`——`SELECT providerId, modelId, promptTokens, cachedTokens, completionTokens FROM usageTurns WHERE sessionId=?`，逐行套 `loadCostMap()` 当前价求和（公式同 querySeries）。泵流进行中 usageTurns 落后一轮，可接受（D7 不轮询）。
- **contextWindow**：`modelConfigStore.readRawYaml()` 按 providerId/modelId 取；**yaml 缺失/损坏时 catch RuntimeError → contextWindow=null、cost 按 costMap 为空自然得 0**，不影响 workDir/gitBranch/usage（评审 M1）。
- **gitBranch**：子进程 `git -C <workDir> rev-parse --abbrev-ref HEAD`（参数数组、无 shell），`timeout=2`、`capture_output`；非仓库/超时/workDir 已删 → `null`，不报错。**不缓存**，每次现查（<10ms，分支切换要立即反映）。

### 3.3 `PATCH /api/sessions/{sessionId}/model`

请求体：`{ "providerId": "...", "modelId": "..." }`（均必填——指令面板已把确切模型带上来）。

处理顺序（沿用 createSession 的「先预检后落库」原则）：

1. sessionId 校验 + 会话存在（404）；
2. `models.yaml` 存在性 + `loadModelConfigFromYaml(providerId, modelId)` 预检（400 透传中文错误）；
3. `sessionStore.updateSessionModel(sessionId, providerId, modelId)`（新函数，原子写索引）；
4. `agentManager.dropAgentIfIdle(sessionId)`——**新函数，同一把 managerLock 内完成「有活跃流 → 返回 False / 否则 pop 缓存 → 返回 True」**（评审 M7：消除「查活跃流」与「dropAgent」之间的竞态窗口）。返回 False → 409「该会话有活跃流，无法切换模型」。注意顺序：先落索引再尝试丢弃，409 时索引已是新模型——语义即「本轮跑完旧模型，下轮起新模型」，前端把 409 消息透传给用户即可，不回滚索引（评审确认窗口小、后果轻）。

**已知行为声明（评审 M8）**：waitingConfirm（待确认）不是活跃流，PATCH 会放行；dropAgent 丢弃内存 pending 后，前端确认框再点「批准」会收到 confirmationMismatch 并走现有自愈刷新。前端在切换模型成功且本地处于 waitingConfirm 时，主动关确认框并提示「切换模型已放弃当前待确认」。

响应：更新后的 session 对象。前端收到后 `sidebarView.refresh()` 同步 `appStore.sessions`（否则 `syncTopbar()` 读旧 modelId，评审 L4），再刷 topbar 与状态栏。

### 3.4 `GET /api/sessions/{sessionId}/files?path=<相对路径>`

- `path` 为相对 workDir 的路径，缺省/空 = workDir 根；**前端必须 `encodeURIComponent`**（含 `#`/`?`/空格的路径，评审 L4）。
- **安全**：`(workDir / path).resolve()` 后用 **`Path.is_relative_to(workDirResolved)`** 校验拘禁（评审 M6：杜绝 startswith 的 `/work` vs `/workEvil` 陷阱），否则 400；符号链接逃逸由 resolve 后校验一并拦截。
- 响应：`{ "path": "sub/dir", "entries": [{ "name": "src", "type": "dir" }, { "name": "a.py", "type": "file", "size": 1234, "attachable": true }] }`
  - 目录在前、文件在后，各自按名称排序；
  - **不屏蔽任何文件**（v1.2 用户明示）：dotfiles（含 `.env`）、`.git/` 全部照常返回，无 showHidden 参数；⚠️ 已知风险：`@.env` / `@.git/config` 会把凭证随请求发给模型 provider，属用户主动操作，由使用者自负；
  - `attachable = size ≤ 512KB`（@面板置灰依据）；目录无 size/attachable 字段；
  - 单条目 `stat` 失败（坏符号链接、竞态删除）→ 跳过该条目，不拖垮整层（评审 L4）；
  - 单层超过 500 条时截断并带 `"truncated": true`。

### 3.5 `GET /api/sessions/{sessionId}/fileContent?path=<相对路径>`

- 同样的路径拘禁校验；必须是文件（目录 → 400）。
- 大小 > 512KB → 400「文件过大」；内容含 NUL 字节 → 400「二进制文件不支持」。
- **workDir 被删/权限被收/坏链接等 `OSError` 统一在 fileBrowser 层转 `RuntimeError('目录不存在或不可读：…')`**，走现有 RuntimeError→400 中文透传通道，绝不落 fallback 500（评审 H2）。前端 fileExplorer 收到 400 显示空态提示而非错误条。
- 响应：`{ "path": "...", "size": 123, "content": "<utf-8 文本>" }`（`errors='replace'` 解码）。

### 3.6 库与泵线程小改：lastTurnTokens 追踪

- `flamingoAgents/core/conversation.py`（v1.8→v1.9）：`_accumulateUsage` 中追加 `self.lastTurnTokens = 本次 promptTokens + completionTokens`；`__init__` 初始化 `0`，resume 重放时从最后一条带 usage 的 assistantMessage 天然重建（usage=None 的提前 return 不会错误清零，评审已确认安全）。
- `webApp/backend/agentManager.py`：泵线程 `_recordUsage` 回写索引时多写 `contextTokens` 字段（`sessionStore.updateUsage` 加可选参数），供 status 端点读取（§3.2 单一数据源）。

### 3.7 `POST /api/chat/stream` 扩展：attachments

- 请求体新增可选 `attachments: [{ "path": "相对路径" }]`，元素 ≤ 8 个、**合计 ≤ 1MB**（评审 M3）；每个 path 走与 §3.5 相同的拘禁/大小/文本校验，任一失败 → 400 并指明文件。
- **message 校验放宽**：`message.strip()` 为空但 attachments 非空时放行（D8-A）；两者皆空 → 400。落库/发模型用拼接后的最终文本，标题沿用原文前 20 字（原文为空时用第一个附件名，同样截断前 20 字）。
- 拼接约定（**后端**完成，落 jsonl 的就是最终文本）：

  ```text
  <用户原文>

  <attachment path="src/main.py">
  ...文件全文...
  </attachment>
  ```

- **标记冲突防护（评审 M5）**：文件内容含字面量 `</attachment>` → 400 拒绝该附件并提示文件名（不静默篡改代码内容）；前端历史渲染解析时校验 `path` 属性字符集（仅 `[A-Za-z0-9_\-./]`），用户手输伪造的标记块不渲染为 chip。

### 3.8 新文件 `webApp/backend/fileBrowser.py`

集中放：常量（`maxFileBytes=512*1024`、`maxTotalBytes=1024*1024`、`maxEntries=500`）、`resolveInside(workDir, relPath)`（is_relative_to 拘禁）、`listDir`、`readFile`（**OSError→RuntimeError 中文消息**，评审 H2）。server.py 只做参数校验与调用，保持路由层单薄。

---

## 4. 前端设计

### 4.1 布局变更（index.html / styles.css）

聊天页改为三栏：左会话侧栏（不动）｜中间主区（不动）｜**右侧文件浏览器 `filePanel`**（宽 260px，`filePanelToggle` 按钮折叠/展开，折叠状态存 localStorage）。窄屏（<1100px）默认折叠。

composer 区域在输入框下方新增状态栏容器，输入框上方新增附件 chip 容器：

```html
<footer class="composer">
  <div id="attachmentChips" class="attachment-chips hidden"></div> <!-- @文件 chips -->
  <textarea …>
  <div id="composerStatus" class="composer-status hidden">
    <div id="statusLocation" class="status-line"></div>  <!-- workDir + git 分支 -->
    <div id="statusUsage" class="status-line"></div>     <!-- tokens/费用/窗口剩余 -->
  </div>
  <button id="sendButton" …>
</footer>
```

### 4.2 新模块 `js/statusBar.js`

- `refresh()`：有 currentSessionId 时 `GET status`，渲染两行：
  - 行一：`📁 /abs/workDir` + ` ⎇ branch`（branch 为 null 不渲染分支段）；路径过长 CSS ellipsis + title 悬浮全文。
  - 行二：`↑ 12.3k in · ↓ 4.5k out · ⚡ 9.8k cached · $0.0123 · 窗口剩余 97.5%`；`contextWindow` 为 null / `cost` 为 0 时对应段显示 `-`。千位缩写（≥1000 → x.xk）。
- 触发时机（§D7）：`chatView.open` 完成、流终态 `completed/error`、`/model` 切换成功、`/new` 进入新会话。chatView 流终态处调用 `window.statusBar.refresh()`（与现有 `sidebarView.refresh()` 并列）。
- 无会话时容器 hidden。

### 4.3 键盘事件总约定（评审 H1，两个面板模块共用）

chatView.js 已在 composerInput 上注册 `keydown`（Enter→send()，冒泡阶段、先注册先触发）。为避免面板打开时 Enter 把 `/model` 原文发出去：

- **面板模块一律以 capture 阶段注册 keydown**：面板打开时，对 `Enter/Tab/Escape/ArrowUp/ArrowDown` 执行 `stopPropagation() + preventDefault()` 并在面板内处理；面板关闭时不拦截、不消费。
- **Esc 全局协调（评审 L4）**：各弹层（确认框 / /面板 / @面板 / 文件预览）open 时向 `window.appStore.modalStack` 登记 close 回调，全局唯一 Esc 监听只关栈顶一层。
- 反向兜底：`chatView.send()` 开头不查面板状态（保持 chatView 不被侵入），全部依赖 capture 拦截。

### 4.4 新模块 `js/slashCommand.js`（/指令面板）

- 监听 composer `input`：值以 `/` 开头且不含空白 → 弹出面板，按前缀过滤。
- **指令注册表**（数组，后续加指令只动这里）：

  ```js
  [
    { name: '/model', desc: '切换当前会话模型', run: openModelPicker },
    { name: '/new',  desc: '在当前目录新开一个会话', run: newSessionHere }
  ]
  ```

- 键盘交互按 §4.3 约定（↑↓ 选择、Enter/Tab 执行、Esc 关闭）；鼠标点击执行。执行后清空输入框。
- `openModelPicker`：面板二级视图，`GET /api/models` 拉 provider/model 树，选中后 `PATCH /sessions/{id}/model` → `sidebarView.refresh()` + 刷 topbar + 状态栏 + 提示「已切换到 provider/model」。已是当前模型 → 提示「已是当前模型」不调接口。waitingConfirm 态切换成功 → 关确认框 + 提示「已放弃当前待确认」（§3.3）。
- `newSessionHere`：取当前会话的 `workDir/providerId/modelId` 直接 `POST /api/sessions`（`allowCreate:false`，目录必然已存在）→ `location.hash = '#/chat/' + newId`。
- 输入以 `/` 开头但不命中任何指令就按发送 → 按普通文本发送（不拦截）。

### 4.5 新模块 `js/fileMention.js`（@面板）

- **触发条件（评审 L1）**：光标前最近的 `@`，且**该 `@` 前必须是行首或空白字符**、其后无空白 → 弹面板（避免 `user@example.com`、装饰器代码误触发）。按 `@` 后已输入子串对当前目录层做名称过滤。
- 选中**目录** → 进入该层继续选；选中**文件** → 生成 chip。
- 数据源：§3.4 `files` 端点（全量返回，无隐藏过滤）。`attachable:false` 的文件置灰不可选。
- **chip 模型**：textarea 无法内嵌富文本 → chip 渲染在输入框上方 `attachmentChips` 容器（`📄 src/main.py ✕`），textarea 中不留 `@路径` 文本（选中即清掉 `@子串`）。
- 发送流程改动（chatView.send）：`attachments` 非空 → 请求体带上；**发送条件放宽为 `if (!text && chips.length === 0) return`**（D8-A）；气泡渲染 = 用户原文 + 附件 chip 行；发送成功清空 chips。
- chips 有独立 ✕ 删除入口，不随清空输入连带清除。

### 4.6 附件块的历史渲染（chatView 小改）

- 渲染 user 消息时按 §3.7 标记切分：`<attachment path="...">…</attachment>` 块渲染为可展开的折叠 chip（`<details>`，summary 为 `📄 path`，内容 `<pre>`）；`path` 字符集校验失败 → 按普通文本处理（评审 M5）。
- 其余文本照常；附件块与文件内容**一律 textContent**，不走 marked/innerHTML（XSS 红线与现有约定一致）。

### 4.7 新模块 `js/fileExplorer.js`（右侧文件树 + 预览）

- **挂载点（评审 L4）**：与 statusBar 一致，由 `chatView.open`（重载整棵树）与 `chatView.showEmpty`（隐藏面板）显式调用。
- 树：懒加载——点目录箭头才 `GET files?path=` 展开子层；每层缓存结果，刷新按钮清缓存重拉根层；`files` 接口 400（如 workDir 被删）→ 面板显示空态提示而非错误条（评审 H2）。图标：📁/📄。
- 预览：点文件 → `GET fileContent` → 弹层（复用 `.modal-mask` 体系新增 `filePreviewModal`，登记进 Esc 栈）：标题为相对路径；正文按 D6 渲染——代码文件 highlight.js 高亮（输出过 DOMPurify）+ 行号列，`.md` 默认 marked + DOMPurify 渲染且头部有「渲染/源码」切换，纯文本等宽 `<pre>` + 行号；Esc/点遮罩/✕ 关闭；内容超 10 万字符截断显示并提示「已截断」。
- 无会话（chatEmpty）时面板隐藏。

### 4.8 品牌图标（新需求 §1.4）

- `webApp/flamingo.png`、`webApp/flamingo2.png` 复制进 `webApp/frontend/`（静态挂载只服务该目录，原文件保留不动）。
- favicon：index.html `<head>` 加 `<link rel="icon" type="image/png" href="/static/flamingo.png">`；同时引入 `<script src="/static/vendor/highlight.min.js">`（dompurify 之后）与 hljs 主题 css link。
- 机器人头像：chatView `buildAssistantShell` 的 🦩 emoji 与 `chatEmpty` 空态 logo 改为 `<img src="/static/flamingo2.png">`（CSS 圆形裁切 32px；空态 64px；登录门 logo 不换，保持范围收敛）。原图各 ~1.5MB，浏览器缓存后无重复开销；如后续在意首屏，再压缩出缩略图，本期不处理。

### 4.9 api.js 新增

```js
getSessionStatus(sessionId)
updateSessionModel(sessionId, providerId, modelId)
listFiles(sessionId, path)               // path 一律 encodeURIComponent
getFileContent(sessionId, path)
```

`sse.streamPost('/api/chat/stream', …)` 调用处请求体加 `attachments`。

---

## 5. 安全清单

| 风险 | 措施 |
|---|---|
| 路径逃逸（`../`、符号链接） | `resolveInside`：resolve 后 `Path.is_relative_to` 校验，统一 400（M6） |
| 大文件撑爆上下文/内存 | 单文件 512KB（列举置灰 + 读取/附件 400 双保险）+ 附件合计 1MB + ≤8 个（M3） |
| 二进制文件 | NUL 嗅探，400 |
| 凭证泄漏 | 用户明示不屏蔽任何文件（含 `.env`/`.git`）：@/预览敏感文件属主动操作，风险已声明接受（v1.2） |
| XSS | 文件内容/名称/附件块一律 textContent；预览高亮（hljs 输出）与 md 渲染（marked 输出）均过 DOMPurify 后才 innerHTML；附件 path 字符集校验（M5） |
| 标记注入/伪造 | 内容含 `</attachment>` 的附件 400 拒绝；前端解析校验 path 字符集（M5） |
| git 命令注入 | 参数数组、无 shell、固定子命令、timeout=2 |
| 模型切换竞态 | `dropAgentIfIdle` 单锁完成检查+丢弃（M7）；waitingConfirm 丢弃 pending 已声明（M8） |
| 预期内 IO 错误 | fileBrowser 层 OSError→RuntimeError→400 中文透传，不落 500（H2） |
| yaml 缺失/损坏 | status 端点降级：contextWindow=null、cost=0，主信息不受影响（M1） |
| 键盘事件冲突 | 面板 capture 阶段拦截 + 全局 Esc 栈（H1/L4） |

---

## 6. 验证计划（目标驱动，不引入测试框架）

1. **状态栏**：非 git 目录与 git 目录各建会话 → 分支段有/无；发一条消息 → 流终态后 tokens/费用/窗口百分比刷新，费用与用量页同会话数字一致（含 /model 切换后的跨模型会话）；`git checkout -b x` 后刷新 → 分支变更；删除 models.yaml 中 contextWindow → 该段显示 `-` 且其余正常。
2. **/model**：切到另一模型 → topbar/侧栏/状态栏 modelId 均变化，发消息确认走新模型（jsonl assistantMessage.model）；活跃流中切换 → 409 提示且索引已是新模型、下轮生效；waitingConfirm 态切换 → 确认框关闭并提示。
3. **/new**：会话内执行 → 新会话同 workDir 同模型，侧栏出现新条目并跳转。
4. **@附件**：@选中 py 文件发送 → 模型回答能引用文件内容；纯 chip 无文本可发送；jsonl userMessage 含 attachment 块；刷新后该消息附件折叠为 chip；@超 512KB 文件置灰；合计超 1MB → 400；内容含 `</attachment>` → 400；输入 `user@example.com` 不弹面板。
5. **路径安全**：手工构造 `path=../../etc/passwd`、`path=../workEvil`（与 workDir 同前缀目录）调 files/fileContent → 400。
6. **文件浏览器**：懒展开多层目录（含 `.git`、dotfiles 均可见）；点 py 文件出预览、高亮与行号正确；点 md 文件默认渲染态、可切源码；Esc 只关预览不关确认框；折叠状态刷新后保持；删除 workDir 后刷新面板 → 空态提示而非 500 错误条。
7. **键盘回归**：/面板打开时按 Enter → 执行指令而非发送；@面板打开时 Enter → 选中文件；面板关闭时 Enter → 正常发送。
8. **图标**：浏览器 tab 显示 flamingo.png favicon；assistant 消息头像与空态 logo 显示 flamingo2.png。
9. **回归**：确认框、停止、pending 恢复、用量页、设置页原有流程不受影响。

---

## 7. 实施步骤（建议拆任务顺序）

1. `fileBrowser.py` + files/fileContent 端点 → 验证 5、6（后端部分）；
2. conversation.py lastTurnTokens + agentManager/sessionStore contextTokens 回写 + usageStore.querySessionCost + status 端点 → 验证 1（后端部分）；
3. dropAgentIfIdle + PATCH model 端点 → 验证 2（后端部分）；
4. chat/stream attachments（含总量/标记防护、message 校验放宽）+ chatView 历史折叠渲染 → 验证 4（后端部分）；
5. statusBar.js + 布局 → 验证 1；
6. slashCommand.js（/model、/new，capture 键盘约定）→ 验证 2、3、7；
7. fileMention.js + send 流程改造 → 验证 4、7；
8. fileExplorer.js + 预览弹层（Esc 栈、highlight.js/md 渲染）→ 验证 6；
9. 图标替换（favicon + 头像，vendor 引入 highlight.js）→ 验证 8；
10. 全量回归（验证 9）。
