# workDirPickerPlan 审核报告

对照文件：`docs/plan/workDirPickerPlan.md`（2026-08-17）
对照代码：`server.py` v1.10、`sidebarView.js` v1.4、`store.js` v1.2、`index.html` v1.8、`styles.css` v1.14、`fileBrowser.py` v1.2、`api.js` v1.5

## 问题列表

### 1. 【严重等级：高】路径切分规则在「仅一个前导 `/`」时目录部分为空，最常见输入会直接 400

**位置**: 方案 §2.2 目录部分切分规则

**问题**: 方案写「以最后一个 `/` 切分」。按字面实现：

| 输入 | `lastIndexOf('/')` | 目录部分 | 过滤前缀 | 实际应浏览 |
|---|---|---|---|---|
| `/Users/wilbur/proj` | 13 | `/Users/wilbur` | `proj` | ✓ |
| `/Users/wilbur/` | 末尾 | `/Users/wilbur` | `''` | ✓ |
| `/Users` | **0** | **`''`（空串）** | `Users` | `/` |
| `/` | **0** | **`''`** | `''` | `/` |
| `Users` | -1 | 走「无 `/`」分支 | `Users` | `/` ✓ |

`/Users`、`/tmp`、`/` 是用户打开弹窗后的主流输入。空串打到后端会命中「path 必须是非空字符串」→ 400 → 方案规定失败就隐藏下拉。结果是：补全在最该工作的时候不工作。

回填同样被坑：方案写回填 `目录部分 + 名称 + '/'`。若目录部分是 `''`，点选 `Users` 会写成相对路径 `Users/`，违背「不引入相对路径」的非目标。

**修复建议**: 切分后把空目录部分归一成 `'/'`；单独的 `'/'` 视为浏览根、无过滤。建议写成可单测的纯函数，覆盖下表：

```javascript
function splitWorkDirInput(raw) {
  var value = String(raw || '');
  if (value === '' || value === '/') return { browsePath: '/', prefix: '' };
  if (value === '~' || value === '~/') return { browsePath: '~', prefix: '' };
  var slash = value.lastIndexOf('/');
  if (slash < 0) {
    if (value.charAt(0) === '~') return { browsePath: value, prefix: '' }; // ~user
    return { browsePath: '/', prefix: value };
  }
  var browsePath = value.slice(0, slash);
  var prefix = value.slice(slash + 1);
  if (browsePath === '') browsePath = '/';
  if (browsePath === '~') browsePath = '~';
  return { browsePath: browsePath, prefix: prefix };
}
```

必测：`'/'`、`'/Users'`、`'/Users/'`、`'/Users/wilbur/pr'`、`'~'`、`'~/Do'`、`'Users'`、`''`。

---

### 2. 【严重等级：高】`mkdir(parents=True)` 后继续把 `FileExistsError` 当成功，会把「中间级是文件」误判成建目录成功

**位置**: 方案 §2.3 TOCTOU 兜底；现码 `server.py` `createSession` 约 247–251 行

**问题**: 现码 `workPath.mkdir()` **不带** `parents`，`FileExistsError` 只表示最后一级被抢建，当作成功是对的。

换成 `mkdir(parents=True)` 后，`FileExistsError` / `NotADirectoryError` 还表示中间某一级已存在且**不是目录**（例如目标 `/tmp/existingFile/a/b`）。若仍 `except FileExistsError: pass`，会接着 `sessionStore.createSession(str(workPath.resolve()), ...)` 写入一个根本不是目录的 workDir。

方案只点了 `FileExistsError` / `FileNotFoundError` / `PermissionError`，漏了：

- `NotADirectoryError`（中间级是文件，POSIX 上很常见）
- 现码已有的泛化 `OSError`（磁盘满、文件名过长、只读 FS）。若实现时按方案「只捕这三种」替换现有 `except OSError`，这些会掉进全局 500

**修复建议**: 公共判定失败就不要 mkdir；mkdir 之后**无论是否 FileExistsError，都必须再验 `is_dir()`**；保留泛化 `OSError`。

```python
def nearestWritableAncestor(path: Path) -> Path | None:
    current = path.parent
    while True:
        try:
            if current.exists():
                if current.is_dir() and os.access(current, os.W_OK | os.X_OK):
                    return current
                return None  # 存在但不是可写目录（含「是文件」），禁止跨过去
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


# createSession 不存在分支
ancestor = nearestWritableAncestor(workPath)
if ancestor is None:
    raise HTTPException(status_code=400, detail=f'无权限创建目录：{workPath}')
try:
    workPath.mkdir(parents=True, exist_ok=True)
except FileNotFoundError:
    raise HTTPException(status_code=400, detail='祖先目录已被删除，请重试。')
except PermissionError:
    raise HTTPException(status_code=400, detail=f'无权限创建目录：{workPath}')
except OSError as error:
    raise HTTPException(status_code=400, detail=f'创建目录失败：{error}')
if not workPath.is_dir():
    raise HTTPException(status_code=400, detail=f'路径已存在且不是目录：{workPath}')
if not os.access(workPath, os.R_OK | os.W_OK | os.X_OK):
    raise HTTPException(status_code=400, detail=f'目录不可读写：{workPath}')
```

`exist_ok=True` 只覆盖「目标已是目录」，中间级是文件仍会抛错，再加 `is_dir()` 双保险。不要再用「FileExistsError 一律视为成功」。

---

### 3. 【严重等级：高】键盘优先序只写了原则，没钉死实现位置；「下拉可见」当闸门会吞掉 Enter；`→` 无条件拦截会破坏光标

**位置**: 方案 §2.2 键盘 / §5 风险；现码 `sidebarView.js` 318–328 行、`store.js` 13–18 行

**事实核对**（方案把 store.js `modalStack` 和新建弹窗键盘并列，容易误导实现）：

- 新建弹窗**没有** `pushModalClose`。`store.js` 的 Esc 监听在 `modalStack.length === 0` 时直接 return，**单独打开新建弹窗时它根本不参与**。
- 真正会抢 Enter/Esc 的是 `sidebarView.js` 自己的 `document` 冒泡监听：弹窗可见则 Enter=`onCreate()`、Esc=`closeModal()`。
- `store.js` 先于 `sidebarView.js` 注册（`index.html` 脚本顺序）。只有「文件预览 / 技能编辑已在栈上，再点新建会话」这种叠层，Esc 才会两个监听都开火——这是既有问题，本次不是主路径。
- `/`、`@` 面板用的是 **input 上 capture + `stopImmediatePropagation`**（`slashCommand.js` / `fileMention.js`），那是另一套，和新建弹窗无关。

**问题**:

1. 若再挂一个平行的 `document`/`input` 监听、却不改现有 318–328 行，Enter 会「回填 + 创建」同时发生。方案只说「先拦下拉再走原逻辑」，必须写成：**改现有这一处监听，禁止再加一条 document keydown**。
2. 「下拉可见 ⇒ Enter 只回填不创建」在这些情况下会变成吞键：列表被前缀滤空、请求尚未回来、还没有高亮项。用户以为在创建，实际什么都没发生。
3. `→` 在文本框里是移光标。下拉可见就拦截 `→`，光标无法右移。VSCode 只在光标已在末尾时才用 `→` 接受补全。
4. 现有 Enter 监听没挡 IME（`slashCommand` / `fileMention` 都有 `isComposing || keyCode === 229`）。中文输入法回车上屏时，document 监听会把这次 Enter 当成创建或选中。下拉加上之后误触面更大。

**修复建议**: 在现有 document 监听里集中分流；Tab/`→` 只在「有高亮项」时消费；Enter 仅在「有高亮项」时回填，否则回落创建；IME 直接放行。

```javascript
function suggestVisible() {
  return !suggestEl.classList.contains('hidden') && suggestItems.length > 0;
}

document.addEventListener('keydown', function (event) {
  if (modalEl.classList.contains('hidden')) return;
  if (event.isComposing || event.keyCode === 229) return;

  if (suggestVisible()) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveSuggestHighlight(event.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (event.key === 'Tab' || (event.key === 'ArrowRight' && isCaretAtEnd(workDirInput))) {
      event.preventDefault();
      applySuggest(suggestItems[suggestIndex]);
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      applySuggest(suggestItems[suggestIndex]); // 有高亮才进这个分支
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      hideSuggest();
      return;
    }
  }

  if (event.key === 'Enter' && event.target.tagName !== 'TEXTAREA') {
    event.preventDefault();
    onCreate();
  } else if (event.key === 'Escape') {
    event.preventDefault();
    closeModal();
  }
});
```

补充约定（方案应写进 §2.2，否则实现会各写各的）：

- 渲染后默认 `suggestIndex = 0`（否则必须先按 ↓ 才能 Tab）
- `applySuggest` 后保持 focus 在输入框
- 本次**不要**把新建弹窗推进 `modalStack`（推进去 Esc 会关弹窗而不是先关下拉，和本次闸门设计相反）

---

### 4. 【严重等级：中】方案把「改 `.value` 会触发 `input`」当成既有事实，预填/回填两条链路都会断

**位置**: 方案 §2.2「回填后 input 事件仍会触发 hideProbeFeedback()，逻辑零改动」；§4 TODO 6「预填 defaultWorkDir 即触发补全」

**问题**: 现码 `workDirInput.addEventListener('input', hideProbeFeedback)` 只响应用户输入或 `dispatchEvent('input')`。JS 赋值 `workDirInput.value = ...` **不会**触发 `input`。

现码 `openModal`（约 185–186 行）正是直接赋值预填，且**不会** `focus()` 输入框。因此：

- 「逻辑零改动」不成立：点击/键盘回填后确认区/红字会残留旧路径
- 「打开弹窗即触发补全」不成立：既没有 `input`，也没有 focus
- 下钻依赖「回填后再触发补全」，必须在回填函数里显式调用，不能指望事件冒泡

**修复建议**:

```javascript
function setWorkDirValue(next) {
  workDirInput.value = next;
  hideProbeFeedback();
  scheduleSuggest(); // 显式，不依赖 input
}

// openModal 预填之后
if (probe.defaultWorkDir) setWorkDirValue(probe.defaultWorkDir);
workDirInput.focus();
```

点击条目用 `mousedown` + `preventDefault()`，避免 input `blur` 先于 `click` 把下拉拆掉导致点不中（方案完全没写这条，是补全 UI 的常规坑）。

`closeModal` 必须 `hideSuggest()` 并递增请求序号（或清掉 debounce timer），防止弹窗关掉 200ms 后过期响应又把下拉画出来。

---

### 5. 【严重等级：中】`nearestWritableAncestor` 合同有歧义：最近存在节点是文件时，不能继续往上找可写目录

**位置**: 方案 §2.3「沿父链向上找最近存在的祖先目录」

**问题**: 「最近存在的祖先**目录**」可以读成两种：

- A. 最近一个**存在的节点**，它必须是可写目录，否则失败
- B. 跳过「存在但不是目录」的节点，继续往上找目录

B 是错的：`/tmp/existingFile/a/b` 的最近存在节点是文件 `/tmp/existingFile`，再往上 `/tmp` 可写，但 `mkdir(parents=True)` 无法穿过文件。若按 B 判定 `creatable=True`，probe 绿灯、create 400，先探后建被破坏。

方案括号里「目标存在时返回 None 由调用方先判存在性」只说了目标自身，没说中间级。

**修复建议**: 合同写成三句话，并在 TODO 2 加一条验证：

1. 不考虑 `path` 自身，从 `path.parent` 往上走
2. 遇到第一个 `exists()` 的节点：仅当 `is_dir() && access(W_OK|X_OK)` 才返回它，否则返回 `None`（存在的文件 / 不可写目录都是 `None`）
3. 走到根仍没有 → `None`

验证：`probeWorkDir {"workDir":"/etc/hosts/foo/bar"}`（或任意已存在文件下的子路径）→ `creatable=false`，message 指出该文件路径；create 同路径 `allowCreate=true` → 400，且**不会**把 FileExistsError 吃成成功。

---

### 6. 【严重等级：中】`fs/listDir` 若照抄 `fileBrowser.listDir` 的「scandir 中途 500 截断」，补全会丢匹配项

**位置**: 方案 §2.1「单层上限 500 条，超出 truncated: true（与 fileBrowser 口径一致）」；`fileBrowser.py` 139–151 行

**问题**: `fileBrowser.listDir` 按 **scandir 物理顺序**数到 500 就 break，再排序。目录补全的匹配发生在前端 `startsWith`。一个含大量子目录的目录里，用户要的前缀若排在 scandir 500 名之后，下拉是空的，看起来像「没有这个目录」。

fileExplorer 浏览可以接受「这一层被截断」；补全按前缀找名字，截断语义不同。`showHidden` 默认过滤若也在截断之后做，500 配额还会被隐藏目录占掉。

**修复建议**: 不要复用 fileBrowser 的 early-break 顺序。本端点建议：

```text
scandir → 只保留目录（follow_symlinks=True，与 fileBrowser 一致）
        → 单条 stat 失败跳过
        → 默认丢掉 name 以 '.' 开头的（showHidden=false）
        → 全量按 name.lower() 排序
        → 再截 500，truncated=true
```

若担心极大目录（数万子目录），更好的最小改动是让请求体带上前端已经算好的 `prefix`，后端按前缀过滤后再截断。这比盲目复用 500 口径更贴补全。不需要为此上树形浏览。

另外请写明：符号链接指向目录时，用 `is_dir(follow_symlinks=True)`，否则用户在 `/tmp` 这类全是链接的位置会看到空列表。

---

### 7. 【严重等级：中】`~user`（无斜杠）被方案当成「裸输入，浏览 `/`」

**位置**: 方案 §2.2 `~/xx` / 单独 `~` / 无 `/` 的裸输入

**问题**: 后端 `Path(...).expanduser()` 支持 `~user`。前端规则把无 `/` 的 `~wilbur` 送去浏览 `'/'`、前缀 `'~wilbur'`，根下不会有这个名字，下拉恒空。创建时后端又能展开，探测和补全不一致。

单独的 `~`、`~/xx` 方案是对的；漏的是 `~user` 和 `~user/xx`。

**修复建议**: 以 `~` 开头且第一段没有 `/` 时，browsePath 用整段 `~user`（交给后端 expanduser），不要当根目录前缀过滤。见第 1 条 `splitWorkDirInput`。

Windows 反斜杠本次可明确不做（本工具按 macOS 单用户本地），在非目标里写一句，避免有人顺手 `split(/[\\/]/)`。

---

### 8. 【严重等级：低】方案与现码的事实性出入（实现时会抄错）

1. **不存在 CSS 变量 `--panel`**。`styles.css` `:root` 只有 `--sidebar-bg` / `--main-bg` / `--gray-block` / `--border` / `--accent` 等；弹窗背景是写死的 `#fff`。应写「`#fff` + `--border` + hover 用 `--gray-block` 或 `--bubble-user`」，可直接抄 `.command-panel` / `.command-item.active`（已有补全面板样式）。
2. **改动表漏了文件头版本**：`index.html` 当前 1.8、`styles.css` 当前 1.14，按仓库规则这次应 → 1.9 / 1.15。`api.js` 写「+0.1」是对的（1.5→1.6），最好把目标版本写死以免看漏。
3. **「与 fileBrowser 口径一致」范围被写大了**。fileBrowser：不藏 dot、文件+目录都返回、`path` 是相对 workDir、无 `parent` 字段、scandir 中途截断。新端点只在「单条失败跳过 + 上限 500」两点相近，其它都应写「刻意不同」。
4. **`createSession` 现码并不是 `expanduser().resolve()` 之后再 mkdir**（方案 §1 非目标那句容易让人改错顺序）。现码是 `Path(workDirRaw).expanduser()` → mkdir → 最后 `workPath.resolve()` 入库。先 resolve 再 mkdir 对不存在路径通常也能工作（macOS `/tmp` → `/private/tmp`），但应明确：**mkdir 前至少 `expanduser()`；resolve 用于 probe 返回值和入库**。不要为了「对齐那一行字」先 resolve 再改语义。

---

### 9. 【严重等级：低】轻微过度设计 / 可再简化的点

- 响应里的 `parent`：方案前端没有「上一级」条目，用不到。要么删，要么 TODO 5 加一条 `..`。现在属于接口多一块。
- `showHidden`：补全默认藏 dot 是对的，但第一版前端不传这个字段。可以后端写死不返回 hidden，等真要再加开关（符合「不要未经要求的可配置性」）。
- `listDir` 不必硬塞进 `server.py` 路由函数里复写一遍 scandir。在 `fileBrowser.py` 加一个**不**走 `resolveInside` 的 `listAbsDirs(absPath, showHidden=False)` 更干净，路由只做校验和异常映射。这不是必须，但比在 `server.py` 里再堆 40 行更贴现有分层。

这些都不是阻塞，选「少写」即可。

---

### 10. 【严重等级：低】其它实现时容易漏、但方案未写的边界

- **空输入**：`path` 非空校验会 400。focus 时若值为空，应直接 `hideSuggest()`，不要发请求。
- **`.modal { overflow-y: auto }`**（`styles.css` 约 567–571 行）：下拉 `position: absolute` 挂在输入框容器上时， theoretically 会被弹窗裁剪。新建弹窗内容不高，max-height ~200px 的下拉大概率还能看见；若联调被裁，给 `.modal` 在新建场景加 `overflow: visible`，或把下拉挂到 `body` 用 `fixed` 对齐输入框。不必一开始就 portal。
- **预填路径没有尾斜杠**：按切分规则会浏览**父目录**、过滤最后一段（例如 `.../FlamingoAgents` 只高亮自己这一项）。这和 VSCode 地址栏一致，但和 TODO 6「打开就下钻」的语气不符。打开后默认高亮这一项，Tab 加上 `/` 再列下级即可，不必改成「存在则列自身子目录」。
- **`parents=True` 非原子**：mkdir 中途失败可能留下半截目录。单用户可接受，在 §5 提一句即可，不必加回滚。

---

### 11. 【严重等级：低】`fs/listDir` 安全评级结论同意，表述差一句即可

方案判断正确：已登录、单用户本地、workDir 本就可指任意路径、只返回目录名不返回文件内容/文件列表，不引入新的实质能力。token 一旦泄漏，现有 `createSession` + 会话内 files/fileContent + agent 工具已经能摸到文件系统。

建议在 §2.1 / §5 补半句，避免以后有人按「已评审可接受」原样搬到多用户/局域网开放部署：

- 本端点的信任边界 = 持有登录 token 的本机用户
- 多用户或对外网暴露时必须加根路径白名单（方案已有），且应同时收紧 `createSession` 的 workDir，单收紧 listDir 没有意义

不必为此加白名单，同意按现状做。

---

## 无问题的方面

- **不能复用 `GET /api/sessions/{id}/files`**：该端点经 `fileBrowser.resolveInside` 拘禁在已有会话 workDir 内；新建会话还没有 sessionId，浏览范围是服务器绝对路径。必须新开独立端点。判断正确。
- **挂 `authedApi`、失败走 `RuntimeError` → 现有 400 映射**：和 `fileBrowser` 的错误出口一致，不会掉进 fallback 500。
- **只列目录、默认不返回 dot 目录**：补全 workDir 场景正确；与 fileExplorer「不藏 dot、文件也列」是不同产品，分开是对的。
- **单条 `stat` 失败跳过**：与 `fileBrowser.listDir` 容错语义一致，坏符号链接/竞态删除不会拖垮整层。
- **前端防抖 200ms + 请求序号丢弃过期响应**：快输时旧列表盖新列表是这类 UI 的典型 bug，方案已覆盖。
- **浏览失败隐藏下拉、错误交给 probe 红字**：职责不重复，避免输入过程弹两套报错。
- **`creatable` / `willCreate` 两布尔语义保持**：现有 `sidebarView.onCreate` 只认这两个布尔（约 229–236 行），不改前端判定就能接上多级创建确认区。
- **确认区复用缓解 `mkdir(parents=True)` 手滑建一串目录**：现有「确认创建」展示 `resolvedPath`，用户能看到完整待建路径。不必再加第三层确认。
- **抽出 `nearestWritableAncestor` 供 probe/create 共用**：能避免先探后建不一致，这个抽象值得写，不是过度设计。
- **create 仍先做 providerId/modelId 预检再动目录**：方案没打算改这段顺序，孤儿目录风险维持现状。
- **存量 `webData/sessions.json` 零迁移**：只改创建路径，正确。
- **不改 fileExplorer / fileMention、不引入相对路径存储**：边界清楚。`~` 只作输入糖、入库仍 `expanduser` 后的绝对路径。
- **安全面「单用户 + 鉴权 + 不暴露文件内容」的风险接受**：合理（见问题 11 的补充表述，不是推翻结论）。
- **版本号规划（server 1.10→1.11、sidebarView 1.4→1.5、api.js +0.1）**：与现文件头一致。
- **TODO 分层（后端端点 → 多级创建 → api → 骨架 → 交互 → 端到端）和手测用例**：覆盖了 `~`、不存在多级、无权限祖先、已存在目录回归，主路径完整。
- **补全下拉优于再做一套文件管理器**：改动面和控制力匹配「类似 vscode 输入地址」的诉求。

---

## 修复优先级建议

实现前先改方案文案，再动手：

1. **重写路径切分**（问题 1 + 7）：空目录部分 → `'/'`，补 `~user`。这是补全能不能用的前提。
2. **收紧 mkdir 并发兜底 + ancestor 合同**（问题 2 + 5）：`exist_ok` / 事后 `is_dir()` / 不跨文件 / 保留泛化 `OSError`。否则放开多级创建会引入错误 workDir。
3. **把键盘分流写成对现有 document 监听的补丁**（问题 3 + 4）：有高亮才拦截 Enter、`→` 仅 caret 在末尾、IME 放行、回填走显式函数而不是指望 `input`。这是方案自己标的「最易出 bug 的点」，目前仍不够可执行。
