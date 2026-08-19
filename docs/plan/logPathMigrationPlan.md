# 日志存放路径迁移方案（logPathMigrationPlan）

Author: wilbur
Version: 2.3
Date: 2026-08-17

> **v2.0：用户三点决策驱动的结构性修订——**
> 1. **usage.db 也迁到 `~/.flamingo/logs/`**（原方案留在仓库 webData）；
> 2. **旧数据做一次性迁移（one-shot migration），不做每次启动的 fallback 查找；迁移不了的直接丢弃**——v1.x 的「保留旧文件 + 读侧三级 fallback」整套设计**全部废除**，方案大幅简化；
> 3. 子文件夹命名维持「名字-短hash」（D 方案，不变）。
>
> v1.x 历史见文末附录（仅存档，不再执行）。

## 1. 背景与目标

当前 session jsonl 日志与 usage.db 的路径：

| 数据 | 现状路径 | 问题 |
|---|---|---|
| CLI session jsonl | `<workDir>/.agentLogs/` | 散落在每个 workDir，污染用户项目目录 |
| Web session jsonl | `<repo>/webData/sessionLogs/`（扁平） | 绑死仓库；所有 workDir 的 session 混在一起 |
| usage.db | `<repo>/webData/usage.db` | 绑死仓库 |

**目标结构**：

```
~/.flamingo/
└── logs/
    ├── usage.db            # 用量统计库（webApp）
    ├── webData/            # webApp 产生的 session 日志
    │   └── <workDir标识>/
    │       └── session_xxx.jsonl
    └── cliData/            # CLI/SDK 产生的 session 日志
        └── <workDir标识>/
            └── session_xxx.jsonl
```

- 同一 workDir 的 session 收敛到同一子文件夹。
- 仓库内不再出现 `.agentLogs` / `webData/sessionLogs` / `webData/usage.db`。
- `webData/` 仅剩 `sessions.json`（会话索引，应用数据，留仓库）。

## 2. 关键设计决策

### 2.0 已核实的关键代码事实（前两轮审核产物）

- **sessionId 由调用方生成**：webApp 是 `sessionStore.createSession`；CLI 是 `agent.createSessionId()`。`agent.getConversation` 用入参 sessionId 拼 `logDir / f'{sessionId}.jsonl'`。
- **`jsonlLog.__init__` 会 `mkdir(parents=True, exist_ok=True)`**（jsonl.py L21-23）：读侧定位不能经 jsonlLog，否则误建目录。
- **`jsonlLog.readEvents()` 对不存在文件返回 `[]`**（jsonl.py L34-35）。
- **单 agent 实例只持有一个 logDir**（agent.py L65 构造注入）：同一 session 的 jsonl 只会存在于一处。
- **webApp 启动入口** `webApp/__main__.py`：uvicorn 启动前调用 `initUsageDb()`（建表 + 空表回填）——**一次性迁移挂在这里**。

### 2.1 workDir → 子文件夹名映射（「名字-短hash」，用户已确认）

`{basename}-{sha1(resolvedWorkDir)[:8]}`，如 `FlamingoAgents-a1b2c3d4`。

- sanitize：仅保留 `[A-Za-z0-9._-]`，其余替换为 `-`，连续 `-` 折叠，去首尾 `-/.`；空串退化 `'root'`；截断 40 字符。
- hash 输入 `Path(workDir).resolve()` 字符串（UTF-8），同一物理目录恒等。
- basename 冲突由 hash 兜底（hash 输入是完整路径）。

### 2.2 一次性迁移策略（用户决策，替代 v1.x 的 fallback）

**核心原则：启动时一次性把旧数据搬到新位置；日志类（jsonl）搬不了的直接丢弃，账单类（usage.db）目标已存在则 merge 不丢账（审核 B1）；OSError 瞬时失败记 `.migrationPartial` 下次重试（审核 M1）。不保留 fallback、不每次启动查找。**

迁移内容三类：

| 数据 | 源 | 目标 | 冲突/失败处理 |
|---|---|---|---|
| webApp jsonl | `webData/sessionLogs/*.jsonl` | `~/.flamingo/logs/webData/<workDir标识>/` | 见下 |
| CLI jsonl | 各 workDir 的 `.agentLogs/*.jsonl` | `~/.flamingo/logs/cliData/<workDir标识>/` | 见下 |
| usage.db | `webData/usage.db` | `~/.flamingo/logs/usage.db` | 目标不存在 → move；**已存在 → 全字段去重 merge，不丢弃**（审核 B1） |

**webApp jsonl 迁移**：遍历 `webData/sessionLogs/*.jsonl`，按 `sessions.json` 索引查 sessionId 得 workDir → 算目标子文件夹 → `shutil.move`。
- 索引里查不到 sessionId（会话已删但文件残留）→ **丢弃**（无法确定 workDir 归属）。
- 目标已存在同名文件 → **丢弃源文件**（不覆盖，新数据优先）。

**CLI jsonl 迁移**：CLI 无索引，无法从 sessionId 反查 workDir。扫描范围 = **当前用户指定的若干 workDir**（见 §6 待确认 Q1；接口设计为 `cliWorkDirs: list[Path] = ()`，空列表即跳过本步——审核 M2，无论 Q1 选哪个代码都不用改），对每个 `<workDir>/.agentLogs/*.jsonl` → 算 cliData 目标子文件夹 → `shutil.move`。
- 目标已存在同名 → **丢弃源文件**。
- 不在扫描列表里的 workDir → 其 `.agentLogs` 不迁移（自然丢弃）。

**usage.db 迁移**（审核 B1 修订，**不再「目标已存在就丢弃源」**）：
- 目标不存在 → `shutil.move` 直接搬；
- **目标已存在 → 按全字段去重 merge**：打开源库与目标库，`INSERT OR IGNORE`（attach 后按除自增 id 外全字段去重），merge 完成后删源库。**绝不丢弃旧账**——usage 是账单数据，静默丢失用户可感知；
- 依据（审核 B1）：`usageStore.getConnection` 空表也会物理建库文件，新代码跑过一次目标就必然存在，「丢弃源」会让旧账永远进不了新库；且 `backfillFromLogs` 的存在本身证明「账可能只在 jsonl」是真实历史状态，不能假设旧库可丢；
- sqlite **未开 WAL**（已核实无 `-wal`/`-shm` 伴生文件），直接 move 单文件即可。

**迁移执行标记**（审核 M1 修订）：
- 全部文件处理完毕**且无 OSError 失败跳过** → 写 `~/.flamingo/logs/.migrationDone`（含迁移时间 + moved/dropped 计数），启动检测到 → 跳过；
- 存在 **OSError 导致的失败跳过**（瞬时权限/锁）→ **不写 `.migrationDone`，改写 `.migrationPartial`**（记录失败清单），下次启动**重试**；
- **「索引查不到的合理丢弃」不计入失败**（确定性决策，重试无意义），只有 OSError 瞬时失败才阻断完成标记；
- 迁移幂等：重复执行无害（目标已存在则跳过/merge，不重复）。

**迁移失败兜底**：单个文件迁移抛 OSError → 记 warning、记入 `.migrationPartial` 待重试清单、继续其余（不中断启动）。瞬时失败不静默丢弃（审核 M1）。

### 2.3 不变更的部分（精准修改红线）

- `webData/sessions.json` 留仓库不动（应用数据，不是日志）。
- `agent.py` 的 `logDir` 入参语义不变：调用方给什么目录写什么目录。
- 前端零改动。

## 3. 改动清单（按文件）

### 3.1 新增 `flamingoAgents/utils/logPaths.py`

唯一路径知识中心。**读侧 `resolve*`（纯计算零副作用）/ 写侧 `ensure*`（计算+mkdir）物理拆分**（一轮 Blocker 修复，仍适用）：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-08-17
Description: 集中管理 session 日志根目录（~/.flamingo/logs/{webData,cliData}/<workDir标识>/）
            与 usage.db 路径，提供 workDir → 子文件夹名（basename + sha1 短哈希）的恒等映射；
            resolve* 纯计算（读侧，零副作用），ensure* 计算+mkdir（写侧）。
'''

from pathlib import Path
import hashlib
import re

flamingoHome = Path.home() / '.flamingo'
logsRoot = flamingoHome / 'logs'
webLogsRoot = logsRoot / 'webData'
cliLogsRoot = logsRoot / 'cliData'
usageDbPath = logsRoot / 'usage.db'

_categoryRoots = {'webData': webLogsRoot, 'cliData': cliLogsRoot}


def sanitizeName(name: str) -> str:
    cleaned = re.sub(r'-+', '-', re.sub(r'[^A-Za-z0-9._-]', '-', name)).strip('-.')
    return (cleaned or 'root')[:40]


def workDirFolderName(workDir: Path) -> str:
    resolved = str(workDir.resolve())
    digest = hashlib.sha1(resolved.encode('utf-8')).hexdigest()[:8]
    return f'{sanitizeName(workDir.name)}-{digest}'


def resolveSessionLogDir(category: str, workDir: Path) -> Path:
    # 读侧专用：纯计算，不 mkdir
    return _categoryRoots[category] / workDirFolderName(workDir)


def ensureSessionLogDir(category: str, workDir: Path) -> Path:
    # 写侧专用：计算 + mkdir；category 非法 KeyError
    folder = resolveSessionLogDir(category, workDir)
    folder.mkdir(parents=True, exist_ok=True)
    return folder
```

验证点：同 workDir 恒等；异路径同名不冲突；`'///'`/特殊字符 → `'root'`+hash；`resolve*` 零副作用；`category='typo'` 抛 KeyError。

### 3.2 新增 `flamingoAgents/utils/logMigration.py`（独立脚本，7c 决策）

一次性迁移逻辑，**不挂 `__main__.py` 启动路径，改为手动跑一次的独立脚本**（§7 选 7c）。best-effort、单文件失败不中断、瞬时失败可重试：

- 文件含 `if __name__ == '__main__':` 入口 + `migrateAll(...)` 函数；
- 运行方式：`uv run python -m flamingoAgents.utils.logMigration`；
- `cliWorkDirs` 在 `__main__` 入口直接写死 `[Path('/Users/wilbur/project/FlamingoAgents')]`（§6-1 选 a，一次性迁移不需要参数化）；
- 迁移完成后 `.migrationDone` 落盘，脚本即完成使命（§7）；
- 内部实现（usage.db merge / jsonl move / partial 重试）同 v2.1 §3.2 描述，不变。

验证点（同 v2.1 §3.2）：①二次运行幂等跳过；②usage.db merge 去重不丢账；③jsonl 同名不覆盖；④索引查不到的丢弃；⑤OSError 写 `.migrationPartial` 下次重试。

### 3.3 `flamingoAgents/builder.py`

- 当前：`resolvedLogDir = ... else workDirPath / '.agentLogs'`
- 改为：`... else ensureSessionLogDir('cliData', workDirPath)`（写侧）。
- 显式传 logDir 行为不变。
- **CLI 侧无 fallback**（v1.x 已论证，v2.0 维持）：CLI 旧日志由 `logMigration` 一次性搬迁，运行时不再回查。
- 文件头 Version → 1.6。

### 3.4 `webApp/backend/sessionStore.py`

- `sessionLogsDir = webDataDir / 'sessionLogs'`：**删除**（迁移完成后该目录不再被运行时引用；迁移代码从 `logMigration` 拿旧路径，不经 sessionStore）。
- 仅保留 `webDataDir`（sessions.json 仍在用）。
- 文件头版本递增，Description 说明移除 legacy 常量。

### 3.5 `webApp/backend/agentManager.py`

- `getAgent`：`logDir=sessionLogsDir` → `logDir=ensureSessionLogDir('webData', Path(meta['workDir']))`。
- 删掉对 `sessionLogsDir` 的 import。
- **不需要** `getAgentWorkDir`（v1.x 为 fallback 兜底设计，v2.0 无 fallback，索引即唯一权威）。
- 文件头版本递增。

### 3.6 `webApp/backend/historyView.py`

- 当前：`logPath = sessionLogsDir / f'{sessionId}.jsonl'`，不知 workDir。
- 改为：`meta = sessionStore.getSession(sessionId)` 取 workDir → `resolveSessionLogDir('webData', Path(meta['workDir'])) / f'{sessionId}.jsonl'` → **`if not logPath.exists(): return []` 门控（不构造 jsonlLog，零 mkdir）** → 存在才 `jsonlLog(logPath).readEvents()`。
- meta 为 None（索引没有）→ 直接返回 []（无 fallback）。
- 文件头版本递增。

### 3.7 `webApp/backend/server.py`

- 删除会话路由：`meta = sessionStore.getSession(sessionId)` 先取 → `sessionStore.deleteSession` → `resolveSessionLogDir('webData', Path(meta['workDir'])) / f'{sessionId}.jsonl'` 删文件（`unlink(missing_ok=True)` 包 try/except OSError，失败只 warning 不 500）→ `agentManager.dropAgent`。
- grep 确认无 `sessionLogsDir` 残留。
- 文件头版本递增。

### 3.8 `webApp/backend/usageStore.py`

- `dbPath = sessionStore.webDataDir / 'usage.db'` → `dbPath = usageDbPath`（从 logPaths import）。
- `getConnection` 里 `dbPath.parent.mkdir(...)` 改为 `logsRoot.mkdir(parents=True, exist_ok=True)`（确保 `~/.flamingo/logs/` 存在）。
- **`backfillFromLogs` 整段删除**：旧 usage.db 由 `logMigration` 搬到新位置（不存在则 move、存在则 merge），历史账随库走，无需再从 jsonl 重建；全新用户空表即正确状态。此删除同时**消除 usageStore 对 `sessionStore.sessionLogsDir`（L81）的唯一引用**（审核 m1），与 §3.4 删常量配套。
  - 「usage.db 缺失但 jsonl 还在」的账：经 B1 修订后 usage.db 必被 move/merge 到新位置，不会再出现「旧库有账但被丢弃」；jsonl 本身不再作为账单数据源（符合「做不了就不要」）。
- 文件头版本递增。

### 3.9 `webApp/__main__.py` —— **不改动**（7c 决策）

- v2.0/v2.1 原计划在 `initUsageDb()` 前挂 `migrateAll`，**7c 决策后撤销**：迁移改为独立脚本手动跑（§3.2），`__main__.py` 保持原样。
- 启动路径零新增代码，`initUsageDb()` 仍只做建表（§3.8 删 backfill 后）。
- **时序约束**（7c 固有代价，§7 已论证）：必须先手动跑 `python -m flamingoAgents.utils.logMigration` 完成迁移，再启动新代码的 webApp/CLI；否则旧日志/旧 usage.db 不会被自动搬迁，webApp 看到的是空历史、CLI 旧日志被冻结。此约束写进 README。

### 3.10 文档/README

- README L88 `webData/ ... 集中 jsonl 日志、usage.db` → 更新为「`webData/` 现仅含会话索引 `sessions.json`；日志与 usage.db 迁至 `~/.flamingo/logs/`」。
- L28 等涉及路径口径顺带对齐。
- 本方案归档。

## 4. TODO list（执行顺序）

1. [ ] 新建 `flamingoAgents/utils/logPaths.py`（`resolve*`/`ensure*` 拆分 + `usageDbPath`）
   → 验证：REPL 断言同 workDir 恒等；异路径同名不冲突；`'///'`/特殊字符 → `'root'`+hash；`resolve*` 零副作用；`category='typo'` 抛 KeyError。
2. [ ] 新建 `flamingoAgents/utils/logMigration.py`（一次性迁移 + `.migrationDone`/`.migrationPartial` 标记）
   → 验证：造假的旧 webData/sessionLogs + 旧 usage.db + 假 CLI `.agentLogs` 跑 `migrateAll`——①文件搬到正确新位置；②二次运行跳过；③jsonl 目标同名不覆盖；④usage.db 目标已存在时 merge 去重、旧账不丢（审核 B1 专项）；⑤索引查不到的文件被合理丢弃且不阻断标记；⑥单文件权限错误 → 写 `.migrationPartial` 下次重试（审核 M1 专项）。
3. [ ] 改 `builder.py` 默认 logDir
   → 验证：REPL `createAgent(workDir=tmp)` 发消息，jsonl 落 `~/.flamingo/logs/cliData/<folder>/`；显式 logDir 不变。
4. [ ] 改 `sessionStore.py` 删 `sessionLogsDir`
   → 验证：grep 无残留引用，索引 CRUD 不变。
5. [ ] 改 `agentManager.py` 按 workDir 注入 logDir
   → 验证：webApp 新建会话发消息，jsonl 落 `~/.flamingo/logs/webData/<folder>/`。
6. [ ] 改 `historyView.py` 按索引 workDir 定位 + `exists()` 门控
   → 验证：①新 session 正常；②**POST 会话立刻 GET messages（未发消息）→ 返回空且 `~/.flamingo` 不建空目录**（二轮 M1 专项）；③索引没有的 sessionId → 返回空。
7. [ ] 改 `server.py` 删除路由 + grep 清残留
   → 验证：删 session 后对应 jsonl 被删；unlink 抛 OSError 时接口仍 200 仅 warning。
8. [ ] 改 `usageStore.py` dbPath 指向新位置 + 删 `backfillFromLogs`
   → 验证：`~/.flamingo/logs/usage.db` 被创建/打开；无 backfill 调用。
9. [ ] ~~改 `__main__.py` 挂载迁移~~ → **撤销（7c）**，改为「迁移脚本使用说明 + 时序约束」写入 README
   → 验证：README 有明确步骤「先 `uv run python -m flamingoAgents.utils.logMigration`，再启动 webApp/CLI」；端到端手动跑脚本后启动 webApp，旧 jsonl/usage.db 就位、历史消息/用量正常、`.migrationDone` 生成。
10. [ ] README 口径 + 全部改动文件头版本号递增
    → 验证：grep 全仓库（排除 .venv）无 `.agentLogs`/`sessionLogs`/旧 usage.db 路径陈旧描述。

> 全程手工/REPL/界面验证，不写任何测试框架。

## 5. 风险与边界

| 风险 | 影响 | 缓解 |
|---|---|---|
| 同一机器多用户 | `~/.flamingo` 按 OS 用户隔离 | 无需处理 |
| workDir 被 rename | rename 后 `resolve()` 变 → hash 变 → **旧文件夹下历史对该 session 不可再检索**（无 fallback），属已知损失（审核 M3 修正：不是「留在那可选读」，是找不回） | 需用户确认接受（§6 Q4）；文档明确说明 |
| Windows 路径分隔符 | `resolve()` 含 `\`，hash 跨平台不一致 | 单机单用户无碍；`resolve()` 为非严格模式，workDir 不存在也不抛错（审核 n1） |
| 存量 session 切换 logDir | 进行中的流仍写旧目录 | agent 懒建+缓存，流持有旧 logDir 直到重建，无割裂 |
| `~/.flamingo` 权限 | mkdir 抛错 | 与现状同级，不额外处理 |
| 迁移时序错位（7c 特有） | 忘先跑迁移脚本就启用新代码 → 旧日志/旧账不会被自动搬迁，webApp 看空历史、CLI 旧日志冻结 | README 醒目标注「先跑迁移脚本再启用」；脚本可重复跑（幂等），发现遗漏后补跑即可 |
| 迁移部分失败 | 个别文件未搬 | best-effort：OSError 记 warning + `.migrationPartial` 下次重跑脚本重试（审核 M1）；脚本手动重跑无成本 |
| 迁移后旧目录残留 | `webData/sessionLogs`、`.agentLogs` 空目录/被丢弃文件 | 见 §6 Q2（是否清理） |
| `.migrationDone` 已写但数据残缺 | OSError 跳过的文件永久失联 | **已消除**：全部成功才写 `.migrationDone`，有 OSError 改写 `.migrationPartial` 触发重试（审核 M1） |
| `.migrationDone` 被误删 | 重复迁移 | 迁移幂等（目标已存在则跳过/merge），重复执行无害 |
| 旧 usage.db 与新空库并存 | 旧账丢失 | **已消除**：目标已存在则全字段去重 merge，不丢弃（审核 B1） |

## 6. 决策记录（用户已拍板，v2.2 定稿）

1. **CLI 旧日志迁移范围** → **选 a：只迁移当前仓库 `/Users/wilbur/project/FlamingoAgents/.agentLogs`**。
   落地：`__main__.py` 挂 migrateAll 时 `cliWorkDirs=[Path('/Users/wilbur/project/FlamingoAgents')]`。
   ⚠️ 注意：此硬编码路径仅服务于「这台机器这一次迁移」，属一次性代码（见 §7）。
2. **迁移后旧目录残留** → **保留，用户手动删**。迁移代码**不删**任何旧文件/旧目录：`shutil.move` 成功的源文件自然消失；被丢弃/跳过的源文件**原样留在** `webData/sessionLogs/`、`.agentLogs/`，由用户事后手工清理。迁移代码不做任何 `rmdir`/删残留动作。
3. ~~usage.db 是否搬 `-wal`/`-shm` 伴生文件~~ → 已核实未开 WAL，无伴生文件，作废。
4. **workDir 被 rename 后旧日志不可检索**（无 fallback）→ **接受**。

## 7. 一次性迁移代码的生命周期（回答「logMigration 只用一次吧？」）

**是的，`logMigration.migrateAll` 本质是一次性代码**——它的价值只在「本机从旧结构迁到新结构」那一次启动。`.migrationDone` 落盘后，之后每次启动它都在第 0 步直接 return，成为死代码。

但这带来一个矛盾，需要你决策：

- **矛盾点**：migrateAll 挂载在 `webApp/__main__.py` 启动路径上。若代码一直留在仓库，则——
  - 对**你这台机器**：迁过一次后永远走「已有 .migrationDone → return」，是无害的死代码；
  - 对**任何新环境/新用户**（ fresh `~/.flamingo`、无旧数据 ）：migrateAll 也跑，但三个源都不存在，等于空转一次写个 `.migrationDone`，同样无害。
  - 也就是说：**留着不会错，只是冗余**。

- **三个选项**：

| 选项 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **7a. 长期保留（推荐）** | migrateAll 常驻 `__main__.py`，靠 `.migrationDone` 幂等跳过 | 任何环境「有旧数据就迁、没有就跳过」，零维护；万一你换机器/重装，旧数据仍能自动迁 | 仓库里长期带一段一次性逻辑（约几十行） |
| 7b. 迁完即删 | 本机迁移成功、确认无误后，再提交一个 commit 把 logMigration.py 和 __main__ 挂载删掉 | 仓库最终干净 | 需二次操作；换机器/重装就失去迁移能力（得靠 git 历史找回） |
| 7c. 独立脚本 | migrateAll 不挂 `__main__.py`，改成 `python -m flamingoAgents.utils.logMigration` 手动跑一次的脚本 | 启动路径干净 | 你得记得手动跑；且 CLI 侧的 builder.py 默认 logDir 一改，**旧 CLI 日志若不先迁就会被新结构"冻结"**——脚本必须在启用新代码的同一时机跑，时序靠人保证 |

**我的推荐：7a 长期保留**。理由：①逻辑有 `.migrationDone` 幂等保护，留着零副作用；②几十行成本换「任何环境自动适配旧数据」很值；③避免 7c 的「人忘了先迁移就启用新代码 → CLI 旧日志被冻结」时序坑。

> 若你选 7a：方案 §3.2/§3.9 维持现状，无需改。
> 若你选 7b/7c：我再补一份「迁移后清理/脚本化」的增量 TODO。

**决策：7c 独立脚本**。方案已按 7c 修订（§3.2 脚本化、§3.9 不改动、§5 补时序风险、TODO 第 9 项改写）。**方案最终定稿，可落地。**

---

## 附录：v1.x 历史（存档，不再执行）

- **v1.0**：初版，读侧 fallback + `sessionLogPath` 放 sessionStore。一轮审核出 Blocker（读侧 mkdir 污染）+ M1（索引单点）+ M2（usageStore 去重丢账）+ 4 Minor。
- **v1.1**：修一轮——`resolve/ensure` 拆分、双候选定位、usageStore 三源扫描。二轮审核出 M1（historyView 建空目录）+ M2（扫 CLI `.agentLogs` 不合理）。
- **v1.2**：修二轮——`exists()` 门控、撤回 CLI 扫描。三轮审核确认可落地。
- **v2.0**：用户决策推翻 v1.x 的 fallback 路线，改一次性迁移 + usage.db 同迁，全文重写。
- **v2.1**：kimi/k3 一轮审核——B1（usage.db 丢弃源丢账 → 改 merge）、M1（`.migrationDone` 幂等洞 → `.migrationPartial` 重试）、M2（Q1 未定 → `cliWorkDirs=()` 空列表跳过）、M3（rename 风险描述修正）；WAL 伴生文件确认为伪需求删除。确认轮判定「可落地」。
- **v2.2**：用户四项决策落档（§6）+ 新增 §7 一次性迁移代码的生命周期决策。
- **v2.3**：用户选 7c（独立脚本）——§3.2 改 `python -m` 手动跑 + `__main__` 入口写死 cliWorkDirs；§3.9 撤销启动挂载；§5 补「迁移时序错位」风险；TODO 第 9 项改为 README 时序说明。
