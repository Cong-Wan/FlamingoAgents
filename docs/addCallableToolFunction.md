> Author: wilbur

> FlamingoAgents 让你新增可被模型 function call 调用的工具函数。照着本文写一个就行。

# 新增 Callable Tool 函数

本文说明在 callable tool registry 架构下，如何新增一个模型可调用的工具函数，例如 `currentTime`、`listFiles`、`fetchUrl`。

> **设计原则：** 第一阶段保持轻量——不引入 LangChain / Pydantic / 自动 schema 推断 / 测试框架。所有工具都是手写 callable + factory + 注册，没有魔法。

## Table of Contents

- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [File Map](#file-map)
- [Naming & File Header](#naming--file-header)
- [Full Walkthrough](#full-walkthrough)
- [Permissions](#permissions)
- [Common Errors](#common-errors)
- [Checklist](#checklist)

## Quick Start

下面以 `currentTime`（返回当前 UTC 时间）为例，展示一个工具的最小闭环。新增一个工具只需要四件事：写函数、写 preview、写 factory、注册并启用。

在 `flamingoAgents/tools/builtinTools.py` 中新增：

```python
from datetime import datetime, timezone

def currentTimeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    timezoneName = str(arguments.get('timezone', 'utc'))
    if context.debugConsole:
        context.debugConsole.debug(f'currentTime 工具开始 timezone={timezoneName}')
    if timezoneName != 'utc':
        return toolOutput(content='currentTime.timezone 第一阶段只支持 utc。', isError=True)
    nowText = datetime.now(timezone.utc).isoformat(timespec='seconds')
    return toolOutput(content=nowText, details={'timezone': timezoneName})


def previewCurrentTimeTool(arguments: dict[str, Any]) -> str:
    return 'timezone=' + str(arguments.get('timezone', 'utc'))


def createCurrentTimeTool(permissions: list[permissionRule] | None = None) -> toolDefinition:
    return defineTool(
        name='currentTime',
        description='获取当前 UTC 时间，返回 ISO 8601 字符串。',
        parameters={
            'type': 'object',
            'properties': {
                'timezone': {'type': 'string', 'default': 'utc'},
            },
            'required': [],
            'additionalProperties': False,
        },
        execute=currentTimeTool,
        permissions=permissions or [],
        preview=previewCurrentTimeTool,
    )
```

在同一个文件的 `createBuiltinTools()` 里注册：

```python
builtinFactories: dict[str, Callable[[list[permissionRule]], toolDefinition]] = {
    'read': createReadTool,
    'write': createWriteTool,
    'edit': createEditTool,
    'bash': createBashTool,
    'currentTime': createCurrentTimeTool,
}
```

在 `config/tools.yaml` 启用：

```yaml
enabledTools:
  - read
  - write
  - edit
  - bash
  - currentTime
```

验证（没有测试框架，靠编译 + 让模型实际调用）：

```bash
uv run python -m py_compile flamingoAgents/tools/builtinTools.py
uv run python askModel.py    # 让模型读一段触发 currentTime 的 prompt
```

下面各节解释每一步的约束，以及需要权限确认时的配置。

## How It Works

新增工具走一条主干，没有分支：

```text
写 Python callable
  -> 写 createXTool factory
  -> 注册到 createBuiltinTools 的 factory map
  -> 在 config/tools.yaml 的 enabledTools 中启用
  -> 如需权限确认，在 toolPermissions 中配置规则
```

启动时 `createBuiltinTools()` 按 `enabledTools` 逐个调用 factory 生成 `toolDefinition`，交给 `toolRegistry` 按 name 去重注册。模型发起 function call 时，executor 校验参数 schema → 检查权限 → 调用 `definition.execute()` → 把结果统一包装成 `toolResult`。

工具函数本身只负责「拿到 `arguments` 做事、返回 `toolOutput`」，不用管 `toolCallId`、schema 校验、权限确认——这些都是 executor 的事。

## File Map

| 文件 | 职责 |
|------|------|
| `flamingoAgents/tools/builtinTools.py` | 内置工具函数、preview 函数、factory、factory map |
| `flamingoAgents/tools/toolDefinition.py` | `toolDefinition`、`defineTool()`、`permissionRule` 类型 |
| `flamingoAgents/tools/toolRegistry.py` | 按 name 去重注册 tool definition |
| `flamingoAgents/tools/toolRuntime.py` | 通用 executor：校验参数、检查权限、调用 `execute()`、包装 `toolResult` |
| `flamingoAgents/tools/toolConfig.py` | 解析 `enabledTools` 和 `toolPermissions` |
| `config/tools.yaml` | 决定启用哪些工具、哪些需要权限确认 |

新增工具的代码改动集中在 `builtinTools.py` 和 `tools.yaml`，不要往其他文件塞业务逻辑。

## Naming & File Header

新增代码必须遵守：

- 函数名小驼峰：`currentTimeTool`、`createCurrentTimeTool`、`previewCurrentTimeTool`。
- 新文件名小驼峰，例如 `dateTools.py`。
- 新建代码文件开头必须有文件头：

```python
'''
Author: wilbur
Version: 1.0
Date: 2026-07-08
Description: Defines callable tools for date and time operations.
'''
```

只修改已有文件时，更新文件头的小版本号和 description（如 1.0 → 1.1）。

## Full Walkthrough

Quick Start 是完整闭环。下面把每一步的约束讲清楚，需要细节时查这里。

### 1. 真实执行函数

签名固定为：

```python
def currentTimeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    ...
```

- `arguments` 是模型传入的 JSON object。
- `context.workDir` 是当前工作目录；`context.debugConsole` 是 `--debug` 下的日志通道。
- 返回 `toolOutput`，不要返回 `toolResult`，也不要碰 `toolCallId`（executor 统一包装）。

### 2. 确认预览函数

preview 用于权限确认界面和日志，把调用摘要压缩成一行：

```python
def previewCurrentTimeTool(arguments: dict[str, Any]) -> str:
    return 'timezone=' + str(arguments.get('timezone', 'utc'))
```

即使工具永远不需要权限确认，也建议写 preview，方便调试。

### 3. createXTool factory

factory 把函数、schema、description、permissions、preview 绑成一个 `toolDefinition`。schema 约束：

| 约束 | 要求 |
|------|------|
| 顶层类型 | 必须是 `type: object` |
| `properties` | 必须明确写出 |
| `required` | 必须明确写出（可为空数组） |
| `additionalProperties` | 建议写 `False`，挡掉无关参数 |
| 支持的类型 | 第一阶段支持 `string` / `integer` / `array` / `object` |

### 4. 注册到 factory map

在 `createBuiltinTools()` 的 `builtinFactories` 中加入映射。只有注册后，registry 才能按工具名找到 definition。

### 5. 在配置中启用

在 `config/tools.yaml` 的 `enabledTools` 加入工具名。不需要权限确认的工具，无需在 `toolPermissions` 下配置。

## Permissions

工具某些调用需要用户确认时，在 `toolPermissions.<工具名>` 下配置规则。例如 `fetchUrl` 访问内网地址要确认：

```yaml
toolPermissions:
  fetchUrl:
    - id: localNetworkUrl
      field: url
      action: requireApproval
      reason: 访问内网地址需要用户确认
      match:
        type: regex
        patterns:
          - '^https?://(127\.0\.0\.1|localhost|192\.168\.|10\.|172\.(1[6-9]|2[0-9]|3[0-1])\.)'
```

- `field` 必须对应工具 arguments 中的字段名。
- `action` 第一阶段只支持 `requireApproval`。
- `match.type` 第一阶段只支持 `regex`，以 `re.IGNORECASE` 编译。
- 触发权限的调用返回 `confirmationRequired`，不会提前执行真实函数。

## Common Errors

**模型调用时返回「未知工具：currentTime」**
→ `createBuiltinTools()` 的 `builtinFactories` 里漏了 `'currentTime': createCurrentTimeTool`。

**`createAgent()` 抛「未知内置工具：currentTime」**
→ 配置启用了工具，但 Python 没有对应 factory。补齐 factory 并注册。

**executor 返回「工具参数不符合 schema」或函数内 KeyError 被包成「工具执行异常」**
→ schema、函数读取字段、preview 三者字段名不一致。让它们完全相同。

**executor 包装时 `AttributeError`**
→ 工具函数直接返回了字符串。必须返回 `toolOutput(content=..., details=...)`。

**无 `--debug` 时仍有调试输出**
→ 用了 `print()`。改用 `if context.debugConsole: context.debugConsole.debug(...)`。

## Checklist

- [ ] 真实函数：`currentTimeTool(arguments, context) -> toolOutput`
- [ ] 预览函数：`previewCurrentTimeTool(arguments) -> str`
- [ ] factory：`createCurrentTimeTool(permissions=None) -> toolDefinition`
- [ ] 注册：`'currentTime': createCurrentTimeTool`
- [ ] 配置：`enabledTools` 加入 `currentTime`
- [ ] 权限：需要确认时在 `toolPermissions.currentTime` 中配置规则
- [ ] 验证：`uv run python -m py_compile ...` + `uv run python askModel.py`
