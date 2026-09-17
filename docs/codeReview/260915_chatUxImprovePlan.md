'''
Author: wilbur
Version: 1.0
Date: 2026-09-15
Description: chatUxImprovePlan v1.0 独立审核（pi -p --model xai/grok-4.6，只读 read/bash，无 session）。无 P0；P1 两条须先改方案原文。
'''

# chatUxImprovePlan 方案审核

审核对象：docs/plan/chatUxImprovePlan.md v1.0  
核对代码：`webApp/frontend/js/markdown.js` v1.0；`chatView.js`（`flushLivePaint`/`renderFinal`/`renderHistory`/`appendAssistantHistory`/`appendInlineErrorBlock`/`handleStreamError`/`send`/`userBubbleText`/`reloadSession`/`initAttachedStream`/`composerInput` keydown）；`webApp/backend/historyView.py` v1.4；`flamingoAgents/core/agent.py` `driveModelLoop` except + `logModelError`；`docs/webApiSpec.md` §2.2；`docs/plan/modelUxImprovePlan.md` D5；`slashCommand.js`/`fileMention.js` capture keydown；`index.html`；`styles.css`（`.msg-body` / `.msg-error-block` / `.markdown-content pre`）；`tests/testLiveUsageFrontend.py` `testIndexCacheBustOrder`、`tests/testToolCardCollapse.py` cache-bust；`fileExplorer.js` `buildMarkdownView`  
审核方式：独立 Terminal `pi -p --no-session --no-context-files --thinking low --model xai/grok-4.6 -t read,bash`  
审核模型：xai/grok-4.6

## 审核结论

四项需求与 HEAD 流水线总体对齐：mermaid/复制应绑 `opts.highlight`；错误数据已在 jsonl，卡在 `historyView.loadMessages` 与 M2；↑ 与斜杠/@ capture 可叠加。无 P0。必须先改方案原文中的 **召回栈口径（send vs 历史灌入、attach 二次 `renderHistory`）** 两条 P1，否则按 TODO 实施会出现刷新前后 ↑ 内容不一致、以及 attach 截断把栈冲掉。

**无 P0。**

## 必须修（P0/P1）

### P1-1 ↑ 栈：`send()` 推 `userText` 与历史灌入 `userBubbleText` 不同构，且与 §0.4「纯 skill 不入栈」打架

位置：方案 D4 规则 5–6、§0.4；代码 `chatView.js` `send()`（`userText` / `displayText` / `wireText`）、`userBubbleText`（约 386–390 行）、`reloadSession` → `renderHistory`。

问题：

- 带 skill 发送时，气泡是 `displayText = '/skill:' + name + (userText ? '\n' + userText : '')`，jsonl `content` 是含 `<injected_skill>` 的 `wireText`。`userBubbleText` 把历史折成 `'/skill:' + name`（无补充）或 `'/skill:' + name + '\n' + 补充`。
- 规则 5 只 `push userText`：当场 ↑ 得到纯补充字；刷新后规则 6 灌入带 `/skill:` 前缀的串。
- 纯 skill、补充为空：规则 5 因 `userText` 空不入栈（符合 §0.4）；规则 6 会对 `userBubbleText` 得到的 `'/skill:xxx'` **重置进栈**（`INJECTED_SKILL_RE` 无 group 2 仍返回 `'/skill:' + name`），刷新后又能 ↑ 出 skill 名。

怎么改方案原文：

- 规则 5 改为：非 retry 时 push **与气泡一致**的文字：无 chip → `userText`；有 chip → `displayText`。`userText` 与 chip 都空（纯附件）不入栈。
- 规则 6 改为：`userBubbleText(content)` 后再丢掉「仅 `/skill:名`、无补充」以及折完仍为空的项，与 §0.4 一致。
- 写明：召回栈存的是气泡可见用户原文，不是 `wireText`。

### P1-2 `renderHistory`「结束即重置栈」会在 attach 二次渲染时丢掉后半段用户句

位置：方案 D4 规则 6；代码 `reloadSession` 先对**完整** `messages` 调 `renderHistory`，随后 `initAttachedStream` 再对 `messages.slice(0, baseCount)` 调一次 `renderHistory`。

问题：若按「每次 `renderHistory` 结束用本批 `kind:user` **重置**该 sessionId 栈」，乐观 attach 的第二次调用只含 `baseCount` 之前的 user，会把第一次灌入的后续用户原文清掉。会话仍在流、刷新进 attach 时，↑ 只能召回水位线前的句子。

怎么改方案原文：

- 只在 `reloadSession` 拿到完整 GET `messages` 后灌入/重置栈。
- `initAttachedStream` 内截断用的 `renderHistory` **不得**重置 `inputHistory`。
- 或：灌入逻辑抽成 `rebuildInputHistory(messages)`，仅 `reloadSession` 调用，不放进 `renderHistory` 本体。

## 建议（P2）

### P2-1 mermaid 失败回源码后未规定补回代码复制按钮

位置：D1/D2；`markdown.js` `highlightFences` 只在 `opts.highlight` 时跑一遍 `pre code`。

失败路径「占位改回 `pre`」发生在复制按钮注入之后，该围栏会既无图也无复制。方案应写：回源码后对该 `pre` 再走一次代码复制按钮（仍跳过成功出图的节点）。

### P2-2 `testMarkdownEnhance.py` 未写清 node harness

`markdown.js` 依赖 `window.marked` / `DOMPurify` / 可选 `hljs`。实施者若只 `vm` 加载单文件会红。应注明加载 `vendor/marked.min.js`、`dompurify.min.js`（及假 DOM）。

### P2-3 cache-bust 未覆盖 `markdown.js`

T9 给 `markdown.js?v=1.1`；`testLiveUsageFrontend.py` `testIndexCacheBustOrder` 与 `testToolCardCollapse.py` 只断言 `styles.css?v=1.22`、`chatView.js?v=1.23`。T11 同步这两处即可不红，但 markdown 漏戳不会被测到。建议 T11 增加 `src="/static/js/markdown.js?v=1.1"` 断言。

### P2-4 历史旧 `<attachment>` 全文会进召回栈

`userBubbleText` 只折 skill，不折 `ATTACHMENT_RE`。v1.3 历史 user `content` 可能含文件全文。§0.4/规则 6 应写：灌入前对 attachment 块剥成空或只留块外用户字，与「不召回附件 chip」一致。

### P2-5 空助手壳 + 红块不要挂文字复制

`renderFinal` 在 `!step.textBuf` 时直接 return（首 round 失败空气泡无复制，合理）。历史「新建壳 + 红块」若走 `appendAssistantHistory`/`T5` 可能给空 `msg.content` 挂复制。应写：原文为空不插 `.msg-copy-btn`。

### P2-6 DOMPurify 与 mermaid SVG

现网是 `DOMPurify.sanitize(html)` 无 profile。方案已要求 SVG 再消毒，建议补一句：沿用默认 HTML+SVG，禁止 `KEEP_CONTENT` 把脚本文本漏出；`foreignObject` 若被剥导致图残缺则只记已知限制，不要关 `strict`。

## 方案已正确、实施时可直接采用

- **markdown 流水线**：`marked.parse({gfm, breaks})` → `DOMPurify` → `innerHTML` → 仅 `opts.highlight` 时 `highlightFences`；未知语言（含 mermaid）`hljs.getLanguage` 为假则跳过。与 §0.1 一致。
- **四处调用**：`flushLivePaint` `highlight:false`；`renderFinal` / `appendAssistantHistory` / `buildMarkdownView` `highlight:true`。绑 highlight、live 不出图正确。`fileExplorer.js` 可不改。
- **`index.html` 无 mermaid 启动 script**；`markdown.js` 现无 query；`chatView.js?v=1.23`、`styles.css?v=1.22` 与 T9 升戳方向一致。
- **复制挂点**：`.msg-body`、`.markdown-content pre` 确无按钮、pre 无 `position:relative`。`renderFinal` 只重写 `contentEl`，文字按钮挂 `bodyEl` 不会被冲掉。
- **LAN Clipboard**：方案 `execCommand` 降级必要。
- **错误链**：`logModelError` 已写 jsonl（`attempt+1`、`willRetry`、`message=str(error)`）；终态 SSE 为 `模型调用失败（已重试{attempt}次）：{error}`（attempt 0-based）。`loadMessages` 对 `modelError` 无分支、注释写明 M2 不下发。D5「错误块不落盘」确需推翻。DTO 白名单、不改 `agent.py` 正确。
- **live 挂载**：`handleStreamError` 非 `stopped`/`pendingConfirmationExists`/`confirmationMismatch`/`emptyMessage` 且有 `currentStep.live.bodyEl` → `appendInlineErrorBlock`。`send()` 在 POST 前 `createStep()` 预建空壳，与「首 round 失败空气泡」同构。
- **`renderHistory` 只处理 user/assistant**，`kind:tool` 配对不渲染、也不更新任何 lastKind；方案要求显式 `lastKind='tool'` 必要。旧客户端忽略未知 `kind:error` 成立。
- **↑ 与面板**：`slashCommand.js`/`fileMention.js` 均为 **capture + `stopImmediatePropagation`**，面板打开且非 IME 时吃掉 ArrowUp/Down；`chatView` 现 bubble 只处理 Enter。召回放同一 bubble 监听可行。
- **IME**：两面板已 `isComposing || keyCode===229` 放行；召回必须同样放行。
- **非目标**：`stopped`/`emptyMessage`/`imageInputError` 不走 `logModelError`，与代码一致。
