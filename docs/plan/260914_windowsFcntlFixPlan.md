# Windows `fcntl` 启动崩溃修复方案

> Author: wilbur  
> Version: 1.0  
> Date: 2026-09-14  
> Description: 让原生 Windows 能导入 `flamingoAgents` 并启动 `python -m webApp`，凭据文件锁与读写不因 POSIX API 崩溃。POSIX 行为保持不变。确认前不实施。

配套：[调研](260914_windowsFcntlStartupResearch.md)

状态：**已实施**（D1/D2 已确认；README 增加 Windows 操作指引）。

---

## 0. 目标与非目标

### 0.1 目标（修完用户应感知到）

1. 原生 Windows（本次复现：CPython 3.14 + PowerShell）执行  
   `$env:FLAMINGO_WEB_TOKEN="flamingo"; uv run python -m webApp`  
   能越过 import，进入 `webApp.__main__.main()`，不再 `ModuleNotFoundError: No module named 'fcntl'`。
2. Windows 上 `credentialStore` 读/写/删 `auth.json` 不因 `fcntl` / `getuid` / `fchmod` / 目录 `fsync` 崩溃；同进程内线程锁语义不变。
3. macOS / Linux 路径仍走 `fcntl.flock`、uid 属主检查、`fchmod`、目录 `fsync`，测试断言与现网行为不变。
4. 不新增第三方依赖（继续标准库）。

### 0.2 非目标（本期不做）

| 项 | 说明 |
|----|------|
| 把 Windows 升为一等公民 | MCP/workflow/路径反斜杠/进程组仍按既有方案「不承诺」 |
| `os.killpg` / 进程组 | `builtinTools._killProcessGroup` 已有 `terminate` 退化，与本次启动崩溃无关 |
| Windows ACL 替代 0600 | NTFS 没有 Unix mode；本期只跳过无效检查，不引入 win32security |
| 改 `flamingoAgents/__init__.py` 延迟导入 | 只推迟爆炸点，第一次碰模型认证仍会崩 |
| 第三方锁库 | 不引入 `portalocker` / `filelock` |
| 用 WSL 代替修复 | 那是不改代码的权宜之计，不是本方案 |
| 符号链接 / fork 跨进程测试在 Windows 上补齐 | 无 fork；symlink 需开发者模式。Windows 跳过即可 |

### 0.3 成功标准

- [ ] Windows：`uv run python -c "from flamingoAgents.utils.configPaths import ensureUserConfig"` 成功
- [ ] Windows：`uv run python -c "from webApp.backend.server import app"` 成功
- [ ] Windows：临时目录上 `credentialStore.writeCredential` / `readCredential` 往返成功
- [ ] POSIX：`uv run pytest tests/testCredentialStore.py tests/testSubscriptionAuth.py` 全过，权限 0700/0600、symlink 拒绝、fork 跨进程写入仍测
- [ ] `pyproject.toml` 依赖列表不变

---

## 1. 问题复述

`credentialStore.py` 与 `subscriptionAuth.py` **模块顶层** `import fcntl`。Windows 标准库没有该模块。  
`python -m webApp` → `flamingoAgents/__init__.py` → `createAgent` → 模型认证 → `credentialStore`，启动即崩，与是否登录 OAuth 无关。

只删 `import fcntl` 不够：第一次 `_locked()` / `callbackLease.acquire()` 还会撞 `os.getuid`、`os.fchmod`、目录 `os.open`+`fsync`。

证据见调研文档，不重复粘贴。

---

## 2. 决策

### 2.1 方案内已拍板（由目标直接推导）

| # | 决策 | 说明 |
|---|---|---|
| A1 | 抽一层标准库文件锁，两处共用 | `credentialStore._locked` 与 `subscriptionAuth.callbackLease` 都是「对一个 lock 文件做排他 flock」 |
| A2 | POSIX 继续 `fcntl.flock` | 不换锁语义、不换测试 |
| A3 | Windows 用 `LockFileEx`，不用 `msvcrt.locking` | 锁文件常为空；`msvcrt.locking` 锁字节区间，空文件会失败。`LockFileEx` 可锁整个可能文件范围，并支持 `LOCKFILE_FAIL_IMMEDIATELY`（对标 `LOCK_NB`） |
| A4 | 不增依赖 | `ctypes` + `msvcrt.get_osfhandle` 即可 |
| A5 | Windows 跳过 uid / `fchmod` / 目录 fsync | 无 POSIX 属主与 mode；检查失败或 API 缺失会误伤合法用户 |
| A6 | 符号链接与「必须是文件/目录」检查两边都保留 | `lstat` + `S_ISLNK` / `S_ISREG` / `S_ISDIR` 在 Windows 可用 |
| A7 | 线程侧 `RLock` 不动 | 同进程已靠 `fileThreadLocks` 串行化；Windows 上同进程多句柄等锁可能死等，必须继续先拿线程锁再拿文件锁 |

### 2.2 待你确认

| # | 决策点 | 建议 | 若否 |
|---|---|---|---|
| D1 | 本期是否允许原生 Windows 启动 Web，同时**不**宣称完整 Windows 支持？ | **是**（已确认）。只修启动 + 凭据锁。文档里 MCP/workflow「不承诺 Windows 进程组」保持 | 维持现状，只用 WSL；本方案作废 |
| D2 | Windows 凭据文件是否接受「无 0600、无 uid 属主」？ | **是**（已确认）。单机单用户；不引入 ACL | 要做 Windows ACL 则另立专题，本期仍不能启动 |

README「快速开始」增加 Windows PowerShell 操作指引；OAuth 凭据一句注明 Windows 无 0700/0600。

---

## 3. 设计

### 3.1 新模块 `flamingoAgents/utils/fileLock.py`

只负责排他锁，不管 uid/mode。`configPaths.py` 继续零库内依赖，不 import 本模块。

```python
def lockExclusive(fd: int, *, nonBlocking: bool = False) -> None: ...
def unlock(fd: int) -> None: ...
```

- POSIX：`fcntl.flock(fd, LOCK_EX [| LOCK_NB])` / `LOCK_UN`。`fcntl` 只在非 Windows 分支 import。
- Windows：`LockFileEx` / `UnlockFileEx`，`OVERLAPPED` 偏移 0，长度 `0xFFFFFFFF, 0xFFFFFFFF`。
  - 阻塞：`LOCKFILE_EXCLUSIVE_LOCK`
  - 非阻塞：再加 `LOCKFILE_FAIL_IMMEDIATELY`；`ERROR_LOCK_VIOLATION(33)` / `ERROR_LOCK_FAILED(167)` 抛 `BlockingIOError`（是 `OSError` 子类，现有 `except OSError` 仍能接住回调租约）
  - `unlock` 遇到 `ERROR_NOT_LOCKED(158)` 视为已解锁，不抛

调用方必须在持锁期间保持 fd 打开，`finally` 里先 `unlock` 再 `os.close`（与现在 flock 用法相同）。

### 3.2 `credentialStore.py`：平台差异收敛到两个小函数

```python
def currentUid() -> int | None:
    getter = getattr(os, 'getuid', None)
    return None if getter is None else getter()

def applyFdMode(fd: int, mode: int) -> None:
    fchmod = getattr(os, 'fchmod', None)
    if fchmod is not None:
        fchmod(fd, mode)
```

替换点：

| 现逻辑 | 改为 |
|--------|------|
| 顶层 `import fcntl` | 删除；`from flamingoAgents.utils.fileLock import lockExclusive, unlock` |
| `pathStat.st_uid != os.getuid()` | `uid = currentUid()`；仅当 `uid is not None` 时比较 |
| `os.fchmod(lockFd/tempFd, 0o600)` | `applyFdMode(...)` |
| `fcntl.flock(LOCK_EX)` / `LOCK_UN` | `lockExclusive(lockFd)` / `unlock(lockFd)` |
| `_writeDocumentUnlocked` 里 `os.chmod(..., follow_symlinks=False)` | 与 `_ensureBaseDir` 相同：`NotImplementedError`/`TypeError` 时退回无该参数的 `chmod` |
| `os.open(目录) + fsync` | 抽 `_syncDirectory()`：Windows 上 `os.open` 目录失败则直接 return；POSIX 仍失败上抛 |

`(st_dev, st_ino)` 替换检测：Windows 上常为 `(0,0)==(0,0)`，不误报、也不提供保护。本期不改，不假装有 inode。

`_ensureBaseDir` 的 `chmod(..., follow_symlinks=False)` 已有 fallback，不动。

### 3.3 `subscriptionAuth.py`

- 删除顶层 `import fcntl`（以及因此变成未使用的 `errno`，若确认无其它引用则删）。
- `callbackLease.acquire`：属主改 `currentUid()`（从 `credentialStore` 导入，该模块已依赖它）；`fchmod` 仅 `hasattr`；`fcntl.flock(LOCK_EX\|LOCK_NB)` → `lockExclusive(lockFd, nonBlocking=True)`。
- `release`：`unlock(self.lockFd)`。
- 失败仍 `return False`（占用端口走手动码），不把 Windows 锁失败升级成硬异常。

### 3.4 测试

`tests/testCredentialStore.py`：

- `testWritePermissionsRoundTripAndSafeRepr`：往返与 `repr` 脱敏 **Windows 也跑**；`S_IMODE == 0o700/0o600` 仅 `os.name != 'nt'`。
- `testSymlinkCredentialAndLockAreRejected`：`skipif os.name == 'nt'`。
- `testTwoProcessesDoNotLoseDifferentProviderWrites`：无 `fork` start method 则 skip（不改用 spawn：pytest 子进程再导入测试模块不稳定）。
- 未知 provider 保留、损坏 JSON 不覆盖：不依赖 mode，Windows 应直接过。

不强制新增 `testFileLock.py`。若实施时 Windows 上要锁行为回归，可加「同文件第二句柄 `nonBlocking=True` 必失败」的短测；非开工必选项。

`tests/testSubscriptionAuth.py` 不测 flock 本身，只需保证去掉 `fcntl` 后收集/执行仍过。

### 3.5 明确不改

- `flamingoAgents/__init__.py` 导入图
- `webApp/__main__.py` 启动顺序
- `pyproject.toml`
- `builtinTools._killProcessGroup`
- 既有 MCP/workflow「不承诺 Windows」表述（可在 README 快速开始加一句「原生 Windows 仅保证能启动；完整能力仍以 POSIX 为准」——**仅当 D1 确认且你要求改 README 时**，否则文档也不动）

---

## 4. 文件级改动清单

| 文件 | 操作 |
|------|------|
| `flamingoAgents/utils/fileLock.py` | **新建** |
| `flamingoAgents/models/credentialStore.py` | 去 fcntl；加 `currentUid`/`applyFdMode`/`_syncDirectory`；锁与 chmod 走上述 API |
| `flamingoAgents/models/subscriptionAuth.py` | 去 fcntl；租约锁走 `fileLock` + `currentUid` |
| `tests/testCredentialStore.py` | 权限断言/symlink/fork 按平台跳过 |
| `docs/plan/260914_windowsFcntlStartupResearch.md` | 不动（调研截面） |
| 本文件 | 实施后把状态改为已实施 |

---

## 5. 实施顺序（确认后才做）

1. 新增 `fileLock.py`，Windows 与 POSIX 分支隔离，禁止模块顶层 `import fcntl`。
2. 改 `credentialStore.py`，保证 `writeCredential`/`readCredential` 在 Windows 临时目录往返。
3. 改 `subscriptionAuth.py` 租约。
4. 改测试跳过项。
5. 验收：
   - Windows：import `configPaths` / `server`；凭据往返；`python -m webApp` 能听端口（可用后 Ctrl+C）。
   - POSIX：`pytest tests/testCredentialStore.py tests/testSubscriptionAuth.py`。
6. 不提交、不推送，除非你另说。

---

## 6. 风险与不做什么

- **锁语义不是 100% flock**：Windows 锁更接近强制锁，且与句柄绑定。本项目锁文件只用于互斥、不往锁文件写业务数据，可接受。
- **同机 Linux 与 Windows 不会共享 `~/.flamingo/auth.json`**，无需跨 OS 锁兼容。
- **不要**在 Windows 上把 `chmod 0o600` 当成保密措施；单用户家目录是现有假设。
- 实施时禁止顺手改 `killpg`、路径分隔符、MCP、workflow。
