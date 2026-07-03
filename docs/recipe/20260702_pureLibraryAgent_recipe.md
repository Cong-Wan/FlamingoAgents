<!--
Author: wilbur
Version: 1.2
Date: 2026-07-02
Description: Marks this pure-library Agent design as superseded by the unified pure library Agent Runtime recipe.
-->

# 将 FlamingoAgents 改造为纯库 Agent — 设计规范

> Superseded by `docs/recipe/20260702_pureLibraryAgentRuntime_recipe.md`. This file is retained only as historical design input and must not be used as an execution source.

## 1. 背景与目标

当前 `flamingoAgents/` 是一个**带两个入口的应用**：

- `flamingoAgents/app/cli.py` 暴露 `Flamingo` 命令（交互式 `input()` 循环）
- `flamingoAgents/app/server.py` 暴露 `flamingo-agents-server` 命令（`ThreadingHTTPServer`）
- 两个入口各自复制了一份 `buildAgent()` 装配逻辑（debug → modelConfig → adapter → registry → agent），差异仅在 `confirmDeletion`

**目标**：剥离全部入口层，让 `flamingoAgents` 变成**纯库**——只对外暴露「构建一个能用的 agent」的工厂 + agent 核心能力。输入输出方式（CLI / HTTP / GUI / notebook / 其他 host 进程）完全交给使用者。

**关键不变量**：请求 LLM 的网络逻辑位于 `models/chatCompletions.py::chatCompletionsAdapter.complete()`（内部用 `urllib.request.urlopen` POST `/chat/completions`），它属于 `models/` 层，**属于库本体，本次不改动**。`app/` 层从未包含任何网络请求代码，删除入口不影响「怎么跟 LLM 对话」。

## 2. 范围确认

纳入本次：
- 删除 `flamingoAgents/app/` 整个目录（3 个文件）
- 新增 `flamingoAgents/builder.py`，提供 `createAgent()` 装配工厂
- 修改 `flamingoAgents/__init__.py`，re-export 公开 API
- 修改 `pyproject.toml`，删除 `[project.scripts]`，更新 description
- 修改 `manualChecks.py`，删除依赖入口层的 `http` 检查

不纳入本次（正交问题，见 `docs/codeReview/260702_moduleBoundaries.md`）：
- 文件工具 `workDir` 沙箱逃逸
- `agent` 并发锁 / HTTP 状态机缺陷（`continueConfirmation` 先 pop 后校验、多 tool call pending、pending 时禁止新消息）
- 工具层泄漏 OpenAI schema、adapter 读 `os.getenv`、配置层写 `os.environ`
- `confirmDeletion` 的具体实现策略（使用方自行处理，库不内置）

## 3. 现状：依赖关系快照

```
pyproject.toml [scripts]
   └─► flamingoAgents.app.cli:main       ─┐
   └─► flamingoAgents.app.server:main     ├─ 入口层（本次删除）
                                          │
flamingoAgents/app/                       │
  cli.py   ──buildAgent()──┐              │
  server.py ──buildAgent()─┤  复制装配    │
                            │             │
manualChecks.py ◄── imports server.makeHttpHandler  ◄── 唯一外部对入口层的依赖
```

已验证的边界事实：
- `core/` **不依赖** `app/`（`grep` 确认）→ 删入口对 agent 核心零影响 ✅
- `models/`、`tools/`、`utils/` 均**不依赖** `app/` ✅
- 仅 `manualChecks.py` 一处 `from flamingoAgents.app.server import makeHttpHandler` 引用入口层
- 仅 `pyproject.toml` 的 `[project.scripts]` 把入口暴露成命令

## 4. 设计决策

### 4.1 决策 A（已批准）：装配逻辑提升为库的公开工厂

采用 **A1**：新建 `flamingoAgents/builder.py`，提供 `createAgent()` 工厂；在 `__init__.py` 里 re-export。

**理由**：
- `core/` 不该读 config/env（分层边界）→ 装配不能塞进 `core/agent.py`
- 让使用者逐个 import config/adapter/registry 来拼装 → 使用成本高，人人复制样板代码
- `buildAgent()` 本就是「装配」职责，从「入口层私有」提升为「库公开 API」是最自然的归宿

### 4.2 决策 B：库不内置任何确认策略

`confirmDeletion` 的实现（如基于 `input()` 的 `askDeletionConfirmation`）属于使用方的交互策略，**从库中完全移除**。

- `agent.__init__` 本身已支持两种模式（传回调=同步确认；传 `None`=异步 `confirmationRequired` 状态机），**无需改动**
- `createAgent()` 透传一个**可选** `confirmDeletion` 参数给 `agent`，便于需要同步确认的使用方；不传则默认 `None` 走异步状态机
- **不内置** `askDeletionConfirmation` 等任何具体实现

> 注：使用方对 `confirmDeletion` 另有处理方案，本规范不规定其细节，只保证库层面的透传通道存在。

## 5. 改动清单

### 5.1 删除（3 个文件 + 目录）

| 路径 | 内容 |
| --- | --- |
| `flamingoAgents/app/cli.py` | CLI 入口、`buildAgent()`、`askDeletionConfirmation()` |
| `flamingoAgents/app/server.py` | HTTP 入口、`makeHttpHandler()`、`buildAgent()` |
| `flamingoAgents/app/__init__.py` | app 包初始化 |
| `flamingoAgents/app/` | 整个目录 |

### 5.2 新增 `flamingoAgents/builder.py`（v1.0）

纯库装配工厂。把原 `buildAgent()` 的装配逻辑收口为单一公开函数，并支持可选的 `confirmDeletion` / `logDir` / `debug`。

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Pure-library assembly factory: loads model config, builds adapter/registry,
and returns a ready-to-use agent instance.
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.agent import agent, confirmationHandler
from flamingoAgents.models.registry import loadModelConfig
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
from flamingoAgents.tools.registry import createDefaultRegistry
from flamingoAgents.utils.debug import debugConsole


def createAgent(
    workDir: str | Path,
    *,
    debug: bool = False,
    confirmDeletion: confirmationHandler | None = None,
    logDir: str | Path | None = None,
) -> agent:
    workDirPath = Path(workDir).resolve()
    printer = debugConsole(debug)
    config = loadModelConfig()
    adapter = chatCompletionsAdapter(config, printer)
    resolvedLogDir = Path(logDir).resolve() if logDir else workDirPath / '.agentLogs'
    return agent(
        modelAdapter=adapter,
        registry=createDefaultRegistry(),
        workDir=workDirPath,
        logDir=resolvedLogDir,
        debugConsole=printer,
        confirmDeletion=confirmDeletion,
    )
```

要点：
- `debug` / `confirmDeletion` / `logDir` 均为仅关键字参数，默认值保证「最简调用」即可拿到可用 agent
- 默认 `logDir = workDir/.agentLogs`，与原入口行为一致
- 不在工厂里做任何新逻辑，仅收口装配

### 5.3 修改 `flamingoAgents/__init__.py`（1.1 → 1.2）

re-export `createAgent`，提供最短 import 路径：

```python
from flamingoAgents.builder import createAgent

packageVersion = '0.1.0'

__all__ = ['createAgent', 'packageVersion']
```

（保留原 `packageVersion`；**仅**新增 re-export `createAgent`。需要自定义装配时，使用者从 `flamingoAgents.core.agent` 自行 import `agent` / `confirmationHandler`，不暴露到包根。）

### 5.4 修改 `pyproject.toml`

- 删除整个 `[project.scripts]` 段（`Flamingo` 与 `flamingo-agents-server` 两条命令）
- `description` 改为：`Local Flamingo Agents as a pure library`
- `name`、`version`、`requires-python`、`dependencies`、`[build-system]`、`[tool.hatch.build.targets.wheel]` **不变**

### 5.5 修改 `manualChecks.py`（1.2 → 1.3）

剥离对入口层的依赖：

- 删 import：`import http.client`、`from http.server import ThreadingHTTPServer`、`import threading`、`from flamingoAgents.app.server import makeHttpHandler`
- 删函数：`runHttpCheck()`
- `argparse` 的 `choices` 去掉 `'http'`
- `main()` 删除 `if args.check in {'all', 'http'}: runHttpCheck(args.debug)` 分支
- 文件头 description 补充「移除依赖入口层的 http 检查」
- `runAgentCheck()` **保留不动**（它直接构造 `agent`，覆盖 `continueConfirmation` 的 reject 分支，不依赖入口层）

改后剩余检查项：`fileTools` / `bash` / `guard` / `logger` / `adapter` / `agent`（均不联网、不依赖入口层）。

## 6. 公开 API 契约

改造后，包根只公开 `createAgent`；需要自定义装配时使用者从子模块取 `agent` / `confirmationHandler`：

| 符号 | 来源 | 用途 |
| --- | --- | --- |
| `createAgent(workDir, *, debug, confirmDeletion, logDir)` | `flamingoAgents` 包根（`builder.py`） | 一行拿到可用 agent |
| `agent` | `flamingoAgents.core.agent` | 需要自定义装配时直接构造 |
| `confirmationHandler` | `flamingoAgents.core.agent` | `confirmDeletion` 参数的类型别名 |

## 7. 使用者示例

```python
from pathlib import Path
from flamingoAgents import createAgent

# 最简：异步确认模式（不传 confirmDeletion）
agent = createAgent(Path('.'))
result = agent.runUserMessage('帮我读 sample.txt', sessionId='s1')
print(result.status, result.message)

# 需要同步确认时，使用方自行实现并透传
def myConfirm(call, reason):
    return input(f'{reason}\n允许？[y/N] ').lower() in {'y', 'yes'}

agent2 = createAgent(Path('.'), confirmDeletion=myConfirm)
```

异步确认模式完全不变：`runUserMessage` → `runResult(status='confirmationRequired')` → `continueConfirmation(sessionId, confirmationId, approved)`。

## 8. 验证方式

无测试框架，依赖 `manualChecks.py`：

```bash
uv run python manualChecks.py all
```

预期：`fileTools` / `bash` / `guard` / `logger` / `adapter` / `agent` 六项全部 `PASS`（删除 http 检查后）。

补充检查：
- `uv run python -c "from flamingoAgents import createAgent; print('import ok')"` —— 确认 re-export 与 builder 导入链无误（包根只公开 `createAgent`）
- `uv run python -c "import flamingoAgents.builder, flamingoAgents.core.agent, flamingoAgents.models.chatCompletions; print('chain ok')"` —— 确认装配链完整、`models` 层未受影响

成功标准：
1. `manualChecks.py all` 六项全 PASS
2. 两条 import 检查均输出 `ok`
3. `flamingoAgents/app/` 目录不存在
4. `uv run` 不再能找到 `Flamingo` / `flamingo-agents-server` 命令

## 9. 不在范围（再次声明）

- **`docs/flamingoAgentsFlow.md` 的同步更新不在本次**——删除入口后该文档的部分章节会过时，留待后续单独处理。
- 模块边界审核报告里的所有问题均**不在本次**：workDir 沙箱、并发锁、HTTP 状态机缺陷、协议层泄漏、配置层副作用、`confirmDeletion` 具体策略。本次只做「剥离入口、收口装配」，不顺手改这些。

---

## 已确认的决策（用户 2026-07-02）

1. **范围**：`docs/flamingoAgentsFlow.md` 同步更新**不纳入**本次。
2. **`confirmDeletion`**：`createAgent()` **保留** `confirmDeletion` 参数，默认 `None`（透传通道留着）。
3. **`__init__.py` re-export**：**只** export `createAgent`。
