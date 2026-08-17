'''
Author: wilbur
Version: 1.2
Date: 2026-08-17
Description: 调研结论与方案：聊天流式 markdown 与文件预览共用同步渲染层；样式用手写产品 token（D2=B）。v1.1 按审核修订挂载点/消毒路径。v1.2 用户拍板 D2=B、D5 聊天 breaks:true / 预览 false，进入实施。
'''

# Markdown 渲染层统一与样式翻新方案

- Author: wilbur
- Version: 1.2
- Date: 2026-08-17
- 状态：**已实施**（D2=B 手写 token 风；D5 聊天 true / 预览 false）
- 审核：`docs/codeReview/260817_markdownRenderUnifyPlan.md`（v1.0 → 方案 v1.1）；复审 `docs/codeReview/260817_markdownRenderUnifyPlan_r2.md`
- 相关代码：`webApp/frontend/js/chatView.js`、`webApp/frontend/js/fileExplorer.js`、`webApp/frontend/styles.css`
- 相关契约：`docs/plan/webAppPlan.md` §4.6（vendor marked + DOMPurify）、`docs/plan/streamingLatencyFixPlan.md` D4（rAF 全量 parse，禁止流式期纯文本）
- 前端约束：原生 HTML/CSS/JS，无构建、无框架、无运行时 CDN；第三方进 `webApp/frontend/vendor/`

---

## 0. 调研结论（先讲清楚，再谈改）

### 0.1 现状不是「两套引擎」，是「两处复制的同一条流水线」

| 项 | 对话框 `chatView.js` | 文件预览 `fileExplorer.js` |
|----|----------------------|----------------------------|
| 函数 | 模块内 `renderMarkdown(el, text)` | 模块内 `buildMarkdownView(content)` |
| 解析 | `window.marked.parse`（v12.0.2，35KB） | 同左，同一份 vendor |
| 消毒 | `DOMPurify.sanitize` 后 `innerHTML` | 同左 |
| 容器 class | `markdown-content` | `markdown-content preview-markdown` |
| marked 配置 | 启动时全局 `setOptions({ gfm:true, breaks:true })` | **自己没配**，吃 chatView 的全局副作用 |
| 代码高亮 | **无**（markdown 里的 \`\`\` 是灰块纯文本） | 仅「源码」模式走 highlight.js + 行号；渲染模式也不高亮代码块 |
| 调用节奏 | 流式：`textBuf` + rAF，**每帧对全文** parse | 一次性，完整文件 |

流水线字面相同：

```text
markdown 文本 → marked.parse → DOMPurify.sanitize → el.innerHTML
```

没有公共模块。改一边的选项 / 后处理，另一边不会跟上。`breaks:true` 是 chatView 的全局副作用，预览「碰巧」也是单换行变 `<br>`——对真实 `.md` 文件通常是错的。

### 0.2 流式路径（这是能不能共用的硬约束）

当前 live 路径（`streamingLatencyFixPlan` Phase2 / D4，已落地）：

```text
textDelta
  → currentStep.textBuf += chunk          // 只改字符串，不碰 DOM
  → scheduleLivePaint(step)               // 每帧最多一次
  → flushLivePaint
       reasoningBuf → thinkingContentEl.textContent   // 思考链故意不走 markdown
       textBuf      → renderMarkdown(contentEl, 全文) // 每帧全量 marked + purify
```

强制 `flushLivePaint` 点（漏则丢尾，动渲染层必须保留）：

1. step 切换前
2. `completed` / `error` / `confirmationRequired` 入口
3. 进入 `stopping`
4. `goIdle` / `onStreamClosed` 双保险
5. 折叠 thinking 前（双 buffer 同时上屏）

历史回放是一次性 `renderMarkdown(contentEl, msg.content)`，与 live 终态应对齐。

**思考链不是 markdown**：`reasoningBuf` 一直是 `textContent`。统一渲染层不要去「顺便」把 thinking 也 marked 掉。

### 0.3 样式难受的根因——不是 marked 丑，是几乎没样式

证据：

1. 全局 `* { margin: 0; padding: 0 }` 清掉了 `h1–h6 / p / ul / ol / li / hr` 的浏览器默认间距。
2. `.markdown-content` 只写了 `pre / code / table / blockquote` 四类，**没有**标题、段落、列表、链接、图片、分割线、任务列表。
3. 聊天里的围栏代码块没有 highlight.js，和文件预览源码不是一路。
4. `breaks:true` 让模型常见的单换行变成 `<br>`，标题/列表之间更挤。

所以「换一个会渲染 markdown 的 JS」**解决不了观感**。marked / markdown-it / showdown 都只出 HTML，样子取决于 CSS。用户看到的「难受」主要是 **reset 后没补排版** + **代码不高亮**。

### 0.4 现成库盘点（只列能进本仓库约束的）

约束：UMD / 浏览器全局、可 vendor 单文件、无 React/Vue、XSS 仍须消毒。

| 候选 | 它到底是什么 | 体积量级 | 流式 | 结论 |
|------|--------------|----------|------|------|
| **marked**（已在用 v12.0.2） | 解析器，出 HTML | 35KB | 每次全量 parse，无增量 API | **解析器保留** |
| **DOMPurify**（已在用） | HTML 消毒 | 21KB | 与 parse 绑定 | **红线，保留** |
| **highlight.js**（已 vendor 119KB） | 代码高亮 | 已付体积 | 对大块反复 highlight 贵 | **复用，不要再引一份** |
| **markdown-it** | 另一个解析器 | ~UMD 可用 | 同样全量 | 换它观感不变，无收益 |
| **showdown** | 老解析器 | 中 | 全量 | 不换 |
| Milkdown / Toast UI / EasyMDE / vditor | 编辑器 | 大、常带构建 | 不适合气泡 | 否 |
| react-markdown / Streamdown | React 组件 | 框架 | 流式友好但绑 React | **否**（破「原生前端」） |
| **streaming-markdown**（thetarnav） | 专为 LLM 增量 parse + 补全未闭合 token | 很小 | 为此而生 | **本期不采用**（见 D3） |
| **github-markdown-css** | **只有 CSS**，给 GFM HTML 用 | light 约 20KB | 无关 | 调研首选现成件；**用户拍板改用手写 token（D2=B）** |

一句话：

- 没有「一个 JS 扔进去，聊天流式 + 文件预览都又好看又增量」的现成 UMD。
- 好看来自 **CSS**；安全来自 **DOMPurify**；流式来自 **我们自己的 rAF + 全文 buffer**。
- 拍板组合：`marked + DOMPurify + 已有 hljs + 手写 .markdown-content`（不引 github-markdown-css）。

---

## 1. 目标 / 非目标

### 1.1 目标

1. **一套同步渲染函数**给聊天（live + 历史）和文件预览「渲染模式」共用。
2. **观感达到可读的 GFM 正文**：标题层级、段落间距、列表、引用、表格、链接、围栏代码高亮，不再像被拍扁的纯文本。
3. **不回退流式性能**：长正文仍走 rAF 合并；禁止回到「每 token 同步 parse」。
4. **XSS 红线不动**：不可信文本必须 parse → purify 后才 `innerHTML`。
5. **切断全局 `marked.setOptions` 副作用**：预览与聊天的 `breaks` 可分开。

### 1.2 非目标（本期不做）

| 项 | 原因 |
|----|------|
| 换 React / 上构建链 / 运行时 CDN | 破现有前端约束 |
| 用 streaming-markdown 替换 marked | GFM 覆盖与现网回归成本，见 D3 |
| 流式期纯文本、终态再 markdown | 已被 `streamingLatencyFixPlan` D4 否决 |
| 思考链改 markdown | 现契约是纯文本，且流式更重 |
| 文件预览「源码」模式并进渲染层 | 那是 highlight + 行号，不是 markdown |
| 增量 DOM diff / 保留选区 | 现 `innerHTML` 整替换；未测到选区需求 |
| 主题切换（暗色 GitHub） | 产品现是浅色 |

### 1.3 成功标准

| # | 标准 |
|---|------|
| S1 | chat 历史、chat live 终态、文件预览渲染模式，同一输入（同一 `breaks`）HTML 结构一致（class 可多一层皮肤）。**例外**：attach 重连中间态允许历史已高亮、live 续播未高亮，终态后收敛 |
| S2 | 流式仍每帧 ≤1 次 parse；flush 五处不丢尾 |
| S3 | 未闭合 ` ``` ` / 半截表格 / 半截 `**` 流式过程允许不完美，终态与历史一致、无抛错 |
| S4 | 含 `javascript:` / `<script>` / `onerror=` 的内容消毒后不可执行 |
| S5 | 标题、列表、段落、引用、表格、链接肉眼有间距与层级；围栏代码在**非流式**路径有高亮 |
| S6 | 思考链、工具卡、确认框、stop / dangling / pending 行为不变 |
| S7 | 不新增 npm/构建/vendor CSS；新文件仅一个原生 `markdown.js` |
| S8 | marked 未加载时聊天与预览均显示纯文本而非空白（现网两处都是 innerHTML 空串丢内容，本期附带修） |

---

## 2. 决策点（实施前锁定）

### D1. 共用范围 — **选定：只抽「同步渲染函数」，不抽流式调度**

| 选项 | 做法 | 本期 |
|------|------|------|
| **A（选定）** | 新建 `webApp/frontend/js/markdown.js`，导出 `window.renderMarkdown(el, text, opts)`。chatView live/历史、fileExplorer 渲染模式都调用它。`scheduleLivePaint` / `flushLivePaint` / step 状态留在 chatView | ✅ |
| B | 再做一个 streaming helper，把 rAF 也收进去 | ❌ 预览用不上，增加空状态机 |
| C | 继续两处复制，只改 CSS | ❌ 修不了 breaks 副作用，高亮也会再复制一次 |

**API 草案：**

```js
window.renderMarkdown(el, text, {
  breaks: true,          // 默认 true（聊天）；预览传 false
  highlight: false       // live 默认 false；历史 / 流终态 / 预览 true
});
```

实现要点：

- 内部 `marked.parse(text || '', { gfm: true, breaks: !!opts.breaks })`，**禁止**再 `setOptions` 污染全局。
- `html = DOMPurify.sanitize(html)`；无 purify 则空串（与现红线一致，禁止裸 innerHTML）。
- `el.innerHTML = html`。只改传入的 `el`，调用方保证 `el` 是 `contentEl` / 预览容器，**绝不**传入 `bodyEl`（其上还有 thinking / 工具卡 / retry notice / interrupted badge / inline error）。
- **highlight 唯一路径**（与 `fileExplorer.highlightCode` 现网一致，禁止 `highlightElement`）：`highlight === true` 且 `window.hljs` 时，遍历 `el.querySelectorAll('pre code')`；用围栏 class（如 `language-js`）或跳过；`getLanguage` 失败则保持纯文本；成功则 `code.textContent` → `hljs.highlight(text, { language })` → `DOMPurify.sanitize(result.value)` → `code.innerHTML`，并给 `code` 加 `hljs` class。
- 缺 marked 时降级 `el.textContent = text`，不抛。**行为变更 / 附带 bug fix**：现网两处都是 `innerHTML = ''` 丢内容，见 S8。

chatView 删除本地 `renderMarkdown` 和顶部 `setOptions`。
fileExplorer `buildMarkdownView` 改为调公共函数，`breaks:false, highlight:true`。源码模式 `highlightCode` / `buildCodeView` **不进**本函数。

### D2. 样式来源 — **选定 B：手写 `.markdown-content`，贴本产品浅色 token**

| 选项 | 做法 | 本期 |
|------|------|------|
| A | vendor `github-markdown-css` light + 本地覆盖页级 padding | ❌ 用户未选 |
| **B（选定，2026-08-17 用户拍板）** | 在现有 `.markdown-content` 上补齐被全局 reset 清掉的标题 / 段落 / 列表 / 链接 / 图片 / 分割线；pre/code/table/blockquote 沿用产品 token | ✅ |
| C | 换解析器指望变好看 | ❌ 无效，见 §0.3 |

落地（**不**引 github-markdown-css，**不**加 `markdown-body` class）：

- 根节点保持 `markdown-content`（预览再加 `preview-markdown`）。
- **保留**现有 L270–279（`:first-child` / pre / code / pre code / table / th,td / blockquote），它们已经用 `--gray-block` / `--border`，不要拆掉重写。
- **新增**被 reset 漏掉的排版（写在 `styles.css` `.markdown-content` 下）：

```css
.markdown-content { line-height: 1.7; word-wrap: break-word; }
.markdown-content h1, .markdown-content h2, .markdown-content h3,
.markdown-content h4, .markdown-content h5, .markdown-content h6 {
  margin: 16px 0 8px; line-height: 1.35; font-weight: 600;
}
.markdown-content h1 { font-size: 22px; }
.markdown-content h2 { font-size: 18px; }
.markdown-content h3 { font-size: 16px; }
.markdown-content h4, .markdown-content h5, .markdown-content h6 { font-size: 14px; }
.markdown-content p { margin: 8px 0; }
.markdown-content ul, .markdown-content ol { margin: 8px 0; padding-left: 1.6em; }
.markdown-content li { margin: 3px 0; }
.markdown-content li > p { margin: 4px 0; }
.markdown-content hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
.markdown-content a { color: var(--accent); text-decoration: none; }
.markdown-content a:hover { text-decoration: underline; }
.markdown-content img { max-width: 100%; height: auto; border-radius: 6px; }
.markdown-content pre code.hljs { background: transparent; padding: 0; }
```

- 全局 `code { ... }`（L45）保留给非 markdown 区域；围栏内已有 `.markdown-content pre code { background:none; padding:0 }`。
- `.preview-markdown { padding: 14px 18px }` 保留，不改。
- 不引入任何 vendor CSS；hljs 主题继续用已有 `highlight-theme.min.css`。

### D3. 流式解析策略 — **选定：继续全文 marked；不换增量库**

| 选项 | 做法 | 本期 |
|------|------|------|
| **A（选定）** | 维持 D4：buffer + rAF + **全文** `renderMarkdown`。增量库以后若长文掉帧再单独立项 | ✅ |
| B | 流式用 streaming-markdown，终态再 marked 一遍对齐 | ❌ 两套语法、终态可能跳变 |
| C | 流式 `textContent`，completed 再 markdown | ❌ 已否决，半截代码块体验差 |

理由：

- v1.7 前每 token 同步 parse 已证明不可行；rAF 合并后每帧一次全文 parse，在当前消息长度下可接受（latency 方案成功标准：≥2k 字可滚动）。
- `streaming-markdown` 对未闭合 fence 更稳，但表格/GFM/脚注与 marked 不一致，历史回放仍要用 marked，会出现「流式一种样子、刷新另一种」。
- 真正的闪烁来自 **不完整 markdown 的全量重 parse**（半截 `**`、未闭合 fence）。增量库能减闪，但不是这次「统一 + 好看」的前置。

**流式半截内容的接受标准：** 允许中间帧难看，不允许抛错、不允许终态和历史不一致。

### D4. 高亮时机 — **选定：live 帧不高亮，终态 / 历史 / 预览高亮**

| 选项 | 做法 | 本期 |
|------|------|------|
| **A（选定）** | `flushLivePaint` **永不**带 highlight。新增 chatView 本地 `renderFinal(step)`，只对 `step.live.contentEl` 再 render 一次 `{ breaks:true, highlight:true }`。历史 / 预览直接 highlight | ✅ |
| B | 每帧 hljs | ❌ 长代码块比 marked 更卡，打脸 latency 方案 |
| C | 只高亮最后一个 code | ❌ 复杂，收益小 |
| D | 给 `flushLivePaint` 加 highlight flag | ❌ flush 还被 step 切换、goIdle、collapse 复用，中间帧会被误高亮 |

**`renderFinal(step)` 契约：**

- 前置：调用方已 `flushLivePaint` / `flushAndCollapseThinking`（buffer 已在 DOM 且 `textBuf` 完整）。
- 动作：`window.renderMarkdown(step.live.contentEl, step.textBuf, { breaks:true, highlight:true })`。
- **只碰 `contentEl`**。不重渲 reasoning，不碰 `bodyEl` 上的 thinking / 工具卡 / retry notice / interrupted badge / inline error。
- `!step || !step.live || !step.live.contentEl` 或 `!step.textBuf` 则 return。

**挂载点（仅此四处，多挂会双渲染，少挂会终态无高亮）：**

| # | 代码位置 | 插在哪之后 | 不插的理由（对照） |
|---|----------|------------|-------------------|
| 1 | `case 'completed'` | `flushAndCollapseThinking` 之后、`goIdle()` 之前 | — |
| 2 | `handleStreamError` 的 `stopped` 分支 | `settleRunningCardsOnStop()` 之后、`markInterrupted()` 之前或之后均可（badge 不在 contentEl） | 跨窗口停止；走不到 `stop()` |
| 3 | `handleStreamError` 的「其它 errorType」内联分支 | `appendInlineErrorBlock(...)` **之后**（error 块在 bodyEl，重渲 contentEl 不会丢它） | `emptyMessage` / 无 step 走顶部 errorBar，无 contentEl |
| 4 | `stop()` | `flushLivePaint` 之后、`markInterrupted` 之前或之后均可 | 本窗口点停止；abort 后收不到后端 stopped，走不到 #2 |

**明确不挂：**

- `confirmationRequired`：流未结束，后续 textDelta 会整段 innerHTML 覆盖，白做。
- `goIdle`：双保险只 flush 文本；已完成 step 再高亮会二次 hljs。
- `beginNewStepIfNeeded` 的旧 step flush：中间边界，不是终态。
- `onStreamClosed` / `pendingConfirmationExists` / `confirmationMismatch`：前者 stream 可能已空，后两者无半截正文要高亮或会整页重载。

历史 `appendAssistantHistory` 只走一次，直接 `highlight:true`。
文件预览渲染模式一次 `highlight:true`。源码模式继续 `buildCodeView`，不经过 `renderMarkdown`。

### D5. `breaks` — **选定：聊天 true，预览 false**

| 场景 | breaks | 原因 |
|------|--------|------|
| 聊天 live / 历史 | `true` | 模型常单换行分段；关掉会黏成一坨。这是现网行为，改了等于改对话排版 |
| 文件预览 `.md` | `false` | 真实文件按 GFM 空行分段；全局 setOptions 让预览误吃 true 是 bug |

公共函数按调用方传入，不再 set 全局。

---

## 3. 架构（改动后）

```text
                    ┌─────────────────────────────┐
                    │  js/markdown.js             │
                    │  renderMarkdown(el, text,   │
                    │    {breaks, highlight})     │
                    │  marked → DOMPurify → DOM   │
                    │  optional hljs on pre code  │
                    └────────────▲────────────────┘
                                 │
            ┌────────────────────┼─────────────────────┐
            │                    │                     │
   chatView live          chatView 历史            fileExplorer
   flushLivePaint         appendAssistantHistory   buildMarkdownView
   highlight:false        highlight:true           highlight:true
   breaks:true            breaks:true              breaks:false
   renderFinal ×4 处
   highlight:true
            │
            │  scheduleLivePaint / flush 清单
            │  仍只属于 chatView
```

脚本顺序：`marked` → `DOMPurify` → `highlight` → **`markdown.js`** → `fileExplorer.js` → `chatView.js`。

---

## 4. 文件改动

| 文件 | 动作 |
|------|------|
| `webApp/frontend/js/markdown.js` | **新建**。文件头 Author/Version/Date/Description。IIFE，挂 `window.renderMarkdown` |
| `webApp/frontend/index.html` | script 加 `markdown.js`（在 chatView / fileExplorer 之前）；**不加**任何新 CSS link |
| `webApp/frontend/js/chatView.js` | 删 `setOptions` 与本地 `renderMarkdown`；live/历史改调 `window.renderMarkdown`；新增 `renderFinal`，按 D4 四处挂载 |
| `webApp/frontend/js/fileExplorer.js` | `buildMarkdownView` 改调公共函数 |
| `webApp/frontend/styles.css` | 按 D2=B 补标题/段落/列表/链接/图片/hr；保留现有 pre/table/blockquote |
| `docs/plan/markdownRenderUnifyPlan.md` | 本文 |

不改后端、不改 SSE 契约、不改 thinking / 工具卡。

---

## 5. 风险与回滚

| 风险 | 缓解 |
|------|------|
| 手写 CSS 漏元素（任务列表 / 嵌套列表） | D2 覆盖常用 GFM；漏了再补，不引入整套主题 |
| 每帧 highlight 卡死 | D4：`flushLivePaint` 永不 highlight；只 `renderFinal` ×4 |
| 给 flushLivePaint 加 flag 误高亮中间帧 | D4 选项 D 已否决 |
| 全局 setOptions 删除后有第三处依赖 | 全仓仅 chatView 一处；删完 grep `setOptions` 为空 |
| hljs 语言包不全（highlight.min.js 是精简包） | 与预览源码同一包；未注册语言跳过。不在本期扩语言包 |
| `.hljs` 背景叠在围栏 pre 上 | `.markdown-content pre code.hljs { background: transparent }` |
| 消毒吃掉 hljs class | 不走 highlightElement；highlight 输出再 sanitize（现网 DOMPurify 默认保留 class） |

回滚：

1. 删 `markdown.js`。
2. chatView / fileExplorer 改回各自复制函数；**必须**恢复 chatView 顶部 `setOptions({ gfm, breaks:true })`，或在复制函数里 per-call 传 `breaks`。
3. `styles.css` 新增的标题/列表规则撤回。

---

## 6. 分层验收

| # | 场景 | 期望 |
|---|------|------|
| A | 历史消息含标题/列表/表格/引用/行内 code/围栏 js | 有层级与间距；围栏有高亮 |
| B | 流式长正文（≥2k） | 可滚动、不假死；每帧不卡死；停/完成后无缺尾 |
| C | 流式半截 \`\`\`js 再补全 | 中途可乱，结束后与刷新历史一致 |
| D | 点开会话目录里的 README.md | 渲染模式：GFM 排版 + 代码高亮；切源码：行号 + 按扩展名高亮（旧行为） |
| E | 同一段 md 里写 `<script>alert(1)</script>` 与 `[x](javascript:alert(1))` | 不弹窗、不跑脚本 |
| F | thinking / 工具卡 running / 确认框 / stop 定格 | 与改前一致 |
| G | 窄聊天栏 + 预览大弹层 | 无横向撑破；含大图的 `.md` 预览里 img 不撑破 `.file-preview-body`（靠手写 `img{max-width:100%}`） |
| H | streaming 中刷新页面触发 attach 回放 | 中间态允许前半高亮后半不高亮；终态后整条消息高亮一致，thinking / 工具卡还在 |
| I | completed 一次后看 contentEl | 围栏 code 只有一层 `hljs-*` span，无二次高亮套娃 |

---

## 7. TODOlist

### Phase 0 — 拍板（实施前）

- [x] T0.1 确认 D1–D5：D2=**B** 手写 token 风（2026-08-17 用户拍板）
- [x] T0.2 确认聊天继续 `breaks:true`；预览 `false`

### Phase 1 — 公共渲染函数

> 注意：本阶段**不是**观感零变化。D5 一生效，预览 `.md` 单换行不再变 `<br>`（修 bug）。聊天 breaks 仍为 true，对话排版应与改前一致。

- [x] T1.1 新建 `js/markdown.js`：`renderMarkdown(el, text, {breaks, highlight})`，实现按 D1（含缺 marked 降级 textContent、hljs 唯一路径）
- [x] T1.2 `index.html` 接入：`marked` → `DOMPurify` → `highlight` → `markdown.js` → `fileExplorer.js` → `chatView.js`
- [x] T1.3 `chatView.js` 删除 `setOptions` + 本地函数；`flushLivePaint` 调 `{ highlight:false, breaks:true }`；`appendAssistantHistory` 调 `{ highlight:true, breaks:true }`；根节点保持 `markdown-content`
- [x] T1.4 新增 `renderFinal(step)`，按 D4 挂四处（completed / stopped 分支 / 其它 error 内联之后 / stop()）。confirmationRequired 与 goIdle **不挂**
- [x] T1.5 `fileExplorer.js` `buildMarkdownView` 改调公共函数，`{ breaks:false, highlight:true }`；源码模式不动
- [x] T1.6 grep：`marked.setOptions` 为空；`marked.parse` 仅 `markdown.js` 一处；`hljs.highlight` 两处（`markdown.js` + `fileExplorer.highlightCode`）属预期

**验证：** 聊天排版与改前一致；预览单换行按空行分段（预期变更）；S4 XSS；S8 可抽 marked script 手测纯文本；验收 I（completed 无二次 hljs 套娃）。

### Phase 2 — 样式（D2=B）

- [x] T2.1 **跳过** vendor github-markdown-css（用户选 B）
- [x] T2.2 **跳过** `markdown-body` class
- [x] T2.3 按 D2=B 代码块写入标题/段落/列表/链接/图片/hr，以及 `pre code.hljs` 透明底
- [x] T2.4 **不删** 原 pre/table/blockquote；全局 `code` 不动
- [ ] T2.5 手测验收 A / D / G（G 含：含大图的 `.md` 预览 img 不撑破 `.file-preview-body`）

### Phase 3 — 流式与回归

- [ ] T3.1 长正文流式：S2 / 验收 B
- [ ] T3.2 半截 fence：验收 C
- [ ] T3.3 thinking / 工具卡 / 确认 / stop 定格：验收 F
- [ ] T3.4 预览源码模式行号 + 按扩展名高亮仍在
- [ ] T3.5 attach 回放：验收 H

---

## 8. 明确不在本期写进代码的东西

1. 不把「调研里提到的 streaming-markdown」先引进来「以后再用」。
2. 不把 highlight 语言包扩到全量 hljs（119KB 已是精简包）。
3. 不为一次性预览去抽象 `buildCodeView`。

---

## 9. 拍板记录

- **D2 = B**（2026-08-17 用户）：手写浅色 token，不引 github-markdown-css。
- **D5**：聊天 `breaks:true`，预览 `false`。
