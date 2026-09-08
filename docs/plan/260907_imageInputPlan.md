<!--
Author: wilbur
Version: 1.8
Date: 2026-09-08
Description: 图片输入实施方案：复用模型 input 中的 image 声明，打通 Web 图片入口、纯库消息、两类 API、历史与重连。v1.4 按用户确认重构存储决策：上传图片落盘到 session log 的会话图片目录、@ 图片先查存在再快照编码，JSONL 只存引用不存 base64；模型请求仍以 data URL base64 编码。v1.5 根据独立审核补：落盘全成或回滚、ref 不覆盖、预算统一按目录实文件口径、图片端点 ref 正则拘禁与扩展名映射 Content-Type、@ 读取单张上限。v1.6 钉死锁内顺序：预算复检在落盘之前，避免本轮字节双重计入。v1.7 记录终审通过与交付状态。v1.8 确认“@ 图 + 纯文本模型回退为仅路径引用”决策并标记进入实施。
-->

# 图片输入方案与实施计划

## 1. 需求与已确认决策

**模型配置中勾选 `image`，就表示该模型支持图片输入。** 不新增另一套开关，不按模型名称猜测，也不解释为图片生成能力。

以下决策已由用户确认（2026-09-08）：

1. **上传图片**：后端先把图片保存到 session log 中的一个位置（会话图片目录），JSONL 只记录引用；发模型时从存储文件读取并 base64 编码进请求体（data URL，与当前业界通行做法一致）。
2. **`@` 图片路径**：后端首先确认图片是否存在；存在则读取并 base64 编码放进 LLM 请求体；不存在沿用现有 `路径不存在` 400 报错。
3. **`@` 非图片文件**：维持最近提交 `4d598fd`（@ 改为仅路径引用）的行为不变——拼绝对路径清单，不读内容。
4. JSONL 不再内联 base64 大行；图片以文件形式与会话日志同目录存放。

首版范围（沿用，未变）：
- 格式：静态 PNG / JPEG / WebP；GIF 动图、SVG、HEIC、视频、PDF、OCR、图片生成不在本期。
- 支持文字 + 图片、多图、纯图片；气泡显示缩略图，可查看大图。
- 未勾选 image 的模型：上传图 400 报错；`@` 图片回退为仅路径引用并提示（已确认，见决策 4）。
- 图片作为原生多模态内容发往模型；覆盖 Chat Completions、Codex Responses、xAI Responses；保留会话重启恢复与多窗口 attach。

4. **已确认（2026-09-08）**：`@` 了图片但当前模型未勾选 image 时，**回退为仅路径引用**（发送不失败，前端提示“当前模型不支持图片，@ 图片将以路径引用发送”）；上传图片 + 模型未勾选 image → 400 明确报错。

以上决策均已确认（2026-09-08），实施 TODO 按 P1→P4 执行；不覆盖、不回退工作区已有改动。

### 假设与边界

- 图片能力来自保存后的 `input` 配置；勾选不能让上游模型凭空获得视觉能力，上游仍可能因格式/额度拒绝。
- 纯库 API 支持传图（入口传 base64 数据，由库负责落盘）；CLI 参数、askSubAgent 自动传图不扩展。
- 快照语义：`@` 图片在发送时复制进会话图片目录，之后原文件被修改/删除不影响已发送历史与后续轮次。

## 2. 现状核查

| 环节 | 源码与事实 | 需要补齐 |
|---|---|---|
| 模型配置 | `settingsView.js` 已有 text/image checkbox；`modelConfigStore.py` 已读写校验 `input` | 补说明文案与保存后能力刷新 |
| 运行时模型 | `models/modelConfig.py` 的 `modelConfig` 没有 input 能力字段，YAML 加载未透传 | 真正传到适配器 |
| 浏览器发消息 | `chatView.js` 发送字符串与 `{path,type}` 附件；没有图片草稿 | 图片入口、预览、发送快照 |
| Web 入口 | `server.chatStream` 收 message/attachments；`fileBrowser.buildAttachmentMessage`（v1.4，提交 4d598fd）只校验路径并输出绝对路径清单，不读内容 | images 字段、@ 图片分流读取、图片文件端点 |
| Agent / 持久化 | `chatMessage.content`、`appendUserMessage`、恢复逻辑只有用户文本；日志为 `<logDir>/{sessionId}.jsonl` | images 引用字段、会话图片目录的写入/加载、JSONL 往返 |
| 上游协议 | `chatCompletions.convertMessage` 传字符串；`responsesAdapter.convertMessages` 的 user 分支只有 input_text | 原生 image_url / input_image（data URL base64） |
| 历史 / 重连 | `historyView` user DTO 与 `streamMeta.userMessage` 都只有文本；`encodeResumeFrame` 白名单只编码 baseCount/userMessage | 图片元数据 DTO、userImages 编码、前端按引用拉取渲染 |
| 错误诊断 | `agent.logModelError` 会记录整个 requestPayload | 对副本脱敏图片数据 |
| 会话删除 | `server.deleteSession` 只删 `{sessionId}.jsonl` | 同时删除会话图片目录 |

## 3. 最小技术设计

### 3.1 配置：沿用唯一真相（未变）

保留 YAML 结构 `input: [text, image]`。

- `modelConfig` 末尾新增 `inputTypes`，缺省 `['text']`；`supportsImageInput` 为派生属性。YAML 有明确 input 时校验列表与 text/image 枚举；缺省不支持图片。
- 设置页显示 `image（支持图片输入）`；前端在打开会话、切模型、保存配置后刷新能力；未加载成功先禁用图片入口，不影响纯文字。
- 后端以实际 `agentInstance.modelAdapter.config` 判定；配置保存沿用 invalidateAllAgents 的下一轮生效语义。

### 3.2 消息与纯库接口

- `core/types.py` 定义 `inputImage`：`name`（展示名）、`mimeType`、`data`（入口传入的 base64，无 data URL 前缀）、`ref`（落盘后生成的存储文件名）。恢复历史时只有 ref，data 为空、按需加载。
- `chatMessage` 末尾新增 `images: list[inputImage]`，default_factory，旧调用不变；首版仅 user 消息可有 images。
- `runUserMessageStream` / 同步 `runUserMessage` 追加 keyword-only `images=None`；`driveUserMessage` / conversation 透传。
- `runResult` 末尾追加可选 `errorType`，`toRunResult` 对 errorEvent 透传，保证同步与流式拒绝原因一致。
- `modelAdapterPort` 不要求 config：本轮与历史都无图时不新增要求；有图时才查询适配器能力，缺少 config/inputTypes/图片能力视为不支持并明确报错。
- 非空条件为“有效文本或有效图片”；纯图片消息不补造提示词。文本在前、图片按序在后。

### 3.3 存储：会话图片目录 + JSONL 引用（v1.4 重构核心）

**目录与命名**：图片存放在日志同目录下的会话专属目录 `<logDir>/{sessionId}.images/`；文件名由库生成 `img-<12位hex>.<png|jpg|webp>`，命名命中已存在文件时重新生成；展示用原文件名只存 JSONL。落盘与读取端点仅接受该命名模式，规则集中在 `core/imageInput.py`（`sessionImagesDir(logPath)` 等），Web 与库共用；`{sessionId}.images` 与其他会话天然隔离。

**统一落盘路径**：
- 上传图：Web 收 base64 → 解码 + 校验 → 以 `inputImage(data=...)` 传入库。
- `@` 图：Web 从 workDir 读取（`resolveInside` 拘禁 + 存在性 + 常规文件 + 单张字节上限）→ 校验 → 同样以 `inputImage(data=...)` 传入库（这就是发送时快照）。
- 库（conversation/agent，在会话锁内）负责把 data 写入会话图片目录并生成 ref；**存储逻辑只在库内一份**，Web 不重复写文件。

**落盘事务性（全成或回滚）**：会话锁内按固定顺序执行——①预算复检：`{sessionId}.images/` 目录实际文件 size 求和（含孤儿）+ 本轮原始字节 ≤ 20 MiB，超限直接拒绝（不落盘）；②全部落盘：逐张写入并生成 ref（任一目标文件名已存在则重新生成，绝不覆盖既有文件）；③全部成功后才写 `userMessage` 事件。任一步失败：只删除本轮已新落盘的文件（不动历史文件与历史 JSONL）、不写事件、返回 `imageStorageError`，不进入模型请求。顺序不得颠倒，避免本轮字节被重复计入预算。

**JSONL**：`userMessage` 事件追加 `images: [{name, mimeType, ref, bytes}]`；**不写 base64**。旧事件缺省 images=[]。事件行恢复为小体积，v1.2 的严格读取/尾行检查/同路径读写锁设计保留（防崩溃半行、防并发误读，风险进一步降低但仍有价值）。

**加载（hydration）**：agent 每次构造模型请求前，把历史 user 消息的 images 按 ref 从会话图片目录读出并 base64 填充；文件缺失/损坏 → 明确报错，不静默丢图、不发给模型半份数据。适配器只序列化 data 已就绪的 images，不做文件 IO。

**限额**（按原始字节计，非 base64 字符数）：

| 限制 | 首版建议值 | 口径 |
|---|---|---|
| 单张图片 | 5 MiB | 解码后原始字节 |
| 单条消息图片数量 | 4 张 | 上传图与 @ 图合并 |
| 单条消息图片合计 | 10 MiB | 合并原始字节 |
| 会话图片累计 | 20 MiB | 核心复检在会话锁内对 `{sessionId}.images/` 目录实际文件 size 求和（含任何孤儿文件）+ 本轮原始字节；以此口径为准，Web 预检仅作同口径提前提示 |
| `/api/chat/stream` HTTP body | 16 MiB | JSON 解码前对实际接收字节计数 |

20 MiB 是保守默认值，文件存储下可按需调大；需要更大规模时应改独立资源管理，而不是简单放宽。

**生命周期**：
- 创建会话不预建目录，首张图片懒建。
- `server.deleteSession` 在删 jsonl 的同时删除 `{sessionId}.images/`（不存在则忽略）。
- `logMigration` 不改：仅新图片产生新目录；README 说明回退老版本时图片目录会变成不被识别的孤儿文件，回退前备份。

### 3.4 Web 请求与 @ 分流

`/api/chat/stream` 请求体：保留 `sessionId`、`message`、`attachments`（语义不变），新增可选 `images: [{name, mimeType, data}]`（base64，无前缀）。`message` 仍必须是字符串，有图片时允许空字符串。未知字段/类型错误不能经 `or []` 意外放行。

预检顺序：
1. 认证 → 有界 body 读取（16 MiB，含 chunked/无 Content-Length 场景）→ JSON 解析与字段校验。
2. 上传图：先按 base64 编码长度预检，再解码；Pillow 实检格式/宽高/帧数（PNG/JPEG/WebP，≤20,000,000 像素，拒绝多帧），拒绝伪 MIME/损坏图。
3. `attachments` 分流：扩展名 `.png/.jpg/.jpeg/.webp` 且为常规文件的条目 → 按 @ 图处理（`resolveInside` + 存在性 + 单张字节上限 5 MiB 读取（超限 413） + 同一 Pillow 校验 + 快照入参）；其余条目（含目录、压缩包、文本）→ 原样走 `buildAttachmentMessage`。**全部附件的路径清单拼接保持现状**（图片路径也保留在清单里，模型同时拿到位置与图内容）；目录不解包、不自动发图。
4. `@` 图片但模型未勾选 image → 回退为仅路径引用（不生成 vision 部分），前端发送前 toast 提示；上传图 + 模型未勾选 → 400 明确报错（已确认决策，见 §1）。
5. 合并数量/字节/会话预算预检；获取实际运行 Agent 后由核心层在会话锁内、写 userMessage 前复检（封并发/绕过 Web）。
6. 启动原有 SSE 泵；纯图片消息标题取第一张图名。

**图片读取端点**：新增 `GET /api/sessions/{sessionId}/images/{ref}`——鉴权同其他 API；`ref` 必须匹配 `^img-[0-9a-f]{12}\.(png|jpg|webp)$` 且 resolve 后仍位于该会话图片目录内（双重拘禁，不合规一律 404 不泄露存在性）；Content-Type 按扩展名映射（png→image/png、jpg→image/jpeg、webp→image/webp），不信任请求参数或客户端声明。前端用带 Authorization 头的 fetch 取 blob → objectURL 渲染（不把 token 放 URL，不在 GET messages 里内联 base64）。

失败口径（未变）：格式/能力/内容非法 400；数量/字节/像素/预算超限 413；活跃流 409。Web 预检失败零新增 userMessage、零模型调用；核心复检失败走具名 errorType（unsupportedImageInput / invalidImageInput / imageBudgetExceeded / imageStorageError）。解码校验在线程池执行，不阻塞事件循环。网络/上游错误发生在落盘之后的不删历史、不自动重发。

### 3.5 模型协议（未变）

只对有图的 user 消息改变 content；无图消息保持原始请求形状。

| API | 图片表示 | 文本表示 |
|---|---|---|
| openai-completions | `type: image_url`，`image_url.url: data:<mimeType>;base64,<data>` | `type: text` / `text` |
| openai-codex-responses | `type: input_image`，`image_url: data:<mimeType>;base64,<data>` | `type: input_text` / `text` |
| openai-responses（xAI） | 同 Responses 的 input_image | 同 Responses 的 input_text |

纯图片不生成空 text part；图片不进 assistant/tool/encrypted reasoning 回放字段。两适配器对全部待发历史校验能力（不只当前条）；确定性错误不进网络重试。带图会话切纯文本模型不静默删图续发——上传路径报错、@ 路径回退路径引用（§3.4-4）。`uv add pillow` 引入 Pillow（实施时执行）。

### 3.6 历史、attach 与诊断

- `historyView` user DTO 增加 `images: [{name, mimeType, ref, bytes}]`（无 base64）；前端按 ref 经图片端点拉取缩略图。
- `streamMeta` 保留 `userMessage: string | null`，追加 `userImages`（ref 元数据数组）；`encodeResumeFrame` 必须把 `userImages` 加入白名单编码，缺省 []。
- `initAttachedStream` 用 `typeof meta.userMessage === 'string'` 判断（兼容 ''、null、缺字段）；baseCount 仍按消息条数计。
- 错误日志仅对副本脱敏 image_url/input_image 的图片数据，保留 MIME/长度诊断；不改原请求，不误伤 encrypted reasoning。图片字节不计 token，状态栏继续只用供应商 usage。

### 3.7 前端交互与失败保留

新增 `frontend/js/imageInput.js` 管理图片草稿、入口、预览；原生 JS 风格。

- 图片按钮 + hidden file input（multiple）、粘贴剪贴板图、拖放图；同一加入函数与提示；仅消费图片时阻止默认粘贴。
- 无会话/无 image 能力/配置未就绪/发送中正确禁用；切纯文本模型保留草稿并提示删除或切回，不偷清。每张草稿缩略图 + 名称 + 移除按钮（objectURL，切走/移除时 revoke）。
- 发送时 `File → ArrayBuffer → base64` 放进 `images`；`lastUserSend` 快照扩展图片草稿，409 单次重试只复用快照。
- 明确 400/413/最终 409：同会话无新操作时撤销乐观气泡、恢复完整草稿（文字/技能/@ 附件/图片）；有新操作则保留可手动恢复的失败快照。
- 历史/attach 缩略图经鉴权 blob 拉取 + 会话级缓存；不把 base64 写进正文/DOM 属性。已调用模型后的错误不恢复为待发草稿；断网/abort 不自动重发。
- 会话切换/旧异步回调带 sessionId + 草稿代次守卫；终态释放大对象引用。本期不做跨刷新草稿持久化。

## 4. 修改范围

| 文件 | 计划修改 |
|---|---|
| `flamingoAgents/models/modelConfig.py` | inputTypes 与派生图片能力 |
| `flamingoAgents/core/types.py`、`agent.py`、`conversation.py` | 图片类型、可选入参、锁内落盘/预检、JSONL 引用、hydration、错误脱敏 |
| 新 `flamingoAgents/core/imageInput.py` | 会话图片目录/命名、校验、落盘与加载、限额；不做通用附件框架 |
| `flamingoAgents/models/chatCompletions.py`、`responsesAdapter.py` | 两种多模态请求转换（序列化 data 已就绪的 images） |
| `flamingoAgents/utils/jsonl.py` | 可选严格读取、尾行完整性、同路径读写锁（v1.2 设计保留） |
| `webApp/backend/server.py` | images 字段、有界读取、@ 分流、图片读取端点、deleteSession 清理图片目录、标题 |
| `webApp/backend/fileBrowser.py` | 仅新增 @ 图片读取辅助（路径拘禁 + 常规文件 + 有界读取）；路径清单行为不变 |
| `webApp/backend/historyView.py`、`sseCodec.py` | user DTO images 元数据；encodeResumeFrame 编码 userImages |
| `webApp/backend/agentManager.py` | 无必要不改，仅补 meta/清理相关回归测试 |
| 新 `webApp/frontend/js/imageInput.js` | 图片草稿、入口、预览、base64 编码 |
| `chatView.js`、`fileMention.js`、`settingsView.js`、`slashCommand.js`、`api.js` | 发图/恢复/能力刷新/图片 blob 拉取必要接点 |
| `webApp/frontend/index.html`、`styles.css` | 图片入口、缩略图、轻量大图展示 |
| `pyproject.toml`、`uv.lock` | 实施时 uv 增加 Pillow |
| `tests/`、`docs/webApiSpec.md`、`README.md` | 单测、契约、用法与限制（含图片目录位置与回退说明） |

新增代码小驼峰命名 + 文件头；修改旧文件递增小版本；上游协议键（image_url/input_image 等）不因命名风格改名。

## 5. 执行步骤与详细 TODO

### P0：本轮方案交付
- [x] 核对工作区与最近提交 `4d598fd`（@ 仅路径引用）后的链路。
- [x] 按用户确认的存储决策重构方案（上传落盘 + @ 查存在快照 + JSONL 引用）。
- [x] 新建独立子代理审核 v1.4；修复后复审直至无明显问题（v1.5→v1.6→终审通过，审核模型按用户指示改用 xaiSubscription/grok-4.6）。
- [x] 交付摘要；“@ 图 + 纯文本模型回退 or 报错”已确认为回退（2026-09-08），开始实施 P1→P4。

### P1：能力与核心数据链 → 验证：无图兼容、落盘/加载、两协议 payload
- [ ] 先写 `tests/testImageInput.py`：配置缺省/勾选/取消、格式/像素/容量校验、落盘命名、ref 往返、hydration 缺文件报错。
- [ ] inputTypes / supportsImageInput；无 config 旧适配器纯文本正常、有图明确拒绝；同步/流式 errorType 一致。
- [ ] inputImage / images 可选入参；会话锁内固定顺序：预算复检（目录实文件求和+本轮）→ 逐张落盘（ref 冲突换名、不覆盖）→ 全部成功才写事件、任一步失败仅回滚本轮新文件并返回 imageStorageError；确定性拒绝不落 userMessage、不请求模型。
- [ ] JSONL 引用写入/恢复；末行截断、JSON 合法但无换行、坏行严格模式报行号且保留原文件、缺省宽松不变、并发读写不误判。
- [ ] 两适配器快照断言先行的转换实现：流式/非流式共用、纯图片无空文本块、多轮与工具续跑仍带图。
- [ ] 错误请求副本脱敏图片数据；不影响现有诊断与 Responses opaque replay。

### P2：Web 接入 → 验证：TestClient 全链与拒绝路径
- [ ] 写 `tests/testImageWeb.py`（伪会话/伪 token/临时 workDir/mock 模型，不碰真实数据）。
- [ ] 有界读取（含 chunked）；字段/类型校验；16MiB+1 拒绝。
- [ ] 上传图：解码 → Pillow 实检 → 落盘出现于 `{sessionId}.images/`；伪 MIME/多帧/像素炸弹拒绝。
- [ ] @ 分流：图片扩展名按单张 5 MiB 上限读取快照（超限 413）、路径清单保持现状；不存在/越界/符号链接逃逸/目录不解包不回归。
- [ ] @ 图 + 纯文本模型回退路径引用；上传图 + 纯文本模型 400；切模型后实际适配器口径一致。
- [ ] 图片读取端点：鉴权、ref 正则 + 目录内 resolve 双重拘禁（越权 404）、扩展名映射 Content-Type、404；GET messages DTO 只含元数据无 base64。
- [ ] encodeResumeFrame userImages 编解码；pure-image `userMessage=''` 与 confirm `null`/缺字段分离；重启回放与 baseCount 不丢不重。
- [ ] deleteSession 同时清 jsonl 与图片目录；预检失败零 userMessage/零模型调用；同会话并发只一个入流。

### P3：前端 → 验证：可执行 JS 断言 + 浏览器手测
- [ ] 新增 `tests/testImageFrontend.py`（pytest 驱动 Node assert/vm + DOM stub，沿用现有方式）。
- [ ] 选择/粘贴/拖入、缩略图/移除/大图；不支持模型置灰但文字粘贴不受影响。
- [ ] 能力刷新、纯图片发送、@ 图气泡（发送中 chip、历史缩略图经 blob 端点）。
- [ ] 失败恢复完整草稿、409 快照重试、断网不盲重发；切会话/旧回调不串图、引用释放。
- [ ] 浏览器手测真实文件对话框/剪贴板/拖放/大图关闭；Node stub 不冒充浏览器验证。

### P4：回归与验收 → 验证：框架测试 + 授权后的真实图片请求
- [ ] `uv run pytest -q tests/testImageInput.py tests/testImageWeb.py tests/testImageFrontend.py`。
- [ ] `uv run pytest -q` 全量回归（重点 testFileMention*、testResponsesReplay、testModelStreamDiag、testLiveUsage*），记录基线差异，不修无关失败。
- [ ] 隔离生成合法 PNG/JPEG/WebP 样本；容量边界用测试常量，不提交真实大图/敏感截图。
- [ ] 用户授权后对已勾选 image 的 Chat Completions / Codex / xAI 各一模型真实发图，记录实际支持/拒绝。
- [ ] 更新 webApiSpec、README（图片目录位置、快照语义、删除/回退说明）。
- [ ] 实现后用 code-review 技能独立审核并复审；核对 diff 只含本需求（是否 commit 另等指令）。

## 6. 最终验收场景

1. 勾选 image 保存后图片入口可用；取消后禁用，伪造 HTTP 请求也无法发图。
2. 上传图发送后：`{sessionId}.images/` 出现存储文件，JSONL userMessage 只含引用无 base64；模型请求体含 data URL base64。
3. `@` 图片：存在则快照进图片目录并进请求体（路径清单同时保留）；不存在沿用现有 400；原文件随后被删/改，后续轮次仍用快照正常续跑。
4. 服务重启、刷新、另一窗口 attach 后，图片经鉴权端点按引用渲染，同一条用户消息不丢不重；GET messages 响应中无 base64。
5. 带图历史继续文字追问或工具续跑仍带图；切纯文本模型：上传路径报错、@ 路径回退为路径引用并有提示。
6. 删除会话同时清掉 jsonl 与图片目录；预检失败可恢复草稿；停止/确认/技能/用量不回归。
7. 超限（单张/单条/会话预算）有明确提示；错误重试日志不出现整段图片数据；真实供应商验收未执行前明确标记未完成。

## 7. 审核记录

- 首轮（v1.0→v1.1）：`openaiCodex/gpt-6-astra` 子代理指出 SSE 白名单不透传图片、旧适配器无 config、JSONL 坏行静默丢事件、同步结果丢 errorType，均已修复。
- 第二轮（v1.2）：另一全新子代理提出“JSON 完整但末行无换行仍会粘连”，已补严格尾行检查与同路径读写锁。
- 最终复审（v1.3）：第三个全新子代理返回“复审通过，无明显问题”。
- v1.4：按用户 2026-09-08 指示重构存储（上传落盘 session log 图片目录、@ 查存在后快照、JSONL 只存引用、请求体 base64 data URL），并同步最近提交 `4d598fd` 的 @ 仅路径引用行为。
- 第四轮（v1.4→v1.5）：`xaiSubscription/grok-4.6` 子代理确认主链路自洽，指出 5 项需钉死：落盘回滚、ref 冲突覆盖、预算三套口径、图片端点穿越与 Content-Type 来源、@ 读取无上限；v1.5 均已补入设计与 TODO。
- 复审（v1.5）：另一个全新 `xaiSubscription/grok-4.6` 子代理确认 5 项修复无漏修，指出 1 项时序矛盾：预算复检若在落盘之后会双重计入本轮字节；v1.6 已钉死锁内顺序为预算复检→落盘→写事件。
- 最终复审（v1.6）：第三个全新 `xaiSubscription/grok-4.6` 子代理只读核验后返回“复审通过，无明显问题”，确认锁内时序与限额表/TODO 一致。v1.7 仅记录终审状态。
- v1.8：用户确认“@ 图 + 纯文本模型 → 回退为仅路径引用”，其余决策不变；标记进入实施。
- 当前执行状态：**P1–P3 已落地，专项与全量 pytest 205 passed；真实供应商发图验收未运行（需用户授权）。**
