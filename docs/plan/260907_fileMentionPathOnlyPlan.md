<!--
Author: wilbur
Version: 1.2
Date: 2026-09-07
Description: 按用户确认将 Web @ 统一改成 pi 式路径引用；保留 chip 和 attachments 请求结构，后端仅校验位置并输出绝对路径清单，取消内容内联和目录递归，保留旧历史/预览并明确图片方案边界。本轮只交付方案，不实施生产代码。v1.1 根据首轮审核允许存在的纯空白文件名、将409跨会话定时重试竞态列为既有问题而非虚假验收承诺，并精确区分预览含NUL拒绝与无NUL替换解码。v1.2 用户明确去掉上限：删除 8 项选择/请求个数限制，不另设替代个数上限；listDir 单层 500 条截断仍是浏览分页，不是发送上限。
-->

# @ 仅路径引用修改方案

## 1. 已确认目标与本轮范围

用户确认：“我想要的就是 pi 这种，只是给路径；给一个修改方案”。

**目标行为：所有通过 @ 选择的文件和目录都只提供路径。** 普通文本、代码、ZIP/TAR/RAR/7z、PDF、图片、无扩展名文件等一视同仁：发送前不读取内容、不探测 MIME、不解包、不递归列目录、不自动转图片输入。Agent 后续是否以及如何读取，由已有工具和用户任务决定。

方案 v1.2 已实施（验收见专项/全量 pytest）。历史消息不迁移。

调研依据：[当前机制排查](../codeReview/260907_fileMentionSupportReview.md)、[本机 pi 0.85.1 对比](../codeReview/260907_piFileMentionComparison.md)。pi 交互 @ 与启动参数 `pi @file` 不同；本方案对齐前者的路径语义，不模仿后者无条件 UTF-8 解码。

## 2. 设计决策与权衡

| 决策 | 本方案 | 原因及边界 |
|---|---|---|
| 选择界面 | 保留 chip、目录 Enter 选中/Tab 下钻、去重、移除；**不再限制 chip 个数** | 用户要改的是读取语义，不重写编辑器为 pi TUI；去掉上限后 chip 区可能折行变长，不为此新增虚拟列表 |
| 请求结构 | 保留 `attachments: [{path,type?}]` 和 `getAttachments()` | 不改客户端/重试结构，不增加 references/mode 配置；字段是历史命名，此后只代表路径引用 |
| 发送前操作 | 只做结构、workDir 拘禁与目标文件/目录存在性校验 | 路径解析和 stat 属于元数据访问；不检查内容可读性，不要求先打开文件。保留原 Web 有效路径边界，不承诺完全照搬 pi 的任意路径输入。**不对 attachments 个数设产品上限** |
| 路径文本 | 使用 `resolveInside()` 返回的规范化真实绝对路径，逐项 JSON 字符串转义 | 现有 read/bash 均支持绝对路径；不带 @ 标记，不要求工具新增前缀/引号剥离。空格、中文、引号、换行等不会破坏清单边界 |
| 提示语 | 一行明确“仅提供位置，未读取内容”，后接路径列表 | 不能让模型误以为已看到文件正文；无摘要、文件内容、base64、缩略图或文件大小 |
| 新消息回放 | 路径清单作为普通文本渲染 | 不增加 XML 标记、新 parser 或历史元数据；历史显示不像当轮 chip，但这与 pi 的路径文本方向一致，是有意的最小实现取舍 |
| 旧消息回放 | 保留旧 `<attachment>` 解析和原内容，不迁移 | 不破坏已经落盘的内容/上下文；本次仅保证新 @ 不再注入内容 |
| 预览 | `GET fileContent` / `readTextFile()` 原样保留 | “能引用”与“能预览”是两种能力；压缩包可 @，文本预览仍可报不支持 |
| 图片 | @ 图片也仅路径；直接发图走独立图片入口 | 不按扩展名偷偷改变 @ 语义，不把 @ 图片算成已上传 images |
| 限额 | 移除 8 项个数上限、1 MiB 内容预算、100 文件目录展开限制 | 用户明确「去掉上限」。不另设 100/1000 等替代个数上限。listDir/listAbsDirs 单层 500 条是浏览截断，不是 @ 发送上限。既有 HTTP/JSON 请求体限制（若运行时已有）不是本功能新加的产品 cap，本轮不借机加 body size 校验 |

### 不选的替代方案

- 文本内联、其它文件路径兜底：用户已明确要 pi 式统一路径，不再采用前轮折中建议。
- 新增 `<fileReference>`/JSON消息字段并改历史 parser：本次只传路径，不需要新的持久化协议和渲染抽象。
- 直接让前端拼路径后删除后端校验：会绕开 Web 路径边界，且更改发送/重试链路，无必要。
- 无条件输出 `@"path"`：本项目内置 read 不会剥离 @，与 pi 不同；使用明确的真实路径字符串即可，无需改工具。

### 绝对路径的明确含义

- chip 仍显示用户选中的相对路径；模型消息与新历史显示真实绝对路径。
- workDir 内符号链接允许，但消息使用其 resolve 后的真实目标路径；指向 workDir 外的链接仍拒绝。这里不是保留符号链接别名的承诺。
- 带 `..` 或绝对路径的 API 输入仍先走 `resolveInside()`：最终目标在 workDir 内可以引用，越界拒绝；不靠字符串前缀判断。
- 不把整个工作区快照发给模型。旧历史已有的文本、系统上下文以及 Agent 后续主动工具读取不受此方案限制。

## 3. 最终消息契约

请求结构不变：

```json
{
  "sessionId": "example-session",
  "message": "查看这些文件",
  "attachments": [
    {"path": "data/sample.zip", "type": "file"},
    {"path": "src", "type": "dir"}
  ]
}
```

假设 workDir 为 `/work/project`，后端得到并同时用于模型输入、用户 JSONL 内容及 streamMeta.userMessage 的最终文本：

```text
查看这些文件

引用路径（仅提供位置，未读取内容）：
- "/work/project/data/sample.zip"
- "/work/project/src"
```

规则固定如下：

1. 原始 message 的 trim 沿用 server 现状；非空原文与引用段之间两个换行；没有原文时只输出引用段。
2. 路径数组保持请求顺序；前端既有去重维持，后端不新增跨目录/符号链接去重规则。
3. 每个真实路径使用 `json.dumps(str(target), ensure_ascii=False)`，一条一行，双引号/反斜线/控制字符由 JSON 编码保护。说明它是路径字符串表示，调用工具时使用字符串的实际值，而不是把列表符号和显示引号一起传入。
4. 不生成新的 `<attachment>` 块，不依赖正文是否含 `</attachment>`；用户自己原文中的文本保持不变。
5. 没有 attachments 时直接返回原文，不追加空标题或空引用段；纯引用发送仍允许。
6. 该结构依旧是普通用户消息文本，不是机器鉴权令牌或受信任指令。文件名/路径视作数据，不拼 shell 命令、不执行路径内容；前端继续使用 textContent。

## 4. 后端改动

### 4.1 `webApp/backend/fileBrowser.py`

保留现有 `buildAttachmentMessage(text, workDir, attachments)` 函数名和调用签名，更新说明为“历史 attachments 字段的路径引用拼接”；避免为改语义大范围重命名接口。

将实现缩减为以下步骤：

1. **删除** `maxAttachments` 及「附件个数超限」拒绝。空数组按 §3 不追加引用段；非空则逐项校验。不把「很大的合法数组」当成错误。
2. 逐项检查字典及字符串 path；只拒绝空串 `''`、非字符串和 path 中的 NUL（非法路径字符，与文件内容是否有 NUL 无关）。**不调用 strip 判空或裁剪路径**：单个空格/制表符也可能是存在的合法文件名，交由 resolveInside 和目标存在/类型检查处理。原始 message 的 trim 不受影响。
3. 在统一错误映射范围内调用 `resolveInside(workDir, rawPath)`，然后确认实际目标是普通文件或目录；缺失/不可 stat/其它节点类型以 RuntimeError 中文错误进入400。不给设备/FIFO创建新的引用能力；不打开这些节点。
4. type 是现有 UI 展示提示，路径处理不按它分流：旧请求缺省 type、显式 file/dir 都按实际目标校验并仅拼路径，不会因为 type='dir' 递归。未知 type 不新增解析分支，本次也不额外建设类型协议。
5. 收集转义后的真实绝对路径，按 §3 构造文本；不 stat.st_size，不检查文件编码、NUL内容、结束标记，不进行 MIME 嗅探。
6. `OSError` 转为 RuntimeError 400；`resolveInside` 既有越界 RuntimeError 保留；对于解析时符号链接环等异常，确认仍走明确400、不误开 Agent。错误消息只描述路径/元数据问题，不再声称“附件二进制不支持”。

删除**因本次切换而成为孤立内容**的：
- `expandDirAttachment()`；
- `maxAttachments`、`maxTotalBytes`、`maxDirExpandFiles`、`attachmentCloseTag`。

这些符号目前只由本附件实现及 `tests/testFileMention.py` 使用；实施时再次全仓检索再删。`os` 仍用于 listDir/listAbsDirs，不能误删。新增标准库 `json` 即可；不引入解析包。

保持不动：`resolveInside()` 的安全语义、`listDir()`、`listAbsDirs()`、`readTextFile()` 的预览行为；不借机修预览乱码/内存策略或已有前端缓存问题。

### 4.2 `webApp/backend/server.py`

- 保留已有调用位置：路径引用构造仍在 `getAgent()`/启动流之前；任一非法路径整请求400，不部分发送。
- 保留 HTTP `attachments` 字段、message 非空规则、纯引用标题回退、409闸和重试约定、JSONL与SSE最终文本一致性；不修改 sessionStore/agentManager/SSE 编码结构。
- 功能逻辑原则上无需修改；若现有“拼接附件全文”等注释失真，只精确更新注释和文件头版本。
- 不顺便重写现有 `body.get('attachments') or []` 的边缘入参语义，专项测试必须按本轮契约划分，不扩大成整个请求验证改造。

## 5. 前端与历史

### 5.1 `fileMention.js`

- 不改选中、目录下钻、Enter/Tab/IME、滚动、去重、会话切换清空、技能chip共存、发送快照结构。
- chip tooltip 在路径之外注明“仅路径引用，不预读内容”。**删除** `chips.length >= 8` 拦截和「附件最多 8 个」toast，不改成另一条数量提示。
- `getAttachments()` 仍返回 `{path,type}`。@ 选中不请求 fileContent，不做图片分流。
- 既有 `attachable:false` 历史分支和“超过512KB”死文案在当前后端从不会触发，是预先存在的内容；不因本次任务无关地清理它。API文档需修正真实 attachable 语义，不能继续描述为大小判定。

### 5.2 `chatView.js`

- 当轮发送气泡继续显示文件/目录 chip，仅 tooltip 标明是路径引用。
- **不改变 `ATTACHMENT_RE`、`buildAttachmentBlock()` 和 `userBubbleText()`**：旧 `<attachment>` 内容仍按既有方式回放，含中文/特殊路径的旧 parser 限制也不在本次借机重构。
- 新引用清单作为普通文本由现有 `appendTextSegment()` 渲染；中文、空格、引号不依赖旧附件正则。新内容没有可展开正文，也不再伪造“附件内容已读取”样式。
- `streamResume.userMessage`/GET messages/刷新后显示同一份路径清单。UI初发是chip、回放是文本是已说明的取舍，不新增专用解析器强求两者同形。
- 409重试继续复用已捕获 `{text,attachments}`；不新增前端拼接，也不往快照原文再次追加路径，避免同会话重试产生重复引用。
- **已有跨会话竞态**：当前409定时重试只捕获text/attachments，约600ms后 send 重新读取当前sessionId，期间切会话可能误发到新会话。当前连接回包守卫不能覆盖此定时器。本方案不修改发送状态机，所以不能承诺该场景通过；将它记录为实施基线问题。图片方案的发送快照/会话守卫若先落地则保留其修复，否则本轮不隐含承担这一独立修复。
- 普通400后的草稿恢复是已有问题，本次不一起修；若命中其它已在做的图片/草稿恢复改动，合并时保留对方行为，仅接入路径语义。

## 6. 与图片方案和并行改动的协调

工作区已有图片方案 `260907_imageInputPlan.md`，其中“@图片读取并加入images”“目录继续展开文本”“文本附件保留1MiB”等条款与本次最新要求直接冲突。

**以本次明确的 @ 统一路径需求为准：**
- 不把 attachments 分流成 images；@ .png/.jpg/.webp/.gif/.heic 等均只是路径，不受模型 image 能力开关限制。
- 不从 @ 目录递归收集文本或图片。
- 图片上传/粘贴/拖入等独立图片入口仍由图片方案负责；本方案不取消或实现那部分功能。
- 图片方案关于浏览器 images 的容量/视觉能力校验可以保留；涉及“@图片作为图片内容”、1MiB 文本附件，以及「新旧来源合计附件数最多 8」必须重新对齐：@ 路径引用无个数上限。独立上传/粘贴图片的张数上限（如 4 张）不属于本次 @ 上限，不在本方案取消。
- 本轮不覆盖另一条并行任务的计划文件；在本方案记录冲突点，实施前协调同步相关条款及测试后再改交叉文件。若发现图片分流代码已经先落地，先更新本方案改动清单并复审，不偷偷保留扩展名例外。

本次开始时已有未提交的用量流、会话恢复/索引、README/API文档等修改。实施前重读当前文件，只作本方案对应行的精准修改，不回退、格式化或删除其它任务内容。

## 7. 测试计划：先红后绿

使用既有 uv 环境和 pytest；前端参照已有 pytest→Node 内置断言/vm 模式，不新增 npm 依赖。新增测试代码文件小驼峰、带 Author/Version/Date/Description 文件头。**本轮只设计测试，不执行或宣称这些目标已通过。**

### 7.1 改造 `tests/testFileMention.py`

上一轮29项是“当前限制刻画”，不能保留相反断言却声称新功能通过。

| 目标场景 | 新断言 |
|---|---|
| 普通文本/源码/中文文件名/无扩展名/未知扩展名 | 消息仅含原文+路径，不含独有正文canary；顺序与引用条数正确 |
| 真实 ZIP/TAR/TAR.GZ、含NUL二进制/非UTF8、图片/PDF等扩展名 | 文件格式不参与构造，均可只引用；不声称测试了这些格式的解析器 |
| 空文件、空目录、纯二进制目录、>100文件目录 | 均仅引用根路径；不要求目录内有可读文本，不产生展开/跳过说明 |
| 目录含越界子链接/链接环 | 只引用合法目录本身，不枚举子项；“跳过外部链接”的旧展开测试改为“根本没有扫描” |
| 直接越界路径/链接，缺失路径，非法节点，非字典/空path/path含NUL | 明确400前置异常，不启动 Agent；workDir内链接输出真实目标 |
| 单文件或合计超过原1MiB预算 | 仍可引用，消息大小只依赖路径文字；用稀疏文件或受控stat测试，不创建实占巨型样本 |
| 正文含 `</attachment>`，纯空白/前后空格/引号/反斜线/换行文件名 | 正文根本未读取；路径每行仍是可 JSON 解码的单个字符串，值等于真实路径，无伪造附件块；根目录下单个空格/制表符名称也可引用 |
| 纯引用、无引用、9 项及以上、type缺省与目录 | 固定文本格式、空引用不加标题、**9 项（及更多）允许且按请求顺序全部出现**、type不决定读内容；断言不再引用 `maxAttachments` |
| 预览回归 | 保留 readTextFile 的文本/NUL拒绝/替换解码现状用例，但移除“失真内容会进入 @ 消息”的旧断言 |

**零内容读取硬断言：**生成样本后，在目标调用范围把 `readTextFile`、目标路径的 `Path.open/read_bytes/read_text` 及 `os.scandir` 替换为失败哨兵（需按目标范围隔离，不拦 pytest/应用正常导入）。单文件和目录构造均应成功且哨兵未触发；允许 resolve/stat 的元数据检查。不能只断言结果不含正文，却实际偷偷读了一遍文件。

### 7.2 Web 路由集成

- 用临时 workDir、伪 token、伪 session 和 fake Agent/pump，不读写真实会话、不调用真实模型。
- 真实 `/files` 返回可选；真实 `/chat/stream` 对 ZIP/文本/目录引用进入 fake Agent，捕获 `runUserMessageStream()` 参数，断言是精确路径清单，不是文件正文。
- 截获 `startStream` 的 meta，确认 `userMessage` 与 fake Agent 入参完全一致。mock历史读取、标题/touch等会话操作；有限的fake SSE立即结束，避免吊起真实后台泵。
- 另用纯库离线 Agent+fakeAdapter、临时日志覆盖模型所见 user content 和实际 JSONL userMessage 内容一致；不 monkeypatch 掉被断言的落盘链路。
- 含NUL样本 `/fileContent` 仍400；无NUL的非UTF8样本仍按现状替换解码返回，不能概括为“所有二进制都拒绝”。@引用成功不改变预览。非法引用保持400且 `getAgent/startStream` 均未调用。

### 7.3 前端自动化与人工验收

新增 `tests/testFileMentionFrontend.py`，必要时复用当前轻量 DOM/vm 测试方式，不创建通用前端测试框架：
- 选中文本/压缩包/目录→chip/tooltip正确；payload仍是{path,type}，选中过程不调用fileContent；连续选 9 个互异路径全部成为 chip，无个数 toast；去重与 skill chip 保留。
- 当轮用户气泡保持chip；新路径文本的历史渲染含中文/空格/引号不触发HTML执行；旧attachment折叠与原内容保留。
- `/skill` 混合路径引用：已有技能注入展示仍正确，不因新路径段重复渲染技能全文。
- 同会话409首次重试复用原快照、不重复追加路径；streamResume及历史入口显示相同的新路径文字。跨会话600ms竞态按§5.2记录为基线限制，不虚称路径改动能修复它；若图片任务已有修复，既有守卫回归必须继续通过。
- 人工：@一个压缩包、一个含特殊字符名称的文本、一个目录发送→工具可获得正确路径；刷新/重连后路径可见；旧会话附件可展开；预览入口行为不变。模型是否选择解包不作为确定性单测成功条件。

验证命令（实施后）：

```sh
uv run --offline --no-sync pytest -q tests/testFileMention.py tests/testFileMentionFrontend.py
uv run --offline --no-sync pytest -q tests
```

先运行当前全量建立基线，记录其它并行改动已有失败；目标测试先在旧实现上失败，再实现至通过。不把上一轮149项或任何旧计数当作本轮结果。

## 8. 改动清单与版本

| 文件 | 改动 |
|---|---|
| `webApp/backend/fileBrowser.py` | 核心改动：buildAttachmentMessage改为路径构造；删除本次产生的展开函数/内容限额孤立符号；1.3→1.4（若实施时版本已变则从当时版本+0.1） |
| `webApp/backend/server.py` | 仅必要注释/文件头，调用链和协议不变；没有实际改动则不升版本 |
| `webApp/frontend/js/fileMention.js` | chip tooltip；删除 8 项拦截/toast；不改面板机制；1.3→1.4或从实施时基线+0.1 |
| `webApp/frontend/js/chatView.js` | chip tooltip、失真注释；保留旧历史parser/发送/重试状态机；从实施时基线+0.1 |
| `tests/testFileMention.py` | 将刻画测试改为路径目标验收+保留预览回归，版本1.0→1.1 |
| `tests/testFileMentionFrontend.py`（新） | pytest+Node定向前端回归，版本1.0 |
| `README.md` | @仅引用路径、读取/解包由Agent按需工具处理、引用不等于预览/上传 |
| `docs/webApiSpec.md` | §2.2旧attachment标记为历史兼容，新消息为路径文本；§3.16 attachable不按512KB；§3.17明确仅预览、移除已失真的512KB说明；§4.1更新引用契约/取消 ≤8、1MiB 与正文标记检查 |

不改：库 read/bash、模型适配器、core types、SSE字段、会话存储格式、CSS/HTML布局、模型配置与依赖。只有测试为证明闭环可能调用既有库接口，不能借机更改其语义。

## 9. 详细 TODO List

### 本轮方案交付
- [x] P1 确认用户已选“所有 @ 仅路径”，排除文本内联折中。
- [x] P2 检查当前代码、旧测试、工具路径接口及并行图片计划，标明冲突与边界。
- [x] P3 落档最小实现、消息契约、历史取舍、测试和改动清单。
- [x] P4 新建独立subagent审核方案；v1.0 首轮三项修订后，v1.1 由全新子代理复审通过。
- [x] P5 落档审核结论；最终回复提供方案摘要，明确尚未实施。
- [x] P6 用户要求去掉上限：v1.2 删除 8 项 cap；volcano/glm-5.3 独立审核通过。

### 后续实施
- [x] T1 确认实施时工作区基线；未改 imageInputPlan，@ 不分流图片。
- [x] T2 全量测试在实施后跑通（182 passed，含本专项）。
- [x] T3 重写 testFileMention.py 为路径目标验收，含零读取哨兵与非法路径。
- [x] T4 后端路径构造，删除 expandDirAttachment 与内容/个数预算常量。
- [x] T5 Web fakeAgent/fakePump：ZIP 引用进入 Agent，userMessage 与入参一致；非法路径不创建 Agent。未做真实 jsonl 落盘集成（fake 层已断言 meta.userMessage）。
- [x] T6 chip tooltip；9 chip 无上限与去重的 Node 测试；旧 ATTACHMENT_RE 未改。skill/409/重连未做浏览器 E2E。
- [x] T7 同步 README 与 webApiSpec；未改 imageInputPlan。
- [x] T8 文件头版本：fileBrowser 1.4、fileMention 1.4、chatView 1.22。
- [x] T9 专项 31 passed，全量 182 passed。人工压缩包发送未做。
- [x] T10 主代理验收：专项 31 / 全量 182 passed；diff 仅限 @ 路径引用相关文件；不自动提交 Git。

## 10. 风险与验收底线

- **非内容快照**：文件在引用之后变化/删除，Agent后续读取可能得到新内容或失败，这是路径引用的天然语义，不复制文件以规避。
- **不是文件系统沙箱扩展**：这里只保留 Web 提交时的路径校验；Agent工具的已有权限和检查-使用时间差不因此消失。目录里的外部链接不会在 @ 时遍历，后续工具行为是另一层，不能宣传为已全树安全检查。
- **历史不迁移**：旧会话仍可能包含已经内联的正文；切换后新引用不再注入，不能宣称旧上下文也已只剩路径。
- **新旧显示有区别**：当轮chip/历史文本取舍明确。若用户另要求刷新后也保持新引用chip，应作为明确增量需求再计划，不悄悄增加协议。
- **无个数上限**：一次引用很多路径会使 chip 折行、消息变长、并对每个路径做 resolve/stat；这是去掉产品 cap 后的预期成本。不为此偷偷加回个数限制，也不为此做虚拟列表。超大 JSON 请求若被运行时框架拒绝，属于既有 HTTP 限制，不是本功能的 8 项替代 cap。
- 验收底线：**选中任意常规文件/目录后，发送前只处理其路径/必要元数据，模型所见、日志和重连三处均没有由本次 @ 读取的文件内容；选 9 个互异路径不得因个数被拒。**
