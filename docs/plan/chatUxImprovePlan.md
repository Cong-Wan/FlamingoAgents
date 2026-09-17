'''
Author: wilbur
Version: 1.1
Date: 2026-09-15
Description: 聊天交互四项改进方案——(1) markdown 预览 mermaid 渲染；(2) 文字块/代码块右上角一键复制；
             (3) LLM 错误持久化到历史对应位置；(4) 输入框 ↑ 召回上一轮已发送内容。
             v1.1：↑ 与流式/画消息解耦；召回文字与气泡一致；独立审核 P1 已吸收。
'''

# 聊天交互四项改进方案（mermaid / 一键复制 / 错误持久化 / ↑ 召回）

- Author: wilbur
- Version: 1.1
- Date: 2026-09-15
- 状态：**待实施**（v1.1 已按独立审核 P1 + 用户澄清修订）
- 审核：`docs/codeReview/260915_chatUxImprovePlan.md`（无 P0；P1 两条已改）
- 相关代码：`webApp/frontend/js/markdown.js`、`chatView.js`、`fileExplorer.js`、`styles.css`、`index.html`；
            `webApp/backend/historyView.py`（需求 3 唯一后端触点）；`docs/webApiSpec.md` §2.2
- 前端约束：原生 HTML/CSS/JS，无构建、无框架、无运行时 CDN；第三方进 `webApp/frontend/vendor/`
- 上游约束：XSS 红线（marked → DOMPurify 后才 innerHTML）；live 帧 rAF 合并（`streamingLatencyFixPlan` D4）；
            错误内联块既有路径（`modelUxImprovePlan` D5/T10，**需求 3 推翻其「错误不落盘」口径**）

---

## 0. 调研结论（先讲清楚，再谈改）

### 0.1 需求 1：mermaid —— 渲染层已统一，缺的是终态后处理

现状流水线（`markdown.js`，聊天 live/历史 + 文件预览共用）：

```text
markdown 文本 → marked.parse({ gfm, breaks }) → DOMPurify.sanitize → el.innerHTML
                 → 若 opts.highlight：围栏 pre>code 走 highlight.js（未知语言跳过）
```

` ```mermaid ` 围栏会被 marked 输出为 `<pre><code class="language-mermaid">`。`hljs.getLanguage('mermaid')` 为假，高亮路径跳过，所以现在看到的是灰底纯文本代码块——**不是解析器缺能力，是没有图表渲染器**。

调用点与 `highlight` 开关（已读码确认）：

| 调用点 | 文件 | highlight | 含义 |
|--------|------|-----------|------|
| `flushLivePaint` | chatView.js | **false** | 流式每帧，禁止重活 |
| `renderFinal` | chatView.js | true | completed / 跨窗口 stopped / 内联 error / 本窗口 stop |
| `appendAssistantHistory` | chatView.js | true | GET messages 历史回放 |
| `buildMarkdownView` | fileExplorer.js | true | 文件预览「渲染」模式（`.md`） |

约束（`markdownRenderUnifyPlan` 已拍板，不得回退）：

- 无构建、无运行时 CDN；第三方必须 vendor 单文件 UMD。
- 不可信文本必须 marked → DOMPurify 后才 `innerHTML`（mermaid 产出的 SVG 同样过 DOMPurify）。
- live 帧每 token 全量 parse 已用 rAF 合并；**不得在 live 帧里跑 mermaid.render**（半截语法必抛、且每帧重渲图表会卡死）。

mermaid 体积：UMD `mermaid.min.js` 约 2.5–3.5MB，比现有 vendor（chart 208KB / hljs 123KB / marked 35KB）大一个数量级。必须懒加载，不能进 `index.html` 启动脚本链。

### 0.2 需求 2：一键复制 —— 无现成按钮，LAN HTTP 无 Clipboard API

- 聊天助手气泡：`.msg-body` 无复制入口；用户气泡是 `textContent` 纯文本。
- 围栏代码：`.markdown-content pre` 无按钮、无 `position: relative`。
- 工具卡入参/出参：有折叠无复制（**本期不做**）。
- 思考链：`textContent`，不是 markdown（**本期不做**）。

剪切板红线：服务以 `http://<局域网IP>:8787` 访问时 **不是 secure context**，`navigator.clipboard` 不可用。localhost 才是。必须做 `document.execCommand('copy')` 降级，否则「点了没反应」。

「文字块、代码块」口径（本期选定）：

- **代码块** = markdown 围栏 `pre`（含文件预览渲染态里的围栏）。
- **文字块** = 助手消息整段原始 markdown（`msg.content` / `step.textBuf`），不是每个 `<p>`。
- 用户气泡、工具卡、思考链、文件预览「源码」模式：不做。

### 0.3 需求 3：错误刷新即消失 —— 是当年的明确决策，不是漏实现

live 路径已经把模型错误内联到当前 step（`appendInlineErrorBlock` → `.msg-error-block`）：

```text
SSE error（非 stopped / pendingConfirmationExists / confirmationMismatch / emptyMessage）
  → 有 currentStep.live.bodyEl → 挂红块 + ✕
  → 否则回退顶部 #errorBar
```

刷新后消失的根因链：

1. 库 `agent.logModelError` **已经把终态失败写入 jsonl**（`type: modelError`，含 `errorType` / `message` / `attempt` / `willRetry` / 诊断字段 / 可选 `request`）。
2. `historyView.loadMessages` 按契约 §2.2-M2 **主动过滤** `modelError`（与 `systemMessage` 并列不下发）。
3. `modelUxImprovePlan` D5 原文：「错误块不落盘：刷新后消失（错误是本轮瞬态，非历史事实）」。
4. 前端 `renderHistory` 只认识 `user` / `assistant` / `tool` 三种 kind。

所以：**数据在盘上，契约故意不下发，前端无从渲染。** 这不是纯前端能修的——至少要改 `historyView.py` 的 DTO 口径（库 jsonl 格式不动，agent.py 不必改）。

jsonl `modelError` 与 SSE `error` 文案不对齐，必须在 DTO 层对齐：

| | jsonl `modelError` | SSE `errorEvent` |
|--|--------------------|------------------|
| `message` | `str(error)` 原始异常 | `模型调用失败（已重试{attempt}次）：{error}`，`attempt` 是 0-based 重试次数 |
| `attempt` | 1-based（`attempt + 1`） | 无独立字段 |
| `willRetry` | 每次尝试都写；重试中为 `true`，终态为 `false` | 只有终态才 yield errorEvent |
| `request` | 可能含整段请求（图片已脱敏），体积可能很大 | 无 |

下发规则：只收 `willRetry is not True` 的终态行；**禁止把 `request` / traceback / diag 大字段塞进 GET messages**。

live 挂载位置 vs 历史回放位置必须同构：

| 失败时机 | live | jsonl 顺序 | 历史应挂到 |
|----------|------|------------|------------|
| 发送后模型首 round 就失败（无 assistantMessage） | `send()` 预建的空助手壳 | `userMessage` → `modelError` | **新建**助手壳 + 红块（对齐 live 空气泡） |
| 工具批次之后下一 round 失败 | 红块挂在当前 step（含工具卡的那块） | `assistantMessage` → `toolResult*` → `modelError` | 挂到**上一条助手壳** body |
| 确认续流失败 | 红块挂在 restored 历史助手壳 | 同上 | 同上 |

`kind === 'tool'` 目前在 `renderHistory` 里被跳过（配对消费），但仍要记 `lastKind`，否则「工具后失败」会被误判成「跟在助手正文后」——视觉上碰巧对，逻辑上不稳。实现时显式记 `lastKind`。

**本期不覆盖**（jsonl 根本没对应终态行，不是「过滤掉了」）：

- 用户停止（`stopped` / `modelInterruptedError`）：无 `modelError`；工具中断靠 dangling 卡，纯文本中途停止刷新后半截正文会丢（既有行为）。
- `maxStepsExceeded` / `emptyMessage` / `imageInputError` / `pendingConfirmationExists`：不走 `logModelError`。
- 泵诊断 `pumpError` / `sseGenError`：服务端故障，不是 LLM 回答错误。

### 0.4 需求 4：↑ 召回 —— composer 已有 ↑↓ 占用，但可安全叠加

`composerInput` 的 keydown 现状：

| 监听 | 阶段 | 何时吃掉 ↑↓ |
|------|------|-------------|
| `slashCommand.js` | **capture** + `stopImmediatePropagation` | 斜杠面板打开 |
| `fileMention.js` | **capture** + 同上 | @ 面板打开 |
| `chatView.js` | bubble | 只处理 Enter（发送） |

面板打开时 capture 先拦，召回监听不会误伤。IME 组合态（`isComposing` / `keyCode === 229`）必须放行，否则中文候选被 ↑ 改成历史消息。

**↑ 跟模型还在吐字、画气泡、多窗口接流没有任何关系。** 它只记「你点发送时气泡里那行字」。

「上一轮发出去的内容」= 气泡可见原文（无技能 chip 就是你打的字；有 chip 就是气泡上的 `/skill:名` + 补充），**不是**发给模型的 `wireText`（里面可能有整份技能正文）。纯附件、一个字都没有 → 不记。

刷新后还想 ↑：打开会话、**第一次拿到完整历史之后**灌一次列表即可。禁止在「把消息画到屏幕上」或「接上正在跑的流」时改这个列表。

---

## 1. 设计决策（含权衡，已选定）

### D1 mermaid：vendor UMD + 懒加载 + 仅终态渲染（绑 `opts.highlight`）

- 把 `mermaid.min.js`（选定 **mermaid 11.4.x** 的 UMD 构建，以实际下载的 `dist/mermaid.min.js` 为准）放进 `webApp/frontend/vendor/`。`index.html` **不**加启动 `<script>`。
- `markdown.js` 在 `opts.highlight === true` 且 DOM 里存在 `pre > code.language-mermaid` 时，才注入 `/static/vendor/mermaid.min.js`（Promise 单例；失败则保留源码块）。
- 加载后 `mermaid.initialize({ startOnLoad: false, theme: 'default', securityLevel: 'strict', fontFamily: 与应用字体对齐 })`。
- 每个围栏：用占位 `.mermaid-block` 替换 `pre` → `mermaid.render(uniqueId, source)` → `DOMPurify.sanitize(svg)` 写入占位（沿用默认 HTML+SVG，禁止 `KEEP_CONTENT`）。失败：占位改回源码 `pre` + 一行「图表渲染失败」，**并给这块 `pre` 补上代码复制按钮**。
- `mermaid.render` **串行队列**（v10/v11 内部临时节点 `#d{id}` 并发会撞）。id 用递增计数，避免冲突。
- live 帧 `highlight:false`：继续显示灰代码块（半截 mermaid 源码可见，可接受）。`renderFinal` / 历史 / 文件预览自动出图。
- **不**把 mermaid 绑到新的 `opts.diagrams`：现有四处调用点已经用 `highlight` 区分「廉价帧 / 终态抛光」，再加开关要改所有调用方，收益为零。注释写明：`highlight:true` = 高亮 + mermaid + 代码复制按钮。

### D2 复制按钮：代码块在 `markdown.js`，文字块在 `chatView.js`

- 公共 `copyText(text)` 放 `markdown.js`（`window.copyText`）：secure context 走 `navigator.clipboard.writeText`；否则隐藏 `textarea` + `execCommand('copy')`。返回 Promise。
- **代码块**：`opts.highlight` 后处理每个 `pre`（**跳过即将被 mermaid 替换的 `language-mermaid`**，避免按钮闪一下被卸掉）。`pre { position: relative }`，按钮绝对定位右上角，默认透明，`:hover` / `:focus-visible` 显示；`(hover: none)` 触摸设备常显。点击复制 `code.textContent`，1.5s 文案改「已复制」。
- **文字块**：助手壳 `.msg-body { position: relative }` 右上角按钮，复制**原始 markdown**（历史 = `msg.content`；live 终态 = 当时的 `step.textBuf`）。闭包/属性持有原文，禁止把全文塞进 `data-*`。幂等：`renderFinal` 可能重复调用，已有按钮则更新其闭包文本，不叠第二个。**原文为空（空气泡只挂了错误红块）不插复制按钮。**
- 成功反馈做在按钮自身（「已复制」），不走 `window.toast`（toast 在屏幕底部，离复制点太远）。
- mermaid 出图后的 SVG **不**做「复制源码」按钮（可后续加；本期源码已被替换，复制 SVG 无意义）。

### D3 错误持久化：推翻 D5「错误不落盘」，只扩展 GET messages DTO

**后端（唯一后端改动）** `historyView.loadMessages`：

- 遇到 `eventType == 'modelError'` 且 `willRetry is not True`（缺省当终态，兼容 v1.21 前旧日志）→ 追加：

```json
{
  "kind": "error",
  "content": "模型调用失败（已重试N次）：{message}",
  "errorType": "...",
  "attempt": 4,
  "timestamp": "..."
}
```

- `N = max(0, (attempt or 1) - 1)`，与 SSE 文案同构。
- **不下发** `request` / `traceback` / diag（`stage`/`durationMs`/…）。
- `systemMessage` 仍过滤；`assistantMessage.timings` 仍不下发。
- 旧客户端忽略未知 `kind` → 向前兼容。

**前端** `renderHistory`：

- 识别 `kind === 'error'`。
- 若 `lastKind ∈ {assistant, tool}` 且已有 `lastAssistant.bodyEl` → `appendInlineErrorBlock(bodyEl, msg.content)`（复用 live 红块，含 ✕；关掉只影响本页 DOM，刷新再现，jsonl 不动）。
- 否则 `buildAssistantShell()` 挂到列表，再挂红块（对齐 `send()` 预建空壳）。
- `kind === 'tool'` 虽不单独渲染，也要 `lastKind = 'tool'`。

**不改** `agent.py` / jsonl 写入 / SSE 事件。live 内联块路径保持。`#errorBar` 的 REST 预检 / confirmationMismatch / emptyMessage 路径保持。

契约：`docs/webApiSpec.md` §2.2 增补第四种 kind，并改写 M2：「`systemMessage` 不下发；**终态 `modelError`（`willRetry` 非 true）下发为 `kind:error`**；重试中的 `modelError` 仍不下发；`assistantMessage.timings` 不下发」。

### D4 ↑ 召回：只记「你发出去的字」；与画消息、流式、attach 无关

用户要的是：输入框空着按 ↑，填回上一句自己发过的话。不是终端命令史，更不是流式状态机的一部分。

状态（`chatView.js` 模块级，按会话）：

```text
inputHistory[sessionId] = string[]     // 该会话气泡可见原文，新在末尾
browse = { sessionId, index } | null
draftBeforeRecall = string
```

**只在两处改列表，禁止第三处：**

1. **`send()` 成功发出去时 push**（非 retry）。文字与气泡一致：无技能 chip → `userText`；有 chip → `displayText`（`/skill:名` + 可选补充）。两者都空（纯附件）不 push。与栈顶相同不重复。上限 100。
2. **打开会话、GET 完整 messages 返回后，调用一次 `rebuildInputHistory(messages)`**（挂在 `reloadSession`，用完整数组）。每条 `kind:user`：先 `userBubbleText` 折掉技能注入全文，再剥掉旧版 `<attachment>` 块，剩下的用户字非空才入栈。

**禁止**在 `renderHistory` / `initAttachedStream` / 任何流式事件里改 `inputHistory`。接流会把消息再画一遍（还可能只画一半），跟「你上次打了什么」无关。

键盘：

- 输入框**空着**才接管 ↑（正在打字时 ↑ 只移动光标）。
- 连按 ↑ 看更早；↓ 往回，越过最新一句回到空。
- 手动改了召回的字 → 退出浏览，不覆盖正在改的内容。
- IME 组合态放行。斜杠/@ 面板打开时它们自己吃 ↑↓，召回收不到。
- 填入后 `autoResize()`，光标在末尾。
- 切会话 / 空首页：`browse = null`。
- 不写 localStorage。刷新能 ↑，靠上面第 2 处那一次灌入。

---

## 2. 影响面与兼容

| 面 | 影响 |
|----|------|
| 契约 §2.2 | 新增 `kind:error`；M2 口径从「modelError 一律不下发」改为「终态下发、重试不下发」 |
| jsonl / 库 | **零改动**。旧日志缺 `willRetry` 的 `modelError` 按终态展示 |
| SSE | 零改动。live 错误块仍走现有 `handleStreamError` |
| 前端启动体积 | 不增加（mermaid 懒加载）。首次遇到 mermaid 围栏才拉 ~3MB |
| 文件预览 | 渲染 `.md` 自动享有 mermaid + 代码复制（同 `renderMarkdown`） |
| 缓存戳 | `styles.css?v=1.22→1.23`；`chatView.js?v=1.23→1.24`；`markdown.js` 目前无 query，改为 `?v=1.1` |
| 测试 | `testLiveUsageFrontend.py` / `testToolCardCollapse.py` 断言了旧 cache-bust，必须同步；新增 historyView 单测 |

旧前端收到未知 `kind:error`：现有 `renderHistory` 的 if/else 会忽略（既不渲染也不抛）→ 兼容。新前端读旧后端：没有 error kind，表现与今天一样（刷新无红块）。**前后端需同发。**

---

## 3. 风险与缓解

| 风险 | 缓解 |
|------|------|
| live 帧跑 mermaid 卡死 / 半截语法抛错 | 绑 `highlight`；live 保持源码块 |
| mermaid SVG XSS | `securityLevel:'strict'` + **DOMPurify 后再 innerHTML**（红线） |
| mermaid.render 并发撞临时节点 | 模块级 Promise 链串行 |
| mermaid.min.js 缺失/加载失败 | onerror 保留源码块，聊天不白屏 |
| LAN HTTP 点复制没反应 | `execCommand` 降级；失败时按钮文案「复制失败」 |
| 复制按钮被流式 innerHTML 冲掉 | 只在终态 `highlight:true` 注入；文字块按钮挂在 `bodyEl` 而非 `contentEl`（`renderFinal` 只重写 contentEl） |
| 文字块按钮挡住首行 | `.msg-body` 预留右上 padding；按钮 hover 才实显 |
| `modelError.request` 泄露进 GET | DTO 白名单只 content/errorType/attempt/timestamp |
| 重试中的 modelError 被当成终态红块 | 显式跳过 `willRetry === true` |
| 工具后失败挂错壳 | `tool` 也更新 `lastKind` |
| ↑ 与斜杠/@ 抢键 | 面板 capture + stopImmediatePropagation，已验证无冲突 |
| ↑ 毁掉正在写的多行草稿 | 仅空输入进入浏览 |
| IME 候选被 ↑ 替换 | `isComposing` / keyCode 229 放行 |
| 刷新后 ↑ 和当场 ↑ 文字不一致 | 两处都用气泡可见原文；禁止 `renderHistory` 改栈 |
| attach 二次画消息冲掉召回列表 | 列表只在 send / reloadSession 完整 GET 两处改 |
| 旧 attachment 全文进召回 | 灌入时剥 `<attachment>` 块 |
| cache-bust 测试红 | 与 index.html 同步；并断言 markdown.js?v=1.1 |

---

## 4. 文件级改动清单

### 4.1 新增

| 文件 | 内容 |
|------|------|
| `webApp/frontend/vendor/mermaid.min.js` | mermaid 11.4.x UMD（不进 git LFS；与现有 vendor 同级） |
| `tests/testHistoryErrorView.py` | jsonl 含终态/重试 `modelError` → GET DTO 断言 |
| `tests/testMarkdownEnhance.py` | node 断言：复制按钮注入、mermaid 占位、highlight:false 不增强 |

### 4.2 修改

| 文件 | 改什么 |
|------|--------|
| `webApp/frontend/js/markdown.js` | `copyText`；`highlight` 后：代码复制按钮 + mermaid 懒加载/串行渲染 |
| `webApp/frontend/js/chatView.js` | 助手文字块复制；`renderHistory` 的 `kind:error`；↑ 召回（send push + reloadSession 灌入，不进 renderHistory） |
| `webApp/frontend/styles.css` | `.copy-btn` / `.msg-copy-btn` / `.mermaid-block` |
| `webApp/frontend/index.html` | cache-bust；**不加** mermaid 启动 script |
| `webApp/backend/historyView.py` | 终态 `modelError` → `kind:error` |
| `docs/webApiSpec.md` | §2.2 第四种 kind + 改写 M2 |
| `tests/testLiveUsageFrontend.py` | cache-bust 1.24 / 1.23 |
| `tests/testToolCardCollapse.py` | 同上 |

`agent.py` / `sseCodec.py` / `types.py` / `fileExplorer.js`：**不改**（预览自动吃到 markdown.js 增强）。

---

## 5. TODO（实施顺序）

- [ ] T1 下载 mermaid 11.4.x UMD 到 `webApp/frontend/vendor/mermaid.min.js`（校验 `window.mermaid.render` 存在）
- [ ] T2 `markdown.js`：`copyText` + 代码块复制按钮（仅 `opts.highlight`，跳过 mermaid 围栏）
- [ ] T3 `markdown.js`：懒加载 mermaid + 串行 `render` + DOMPurify + 失败回源码
- [ ] T4 `styles.css`：`.copy-btn` / `.mermaid-block` / `.msg-copy-btn`；`pre { position: relative }`
- [ ] T5 `chatView.js`：助手文字块复制（历史 `appendAssistantHistory` + live `renderFinal` 幂等）
- [ ] T6 `historyView.py`：终态 modelError → kind error；白名单字段；文案对齐 SSE
- [ ] T7 `chatView.js` `renderHistory`：kind error 挂载规则（新建壳 vs 复用 lastAssistant）
- [ ] T8 `chatView.js`：↑ 召回。`send()` push 气泡原文；`rebuildInputHistory` 仅 `reloadSession` 完整 GET 后调用一次；IME；空输入才接管
- [ ] T9 `index.html` cache-bust：styles 1.23、chatView 1.24、markdown.js?v=1.1；文件头 version
- [ ] T10 `webApiSpec.md` §2.2 + M2
- [ ] T11 测试：`testHistoryErrorView.py`、`testMarkdownEnhance.py`（加载 vendor marked + DOMPurify + 假 DOM）；同步 cache-bust 并断言 `markdown.js?v=1.1`
- [ ] T12 各改动文件头 Author/Version/Date/Description

建议批次：T1–T5（纯前端增强，可单独验收 mermaid+复制）→ T6–T7（错误持久化，前后端同发）→ T8（召回）→ T9–T12（契约/戳/测试/头）。

---

## 6. 验收清单

1. **mermaid 聊天终态**：助手回复含完整 ` ```mermaid ` 流程图，结束后出 SVG，不是灰代码块；流式过程中可以一直是源码块。
2. **mermaid 非法语法**：该块回退源码 +「图表渲染失败」，同一气泡其它段落/代码块正常。
3. **mermaid 文件预览**：打开 workDir 里带 mermaid 的 `.md`，「渲染」模式出图；切「源码」仍是文本。
4. **懒加载**：无 mermaid 的会话不请求 `mermaid.min.js`（DevTools Network）。
5. **代码复制**：终态/历史/文件预览渲染态，每个非 mermaid 围栏右上角按钮；点击后剪贴板为代码原文（含 LAN HTTP）。
6. **文字复制**：助手气泡右上角按钮复制**原始 markdown**（含围栏标记），不是渲染后的纯文本。
7. **错误持久化**：人为让模型调用终态失败 → 红块在该轮助手位置 → 刷新 / 切走再回来，红块仍在同一位置，文案含「已重试 N 次」。
8. **错误过滤**：jsonl 里 `willRetry: true` 的中间行不出现红块；重试提示块仍只在 live/attach 出现。
9. **错误位置**：用户消息后立刻失败 → 新助手壳 + 红块；工具卡之后失败 → 红块在该助手块底部（头像不重复）。
10. **↑ 召回**：空输入 ↑ 填上一句自己发的、气泡里看见的字；再 ↑ 更早；↓ 回到空；正在打字时 ↑ 不抢；斜杠/@ 面板打开时 ↑ 仍选面板条目。与模型是否还在输出无关。
11. **↑ 刷新后**：关掉页面再打开同一会话，空输入 ↑ 仍是历史上最后一句用户气泡原文（无技能注入全文、无旧附件正文）。
12. **回归**：流式 rAF、停止、确认框、斜杠/@、工具卡折叠、多窗口 attach 行为不变。

---

## 7. 明确非目标（本期不做）

| 项 | 原因 |
|----|------|
| live 帧渲染 mermaid | 半截语法 + 每帧重绘 |
| 运行时 CDN 拉 mermaid | 破「第三方进 vendor」 |
| 用户气泡 / 思考链 / 工具卡复制 | 需求是文字块+代码块；工具卡可后续 |
| mermaid 图上「复制源码」 | 可后续 |
| 把 `stopped` / `maxStepsExceeded` / `pumpError` 写入历史 | jsonl 无对应终态 modelError；范围外 |
| 改 `agent.py` 补记更多错误类型 | 需求 3 的数据已在盘上 |
| ↑ 写入 localStorage | 刷新靠打开会话灌一次足够 |
| ↑ 绑在 renderHistory / attach / 流式事件上 | 用户已否决；跟吐字无关 |
| ↑ 召回附件 chip / 图片草稿 / 把技能 chip 还原成 chip | 只填文字 |
| 暗色 mermaid 主题 | 应用是浅色 |

---

## 8. 给「四个需求都是前端」的校准

| 需求 | 是否纯前端 | 说明 |
|------|------------|------|
| 1 mermaid | 是 | `markdown.js` + vendor + CSS |
| 2 复制 | 是 | `markdown.js` + `chatView.js` + CSS |
| 3 错误持久化 | **否，必须动 DTO** | jsonl 已有 `modelError`；不下发是契约 M2。只改 `historyView.py` + 契约，不动库 |
| 4 ↑ 召回 | 是 | 发送时记下气泡原文；打开会话再灌一次。不碰流式。 |

需求 3 若坚持零后端，只能把错误塞 `sessionStorage`——多窗口/刷新清缓存/换设备全丢，和「持久化显示在对应位置」不符。正确做法是让 GET messages 把已经落盘的终态 `modelError` 露出来。
