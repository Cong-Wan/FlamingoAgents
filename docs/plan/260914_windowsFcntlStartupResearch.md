# Windows 启动失败：`fcntl` 调研记录

> Author: wilbur  
> Version: 1.0  
> Date: 2026-09-14  
> Description: 记录在 Windows 上执行 `uv run python -m webApp` 时 `ModuleNotFoundError: No module named 'fcntl'` 的根因、导入链、相关 POSIX API，以及项目现有平台边界。本次仅文档，不改源码、不实施方案。

## 范围与约束

- 用户原始问题是「怎么回事？」，要的是原因说明，不是补丁。
- 本文件只落档调研证据。不修改 `flamingoAgents/`、`webApp/`、`tests/`，不新增依赖，不提交。
- 项目既有方案已写明首版按单机 POSIX（macOS，可测 Linux）设计，不承诺 Windows。本记录不把 Windows 支持当成待实施任务。

## 复现

环境：Windows PowerShell，工作目录 `D:\company\FlamingoAgents`，CPython 3.14.7，`uv run`。

```powershell
$env:FLAMINGO_WEB_TOKEN = "flamingo"; uv run python -m webApp
```

结果：虚拟环境创建成功、依赖安装成功，随后在导入阶段崩溃，未进入 `webApp.__main__.main()`，uvicorn 未监听。

关键栈（节选）：

```text
webApp/__main__.py
  from flamingoAgents.utils.configPaths import ensureUserConfig
flamingoAgents/__init__.py
  from flamingoAgents.builder import createAgent
flamingoAgents/builder.py
  from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
flamingoAgents/models/chatCompletions.py
  from flamingoAgents.models.modelAuth import modelAuth
flamingoAgents/models/modelAuth.py
  from flamingoAgents.models.credentialStore import credentialStore
flamingoAgents/models/credentialStore.py
  import fcntl
ModuleNotFoundError: No module named 'fcntl'
```

## 根因

`fcntl` 是 CPython 的 Unix 专用标准库模块（文件描述符控制 / `flock`），Windows 发行版不包含该模块。这不是 `uv` 漏装包，也不是虚拟环境损坏。

失败发生在**模块顶层 import**，与是否真的读写 OAuth 凭据无关。只要导入 `flamingoAgents` 包就会炸：`flamingoAgents/__init__.py` 会拉取 `createAgent` → 模型适配器 → `modelAuth` → `credentialStore`。因此 `from flamingoAgents.utils.configPaths import ensureUserConfig` 也会间接触发（`configPaths.py` 自身零库内依赖，但包 `__init__.py` 会先执行）。

## 代码证据

### 顶层 `import fcntl`（启动即崩）

| 文件 | 用途 |
| --- | --- |
| `flamingoAgents/models/credentialStore.py` | `fcntl.flock(LOCK_EX)` / `LOCK_UN`，保护 `~/.flamingo/auth.json` 跨进程读写 |
| `flamingoAgents/models/subscriptionAuth.py` | `fcntl.flock(LOCK_EX \| LOCK_NB)` / `LOCK_UN`，OAuth 浏览器回调口占用租约 `oauthCallback.lock` |

`credentialStore` 还用同一把锁文件 `auth.lock` 包住读/写/删/modify；线程侧另有 `threading.RLock`（`fileThreadLocks` / `threadLocks`），进程间互斥完全依赖 `fcntl.flock`。

### 同一条凭据链上的其它 POSIX API（当前不是启动崩溃点）

这些在 Windows 上同样不存在或语义不同。现在被 `import fcntl` 挡在前面；若只拿掉 `fcntl` 导入，一碰到凭据目录/锁文件就会继续失败。

| API | 位置 | Windows 情况 |
| --- | --- | --- |
| `os.getuid()` | `credentialStore._validateOwnedType`；`subscriptionAuth.callbackLease.acquire` | 无此函数 |
| `pathStat.st_uid` | 同上，与 `getuid()` 比较 | 常为 0，不能当 POSIX 属主检查 |
| `os.fchmod(fd, 0o600)` | 锁 fd、临时凭据 fd | Unix only |
| `os.chmod(..., 0o700/0o600)` | 凭据目录/文件 | 只能影响只读标志，不保存 Unix mode |
| `os.chmod(..., follow_symlinks=False)` | 写完 `auth.json` 后 | Windows 上该参数不可用，会 `NotImplementedError` |
| `os.open(dir, O_RDONLY \| O_DIRECTORY)` + `os.fsync` | `_writeDocumentUnlocked` 目录持久化 | Windows 不能按 POSIX 打开目录做 fsync |
| `os.O_NOFOLLOW` | 打开锁/凭据文件 | 代码已 `getattr(..., 0)`，缺省时退化为 0 |
| `(st_dev, st_ino)` 替换检测 | 读取 `auth.json` | Windows 上 `st_ino` 等常为 0，检测无效但不一定抛错 |

### 启动之后才可能碰到的 POSIX 点（与本次崩溃无关）

| 位置 | 说明 |
| --- | --- |
| `flamingoAgents/tools/builtinTools.py` `_killProcessGroup` | `os.killpg`；已包在 `try/except` 里，失败则 `terminate()`/`kill()`，不是 import 崩溃 |
| `tests/testCredentialStore.py` | `multiprocessing.get_context('fork')`；Windows 无 fork。权限断言 `0o700/0o600`、symlink 拒绝用例也依赖 POSIX |
| 既有方案文档 | 明确不承诺 Windows 进程组、反斜杠路径等 |

## 项目平台边界（已有文档，不是新决策）

调研时仓库内已经写过 POSIX 优先、Windows 不承诺，例如：

- `docs/plan/260909_mcpPlan.md`：平台 = 单机 POSIX，单用户，Web 单 worker；不承诺 Windows 进程组。
- `docs/plan/260909_mcpTasks.md`：不测 Windows。
- `docs/plan/260909_workflowPlan.md`：首版单机 POSIX（当前 macOS，可测 Linux）；不承诺 Windows 进程组语义。
- `docs/plan/260909_workflowTasks.md`：Windows 未实现明确拒绝。
- `docs/plan/workDirPickerPlan.md` 等：Windows 反斜杠本版明确不做。

因此本次启动失败与「项目按 POSIX 单机写」一致，不是回归。

## 不改代码的可用路径

要在当前树原样运行 Web：

- 在 macOS / Linux 上跑；或
- Windows 上用 WSL2（Linux 用户态，有 `fcntl`）。

不要指望在原生 Windows 的 `uv run python -m webApp` 下通过导入。

## 若将来要原生 Windows（仅调研备忘，非方案、不实施）

最小障碍是**启动期顶层 import**。要让进程能进 `main()`，必须让 `credentialStore` / `subscriptionAuth` 在 Windows 上不要 `import fcntl`。

若还要凭据读写可用，还需要等价的跨进程互斥，以及决定如何处理属主/0600（Windows 无 Unix uid/mode；属主检查应跳过或改 ACL，而不是调用 `getuid`）。常见锁实现：

- 标准库：`ctypes` 调 `LockFileEx`（可锁空文件、可非阻塞）；或 `msvcrt.locking`（锁字节区间，空文件要先写入）。
- 第三方：`portalocker` / `filelock`（会增依赖，与当前「标准库锁」风格不一致）。

这些都不在本次范围。未实施，也没有未提交的业务补丁。

## 校验

- `flamingoAgents/models/credentialStore.py`、`flamingoAgents/models/subscriptionAuth.py`、`tests/testCredentialStore.py` 已回到本记录撰写时的 git HEAD 内容（仍为顶层 `import fcntl`）。
- 不存在 `flamingoAgents/utils/fileLock.py`。
- 工作区相对上述文件无未提交业务改动。
