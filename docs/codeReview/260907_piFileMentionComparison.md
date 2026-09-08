<!--
Author: wilbur
Version: 1.1
Date: 2026-09-07
Description: 本机 pi 0.85.1 与 FlamingoAgents Web @ 机制对比；区分交互路径引用、CLI 内容内联、read 工具及图片，记录 21 项 Node 隔离验证和 29 项本项目复现，不改生产行为。v1.1 补充全新子代理最终审核通过、实际 CLI bundle 与日志及指纹核验结果。
-->

# pi 与 FlamingoAgents 的 @ 文件机制对比

## 1. 结论与版本

**pi 交互界面的 @ 是路径补全/引用，连普通文本文件也不会因为被 @ 就自动读取；本项目 Web @ 则在发送前读取文件并内联全文。** 这是压缩包体验不同的关键，并不是 pi 有通用压缩包解析器。

另一个同名入口 **`pi @file` 启动参数** 会立即读取文件；它与交互输入框不是同一套处理。非图片（包括 ZIP/TAR/TAR.GZ）落入 UTF-8 文本分支，可能把 NUL、替换字符和原始归档字节混进消息，**没有自动解包**。

- 本机命令 `/Users/wilbur/.brew/bin/pi` 指向 `@earendil-works/pi-coding-agent/dist/bundle/cli.js`。
- 安装包与内置 pi-tui 均为 **0.85.1**。仓库元数据为 `https://github.com/earendil-works/pi`；不声称远端最新版或旧 pi 0.84.4 的行为相同。
- 以下 pi 路径统一相对 `piRoot=/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent`；依据安装包发布 JS 与本机模块测试，不启动完整 CLI。
- 不读取用户扩展、配置、凭据或真实会话；结论针对**默认核心链路**。输入扩展钩子可以改变文本/图片，第三方扩展行为不在结论范围内。

## 2. 主要对照表

| 比较项 | 当前 FlamingoAgents Web | pi 0.85.1 交互输入框 |
|---|---|---|
| @ 选择后 | 清除触发文字，保存 `{path,type}` chip | 将 `@路径` 插入编辑器，保留在消息文字里 |
| 普通文本文件 | 发送前读取全文，拼 `<attachment>` | 只引用路径；Agent 后续可选择 read |
| ZIP/TAR/TAR.GZ | 样本含 NUL，发送前 HTTP 400 | 可补全路径，不因文件内容是二进制而在该步骤拒绝 |
| 文件夹 | 后端递归拼文本，100 文件/字节限制，跳过二进制 | 插入 `@folder/`，不自动递归读取目录 |
| 图片 | @ 链路没有图片内容分支，含 NUL 会拒绝 | @ 本身仍是路径；后续 read 支持图片内容块 |
| 文件内容大小 | 聚合内容 >1 MiB 整请求失败，最多 8 个 chip | @ 只补全/发送路径，不在此处读取并累计文件字节；仍受一般消息/模型上下文限制 |
| 读取时机 | 在创建 Agent 前由 Web 后端强制读取 | 由 Agent 后续工具选择决定，不保证一定选择正确工具 |
| 文件搜索 | 当前目录列表+前缀过滤，500 条截断，保留 dotfiles/.git | fd 搜索文件及目录，支持隐藏条目、按默认忽略规则搜索、显式排除 .git；候选返回前20项，不是附件数上限 |
| 路径边界 | Web 入口强制限定 workDir，越界拒绝 | 本地路径解析接受相对/绝对/~/；不应直接照搬为 Web 放开拘禁 |

**注意：前一轮建议“文本继续内联，二进制路径兜底”是本项目兼容性折中，不是 pi 的真实交互机制。完全对齐 pi 应使文本文件也只引用路径。**

## 3. pi 的三条真实链路

### A. 交互 @：补全路径，不预读附件

1. `node_modules/@earendil-works/pi-tui/dist/autocomplete.js:191–209、585–652`：识别 @ 前缀，经 fd 按路径搜索，无压缩包解码/内容读取步骤。
2. 同文件 `265–304`：`applyCompletion()` 将 `item.value` 原样插入输入文本。普通文件后加空格，目录不加；带空格路径生成 `@"space name.zip"`。
3. `dist/modes/interactive/interactive-mode.js:487` 配置补全器；`2361–2550` 提交回调将普通文字交给 `onInputCallback`/待处理队列；`835–844` 主循环执行 `session.prompt(userInput)`。
4. `dist/core/agent-session.js:821–949`：可先走 input 扩展钩子、`/skill`/模板扩展；普通 `查看 @sample.zip` 没有文件内联扫描，`900` 行直接构造：
   ```javascript
   const userContent = [{ type: "text", text: expandedText }];
   ```
5. 后续由 Agent 选择工具。例如它**可以**调用 `bash` 中的 `unzip -l 'sample.zip'` 查看清单；这只是可能的工具选择，不是 @ 机制自动执行、不保证格式解析成功。本轮未执行解包或模型请求。

补充：`interactive-mode.js:2334–2344` 的粘贴图片把剪贴板 bytes 写成临时图片文件，再插入临时路径。该版本此分支也不是直接把图片附件塞进普通交互消息。这里只静态查看，没有访问剪贴板。

### B. CLI `pi @file`：启动时内联，与交互 @ 不同

1. `dist/cli/args.js:21–29、214–215`：独立参数以 `@` 开头，则去掉前缀加入 `fileArgs`；`inspect @note.txt` 这样的单个普通消息参数不被扫描展开。
2. `dist/main.js:170–180`：`prepareInitialMessage()` 调用 `processFileArguments()`，再合成初始消息。
3. `dist/cli/file-processor.js:12–69`：
   - 路径检查；size=0 则跳过。
   - 识别支持的图片：生成独立 `{type:'image', mimeType, data}`，文本部分是 `<file name="绝对路径">` 引用及可选处理说明。
   - **其它一律读取为 UTF-8 并拼接 `<file>`，没有 NUL 拒绝，也没有 archive 分支**：
     ```javascript
     const content = stripBom(await readFile(absolutePath, "utf-8"));
     text += `<file name="${absolutePath}">\n${content}\n</file>\n`;
     ```

实测 ZIP/TAR/TAR.GZ 的返回值与 `原始归档 bytes.toString('utf-8')` 完全相同，并保留 NUL。它没有提取文件列表/成员内容；TAR 本身可包含可见文字也不代表发生了解包。不能把“未报错”当成压缩包内容支持。

### C. 模型 read 工具：文本/图片分流，不是压缩包阅读器

- `dist/core/tools/read.js:37、64–92`：图片经 processImage 生成 image 块；非图片调用整文件 read，再 `buffer.toString('utf-8')`。
- `dist/core/tools/truncate.js:10–11` 与 read `93–147`：默认文本**输出**最多 2000 行或 50 KiB，支持 offset/limit；先全量读取再截断，不是有界磁盘读取，也不是 @ 文件大小限制。
- `dist/utils/mime.js:2–32`：读取前 4100 字节做图片签名判断；支持 JPEG/PNG/GIF/WebP/符合条件的 BMP，部分特殊图片明确不识别。识别图片不依赖扩展名。
- `dist/core/tools/path-utils.js:35–44`：允许工具参数路径带开头 `@`，会移除它。**并不移除编辑器用于显示的双引号**；模型调用工具时应提交实际路径字符串，而不是机械照搬 `@"..."`。
- read 产生 image 块不意味着所有模型能看图；仍依赖模型输入能力和后续请求适配。没有实测模型视觉/压缩包理解能力。

## 4. 可追溯证据矩阵

| 比较项 | FlamingoAgents 证据 | pi 证据 | 验证性质 |
|---|---|---|---|
| 选择后的表示 | `fileMention.js pickItem/addChip/getAttachments` | autocomplete `applyCompletion` | 本项目静态；pi 真实 fd+模块测试 8 项 |
| 提交时是否内联 | `server.py:571–574`、`fileBrowser.py buildAttachmentMessage` | interactive 主循环→AgentSession prompt | 本项目 TestClient；pi 提交全链路静态，不是 TUI E2E |
| 压缩包可选但被拒/不预读 | `testCurrentRealArchivesAreSelectableButRejected`、`testCurrentArchiveHttpErrorPrecedesAgentCreation` | 三种归档的真实候选及路径补全用例 | 双方隔离实测；不声称 pi 已发真实模型 |
| 目录 | `testCurrentDirectorySkipsBinaryButKeepsText`、100 文件/越界用例 | folder 路径补全、默认 prompt 无目录展开 | 本项目实测；pi 补全实测+提交静态 |
| CLI 非图片内联 | 本次 Web @ 无独立同名 CLI 入口对应，不推断整个项目 CLI | parseArgs 1 项+processFileArguments 5 项 | pi 模块实测 |
| 图片 | `readTextFile` 无图片分支，NUL 刻画用例 | MIME、CLI image 2 项、read image 1 项 | 本项目源码+bytes 用例；pi 小 PNG 实测，autoResizeImages=false |
| read 的归档/大小行为 | Web @ 使用 fileBrowser，不将库 read 工具混入本次比较 | read ZIP 1 项、2000 行截断 1 项 | pi 模块实测；50 KiB 常量/先读后截为静态证据 |
| 前缀和引号 | Web chip 原生 path 字段不保留 @ 标记 | resolveReadPath 1 项 | 本项目静态；pi 实测 |
| 无 fd 的候选 | 不适用 | getSuggestions 1 项 | pi 实测返回 null，未拿 getFileSuggestions 替代真实 @ 搜索 |

## 5. 建议：借鉴语义，不照抄文本读取缺陷

1. 若目标是和 pi 交互一致：**@ 只引用文件/目录路径，文本也不预读、不自动展开目录；Agent 再按需使用工具。** 本项目仍可保留 chip UI，UI 是否内联文字不是最关键，发送语义才是。
2. 若需保留“发送即给出全文”，应与“引用路径”明确区分；前一轮的文本内联+二进制引用是兼容折中，需用户选择，不能称为完全对齐 pi。
3. 保留 Web workDir 校验与明确的“内容未读取”状态。不因为 pi 本地工具允许绝对路径就放开 Web 的路径边界。
4. **不照抄 pi CLI/read 对非图片无条件 UTF-8 解码的行为**，也不把输出截断误当成内存保护。压缩包只发路径不等于 Agent 的 read 工具突然具备解压能力，后续仍需正确的工具处理。

本轮未改生产代码；用户尚未批准任何实施方向。

## 6. 隔离验证与审核记录

- 临时目录：`/tmp/piMentionCompare.ucqwZs`（macOS canonical path 为 `/private/tmp/piMentionCompare.ucqwZs`）。
- 测试文件：`piMentionProbe.mjs`，规范文件头，小驼峰命名；未加入常规项目测试集，避免依赖本机全局安装路径。系统清理临时目录后脚本和样本可能消失，本文保留关键证据与结果。
- Node 内置 `node:test`：**21 passed，0 failed/0 skipped**，实际调用本机 `/Users/wilbur/.brew/bin/fd` 搜索生成的临时样本，没有下载工具。
- 运行环境通过 `env -i` 清空继承变量，只传必要 PATH、临时 HOME/TMPDIR/XDG/pi 配置位置、PI_OFFLINE=1 及模块路径；cwd/basePath 是 fixtures，没有外部 symlink。未构造 AgentSession，未启动完整 pi CLI，未访问真实模型/凭据/会话。
- 图片测试显式 `autoResizeImages:false`，未覆盖默认缩放/native 转换和视觉供应商请求。
- 本项目对照：使用已有 uv 环境 `--offline --no-sync`、临时 HOME/cwd/cache、`python -B -m pytest`，**29 passed**。本轮未重跑全量149项，不把上一轮结果当本轮新验证。
- 命令主干：
  ```sh
  # 外层 env -i 与临时目录隔离如上，piRoot/fdPath 作为测试参数传入。
  node --test /tmp/piMentionCompare.ucqwZs/piMentionProbe.mjs
  uv run --offline --no-sync --project /Users/wilbur/project/FlamingoAgents python -B -m pytest -q /Users/wilbur/project/FlamingoAgents/tests/testFileMention.py -o cache_dir=/tmp/piMentionCompare.ucqwZs/pytestCache --basetemp=/tmp/piMentionCompare.ucqwZs/pytestTemp
  ```
- 计划首审提出 fd 依赖、临时运行隔离/no-sync、双侧证据矩阵三项；v1.1 修订后由全新子代理复审：**通过，无明显问题**。
- 最终独立结论复核：全新子代理审核**通过，无明显问题**；核对发布 JS、实际 CLI bundle、本项目调用链、探针、21/29 项日志及五项 SHA-256。确认实测/静态边界准确，不要求未授权功能修改；审核代理未复跑测试或启动 pi。

### 核验指纹（SHA-256）

| 文件 | SHA-256 |
|---|---|
| pi package.json | `f1738e4b42203e5f22bcb513f13fb2fb224f1e98d1f129ff042f87048665a94c` |
| pi-tui autocomplete.js | `c616bdb2993cc0e8caf8bf6f32927a10d9226755de3afe8efd8b51e97f3ef2bd` |
| pi file-processor.js | `4946f4e3e193713135a4924982d31c9403190befd421b7be9893d0e5c65055ae` |
| pi read.js | `c3d1fa1994c44541e044629b6b5a206bfc7b6e1f93488c0118364d51fa41d888` |
| piMentionProbe.mjs | `2ed4491aff5cb1cab0e903e82c2559bf0614797b920c314ec4eb3c8c561b1f4f` |

关联：[本轮计划](../plan/260907_piFileMentionComparePlan.md) · [本项目前轮排查报告](260907_fileMentionSupportReview.md)。
