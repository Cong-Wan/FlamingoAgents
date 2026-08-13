<!--
Author: wilbur
Version: 1.6
Date: 2026-08-13
Description: 前端两处体验改动方案——(1) 模型回答结束后光标自动回落输入框；(2) 快捷键 Cmd/Ctrl+K 直达「新建会话」弹窗（方案 A）。
             v1.1 评审后修订：采纳 R1（allComplete 单点收口）、R3（onStreamClosed 统一 focus）、R6（新窗口 URL 加 hash）、
             R9（node --check 验证）；明确 R2 焦点显式化、R4 空 hash 现状、R5 stopped 语义、R7/R8 维持决策。
             v1.2 二次评审（C1/C2/C3 严重项修复）：focus 补挂 reloadSession（无 pending 完成路径）与 onStreamFailed；
             新窗口 URL 改为 /#/chat 对齐路由约定；focus 守卫扩展为通用 .modal-mask 检测（S1）；
             快捷键守卫追加弹层打开忽略（S3）；补 S2/O1/O2/O3 显式声明；修正 §3.3 与路由事实不符的表述。
             v1.3 三次评审（N1/N2 轻微项修复）：onStreamClosed 伪代码显式处理 stopping 早退分支的 focus（否则 V5 必挂）；
             补 open() 既有 focus 与挂载点 2 的并存声明；V8/V9 验证方式降级为代码走查为主。
             v1.4 用户拍板：快捷键 N → K——§3.1 预判的浏览器保留键限制成立，采纳原方案预留的备用键 Cmd/Ctrl+K
             （可派发可 preventDefault）；守卫/URL/落地态不变，浏览器原生行为从「新建窗口」变为「地址栏搜索」（已拦截）。
             v1.5（grok 验收 + 用户反馈双重返工）：
             - F1：grok 验收发现主路径失效——completed/error/stopped 先 goIdle 置空 stream，onStreamClosed 的 !stream 早退
               永远跳过末尾 focus；且 C1 挂载点（reloadSession 内）被 attachStream 同步禁用 composer 短路成死代码。
               修复：!stream 早退改为补 focus 再 return；C1 挂载点移至 resetToHistoryState；文件头 1.9→1.11。
             - F2：用户实测反馈「新建窗口无 workDir 确认流程」，语义理解偏差（O3 遗留歧义实锤）——
               用户要的是「快速新建会话」而非「新开浏览器窗口」。拍板方案 A：Cmd/Ctrl+K 直达新建会话弹窗，
               sidebarView 新增 openNewSessionModal() 对外口，main.js 快捷键改调它（window.open 逻辑移除）。
             v1.6（grok 复核建议项闭环）：focusComposerIfReady 守卫 +1（#app 隐藏不 focus，堵登录门窄窗口）；
             方案正文 §3.2/T1 同步至 v1.11/v1.12 真实实现（此前仅文档头更新，正文仍是 v1.10 旧挂法，grok 抓出）。
-->

# 前端改动方案：流结束后聚焦输入框 + Cmd/Ctrl+K 新建会话弹窗

## 1. 需求

| # | 需求 | 用户原话 |
|---|------|---------|
| F1 | 每次模型回答完后，光标重新落回输入框 | 「省得再去点一下」 |
| F2 | 快捷键 `Cmd+K`（mac）/ `Ctrl+K`（Windows）直达「新建会话」弹窗（含 workDir 探建确认；v1.5 方案 A，原「新开浏览器窗口」语义被用户否決） | —— |

## 2. 现状梳理（代码事实）

- `webApp/frontend/js/chatView.js`：流生命周期事件全部汇入 `onStreamEvent`；终态有 3 个——`completed` / `confirmationRequired` / `error`（`handleStreamError` 分 4 支处理）。`goIdle()` 是所有回空闲路径的收口；`onStreamClosed()` 是连接关闭回调（SSE 泵结束后必触发一次，无论事件流是否收到终态）；`open()` 末尾已有 `composerInput.focus()`。
- `webApp/frontend/js/main.js`：IIFE 启动引导 + hash 路由，目前**没有任何全局键盘快捷键**，只有登录输入框的 Enter 绑定；已有 `document.getElementById('app')` / `loginGate` 引用。
- `webApp/frontend/js/sidebarView.js`：`openModal()`（新建会话弹窗，§186）是模块内私有函数，外部按钮经 `click` 绑定调用。
- 快捷键相关现状：`store.js` 有一个 document 级 keydown（仅处理 Escape 弹层栈）；`slashCommand.js`/`fileMention.js` 在 composer 上做 capture 阶段拦截（仅处理面板打开时的 Enter/Tab/Esc/↑↓）——均不与 Cmd/Ctrl+N 冲突。
- 窗口概念：纯浏览器 Web 应用，「窗口」= 浏览器窗口/标签页，前端可用 `window.open()` 打开。

## 3. 关键权衡（必须显式声明）

### 3.1 ⚠️ Cmd/Ctrl+N 是浏览器保留快捷键（F2 的最大风险）

- macOS Chrome / Firefox / Safari 的普通标签页中，`Cmd+N` **不会把按键事件派发给网页**（或派发但 `preventDefault()` 无效）；Windows `Ctrl+N` 同理。即：监听器大概率根本收不到事件，按键表现为浏览器原生新建窗口（打开浏览器主页，不是本应用）。
- **最终决策（v1.4，用户拍板）**：快捷键定为 **`Cmd/Ctrl+K`**——K 键事件浏览器正常派发给网页且 `preventDefault()` 有效（GitHub/Linear 同款做法），拦截的是浏览器原生「地址栏搜索」，无功能损失。
- 历史记录：v1.0~v1.3 按用户原需求实现 `Cmd/Ctrl+N`；经声明平台限制后，用户选择换 K，本方案随之更新。

### 3.2 F1 的聚焦时机：终态 vs 连接关闭

| 方案 | 聚焦点 | 优点 | 缺点 |
|------|--------|------|------|
| A. 终态时聚焦 | `completed` / `confirmationRequired` / `error` 各分支 | 「回答完即可打字」时机最早 | 需在多个分支埋点，易漏 |
| B. 连接关闭时聚焦 | `onStreamClosed()` | 单点收口，天然覆盖所有终态（终态事件处理后连接即关闭） | 比 A 晚一个 SSE 关闭 RTT（毫秒级，无感知） |

- **决策：B（`onStreamClosed` 为主收口）**。理由：
  1. SSE 事件流设计为「终态事件 → 泵回写 → 关连接」（见 `onStreamClosed` 注释「先回写后哨兵」），终态与关连接几乎同时，体验差异不可感知；
  2. 现有代码**没有**「所有终态公共后置钩子」，`goIdle()` 语义是「回空闲态」而非「回答结束」，往里塞 focus 会污染其语义（评审 R1 结论：不新增 allComplete 类新函数，避免改动面扩大）；
  3. `onStreamClosed` 中早退分支（`waitingConfirm`）正好是「不应聚焦」的场景，单点判断即可。
- **二次评审修正（C1/C3）**：onStreamClosed **并不能覆盖全部「回答结束」路径**，有两条例外必须单独补 focus：
  - **C1 confirmationMismatch → reloadSession 路径**：`handleStreamError` 的 confirmationMismatch 分支 `goIdle()`（stream 置 null）后调 `reloadSession`，其内部 `attachStream` 重建占位流；后续 attach 连接关闭时 `onStreamClosed` 第一行取到 `stream === null` 直接 return——尾部 focus 永远到不了。**v1.11 修复**：挂载点改在 `resetToHistoryState`（attach 404/失败、composer 恢复可用后）。
  - **C3 REST 预检失败路径**：`onStreamFailed`（400/404/409 未开流，`sse.js` 预检 reject）只 `showError + goIdle`，不经 `onStreamClosed`。修复：`onStreamFailed` 的 `goIdle()` 后补一次 focus。
- **grok 验收修正（v1.11，两个严重项）**：
  - **主路径失效**：completed/error/stopped 会先 `goIdle()` 把 stream 置空，`onStreamClosed` 的 `!stream` 早退若裸 return 会跳过末尾 focus——**最常用的「回答完」路径永远走不到 focus**。修复：`!stream` 早退改为 `{ focusComposerIfReady(); return; }`（切页/登录门由守卫拦截）。
  - **C1 挂载点死代码**：v1.10 把 focus 挂在 `reloadSession` 的 `attachStream()` 调用处，但 `attachStream` 同步置 attaching 并禁用 composer，守卫 2 必然拦截——运行时不生效。修复：挂载点移至 `resetToHistoryState`。
- 聚焦守卫（**必须全部满足才 focus**，v1.12 起 4 条）：
  1. `chatPageEl` 未隐藏——防设置页/用量页挂着的旧流回包抢焦点；
  2. `#app` 未隐藏（v1.12 grok 复核建议项）——堵「completed → goIdle 启用输入框 → 401 跳登录门 → onStreamClosed 补 focus」窄窗口抢登录框焦点；
  3. `!composerInput.disabled`——防无会话态/disabled 态 focus 无效调用；
  4. **无任何 `.modal-mask` 弹层可见**（通用判断，S1 修复）：覆盖 confirmModal / newSessionModal / filePreviewModal 及未来新弹层；waitingConfirm 早退已拦大部分，此处兜底 confirmationRequired 之外的弹框场景。
- 场景声明（二次评审补齐，均不加守卫、不改行为）：
  - **slashPanel/mentionPanel 打开中**（S2）：此时焦点本就在输入框，`focus()` 幂等无害；
  - **errorBar 显示中**（O1）：非模态通知条，不持焦，无需避让；
  - **跨窗口 stopped**（O2）：focus 落在收到 stopped 广播的窗口；浏览器 `focus()` 不激活非前台窗口，前台窗口出现光标属可接受副作用；
  - **attach 404 静默复位**（resetToHistoryState）：v1.11 起**此处补 focus**（attach 落空/失败后 composer 恢复可用，是 C1 真正生效点）；`open()` 正常进入路径的 focus 由 open 末尾既有 focus 与本挂载点幂等叠加。

### 3.3 F2 的最终语义：直达「新建会话」弹窗（v1.5 用户拍板方案 A）

- **语义纠偏**（O3 遗留歧义实锤）：用户「新建一个窗口」的真实意图 = **应用内新建会话**（带 workDir 探建确认流程），非浏览器新窗口。v1.0~v1.4 按字面实现 `window.open` 被用户实测否決。
- **方案 A 实现**：`Cmd/Ctrl+K` → `window.sidebarView.openNewSessionModal()`（等同点击侧栏「＋ 新建会话」按钮），弹窗含 workDir 探建、provider/model 选择、创建跳转全流程。
- 为支持快捷键，`sidebarView` 新增对外口 `openNewSessionModal()`（包装私有 `openModal`）——`slashCommand.js` 的 `/new` 走 API 直连不弹窗，无现成口子，故新增。
- 守卫不变：登录门态/任一弹层打开（含新建会话弹窗自身，防重复触发）不响应；浏览器原生「地址栏搜索」被 `preventDefault` 拦截。
- 不再使用 `window.open`；「新开浏览器窗口」能力如后续需要另行提需求。

### 3.4 边界确认

- **`window.open` 被弹窗拦截？** 键盘事件处理器内同步调用 `window.open` 属于「用户激活（user activation）」场景，现代浏览器不拦截。无需处理。
- **登录门态按快捷键？** 守卫 `appEl.classList.contains('hidden')` 则忽略（未登录开新窗口无意义）。
- **与现有 Escape 栈/capture 拦截冲突？** 无——现有监听只看 Escape / composer 面板键，不看 Cmd/Ctrl 组合。
- **`event.key` 兼容性**：`keydown` 时 `event.key === 'k'/'K'`（按住 Shift 时为大写，但快捷键无 Shift 场景；`toLowerCase()` 归一）。注意 mac 中文输入法下 `event.key` 仍为 `'k'`（ composition 未开始前的功能键），无需 isComposing 守卫（功能组合键不进 IME 组合）。
- **弹层打开时不响应快捷键**（S3 修复）：监听器为 document 级，弹层内按键会冒泡到达。若任一 `.modal-mask` 可见（newSessionModal 输入框内误触等），忽略本次按键。
- **settingsPage/usagePage 允许响应**：全局快捷键语义——快捷键服务的是「应用」而非「聊天页」，与浏览器书签管理器等全局键同理。
- **K 与现有键位冲突核查**（v1.4）：`store.js` Esc 栈、`slashCommand.js`/`fileMention.js` 的 capture 拦截（仅面板打开时处理 Enter/Tab/Esc/↑↓）、`chatView.js` 的 Enter 发送，均不看 Cmd/Ctrl 组合；浏览器侧 mac Cmd+K / win Ctrl+K 均为「地址栏搜索」，被 preventDefault 拦截，无第三方冲突。

## 4. 改动清单（精准修改，只碰必须碰的）

### T1. `chatView.js` —— 流终态后聚焦输入框

- 位置：`onStreamClosed()` 函数内。
- 改动（N2 + v1.11 grok 验收修正）：源码 stopping 分支是 `goIdle(); return;` **早退**，focus 只放函数末尾会跳过 stopping 路径（V5 必挂）；且 `!stream` 早退裸 return 会跳过 completed/error/stopped 主路径（V2/V4 必挂）。因此 stopping 分支、`!stream` 早退、函数末尾**各调一次**（同一守卫函数，幂等）；仅 `waitingConfirm return` 不调（弹框在手，不应聚焦）。

```js
function onStreamClosed() {
  var stream = window.appStore.stream;
  window.statusBar.refresh();
  if (!stream) { focusComposerIfReady(); return; } // v1.11：completed/error/stopped 已 goIdle 置空 stream，早退前补 focus（F1 主路径）
  if (stream.phase === 'waitingConfirm') return; // 弹框在手，不抢焦点
  if (stream.phase === 'stopping') {
    goIdle();
    focusComposerIfReady(); // N2：本窗口点停止的早退分支单独补 focus
    return;
  }
  if (!stream.terminalSeen) {
    markInterrupted();
    showError('连接中断：未收到终态事件，刷新页面可恢复最新状态。');
  }
  goIdle();
  focusComposerIfReady(); // 主收口：中断后的连接关闭
}
```

新增小函数（紧邻 `updateComposer` 定义处，保持就近原则）：

```js
// 回答结束后光标回落输入框（方案 §3.2）：仅聊天页可见、已登录、输入框可用、无弹层时聚焦
function focusComposerIfReady() {
  var chatPageEl = document.getElementById('chatPage');
  if (chatPageEl.classList.contains('hidden')) return;
  if (document.getElementById('app').classList.contains('hidden')) return; // v1.12：登录门态不抢焦点
  if (composerInput.disabled) return;
  if (document.querySelector('.modal-mask:not(.hidden)')) return; // S1：任一弹层可见不抢焦点
  composerInput.focus();
}
```

**focus 挂载点清单**（v1.11/v1.12 最终版）：
1. `onStreamClosed()`——`!stream` 早退（**F1 主路径**：completed/error/stopped goIdle 后的连接关闭）、stopping 早退、函数末尾（连接中断），共 3 次（守卫函数幂等）；
2. `onStreamFailed()` 的 `goIdle()` 之后——覆盖 REST 预检失败路径（C3）；
3. `attachStream` 的 `resetToHistoryState()`——覆盖 attach 404/失败路径（C1 真正生效点，v1.11 自 reloadSession 迁移）。

**已移除的错误挂载点**（v1.11）：`reloadSession` 无 pending 分支的 `attachStream()` 调用处——attachStream 同步禁用 composer，focus 必被守卫 3 拦截，属死代码。

**与 open() 既有 focus 的并存声明**（N1）：`open()` 末尾原有的裸 `composerInput.focus()` **保留不动**；open 途经 reloadSession → attachStream，attach 404 后 resetToHistoryState 会补守卫 focus，与 open 裸 focus 幂等叠加。

- 文件头：Version 1.9 → 1.12（v1.10 初版 → v1.11 grok 验收返工 → v1.12 守卫 +1），Description 追加说明。
- **不改动**：`handleStreamError` 的 4 个分支、`goIdle()` 本体——避免评审 R1 反对的改动面扩大。

### T2. `main.js` —— 全局快捷键 Cmd/Ctrl+K 直达新建会话弹窗（v1.5 方案 A）

- 位置：IIFE 尾部（`hashchange` 绑定附近），与登录绑定同层。

```js
// 全局快捷键（方案 F2 方案 A）：Cmd+K（mac）/ Ctrl+K（win）直达「新建会话」弹窗
document.addEventListener('keydown', function (event) {
  if (!(event.metaKey || event.ctrlKey)) return;
  if (event.altKey || event.shiftKey) return; // 不抢 Cmd+Option+K / Cmd+Shift+K 等其它组合
  if (event.key.toLowerCase() !== 'k') return;
  if (appEl.classList.contains('hidden')) return; // 登录门态不响应
  if (document.querySelector('.modal-mask:not(.hidden)')) return; // S3：弹层打开时不响应（含新建会话弹窗自身）
  event.preventDefault(); // 拦截浏览器默认行为（地址栏搜索）
  window.sidebarView.openNewSessionModal(); // 方案 A：打开新建会话弹窗（含 workDir 探建确认）
});
```

- 依赖：`sidebarView` 新增对外口 `openNewSessionModal()`（v1.2 暴露私有 `openModal`）。
- 文件头：Version 1.2 → 1.3，Description 追加说明。
- **复用既有变量**：`appEl`（文件顶部已有），不新增 DOM 查询。

### 不改动的文件（及原因）

| 文件 | 不改原因 |
|------|---------|
| `sidebarView.js` | **v1.5 起有改动**：新增 `openNewSessionModal()` 对外口（包装私有 `openModal`），支撑方案 A。 |
| `store.js` | Escape 栈与快捷键无关，不混入。 |
| `index.html` / `styles.css` | 无 UI 变更。 |

## 5. 验证计划（目标驱动，无测试框架）

成功标准定义：
1. F1 完成标准：发送消息 → 流式输出 → 回答结束（completed / error / 跨窗口停止）→ **光标在输入框内可直接打字**；确认弹框出现时**不抢焦点**。
2. F2 完成标准：登录态页面按 `Cmd+K`（mac）/ `Ctrl+K`（win）→ 弹出「新建会话」弹窗（含 workDir 输入、provider/model 下拉），不触发浏览器地址栏搜索；登录门态或任一弹层打开时按键无反应；弹窗内填 workDir 走原有探建确认流程。

验证步骤（非 pytest，浏览器手测 + 语法检查）：

- [ ] V1 `node --check webApp/frontend/js/chatView.js && node --check webApp/frontend/js/main.js` 语法通过（评审 R9 建议采纳）
- [ ] V2 手动验证 F1 主路径：发一条消息，模型回答完毕 → 输入框自动获得焦点（可直接打字，无需点击）
- [ ] V3 手动验证 F1 确认态：触发需确认的工具调用 → 弹框出现，焦点**不**在输入框（在弹框按钮上）；点批准后续流结束 → 焦点回输入框
- [ ] V4 手动验证 F1 错误态：断网/无效模型触发 error → 错误展示后焦点回输入框
- [ ] V5 手动验证 F1 停止态：回答中点「停止」→ 连接关闭后焦点回输入框
- [ ] V6 手动验证 F2：mac 浏览器按 `Cmd+K`；Windows 按 `Ctrl+K` → 弹出新建会话弹窗且不触发地址栏搜索
- [ ] V7 手动验证 F2 守卫与流程：登录门页按键无响应；弹窗已打开时再按不重复弹；弹窗内 workDir 探建确认流程与点击「＋ 新建会话」按钮一致
- [ ] V8 验证 C1 修复路径（N3 降级：**代码走查为主**——confirmationMismatch 无故障注入手段难手测）：走查 handleStreamError → reloadSession → attachStream 链路确认 focus 挂载点 2 生效
- [ ] V9 验证 C3 修复路径（N3 降级：**代码走查为主** + 断网辅助）：走查 onStreamFailed 确认 focus 已挂；可断网后发送触发网络层 reject（同走 onStreamFailed）观察焦点回落
- [ ] V10 手动验证 S1：文件预览弹层打开期间有旧流 close → 焦点不被抢走

## 6. 风险与声明

| 风险 | 等级 | 应对 |
|------|------|------|
| ~~浏览器不派发/不允许拦截 Cmd/Ctrl+N~~（v1.4 已消除：改 K 后按键可正常派发与拦截） | — | 用户拍板换 K，风险关闭 |
| focus 与移动端虚拟键盘弹起冲突 | 低 | 本应用桌面端为主；移动端 focus 不强制弹键盘（无用户激活的 focus 多数移动浏览器不弹键盘），可接受 |
| ~~F2 语义理解偏差（窗口 vs 会话弹窗）~~（v1.5 已消除：用户拍板方案 A = 会话弹窗） | — | 语义纠偏完成，风险关闭 |

## 7. TODO lists

- [x] T1 chatView.js：focusComposerIfReady（4 守卫）+ onStreamClosed 3 处/onStreamFailed/resetToHistoryState 挂载 + 文件头 1.12
- [x] T2 main.js：document 级 Cmd/Ctrl+K 监听 → 调 sidebarView.openNewSessionModal()（v1.5）+ 文件头 1.3
- [x] T2b sidebarView.js：新增 openNewSessionModal() 对外口 + 文件头 1.2
- [ ] T3 V1 语法检查
- [ ] T4 V2–V7 浏览器手测（逐项记录结果）
- [x] T5 向用户汇报 Cmd/Ctrl+N 实机拦截风险 → 用户拍板换 K（v1.4 已实施）
- [ ] T6 手动验证 discoverability：「新建会话」按钮右侧显示 kbd 快捷键胶囊（mac 显 ⌘K）；悬停 title 显完整提示；Windows 显 Ctrl+K
