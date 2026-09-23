# 版本号统一显示与单一数据源 —— 方案与计划

> Author: wilbur
> Version: 1.1
> Date: 2026-09-23
> 目的：Web UI「所有界面」显示版本号（登录门 + 登录后全部页面），并把散落 4 处的硬编码版本号收敛为单一数据源文件，其余位置全部引用。
> v1.1：按首轮审核（S1 契约基线 v1.16→v1.28 事实错误）与复审意见修订——契约版本改为 v1.28 → v1.29；新增 §3.13 health 示例改口径；测试风格描述修正；§3.29 新增示例需附「version 随当前版本」口径注释（复审小注）。
> 关联契约：docs/webApiSpec.md（当前 v1.28，需随本方案升 v1.29）。

---

## 1. 背景与现状研究

### 1.1 界面布局结论（显示位分析）

当前 Web UI（`webApp/frontend/index.html`）结构：

| 区域 | 结构 | 版本号显示结论 |
|---|---|---|
| 登录门 `#loginGate` `.login-box` | 全屏遮罩 + 居中卡片：🦩 logo、标题、提示、token 输入、进入按钮 | **显示**：按钮下方居中小字（登录前唯一界面） |
| 侧栏 `.sidebar-bottom` | 新建会话按钮 / 会话列表 / 底部三入口（模型配置、技能、用量统计） | **显示**：底部入口下加一行小字。登录后 4 个页面（chat / settings / skills / usage）共享同一侧栏，**一处修改覆盖全部页面** |
| 4 个页面各自 topbar | chat 页含标题+模型徽章+文件面板按钮；usage 页含时间筛选器 | 不逐页添加：由侧栏统一覆盖，避免 4 处重复且污染 topbar 语义 |
| 弹窗（新建会话/订阅登录/工具确认/技能编辑/文件预览） | 临时遮罩层 | 不显示：临时态，关闭后回到常驻界面 |

边界：侧栏可收起（`sidebarCollapsed`，收起后 `display:none`），收起态版本号不可见。收起是用户主动的临时态，展开即恢复，本方案接受此边界（如需收起态也可见，可后续在 topbar 追加，见 §3 Q1）。

### 1.2 版本号散落现状（4 处硬编码）

| 位置 | 现状 |
|---|---|
| `pyproject.toml` | `version = "0.1.0"`（uv.lock 已同步） |
| `flamingoAgents/__init__.py:19` | `packageVersion = '0.1.0'`（导出但无内部使用方） |
| `webApp/backend/server.py:181` | `/api/health` 响应硬编码 `'0.1.0'` |
| `flamingoAgents/models/responsesAdapter.py:36` | `userAgent = 'FlamingoAgents/0.1.0'`（请求头 User-Agent） |

问题：升版本需改 4 处，漏改即漂移（User-Agent、health、包元数据互不一致）。

## 2. 需求

1. 版本号在「所有界面」可见：登录门 + 登录后全部页面（经侧栏底部常驻）。
2. 版本号单一数据源：一个文件定义，其余位置（含 pyproject.toml）全部引用，改版本只动一个文件。

## 3. 决策

### 已定

| 编号 | 决策 |
|---|---|
| D1 | 唯一权威文件：新建 `flamingoAgents/version.py`，定义 `__version__ = '0.1.0'`（零依赖叶子模块） |
| D2 | 兼容导出：`flamingoAgents/__init__.py` 改为 `from flamingoAgents.version import __version__ as packageVersion`，`packageVersion` 名字不变 |
| D3 | `server.py` `/api/health` 与 `responsesAdapter.userAgent` 改为引用 `__version__` |
| D4 | `pyproject.toml` 改 dynamic version，hatchling 从 `flamingoAgents/version.py` 读取（默认 pattern 匹配 `__version__`），pyproject 不再写死版本 |
| D5 | 前端经免认证 `GET /api/version` 获取（登录门前也要显示，而现有 `/api/health` 需认证；版本号非敏感，业界惯例登录页 footer 免认证展示） |
| D6 | 显示格式 `v0.1.0`；读取时机：前端启动拉取一次，失败静默（不显示、不阻塞启动）；Python 侧模块 import 一次（进程内版本不变属预期，不做热更新） |
| D7 | 弹窗内不显示版本（§1.1 边界） |
| Q1 | 侧栏收起态不显示版本号（用户 260923 拍板：不要） |
| Q2 | 接受 pyproject dynamic version（用户 260923 拍板：接受） |

### 待用户拍板

| 编号 | 问题 | 默认建议 |
|---|---|---|
| Q1 | 侧栏收起态是否也要可见版本号 | 默认否（临时态）；要的话追加 4 个 topbar 右侧显示，工作量 +1 轮 |
| Q2 | pyproject.toml 是否接受 dynamic version（涉及 uv.lock 重锁） | 建议接受（真正单一来源）；不接受则 pyproject 与 version.py 手动同步，并在测试中加一致性断言 |

## 4. 方案设计

### 4.1 单一版本源（Python 侧）

```python
# flamingoAgents/version.py（新建）
__version__ = '0.1.0'
```

引用方：
- `flamingoAgents/__init__.py`：`from flamingoAgents.version import __version__ as packageVersion`（替换字面量，`__all__` 不变）
- `flamingoAgents/models/responsesAdapter.py`：`from flamingoAgents.version import __version__` → `userAgent = 'FlamingoAgents/' + __version__`
- `webApp/backend/server.py`：`from flamingoAgents.version import __version__ as packageVersion` → `/api/health` 返回 `{'ok': True, 'version': packageVersion}`

循环导入分析：import 链为 `flamingoAgents/__init__` → `builder` → `responsesAdapter` → `flamingoAgents.version`。`from flamingoAgents.version import __version__` 走子模块导入（查 `sys.modules['flamingoAgents.version']`，无则加载 version.py），不要求父包 `__init__` 执行完毕，无死锁；version.py 零依赖。TODO A3 含验证步骤。

### 4.2 pyproject.toml dynamic version

```toml
[project]
name = "flamingo-agents"
dynamic = ["version"]   # 删除 version = "0.1.0"

[tool.hatch.version]
path = "flamingoAgents/version.py"
```

hatchling version 插件默认 pattern 匹配 `__version__ = '...'`，无需自定义正则。改后执行 `uv sync` 重新生成 editable 元数据与 uv.lock（预期 diff 仅版本相关行）。

回退方案（若 uv 对 dynamic version 处理异常）：pyproject 恢复静态 `version = "0.1.0"`，接受 version.py + pyproject 两处手动同步，测试 D1 中加断言 `importlib.metadata.version('flamingo-agents') == __version__` 防漂移。

### 4.3 免认证版本接口（后端）

`webApp/backend/server.py` 免认证区（`POST /api/auth/login` 旁）新增：

```python
@app.get('/api/version')
def version():
    # 免认证：登录门前也要显示；仅返回版本号，无敏感信息（webApiSpec v1.29 §3.29）
    return {'ok': True, 'version': packageVersion}
```

注册在 `app.mount('/', ...)` 之前（与现有 authedApi 同理，天然满足）。契约文档 `docs/webApiSpec.md` 同步：
- §1.1 认证规则改为「例外：`POST /api/auth/login`、`GET /api/version`」；
- 新增 §3.29 `GET /api/version`（200：`{"ok": true, "version": "0.1.0"}`，免认证，理由：登录门展示需要且无敏感信息；示例旁附一句「version 随当前版本」口径注释，防升版漂移——复审小注）；
- §3.13 `/api/health` 的 200 示例硬编码 `"0.1.0"` 改为「`version` = 当前版本（`flamingoAgents.version.__version__`）」口径，防升版漂移；
- 文档头部版本 v1.28 → v1.29 并注明变更。

### 4.4 前端显示与获取

**index.html**：
- 登录门 `.login-box` 内、`#loginButton` 之后：`<div id="loginVersion" class="login-version"></div>`
- `.sidebar-bottom` 末尾（三入口之下）：`<div id="sidebarVersion" class="sidebar-version"></div>`
- 递增静态资源版本参数：`styles.css?v=1.25` → `?v=1.26`、`api.js?v=1.10` → `?v=1.11`（破浏览器缓存；main.js 本身无参数，但 index.html 由 `/` html=True 直接伺服，改 html 即生效）

**styles.css**（v1.26）：

```css
.login-version { margin-top: 16px; font-size: 12px; color: var(--text-dim); }
.sidebar-version { padding: 8px 8px 4px; font-size: 11px; color: var(--sidebar-dim); }
```

**api.js**（v1.11）新增（免认证，不带 Bearer 头）：

```js
// 版本号（免认证：登录门也要显示，webApiSpec v1.29 §3.29）
getVersion: async function () {
  var resp = await fetch('/api/version', { headers: { 'Accept': 'application/json' } });
  if (!resp.ok) throw buildError(resp.status, await parseErrorBody(resp));
  return resp.json();
}
```

**main.js**（v1.6）IIFE 末尾新增（登录门态与主界面态都填充，启动即调）：

```js
(function fillVersion() {
  window.api.getVersion().then(function (body) {
    var text = 'v' + body.version;
    var loginEl = document.getElementById('loginVersion');
    var sidebarEl = document.getElementById('sidebarVersion');
    if (loginEl) loginEl.textContent = text;
    if (sidebarEl) sidebarEl.textContent = text;
  }).catch(function () { /* 拉取失败静默：不显示版本，不阻塞启动 */ });
})();
```

## 5. TODO List

### 阶段 A：单一版本源（Python 侧）

- [x] **A1** 新建 `flamingoAgents/version.py`：文件头（Author/Version 1.0/Date/Description）+ `__version__ = '0.1.0'`
      → 验证：`uv run python -c "from flamingoAgents.version import __version__; print(__version__)"`
- [x] **A2** `flamingoAgents/__init__.py`：删除字面量，改 `from flamingoAgents.version import __version__ as packageVersion`；文件头 Version 1.3 → 1.4，Description 注明
      → 验证：`uv run python -c "import flamingoAgents; print(flamingoAgents.packageVersion)"`
- [x] **A3** `flamingoAgents/models/responsesAdapter.py`：`userAgent` 改为 `'FlamingoAgents/' + __version__`（from flamingoAgents.version import）；文件头 Version 1.5 → 1.6
      → 验证：`uv run python -c "import flamingoAgents"`（无循环导入）+ `uv run pytest tests/testResponsesAdapter.py tests/testResponsesReplay.py`
- [x] **A4** `webApp/backend/server.py`：import `packageVersion`，`/api/health` 改用它；文件头 Version 1.20 → 1.21
      → 验证：代码内不再有字面量 `'0.1.0'`（`grep -n "'0.1.0'" webApp/backend/server.py` 为空）
- [x] **A5** `pyproject.toml`：`dynamic = ["version"]` + `[tool.hatch.version] path = "flamingoAgents/version.py"`
      → 验证：`uv sync` 成功；`uv run python -c "from importlib.metadata import version; print(version('flamingo-agents'))"` 输出 `0.1.0`；uv.lock 中 `name = "flamingo-agents"` 的 version 仍为 0.1.0。异常则走 §4.2 回退方案

### 阶段 B：免认证版本接口 + 契约

- [x] **B1** `server.py` 免认证区新增 `GET /api/version`（§4.3 代码）
      → 验证：TestClient 无 Authorization 头 GET 200 且 `body['version'] == flamingoAgents.packageVersion`
- [x] **B2** `docs/webApiSpec.md`：§1.1 认证例外 + 新增 §3.29 + §3.13 health 示例改「version = 当前版本」口径 + 头部 v1.28 → v1.29
      → 验证：文档内 `grep -n "api/version"` 命中新章节；`grep -n '"0.1.0"' docs/webApiSpec.md` 仅剩 §3.29 新增示例一处

### 阶段 C：前端显示

- [x] **C1** `index.html`：`#loginVersion`（login-button 后）、`#sidebarVersion`（sidebar-bottom 末尾）两个空 div；`styles.css?v` 与 `api.js?v` 递增；文件头 Version 1.23 → 1.24
      → 验证：两 id 存在且位于正确容器内
- [x] **C2** `styles.css`：`.login-version` / `.sidebar-version` 样式；文件头 Version 1.25 → 1.26
      → 验证：`grep -n "sidebar-version" webApp/frontend/styles.css` 命中
- [x] **C3** `api.js`：`getVersion`；文件头 Version 1.10 → 1.11
      → 验证：`grep -n "api/version" webApp/frontend/js/api.js` 命中
- [x] **C4** `main.js`：`fillVersion`（§4.4 代码）；文件头 Version 1.5 → 1.6
      → 验证：`grep -n "loginVersion\|sidebarVersion" webApp/frontend/js/main.js` 均命中

### 阶段 D：测试与回归

- [x] **D1** 新建 `tests/testVersionApi.py`（含文件头）：
      ① `GET /api/version` 无认证头 200，`version == flamingoAgents.packageVersion == '0.1.0'`；
      ② `GET /api/health`（带 token）`version` 与 ① 一致；
      ③ `responsesAdapter.userAgent == 'FlamingoAgents/' + packageVersion`；
      ④ `importlib.metadata.version('flamingo-agents') == packageVersion`（防 pyproject 漂移，回退方案下同样适用）
      → 验证：`uv run pytest tests/testVersionApi.py -q` 全绿
- [x] **D2** 新建 `tests/testVersionFrontend.py`（含文件头，仿 `testUsageViewFrontend.py` 静态断言风格）：
      ① index.html 含 `id="loginVersion"`、`id="sidebarVersion"`，且后者位于 `sidebar-bottom` 容器内；
      ② api.js 含 `getVersion` 与 `/api/version`；
      ③ main.js 同时引用 `loginVersion` 与 `sidebarVersion` 且调用 `getVersion`
      → 验证：`uv run pytest tests/testVersionFrontend.py -q` 全绿
- [x] **D3** 全量回归：`uv run pytest -q`
      → 验证：无失败无新增跳过

## 6. 测试计划

- 后端：`tests/testVersionApi.py`（D1，pytest + fastapi.testclient，token 经 `monkeypatch.setattr(auth, 'serverToken', ...)`，与 testUsageApi.py 同法）。
- 前端：`tests/testVersionFrontend.py`（D2，静态断言，仿 `testUsageViewFrontend.py` 的 `testUsagePageHasSinglePeriodSelectAndNoSessions` 与 `testFileMentionFrontend.py` 的纯静态断言模式；不引入该文件主体的 node vm 运行时——本改动无状态机逻辑，静态断言足够）。
- 回归：全量 pytest（重点 `testResponsesAdapter` / `testResponsesReplay`（userAgent 改引用）与 `testUsageApi`（TestClient 启动路径））。

## 7. 风险与边界

| 编号 | 风险/边界 | 应对 |
|---|---|---|
| R1 | `responsesAdapter` 引入对 `flamingoAgents.version` 的 import 造成循环 | version.py 零依赖叶子模块 + 子模块导入语义（不要求父包初始化完成）；A3 验证步骤 + D3 全量回归兜底 |
| R2 | dynamic version 与 uv.lock/build 链不兼容 | §4.2 回退方案：pyproject 恢复静态版本 + D1④ 一致性断言防漂移 |
| R3 | 浏览器缓存旧 styles.css / api.js | C1 递增 `?v=` 版本参数（项目既有惯例） |
| R4 | 免认证接口暴露版本号 | 仅语义版本号，非敏感；登录页展示是业界惯例；不返回其它任何信息 |
| R5 | 侧栏收起态版本号不可见 | 用户拍板接受（Q1），展开即恢复 |
| R6 | 版本运行期热更新 | 不做（D6）：import 一次进程内不变，升级即重启服务 |

## 8. 明确不做

- CLI `--version` 参数（sdkEntry.py / modelLogin.py）——用户未要求；
- 弹窗内显示版本；
- README / 文档正文的版本标注；
- 版本号运行期热更新（R6）。
