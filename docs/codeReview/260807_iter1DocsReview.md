# 代码审核报告 — 迭代一方案（webAppPlan §11 / webApiSpec v1.2）

> Author: wilbur
> Version: 1.0
> Date: 2026-08-07
> 审核对象：docs/webAppPlan.md §11（v1.4）、docs/webApiSpec.md v1.2 变更（§3.3/§3.4/§3.10 + 编号顺延）
> 对照源码：webApp/backend/{server,agentManager,sessionStore}.py、webApp/frontend/js/{sidebarView,settingsView,usageView,chatView,api}.js、styles.css、index.html、flamingoAgents/core/conversation.py、webData/sessionLogs/*.jsonl

## 总览

- 审核文件：文档 2 个 + 源码 10 个
- 发现问题：🟠 高 4 个 / 🟡 中 5 个 / 🔵 低 5 个
- 整体评价：方案整体扎实（probe 无副作用、create 复查父目录防 probe 后竞态、泵线程终态写 delta、查询时算 cost 都是正确方向），主要问题集中在 **probe 响应表自相矛盾**、**usage/series 的时区与 provider 维度两个语义空洞**、**删会话后两套用量口径发散未声明**，以及用户预判的**契约编号顺延漏改前端引用**（属实，3 处）。

---

## 问题清单

### 🟠 高 1 — probe 响应表 writable/willCreate 口径自相矛盾，且缺「存在但非目录」行

**位置**: webApiSpec.md §3.4 情形表；webAppPlan.md §11.1 前端三分支逻辑
**问题**: 表中「不存在，父目录不可写」行是 `writable=true, willCreate=true`，而「不存在，父目录不存在」行是 `writable=false, willCreate=true`。`willCreate=true` 同时出现在「可创建」和两种「不可创建」情形里，`writable` 在两行不可创建情形里取值又相反——前端按 §11.1「不可写/父目录不存在 → 红字禁止提交」**没有任何单一字段可以可靠判定「可创建」**。另外表格缺「路径已存在但不是目录」（如指向一个文件）的情形：probe 实现必然用 `is_dir()` 判断，此时 exists/writable/willCreate 该返回什么无定义。
**修复方案**:
1. 响应增加显式 `creatable: bool` 字段（仅「不存在且父目录可写」为 true），前端逻辑简化为：`exists && writable` → 直接提交；`!exists && creatable` → 弹确认；其余 → 红字 + `message`；
2. 表格补第 6 行：「存在但不是目录」→ `exists=true, writable=false, willCreate=false, creatable=false, message='路径已存在但不是目录：…'`。

### 🟠 高 2 — workDir 改必填后，前端拿不到「项目根路径」默认值

**位置**: webAppPlan.md §11.1「预填项目根路径占位」；webApiSpec.md §3.3/§3.4；对照 server.py `createSession`（现状 `workDirRaw is None → projectRoot`）
**问题**: v1 的默认 workDir 是**后端** `projectRoot`，前端从来不知道这个绝对路径。改必填后契约里没有任何端点暴露它，「预填项目根路径」无法实现——这是契约缺口不是文案问题。index.html 现状 placeholder「留空使用项目根目录」也将失效。
**修复方案**: 给 probeWorkDir 响应加 `defaultWorkDir` 字段（= 后端 projectRoot，任何合法请求都带回），前端打开弹窗时若输入框为空可先 probe 占位路径或直接展示。成本：契约加一个字段，实现一行。

### 🟠 高 3 — usage/series 桶聚合时区未定义

**位置**: webApiSpec.md §3.10；webAppPlan.md §11.4（timestamp 注释「ISO 8601 UTC」）
**问题**: 事实核对——jsonl 与索引时间戳均为 UTC（如 `2026-08-07T03:35:55+00:00`）。hour/day 桶按 UTC 还是服务器本地时区切，文档完全没说；label 样例 `2026-08-07 13` 无时区后缀。中国时区用户看「今天」的图，UTC 切日会差 8 小时，前后端各自实现必然对不上。这是接口语义级歧义。
**修复方案**: 契约明确一句：「桶按**服务器本地时区**的日历时/日/月切分，label 为本地时间」，SQL 用 `datetime(timestamp, 'localtime')` 或入库时同时存本地日期列。二选一，但必须写死。

### 🟠 高 4 — cost 计算丢失 provider 维度，byModel 只按 modelId 聚合

**位置**: webApiSpec.md §3.10；webAppPlan.md §11.4（usageTurns 表有 providerId 列，byModel key 是 modelId）
**问题**: cost「按 models.yaml 当前 cost 计算」需要 `(providerId, modelId)` 二元组才能在 yaml 里定位价格；两个 provider 下存在同 id 模型（完全可能，如都配 `deepseek-v4-flash`）时，byModel 按 modelId 合并会撞桶、cost 取哪家的价格无定义。另外「模型已从 yaml 删除/改名后历史记录 cost 怎么算」也未定义。
**修复方案**:
1. cost 计算明确按 `(providerId, modelId)` 查表，查不到（已删/未配）按 0 计并写入契约；
2. byModel 的 key 改为 `"providerId/modelId"`（`models` 列表同步），前端配色哈希同口径；若坚持按 modelId 展示，需声明合并时 cost 按各记录自身 provider 分别算再求和。

---

### 🟡 中 1 — 删会话后「卡片区」与「图表区」口径发散，文档未声明；总费用卡数据源未定义

**位置**: webAppPlan.md §11.4（删会话保留 usageTurns）+ webApiSpec.md §2.3/§3.9（卡片数据来自 sessions 索引）
**问题**: 顶部卡片（GET /api/usage）数据源是 sessions 索引——删会话即扣减；图表（usageTurns）「账单性质保留」——删会话不扣。**同一页面上方总数和下方图表对不上**，用户必困惑，而两份文档都没提这个必然出现的分叉。另外「第 5 张总费用卡」的数据源完全没定义：series 接口 hour 限 72h、day 限 90 天，全量费用只能靠 month=全部 求和，文档应写明。
**修复方案**: 契约/方案各补一句：「卡片=现存会话口径，图表=历史账单口径（含已删除会话），两者可不一致」，UI 在图表标题加小字注明；总费用卡定义为 month 粒度全量 cost 求和（或 series 响应加 `totalCost` 字段）。

### 🟡 中 2 — create 的 mkdir TOCTOU 兜底与操作顺序未定义

**位置**: webApiSpec.md §3.3；webAppPlan.md §11.1
**问题**: probe→create 的父目录复查已防一层（方向正确），但 create 内部「校验 → mkdir」之间仍有竞态与系统调用失真：① 父目录此刻被删 → `FileNotFoundError` 走 500 兜底而非中文 400；② 目录此刻被他人创建 → `FileExistsError`，应视为成功（若是目录）而非报错；③ `os.access(W_OK)` 对 root 用户/只读挂载判断失真 → mkdir 抛 `PermissionError` 同样应映射 400。④ 顺序：若先 mkdir 后做 providerId/yaml 预检，400 时会留下孤儿空目录。
**修复方案**: 契约补实现要求：先 providerId/yaml 预检、后 mkdir；mkdir 包 `try/except OSError` → 400 透传中文消息；`FileExistsError` 时 `is_dir()` 复核通过即继续。

### 🟡 中 3 — 「一轮对话一条」名不副实：confirm 链路拆条，行数对账验证不可达

**位置**: webAppPlan.md §11.4（表注释「一轮对话一条」、验证「usageTurns 行数/数值与 sessions 索引对账一致」）
**问题**: 写入挂在**泵线程终态**——一轮含工具确认的对话，stream 泵（confirmationRequired 终态）写一条 delta、confirm 泵再写一条，实际是「每个泵流一条」。聚合求和不受影响（设计本身没问题），但文档表述错误，且 §11.4 验证标准「行数对账一致」与回填粒度（按 assistantMessage 事件，比 turn 更细）叠加后**根本对不上行数**，验证项不可达。
**修复方案**: 表述改为「每个泵流终态写一条 delta（终态 usageTotal − 本流开始快照）」；验证项改为「token 总量对账：ΣusageTurns == Σjsonl 重放 == 索引现存会话 + 已删会话差值」。

### 🟡 中 4 — 回填的 providerId 来源与 SQLite 连接管理未定义

**位置**: webAppPlan.md §11.4 历史回填段；事实核对：jsonl `assistantMessage` 事件有 `timestamp/model/usage`，**无 providerId**
**问题**: ① usageTurns.providerId NOT NULL，但回填数据源里没有 providerId——只能从 sessions 索引按 sessionId 补，索引条目缺失（jsonl 残留/索引损坏）时无定义行为；② 泵线程是工作线程，`sqlite3` 连接默认 `check_same_thread=True` 不能跨线程共享，方案没提连接管理，实现者极易踩坑；③ 回填时机应写明在「开始服务前」执行，避免与泵线程写入并发。
**修复方案**: 文档补三句：providerId 从索引补、缺失则写空串（或跳过该文件并记 warning）；usageStore 每调用开短连接（或线程局部连接 + `check_same_thread=False`）；回填在 app startup 钩子、路由服务前完成。

### 🟡 中 5 — create 对「已存在目录」的 R_OK|W_OK|X_OK 校验是对现有代码的增量要求，验证项未覆盖

**位置**: webApiSpec.md §3.3「必须是目录且当前进程可读写进入」；对照 server.py 现状只查 `is_dir()`
**问题**: v1 已交付代码没有 os.access 校验，T7.1 的改造说明只强调 workDir 必填 + allowCreate，没点明「已存在分支也要补权限校验」这个 diff；§11.1 验证项也没有「存在但不可写目录 → 400」用例。漏改概率高。
**修复方案**: T7.1 描述与验证清单各加一条：已存在但不可写目录（如 `chmod 555` 的目录）probe 返回 `writable=false`、create 返回 400。

---

### 🔵 低 1 — 契约编号顺延漏改前端引用（用户预判属实，3 处）

**位置**: spec 顺延：pending §3.7→§3.8、usage §3.8→§3.9、GET/PUT models §3.9/§3.10→§3.11/§3.12
**问题**:
- `webApp/frontend/js/chatView.js:337` 注释「契约 §3.7/§5」→ 应为 §3.8；
- `webApp/frontend/js/usageView.js:5` 文件头「契约 §2.3/§3.8」→ 应为 §3.9；
- `webApp/frontend/js/settingsView.js:6` 文件头「契约 §2.4/§3.9/§3.10」→ 应为 §3.11/§3.12；
- spec 文档内部交叉引用（§2.2→§3.8 pending）已正确更新，无漏。
**修复方案**: T7.7 加子项「同步前端文件头/注释中的契约编号」，改上述 3 处。

### 🔵 低 2 — 侧栏浅色化只改 CSS 变量不够，存在硬编码深色残留

**位置**: webAppPlan.md §11.2；styles.css
**问题**: 不随变量走的硬编码深色：`.btn-new-session{color:#fff;border:1px solid #3a3a3c}`、`.session-action-btn:hover{color:#fff;background:#3a3a3c}`、`.sidebar-bottom{border-top:1px solid #2c2c2e}`。只改 `:root` 变量会留下深色边框/白字。「浅色系无残留深色样式」的验证项需要这些点一并处理。
**修复方案**: T7.3 范围显式列出上述选择器，一并变量化（`--sidebar-border` 等）。

### 🔵 低 3 — delta 快照取法有坑，写入顺序未约定

**位置**: webAppPlan.md §11.4「终态 usageTotal − 流开始时快照」
**问题**: 流开始时 conversation 可能尚未创建（`getConversation()` 有建会话/jsonl 的副作用），实现者若直接调它取快照会改变现有行为（空 jsonl 提前落盘）。另 usageTurns 写入与 sessions 索引回写的先后顺序未约定——先索引后库，崩溃丢账；先库后索引，索引可由 jsonl 重放补回。
**修复方案**: 文档补：快照沿用 `_writebackUsage` 同款读法（`agent.conversations.get(sessionId)` under `sessionLocksGuard`，None 记 0 快照）；顺序约定为先写 usageTurns 后回写索引。

### 🔵 低 4 — month=全部 的空桶补齐起点未定义

**位置**: webApiSpec.md §3.10
**问题**: hour/day 有固定窗口可补齐，month「全部」从何时开始补、无记录时返回什么，未定义。
**修复方案**: 补一句：month 桶范围 = 最早记录所在月 → 当前月；表无任何记录时 `buckets: []`、`models: []`。

### 🔵 低 5 — 设置页 tab 切换与未保存修改的交互未定义

**位置**: webAppPlan.md §11.3（provider tab 式切换）
**问题**: 现状 `settingsView.open()` 每次重拉 GET 重建工作副本。tab 化后若切 tab 触发重拉会丢未保存修改；「底部固定保存/重置栏」的重置语义也未定义（重置=重新 GET 丢弃修改？）。
**修复方案**: 补一句：tab 切换只切展示、不重拉数据，工作副本始终为全量 config；重置按钮 = 重新 GET 并提示丢弃未保存修改。

---

## 优点记录

- probe 无副作用 + create 侧复查父目录，「先探后建」的竞态防住了关键一层；
- 用量写入挂在泵线程终态、delta=终态−快照，confirm 拆流也能正确求和，方向正确；
- cost 查询时计算（而非入库时固化）让后补价格能回溯历史，且与 yaml 单一数据源一致；
- 删除会话保留 usageTurns 的「账单」定性合理（缺的只是与卡片口径的发散声明）；
- 无过度设计：单表无 ORM、空表一次性回填、byModel 为堆叠图刚需，均为最小方案；未发现有可砍的 speculative 设计。

## 修复优先级建议（Top 3）

1. **高 1 + 高 2（probe 契约）**：加 `creatable` 与 `defaultWorkDir` 两个字段、补「存在但非目录」行——不改前端三分支逻辑无法实现，阻塞 T7.2；
2. **高 3（时区）**：一句话契约，但不写死后端按 UTC、前端按本地猜，图表数据必然错位且事后难查；
3. **中 1（口径发散声明）**：删一次会话就会出现卡片与图表对不上的「bug 报告」，文档与 UI 文案先声明成本最低。
