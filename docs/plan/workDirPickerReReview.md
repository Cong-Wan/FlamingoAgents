# workDirPickerPlan 复审报告

对照：`docs/plan/workDirPickerReview.md`（11 条）× 修复后 `docs/plan/workDirPickerPlan.md`（2026-08-17，状态栏自称高危 1/2/3 已重写）

## 结论

**仍需修改**

原 11 条里，高危 2/3、中危 4/5/6/7、低危 8/9/10/11 均已按审核意见落地；高危 1 的**切分**已修好，但回填拼接公式与 `splitWorkDirInput` 的返回值对不上，常规路径和下钻会丢中间 `/`。这是主路径正确性问题，实现时按字面抄会直接坏掉，不能带进编码。

## 逐条核对

### 1. 【高】仅一个前导 `/` 时目录部分为空 → 部分解决

**已修好的部分：**
- `splitWorkDirInput` 已按审核稿写入：`''`/`'/'` → `{ browsePath: '/', prefix: '' }`；`browsePath === ''` 归一成 `'/'`。
- `/Users` 浏览 `/`、前缀 `Users`，不再打空串 400。
- 必测用例已列出（含 `/`、`/Users`、`~`、`~wilbur`、裸 `Users`、`''`）。
- `/` 回填特例写了：`'/' + 名称 + '/'` → `/Users/`，不再退化成相对路径 `Users/`。

**缺什么：**
切分函数的 `browsePath` 是 `slice(0, lastIndexOf('/'))`，**不含尾斜杠**。方案回填公式仍写：

> 回填为 `browsePath + 名称 + '/'`（browsePath 为 `'/'` 时拼成 `'/' + 名称 + '/'`）

按字面实现：

| browsePath | 选中 | 方案公式结果 | 正确结果 |
|---|---|---|---|
| `/` | `Users` | `/Users/` | `/Users/` ✓（已特判） |
| `/Users/wilbur` | `proj` | `/Users/wilburproj/` | `/Users/wilbur/proj/` |
| `~` | `Documents` | `~Documents/` | `~/Documents/` |
| `~wilbur` | `proj` | `~wilburproj/` | `~wilbur/proj/` |

只特判了 `'/'`，最常见的「有多级的绝对路径」和 `~` / `~user` 下钻全缺分隔符。审核当时只逼修了空串→相对路径，拼接函数本身没钉死，修复稿把 `browsePath` 收成正式返回值后，这个洞更大了。

方案 §2.2 必须补可单测的 join（与 split 成对），例如：

```javascript
function joinWorkDir(browsePath, name) {
  var base = browsePath === '/' ? '' : browsePath;
  return base + '/' + name + '/';
}
```

覆盖：`'/' + Users`、`'/Users/wilbur' + proj`、`'~' + Documents`、`'~wilbur' + proj`。TODO 6 手测应加一条「回填后路径里斜杠还在」。

### 2. 【高】`mkdir(parents=True)` 后把 `FileExistsError` 当成功 → 已解决

- 改为 `mkdir(parents=True, exist_ok=True)`，明确废弃「FileExistsError 一律成功」。
- 捕获 `FileNotFoundError` / `PermissionError` / 泛化 `OSError`（文中点名含 `NotADirectoryError`、目标被文件占的 `FileExistsError`）。
- mkdir 后 `is_dir()` + `R_OK|W_OK|X_OK` 双保险。
- 顺序钉死：`expanduser` → mkdir → `resolve` 入库。
- TODO 3 有 `/etc/hosts/foo/bar` 的先探后建反例。

未引入新的异常漏网。

### 3. 【高】键盘分流位置 / 吞 Enter / 无条件拦截 `→` / 无 IME → 已解决

- 写死：**改写** `sidebarView.js` 现有那一处 document keydown，禁止再挂平行监听。
- IME：`isComposing || keyCode === 229` 入口放行。
- 闸门改为 `suggestVisible = 非 hidden 且 suggestItems.length > 0`，滤空/请求未回时 Enter 回落创建。
- `→` 仅 caret 在末尾才消费；Tab 同样要有高亮。
- 默认 `suggestIndex = 0`；本次不推进 `modalStack`；事实核对（store.js 空栈直接 return）已写进方案。
- TODO 6 含 IME、Esc 分层、`→` 不在末尾只移光标。

实现按这段散文就能写，不再是「原则」。

### 4. 【中】改 `.value` 不会触发 `input` → 已解决

- `setWorkDirValue` 显式 `hideProbeFeedback()` + `scheduleSuggest()`。
- `openModal` 预填走该函数并 `focus()`。
- 点击用 `mousedown` + `preventDefault()`。
- `closeModal` 要求 `hideSuggest()` + 递增请求序号/清 debounce。

预填、回填、下钻三条链路都不再指望 `input` 事件。

### 5. 【中】`nearestWritableAncestor` 遇文件不能继续往上找 → 已解决

三句话合同 + 与审核一致的参考实现都在 §2.3：从 `path.parent` 走，第一个 `exists()` 节点必须是可写目录否则 `None`，禁止跨文件。TODO 3 验证 `/etc/hosts/foo/bar` → `creatable=false` 且 create 400。

### 6. 【中】照抄 fileBrowser 物理序 500 截断会丢匹配 → 已解决

截断顺序改为：全量 scandir → 只留目录（`follow_symlinks=True`）→ 丢 dot → `name.lower()` 排序 → 再截 500。明确写了「刻意与 fileBrowser 不同」。审核里「请求带 prefix 再截」是可选优化，方案不采用不构成未修。

残留限制（不阻塞）：子目录按名排序超过 500 后，排在后面的前缀前端仍滤不到。第一版可接受，不必为此改接口。

### 7. 【中】`~user` 被当成裸输入浏览 `/` → 已解决

无斜杠且以 `~` 开头整段作 `browsePath`；`~user/xx` 走 `lastIndexOf` 得到 browsePath=`~user`。Windows 反斜杠写进非目标。必测含 `'~wilbur'`。回填缺 `/` 记在问题 1 / 新发现，不重复算本条未修。

### 8. 【低】与现码事实出入 → 已解决

- 样式改为 `#fff` + `--border` + `--gray-block`，参照 `.command-panel`，不再提 `--panel`。
- 改动表版本写死：html 1.8→1.9、css 1.14→1.15、api 1.5→1.6、fileBrowser 1.2→1.3。
- 与 fileBrowser 的异同收窄到「单条失败跳过」；截断/滤 dot/只目录/绝对路径均标成刻意不同。
- mkdir 前至少 expanduser、resolve 用于 probe 返回值和入库，避免先 resolve 改语义。

### 9. 【低】轻微过度设计 → 已解决

三项都选了「少写」：响应去掉 `parent`；后端写死滤 hidden、不加 `showHidden`；scandir 放到 `fileBrowser.listAbsDirs`，路由只做校验映射。

### 10. 【低】未写边界 → 已解决

空输入 focus 不发请求、`.modal` 裁剪的降级、`parents=True` 非原子半截目录，都补进 §2.2 / §5。预填无尾斜杠会先列父目录，方案不再写「打开就下钻」，与切分规则一致。

### 11. 【低】安全表述差一句 → 已解决

§2.1 / §5 已补：信任边界 = 持 token 的本机用户；多用户/对外网必须同时收紧 listDir 与 createSession，单收紧 listDir 无意义。

## 新发现问题

### A. 【高】回填 join 与 split 的 browsePath 契约不一致

见问题 1。这是修复稿把 `browsePath` 收成正式 API 之后暴露出的主路径 bug，不是吹毛求疵。不补 `joinWorkDir`、不改正文公式，不能开工。

### B. 【低】§2.1 响应示例与正文打架

文首 200 示例仍是：

```text
{ "path": "...", "parent": "/Users/wilbur", "entries": [ {"name": "FlamingoAgents"} ] }
```

正文已改为「只返回 `{ path, entries, truncated }`，不带 `parent`」。实现很容易抄示例把 `parent` 加回去，或缺 `truncated`。示例应改成与正文一致。

无其它新问题。`listAbsDirs` 入参名叫 `absPath` 却内部 expanduser，略不准，不影响实现。

## 最终建议

**不可进入实现阶段。**

差的不是设计方向，是一处会让补全回填写错路径的公式，外加一处示例/正文不一致。改完这两点（`joinWorkDir` 写进 §2.2 + 修正 200 示例）即可开工，不必再开一轮对原 11 条的复审——其余条目已经闭环。
