# 从 pi `models.json` 导入模型配置 —— 方案与计划

Author: wilbur
Version: 1.0
Date: 2026-08-13
Description: 在模型配置页增加「从 pi models.json 导入」：把 `~/.pi/agent/models.json`（或用户粘贴/上传的同结构 JSON）转成 flamingo `config/models.yaml` 可保存的工作副本，经现有 PUT 落盘。不引入新协议、不改库适配器。

## 0. 调研结论（已读码 / 已对照真实文件）

### 0.1 flamingo 现状

| 层 | 位置 | 事实 |
|---|---|---|
| 落盘 | `config/models.yaml` | `providers.{id}` → `baseUrl` / `api` / `apiKey` / `headers?` / `models[]` |
| 库解析 | `flamingoAgents/models/modelConfig.py` | **仅** `api: openai-completions`；`thinking` 原样注入请求体；`reasoningEffort` 注入 `reasoning_effort`；`headers` provider 为底、model 覆盖；`apiKey` 支持明文 / `$ENV` / `${ENV}` |
| Web 读 | `modelConfigStore.readModelsConfig` | 宽松读取 + apiKey 脱敏（`__KEEP__` / `$` 引用） |
| Web 写 | `modelConfigStore.writeModelsConfig` | PUT 校验（契约 §2.4）+ 合并式写回 + `.bak` + 原子替换 + `invalidateAllAgents` |
| UI | `settingsView.js` | 内存工作副本：open 时 GET 一次，保存才 PUT；思考只暴露 `reasoningEffort`，`reasoning`/`thinking` 保留但不展示 |
| 契约 | `docs/webApiSpec.md` §2.4 / §3.11 / §3.12 | 尚无导入端点 |

flamingo **不支持**、导入时必须丢掉或拒绝的 pi 能力：

- `api` ∈ {`anthropic-messages`, `openai-responses`, `google-generative-ai`, `openai-codex-responses`, …}
- `compat` / `thinkingLevelMap` 运行时切换 / `oauth` / `authHeader` / `modelOverrides`
- `apiKey: "!command"`（shell 取值）
- `cost.tiers`（分档计价）

### 0.2 pi `models.json` 官方结构（pi 0.82.1 `docs/models.md`）

路径：`~/.pi/agent/models.json`。顶层只有 `providers`。

```json
{
  "providers": {
    "<providerId>": {
      "baseUrl": "https://...",
      "api": "openai-completions",
      "apiKey": "sk-..." | "$ENV" | "${ENV}" | "!command",
      "headers": { "User-Agent": "curl/8.7.1" },
      "compat": { "supportsDeveloperRole": false, "supportsReasoningEffort": true, "thinkingFormat": "openai" },
      "models": [
        {
          "id": "k3",
          "name": "k3",
          "reasoning": true,
          "thinkingLevelMap": { "off": null, "low": "low", "high": "high", "max": "max" },
          "input": ["text", "image"],
          "contextWindow": 1048576,
          "maxTokens": 128000,
          "cost": { "input": 3, "output": 15, "cacheRead": 0.3, "cacheWrite": 0 },
          "headers": {},
          "compat": {},
          "api": "openai-completions"
        }
      ]
    }
  }
}
```

pi 缺省（文档）：`name=id`、`input=["text"]`、`contextWindow=128000`、`maxTokens=16384`、`reasoning=false`、`cost` 全 0。`api` 可写在 provider 或 model。

用户本机真实文件额外出现、文档未列的字段：`reasoning_effort`（snake_case，glm / huoshan / sub2api_volcano 上有）。按「同语义遗留字段」纳入映射。

**不在范围内**：`~/.pi/agent/models-store.json`（内置目录缓存，形状是 `{provider: {models, checkedAt, etag}}`，含 Codex OAuth 等 flamingo 跑不了的 api）。本期只认 `models.json`。

### 0.3 两边字段对照

| pi | flamingo | 导入策略 |
|---|---|---|
| `providers.<id>.baseUrl` | 同 | 原样；缺则整 provider 跳过 |
| `api` | 仅 `openai-completions` | provider/model 解析后 ≠ 该值 → 跳过该模型；provider 滤完无模型 → 跳过 provider |
| `apiKey` 明文 / `$ENV` / `${ENV}` | 同 | 原样进入转换结果 |
| `apiKey: "!…"` | 无 | 置空 + warning，不执行命令 |
| `headers` | 同（`dict[str,str]`） | 只保留字符串键值；空则省略 |
| `compat` / `oauth` / `authHeader` / `modelOverrides` | 无 | 丢弃；`modelOverrides` 单独 warning（flamingo 无内置目录可覆盖） |
| `models[].id` | 同 | 必填；同 provider 内重复 id 后者覆盖前者 + warning |
| `name` | 同 | 缺省 = `id` |
| `input` | `text`/`image` | 过滤非法元素；空则 `["text"]` |
| `contextWindow` / `maxTokens` | 正整数 | 缺省 128000 / 16384；非正整数用缺省 + warning |
| `reasoning` | 同 | 见 §1 D3 |
| `thinkingLevelMap` | `thinking` + `reasoningEffort` | 见 §1 D3，压成**一个静态档** |
| `reasoning_effort` / `reasoningEffort` | `reasoningEffort` | 有则作为 D3 的保底值（map 优先） |
| `cost.{input,output,cacheRead,cacheWrite}` | 同 | 缺/非法 → 0；`tiers` 丢弃 + warning |
| `models[].api` / `baseUrl` | flamingo 模型级无 api/baseUrl | `api` 只用于过滤；模型级 `baseUrl` 丢弃 + warning（本期不扩展 yaml schema） |
| `stream` 等 flamingo 私有字段 | 已有则 PUT 合并保留 | 导入不写 `stream`；覆盖已有模型时也不主动删 |

用本机 `~/.pi/agent/models.json` 干跑一遍预期：

| provider | api | 结果 |
|---|---|---|
| kimi | openai-completions | 可导入（4 模型） |
| sub2api_gpt | openai-completions | 可导入（1 模型） |
| deepseek | openai-completions | 可导入（2 模型，flamingo 当前没有） |
| glm | openai-completions | 可导入（1 模型） |
| huoshan | **anthropic-messages** | **整组跳过** |
| sub2api_grok | openai-completions | 可导入 |
| sub2api_volcano | openai-completions | 可导入（flamingo 当前没有） |
| software_grok | openai-completions | 可导入 |

与当前 `config/models.yaml` 的 id 交集：`kimi`、`sub2api_gpt`、`sub2api_grok`、`software_grok`。默认策略下这些 provider **只补新模型、不改已有模型/密钥**。

---

## 1. 设计决策（已选定，含权衡）

### D1 导入落点：工作副本，不直接写 yaml

导入只改 settings 页内存工作副本并 `markDirty()`，**不**调用 PUT、不写 `models.yaml`、不清 agent 缓存。用户在现有表单里检查后点「保存」，走既有 §3.12（bak + 合并 + 原子写 + invalidate）。

理由：复用全部校验与脱敏；误导入可「重置」放弃；apiKey 仍按 `__KEEP__` 规则回写。

### D2 转换在后端，合并在前端

新增只读转换端点（§2），纯函数：pi 文档 → flamingo 形状 + 报告。前端把结果按 D5 合并进工作副本。

不把转换放前端：缺省路径 `~/.pi/agent/models.json` 在**服务端**本机，远程打开页面读不到；规则与中文报告需要单一实现。

不在该端点写盘、不接收任意服务器路径（只认请求体里的 JSON，或硬编码默认路径）。

### D3 `thinkingLevelMap` → 一对静态字段

flamingo 适配器没有档位切换，只有：

- `thinking: {type: enabled}` → 请求体 `thinking`
- `reasoningEffort: "<str>"` → 请求体 `reasoning_effort`

转换规则（按顺序）：

1. `reasoning = bool(model.reasoning)`。
2. 若存在合法字符串 `reasoningEffort` 或遗留 `reasoning_effort`，记为 `effort`。
3. 若 `thinkingLevelMap` 是对象：按 `max > xhigh > high > medium > low > minimal` 取**第一个值为非空字符串**的项，`effort = 该字符串`（用发给供应商的值，不是 key），并强制 `reasoning = true`。`off: null` 只表示「不能关思考」，不影响选档。
4. `reasoning == true` → 写出 `thinking: {type: enabled}`；否则不写 `thinking`。
5. `effort` 非空才写 `reasoningEffort`。

UI 仍然只展示思考强度；`thinking` 静默进工作副本，保存后行为与手填一致。

不把整张 map 塞进 yaml（库不认，属于投机字段）。

### D4 只导入 `openai-completions`

provider.api 与 model.api 的生效值（model 覆盖 provider，都缺 = 未知）必须是 `openai-completions`，否则跳过该模型。滤完 models 为空则跳过 provider。

`huoshan` 这类 anthropic 中转整组进报告，不进工作副本。不在本期扩展协议。

### D5 合并策略：默认保守，三开关可选加强

| 情形 | 默认 | 打开对应开关后 |
|---|---|---|
| 新 providerId | 整组加入（含 apiKey） | — |
| 已有 provider，新 model id | 追加到该 provider.models | — |
| 已有 provider + 已有 model id | **跳过该模型** | `overwriteModels`：用转换结果替换（保留 flamingo 侧 schema 外字段，如 `stream`） |
| 已有 provider 的 `baseUrl` / `headers` | **保持现有** | `overwriteProviderFields`：用 pi 的值覆盖（headers 整表替换，空 = 删除） |
| 已有 provider 的 `apiKey` | **永远保持现有**（含 `__KEEP__`） | `overwriteApiKey`：用 pi 的值写入工作副本（明文或 `$` 引用） |

默认三开关全关。密钥默认不覆盖，避免把正在用的 key 换成 pi 文件里另一份。

导入前若工作副本 `dirty`：`confirm('将在当前未保存修改上继续导入。继续？')`。

### D6 入口与交互：轻量面板，不加完整预览表

设置页底栏「重置」左侧加按钮 **「从 pi 导入」**。点开后在 `settings-notice` 下方展开一块面板（不是新路由、不做逐行预览表）：

1. 「读取本机 `~/.pi/agent/models.json`」
2. 选择 `.json` 文件（`<input type="file" accept="application/json,.json">`，前端读文本再 POST）
3. 粘贴 JSON 的 textarea
4. 三个 checkbox，文案对齐 D5
5. 「预览转换」→ 调端点 → 用 `alert` / 面板内报告列出：将新增的 provider/模型数、将跳过的项、warnings
6. 「应用到编辑区」→ 前端合并 + `markDirty()` + `render()` + 收起面板
7. 「取消」收起，不改工作副本

不做逐模型勾选表（一次导入量通常 < 20，漏了可重置或手删）。报告必须能看清「为什么跳过」。

### D7 默认路径只读、不接受任意 path

服务端常量：`Path.home() / '.pi' / 'agent' / 'models.json'`。

- 文件不存在 / 不可读 → 400，中文消息指明路径。
- **禁止**请求里带服务器本地 path（防任意文件读取）。
- 上传/粘贴走请求体 `document`（已解析对象）或 `rawText`（服务端 `json.loads`）。两者都给时 `document` 优先。

### D8 转换器放 Web 层，库零改动

新文件 `webApp/backend/piModelsImport.py`，只被该端点调用。`flamingoAgents.models.modelConfig` / 适配器不改——导入结果必须能被**现有**解析器消化。

不引入测试框架；转换是纯函数，验收靠 §5 清单 + 用本机真实 `models.json` 走一遍 UI。

### D9 契约小幅扩展，PUT schema 不动

只加一个 POST。`modelConfig` 文档形状、GET/PUT 脱敏与校验一字不改。导入产生的对象必须能直接放进现有工作副本并通过前端 `validate()` + 后端 `validateBody()`。

---

## 2. 接口契约（拟增，实施时写入 `docs/webApiSpec.md`）

### 3.18 POST /api/models/importPi —— 预览 pi models.json 转换（不写盘）

鉴权：与其它 `/api/*` 相同。

请求：

```json
{
  "useDefaultPath": false,
  "document": null,
  "rawText": null
}
```

| 字段 | 规则 |
|---|---|
| `useDefaultPath` | 布尔，缺省 `false`。`true` 且未提供有效 `document`/`rawText` 时读 `~/.pi/agent/models.json` |
| `document` | 对象，已解析的 pi JSON |
| `rawText` | 字符串，服务端 `json.loads` |

优先级：`document`（对象）> `rawText` > `useDefaultPath`。三者都无效 → 400 `请提供 models.json 内容，或指定读取本机默认路径。`。

`useDefaultPath=true` 同时带了 body：以 body 为准，不读盘（避免「我贴了内容却读到本机另一份」）。

200：

```json
{
  "source": "body",
  "path": null,
  "providers": { "...flamingo §2.4 形状，apiKey 为 pi 原值（明文或 $ 引用，不做 __KEEP__ 脱敏）..." },
  "report": {
    "importedProviders": ["deepseek"],
    "importedModels": [{ "providerId": "deepseek", "modelId": "deepseek-v4-flash" }],
    "skippedProviders": [{ "id": "huoshan", "reason": "api 为 anthropic-messages，当前仅支持 openai-completions。" }],
    "skippedModels": [],
    "warnings": [
      "provider「sub2api_gpt」模型「gpt-5.6-sol」的 cost.tiers 已忽略（flamingo 不支持分档计价）。",
      "provider「kimi」的 compat 已忽略。"
    ]
  }
}
```

- `source`：`body` | `defaultPath`
- `path`：仅 `defaultPath` 时回展开后的绝对路径，便于 UI 展示；`body` 时为 `null`
- `providers`：**不是**脱敏后的 GET 形状。apiKey 保持 pi 原值，方便「新建 provider」写入工作副本。已有 provider 是否采用该 key 由前端 D5 决定。
- `providers` 允许为空对象（全部被跳过也是 200，报告里写原因）。前端据此禁用「应用到编辑区」。

400：

- JSON 文本非法：`models.json 不是合法 JSON：…`
- 顶层不是对象 / 无 `providers` 对象：`models.json 必须是包含 providers 对象的 JSON。`
- 默认路径不存在 / 不可读：`未找到本机 pi 配置：{absPath}`
- `providers` 不是对象：同上结构错误

无副作用：不读不写 `models.yaml`、不动 agent 缓存。

---

## 3. 转换算法（`convertPiDocument(raw) -> (providers, report)`）

伪代码级，实施时按此写，不自行加字段。

```
require raw 是 dict 且 raw.providers 是 dict（可空）

for providerId, provider in providers.items():
    if providerId 不是非空字符串: warning + continue
    if provider 不是 dict: skippedProviders + continue
    if provider.modelOverrides 存在: warning「flamingo 无内置目录，modelOverrides 已忽略」
    if provider.compat 存在: warning「compat 已忽略」

    providerApi = provider.api  # 可能缺
    baseUrl = provider.baseUrl
    if 不是非空字符串: skippedProviders「缺少 baseUrl」; continue

    apiKey = 规范化 apiKey：
        非字符串 / 空白 → ''
        startswith('!') → '' + warning「!command 不执行，apiKey 置空」
        其余原样 strip

    headers = 只保留 str→str；空则省略

    outModels = []
    seenIds = {}
    for model in provider.models（非 list 则视为空）:
        if model 不是 dict 或 id 非非空字符串: skippedModels; continue
        effectiveApi = model.api or providerApi
        if effectiveApi != 'openai-completions':
            skippedModels「api 为 {effectiveApi or 缺失}」; continue
        if model.baseUrl 存在: warning「模型级 baseUrl 已忽略」
        if model.compat 存在: warning「模型级 compat 已忽略」
        if cost.tiers 存在: warning 忽略

        填缺省：name / input / contextWindow / maxTokens / cost 四字段
        D3 推导 reasoning / thinking / reasoningEffort
        模型 headers 同 provider 规则

        若 id 已在 seenIds: warning「重复 id，后者覆盖」并替换
        else append

    if outModels 为空: skippedProviders「没有可导入的 openai-completions 模型」; continue
    写入结果 providers[providerId]
```

转换结果里每个 model **必带** PUT 所需字段：`id/name/input/contextWindow/maxTokens/reasoning/cost`；`thinking`/`reasoningEffort`/`headers` 按需。`api` 不写在 model 上（flamingo schema 没有）。provider 上写 `api: openai-completions`。

数字：`contextWindow`/`maxTokens` 接受 `int`（拒绝 bool）；`1.0` 这种 float 不当作正整数，走缺省 + warning。cost 接受 int/float，负数当 0 + warning。

---

## 4. 前端合并（`mergePiImport(working, imported, policy)`）

- 遍历 `imported.providers`。
- 工作副本无该 id → 深拷贝整组（含 apiKey）；记入 newProviderIds，便于改名。
- 已有该 id：
  - 按 D5 决定是否改 `baseUrl`/`headers`/`apiKey`。
  - `api` 强制保持 / 写成 `openai-completions`（与现表单一致）。
  - models 按 `id` 索引：新 id 追加；冲突则 skip 或替换。替换时：`Object.assign({}, oldModel, newModel)`，新字段覆盖，旧的 `stream` 等仍在。
- 返回 `{addedProviders, addedModels, overwrittenModels, skippedModels}` 供成功提示。

应用到编辑区后：若当前 tab 的 provider 被删不存在（不会发生）则不管；若导入了新 provider 且原来一个都没有，切到第一个新 tab。已有 tab 保持，避免用户正在看的表单被切走。

---

## 5. 影响面与兼容

| 项 | 影响 |
|---|---|
| `config/models.yaml` | 仅用户点「保存」后变化；导入本身为零 |
| GET/PUT `/api/models` | 不变 |
| 库 `modelConfig` / 适配器 | 零改动 |
| 会话 / agent 缓存 | 仅保存后走既有 invalidate |
| 前端路由 | 仍 `#/settings/models`，无新 hash |
| 契约版本 | `webApiSpec` 1.8 → 1.9，新增 §3.18 |
| 安全 | 不执行 `!command`；不读任意 path；导入的明文 key 只进内存工作副本，GET 回拉仍脱敏 |

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 覆盖正在用的 apiKey | 默认不覆盖；开关默认关；文案写明 |
| `!command` 被当明文或被执行 | 检测 `!` 前缀，置空 + warning，绝不 `subprocess` |
| anthropic / responses 被写成 openai 端点，运行期 4xx | D4 直接跳过，报告写 api 值 |
| `thinkingLevelMap` 压档后与 pi 会话里当前档不一致 | 取最高可用档（与现 yaml 手填 `max`/`high` 习惯一致）；用户可在表单改思考强度 |
| 模型级 `baseUrl` 丢掉导致同 provider 下不同端点失效 | 真实文件无此用法；有则 warning，用户拆成两个 provider |
| 远程访问时「读取本机」读的是**服务器** home | 按钮文案写「读取**服务器**本机 `~/.pi/agent/models.json`」；同时提供上传/粘贴 |
| 脏数据上导入难以撤销 | 先 confirm；仍可用「重置」回 GET |
| 超大 JSON | 真实文件 ~10KB；`rawText` 不设专门上限（已受请求体限制）。不预读 models-store |
| JSON 带注释 / 尾逗号 | 标准 `json.loads` 失败即 400，提示用合法 JSON（用户当前文件是合法的） |

---

## 7. 明确不做什么（防范围膨胀）

- 不导入 `models-store.json`、`auth.json`、`settings.json`。
- 不新增 flamingo 对 anthropic / responses / thinkingLevelMap / compat / cost.tiers 的运行时支持。
- 不把导入做成双向同步，也不写回 pi 的 `models.json`。
- 不在 CLI / `sdkEntry` 加子命令。
- 不改 PUT 合并语义、不改 apiKey 脱敏。
- 不引入测试框架、不新增构建步骤。

---

## 8. TODO（实施顺序）

- [ ] T1 `webApp/backend/piModelsImport.py`：`convertPiDocument` + 报告结构 + 默认路径常量。验证：用本机 `~/.pi/agent/models.json` 在 repl/`python -c` 跑一遍，huoshan 进 skipped，其余 openai 模型都在，kimi 四模型 thinking/effort 为 enabled + `max`。
- [ ] T2 `server.py`：`POST /api/models/importPi`（鉴权路由内），按 §2 优先级读入、400 口径、调用 T1。验证：curl 三种输入（body document / rawText / useDefaultPath）+ 缺参 400 + 坏 JSON 400。
- [ ] T3 `docs/webApiSpec.md`：版本 1.9，新增 §3.18，目录/头部变更记录同步。
- [ ] T4 `api.js`：`importPiModels(body)` → `POST /api/models/importPi`。
- [ ] T5 `index.html`：底栏加「从 pi 导入」；settings 区加可隐藏面板（默认路径按钮 / file input / textarea / 三 checkbox / 预览 / 应用 / 取消 / 报告容器）。
- [ ] T6 `styles.css`：面板用现有 `settings-field` / `form-input` / `btn`，少加 class（一块边框 + 间距即可）。
- [ ] T7 `settingsView.js`：打开/关闭面板；预览；`mergePiImport`；dirty / newProviderIds / render；成功用现有 `alert` 汇总。验证：§5 清单。
- [ ] T8 文件头版本号：`server.py`、`api.js`、`settingsView.js`、`index.html`、`styles.css` 小版本 + description 写明本功能。

---

## 9. 验收清单

1. 设置页底栏能看到「从 pi 导入」；点开面板、取消后工作副本不变、不 dirty。
2. 「读取服务器本机默认路径」在本机有 `~/.pi/agent/models.json` 时返回转换结果；`huoshan` 出现在 skipped，原因含 `anthropic-messages`。
3. 粘贴 / 选文件两条路径与默认路径转换结果一致（同一份文件）。
4. 默认三开关：已有 `kimi`/`sub2api_gpt` 等 **apiKey 仍是 `__KEEP__`**，已有模型字段不被 pi 覆盖；pi 里多出来的模型（如 kimi 的 `kimi-for-coding`）出现在该 tab 模型列表。
5. 新 provider（`deepseek` / `glm` / `sub2api_volcano`）出现在 tab 条，apiKey 为 pi 明文（可点眼睛看见），思考强度为推导档（deepseek-v4-flash → `max`，因其 map 的 xhigh→`max` 且 max 键缺省、高档取 xhigh 映射值？**按 D3 顺序 `max` 键无字符串则落到 `xhigh` 的值 `"max"`**）。
6. 打开 `overwriteModels` 再导入：已有同 id 模型的 `contextWindow`/`cost` 变成 pi 值；`stream` 若 yaml 里有则仍在（保存后 `.bak` 可对）。
7. `overwriteApiKey` 关闭时改不掉已有 key；打开后工作副本 apiKey 变成 pi 值，保存后 yaml 被新 key 替换（先在用得起的副本上试）。
8. 含 `!security ...` 的假 apiKey：该 provider 仍导入，apiKey 空，warning 提到不执行命令。
9. 非法 JSON / 空 body / 默认路径缺失：面板显示中文错误，不改工作副本。
10. 应用后 dirty 提示出现；点「重置」放弃；点「保存」走原 PUT，侧栏模型列表能看到新模型，新建会话能选中（openai 的那些）。
11. 全程不写 `models-store.json`，不改 `flamingoAgents/` 库文件。

---

## 10. 实施时的假设（与「编码前思考」对齐）

1. 用户要的是 **Web 模型配置页的导入**，不是 CLI 子命令。
2. 只认 pi 自定义文件 `models.json`，不要内置目录缓存。
3. 不在本期让 flamingo 学会 anthropic / thinking 多档。
4. 导入是一次性搬运，不是和 pi 双向同步。

若这四条有一条不对，先改本文再写代码。
