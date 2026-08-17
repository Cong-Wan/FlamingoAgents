# workDir 路径探测补全 + 放开多级目录创建 方案计划

- Author: wilbur
- Date: 2026-08-17
- 状态：已按审核报告（workDirPickerReview.md）与复审（workDirPickerReReview.md，均 sub2api_grok/grok-4.6）修复；复审 A（joinWorkDir）/B（响应示例）已补，可进入实现

## 1. 背景与目标

### 现状痛点
1. 新建会话弹窗（`webApp/frontend/index.html` `#newSessionModal`）中的 workDir 是纯文本框（`#newSessionWorkDir`），必须手输绝对路径，无浏览/补全能力，使用不便。
2. 后端创建目录只允许"创建下一级"：`createSession`（`webApp/backend/server.py`）用 `workPath.mkdir()`（不带 `parents`），父目录不存在直接 400 `父目录不存在`。

### 目标
1. **路径探测补全**：workDir 输入框改造为类似 VSCode「打开文件夹」的体验——输入时实时给出目录补全下拉，可点击/键盘选中回填，仅列目录，支持 `~` 展开。
2. **放开多级创建**：只要有权限即可递归创建多级目录（`mkdir(parents=True)`），probe 与 create 两处同步放宽。

### 非目标（明确不做）
- 不做完整的文件管理器式浏览弹窗（只做输入框下方的补全下拉，最贴近 VSCode 输入路径时的 quick pick 体验，且改动最小）。
- 不改动文件浏览器（fileExplorer）与 @ 附件（fileMention）的既有逻辑——它们工作在会话 workDir 内，与本次的服务器文件系统浏览无关。
- 不引入相对路径支持：workDir 仍以绝对路径语义存储与校验（`Path(...).expanduser().resolve()`），仅输入时支持 `~` 前缀。

## 2. 关键设计决策

### 2.1 新增后端目录浏览端点（不可复用现有 files 端点）
现有 `GET /api/sessions/{id}/files` 经 `fileBrowser.resolveInside` 被拘禁在会话 workDir 内，而新建会话时还没有会话、且浏览范围是整台服务器文件系统。因此新增独立端点：

```
POST /api/fs/listDir
body: { "path": "/Users/wilbur/proj" }   # 支持 ~ 前缀
200:  { "path": "/Users/wilbur/proj", "entries": [ {"name": "FlamingoAgents"} ], "truncated": false }
400:  { "error": "目录不存在或不可读：..." }
```

实现要点：
- `path` 必须是非空字符串；`Path(path).expanduser()`（支持 `~` 与 `~user`）。mkdir 无关，本端点只读。
- 仅返回**目录**条目（补全 workDir 用，文件无意义）。
- **默认不返回 dot 目录**（`.git`/`.venv` 等）：补全场景绝大多数不需要隐藏目录，减少噪音。第一版后端**写死**过滤 `name` 以 `.` 开头的条目，不加 `showHidden` 入参（遵守「不要未经要求的可配置性」；真要再加）。
- 符号链接指向目录时用 `is_dir(follow_symlinks=True)` 判定，否则 `/tmp` 这类大量链接的位置会返回空。
- **截断顺序（刻意与 fileBrowser 不同，评审问题 6）**：scandir 全量 → 只留目录（单条 stat 失败跳过）→ 丢掉 dot 目录 → 按 `name.lower()` 全量排序 → **再截 500** 并置 `truncated: true`。fileBrowser 是「scandir 物理序数到 500 就 break 再排序」，按前缀匹配的补全会因此丢匹配项，不可照抄。
- 单条目 `stat` 失败（坏符号链接/竞态删除）跳过，不拖垮整层（与 fileBrowser.listDir 容错语义一致）。
- 目标不是目录 / 不存在 / 不可读 → `RuntimeError` 中文消息，走统一异常映射 400 透传。
- 挂在 `authedApi` 上（需登录鉴权），与其他 `/api/*` 一致。
- 响应只返回 `{ path, entries, truncated }`，**不带 `parent` 字段**（前端无「上一级」条目，用不到，评审问题 9）。
- 实现落点：在 `fileBrowser.py` 新增一个**不走 `resolveInside`** 的 `listAbsDirs(absPath)` 纯函数（自己 scandir，不复用 listDir 的 early-break），`server.py` 路由只做入参校验与异常映射，不在路由里堆 scandir 逻辑（评审问题 9，贴合现有分层）。

安全考量（评审问题 11 补充表述）：该端点允许已登录用户浏览服务器任意目录名（仅目录名，不含文件内容、不含文件列表）。**信任边界 = 持有登录 token 的本机用户**。考虑到本系统是单用户本地工具、workDir 本就可指向任意路径、token 泄漏后现有 createSession + 会话内 files/fileContent + agent 工具已能摸到文件系统，暴露目录名列表不引入新的实质风险。**若未来多用户化或对外网暴露，必须加根路径白名单，且应同时收紧 `createSession` 的 workDir——单收紧 listDir 没有意义。**

### 2.2 前端补全下拉（VSCode 式）

交互模型（对齐 VSCode 打开文件夹的输入体验）：
- 输入框获得焦点或内容变化时，按**当前输入值的目录部分**发起 `fs/listDir` 请求，在输入框下方渲染补全下拉。
- 目录部分切分规则（前端纯函数 `splitWorkDirInput`，可单测，评审问题 1/7）：**切分后空目录部分必须归一为 `'/'`**，否则最常见输入 `/Users`、`/` 会因目录部分为空串打到后端 400 导致补全在最该工作时失效。

```javascript
function splitWorkDirInput(raw) {
  var value = String(raw || '');
  if (value === '' || value === '/') return { browsePath: '/', prefix: '' };
  if (value === '~' || value === '~/') return { browsePath: '~', prefix: '' };
  var slash = value.lastIndexOf('/');
  if (slash < 0) {
    if (value.charAt(0) === '~') return { browsePath: value, prefix: '' }; // ~user：整段交后端 expanduser，不当根目录前缀
    return { browsePath: '/', prefix: value };
  }
  var browsePath = value.slice(0, slash);
  var prefix = value.slice(slash + 1);
  if (browsePath === '') browsePath = '/';
  if (browsePath === '~') browsePath = '~';
  return { browsePath: browsePath, prefix: prefix };
}
```
必测用例：`'/'`、`'/Users'`、`'/Users/'`、`'/Users/wilbur/pr'`、`'~'`、`'~/Do'`、`'~wilbur'`、`'Users'`、`''`。
  - `/Users/wilbur/proj` → 浏览 `/Users/wilbur` 前缀 `proj`；`/Users/wilbur/` → 浏览 `/Users/wilbur` 无前缀。
  - `/Users` → 浏览 `/` 前缀 `Users`（不是浏览空串）。
  - `~/xx` → 浏览 `~` 前缀 `xx`；`~` 浏览 `~`；`~user`（无斜杠）整段作为 browsePath 交后端 expanduser。
  - 无 `/` 裸输入 `Users` → 浏览 `/` 前缀 `Users`。
  - Windows 反斜杠本版明确不做（本工具按 macOS 单用户本地），不要顺手 `split(/[\\/]/`)。
- 回填拼接（复审 A：**`splitWorkDirInput` 返回的 `browsePath` 不含尾斜杠**，直接用 `browsePath + 名称 + '/'` 会丢中间 `/`——`/Users/wilbur` + `proj` 会拼成 `/Users/wilburproj/`）。必须用与 split 成对的可单测 `joinWorkDir`：

```javascript
function joinWorkDir(browsePath, name) {
  var base = browsePath === '/' ? '' : browsePath;
  return base + '/' + name + '/';
}
```
必测覆盖：`'/'+Users→/Users/`、`'/Users/wilbur'+proj→/Users/wilbur/proj/`、`'~'+Documents→~/Documents/`、`'~wilbur'+proj→~wilbur/proj/`。
- 下拉条目渲染规则：
  - 按前缀做大小写不敏感 `startsWith` 过滤后展示（前缀为空则全部展示，受 truncated 上限约束）。
  - 每项显示目录名，点击后回填为 `joinWorkDir(browsePath, 名称)` 并继续触发补全（可连续下钻）。
  - 渲染后默认 `suggestIndex = 0`（默认高亮第一项，否则必须先按 ↓ 才能 Tab 回填）。
  - 点击条目用 `mousedown` + `preventDefault()`，避免 input 的 `blur` 先于 `click` 把下拉拆掉导致点不中（补全 UI 常规坑）。
- 键盘支持（评审问题 3：**改写现有 sidebarView.js 318–328 行的那一个 document keydown 监听，禁止再挂平行的 document/input 监听**，否则 Enter 会「回填+创建」同时发生）：
  - **IME 放行**：监听入口先 `if (event.isComposing || event.keyCode === 229) return;`，中文输入法回车上屏不会被当成创建/选中。
  - 闸门用 `suggestVisible() = 下拉非 hidden 且 suggestItems.length > 0`（不只是 visible——列表被前缀滤空/请求未回/无高亮项时 Enter 必须回落到创建，否则吞键）。
  - 下拉可见且有高亮项时：`↓/↑` 移动高亮；`Tab` 或 `→`（**仅当光标已在输入框末尾**，否则 `→` 是移光标不可拦截）回填选中项；`Enter` 回填选中项（不创建）；`Esc` 只关下拉（不关弹窗）。
  - 下拉不可见时：Enter = 创建、Esc = 关弹窗（沿用现有逻辑）。
  - 本次**不**把新建弹窗推进 `store.js` 的 `modalStack`（推进去 Esc 会关弹窗而非先关下拉，与本闸门设计相反）。store.js 的 Esc 监听在 `modalStack.length === 0` 时直接 return，单独打开新建弹窗时本就不参与（评审事实核对）。
- 防抖：输入后 200ms 发起请求；用请求序号丢弃过期响应（避免快输时旧响应覆盖新列表）。
- 失败降级：浏览失败（目录不存在/不可读）时下拉隐藏，不打断输入（错误交给创建时的 probe 红字展示，职责不重复）。
- **回填/预填必须走显式函数，不能依赖 `input` 事件**（评审问题 4：JS 赋值 `.value` 不触发 `input`，现码 `input` 监听只挂 `hideProbeFeedback`）：

```javascript
function setWorkDirValue(next) {
  workDirInput.value = next;
  hideProbeFeedback();   // 收起旧路径的确认区/红字
  scheduleSuggest();     // 显式触发补全，不依赖 input
}
```
  - `openModal` 预填后调 `setWorkDirValue(probe.defaultWorkDir)` 并 `workDirInput.focus()`，才能「打开即触发补全」。
  - `closeModal` 必须 `hideSuggest()` 并递增请求序号（或清 debounce timer），防止弹窗关掉 200ms 后过期响应又把下拉画出来。
  - 输入框值为空时 focus 不发请求，直接 `hideSuggest()`。

放置位置：下拉挂在 workDir 输入框正下方（`position: absolute`，输入框容器 `position: relative`），z-index 高于弹窗内容。样式**不用 `--panel`**（该变量不存在，评审问题 8）——背景 `#fff` + `var(--border)`，hover/高亮用 `var(--gray-block)`，可直接参照现有 `.command-panel` / `.command-item.active` 补全面板样式。注意 `.modal { overflow-y: auto }` 可能裁剪 absolute 下拉：新建弹窗内容不高、下拉 max-height ~200px 大概率可见；若联调被裁，再给新建场景 `.modal` 加 `overflow: visible`，不必一开始就做 body portal。

### 2.3 放开多级目录创建

`probeWorkDir` 判定逻辑调整（`creatable`/`willCreate` 两布尔语义不变）：
- 目标已是目录：不变（校验 R_OK|W_OK|X_OK）。
- 目标不存在：用 `nearestWritableAncestor` 判定，可写 → `creatable=True, willCreate=True`，message 提示将创建的多级路径；否则 `creatable=False` 指出无权限/被文件阻挡的位置。
- 路径已存在但不是目录：不变（`creatable=False`）。

`createSession` 目录处理调整（评审问题 2，**换 parents=True 后 FileExistsError 语义已变，不能再用「FileExistsError 一律视为成功」**）：
- 已存在目录 / 已存在非目录：不变。
- 不存在且 `allowCreate=True`：去掉「父目录必须存在」，改为 `nearestWritableAncestor` 判定，然后 `mkdir(parents=True, exist_ok=True)`。
- 异常兜底（**保留泛化 `OSError`**，否则磁盘满/文件名过长/只读 FS 会掉进全局 500）：
  - `FileNotFoundError`（祖先被并发删）→ 400「祖先目录已被删除，请重试。」
  - `PermissionError` → 400「无权限创建目录」
  - 其余 `OSError`（含 `NotADirectoryError` 中间级是文件、`FileExistsError` 目标被文件占）→ 400「创建目录失败」
- **mkdir 后双保险**（exist_ok 只覆盖「目标已是目录」，中间级是文件仍可能抛错或静默通过）：
  - `if not workPath.is_dir(): 400 路径已存在且不是目录`
  - `if not os.access(workPath, R_OK|W_OK|X_OK): 400 目录不可读写`
- **顺序保持现码**：`Path(workDirRaw).expanduser()` → mkdir → 最后 `workPath.resolve()` 入库；不要为了对齐而先 resolve 再 mkdir（评审问题 8.4）。probe 返回值用 resolve。

`nearestWritableAncestor` 合同（评审问题 5，三句话钉死，probe/create 共用）：

```python
def nearestWritableAncestor(path: Path) -> Path | None:
    # 1. 不考虑 path 自身，从 path.parent 往上走
    # 2. 遇到第一个 exists() 的节点：仅当 is_dir() 且 access(W_OK|X_OK) 才返回它；
    #    否则返回 None（存在但是文件 / 不可写目录都是 None，禁止跨过去继续往上找）
    # 3. 走到根仍没有 → None
    current = path.parent
    while True:
        try:
            if current.exists():
                if current.is_dir() and os.access(current, os.W_OK | os.X_OK):
                    return current
                return None
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent
```
关键约束：最近存在节点若是**文件**（如 `/tmp/existingFile/a/b`），必须返回 None 判 `creatable=False`——不能跳过文件继续往上找可写目录，否则 probe 绿灯、create 时 `mkdir(parents=True)` 又无法穿过文件，先探后建被破坏。

`webData/sessions.json` 等存量数据零影响（仅创建路径变更）。

## 3. 改动文件清单

| 文件 | 改动 | 版本号 |
|---|---|---|
| `webApp/backend/fileBrowser.py` | 新增 `listAbsDirs(absPath)`（不走 resolveInside、全量排序后截断、滤 dot、仅目录、follow_symlinks） | 1.2→1.3 |
| `webApp/backend/server.py` | 新增 `POST /api/fs/listDir`（调 listAbsDirs）；`probeWorkDir` 与 `createSession` 放开多级创建；新增 `nearestWritableAncestor` | 1.10→1.11 |
| `webApp/frontend/index.html` | workDir 输入框外套相对定位容器 + 补全下拉容器 `#workDirSuggest` | 1.8→1.9 |
| `webApp/frontend/js/sidebarView.js` | `splitWorkDirInput`、补全下拉交互（触发/渲染/键盘分流/防抖/请求序号）、`setWorkDirValue` 显式回填、改写现有 document keydown | 1.4→1.5 |
| `webApp/frontend/js/api.js` | 新增 `listFsDir(path)` | 1.5→1.6 |
| `webApp/frontend/styles.css` | 补全下拉样式（参照 `.command-panel`/`.command-item.active`） | 1.14→1.15 |

## 4. TODO List

1. [x] **后端：fileBrowser.listAbsDirs**
   - 新增 `listAbsDirs(absPath)`：expanduser、仅目录（follow_symlinks=True）、滤 dot 目录（写死）、单条 stat 失败跳过、全量 name.lower() 排序后截 500（truncated）、非目录/不可读→RuntimeError
   - 验证：python REPL 调 `listAbsDirs('~')` 返回家目录非隐藏目录；`listAbsDirs('/etc/hosts')`（文件）与 `listAbsDirs('/nonexistent')` 抛 RuntimeError 中文消息。
2. [x] **后端：fs/listDir 端点**
   - `server.py` 新增 `POST /api/fs/listDir`，只做入参校验（path 非空字符串）+ 调 listAbsDirs + 异常映射
   - 验证：`curl -X POST /api/fs/listDir -d '{"path":"~"}'`（带 auth）返回非隐藏目录；`{"path":"/nonexistent"}` 与 `{"path":"/etc/hosts"}` 返回 400 中文消息；`{"path":""}` 返回 400「非空字符串」。
3. [x] **后端：放开多级创建**
   - 新增 `nearestWritableAncestor`（按 §2.3 合同）；`probeWorkDir` 与 `createSession` 改用其判定；`mkdir(parents=True, exist_ok=True)` + FileNotFoundError/PermissionError/泛化 OSError 兜底 + mkdir 后 is_dir()/可读写双保险；保持 expanduser→mkdir→resolve 顺序
   - 验证：`probeWorkDir {"workDir":"/tmp/x/a/b/c"}`（/tmp/x 不存在）→ `creatable=true, willCreate=true`；create 同路径 `allowCreate=true` → `/tmp/x/a/b/c` 真实落盘；**`probeWorkDir {"workDir":"/etc/hosts/foo/bar"}`（最近存在节点是文件）→ `creatable=false`，create 同路径 → 400 且不把 FileExistsError 吃成成功**（评审问题 2/5）；无权限祖先 → 400；`probeWorkDir {"workDir":"/"}`（已存在）行为不变；`~` 路径创建正常。
4. [x] **前端：api.js 增加 listFsDir**
   - 验证：控制台 `window.api.listFsDir('/Users')` 返回目录数组。
5. [x] **前端：index.html + styles.css 下拉骨架**
   - workDir 输入框包 `.workdir-field`（relative）+ `#workDirSuggest` 下拉容器；CSS 参照 `.command-panel`/`.command-item.active`（`#fff` 背景、`--border`、hover `--gray-block`）、max-height 滚动、`.active` 高亮
   - 验证：弹窗打开时下拉容器存在且默认隐藏，不破坏现有弹窗布局；下拉不被 `.modal` 裁剪（被裁则加 overflow:visible）。
6. [x] **前端：sidebarView.js 补全交互**
   - `splitWorkDirInput`（含 `/`、`/Users`、`~`、`~user` 用例）、`setWorkDirValue` 显式回填、防抖+请求序号、渲染默认高亮第 0 项、mousedown 点击回填追加 `/` 下钻、改写现有 document keydown（IME 放行 + suggestVisible 闸门 + Tab/→ 仅有高亮且 → 需光标在末尾 + Enter 有高分回填无高分创建 + Esc 先关下拉）、closeModal 清下拉+序号
   - 验证：手测——输 `/Users/wilbur/pr` 下拉前缀过滤；输 `/Users`（单斜杠）下拉列出 `/` 下匹配项而非 400；Tab 回填 `/Users/` 并续列下级；**选中 `/Users/wilbur` 下的 `proj` 回填为 `/Users/wilbur/proj/`（斜杠不丢，复审 A）**；`~`/`~user` 下钻回填斜杠不丢；点击连续下钻；Enter 下拉可见有高分只回填、无高分创建；Esc 先关下拉再关弹窗；**中文输入法回车上屏不触发创建/选中**（IME）；快速连输无旧列表闪盖；`→` 光标不在末尾时只移光标不拦截。
7. [x] **端到端联调**
   - 打开弹窗（预填 defaultWorkDir 经 setWorkDirValue 即触发补全 + focus）→ 下钻/手输到不存在的多级路径 → probe 显示将创建多级确认区 → 确认创建 → 会话创建成功、目录真实落盘。
   - 回归：已存在目录直接创建、无权限路径红字、中间级是文件 400、probe 预填逻辑、`~` 与 `~user` 路径。

## 5. 风险与权衡

- **补全 vs 完整文件管理器**：选补全下拉（最贴近诉求"类似 vscode 输入地址"），不引入树形浏览弹窗，改动面小一个量级。若后续要树形浏览，`fs/listDir` 端点可直接复用。
- **键盘冲突**：新建弹窗 document 级 keydown 已有 Enter=创建/Esc=取消，store.js 另有 modalStack Esc（modalStack 空时不参与）。方案改为**改写现有那一处监听**（禁止平行再挂），以「下拉可见且有高亮项」为闸门、IME 放行、`→` 仅光标末尾——这是实现中最易出 bug 的点，TODO 6 已列专项验证。
- **mkdir(parents=True) 非原子**：中途失败可能留下半截目录，单用户场景可接受，不加回滚。
- **mkdir(parents=True) 的副作用**：用户手滑输错中间路径会静默创建一串目录。缓解：保留现有「确认创建」内联确认区，且 probe message 明确显示完整待建路径，用户确认后才落盘——既有交互已覆盖，无需新增确认。
- **安全面**：`fs/listDir` 暴露服务器目录名。判定为可接受（信任边界=持 token 本机用户 + 需鉴权 + 不暴露文件内容），已在 §2.1 记录；未来多用户/对外网暴露需同时收紧 listDir 与 createSession 的根路径白名单。
