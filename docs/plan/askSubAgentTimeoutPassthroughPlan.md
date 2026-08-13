# askSubAgent timeout 透传 —— 方案计划

> Author: wilbur
> Version: 1.2
> Date: 2026-08-13
> Status: 已实施（上限 3600）
> 关联：`config/tools.yaml` askSubAgent schema；`flamingoAgents/tools/builtinTools.py` askSubAgentTool
> 审核：`pi -p`（kimi/k2.5）对照源码，总览「可实施」；🟠1 / 🟡3 已回写本文档

## 背景与目标

当前 `askSubAgent` 的子进程超时写死为 `subAgentTimeoutSeconds = 600`：

- schema（`config/tools.yaml`）没有 `timeout` 字段，模型无法按任务复杂度传入；
- 实现层 `subprocess.run(..., timeout=600)` 一律 10 分钟，简单任务偏长、复杂任务（长推理 / 多轮工具）会误杀。

目标：把 `timeout` 做成与 `bash.timeout` 同构的透传参数——模型按任务复杂度传入，不传则保持现有 600s 默认行为。

**本次只改调用子代理这条链路。不改 sdkEntry、不改 agent 核心、不加测试框架。**

## 现状（只列相关）

| 层 | 现状 | 结论 |
|---|---|---|
| `config/tools.yaml` askSubAgent | 参数只有 model / prompt / system / tools / workDir | 模型看不到 timeout |
| `builtinTools.askSubAgentTool` | `timeout=subAgentTimeoutSeconds`（常量 600） | 写死 |
| `builtinTools.previewAskSubAgentTool` | `f'{model} prompt={prompt[:60]}'` | 卡片上看不见超时 |
| `builtinTools.bashTool` | schema 有 timeout（default 30, min 1）；运行时再夹到 `[1, 120]`；schema **没有** maximum | 已有同构先例，照抄夹紧；上限这次写进 schema（bash 没写，模型看不见 120） |
| `sdkEntry.py` | 无 `--timeout`；自身跑到模型结束（`maxModelSteps = -1`） | 父进程 `subprocess.run(timeout=...)` 杀子进程已足够，不必给 sdkEntry 加旗标 |
| `toolRuntime.validateValue` | integer 已支持 `minimum` / `maximum`；**不应用 schema `default`** | schema 写了 min/max 就会拦非法值；默认值必须在 execute 里 `.get(..., 600)` |
| `toolSchema.buildModelTool` | 原样转发 `definition.parameters` | yaml 里的 description / default / min / max 会进模型 function schema |
| `agent.driveToolBatch` | 先 `preview(raw arguments)` 发 Start，再 `executeToolCall`（内部才 `validateArguments`） | preview 看到的是未校验入参，不能 `int()` 裸转 |
| `agent.executeToolCall` | 同步阻塞，会话锁全程持有 | 子代理跑多久，该会话就卡多久（现有 600s 已如此） |

杀进程的位置就在父进程：`subprocess.run(..., timeout=N)` → `TimeoutExpired` → 返回 `isError` 工具结果。子进程不需要感知 timeout。

## 假设（不确定会提问，不默默拍板）

1. **默认值保持 600s**。不传 timeout 的旧调用行为不变。
2. **上限建议 3600s（1 小时）**。复杂审方案 / 多轮改代码可能超过 10 分钟，但无上限会让失控子代理挂到进程被人手杀。若你希望「完全不封顶」或改成 1800 / 7200，直接说。
3. **sdkEntry 不加 `--timeout`**。超时语义是「父进程等多久」，不是「子进程内部再设一份」。加 CLI 旗标是重复通道，这次不做。
4. **不改 `config/systemPrompt.md`**。已核对：该文件没有硬编码 askSubAgent 参数列表；模型看见的是 tools.yaml 的 description / 参数说明。若以后有人往 systemPrompt 里抄参数表，再同步。
5. **不 bump `tools.yaml` 的 `version: 3`**。只是给已有工具加一个可选字段，配置格式没变；`toolConfig.py` 也只认 version 3。
6. **不处理进程组 / 孤儿子进程**。`subprocess.run(timeout=)` 到期只 `kill` 直接子进程（sdkEntry），它再拉起的 bash 等可能残留——与现在 600s 路径相同，本次不扩范围。
7. **会话锁被占满 timeout 秒可接受**。这是现有同步工具模型的放大，不是新引入的；webApp 工具卡会转圈到超时或结束。不为这次去改 agent / 泵。

## 设计

对齐 `bash.timeout`，两处文件同步，不多加抽象：

```
模型 function call
  arguments.timeout?  --schema-->  integer, min=1, max=3600, default=600
        │
        ├─ preview（Start 之前，未校验）→ 原样展示 arguments.get('timeout', 600)
        │
        ▼
toolRuntime.validateArguments
  有传则拦类型 / 上下限；没传则放行（非 required）
        │
        ▼
askSubAgentTool
  读取 → 夹紧到 [1, 3600] → subprocess.run(timeout=timeout)
        │
        ▼
TimeoutExpired → toolOutput(isError, "子代理超时被终止（Ns）。")
```

### 1. schema（`config/tools.yaml`）

在 askSubAgent.parameters.properties 增加：

```yaml
timeout:
  type: integer
  minimum: 1
  maximum: 3600
  default: 600
  description: 子进程硬超时（秒）。按任务复杂度传入：短问答 60–180，常规编码 300–600，长审 / 多轮工具 900–3600。不传默认 600。
```

`required` 仍只有 `model` / `prompt`。

工具顶层 description 补一句，让模型在没点开参数时也知道能传。完整目标文案：

> 调用子代理完成独立子任务。子代理在独立会话中运行，可使用指定模型与工具，返回最终文本回复。注意：tools 仅支持 read/write/edit/bash，不要传 askSubAgent（避免无限嵌套）。可按任务复杂度传入 timeout（秒，默认 600，上限 3600）。

### 2. 实现（`flamingoAgents/tools/builtinTools.py`）

常量拆开，不再跟 bash 的 120s 上限混用：

```python
defaultSubAgentTimeoutSeconds = 600
maxSubAgentTimeoutSeconds = 3600
```

删掉只被 askSubAgent 使用的 `subAgentTimeoutSeconds`。bash 的 `maxTimeoutSeconds` / `defaultTimeoutSeconds` 不动。

**preview 必须防御，禁止 `int()`。** Start 先于 schema 校验，模型乱传时 preview 异常会被 `buildToolPreview` 吃掉并回退成整段 arguments，卡片更难看。只做展示：

```python
def previewAskSubAgentTool(arguments: dict[str, Any]) -> str:
    model = str(arguments.get('model', ''))
    prompt = str(arguments.get('prompt', ''))
    timeout = arguments.get('timeout', defaultSubAgentTimeoutSeconds)
    return f'{model} timeout={timeout} prompt={prompt[:60]}'
```

`askSubAgentTool` 按 bash 同款夹紧（校验已过后这是防御，不是第二套业务规则）：

```python
timeout = int(arguments.get('timeout', defaultSubAgentTimeoutSeconds))
if timeout < 1:
    timeout = defaultSubAgentTimeoutSeconds
if timeout > maxSubAgentTimeoutSeconds:
    timeout = maxSubAgentTimeoutSeconds
```

- `subprocess.run(..., timeout=timeout)`
- 超时文案用实际 `timeout`，不再写死 600
- 开始 / 完成 / 超时三条 debug 都带 `timeout=`
- 成功 `details` 带上 `'timeout': timeout`
- 超时 **不抄 bash 去拼 stdout/stderr**。现有 600s 路径只回一句文案；子代理 `--json` 的有效输出在最后一行，超时多半不完整，拼进去帮不上主模型，还会把 stderr 思维链噪音塞进 toolResult。保持：

```python
except subprocess.TimeoutExpired:
    return toolOutput(
        content=f'子代理超时被终止（{timeout}s）。',
        isError=True,
        details={'timeout': timeout, 'timeoutExpired': True, 'model': model},
    )
```

- 普通失败（非超时，如非零退出 / JSON 解析不到 reply）details **不**加 timeout。这是相对 bash always-on 的故意选择：失败原因跟等多久无关，避免噪音。

**`schema.default` 不会被 `validateArguments` 填进 arguments。** 不传时 `arguments` 里没有 `timeout` 键，execute 必须自己 `.get(..., defaultSubAgentTimeoutSeconds)`。不要以为 yaml 写了 default 就能省略这行。

### 3. 明确不改

| 文件 | 原因 |
|---|---|
| `sdkEntry.py` | 父进程杀子进程即可，无需 `--timeout` |
| `toolRuntime.py` / `toolSchema.py` | integer min/max 已支持；default 本就不由运行时填充 |
| `toolConfig.py` | 配置格式未变 |
| `agent.py` / webApp | 工具参数透传；会话锁被占是既有同步模型，不为 timeout 去改驱动循环 |
| `config/systemPrompt.md` | 模型看 tools.yaml 即可 |

## 边界行为

| 入参 | 行为 |
|---|---|
| 不传 timeout | 600s，与现在一致；preview 显示 `timeout=600` |
| `1`–`3600` | 按传入值；preview 显示该值 |
| `0` / 负数 | schema 拦（minimum=1），工具不启动子进程；若绕过校验，运行时回落到 600 |
| `> 3600` | schema 拦（maximum=3600），工具不启动子进程；若绕过校验，运行时夹到 3600 |
| 非整数（含 bool、浮点、字符串） | schema 拦，「必须是整数」 |
| 到期 | `TimeoutExpired` → `isError`，文案含实际秒数；details 含 `timeoutExpired: True`；直接子进程被 `kill`；**不**拼超时前 stdout/stderr |

不处理「超时后孙进程残留」——`subprocess.run(timeout=)` 到期只杀 sdkEntry 这一个 PID，与现在 600s 路径相同。

## 权衡

- **为何不把上限做成可配置 / 环境变量**：一次性参数，yaml default + 常量足够。再加配置是「未经要求的灵活性」。
- **为何上限写进 schema 而 bash 没写**：bash 的 120 只在运行时静默夹紧，模型不知道。这次让模型看见上限，避免它传 7200 却被悄悄裁成 3600。
- **为何不加 sdkEntry `--timeout`**：两条通道会分叉（CLI 有、function call 无，或反过来）。父进程超时已经覆盖「调用子代理」这个场景。
- **为何预览不 `int()`**：`driveToolBatch` 先 preview 再 validate；preview 抛错只是静默回退，卡片变成整段 JSON，得不偿失。
- **为何接受 3600s 占会话锁**：改锁 / 改泵超出「只改调用子代理这条链路」。现有 600s 已经占锁，只是上限变长。如实声明，不装成没这回事。
- **为何超时不拼 stdout/stderr**：现有路径就没拼；`--json` 超时时最后一行多半不完整，stderr 是事件流噪音。主模型只需要「跑了 Ns 被杀」。

## TODO

- [x] `config/tools.yaml`：askSubAgent 增加 `timeout` 参数（min/max/default/description），顶层 description 补一句
- [x] `flamingoAgents/tools/builtinTools.py`：
  - [x] 文件头 1.5 → 1.6，Date 改当天，description 写明 timeout 透传（默认 600，上限 3600）
  - [x] `subAgentTimeoutSeconds` 拆成 `defaultSubAgentTimeoutSeconds` / `maxSubAgentTimeoutSeconds`
  - [x] `previewAskSubAgentTool` 展示 timeout，不 `int()`
  - [x] `askSubAgentTool` 读取并夹紧 timeout，传入 `subprocess.run`
  - [x] debug / 成功 details / 超时 details+文案带上实际 timeout
- [x] `uv run python -m py_compile flamingoAgents/tools/builtinTools.py`
- [x] 目测：schema 与实现字段名、默认值、上下限三处一致（yaml default/min/max == 常量 == 夹紧逻辑）

## 成功标准

1. 不传 timeout → 行为与现在相同（等 600s）；preview 显示 `timeout=600`。
2. 传入 `timeout=120` → 子进程最多活 120s，超时文案和 details 都是 120 不是 600。
3. 传入非法值（0、3601、1.5、"120"）→ schema 报错，工具不启动子进程。
4. preview 即使收到非数字 timeout 也不抛，卡片仍能显示。
5. sdkEntry / webApp / bash 工具 / agent 核心零改动、零行为变化。
6. `tools.yaml` 的 `default: 600` / `minimum: 1` / `maximum: 3600` 与代码常量 `defaultSubAgentTimeoutSeconds` / `maxSubAgentTimeoutSeconds` 及夹紧逻辑三处字面一致。

## 实施前待你拍板

唯一需要确认的数：**上限 3600s 是否合适**。

| 选项 | 含义 |
|---|---|
| A. 3600（方案默认） | 够长审 / 多轮改代码；会话最多卡 1 小时 |
| B. 1800 | 更保守，半小时封顶 |
| C. 7200 | 更宽松，适合特别重的审方案 |
| D. 不设上限 | schema 只写 minimum=1；运行时不夹上界。失控子代理只能人手杀 |

其余按上文做。
