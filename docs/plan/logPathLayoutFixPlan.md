# 日志目录/文件名修订方案（logPathLayoutFixPlan）

Author: wilbur
Version: 1.3
Date: 2026-08-19

> 背景：`logPathMigrationPlan` v2.3 已落地。v1.1 把文件夹做成了嵌套真实路径（`webData/Users/wilbur/project/FlamingoAgents/`），**理解错了**。
> 用户要的是 **webData 下一层文件夹，名字就是路径字符串**：
> `/Users/wilbur/project/FlamingoAgents` → 文件夹名 `~-project-FlamingoAgents`。
> `/` 用 `-` 替换（不能用 `/`，也不用全角 `／`）。不是再建 `Users/wilbur/project/...` 这棵目录树。
>
> 本方案是 v2.3 的增量修订。sessionId 新格式（`YYMMDDHHmmss-xxxxxxxx`）维持 v1.1 已落地部分。

## 1. 现状 vs 目标

**现状（v1.1 已跑完 layout V2）**：

```
~/.flamingo/logs/webData/
├── Users/wilbur/project/FlamingoAgents/session_xxx.jsonl   # 错：嵌套树
├── Users/wilbur/project/prime-agent/...
├── private/tmp/verifyE2EWorkdir/...
├── FlamingoAgents-220279c1/     # 已空
└── ...
```

**目标（一层文件夹，名字=家目录缩写后的路径）**：

```
~/.flamingo/logs/
├── usage.db
├── webData/
│   └── ~-project-FlamingoAgents/     # 一层目录；/ 换成 -
│       └── session_046772d618e6.jsonl  # 旧会话文件名不动
│       └── 260819115719-a1b2c3d4.jsonl # 新会话
└── cliData/
    └── ~／project／FlamingoAgents/
```

Finder / `ls` 里看到的就是「路径当名字」。`/` 不能做目录名，用 `-` 替换。

| workDir | 文件夹名 |
|---|---|
| `/Users/wilbur/project/FlamingoAgents` | `~-project-FlamingoAgents` |
| `/Users/wilbur` | `~` |
| `/private/tmp/verifyE2EWorkdir` | `-private-tmp-verifyE2EWorkdir`（不在 $HOME 下，绝对路径的 `/` 也换 `-`） |
| `/` | `-` |

家目录判定：`resolved == home or home in resolved.parents`，再 `~` + `relative_to(home)`。

## 2. 设计决策

### 2.1 一层目录，名字=路径字符串

- `resolve()` 后：在 `$HOME` 下 → `~` 前缀；否则整段绝对路径。
- **`/` 一律换成 `-`**（用户拍板；不用全角 `／`）。`Path / name` 得到的是单个子目录。
- 例：`~-project-FlamingoAgents`、`-private-tmp-verifyE2EWorkdir`。

### 2.2 sessionId（已落地，不改）

`YYMMDDHHmmss-xxxxxxxx`；旧 `session_*` 不改。

### 2.3 再搬一次：layout V4（V3 已用全角 `／`，作废）

`.layoutV2Done` / `.layoutV3Done` 已写，那两步不再跑。新增 `migrateLayoutV4` + `.layoutV4Done` / `.layoutV4Partial`。

源目录（按优先级，命中第一个存在的）：

1. **V3 全角一层名**：`webData/~／project／FlamingoAgents`（当前）
2. **v1.1 嵌套树**：`webData/Users/wilbur/project/FlamingoAgents`
3. **v2.3 名字-hash**：`webData/FlamingoAgents-220279c1`

目标：`webData/~-project-FlamingoAgents/<sessionId>.jsonl`

CLI 同样三源。规则同前：源不存在跳过；目标已存在丢弃源；OSError → Partial；不删空旧目录。

`__main__`：`migrateAll` → V2 → V3 → **V4**（前三步已 Done 则跳过）。

### 2.4 红线不变

无 fallback；`sessions.json` 留仓库；前端零改；7c 独立脚本；不删残留空目录。

## 3. 改动清单

### 3.1 `logPaths.py` 1.2 → 1.3

`workDirFolderName` 末行改为 `return text.replace('/', '-')`。`newSessionId` 不动。

验证：仓库路径 → `~-project-FlamingoAgents`；`/` 不出现在名字里；`resolve*` 的 parent 仍是 `webData`。

### 3.2 `logMigration.py` 1.2 → 1.3

加 `_fullwidthLayoutFolderName`（V3 公式）+ `migrateLayoutV4`。V2/V3 保留（已 Done 空转）。

### 3.3 文档

README / webApiSpec：目录示例改为 `webData/~-project-FlamingoAgents/`。

### 3.4 不改

builder / agentManager / historyView / server / usageStore / `__main__.py` / 前端 / sessionId 生成。

## 4. TODO

1. [ ] `workDirFolderName` 改 `/` → `-`
2. [ ] `migrateLayoutV4` + `__main__` 串上
3. [ ] README + webApiSpec
4. [ ] **停 webApp** → `uv run python -m flamingoAgents.utils.logMigration` → 再启动
   → 当前会话在 `~/.flamingo/logs/webData/~-project-FlamingoAgents/`。

## 5. 风险

| 风险 | 缓解 |
|---|---|
| 运行中搬目录撕裂当前会话 | 必须先停 8787 |
| `/` 不能做目录名 | 换成 `-` |
| 前几版空目录残留（`Users/`、`~／...`） | 不自动删，用户手动清 |
| `.layoutV3Done` 挡住 V3 | 预期；V4 独立标记 |

## 6. 时序

```
停 webApp → uv run python -m flamingoAgents.utils.logMigration → 再启动
```
