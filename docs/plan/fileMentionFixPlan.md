<!--
Author: wilbur
Version: 1.1
Date: 2026-08-14
Description: fileMention 两个 bug 修复方案——(1) @ 面板 ↑↓ 选择时显示框不随高亮滚动（缺失 slashCommand 同款
             scrollActiveIntoView）；(2) 文件夹无法作为附件选中（前端目录只下钻不可选 + 后端附件拼接仅支持文件）。
             方案：前端目录行改为「点击行=下钻 / 点击尾部按钮或 Enter=选中文件夹、Tab=下钻」，chip/气泡图标按类型区分；
             后端 buildAttachmentMessage 支持目录附件（递归展开为「相对路径+内容」文本块），chip 协议加 type 字段。
             v1.1 首轮评审修订：P1 目录行「选中」按钮补 stopPropagation 防冒泡触发下钻；P2 expandDirAttachment
             递归逐条目 resolveInside 校验 + realpath 集合防符号链接环/逃逸；P3 版本号事实修正（fileMention 已 v1.2，
             本次 →v1.3；renderChips 现状已是只删 .attachment-chip 保留 .skill-chip，方案改在既有结构上小改）；
             P4 键盘分工改 Enter=选中/Tab=下钻，保住键盘下钻能力；P5 解码策略与 readTextFile 对齐（replace，不计 skip）；
             P6 D6 措辞修正（历史块是 attachment-block 折叠 details 而非 chip）；P7 目录+子文件重叠附件入「明确不做」；
             P8 补 409 重试 type 透传的显式说明。
-->

# fileMention 修复方案：面板滚动跟随 + 文件夹可选为附件

## 1. 问题确认（代码走查结论）

### Bug 1：@ 面板高亮项移动时，显示框不跟随滚动

- `webApp/frontend/js/fileMention.js` 的 keydown 拦截中，↑↓ 修改 `activeIndex` 后调用 `renderPanel()` 重绘，但**没有滚动逻辑**。
- 对照 `slashCommand.js`：`renderPanel()` 末尾调用 `scrollActiveIntoView()`（只改面板自身 `scrollTop`，不用 `scrollIntoView` 以免沿祖先链带动整页）。fileMention 漏抄了这一段。
- 触发条件：条目数超过面板 `max-height: 260px`（约 6 行）时，高亮移出可视区就"消失"。

### Bug 2：不能选择文件夹

现状链路：

- 前端 `fileMention.js pickItem()`：`item.type === 'dir'` 时唯一行为是下钻（`openLevel(item.path, '')`），不存在"选中文件夹"的路径。
- 后端 `fileBrowser.py buildAttachmentMessage()`：对每个附件调用 `readTextFile()`，目录直接 `RuntimeError('不是文件')`。
- 所以"选择文件夹"是前后端都不支持的能力，需要两端一起加。

## 2. 设计决策（显式权衡）

| # | 决策点 | 选定 | 备选（否决理由） |
|---|--------|------|------------------|
| D1 | 目录行的交互 | **点击行=下钻**（保持现状），**行尾「选中」按钮 / Enter·Tab=选中该文件夹** | 点击行直接选中（破坏既有下钻肌肉记忆）；单击选中双击下钻（双击在列表 UI 中反直觉） |
| D2 | 目录附件内容形态 | **递归展开为文本**：`dir/file1.txt:\n内容` 多块拼接，装进一个 `<attachment path="dir">` 块 | 只塞文件清单（丢失了内容，用户 @ 文件夹的意图是给模型看内容） |
| D3 | 目录展开限制 | 复用既有上限：**合计 1MB / 附件块个数 8 不变**；目录展开新增**单目录最多 100 个文件**（超出截断并在块尾注明） | 无限制递归（`.git`、构建产物瞬间撑爆 1MB，错误消息对用户不友好） |
| D4 | 目录内二进制/不可读文件 | **跳过**，块尾注明「跳过 N 个二进制/不可读文件」 | 报错整包失败（一个坏文件拖垮整个目录，违背 listDir 既有"单条目失败跳过"精神） |
| D5 | chip 协议 | `{ path, type }`，`type ∈ 'file' | 'dir'`；图标 📄/📁 按 type 区分（composer chip + 用户气泡 chip 两处） | 仅按 path 猜（后端 listDir 已返回 type，前端顺手带上，零成本） |
| D6 | 历史消息兼容 | 气泡 chip 的图标仅对**本次发送**（sentAttachments 带 type）生效；历史消息解析 attachment 块的旧路径维持 📄 不变 | 给历史块也猜图标（无端 complexity，且块内 path 无法可靠判断目录） |

### D3 的递归展开顺序

- 深度优先；每层排序复用 `listDir` 规则（目录在前、名称小写）。
- 每个文件先 stat 累加大小，**累计将超 1MB 时停止展开**，块尾注明截断。
- 文件数达 100 同样停止并注明。
- 嵌套目录递归展开（相对路径自然体现层级，如 `src/utils/a.py`）。

## 3. 改动点

### 3.1 `webApp/frontend/js/fileMention.js`（v1.2 → v1.3）

1. **Bug1**：新增 `scrollActiveIntoView()`（逐行照抄 slashCommand.js 实现），`renderPanel()` 末尾调用。
2. **Bug2**：
   - `renderPanel()`：`item.type === 'dir'` 的行追加一个尾部按钮 `<span class="command-desc mention-pick-dir">选中</span>`。
     **按钮 mousedown 必须 `event.stopPropagation()` + `event.preventDefault()`** 后 `pickItem(item, true)`——否则冒泡到行
     mousedown 会再执行下钻，出现「加 chip 同时又钻进目录」（评审 P1）。行本身 mousedown 维持 `pickItem(item)`（下钻）。
   - `pickItem(item, pickDir)`：签名加第二参。`item.type === 'dir' && !pickDir` → 下钻（现状）；`item.type === 'dir' && pickDir`
     → 走文件同款选中路径（clearTriggerText + addChip + closePanel + focus）。
   - keydown 分工（评审 P4，保键盘下钻能力）：**Enter=选中当前项**（目录即选中文件夹），**Tab=下钻**（仅对目录生效，
     对文件=选中）。↑↓/Esc 不变；IME 组合态放行逻辑不变。
   - `addChip(path)` → `addChip(path, type)`；`chips` 元素变 `{ path, type }`；去重键维持 path。
   - `renderChips()`：图标按 `chip.type === 'dir' ? '📁' : '📄'`。**注意现状（v1.2）已是「只删 .attachment-chip 重建、
     保留 .skill-chip」结构，本次只改 label 图标表达式，不动 skill-chip 保留逻辑**（评审 P3 事实修正）。
   - `getAttachments()`：返回 `{ path, type }`。
   - 目录行不显示「超过 512KB」文案（该文案本就只属于 `attachable === false` 的文件行，无交叉，确认不改）。

### 3.2 `webApp/frontend/js/chatView.js`（气泡 chip 图标）

- `appendUserMessage()` 的 sentAttachments 渲染：`chip.textContent = (attachment.type === 'dir' ? '📁 ' : '📄 ') + attachment.path;`
- 仅此一行；历史消息解析分支（`buildAttachmentBlock` 折叠 `<details class="attachment-block">`，非 chip）不动（D6，评审 P6 措辞修正）。
- 409 重试链路显式说明（评审 P8）：`lastUserSend = { text, attachments }` 捕获的是 `getAttachments()` 返回的新数组对象，
  chips 随后清空不影响 payload；type 字段随重试 payload 自动透传，**无需改动**。

### 3.3 `webApp/backend/fileBrowser.py`（v1.1 → v1.2）

1. 新增常量 `maxDirExpandFiles = 100`。
2. 新增 `expandDirAttachment(workDir, relPath) -> dict`：
   - 顶层 `resolveInside` 拘禁校验（复用）。
   - 递归展开（os.scandir，排序同 listDir）。**符号链接安全（评审 P2）：递归中每个条目先算绝对路径再 resolve，
     对每个 resolve 结果做 `is_relative_to(base)` 校验——越出 workDir 的符号链接/条目一律跳过并计入 skipped；
     同时维护已访问 realpath 集合，命中过的目录（符号链接成环或硬链接重复）直接跳过**，防环防重复展开。
   - 每个文件：
     - 目录 → 递归（先过上述校验）；
     - 文件 → 读 bytes；含 `\x00` 或 OSError → `skipped += 1`，继续；**解码与 readTextFile 对齐用
       `errors='replace'`（永不抛 UnicodeError，不产生 skip 计数）**（评审 P5）；
     - 否则累加 `totalBytes += size`，**超限（1MB）或文件数超限（100）→ 置 truncated，停止整棵树的展开**。
   - 返回 `{ content, totalBytes, fileCount, skipped, truncated }`，content 形如：
     ```
     a.txt:
     <内容>

     sub/b.py:
     <内容>
     ```
     尾部按需追加 `\n\n[目录附件已截断：...]` / `[跳过 N 个二进制或不可读文件]`。
   - 空目录/全部跳过 → `RuntimeError('目录附件为空或无可读文本文件：...')`（发送前 400，用户可感知）。
3. `buildAttachmentMessage()`：
   - 取 `item.get('type')`；`'dir'` → 走 `expandDirAttachment`，否则走原 `readTextFile` 路径（**type 缺省按 file 处理，旧客户端/旧调用零影响**）。
   - 目录附件的 `attachmentCloseTag` 冲突校验：对展开后整体 content 做一次（与文件一致）。
   - totalBytes 限额检查对目录附件用展开后的合计，逻辑位不变。
4. `maxTotalBytes` 检查时机：目录展开内部即按同一上限截断，外层再校验一次（双保险，实际上展开内已兜住）。

### 3.4 `webApp/backend/server.py`

- 无签名改动；`attachments` 数组元素从「仅 path」变为「path + 可选 type」，现有 `isinstance(item, dict)` 校验天然兼容。**确认无需改动**。

## 4. TODO List

- [ ] T1 fileMention.js：加 `scrollActiveIntoView` + renderPanel 末尾调用 → 验证：条目 >6 时 ↑↓ 到底部，面板滚动且高亮始终可见；页面本身不滚动
- [ ] T2 fileMention.js：目录行「选中」按钮（stopPropagation）+ pickItem 第二参 + Enter=选中/Tab=下钻 → 验证：点击目录行=下钻（现状不变）；点击「选中」→ chip 出现且面板关闭**且不发生下钻**；Enter 对目录=选中、Tab 对目录=下钻
- [ ] T3 fileMention.js：chips 带 type + renderChips 图标 + getAttachments 带 type → 验证：目录 chip 显示 📁，文件 chip 显示 📄；去重仍生效；8 个上限不变
- [ ] T4 chatView.js：气泡 chip 按 type 出图标 → 验证：发送目录附件后气泡显示 📁
- [ ] T5 fileBrowser.py：`expandDirAttachment`（含符号链接越界跳过 + realpath 防环）+ buildAttachmentMessage 按 type 分流 → 验证：`uv run python -c` 直接构造调用：目录附件展开内容正确；超 1MB/100 文件截断注明；二进制跳过注明；空目录 400；**指向 workDir 外的符号链接被跳过不泄露**；符号链接环不死循环
- [ ] T6 端到端（手动）：@ 选文件夹发送 → 后端拼块正确、模型侧收到附件文本；@ 面板滚动正常；文件附件回归（选中/发送/重试 409 路径）不受影响
- [ ] T7 文件头版本号：fileMention.js 1.2→1.3、chatView.js 当前版本+0.1、fileBrowser.py 1.1→1.2，description 写清本次改动

## 5. 验证标准（目标驱动）

| # | 标准 | 方式 |
|---|------|------|
| V1 | ↑↓ 移动高亮时面板滚动跟随，页面不动 | T1 手动 |
| V2 | 目录可被选中为 chip（按钮与键盘两条路径） | T2 手动 |
| V3 | 目录附件内容=递归文本展开，限制与注明正确 | T5 脚本 |
| V4 | 文件附件全链路回归无回归 | T6 手动 |
| V5 | 旧调用（attachments 仅含 path）后端行为不变 | T5 脚本 |

## 6. 明确不做（防范围蔓延）

- 不做目录下钻后 chip 内联编辑/路径自动补全增强。
- 不做历史消息附件块（attachment-block 折叠 details）的目录图标（D6）。
- **不处理「目录 chip 与其子文件 chip 同时存在」的重叠附件**（内容重复且双倍计 1MB，评审 P7）——用户自行移除即可，不做自动检测。
- 不动 `maxTotalBytes`/`maxAttachments` 既有上限值。
- 不做附件面板 UI 重排（按钮复用 `command-desc` 样式，仅加 `mention-pick-dir` 钩子类，样式表只加 cursor/hover 两条规则，若现有样式够用则一行不加）。
