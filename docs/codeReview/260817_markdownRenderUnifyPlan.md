Author: wilbur
Version: 1.0
Date: 2026-08-17
Description: markdownRenderUnifyPlan 方案审核。

# markdownRenderUnifyPlan 方案审核

审核对象：`docs/plan/markdownRenderUnifyPlan.md`（v1.0，未实施）。
核对代码：`webApp/frontend/js/chatView.js`（v1.16）、`webApp/frontend/js/fileExplorer.js`（v1.1）、`webApp/frontend/styles.css`（v1.14）、`webApp/frontend/index.html`、`docs/plan/streamingLatencyFixPlan.md` D4、`docs/plan/webAppPlan.md` §4.6。

---

## 审核结论

**可以按这个方案实施，但必须先修 2 个 P0。** 方案对现状的事实描述（全局 setOptions 副作用、breaks 被预览误吃、.markdown-content 只有 pre/code/table/blockquote、全局 `code` 选择器、脚本顺序）经逐行核对**全部属实**，D1–D5 的取舍方向正确、没有过度设计。最大风险集中在两点：① D4「封口后再 render 一次 highlight」的挂载点在真实代码里比方案写的复杂——completed/error/stopped 三条终态路径 goIdle 时机不同，错误处理还会往 contentEl 父节点追加块，方案没给精确定位，照 TODOlist T1.4 实施极易漏挂或双渲染；② github-markdown-css 自带的 `code`/`pre` 高特异性规则会覆盖现有全局 `code` 与 `.markdown-content pre code` 的降级样式，方案的 CSS 收缩清单不完整，直接接主题会把聊天内联 code 和围栏代码的样式打乱。这两个不解决，Phase 1 合入即回归。

---

## 必须修（P0/P1）

### P0-1 D4「封口补一次 highlight」的挂载点描述与真实终态路径不符，T1.4 不可执行

**位置**：方案 §2 D4、§7 T1.4；代码 `chatView.js` onStreamEvent（completed L802-811 / error L813-820 / confirmationRequired L783-800）、handleStreamError（L824-864）、stop（L1081-1096）、onStreamClosed（L921-940）、goIdle（L965-973）。

**问题**：方案写「completed/error/stopped 的 flush 之后补一次 highlight:true」，并建议「或 flushLivePaint 接受 flag」。但真实代码里：

1. 三条终态的 flush 入口是 `flushAndCollapseThinking`（completed/error/confirmationRequired）和裸 `flushLivePaint`（stop 进 stopping 前 L1086），**不是同一个函数**；给 `flushLivePaint` 加 flag 会波及全部 5 处强制 flush 点（含 step 切换 L506、goIdle L967），把不该高亮的中间帧也拉高亮，违背 D4 自己「live 帧不高亮」。
2. `stopped` 不经过 completed/error 分支：本窗口 stop 走 `stop()` → abort → onStreamClosed；跨窗口 stopped 走 `handleStreamError` stopped 分支（L829-833）。方案没点名这两个入口，实施者会漏。
3. `handleStreamError` 的非 stopped 分支会调 `appendInlineErrorBlock(stream.currentStep.live.bodyEl, ...)`（L862）——往 **contentEl 的父节点**追加 DOM。若 highlight 重渲染在 inline error 之前执行，时序虽不影响（渲染只改 contentEl 的 innerHTML），但方案没说明 highlight 重渲染只碰 `contentEl`、不碰 bodyEl 里已追加的 error block / retry notice / interrupted badge，实施者容易误把整个 bodyEl 重渲导致这些块丢失。
4. confirmationRequired 也是终态入口且会 flush，但流并未结束（confirm 后续流进同一 step），此时补 highlight 后还会被后续 textDelta 的全量 innerHTML 整替换覆盖，属浪费；方案没排除它。

**怎么改方案原文**：D4 的「做法」列改为——

> 新增 `renderFinal(step)`：对已 flush 的 `step.live.contentEl` 调一次 `window.renderMarkdown(contentEl, step.textBuf, { breaks:true, highlight:true })`（不重渲 reasoning，不碰 bodyEl 其余子节点）。挂载点四处：`completed` 分支 flushAndCollapseThinking 之后、`handleStreamError` stopped 分支 settleRunningCardsOnStop 之后、非 stopped 错误分支 appendInlineErrorBlock 之后、`stop()` 的 flushLivePaint 之后。confirmationRequired 不挂（流未终结）。goIdle 双保险不挂（避免对已完成 step 二次高亮）。

T1.4 同步改为「按 D4 四处挂载点补 renderFinal」，并加一条验证「连续 completed 后 highlight 只执行一次（contentEl 无重复 hljs span）」。

---

### P0-2 github-markdown-css 接入的 CSS 冲突清单不完整，会把现有 code 样式打乱

**位置**：方案 §2 D2、§5 风险表；代码 `styles.css` L23（`*{margin:0;padding:0}`）、L45（全局 `code{background:var(--gray-block);padding:1px 5px;border-radius:4px;font-size:12px}`）、L270-279（.markdown-content 规则）、L276（`.markdown-content pre code{background:none;padding:0}`）。

**问题**：方案只笼统说「收缩 .markdown-content 重复规则」「覆盖全局 code 为 .markdown-body code」，漏了三个真实坑：

1. **github-markdown-css 的 `.markdown-body code` 特异性（0,2,0）高于现有全局 `code`（0,0,1）和 `.markdown-content code`（0,1,1）**。接入主题后，聊天里的行内 code 会被主题强制成 `background:rgba(175,184,193,.2); padding:.2em .4em; font-size:85%`，与产品 token（--gray-block、12px）不一致；方案没列这条覆盖。
2. **主题的 `.markdown-body pre` 带 `background:#f6f8fa; padding:16px; border-radius:6px`**，与现有 `.markdown-content pre`（--gray-block + border + 8px radius）双背景双 padding 叠加——方案风险表提到了「叠两层背景」但 D2 落地清单只写了 padding/font-size/max-width/background 四条容器级覆盖，**没写 pre/code 级别的收缩规则**，T2.4「拆除与主题冲突的旧规则」没有给出具体删哪几条、留哪几条。
3. **hljs 主题背景与 markdown-body pre 背景叠加**：现有 `highlight-theme.min.css` 给 `.hljs` 设背景。主题的 pre 背景 + `.hljs` 背景 + `.markdown-content pre code{background:none}` 三者叠在一起，若 `.markdown-content pre code` 被删而主题 `.markdown-body pre code` 未显式 `background:transparent`，会出现围栏代码块内 code 区域一块深一块浅。方案提了「背景只留一层」但没给选择器写法。

**怎么改方案原文**：D2 落地清单的「本地覆盖」代码块后追加——

```css
/* 主题 pre/code 与产品 token 对齐，且压掉 hljs/旧规则的双背景 */
.markdown-body pre { background: var(--gray-block); border: 1px solid var(--border); border-radius: 8px; padding: 12px; }
.markdown-body pre code.hljs { background: transparent; padding: 0; }
.markdown-body code { background: var(--gray-block); padding: 1px 5px; border-radius: 4px; font-size: 12px; }
.markdown-body pre code { background: transparent; padding: 0; font-size: 13px; }
```

并把 T2.4 改为可执行清单：「删除 styles.css L271-279 中 `.markdown-content pre/code/table/blockquote/th/td` 六条（table/blockquote 由主题接管）；保留 L270 `:first-child`；删除前确认 `.preview-markdown` 内表格边框、引用竖条来自主题而非旧规则」。

---

### P1-3 公共函数在 marked 缺失时的降级行为与现网不一致，且方案自相矛盾

**位置**：方案 §2 D1「实现要点」；代码 `chatView.js` L82-85。

**问题**：现网 renderMarkdown 在 marked 缺失时 `html=''`，经 purify 后 innerHTML 为空串——**内容丢失**。方案 D1 实现要点写「缺 marked 时降级 `el.textContent = text`，不抛」，这是更好的行为，但与 §0.1/§4 描述的「流水线字面相同」矛盾，且**方案没声明这是一次行为变更**。若实施者照抄现网逻辑（求「一致」）则保留丢内容 bug；照实现要点则改变了行为但未在成功标准里登记。另外 fileExplorer 的 buildMarkdownView（L196-201）marked 缺失时同样 innerHTML 空串，统一到 textContent 降级后预览行为也变了，方案没说。

**怎么改方案原文**：D1 实现要点该条改为——

> 缺 marked 时降级 `el.textContent = text`（**行为变更**：现网两处均为 innerHTML 空串丢内容，此处一并修复，登记为附带 bug fix）。

并在 §1.3 成功标准加一条 S8：「marked 未加载时聊天与预览均显示纯文本而非空白」。

---

### P1-4 hljs 高亮的消毒顺序，方案给了两个相互冲突的做法，且其中之一有安全隐患

**位置**：方案 §2 D1 实现要点第 4 条；代码 `fileExplorer.js` L159-163（highlightCode：hljs.highlight 输出先 DOMPurify 再 innerHTML）。

**问题**：方案写「高亮结果再过一次 DOMPurify **或** 只改 code 的 class + 已消毒 HTML（hljs 输出本身是转义后的 span，仍建议 sanitize 或 highlightElement 前确认 text 来自已消毒 DOM）」。这句给了三条岔路：

- 做法 a「highlight 后再 sanitize」：会把 hljs 注入的 `<span class="hljs-*">` 的 class 属性是否保留交给 DOMPurify 默认配置——现网 DOMPurify 默认保留 class，方案风险表也承认这点，但没拍板，实施者仍要自己试。
- 做法 b「highlightElement 改已消毒 DOM」：highlightElement 读取的是 code 的 textContent 再重写 innerHTML，输入来自已消毒 DOM 看似安全，但 hljs 对某些语言会把原始文本里的字符实体二次转义/重组，社区有围绕 highlightElement 的 XSS 讨论；更重要的是——**它与现网 fileExplorer 已验证的「hljs.highlight → sanitize → innerHTML」模式不一致**，等于在公共函数里引入第二条高亮路径，增加审核面。
- 方案没拍板用哪个，留下歧义。

**怎么改方案原文**：D1 实现要点第 4 条改为单一做法——

> highlight 路径与 fileExplorer 现网一致：对每个 `pre code`，取 `code.textContent`，`hljs.highlight(text, {language})`（getLanguage 失败跳过），输出 `.value` 经 `DOMPurify.sanitize` 后赋回 `code.innerHTML`，并给 code 加 `hljs` class。禁止用 highlightElement（与现网消毒顺序不一致，避免引入未验证路径）。

---

### P1-5 调用点排查遗漏：fileExplorer 的 highlightCode / buildCodeView 与全局 setOptions 删除后的验证项

**位置**：方案 §5 风险表、§7 T1.6；代码 grep 结果。

**问题**：grep 全仓确认 `marked.parse` 仅两处（chatView L83、fileExplorer L199）、`setOptions` 仅 chatView L75-76 一处，方案「全仓仅 chatView 一处 setOptions」**属实**。但方案漏了两个相关调用点事实：

1. fileExplorer 的 `highlightCode`（L159）也调 `hljs.highlight` + DOMPurify——这是源码模式路径，方案声明不进渲染层（正确），但 T1.6 的 grep 验证项「无第二处裸 marked.parse」没包含「hljs.highlight 仍有两处（公共函数 + fileExplorer 源码模式），属预期，不算残留」的说明，实施者 grep 到两处 hljs 会误判。
2. 方案说「删完 grep setOptions 为空」，但删除 chatView 的全局 setOptions 后，**预览的 breaks 从 true 变 false 是行为变更**（方案 D5 已拍板，正确），然而历史聊天消息的排版不变（公共函数传 breaks:true）——这点方案写清楚了；没写清楚的是：**在 Phase 1 合入后、Phase 2 接入主题前**，预览排版会因 breaks:false 立刻变化（单换行不再变 `<br>`），这属于 Phase 1 的可见行为变更，方案 §7 Phase 1 验证写「此阶段允许观感仍旧」**不成立**——观感（预览换行）会变。

**怎么改方案原文**：T1.6 改为——

> grep 验证：`marked.setOptions` 为空；`marked.parse` 仅 markdown.js 一处；`hljs.highlight` 两处（markdown.js 公共高亮 + fileExplorer highlightCode 源码模式）属预期。Phase 1 合入后预览 .md 的单换行排版立即变化（breaks:false 生效），此为预期行为变更，验证时确认真实 .md 文件段落按空行分隔正确。

---

### P1-6 方案未提及 attach 重连路径的渲染一致性

**位置**：方案 §2 D1；代码 `chatView.js` attachStream（L1122）、initAttachedStream（L1162）、buildLiveFromHistory（L902）。

**问题**：reloadSession 乐观 attach 成功后会「按 baseCount 截断重渲染 + 回放续播」（initAttachedStream 调 renderHistory 重建历史，再把流事件回放进 live 块）。这意味着：历史部分走 `appendAssistantHistory`（highlight:true），续播的 live 部分走 flushLivePaint（highlight:false），**同一条消息在 attach 场景下前半高亮后半不高亮**，直到终态 renderFinal 才统一。方案的 S1「同一输入 HTML 结构一致」在 attach 中间态不成立。虽然终态会收敛，但方案完全没提 attach 这条路径，实施者测试时会发现中间态不一致而无所适从。

**怎么改方案原文**：§1.3 S1 加注释——

> attach 重连中间态允许历史部分已高亮、live 续播部分未高亮，终态（completed/error/stopped）后收敛一致。

并在 §6 验收表加一行「H：streaming 中断网刷新页面触发 attach 回放，终态后整条消息高亮一致」。

---

## 建议修（P2）

### P2-1 D3「现网已为每 token 全量 parse 付过成本」表述不准确

方案 §2 D3 理由第 1 条写「现网已经为『每 token 全量 parse』付过一次成本」。真实情况是：v1.7 之前是每 token 同步 parse（卡主线程，正是 latencyFix 要修的问题），v1.7 后已是 rAF 合并每帧一次。**当前并不存在「每 token 全量 parse」**，方案想表达的是「全量 parse 的成本已被 rAF 压到可接受」。建议改为：「v1.7 前每 token 同步 parse 已证明不可行；rAF 合并后每帧一次全文 parse 在当前消息长度下可接受（D4 成功标准：≥2k 字可滚动）」。纯措辞修正，不影响决策。

### P2-2 预览容器 `.preview-markdown` 现有 padding 14px 与主题 padding 的关系没写

styles.css L475 `.preview-markdown { padding: 14px 18px }`。github-markdown-css 的 `.markdown-body` 通常带 `padding: 45px`（页级）。方案 D2 覆盖里写了 `.preview-markdown.markdown-body { ... }` 但没写 padding——若主题 padding 生效会把预览弹层内容挤得很小，若保留 L475 则与主题 padding 叠加（padding 不叠加，后者覆盖前者，取决于特异性与顺序）。建议在 D2 覆盖块补一句：`.preview-markdown.markdown-body { padding: 14px 18px; }`（显式保留预览自有 padding，压掉主题页级 padding）。

### P2-3 「img{max-width:100%} 主题一般自带，确认」应改为确定性验证项

§6 验收 G 写「图片不过度撑宽（img{max-width:100%} 主题一般自带，确认）」。github-markdown-css 确实带 `img{max-width:100%}`，但全局 reset 不影响 img 的 max-width，此处风险低；不过「确认」是口头语，应改成 T2.5 的勾选项：「在含大图的 .md 预览中确认 img 不撑破 .file-preview-body」。避免验收时遗漏。

### P2-4 缺 marked 版本锁定与 CSP 说明

方案未提 vendor 的 github-markdown-css 具体版本号（§4 写「建议 5.8.x / 5.9.x」，未拍板锁定）。建议 T2.1 改为「锁定具体版本号并记录在 vendor 文件头注释」，与 marked v12.0.2 的 vendor 先例一致。另外若未来 index.html 加 CSP，`style-src` 需允许内联样式（hljs 主题与 github-markdown-css 均为外链，无碍），本期无 CSP 可仅备注。

### P2-5 回滚方案不完整

§5 回滚写「删 markdown.js + vendor css + 三处调用改回复制函数；CSS 覆盖撤回」。但 Phase 1 已删除 chatView 的全局 setOptions，回滚时若只「改回复制函数」而不恢复 setOptions，聊天 breaks 会失效（复制函数若不带 per-call options）。建议回滚步骤补：「恢复 chatView 顶部 setOptions 或在复制函数内 per-call 传 breaks:true」。

---

## 同意保留的决策

- **D1（只抽同步渲染函数，不抽流式调度）——同意。** 流式调度（scheduleLivePaint/flushLivePaint/step 状态机）与 chatView 的 SSE 状态机深度耦合，抽出去对预览无任何收益，方案判断正确。API 形态 `renderMarkdown(el, text, {breaks, highlight})` 合理，但须按 P0-1/P1-3/P1-4 修正实现要点。
- **D2（vendor github-markdown-css light + 本地覆盖）——方向同意，清单须补。** 现成 GFM 观感确实比手写 80 行省事且与 hljs GitHub 主题同宗，但必须按 P0-2 补全 pre/code 级别的收缩规则，否则接入即回归。
- **D3（继续全文 marked，不换增量库）——同意。** streaming-markdown 的 GFM 覆盖与历史回放一致性风险真实存在，且 D4 的 rAF 合并已把性能压到可接受，不换库是对的。措辞按 P2-1 修正。
- **D4（live 帧不高亮，终态/历史/预览高亮）——方向同意，挂载点必须按 P0-1 重写。** 每帧 hljs 确实会比 marked 更卡，方向对；但「封口后再 render 一次」的落点在真实代码里是四个分散位置，方案现写法不可执行。
- **D5（聊天 breaks:true，预览 breaks:false）——同意。** 切断全局 setOptions 副作用、预览按真实 GFM 空行分段，是修 bug 性质的正确变更；但须按 P1-5 把「Phase 1 即产生预览排版变化」写进验证项，避免误判为回归。

---

## 修复优先级建议（Top 3）

1. **P0-1**：先改 D4 挂载点描述——这是流式路径，改错直接破坏 D4 契约（丢尾/双渲染/inline error 丢失），且 Phase 1 就要动。
2. **P0-2**：补全 CSS 收缩清单——这是 Phase 2 的核心工作量，清单不全等于把样式调试留给实施者现场发挥，与「方案待拍板后照做」的定位矛盾。
3. **P1-4**：拍板 hljs 消毒顺序为 fileExplorer 现网模式——歧义路径会让公共函数引入第二条未验证的高亮链路，XSS 红线相关必须唯一。
