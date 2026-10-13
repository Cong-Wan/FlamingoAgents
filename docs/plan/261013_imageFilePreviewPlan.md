# 前端工作区图片文件查看功能方案与实施计划

- Author: wilbur
- Version: 1.2
- Date: 2026-10-13
- Status: 方案 v1.2 已实施；实现复审后的测试缺口已补齐，最终只读复审结论为无明显问题
- Scope: 在现有 Web 右侧文件浏览器中增加 PNG/JPEG/WebP 工作区图片预览；不新增上传、编辑、下载、缩放或独立图片管理页
- Related code: `webApp/frontend/js/fileExplorer.js`、`webApp/backend/fileBrowser.py`、`webApp/backend/server.py`

## 0. 审核记录

- 2026-10-13 第一轮：使用全新 `subGPT/gpt-6-astra` 子代理审核。运行结束时未返回审核文本，因此不能把该轮记为“无明显问题”。
- v1.1 在等待复审前主动收紧：后端要求文件名后缀与实际图片格式一致；前端失败不改变当前弹层；测试改为可直接导入的后端测试加可观测的前端分流与 URL 回收测试，避免为了 DOM stub 重构生产代码。
- 2026-10-13 有效审核：全新 `DS_Offical/deepseek-flash` 子代理结论为“需要修订”，提出 1 个 Medium 和 3 个 Low。v1.2 已全部吸收。
- 2026-10-13 实施后审核：首轮指出测试覆盖缺口；已补晚到响应、关闭释放 URL、失败保留预览、GIF/目录/缺失文件、HTTP 越界和超像素测试。最终全新只读复审结论为“无明显问题”。

## 1. 需求理解与成功标准

### 1.1 当前需求的可执行解释

用户说“在前端界面新增一个查看图片文件的功能”。结合当前代码，项目已经有：

- 聊天页右侧工作区文件树；
- 点击普通文件后打开 `filePreviewModal`；
- `GET /api/sessions/{sessionId}/fileContent` 文本预览接口；
- 聊天上传图片和会话图片接口，但该接口只服务聊天消息，不读取工作区文件。

本方案将需求解释为：

> 用户在右侧“工作区文件”树中点击图片文件时，在已有文件预览弹层内显示该图片；点击文本/Markdown 文件的既有行为不变。

### 1.2 明确不包含

- 不修改聊天图片上传、粘贴、拖拽和模型图片输入能力；
- 不把工作区图片自动发送给模型；
- 不支持图片编辑、下载按钮、旋转、缩放控件、轮播或独立图片库；
- 不新增独立页面或新的前端依赖；
- 不扩展当前图片白名单到 GIF、SVG、BMP 等格式；
- 不改变 `@` 文件引用的路径-only 语义。

### 1.3 成功标准

1. 右侧文件树中 `.png`、`.jpg`、`.jpeg`、`.webp`（大小写不敏感）显示为图片入口并可打开预览。
2. 图片通过带鉴权的工作区专用二进制接口返回，浏览器不把 token 放在 URL 中。
3. 后端始终使用既有 `resolveInside()` 路径拘禁，拒绝 `../`、符号链接逃逸、非文件和不存在路径。
4. 后端不信任扩展名或客户端声明：文件名后缀必须属于 PNG/JPEG/WebP 白名单，实际字节必须通过既有图片校验，且后缀与实际格式必须一致；单张仍受 5 MiB、像素与多帧限制。
5. 图片预览使用现有弹层、Esc 栈、遮罩点击和关闭按钮。当前图片 URL 只在关闭预览或新预览成功提交时释放；失败和过期响应只释放自己新建的 URL，不破坏当前预览。
6. 文本/Markdown 文件仍调用 `getFileContent` 并按现有代码高亮/Markdown 渲染；图片不调用文本接口。
7. 未认证、会话不存在、路径越界、无效图片和超限图片均得到明确错误，且不泄露工作区外路径内容。
8. 新增后端、API 和前端测试通过，相关旧测试与全量 `uv run pytest -q` 通过。

## 2. 已核实现状与证据

### 2.1 前端现有入口

- `webApp/frontend/index.html:289` 已有 `#filePreviewModal`，无需新增弹层。
- `webApp/frontend/js/fileExplorer.js` 的 `renderEntries()` 对所有文件调用 `openPreview(childPath)`；`openPreview()` 当前统一请求 `window.api.getFileContent()`。
- `webApp/frontend/js/fileExplorer.js` 已通过 `window.appStore.pushModalClose()` 接入全局 Esc 栈。
- `webApp/frontend/js/api.js` 已有带 Bearer 头的 `getSessionImage()`，但 URL 和后端含义针对会话上传图片的固定 ref，不能用于工作区相对路径。
- `webApp/frontend/styles.css` 已有 `.modal.file-preview-modal` 和 `.file-preview-body`，可在其内增加受限尺寸的图片容器。

### 2.2 后端现有边界

- `webApp/backend/fileBrowser.py:31` 的 `resolveInside()` 对工作区路径进行 `resolve()` + `is_relative_to()` 拘禁，可同时阻止 `../` 和符号链接越界。
- `fileBrowser.py` 当前图片相关白名单为 `.png/.jpg/.jpeg/.webp`；`flamingoAgents/core/imageInput.py` 的 `validateImageBytes()` 已实现真实格式、像素、多帧和单张 5 MiB 校验。
- `webApp/backend/server.py:491-504` 已有会话文件树和文本读取路由，异常统一走认证、会话存在性和中文 400 映射。
- `webApp/backend/server.py:507-517` 的 `/images/{ref}` 只允许会话图片固定 ref，不能直接套用工作区文件路径。

### 2.3 工作区状态

- 当前分支：`dev`。
- 方案创建前工作区无未提交改动。
- 本次只修改与该功能直接相关的后端、前端、契约文档和测试，不清理预先存在的无关死代码。

## 3. 方案决策

### 3.1 后端接口

新增已认证接口：

```text
GET /api/sessions/{sessionId}/fileImage?path=<相对 workDir 路径>
```

处理流程：

```text
校验 sessionId
→ requireSession(sessionId)
→ 校验 path 非空
→ fileBrowser.readImageFile(workDir, path)
   → resolveInside
   → 扩展名白名单预检
   → is_file/stat/5MiB 预检
   → 读取字节
   → validateImageBytes 实际格式/像素/帧校验
→ 校验后缀与实际格式一致
→ Response(content=raw, media_type=实际 MIME)
```

接口只接收相对工作目录路径，不接收绝对路径；前端使用 `encodeURIComponent(path)`。响应使用 `Cache-Control: no-store`，避免把受鉴权工作区内容作为长期 HTTP 缓存。

`imageInputError` 继承 `RuntimeError`，不能依赖全局 RuntimeError→400 处理器。新路由必须显式捕获 `imageInputError` 并调用既有 `mapImageError()`：`imageBudgetExceeded` 返回 413，其它图片校验错误返回 400。普通路径、文件存在性和 IO 错误继续由 RuntimeError 返回 400。

选择独立二进制接口而不是把 base64 放进现有 JSON `fileContent` 的原因：

- 图片不需要 JSON/base64 额外膨胀；
- 文本接口的 NUL/UTF-8 语义不被污染；
- `Response` 的 Content-Type 能让浏览器按实际图片类型渲染；
- 后端可以在发送前完成路径与内容校验。

### 3.2 图片格式与限制

一期只支持项目已经承诺的三类图片：

| 文件名后缀 | 实际格式 | MIME |
|---|---|---|
| `.png` | PNG | `image/png` |
| `.jpg` / `.jpeg` | JPEG | `image/jpeg` |
| `.webp` | WebP | `image/webp` |

后缀和实际格式都必须合法，且二者一致：

- `.png` 只能是 PNG，返回 `image/png`；
- `.jpg` 和 `.jpeg` 只能是 JPEG，返回 `image/jpeg`；
- `.webp` 只能是 WebP，返回 `image/webp`。

后缀与实际格式不一致、损坏字节、多帧/动图或非白名单格式都返回 400，不把字节交给浏览器。这比“只信实际格式并忽略后缀”更严格，也让前端的扩展名分流与最终响应一致。

复用现有限制：

- 单张原始字节最多 5 MiB；先 `stat` 再读取，避免明显超限文件先完整进入内存；
- 最多 20,000,000 像素；
- 拒绝多帧/动图；
- 不把图片内容写入会话日志或临时目录。

### 3.3 前端状态

扩展 `fileExplorer.js` 的预览状态：

```text
文本：{ path, content, isMarkdown, mode: rendered|source }
图片：{ path, imageUrl, isImage: true }
```

图片打开流程：

1. 从路径后缀判断是否走图片接口；
2. 调 `api.getFileImage(sessionId, path)` 获取 Blob；
3. `URL.createObjectURL(blob)` 生成临时 URL；
4. 在现有 `filePreviewBody` 内插入 `<img>`，使用 `textContent` 设置标题和 `alt`；
5. 隐藏 Markdown 的“源码/渲染”切换；
6. 当前预览 URL 只在关闭弹层或新预览成功提交并替换当前状态时释放。过期响应和失败响应只释放本次响应自己创建的 URL，不关闭、不清空、不替换当前预览。

图片展示只设置 `max-width/max-height` 与 `object-fit: contain`，保持原图比例；不增加缩放状态，超大图片由容器滚动和最大尺寸约束处理。

为避免快速连续点击导致旧请求覆盖新预览，前端使用一个递增的预览请求序号：旧响应只释放自己创建的 URL，不更新当前弹层。

### 3.4 错误处理

- REST 401：复用 `api.js` 既有清 token、进入登录门逻辑；
- REST 非 2xx：复用 JSON 错误解析，`fileExplorer` 通过既有 `toast` 提示；
- 图片二进制加载失败：若当前弹层没有预览，则保持关闭；若已有预览，则保留该预览并 toast 错误，不用失败响应清空 body；
- 后端路径/文件/内容错误：沿用 RuntimeError → 400 的项目错误链路；图片超限沿用 `imageInputError` 的 413 映射。

## 4. 精确改动清单

| 文件 | 动作 | 计划内容 | 文件头版本 |
|---|---|---|---|
| `webApp/backend/fileBrowser.py` | 修改 | 新增 `readImageFile()`；复用路径拘禁、扩展名白名单、大小预检和 `validateImageBytes`；保留文本/附件函数不变 | 1.5 → 1.6 |
| `webApp/backend/server.py` | 修改 | 新增已认证 `GET /sessions/{sessionId}/fileImage` 路由；映射图片错误；返回二进制 MIME 与 no-store | 1.24 → 1.25 |
| `webApp/frontend/js/api.js` | 修改 | 新增 `getFileImage(sessionId, path)`；带 Authorization、Accept、401 处理、错误解析并返回 Blob | 1.12 → 1.13 |
| `webApp/frontend/js/fileExplorer.js` | 修改 | 图片分流、图片 DOM、请求序号、object URL 回收、图片图标；文本逻辑保持原行为 | 1.2 → 1.3 |
| `webApp/frontend/styles.css` | 修改 | 新增图片预览容器与图片最大尺寸/等比显示样式 | 1.33 → 1.34 |
| `webApp/frontend/index.html` | 修改 | 同步 `styles.css?v=1.34`、`api.js?v=1.13`、`fileExplorer.js?v=1.3` 与文件头版本/描述 | 1.34 → 1.35 |
| `docs/webApiSpec.md` | 修改 | 增加 `GET .../fileImage` 契约和错误/格式/鉴权说明，版本升至 1.34 | 1.33 → 1.34 |
| `tests/testFileImagePreview.py` | 新增 | 后端纯函数与 HTTP 接口测试，含安全、格式、MIME、限制和鉴权边界 | 1.0 |
| `tests/testFilePreviewFrontend.py` | 新增 | Node + `assert` 测试前端图片/文本分流、API 调用选择和 object URL 回收 | 1.0 |
| `tests/testVersionFrontend.py`、`tests/testLatencyMetricsFrontend.py`、`tests/testLiveUsageFrontend.py`、`tests/testToolCardCollapse.py` | 修改 | 只同步硬编码的 `styles.css?v=` 与 `api.js?v=` 缓存版本断言和对应文件头描述 | 各自小版本 +0.1 |

不改动：`imageInput.js`、聊天消息图片接口、`fileMention.js` 的附件 payload、模型输入协议、数据库和会话日志格式。

## 5. 测试设计

### 5.1 后端纯函数测试

使用 `pytest`、`tmp_path` 和 Pillow 生成最小 PNG/JPEG/WebP（不访问真实工作区）：

- 合法 PNG/JPEG/WebP 返回原始字节和正确 MIME；
- 大小写后缀可用；
- 非图片后缀拒绝；
- 损坏字节、伪造后缀拒绝；
- 后缀与实际格式不一致时拒绝，例如 JPEG 字节命名为 `.png` 返回 400；
- 超过 5 MiB 时在读取前拒绝；
- 使用多帧/动画 WebP 且文件名为 `.webp`，证明请求通过后缀白名单后仍由帧数校验拒绝；GIF 另测为非白名单格式拒绝，不把它当作多帧分支证据；
- 超像素图按现有校验拒绝；
- `../outside.png` 与工作区内指向外部的符号链接拒绝；
- 目录、缺失文件、不可读路径走预期 RuntimeError。

### 5.2 HTTP 接口测试

使用既有 `fastapi.testclient.TestClient` 和 monkeypatch 认证/会话：

- 有效会话 + 有效图片：200、响应字节一致、`Content-Type` 正确、`Cache-Control: no-store`；
- 缺 path：400；会话不存在：404；token 缺失/错误：401；
- 路径越界/非图片/损坏图片：400，不泄露文件内容；
- 超限图片固定返回 413；
- 纯文本 `/fileContent` 旧行为继续通过现有测试；
- 不访问 `/images/{ref}` 会话上传图片接口，不改变其测试。

### 5.3 前端测试

前端测试使用项目现有的 Node `vm` + 最小 DOM stub 风格，但只覆盖能稳定观测的行为，不为了测试重构 `fileExplorer.js`：

- 点击图片文件时调用 `getFileImage`，不调用 `getFileContent`；
- 点击文本文件时调用 `getFileContent`，不调用 `getFileImage`；
- 成功图片预览的 body 中存在 `<img>`，标题和 `alt` 来自文本属性，不来自 `innerHTML`；
- Markdown 模式按钮对图片隐藏；
- 关闭图片预览调用 `URL.revokeObjectURL`；打开第二张图片会释放第一张 URL；
- 旧请求晚返回不覆盖新预览，并释放自己的 URL；
- 图片请求失败时，已打开的文本预览仍保留；
- 当前图片预览切换到另一张图片失败时，当前图片、其 URL 和预览内容仍保留；
- 401 仍触发既有未认证回调；
- 文件树图片行显示图片图标不影响路径拼接。

若实施中发现现有 IIFE 结构无法在不改生产结构的前提下覆盖某项 DOM 行为，保留已能覆盖的前端断言，并在方案中写明未覆盖项和原因；不得为了测试把生产代码拆成可测试框架。

### 5.4 验证命令

```bash
uv run pytest tests/testFileImagePreview.py tests/testFilePreviewFrontend.py tests/testFileMention.py tests/testImageWeb.py -q
uv run pytest -q
 git diff --check
```

Node 前端测试由 pytest 子进程调用 `node`，与现有前端测试风格一致；不引入 Jest/Vitest 等新框架。

## 6. 风险、取舍与回退

| 风险 | 控制 |
|---|---|
| 把图片路径直接拼进静态 URL 绕过鉴权 | 必须走已认证 API + Bearer header，禁止 `<img src="/api/...">` 直接带 URL token |
| `../` 或符号链接越界 | 所有读取统一经过 `resolveInside()`；不能在路由层自行 `Path.read_bytes()` |
| 伪 PNG 或后缀与内容不一致 | 后端调用 `validateImageBytes()`，并要求后缀与实际格式一致；不信任扩展名/MIME |
| 大文件导致内存峰值 | stat 先检查 5 MiB；既有 `maxImageBytes` 作为硬边界 |
| object URL 泄漏 | 当前 URL 只在关闭或新预览成功提交时释放；失败/过期响应只释放自身 URL |
| 图片请求异步乱序 | 请求序号守卫；过期响应不覆盖当前状态 |
| 修改普通文件预览造成回归 | 图片分支只在 `isImagePath()` 命中；文本路径保留原代码 |
| 图片功能与聊天上传格式口径分叉 | 复用同一 `validateImageBytes()` 与现有三格式白名单 |
| 契约编号/版本与当前文档不同步 | 只在 `docs/webApiSpec.md` 增加 `§3.17.1`，避免顺延已有端点编号；同步版本历史 |

回退方案：删除新增 `fileImage` 路由、`readImageFile`、前端图片分支和样式/测试；现有文本文件树和聊天图片输入无需回退或迁移。

## 7. 实施 TODO List

### Phase 0：方案与边界

- [x] 记录现有文件树、文本预览、会话图片接口和路径拘禁实现。
- [x] 明确只支持 PNG/JPEG/WebP，不把 GIF/SVG 纳入本轮。
- [x] 明确图片使用工作区相对路径专用二进制接口，不复用会话图片 ref 接口。
- [x] 创建全新的 subagent 审核本方案。
- [x] 根据审核修订方案，并由另一全新 subagent 复审至无明显问题。

### Phase 1：后端能力

- [x] 为 `fileBrowser.py` 增加 `readImageFile()`，只使用小驼峰命名和既有文件头格式。
- [x] 在读取前执行路径拘禁、普通文件、扩展名和 stat 大小检查。
- [x] 使用 `validateImageBytes()` 做实际格式、像素和多帧校验，返回 `(bytes, mimeType)`。
- [x] 在 `server.py` 增加 `fileImage` 认证路由和错误映射。
- [x] 写后端纯函数与 HTTP 测试。
- [x] 验证：后端专项测试全绿，越界与伪造图片不会返回字节。

### Phase 2：前端 API 与预览

- [x] 在 `api.js` 增加带 Bearer 的 `getFileImage()`，复用 401 和错误解析逻辑。
- [x] 在 `fileExplorer.js` 增加图片路径分流、Blob URL、图片 DOM 和请求序号守卫。
- [x] 在关闭/替换/过期响应路径回收 object URL。
- [x] 在 `styles.css` 增加等比、最大尺寸、滚动容器样式。
- [x] 仅对图片文件更新图标，保持目录展开和普通文件预览不变。
- [x] 写 Node 前端行为测试。
- [x] 验证：图片不调用文本接口，文本不调用图片接口，关闭后无 URL 泄漏。

### Phase 3：契约与质量

- [x] 更新 `docs/webApiSpec.md` 的版本、变更记录和新接口说明。
- [x] 检查所有新增代码文件头；现有修改文件同步小版本和 description。
- [x] 运行专项 pytest、相关旧测试和全量 pytest。
- [x] 运行 `git diff --check`，检查未产生无关修改。
- [x] 若测试发现接口边界与方案冲突，先修方案记录，再修代码。

### Phase 4：实施后独立复审

- [x] 创建全新的 subagent 审核实现与测试，重点检查路径安全、MIME/格式校验、object URL 生命周期和文本回归。
- [x] 修复审核问题。
- [x] 由另一全新 subagent 复审修订后的实现，直到无明显问题。
- [x] 回填本计划实际版本、测试结果和审核记录。

## 8. 交付时需要回填的记录

- 实际修改文件与文件头版本：`fileBrowser.py` 1.6、`server.py` 1.25、`api.js` 1.13、`fileExplorer.js` 1.3、`styles.css` 1.34、`index.html` 1.35、`webApiSpec.md` 1.34，以及相关缓存版本测试。
- 后端专项测试结果：`tests/testFileImagePreview.py` 通过。
- 前端 Node 测试结果：`tests/testFilePreviewFrontend.py` 通过。
- 全量 `uv run pytest -q` 结果：383 passed。
- `git diff --check` 结果：通过。
- 方案审核、实施后审核及修订摘要：方案有效审核后修订并通过；实现审核补齐测试后最终复审为无明显问题。
- 已知边界：仅 PNG/JPEG/WebP、单张 5 MiB、拒绝多帧/动图，图片仍只能查看不能编辑/发送。
