# 从 pi `models.json` 导入模型配置 —— 方案与计划

Author: wilbur
Version: 1.4
Date: 2026-08-13
Description: 在模型配置页增加「从 pi models.json 导入」：用户**上传**一份 pi 格式的 `models.json`，后端只做纯转换（不读盘、不写盘），前端把结果合进工作副本，经现有 PUT 落盘。不引入新协议、不改库适配器。
v1.1 审核修订（M1–M6 / L1 L2 L4 L5 L8）：非 openai provider 整组 skippedProviders；overwriteModels 只保留 schema 外字段；预览用前端 dry-run；headers 的 !/$ 警告；缺 apiKey 警告；空 key 不覆盖在用 key。
v1.2 复审修订（M-新1 / L-a L-b L-d）：headers 删除口径改为工作副本置 `{}`（对齐 PUT「空对象=删除」）；mergePiImport 签名统一；空 providerId 进 skippedProviders；面板打开守卫 modelConfig。
v1.3 需求纠偏：**唯一输入是用户上传的文件**。服务端禁止读取 `~/.pi/agent/models.json` 或任何本机路径；去掉 `useDefaultPath`、默认路径常量、粘贴框。本机 pi 文件仅作调研样本，不是运行时数据源。
v1.4 复审修订：FileReader 强制 UTF-8；空文件前端拦截不发请求；应用只 POST 一次；换文件挂 change 清预览；取消时一并清报告。

## 0. 调研结论（已读码 / 已对照真实样本）

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

用户从 pi 拷出的自定义模型文件，顶层只有 `providers`。调研时对照过一份真实样本以核对字段，**运行时绝不去读该路径**。

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

真实样本额外出现、文档未列的字段：`reasoning_effort`（snake_case，glm / huoshan / sub2api_volcano 上有）。按「同语义遗留字段」纳入映射。

**不在范围内**：`models-store.json`（内置目录缓存，形状是 `{provider: {models, checkedAt, etag}}`，含 Codex OAuth 等 flamingo 跑不了的 api）。本期只认用户上传的 `models.json`。

### 0.3 两边字段对照

| pi | flamingo | 导入策略 |
|---|---|---|
| `providers.<id>.baseUrl` | 同 | 原样；缺则整 provider 跳过 |
| `api` | 仅 `openai-completions` | **provider.api 非空且 ≠ openai-completions、且没有任何 model 级 `api` 覆盖** → 整组 skippedProviders（原因含该 api 值，不再逐模型记 skippedModels）；否则按模型过滤，滤完无模型 → 跳过 provider |
| `apiKey` 明文 / `$ENV` / `${ENV}` | 同 | 原样进入转换结果；缺失/空/“!command” 置空 + warning（flamingo 无 auth.json/oauth 回退） |
| `apiKey: "!…"` | 无 | **strip 之后** 以 `!` 开头 → 置空 + warning，不执行命令 |
| `headers` | 同（`dict[str,str]`） | 只保留字符串键值；值以 `!` 开头或含 `$` → 该键跳过 + warning（flamingo 不解析 pi 取值语法）；空则省略。**模型级 headers 静默进工作副本，UI 不展示**（与 thinking 同例） |
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

用一份真实 pi `models.json` 样本干跑预期（仅验证映射，不是运行时读盘）：

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

### D2 转换在后端，合并在前端；输入只有上传内容

新增只读转换端点（§2），纯函数：用户上传的 JSON 文本 → flamingo 形状 + 报告。前端把结果按 D5 合并进工作副本。

放后端的理由：映射规则与中文报告只需一份实现，前端不复制一套。

**红线**：端点**只认请求体里的 JSON 文本**。不读 `~/.pi/**`、不接收服务器本地 path、不提供「读取本机默认文件」开关。转换器模块里不得出现 `Path.home()` / `.pi` / `models.json` 路径常量。

上传走现有 JSON `request()`：前端 `FileReader.readAsText` 读出文本，POST `{ rawText }`。不走 multipart（现有 `api.js` 全是 JSON，不必为单文件新开一条上传通道）。

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

分两层，避免「整组 anthropic」被拆成 N 条 skippedModels + 一条不含 api 名的 skippedProviders（审核 M1）：

1. **整组短路**：`provider.api` 非空且 ≠ `openai-completions`，且该 provider 下**没有任何** model 自带 `api` 字段 → 直接 `skippedProviders`，原因 `api 为 {provider.api}，当前仅支持 openai-completions。`，不遍历 models。
2. **按模型过滤**：否则（provider.api 缺失 / 就是 openai-completions / 有模型级覆盖）逐模型看 `effectiveApi = model.api or provider.api`，≠ `openai-completions`（含缺失）→ 该模型进 `skippedModels`。滤完 `outModels` 为空 → `skippedProviders`「没有可导入的 openai-completions 模型」。

`huoshan`（provider.api=`anthropic-messages`、模型无 api 覆盖）走第 1 条，验收读 `skippedProviders.reason` 即可看到 `anthropic-messages`。不在本期扩展协议。

### D5 合并策略：默认保守，三开关可选加强

| 情形 | 默认 | 打开对应开关后 |
|---|---|---|
| 新 providerId | 整组加入（含 apiKey） | — |
| 已有 provider，新 model id | 追加到该 provider.models | — |
| 已有 provider + 已有 model id | **跳过该模型** | `overwriteModels`：schema 内字段以转换结果为准（缺省即删旧值），**仅**拷贝旧对象上不在 schema 清单里的 key（如 `stream`）。禁止 `Object.assign` 整表合并（审核 M2） |
| 已有 provider 的 `baseUrl` / `headers` | **保持现有** | `overwriteProviderFields`：用 pi 的值覆盖 `baseUrl`；headers 整表替换。转换结果省略 headers → **工作副本置 `headers: {}`**（不能删 key：PUT `mergeProvider` 只在 `'headers' in body` 且为空对象时才从 yaml 删掉该字段，缺 key 会让旧 headers 复活） |
| 已有 provider 的 `apiKey` | **永远保持现有**（含 `__KEEP__`） | `overwriteApiKey`：仅当 pi 侧 apiKey **非空** 时写入工作副本（明文或 `$` 引用）。pi 侧为空（缺 key / `!command`）→ **不写空串**，保持现有 key，并在 dry-run 报告里说明（审核 M6） |

默认三开关全关。密钥默认不覆盖，避免把正在用的 key 换成上传文件里另一份。

导入前若工作副本 `dirty`：`confirm('将在当前未保存修改上继续导入。继续？')`。

### D6 入口与交互：文件选择 + 轻量报告，不加完整预览表

设置页底栏「重置」左侧加按钮 **「从 pi 导入」**。点开后在 `settings-notice` 下方展开一块面板（不是新路由、不做逐行预览表）：

1. `<input type="file" accept="application/json,.json">`，文案「选择 models.json」。未选文件时「预览 / 应用」禁用。读文件一律 `fileReader.readAsText(file, 'UTF-8')`（带 BOM 的 UTF-8 由服务端 `json.loads` 容忍；非 UTF-8 会 400）。读出文本 `strip` 为空 → 面板报「文件为空或全是空白字符。」，**不发请求**。
2. 三个 checkbox，文案对齐 D5
3. 「预览转换」→ 读文件 → 一次 `POST { rawText }` → 前端 `mergePiImport(working, imported, policy, true)`（不 `markDirty`、不改副本）。面板报告分两层（审核 M3）：
   - 端点 `report`：转换期 skippedProviders / skippedModels / warnings（如 huoshan 的 api、`!command`、缺 key、compat）
   - dry-run：相对**当前编辑区**将新增的 provider/模型、将覆盖的模型、因同 id 且未开 overwrite 而跳过的模型
4. 「应用到编辑区」：若已有**当前这份文件**的转换结果，直接 `mergePiImport(working, imported, policy, false)`，不再 POST。尚未预览则先一次 POST 再合并（全程最多一次 POST）。然后 `markDirty()` + `render()` + 收起面板。`modelConfig == null` 时打开面板/预览/应用直接 return（与现有 `addProvider`/`save` 守卫一致）。
5. 「取消」收起，不改工作副本；清掉 file input 选中状态，并清空报告容器与缓存的转换结果。
6. `fileInput.addEventListener('change', …)`：换文件后清空上一份预览报告与缓存结果，避免套用过期数据。同文件重选若不触发 change，不清（正确）。

不做粘贴框、不做「读取本机默认路径」。不做逐模型勾选表（一次导入量通常 < 20，漏了可重置或手删）。报告必须能看清「为什么跳过」。预览与套用必须走同一合并函数，避免两套口径。

### D7 服务端零读盘

- 请求体只有 `rawText`（非空字符串）。缺 / 非字符串 / 空白 → 400 `请上传 models.json 文件。`
- **禁止**任何 `path` / `useDefaultPath` / `document` 字段。出现也不读，当未知字段忽略。
- `json.loads` 自己捕获 `JSONDecodeError` 再转 400（它是 `ValueError` 子类，打不到 `runtimeErrorHandler`，会落 500）。
- 转换器与路由都不得 `open()` / `Path.read_text` 任何配置文件。

### D8 转换器放 Web 层，库零改动

新文件 `webApp/backend/piModelsImport.py`，只被该端点调用。`flamingoAgents.models.modelConfig` / 适配器不改——导入结果必须能被**现有**解析器消化。

不引入测试框架；转换是纯函数，验收靠 §9 清单 + 用户上传一份真实 `models.json` 走一遍 UI。

### D9 契约小幅扩展，PUT schema 不动

只加一个 POST。`modelConfig` 文档形状、GET/PUT 脱敏与校验一字不改。导入产生的对象必须能直接放进现有工作副本并通过前端 `validate()` + 后端 `validateBody()`。

---

## 2. 接口契约（拟增，实施时写入 `docs/webApiSpec.md`）

### 3.18 POST /api/models/importPi —— 预览用户上传的 pi models.json（不写盘、不读盘）

鉴权：与其它 `/api/*` 相同。

请求：

```json
{ "rawText": "{ ... pi models.json 原文 ... }" }
```

| 字段 | 规则 |
|---|---|
| `rawText` | 必填非空字符串，服务端 `json.loads` |

缺字段 / 非字符串 / 空白 → 400 `请上传 models.json 文件。`

200：

```json
{
  "providers": { "...flamingo §2.4 形状，apiKey 为上传文件原值（明文或 $ 引用，不做 __KEEP__ 脱敏）..." },
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

- 不回 `source` / `path`（没有默认路径这回事）。
- `providers`：**不是**脱敏后的 GET 形状。apiKey 保持上传文件原值，方便「新建 provider」写入工作副本。已有 provider 是否采用该 key 由前端 D5 决定。
- `providers` 允许为空对象（全部被跳过也是 200，报告里写原因）。前端据此禁用「应用到编辑区」。

400：

- JSON 文本非法：`models.json 不是合法 JSON：…`
- 顶层不是对象 / 无 `providers` 对象：`models.json 必须是包含 providers 对象的 JSON。`

无副作用：不读不写 `models.yaml`、不读 `~/.pi/**`、不动 agent 缓存。

---

## 3. 转换算法（`convertPiDocument(raw) -> (providers, report)`）

伪代码级，实施时按此写，不自行加字段。本函数只吃已解析的 dict，不碰文件系统。

```
require raw 是 dict 且 raw.providers 是 dict（可空）

normalizeHeaders(source, location):
    只保留 str→str
    value.strip 后以 '!' 开头或含 '$' → 丢弃该键 + warning「{location} 的 header「{key}」使用了 pi 取值语法，flamingo 不解析，已跳过」
    空则返回省略（调用方不写该字段）

for providerId, provider in providers.items():
    if providerId 不是非空字符串: skippedProviders「providerId 为空」+ continue
    if provider 不是 dict: skippedProviders + continue
    if provider.modelOverrides 存在: warning「flamingo 无内置目录，modelOverrides 已忽略」
    if provider.compat 存在: warning「compat 已忽略」

    providerApi = provider.api  # 可能缺
    baseUrl = provider.baseUrl
    if 不是非空字符串: skippedProviders「缺少 baseUrl」; continue

    # D4 整组短路（审核 M1）
    modelsList = provider.models if list 否则 []
    anyModelApiOverride = 任一 model 是 dict 且带 api 字段
    if providerApi 非空且 != 'openai-completions' 且 not anyModelApiOverride:
        skippedProviders「api 为 {providerApi}，当前仅支持 openai-completions。」
        continue

    apiKey = 规范化 apiKey：
        非字符串 / 空白 → '' + warning「未配置 apiKey（pi 可能走 auth.json/oauth，flamingo 不支持），保存后需手动补 key」
        strip 之后 startswith('!') → '' + warning「!command 不执行，apiKey 置空」
        其余原样 strip

    headers = normalizeHeaders(provider.headers, providerId)

    outModels = []
    seenIds = {}
    for model in modelsList:
        if model 不是 dict 或 id 非非空字符串: skippedModels; continue
        effectiveApi = model.api or providerApi
        if effectiveApi != 'openai-completions':
            skippedModels「api 为 {effectiveApi or 缺失}」; continue
        if model.baseUrl 存在: warning「模型级 baseUrl 已忽略」
        if model.compat 存在: warning「模型级 compat 已忽略」
        if cost.tiers 存在: warning 忽略

        填缺省：name / input / contextWindow / maxTokens / cost 四字段
        D3 推导 reasoning / thinking / reasoningEffort
        模型 headers = normalizeHeaders(model.headers, providerId/modelId)；有则写入，无则省略（不写空对象）

        若 id 已在 seenIds: warning「重复 id，后者覆盖」并替换
        else append

    if outModels 为空: skippedProviders「没有可导入的 openai-completions 模型」; continue
    写入结果 providers[providerId]
```

转换结果里每个 model **必带** PUT 所需字段：`id/name/input/contextWindow/maxTokens/reasoning/cost`；`thinking`/`reasoningEffort`/`headers` 按需。`api` 不写在 model 上（flamingo schema 没有）。provider 上写 `api: openai-completions`。

数字：`contextWindow`/`maxTokens` 接受 `int`（拒绝 bool）；`1.0` 这种 float 不当作正整数，走缺省 + warning。cost 接受 int/float，负数当 0 + warning。

---

## 4. 前端合并（`mergePiImport(working, imported, policy, dryRun)`）

纯函数。`dryRun=true` 只返回统计、不改 `working`（预览用）；`false` 就地改 `working`。

schema 内字段清单（覆盖时只认这些，缺省即从旧对象删除）：
`id` / `name` / `input` / `contextWindow` / `maxTokens` / `reasoning` / `thinking` / `reasoningEffort` / `cost` / `headers`。

- 遍历 `imported.providers`。
- 工作副本无该 id → 深拷贝整组（含 apiKey，可为空）；非 dryRun 时记入 `newProviderIds`，便于改名。
- 已有该 id：
  - `overwriteProviderFields`：改 `baseUrl`；headers 以转换结果为准。转换结果省略 headers → **置 `provider.headers = {}`**（PUT 空对象=删除；删 key 会让 yaml 旧 headers 复活）。默认保持现有。
  - `overwriteApiKey` 且 pi 侧 apiKey 非空：写入。pi 侧为空 → 保持现有，计入 `keptApiKeysBecauseEmpty`。
  - `api` 强制写成 `openai-completions`。
  - models 按 `id` 索引：新 id 追加；冲突且未开 overwrite → `skippedExistingModels`；开了则替换：
    `replaced = { ...只拷旧对象上不在 schema 清单里的 key }; Object.assign(replaced, newModel)`。
    新模型缺 `thinking`/`reasoningEffort` 时从 replaced 删掉旧值；**缺 `headers` 时置 `replaced.headers = {}`**（与 provider 同口径，不能删 key）。
- 返回 `{addedProviders, addedModels, overwrittenModels, skippedExistingModels, keptApiKeysBecauseEmpty}`。

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
| 安全 | 不执行 `!command`；**不读任何本机路径**；导入的明文 key 只进内存工作副本，GET 回拉仍脱敏。端点 `providers` 会回传上传文件全部明文 key（含默认策略下前端不会采用的已有 provider）——鉴权内、有意为之，方便新建 provider 一次写进副本 |

---

## 6. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 覆盖正在用的 apiKey | 默认不覆盖；开关默认关；文案写明 |
| `!command` 被当明文或被执行 | 检测 `!` 前缀，置空 + warning，绝不 `subprocess` |
| anthropic / responses 被写成 openai 端点，运行期 4xx | D4 直接跳过，报告写 api 值 |
| `thinkingLevelMap` 压档后与 pi 会话里当前档不一致 | 取最高可用档（与现 yaml 手填 `max`/`high` 习惯一致）；用户可在表单改思考强度 |
| 模型级 `baseUrl` 丢掉导致同 provider 下不同端点失效 | 真实样本无此用法；有则 warning，用户拆成两个 provider |
| 误读服务器 `~/.pi`（上一版的错） | 端点无路径参数、转换器无默认路径、UI 无「读取本机」按钮 |
| 脏数据上导入难以撤销 | 先 confirm；仍可用「重置」回 GET |
| 超大 JSON | 真实样本 ~10KB；本期不设专门上限（FastAPI/uvicorn 默认也无请求体上限，单用户可接受）。不预读 models-store |
| JSON 带注释 / 尾逗号 | 标准 `json.loads` 失败即 400，提示用合法 JSON |
| 选了文件但未预览就点应用 | 应用复用已缓存的转换结果；没有则先一次 POST 再本地合并，绝不连打两次 |
| 非 UTF-8 / 空文件 | 读文件强制 UTF-8；strip 为空前端拦截，不发请求 |

---

## 7. 明确不做什么（防范围膨胀）

- **不读取** `~/.pi/agent/models.json`、`models-store.json`、`auth.json`、`settings.json`，也不提供默认路径开关。
- 不新增 flamingo 对 anthropic / responses / thinkingLevelMap / compat / cost.tiers 的运行时支持。
- 不把导入做成双向同步，也不写回 pi 的 `models.json`。
- 不在 CLI / `sdkEntry` 加子命令。
- 不改 PUT 合并语义、不改 apiKey 脱敏。
- 不做粘贴框、不做 multipart 上传、不做逐模型勾选表。
- 不引入测试框架、不新增构建步骤。

---

## 8. TODO（实施顺序）

- [x] T1 `webApp/backend/piModelsImport.py`：只有 `convertPiDocument` + 报告结构。**文件内不得出现默认路径 / `Path.home` / `.pi`。** 验证：把一份真实 `models.json` 当字符串喂进去，huoshan 进 `skippedProviders`（reason 含 `anthropic-messages`，且不出现在 skippedModels），其余 openai 模型都在，kimi 四模型 thinking/effort 为 enabled + `max`。
- [x] T2 `server.py`：`POST /api/models/importPi`（鉴权路由内），只读 `rawText`，捕获 `JSONDecodeError` 转 400，调用 T1。验证：curl 合法 rawText 200；缺参 / 空白 / 坏 JSON 均为 400；确认代码里没有读 `~/.pi`。
- [x] T3 `docs/webApiSpec.md`：版本 1.9，新增 §3.18（仅 `rawText`，声明不读盘；注明响应 `providers` 含上传文件明文 apiKey，鉴权内有意为之），目录/头部变更记录同步。
- [x] T4 `api.js`：`importPiModels(rawText)` → `POST /api/models/importPi` `{ rawText }`。
- [x] T5 `index.html`：底栏加「从 pi 导入」；settings 区加可隐藏面板（file input / 三 checkbox / 预览 / 应用 / 取消 / 报告容器）。无默认路径按钮、无粘贴框。
- [x] T6 `styles.css`：面板用现有 `settings-field` / `form-input` / `btn`，少加 class（一块边框 + 间距即可）。
- [x] T7 `settingsView.js`：打开/关闭面板（`modelConfig == null` 直接 return）；`readAsText(file, 'UTF-8')`；空文件前端拦截；预览 = 一次 POST + `mergePiImport(..., true)`；应用 = 复用缓存或一次 POST + `mergePiImport(..., false)`；`change` 清预览；取消清 input/报告/缓存；dirty / newProviderIds / render；成功用现有 `alert` 汇总。验证：§9 清单。
- [x] T8 文件头版本号：`server.py`、`api.js`、`settingsView.js`、`index.html`、`styles.css` 小版本 + description 写明「上传 models.json 导入」。

---

## 9. 验收清单

1. 设置页底栏能看到「从 pi 导入」；点开面板只有文件选择 + 三开关，**没有**「读取本机 / ~/.pi」入口；取消后工作副本不变、不 dirty。
2. 上传一份含 huoshan 的真实 `models.json`：`huoshan` 出现在 **`skippedProviders`**，`reason` 含 `anthropic-messages`（整组短路，不拆成逐模型 skippedModels）。
3. 服务端代码（`piModelsImport.py` / `server.py` 新增部分）不含 `Path.home`、`.pi`、`useDefaultPath`；curl 不带 rawText 得到 400 `请上传 models.json 文件。`，不会去读任何文件。
4. 默认三开关：已有 `kimi`/`sub2api_gpt` 等 **apiKey 仍是 `__KEEP__`**，已有模型字段不被覆盖；文件里多出来的模型（如 kimi 的 `kimi-for-coding`）出现在该 tab 模型列表。
5. 新 provider 出现在 tab 条，apiKey 为文件明文（可点眼睛看见），思考强度按 D3 推导。下列数字是**映射规则示例**（对应用户上传的那份样本时应成立，不是写死在代码里的断言）：deepseek-v4-flash 的 map 无 `max` 键 → 取 `xhigh` 的值 `"max"`；kimi 各模型取 `max`；grok-4.5 取 `high`。
6. 打开 `overwriteModels` 再导入：已有同 id 模型的 `contextWindow`/`cost` 变成文件值；`stream` 若 yaml 里有则仍在（保存后 `.bak` 可对）。
7. `overwriteApiKey` 关闭时改不掉已有 key；打开且文件侧 key 非空时工作副本变成文件值；打开但文件侧 key 为空时**保持现有**（不写空串）。保存后 yaml 仅在真正覆盖时被新 key 替换（先在用得起的副本上试）。
8. 含 `!security ...` 的假 apiKey：该 provider 仍导入，apiKey 空，warning 提到不执行命令。
9. 非法 JSON / 未选文件 / 空文件或全空白：面板显示中文错误，不改工作副本；空文件**不发** POST。
10. 应用后 dirty 提示出现；点「重置」放弃；点「保存」走原 PUT，侧栏模型列表能看到新模型，新建会话能选中（openai 的那些）。
11. 全程不读不写 `~/.pi/**`，不改 `flamingoAgents/` 库文件。
12. 预览报告两层齐全：端点层能看到 huoshan / compat / 缺 key；dry-run 层能看到「k3 因已存在跳过、kimi-for-coding 将新增」。改开关后点预览，dry-run 数字跟着变。换文件后旧报告消失。
13. 模型级 headers 若源文件有，保存后 yaml 里该模型带 headers，设置页不展示控件（与 thinking 一致）。真实样本目前只有 provider 级 UA，可用手造一份验收。
14. 打开 `overwriteProviderFields` / `overwriteModels`，源侧无 headers、yaml 侧有：工作副本对应处变为 `{}`，保存后 yaml 该 headers 字段消失（不是「删 key 后旧值复活」）。

---

## 10. 实施时的假设（与「编码前思考」对齐）

1. 用户要的是 **Web 模型配置页上传一份 models.json**，不是去读服务器或开发者本机的 pi 配置目录。
2. 只认 pi 自定义文件格式（`providers` 对象），不要内置目录缓存 `models-store.json`。
3. 不在本期让 flamingo 学会 anthropic / thinking 多档。
4. 导入是一次性搬运，不是和 pi 双向同步。

若这四条有一条不对，先改本文再写代码。
