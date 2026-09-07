# 会话历史统一从 ~/.flamingo 读写：修复方案

- Author: wilbur
- Version: 1.0
- Date: 2026-09-07
- Description: 根据用户澄清，终止以恢复为主的排查；直接修复运行时代码的索引读写路径，兼容尚存旧索引并验证。本阶段不重建已经删除的真实索引；记录默认模型审核通过及 166 项回归测试通过。

## 1. 目标与根因

用户要求：代码以后从 `~/.flamingo/` 读写 session 历史，不再依赖仓库 `webData/`。

正文路径已经正确；遗漏的是 `webApp/backend/sessionStore.py` 中仍固定到 `<repo>/webData/sessions.json` 的索引。会话列表、历史正文定位、模型配置、状态栏都依赖这个索引。

目标布局：

```text
~/.flamingo/logs/
├── usage.db
├── webData/
│   ├── sessions.json
│   └── <workDir映射目录>/<sessionId>.jsonl
└── cliData/<workDir映射目录>/<sessionId>.jsonl
```

## 2. 最小实施范围

1. `sessionStore.py` 复用 `logPaths.webLogsRoot`，令 `webDataDir` 和 `indexPath` 固定指向用户家目录。所有现有 CRUD 调用自然统一到新位置；正文和 usage.db 路径不变。
2. 首次读取时，仅当新索引不存在且旧索引存在，校验旧索引后原子写入新位置；旧文件保留不删。新索引一旦存在（包括空列表），始终以新索引为准，避免把旧索引中已删除会话反复恢复回来；不在每次读时合并旧数据。
3. 缺少新旧索引是合法新环境，返回空列表且不建目录；损坏、无权限、结构错误的索引明确报错，不当作空索引后覆盖。校验会话条目是具有非空字符串 sessionId 的字典，拒绝重复 ID；保留所有元数据字段。
4. 新索引与旧索引同时存在时，不覆盖或自动合并新索引；旧索引保留供手工核对。全局索引沿用项目既有单服务/单 worker 约束，不增加多进程写入功能。
5. 更新 README 与 API 契约的当前路径说明。旧日志迁移脚本仍需读取迁移源 `<repo>/webData/sessions.json`，这是一次性迁移输入，不是运行时存储；不改动该历史工具或历史方案。

## 3. 计划与验证

1. 写回归测试，先验证现有默认路径错误及旧索引兼容逻辑缺失。
2. 默认模型独立 `pi -p` 新窗口审核本方案，按反馈修订，再实施最小代码改动。
3. 实施路径与兼容读取改动。
4. 运行新测试与完整 pytest；验证默认路径、CRUD、正文读取、旧索引复制保留、迁移幂等、新索引优先、损坏索引不覆盖、原子写失败不损坏源文件。
5. 检查 diff，确认未覆盖用户已有修改、未写真实会话数据；交付准确说明需重启现有 Web 进程。

所有自动化测试只向临时目录写入；使用 uv 现有环境，不请求模型、不写真实 `~/.flamingo`、不创建仓库运行数据。

### 执行结果

- 已在独立 Terminal 窗口启动默认模型 `pi -p --no-session --no-tools ...`，只审核方案及相关源码；退出码 0。
- 审核原文：「通过。方案已覆盖旧索引一次性兼容、新索引优先、损坏索引显式报错且不覆盖，以及测试仅写临时目录等关键要求，无必须修订的问题。」无需技术方案修订。
- 实施前新测试：12 failed / 5 passed，默认路径断言明确复现仓库路径错误。
- 实施后新测试：17 passed；完整 `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync pytest -q -p no:cacheprovider`：166 passed。
- `git diff --check` 通过；仓库 `webData/` 仍不存在，真实家目录 `sessions.json` 未被测试创建。
- 运行时代码仅改 `webApp/backend/sessionStore.py`，并新增 `tests/testSessionStore.py`、更新当前路径文档。正文/数据库/服务进程均未改动。
- 具体问题与修复结果见 `docs/codeReview/260907_sessionIndexPathFix.md`。

## 4. 明确不做

- 不从 JSONL 自动重建已经删除的索引，不把日志文件数量等同于原历史会话数量。
- 不改动、格式化、移动、删除现存 JSONL 或 usage.db。
- 不停止、重启当前 Web 服务；交付时提醒用户重启，避免中断其活跃请求。
- 不改用户已有的 Agent、实时 usage、前端等未提交功能。

## 5. 已完成的只读调查（背景，不阻塞修复）

- 家目录有 63 个日志文件，FlamingoAgents 对应目录有 51 个；61 个为有效逐行 JSONL。
- 另 2 个扩展名为 jsonl、实际内容为完整 JSON 数组，数组可成功解析；本次不转换。
- usage.db 只读 quick_check 为 ok，包含上述 63 个 sessionId 的记录。
- 临时目录 unittest 3/3 通过，已复现“索引移走后列表/历史为空，而正文未变”。
