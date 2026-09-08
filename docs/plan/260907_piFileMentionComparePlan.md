<!--
Author: wilbur
Version: 1.3
Date: 2026-09-07
Description: 对比本机 pi 0.85.1 与 FlamingoAgents 的 @ 输入处理，严格区分交互补全、CLI @file 参数和模型 read 工具；只调研和隔离验证，不修改生产行为。v1.1 根据首轮审核明确真实 fd 搜索与补全的区分、临时 cwd/HOME/配置隔离、uv 离线不更新依赖及逐项证据矩阵。v1.2 同步计划复审、关键调用链调查、21 项 pi/29 项本项目对照验证及报告完成状态。v1.3 记录最终独立审核通过、源文件与探针指纹核验和交付状态。
-->

# pi 与当前 @ 机制对比计划

## 1. 目标、基线与边界

用户要求：查看 pi 如何处理压缩包/其它文件的 @ 引用，与当前机制比较。

- 已确认本机 `pi` 指向 `/Users/wilbur/.brew/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js`。
- 调研基线：安装包 `@earendil-works/pi-coding-agent 0.85.1`，仓库元数据 `https://github.com/earendil-works/pi`；不是旧文档的 0.84.4，也不声称远端最新版。
- 本轮不修改 FlamingoAgents / pi 生产代码，不安装或升级包，不访问用户 `.pi` 配置、凭据、会话，不执行真实模型请求（计划审核子代理除外）。
- 必须区分：**pi 交互编辑器里的 @**、**启动参数 `pi @file`**、**模型调用 read 工具**。不得看到 CLI 文件处理器就把其行为泛化为交互面板。
- 不擅自把上一轮建议“文本内联+其它文件引用”表述为 pi 的实际行为。用户尚未授权本项目功能修改。

## 2. 简要步骤与成功标准

1. 锁定版本和真实实现 → 验证：package.json、安装入口、必要的发布 JS/源码映射、README；每项结论记录路径和行号。
2. 对比三条输入链路 → 验证：补全插入的字符串、交互提交的消息、CLI 文件处理分流、模型 read 的格式和限制；明确压缩包是否解包。
3. 隔离测试关键行为 → 验证：Node 内置 `node:test` 导入已安装的功能模块；真实 @ 搜索会启动已安装 fd，仅搜索临时目录，不能误称纯函数；不启动 pi/模型。已有 FlamingoAgents pytest 复现测试作为对照。
4. 形成对照表和可借鉴建议 → 验证：结论落档、独立复核，区分源码证据、自动化验证与未测试边界。

## 3. 具体调查范围

### pi 交互路径

- `pi-tui/dist/autocomplete.js`：@ 候选搜索、目录、带空格路径、`applyCompletion`，是插入路径还是读取内容。
- `dist/modes/interactive/interactive-mode.js`：创建补全器、提交回调、到 `session.prompt` 的实参；粘贴图片是否另有路径。
- `dist/core/agent-session.js`：普通文本、技能/模板扩展、扩展钩子和最终 user content；默认核心行为与自定义扩展明确分开。
- `dist/core/tools/path-utils.js` 与 utils paths：read 对 @ 前缀/引号的处理，不将本地工具的路径权限直接照搬 Web workDir 拘禁。

### pi CLI 和 read 路径

- `dist/cli/args.js`、`dist/main.js` 的初始消息构造、`dist/cli/file-processor.js`。
- `dist/utils/mime.js`、image-process、`dist/core/tools/read.js`、truncate：图片识别与非图片分支、NUL/编码行为、截断是否仅限制输出。
- 压缩包没有专用分支时应直接说明；“没报二进制不支持”不等于“解析出了压缩包内容”。不扩展到未经查看的第三方插件或其他 UI 客户端。

## 4. 测试方式

- 使用 `mktemp` 创建临时目录，新增临时测试文件命名 `piMentionProbe.mjs`，带 Author/Version/Date/Description 文件头；新变量/函数用小驼峰，外部 API 名保留。
- 用既有 uv 环境（`uv run --offline --no-sync --project <项目路径> python`）与 Python 标准库生成真实 ZIP/TAR/TAR.GZ、UTF-8 文本、含 NUL bytes、少量图片样本；不更新依赖、不解包到真实项目。
- 测试进程 cwd、补全 basePath、HOME、PI_CODING_AGENT_DIR、XDG 配置定位与缓存/输出均设置为临时目录；样本无外部链接。Node 环境不继承凭据变量，只传必要的 PATH、临时 HOME/TMPDIR 和测试参数。
- 真实 @ 候选搜索显式指定已安装 `/Users/wilbur/.brew/bin/fd`，禁止 `ensureTool()` 或下载工具；若不可用，仅验证 `applyCompletion` 并标注未验证候选搜索，不能拿另一条 getFileSuggestions 接口代替。
- 测试补全返回 @ 路径文字（含空格引号、目录）、CLI 参数区分、`processFileArguments()` 对文本/压缩包/图片的结果；根据模块副作用审查决定是否调用真实 read 工具工厂，否则只报告静态结论。
- 不通过完整 CLI 发消息，不调用会话构造器/认证存储；如需要验证交互提交仅引用路径，采用静态完整链路核对，不模拟后声称浏览器/TUI 端到端验收。
- 测试代码不纳入项目常规测试集，避免引入依赖本机全局 pi 安装路径的脆弱测试；报告记录临时测试位置、命令、结果与版本。
- 本项目对照用 `uv run --offline --no-sync --project <项目路径> python -B -m pytest -q <项目路径>/tests/testFileMention.py -o cache_dir=<临时目录>/pytestCache --basetemp=<临时目录>/pytestTemp`，测试 cwd/HOME 和 uv cache 设为临时目录。已有依赖不足则标记未执行，禁止补装。
- 证据矩阵逐项关联“比较项 → FlamingoAgents 文件/函数及现有 pytest 用例 → pi 文件/函数 → 静态/实测状态”。FlamingoAgents 的前端发送链路复用上一轮源码结论，不把后端 pytest 通过当作前端端到端验收。CLI 等不同入口写明无直接对应，而不是强行一一等同。

## 5. 详细 TODO List

- [x] T1 检查工作区，定位本机 pi 安装入口和 0.85.1 版本。
- [x] T2 初步区分交互自动补全与 CLI @file，列出真实模块。
- [x] T3 全新子代理审核计划；v1.1 修订后由另一全新子代理复审通过，无明显问题。
- [x] T4 走查补全模块、交互提交/AgentSession prompt 关键调用链及必要导入副作用，确认无默认附件内联转换遗漏，不扩大为全包审核。
- [x] T5 核对 CLI 参数解析/文件处理与 read 工具、图片和路径规范化的差异。
- [x] T6 隔离 Node 测试 21 passed（含真实 fd 候选搜索）；离线/no-sync/临时缓存运行 testFileMention.py：29 passed。
- [x] T7 落档 `docs/codeReview/260907_piFileMentionComparison.md`，提供版本、证据、对照表、边界、建议。
- [x] T8 全新子代理复核最终结论及验证范围：通过，无明显问题；另核对实际 CLI bundle、21/29 测试日志和五项 SHA-256，未复跑或访问真实会话。
- [x] T9 核对本轮仅新增调研文档、未修改生产行为；最终答复说明 pi 的处理方式及推荐借鉴点。

## 6. 明确不做

- 不把 pi 的无条件 UTF-8 解码当作本项目二进制修复方案。
- 不自动解包用户压缩包、不发送本机文件到模型、不测试凭据或真实会话。
- 不为比较任务引入 Node 运行时依赖或重写本项目附件协议。
- 不把“能提及某路径”宣传成“模型一定能读取该格式”，也不保证模型必定选择正确工具。
