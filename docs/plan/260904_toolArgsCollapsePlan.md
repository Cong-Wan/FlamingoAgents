# 工具卡片入参折叠与高度调整方案（toolArgsCollapsePlan）

> 日期：2026-09-04 ｜ 状态：已实施并通过用户视觉验收
> 需求来源：用户反馈「工具调用入参不折叠、不能上下滑动，看着不舒服；入/出参最大显示高度需调大」

## 1. 现状调研

### 1.1 涉及代码

| 文件 | 位置 | 现状 |
|---|---|---|
| `webApp/frontend/js/chatView.js` | `buildToolCard()` L176-246 | 入参 `argsPre` 在 L223 附近直接 `textContent = safeJson(...)`，**无任何折叠**；出参 `resultPre` 初始 class 带 `collapsed` |
| `webApp/frontend/js/chatView.js` | `setCollapsibleText()` L156-174 | 折叠逻辑：**按字符数/行数判断**（>300 字符或 >8 行）加 `collapsed` class + 追加「展开全部/收起」按钮；按钮追加在 `containerEl`（即 `tool-section`）末尾 |
| `webApp/frontend/styles.css` | `.tool-pre` L337-341 | 入参/出参共用基础样式（灰底、等宽字体、pre-wrap） |
| `webApp/frontend/styles.css` | `.tool-pre.collapsed` L342-345 | `max-height: 120px; overflow-y: auto` |
| `webApp/frontend/styles-container.css` | — | 不存在，忽略 |

### 1.2 差异根因

出参能有「折叠 + 滚动 + 展开全部」是因为它走了 `setCollapsibleText()`；入参在建卡时只做 `textContent` 赋值，从未调用折叠函数。**不是 CSS 类名差异，而是 JS 逻辑根本没走到**。

### 1.3 实测体量（session 日志统计）

- 最大入参 13133 字符（write 工具），edit 4132、askSubAgent 1862；长参数是普遍现象；
- 最大出参 11422 字符。入/出参体量同级，都迫切需要折叠。

### 1.4 现有交互约定（需保持一致）

出参的「展开全部」按钮当前放在 `tool-section` 容器内（`pre` 之后），出参展开后按钮文案「收起」。工具卡片整体展开/收起不影响这两个折叠状态。

### 1.5 版本号现状

- `chatView.js` v1.18 → 本次改后 v1.19
- `styles.css` v1.18 → 本次改后 v1.19（入参折叠复用 `.tool-pre.collapsed`，CSS 仅调数值）

## 2. 方案设计

### 2.1 核心思路

**入参复用出参的现成折叠机制**（`setCollapsibleText` + `.tool-pre.collapsed`），不新造轮子；**高度阈值调大**一个量级（120px → 320px），并统一入/出参行为。

### 2.2 具体修改点

#### T1：入参接入折叠逻辑 + 折叠判据与高度对齐（chatView.js）

`buildToolCard()` 中，入参区现状（L221-225）：

```js
var argsPre = document.createElement('pre');
argsPre.className = 'tool-pre';
argsPre.textContent = safeJson(toolCall.arguments);
argsSection.appendChild(argsTitle);
argsSection.appendChild(argsPre);
```

改为（**调用时机必须在 `appendChild` 之后**，否则 `setCollapsibleText` 内部 `containerEl.appendChild(btn)` 会先插入按钮，随后 `appendChild(argsTitle/argsPre)` 把标题与 pre 排到按钮之后，按钮跑到区块顶部——二审 S1）：

```js
var argsPre = document.createElement('pre');
argsPre.className = 'tool-pre';
argsSection.appendChild(argsTitle);
argsSection.appendChild(argsPre);
setCollapsibleText(argsPre, safeJson(toolCall.arguments), argsSection);
```

**注意**：`setCollapsibleText` 内部已做 `preEl.textContent = text`，无需重复赋值。

**同时调整折叠判据**（`setCollapsibleText` L160）：现状「>300 字符或 >8 行」与新高度 320px 不匹配——会出现「展开全部按钮点了无变化」的困惑交互。判据改为 **>1200 字符或 >16 行**。换算依据：T2 给 `.tool-pre` 显式声明 `line-height: 1.55`；全局 `box-sizing: border-box` 使 320px 包含上下 16px padding，实际内容区约 304px；行高为 12px × 1.55 ≈ 18.6px，内容区约容纳 16.3 行，因此 **>16 行必然溢出**。字符数判据兜底「无换行符的超长单行」（pre-wrap + break-all 下长单行实际渲染多行）。此判据入/出参共用，阈值同步放宽与「显示区域变大」需求同向。

#### T2：折叠高度阈值调大 + 行高确定性（styles.css）

```css
.tool-pre {
  background: var(--gray-block); border-radius: 6px; padding: 8px 10px;
  font-size: 12px; line-height: 1.55; white-space: pre-wrap; word-break: break-all;
  font-family: "SF Mono", Menlo, Consolas, monospace;
}
.tool-pre.collapsed {
  max-height: 320px; overflow-y: auto; position: relative;
  overscroll-behavior: contain;
}
```

- max-height 120px → 320px，与 `.thinking-content` 的 320px 对齐，视觉统一；
- **显式声明 `line-height: 1.55`**（与项目其他 monospace 元素如 `.preview-gutter` 一致）：现状无显式行高、浏览器默认值不定，行数判据无法可靠换算；显式后行高锁定 18.6px，扣除上下 16px padding 后 320px 内容区可容纳约 16.3 行，判据与高度严格对齐（二审 S2）。

#### T3：「展开全部」按钮位置统一（chatView.js · setCollapsibleText）

按钮当前在 `tool-section` 内部、`pre` 之后，入参/出参位置保持一致即可，无需改动。
但需确认：入参折叠时按钮位于入参 `tool-section` 末尾，出参按钮位于出参 `tool-section` 末尾，位置对称。

**验证方式**：构建一张入参超长（>1200 字符或 >16 行）的工具卡片，确认两个按钮位置对称、互不干扰。

#### T4：版本号与文件头（chatView.js / styles.css）

按仓库规范更新文件头 Version/Date/Description，描述写明「入参折叠与高度调整」。

### 2.3 交互细节确认

| 场景 | 行为 |
|---|---|
| 入参 ≤1200 字符且 ≤16 行 | 全量显示，无按钮 |
| 入参 >1200 字符或 >16 行 | `max-height: 320px` + 纵向滚动 +「展开全部」按钮 |
| 点「展开全部」 | 移除高度限制，按钮变「收起」 |
| 点「收起」 | 恢复 320px 折叠态 |
| 卡片折叠状态 | 入/出参各自的展开/收起状态独立保留，卡片整体展开/收起不影响 |

### 2.4 不改的内容

- 出参逻辑除判据同步放宽外完全不动（仅受益于 T2 高度调整与判据对齐）；
- 确认弹窗 `confirmArgsEl`（`showConfirmModal`）——弹窗本身可滚动，入参展示不在此范围。

### 2.5 风险与边界

- **调用时机**：`setCollapsibleText` 必须在 `argsSection.appendChild(argsPre)` 之后调，否则按钮 DOM 顺序错乱（二审 S1，已写入 T1）；
- **为什么不用 `scrollHeight` 实测判断**：`buildToolCard` 调用时卡片尚未挂载 DOM（detached 元素 `scrollHeight` 恒为 0），实测方案需延迟到挂载后异步补判，复杂度不值得；显式 line-height + 行数判据足够可靠。
- **字符阈值的有意取舍**：`>1200 字符`用于兜底 JSON 字符串中的转义换行——例如 write 的 `content` 经 `JSON.stringify` 后可能成为很长的单个逻辑行。在超宽视口中，1200 字符可能尚未撑满 320px，按钮展开前后高度无变化；这是现有轻量判据的可接受边界。继续提高阈值会让常规宽度下的长单行突破最大高度却无展开按钮；改用挂载后异步测量则明显增加状态与时序复杂度。本次保持最小改动，并以「>16 个真实换行」用例验证严格的高度对应关系。
- **按钮移除时机**：`setCollapsibleText` 每次调用先 `containerEl.querySelector('.tool-expand-btn')` 移除旧按钮再按条件重建，且作用域限定在各自 section，入/出参按钮互不干扰（已确认）。入参只在 `buildToolCard` 调一次，出参在 End/历史回放/stop 收尾调，无重复追加风险。
- **历史回放路径**：入参经 `buildToolCard` 统一构建，历史卡片与流式卡片同路径，行为一致。
- **marker 大入参**（write/edit 的 oldText/newText 可能上万字符）：一次性 DOM 构建，与出参同级体量，出参已验证无性能问题。

## 3. TODO List

- [x] T1 入参接入 `setCollapsibleText`（调用位于 appendChild 之后）+ 判据调整为 >1200 字符或 >16 行
- [x] T2 `.tool-pre` 显式 line-height: 1.55；`.tool-pre.collapsed` max-height 120px → 320px
- [x] T3 验证入/出参「展开全部」按钮位置对称（按钮在各自 section 末尾、标题下方）、行为一致；验证 8~16 行内容不再出现无效按钮
- [x] T4 更新 chatView.js / styles.css 文件头版本号与描述
- [x] T5 子代理审核方案（按用户指定使用 xaiSubscription/grok-4.6 终审，结论：通过，无高/中问题）
- [x] T6 用户验收（浏览器实测已生效）

## 4. 验证清单（交付前自查）

1. 长入参（如 write 命令、长 edit）卡片：入参区域 max-height 320px、可滚动、「展开全部/收起」正常切换；
2. 短入参（如 pwd）卡片：入参全量显示，无按钮，不出现多余滚动条；
3. 中等体量（8~16 行且 ≤1200 字符）内容：不触发折叠、无无效按钮（判据与高度对齐的回归点）；
4. 出参行为不回退：折叠/滚动/展开全链路正常，阈值放宽后短出参不再误出按钮；
5. 入参区按钮位于「入参」标题与内容下方（不在标题上方——DOM 顺序回归点）；
6. 历史会话回放：同样的折叠行为（与流式一致）；
7. 待确认弹窗、pending 卡片不受影响；
8. 无 console 报错。

## 5. 实施与验证结果

- 修改 `webApp/frontend/js/chatView.js`：入参复用折叠函数，阈值更新为 >1200 字符或 >16 行；
- 修改 `webApp/frontend/styles.css`：入/出参折叠高度统一为 320px，并固定 `line-height: 1.55`；
- 修改 `webApp/frontend/index.html`：为 v1.19 的 `styles.css` / `chatView.js` 添加版本参数，避免刷新后继续命中旧缓存；
- 新增 `tests/testToolCardCollapse.py`：覆盖字符/行数边界、展开/收起、按钮隔离、DOM 顺序、CSS 契约及版本化资源引用；
- `uv run pytest tests/testToolCardCollapse.py -q`：1 passed；
- `uv run pytest -q`：71 passed；
- `node --check webApp/frontend/js/chatView.js`：通过；
- `git diff --check`：通过。

## 6. 刷新后未生效的补充诊断与修复

### 6.1 现象与根因

用户在首次实施后刷新界面，历史工具卡片仍保持旧展示。排查确认：

- 运行进程为当前仓库的 `python -m webApp`，监听 8787；
- 直接请求服务端时，已返回 `chatView.js v1.19` 与 `styles.css v1.19`，无需重启后端；
- 但 `index.html` 使用固定 URL `/static/styles.css`、`/static/js/chatView.js`；StaticFiles 响应无显式 `Cache-Control: no-cache`，浏览器可能复用旧静态资源；
- 应用内会话刷新只重新拉取数据，不会重载 JavaScript/CSS。

### 6.2 最小修复

只为本次变更的两个资源添加版本参数：

```html
<link rel="stylesheet" href="/static/styles.css?v=1.19">
<script src="/static/js/chatView.js?v=1.19"></script>
```

不修改全局静态缓存策略，避免扩大范围或要求重启服务。

### 6.3 补充 TODO

- [x] T7 `index.html` 为 tool-card JS/CSS 增加 v1.19 缓存失效参数并更新文件头；
- [x] T8 回归测试锁定版本化资源引用；
- [x] T9 `xaiSubscription/grok-4.6` 复审补充修复（结论：通过，无高/中风险）；
- [x] T10 用户浏览器整页重载并完成视觉验收。
