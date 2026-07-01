<!--
Author: wilbur
Version: 1.4
Date: 2026-07-01
Description: Updates the Flamingo Agents flow documentation with verified code alignment and cleaner logic diagrams.
-->

# Flamingo Agents 完整流程图

> 范围：本文档基于 `flamingoAgents/`、`manualChecks.py`、`pyproject.toml`、`config/models.yaml` 的静态阅读整理；忽略 `__pycache__/` 与 `.venv/`。  
> 架构：采用“总览 → 分环节 → 函数索引”的总分结构。每个关键节点都标注代码文件与相关函数/类。  
> 入口：`pyproject.toml` 暴露两个命令：`flamingo-agents` 与 `flamingo-agents-server`。

---

## 0. 图例

| 图中写法 | 含义 |
| --- | --- |
| `文件路径::函数()` | 普通函数或方法调用点 |
| `文件路径::class.method()` | 类方法调用点 |
| `dataclass` | `flamingoAgents/core/types.py` 中的数据载体 |
| `status=completed` | `runResult.status` 最终完成 |
| `status=confirmationRequired` | HTTP 模式下删除命令等待用户确认 |
| `status=error` | 模型、参数、工具执行或循环上限错误 |

---

## 1. 总览架构

### 1.1 总览 Mermaid

```mermaid
flowchart TD
    caller(["用户 / 调用方"])
    mode{"运行路径"}

    subgraph cliEntry["CLI 路径：app/cli.py"]
        cliCommand["flamingo-agents<br/>pyproject.toml"]
        cliMain["main()<br/>解析 --debug / --session-id / --work-dir"]
        cliLoop["while True<br/>input('你>')"]
        cliBuiltIn{"内置命令?"}
        cliExit["/exit：退出"]
        cliHelp["/help：打印帮助"]
        cliEmpty["空输入：continue"]
        cliMessage["普通文本<br/>agent.runUserMessage(userInput, sessionId)"]
    end

    subgraph httpEntry["HTTP 路径：app/server.py"]
        httpCommand["flamingo-agents-server<br/>pyproject.toml"]
        httpMain["main()<br/>解析 --debug / --host / --port / --work-dir"]
        httpServe["ThreadingHTTPServer<br/>serve_forever()"]
        httpPost["agentHttpHandler.do_POST()"]
        httpRoute{"self.path"}
        httpChat["/chat<br/>handleChat()<br/>agent.runUserMessage(message, sessionId)"]
        httpConfirm["/confirm<br/>handleConfirm()<br/>agent.continueConfirmation(...)"]
        http404["其他路径<br/>respondJson(404)"]
    end

    subgraph buildLayer["共享启动装配：CLI / HTTP 都调用 buildAgent()"]
        build["buildAgent(debugEnabled, workDir)"]
        debug["debugConsole(debugEnabled)"]
        config["loadModelConfig()<br/>优先 config/models.yaml<br/>缺失时 fallback 环境变量"]
        adapter["openaiAdapter(config, printer)"]
        registry["createDefaultRegistry()<br/>read / write / edit / bash"]
        agentInstance["agent(..., modelAdapter, registry,<br/>workDir, logDir, confirmDeletion)"]
    end

    subgraph agentLayer["Agent 核心闭环：core/agent.py"]
        runUser["runUserMessage()<br/>清洗消息、定位 session、写入 user"]
        conversation["conversation<br/>messages + JSONL 日志"]
        modelLoop["continueModelLoop()<br/>最多 maxModelSteps=8"]
        exposeTools["registry.listModelTools()<br/>OpenAI tools schema"]
        modelCall["modelAdapter.complete(messages, tools)"]
        assistant["写入 assistantMessage"]
        hasTools{"有 toolCalls?"}
        completed["无工具调用<br/>runResult(status=completed)"]
        loopError["超过循环上限<br/>runResult(status=error)"]
    end

    subgraph confirmLayer["删除确认层"]
        guard["checkToolCall(call)<br/>仅 bash 删除命令需要确认"]
        needConfirm{"需要确认?"}
        cliAsk["CLI：askDeletionConfirmation()"]
        httpPending["HTTP：保存 pendingConfirm<br/>返回 confirmationRequired"]
        continueConfirm["continueConfirmation()<br/>取出 pendingConfirm 后执行或拒绝"]
        blocked["makeBlockedToolResult()<br/>用户拒绝"]
    end

    subgraph toolLayer["工具执行层"]
        router["agent.createRouter()<br/>router.executeTool(call)"]
        registryGet["registry.get(call.toolName)"]
        execute["toolSpec.execute(arguments, context)"]
        tools["tools/file.py 或 tools/bash.py<br/>executeRead / executeWrite / executeEdit / executeBash"]
        toolResult["conversation.addToolResult(result)"]
    end

    subgraph auxLayer["日志与辅助层"]
        jsonl["jsonlLog.logEvent()"]
        preview["makePreview() / redactText()"]
        debugOut["debugConsole.debug()"]
    end

    caller --> mode
    mode -- CLI 命令 --> cliCommand --> cliMain --> build
    mode -- HTTP 服务命令 --> httpCommand --> httpMain --> build

    build --> debug --> config --> adapter --> registry --> agentInstance
    agentInstance --> cliLoop
    agentInstance --> httpServe

    cliLoop --> cliBuiltIn
    cliBuiltIn -- /exit --> cliExit
    cliBuiltIn -- /help --> cliHelp --> cliLoop
    cliBuiltIn -- 空输入 --> cliEmpty --> cliLoop
    cliBuiltIn -- 普通消息 --> cliMessage --> runUser

    httpServe --> httpPost --> httpRoute
    httpRoute -- /chat --> httpChat --> runUser
    httpRoute -- /confirm --> httpConfirm --> continueConfirm
    httpRoute -- 其他 --> http404

    runUser --> conversation --> modelLoop --> exposeTools --> modelCall --> assistant --> hasTools
    hasTools -- 否 --> completed
    hasTools -- 是 --> guard --> needConfirm
    needConfirm -- 否 --> router
    needConfirm -- CLI 有回调 --> cliAsk
    cliAsk -- 允许 --> router
    cliAsk -- 拒绝 --> blocked
    needConfirm -- HTTP 无回调 --> httpPending
    continueConfirm -- 允许 --> router
    continueConfirm -- 拒绝 --> blocked

    router --> registryGet --> execute --> tools --> toolResult
    blocked --> toolResult
    toolResult --> modelLoop
    modelLoop -. 达到 maxModelSteps .-> loopError

    conversation -. 写日志 .-> jsonl --> preview
    toolResult -. 写日志 .-> jsonl
    debug -. debug 输出 .-> debugOut

    classDef entryStyle fill:#fff7ed,stroke:#fb923c,color:#7c2d12;
    classDef actionStyle fill:#eff6ff,stroke:#60a5fa,color:#1e3a8a;
    classDef decisionStyle fill:#fef9c3,stroke:#facc15,color:#713f12;
    classDef successStyle fill:#ecfdf5,stroke:#34d399,color:#064e3b;
    classDef errorStyle fill:#fef2f2,stroke:#f87171,color:#7f1d1d;

    class caller,cliCommand,httpCommand entryStyle;
    class cliMain,cliLoop,cliMessage,httpMain,httpServe,httpPost,httpChat,httpConfirm,build,debug,config,adapter,registry,agentInstance,runUser,conversation,modelLoop,exposeTools,modelCall,assistant,guard,cliAsk,httpPending,continueConfirm,blocked,router,registryGet,execute,tools,toolResult,jsonl,preview,debugOut actionStyle;
    class mode,cliBuiltIn,httpRoute,hasTools,needConfirm decisionStyle;
    class completed successStyle;
    class cliExit,http404,loopError errorStyle;
```

### 1.1.1 Agent 与工具注册的真实关系

| 阶段 | 代码文件 | 函数 / 方法 | 关键动作 |
| --- | --- | --- | --- |
| 创建注册表 | `flamingoAgents/tools/registry.py` | `createDefaultRegistry()` | 新建 `registry()`，连续注册 `read`、`write`、`edit`、`bash` 四个 `toolSpec` |
| 绑定执行函数 | `flamingoAgents/tools/registry.py` | `registry.register()` | 每个 `toolSpec.execute` 分别指向 `executeRead()`、`executeWrite()`、`executeEdit()`、`executeBash()` |
| 注入 Agent | `flamingoAgents/app/cli.py`、`flamingoAgents/app/server.py` | `buildAgent()` | 把 `registry` 作为构造参数传入 `core/agent.py::agent(...)` |
| Agent 持有注册表 | `flamingoAgents/core/agent.py` | `agent.__init__()` | `self.registry = registry`，后续模型工具 schema 与工具执行都靠它 |
| 暴露给模型 | `flamingoAgents/core/agent.py`、`flamingoAgents/tools/registry.py` | `agent.continueModelLoop()`、`registry.listModelTools()` | 每轮模型调用前，把注册表转换成 OpenAI `tools` schema 传给模型 |
| 执行模型工具调用 | `flamingoAgents/core/agent.py`、`flamingoAgents/tools/router.py` | `agent.createRouter()`、`router.executeTool()` | Router 拿同一份 `registry`，通过 `registry.get(call.toolName)` 找到具体工具执行函数 |

> 所以工具注册不是旁支，也不是 CLI/HTTP 各自的一套独立模块。它是共享模块：**CLI 和 HTTP 都调用同一个 `createDefaultRegistry()` 工厂函数，各自得到一个 registry 实例 → Agent 持有该 registry → 每轮模型调用把 registry 暴露成 tools schema → 模型返回 toolCall → router 从 Agent 持有的同一份 registry 找 execute 执行工具 → 工具结果回灌 Agent 会话。**

### 1.2 架构分层说明

| 层级 | 主要职责 | 代码文件 | 关键函数 / 类 |
| --- | --- | --- | --- |
| 入口层 | 接收 CLI 输入或 HTTP 请求，打印/返回 Agent 执行结果 | `flamingoAgents/app/cli.py`、`flamingoAgents/app/server.py` | `main()`、`makeHttpHandler()`、`agentHttpHandler.do_POST()`、`handleChat()`、`handleConfirm()` |
| 启动装配层 | 初始化 debug、模型配置、模型适配器、工具注册表、Agent 实例 | `cli.py`、`server.py`、`models/registry.py`、`models/openai.py`、`tools/registry.py` | `buildAgent()`、`loadModelConfig()`、`openaiAdapter.__init__()`、`createDefaultRegistry()` |
| 核心编排层 | 管理会话、调用模型、执行工具、处理删除确认、返回 `runResult` | `core/agent.py`、`core/conversation.py`、`core/types.py` | `agent.runUserMessage()`、`agent.continueModelLoop()`、`agent.continueConfirmation()`、`conversation.addMessage()`、`conversation.addToolResult()` |
| 模型层 | 把内部消息转成 OpenAI 兼容格式，请求 `/chat/completions`，解析 assistant/tool_calls | `models/openai.py` | `openaiAdapter.complete()`、`convertMessage()`、`parseAssistantPayload()` |
| 工具层 | 注册工具 schema、检查删除风险、路由并执行 `read/write/edit/bash` | `tools/registry.py`、`tools/router.py`、`tools/guard.py`、`tools/file.py`、`tools/bash.py` | `registry.listModelTools()`、`router.executeTool()`、`checkToolCall()`、`executeRead()`、`executeWrite()`、`executeEdit()`、`executeBash()` |
| 日志辅助层 | Debug 输出、JSONL 审计日志、内容预览、敏感信息脱敏 | `utils/debug.py`、`utils/jsonl.py` | `debugConsole.debug()`、`jsonlLog.logEvent()`、`makePreview()`、`redactText()` |
| 验证脚本 | 不依赖测试框架的手动检查 | `manualChecks.py` | `runFileToolCheck()`、`runBashCheck()`、`runGuardCheck()`、`runLoggerCheck()`、`runAdapterParseCheck()`、`runAgentCheck()`、`runHttpCheck()` |

---

## 2. 总流程：从用户输入到最终响应

### 2.1 端到端 Mermaid

```mermaid
flowchart TD
    start["开始：用户输入消息"]

    chooseEntry{"入口类型"}
    cliInput["CLI<br/>cli.py::main()<br/>input('你>')"]
    httpInput["HTTP POST /chat<br/>server.py::agentHttpHandler.handleChat()"]

    validateInput["校验与清洗消息<br/>agent.py::agent.runUserMessage()<br/>message.strip()"]
    emptyInput{"消息为空?"}
    emptyError["返回错误<br/>types.py::runResult<br/>status=error<br/>message='消息不能为空。'"]

    sessionPick["确定 sessionId<br/>agent.runUserMessage()<br/>sessionId or createSessionId()"]
    getConversation["获取/创建会话<br/>agent.getConversation()<br/>conversation.__init__()"]
    addUser["写入用户消息<br/>conversation.addMessage()<br/>chatMessage(role='user')"]
    modelLoop["进入模型-工具循环<br/>agent.continueModelLoop()"]

    modelCall["调用模型<br/>openaiAdapter.complete()<br/>registry.listModelTools()"]
    modelError{"模型调用异常?"}
    modelErrorResult["记录 modelError 并返回<br/>conversation.logger.logEvent()<br/>runResult(status=error)"]

    assistantMsg["写入 assistant 消息<br/>conversation.addMessage()"]
    hasTool{"assistantMessage.toolCalls 为空?"}
    completed["返回自然语言结果<br/>runResult(status=completed,<br/>message=assistantMessage.content)"]

    iterateTools["遍历 toolCalls<br/>agent.continueModelLoop()"]
    guard["删除风险检查<br/>guard.py::checkToolCall()"]
    needConfirm{"需要删除确认?"}

    confirmMode{"confirmDeletion 是否存在?"}
    cliConfirm["CLI 交互确认<br/>cli.py::askDeletionConfirmation()"]
    approvedCli{"用户允许?"}
    pendingHttp["HTTP 返回待确认<br/>pendingConfirm dataclass<br/>runResult(status=confirmationRequired)"]

    blocked["生成拒绝工具结果<br/>guard.py::makeBlockedToolResult()"]
    routeTool["路由执行工具<br/>router.py::router.executeTool()"]
    toolResult["写入工具结果<br/>conversation.addToolResult()"]
    nextStep["继续下一轮模型调用<br/>直到无 toolCalls 或超过 maxModelSteps"]
    maxError["超过最大步数<br/>runResult(status=error)"]

    start --> chooseEntry
    chooseEntry --> cliInput
    chooseEntry --> httpInput
    cliInput --> validateInput
    httpInput --> validateInput
    validateInput --> emptyInput
    emptyInput -- 是 --> emptyError
    emptyInput -- 否 --> sessionPick
    sessionPick --> getConversation
    getConversation --> addUser
    addUser --> modelLoop
    modelLoop --> modelCall
    modelCall --> modelError
    modelError -- 是 --> modelErrorResult
    modelError -- 否 --> assistantMsg
    assistantMsg --> hasTool
    hasTool -- 是 --> completed
    hasTool -- 否 --> iterateTools
    iterateTools --> guard
    guard --> needConfirm
    needConfirm -- 否 --> routeTool
    needConfirm -- 是 --> confirmMode
    confirmMode -- CLI 有 confirmDeletion --> cliConfirm
    cliConfirm --> approvedCli
    approvedCli -- 否 --> blocked
    approvedCli -- 是 --> routeTool
    confirmMode -- HTTP 无 confirmDeletion --> pendingHttp
    blocked --> toolResult
    routeTool --> toolResult
    toolResult --> nextStep
    nextStep --> modelCall
    modelLoop -. for 循环达到上限 .-> maxError
```

### 2.2 关键状态流转

| 阶段 | 输入 | 输出 | 代码文件 | 关键函数 / 类 |
| --- | --- | --- | --- | --- |
| 用户消息进入 | CLI 字符串或 HTTP JSON `message` | `cleanMessage` | `core/agent.py` | `agent.runUserMessage()` |
| 会话定位 | `sessionId` 可空 | 真实 `sessionId` 与 `conversation` | `core/agent.py`、`core/conversation.py` | `createSessionId()`、`getConversation()`、`conversation.__init__()` |
| 消息落会话 | `chatMessage(role='user')` | 内存消息列表 + JSONL 日志 | `core/conversation.py`、`utils/jsonl.py` | `conversation.addMessage()`、`jsonlLog.logEvent()` |
| 模型请求 | `conversation.messages` + 工具 schema | `chatMessage(role='assistant')` | `models/openai.py`、`tools/registry.py` | `openaiAdapter.complete()`、`registry.listModelTools()` |
| 工具调用判断 | `assistantMessage.toolCalls` | 直接完成或执行工具 | `core/agent.py` | `agent.continueModelLoop()` |
| 删除确认 | `toolCall(toolName='bash')` 且命中删除模式 | CLI 同步确认或 HTTP pending confirmation | `tools/guard.py`、`app/cli.py`、`core/agent.py` | `checkToolCall()`、`askDeletionConfirmation()`、`pendingConfirm` |
| 工具执行 | `toolCall.arguments` + `toolContext` | `toolResult` | `tools/router.py`、`tools/file.py`、`tools/bash.py` | `router.executeTool()`、`executeRead()`、`executeWrite()`、`executeEdit()`、`executeBash()` |
| 工具结果回灌 | `toolResult` | `chatMessage(role='tool')`，继续模型循环 | `core/conversation.py` | `conversation.addToolResult()` |
| 最终返回 | 无工具调用或错误 | `runResult` | `core/types.py` | `runResult` dataclass |

---

## 3. 分环节流程

## 3.1 CLI 入口流程

### Mermaid

```mermaid
flowchart TD
    cliStart["命令：flamingo-agents<br/>pyproject.toml<br/>flamingoAgents.app.cli:main"]
    parseArgs["解析参数<br/>cli.py::main()<br/>--debug / --session-id / --work-dir"]
    resolveWorkDir["解析工作目录<br/>Path(args.work_dir).resolve()"]
    build["构建 Agent<br/>cli.py::buildAgent(debugEnabled, workDir)"]
    debug["创建 debug 控制台<br/>utils/debug.py::debugConsole"]
    config["读取模型配置<br/>models/registry.py::loadModelConfig()<br/>优先 YAML，缺失时环境变量 fallback"]
    adapter["创建 OpenAI 兼容适配器<br/>models/openai.py::openaiAdapter(config, printer)"]
    registry["创建默认工具表<br/>tools/registry.py::createDefaultRegistry()"]
    agentCtor["实例化 Agent<br/>core/agent.py::agent(...,<br/>confirmDeletion=askDeletionConfirmation)"]
    loop["进入 while True 输入循环<br/>cli.py::main()"]
    commandCheck{"输入命令?"}
    exitNode["/exit：打印已退出并 return"]
    helpNode["/help：打印帮助并 continue"]
    skipEmpty["空输入：continue"]
    run["发送给 Agent<br/>agent.runUserMessage(userInput, sessionId)"]
    statusCheck{"result.status"}
    printCompleted["completed：打印 Agent 回复<br/>result.message"]
    printError["error：打印 Agent 错误<br/>result.message"]
    printUnexpected["其他状态：打印确认异常<br/>理论上 CLI 删除确认已同步处理"]

    cliStart --> parseArgs --> resolveWorkDir --> build
    build --> debug --> config --> adapter --> registry --> agentCtor --> loop
    loop --> commandCheck
    commandCheck -- 空 --> skipEmpty --> loop
    commandCheck -- /exit --> exitNode
    commandCheck -- /help --> helpNode --> loop
    commandCheck -- 普通文本 --> run --> statusCheck
    statusCheck -- completed --> printCompleted --> loop
    statusCheck -- error --> printError --> loop
    statusCheck -- confirmationRequired/其他 --> printUnexpected --> loop
```

### 环节明细

| 顺序 | 环节 | 文件 | 函数 / 类 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 命令入口 | `pyproject.toml` | `[project.scripts] flamingo-agents` | 指向 `flamingoAgents.app.cli:main` |
| 2 | 参数解析 | `flamingoAgents/app/cli.py` | `main()` | 支持 `--debug`、`--session-id`、`--work-dir` |
| 3 | Agent 装配 | `flamingoAgents/app/cli.py` | `buildAgent()` | 创建 debug、加载模型配置、创建 adapter、创建默认工具表 |
| 4 | 模型配置 | `flamingoAgents/models/registry.py` | `loadModelConfig()` | 若 `config/models.yaml` 存在则走 `loadModelConfigFromYaml()`；否则走 `loadModelConfigFromEnv()` 校验环境变量 |
| 5 | 模型适配器 | `flamingoAgents/models/openai.py` | `openaiAdapter.__init__()` | 保存 `modelConfig` 与 debug 控制台 |
| 6 | 工具注册 | `flamingoAgents/tools/registry.py` | `createDefaultRegistry()` | 注册 `read`、`write`、`edit`、`bash` |
| 7 | 删除确认回调 | `flamingoAgents/app/cli.py` | `askDeletionConfirmation()` | CLI 模式直接用 `input()` 问用户是否允许删除命令 |
| 8 | 输入循环 | `flamingoAgents/app/cli.py` | `main()` | 处理 `/exit`、`/help`、空输入和普通消息 |
| 9 | 运行 Agent | `flamingoAgents/core/agent.py` | `agent.runUserMessage()` | 进入核心模型-工具循环 |

---

## 3.2 HTTP 入口流程

### Mermaid

```mermaid
flowchart TD
    httpStart["命令：flamingo-agents-server<br/>pyproject.toml<br/>flamingoAgents.app.server:main"]
    parseArgs["解析参数<br/>server.py::main()<br/>--debug / --host / --port / --work-dir"]
    build["构建 Agent<br/>server.py::buildAgent(debugEnabled, workDir)"]
    agentCtor["实例化 Agent<br/>confirmDeletion=None<br/>删除确认走 /confirm"]
    makeHandler["创建 Handler 类<br/>server.py::makeHttpHandler(agent)"]
    serverStart["启动 ThreadingHTTPServer<br/>server.serve_forever()"]
    post["接收 POST<br/>agentHttpHandler.do_POST()"]
    route{"self.path"}

    chat["/chat<br/>agentHttpHandler.handleChat()"]
    readJsonChat["读取 JSON<br/>readJson()"]
    validateChat["校验 message/sessionId"]
    runUser["agent.runUserMessage(message, sessionId)"]
    chatDict["转换响应<br/>server.py::resultToDict()"]
    chatResp["返回 JSON<br/>respondJson(statusCode, payload)"]

    confirm["/confirm<br/>agentHttpHandler.handleConfirm()"]
    readJsonConfirm["读取 JSON<br/>readJson()"]
    validateConfirm["校验 sessionId / confirmationId / approved"]
    continueConfirm["agent.continueConfirmation(sessionId,<br/>confirmationId, approved)"]
    confirmDict["转换响应<br/>server.py::resultToDict()"]
    confirmResp["返回 JSON<br/>respondJson(statusCode, payload)"]

    notFound["其他路径<br/>respondJson(404, {'status':'error'})"]

    httpStart --> parseArgs --> build --> agentCtor --> makeHandler --> serverStart --> post --> route
    route -- /chat --> chat --> readJsonChat --> validateChat --> runUser --> chatDict --> chatResp
    route -- /confirm --> confirm --> readJsonConfirm --> validateConfirm --> continueConfirm --> confirmDict --> confirmResp
    route -- 其他 --> notFound
```

### 环节明细

| 顺序 | 环节 | 文件 | 函数 / 类 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | 命令入口 | `pyproject.toml` | `[project.scripts] flamingo-agents-server` | 指向 `flamingoAgents.app.server:main` |
| 2 | 参数解析 | `flamingoAgents/app/server.py` | `main()` | 支持 `--debug`、`--host`、`--port`、`--work-dir` |
| 3 | Agent 装配 | `flamingoAgents/app/server.py` | `buildAgent()` | 与 CLI 类似，但 `confirmDeletion=None` |
| 4 | Handler 创建 | `flamingoAgents/app/server.py` | `makeHttpHandler(agent)` | 闭包捕获 `agent`，内部定义 `agentHttpHandler` |
| 5 | 路由分发 | `flamingoAgents/app/server.py` | `agentHttpHandler.do_POST()` | 只支持 `/chat`、`/confirm` |
| 6 | `/chat` 请求 | `flamingoAgents/app/server.py` | `handleChat()`、`readJson()` | 校验 `message` 必须非空字符串，`sessionId` 若传必须为字符串 |
| 7 | `/confirm` 请求 | `flamingoAgents/app/server.py` | `handleConfirm()`、`readJson()` | 校验 `sessionId`、`confirmationId`、`approved` |
| 8 | 响应转换 | `flamingoAgents/app/server.py` | `resultToDict()` | `confirmationRequired` 时追加 `confirmationId`、`reason`、`commandPreview` |
| 9 | JSON 响应 | `flamingoAgents/app/server.py` | `respondJson()` | 设置 `Content-Type: application/json; charset=utf-8` |
| 10 | 访问日志 | `flamingoAgents/app/server.py` | `log_message()` | 只有 debug 模式才调用父类日志输出 |

---

## 3.3 Agent 核心模型-工具循环

### Mermaid

```mermaid
flowchart TD
    runUser["agent.runUserMessage(message, sessionId)"]
    strip["cleanMessage = message.strip()"]
    blank{"cleanMessage 为空?"}
    blankResult["runResult(status='error')"]
    session["realSessionId = sessionId or createSessionId()"]
    getConv["conversation = getConversation(realSessionId)"]
    addUser["conversation.addMessage(chatMessage(role='user'))"]
    enterLoop["continueModelLoop(realSessionId)"]

    loopStart["for stepIndex in range(maxModelSteps)"]
    createRouter["router = createRouter()<br/>toolContext(workDir, debugConsole)"]
    modelCall["modelAdapter.complete(conversation.messages,<br/>registry.listModelTools())"]
    modelException{"Exception?"}
    logModelErr["conversation.logger.logEvent(type='modelError')"]
    returnModelErr["runResult(status='error', message='模型调用失败')"]
    addAssistant["conversation.addMessage(assistantMessage)"]
    noTools{"not assistantMessage.toolCalls?"}
    returnDone["runResult(status='completed',<br/>message=assistantMessage.content)"]
    forTools["for call in assistantMessage.toolCalls"]
    checkGuard["guard = checkToolCall(call)"]
    needConfirm{"guard.requiresConfirmation?"}
    hasConfirmCb{"self.confirmDeletion is None?"}
    createPending["创建 confirmationId<br/>pendingConfirms[confirmationId] = pendingConfirm(...)"]
    returnPending["runResult(status='confirmationRequired',<br/>commandPreview=call.arguments['command'])"]
    syncAsk["approved = confirmDeletion(call, guard.reason)"]
    approved{"approved?"}
    blocked["makeBlockedToolResult(call, guard.reason)"]
    executeApproved["router.executeTool(call, approvedDeletion=True)"]
    executeNormal["router.executeTool(call)"]
    addTool["conversation.addToolResult(result)"]
    continueFor["继续处理下一 toolCall 或下一轮模型"]
    maxExceeded["runResult(status='error',<br/>message='模型循环超过最大步数')"]

    runUser --> strip --> blank
    blank -- 是 --> blankResult
    blank -- 否 --> session --> getConv --> addUser --> enterLoop
    enterLoop --> createRouter --> loopStart --> modelCall
    modelCall --> modelException
    modelException -- 是 --> logModelErr --> returnModelErr
    modelException -- 否 --> addAssistant --> noTools
    noTools -- 是 --> returnDone
    noTools -- 否 --> forTools --> checkGuard --> needConfirm
    needConfirm -- 否 --> executeNormal --> addTool --> continueFor --> loopStart
    needConfirm -- 是 --> hasConfirmCb
    hasConfirmCb -- 是，HTTP 模式 --> createPending --> returnPending
    hasConfirmCb -- 否，CLI 模式 --> syncAsk --> approved
    approved -- 否 --> blocked --> addTool --> continueFor
    approved -- 是 --> executeApproved --> addTool --> continueFor
    loopStart -. 达到 maxModelSteps .-> maxExceeded
```

### 关键实现点

| 主题 | 文件 | 函数 / 类 | 细节 |
| --- | --- | --- | --- |
| 系统提示词 | `flamingoAgents/core/agent.py` | `systemPrompt` | 约束 Agent 可聊天、可调用 `read/write/edit/bash`；联网只能通过 bash/curl；删除命令必须确认 |
| Agent 状态 | `flamingoAgents/core/agent.py` | `agent.__init__()` | 保存 `modelAdapter`、`registry`、`workDir`、`logDir`、`debugConsole`、`confirmDeletion`、`maxModelSteps`、`conversations`、`pendingConfirms` |
| 会话创建 | `flamingoAgents/core/agent.py` | `getConversation()` | 日志路径为 `logDir / f'{dateText}_{sessionId}.jsonl'` |
| Router 创建 | `flamingoAgents/core/agent.py` | `createRouter()` | 构造 `toolContext(workDir=self.workDir, debugConsole=self.debugConsole)` 后返回 `router` |
| 空消息 | `flamingoAgents/core/agent.py` | `runUserMessage()` | 直接返回 `runResult(status='error')` |
| 模型循环 | `flamingoAgents/core/agent.py` | `continueModelLoop()` | 最多执行 `maxModelSteps` 轮，默认 8 |
| 模型异常 | `flamingoAgents/core/agent.py` | `continueModelLoop()` | 捕获后写入 `modelError` 日志，返回 `status='error'` |
| 无工具调用 | `flamingoAgents/core/agent.py` | `continueModelLoop()` | 返回 `status='completed'` |
| 有工具调用 | `flamingoAgents/core/agent.py` | `continueModelLoop()` | 逐个检查删除风险、执行工具、把结果回灌 conversation，再继续模型循环 |
| HTTP 待确认 | `flamingoAgents/core/agent.py` | `pendingConfirms`、`pendingConfirm` | `confirmDeletion=None` 时，创建 `confirm_<12 hex>` 并返回 `confirmationRequired` |
| CLI 同步确认 | `flamingoAgents/app/cli.py` | `askDeletionConfirmation()` | 用户输入 `y` 或 `yes` 才允许执行 |

---

## 3.4 删除确认与 `/confirm` 续跑流程

### Mermaid

```mermaid
flowchart TD
    toolCall["工具调用<br/>types.py::toolCall"]
    isBash{"call.toolName == 'bash'?"}
    detect["detectDeletionCommand(command)<br/>仅 bash.command 进入删除检测"]
    patterns["deletePatterns<br/>rm / rmdir / unlink / find -delete<br/>os.remove / shutil.rmtree / pathlib.unlink"]
    risky{"命中删除模式?"}
    allow["guardDecision(allowed=True)"]
    need["guardDecision(requiresConfirmation=True,<br/>reason='删除命令需要用户确认')"]

    executeNormal["无需确认<br/>router.executeTool(call)"]
    mode{"confirmDeletion is None?"}

    cliAsk["CLI：askDeletionConfirmation(call, reason)"]
    cliApproved{"用户输入 y/yes?"}
    cliExecute["允许<br/>router.executeTool(call, approvedDeletion=True)"]
    cliReject["拒绝<br/>makeBlockedToolResult(call, reason)"]

    httpPending["HTTP：创建 confirmationId<br/>pendingConfirms[confirmationId] = pendingConfirm(...)"]
    httpReturn["返回 runResult(status=confirmationRequired)<br/>server.resultToDict() 输出 confirmationId/reason/commandPreview"]
    userConfirm["客户端 POST /confirm"]
    continueConfirm["agent.continueConfirmation(sessionId,<br/>confirmationId, approved)"]
    popPending["pendingConfirms.pop(confirmationId, None)"]
    match{"pending 存在且 sessionId 匹配?"}
    mismatch["runResult(status=error)<br/>确认请求不存在或 sessionId 不匹配"]
    approvedHttp{"approved?"}
    httpExecute["允许<br/>router.executeTool(pending.toolCall,<br/>approvedDeletion=True)"]
    httpReject["拒绝<br/>makeBlockedToolResult(pending.toolCall, pending.reason)"]

    addResult["conversation.addToolResult(result)"]
    resume["继续模型循环<br/>CLI：仍在当前 continueModelLoop 内<br/>HTTP：continueConfirmation() 再调用 continueModelLoop()"]

    toolCall --> isBash
    isBash -- 否 --> allow --> executeNormal
    isBash -- 是 --> detect --> patterns --> risky
    risky -- 否 --> allow
    risky -- 是 --> need --> mode

    mode -- 否，CLI 有回调 --> cliAsk --> cliApproved
    cliApproved -- 是 --> cliExecute --> addResult
    cliApproved -- 否 --> cliReject --> addResult

    mode -- 是，HTTP 无回调 --> httpPending --> httpReturn --> userConfirm --> continueConfirm --> popPending --> match
    match -- 否 --> mismatch
    match -- 是 --> approvedHttp
    approvedHttp -- 是 --> httpExecute --> addResult
    approvedHttp -- 否 --> httpReject --> addResult

    executeNormal --> addResult --> resume

    classDef actionStyle fill:#eff6ff,stroke:#60a5fa,color:#1e3a8a;
    classDef decisionStyle fill:#fef9c3,stroke:#facc15,color:#713f12;
    classDef errorStyle fill:#fef2f2,stroke:#f87171,color:#7f1d1d;
    classDef confirmStyle fill:#fff7ed,stroke:#fb923c,color:#7c2d12;

    class toolCall,detect,patterns,allow,executeNormal,cliAsk,cliExecute,cliReject,httpPending,httpReturn,userConfirm,continueConfirm,popPending,httpExecute,httpReject,addResult,resume actionStyle;
    class isBash,risky,mode,cliApproved,match,approvedHttp decisionStyle;
    class mismatch errorStyle;
    class need confirmStyle;
```

### 环节明细

| 环节 | 文件 | 函数 / 类 | 说明 |
| --- | --- | --- | --- |
| 删除模式定义 | `flamingoAgents/tools/guard.py` | `deletePatterns` | 用正则识别 shell 删除命令与 Python 删除 API |
| 删除检测 | `flamingoAgents/tools/guard.py` | `detectDeletionCommand()` | 空命令返回 `False`，否则任一模式命中即 `True` |
| 工具调用检查 | `flamingoAgents/tools/guard.py` | `checkToolCall()` | 非 `bash` 直接允许；`bash` 删除命令需要确认 |
| 拒绝结果 | `flamingoAgents/tools/guard.py` | `makeBlockedToolResult()` | 生成 `isError=True` 的 `toolResult`，内容说明命令被拒绝 |
| CLI 确认 | `flamingoAgents/app/cli.py` | `askDeletionConfirmation()` | 展示 command 与 reason，等待用户输入 |
| HTTP 待确认 | `flamingoAgents/core/agent.py` | `agent.continueModelLoop()`、`pendingConfirm` | 返回 `runResult(status='confirmationRequired')` |
| HTTP 续跑 | `flamingoAgents/core/agent.py` | `agent.continueConfirmation()` | 根据 `approved` 决定执行或拒绝，再继续模型循环 |
| Router 二次保护 | `flamingoAgents/tools/router.py` | `router.executeTool()`、`confirmationNeeded` | 若直接调用 router 且未传 `approvedDeletion=True`，仍会抛 `confirmationNeeded` |

---

## 3.5 模型适配流程

### Mermaid

```mermaid
flowchart TD
    build["启动装配<br/>cli.py/server.py::buildAgent()"]
    loadConfig["models/registry.py::loadModelConfig()"]
    yamlExists{"config/models.yaml 存在?"}

    yamlLoad["loadModelConfigFromYaml()<br/>默认 providerId='101'<br/>默认选第一个 model"]
    readYaml["读取 YAML<br/>yaml.safe_load()"]
    validateYaml["校验 providers / baseUrl / models / api"]
    apiTypeCheck{"api == 'openai-completions'?"}
    apiTypeErr["raise RuntimeError<br/>当前仅支持 openai-completions"]
    apiKeyRule{"apiKey 写法"}
    apiKeyEnvRef["${ENV} 或 $ENV<br/>使用外部环境变量名"]
    apiKeyInline["普通字符串<br/>写入 os.environ['FLAMINGO_AGENTS_101_API_KEY']"]
    yamlMissingKey{"os.getenv(apiKeyEnv) 有值?"}

    envLoad["loadModelConfigFromEnv()"]
    envModel["FLAMINGO_AGENTS_MODEL"]
    envBase["FLAMINGO_AGENTS_BASE_URL"]
    envKeyName["FLAMINGO_AGENTS_API_KEY_ENV<br/>默认 OPENAI_API_KEY"]
    envKeyValue["读取真实 API key 环境变量值"]
    envMissing{"任一配置缺失?"}

    configErr["raise RuntimeError('模型配置缺失...')"]
    configObj["返回 modelConfig<br/>provider / model / baseUrl / apiKeyEnv<br/>apiType='openaiCompatible'<br/>supportsToolCalling=True"]

    adapter["openaiAdapter(config, debugConsole)"]
    complete["openaiAdapter.complete(messages, tools)"]
    apiKeyCheck{"os.getenv(config.apiKeyEnv) 为空?"}
    raiseApiKey["raise RuntimeError('环境变量缺失')"]
    convertMessages["转换 messages<br/>openaiAdapter.convertMessage()"]
    payload["构造 requestPayload<br/>model/messages/tools/tool_choice='auto'"]
    request["POST baseUrl + '/chat/completions'<br/>urllib.request.urlopen(timeout=60)"]
    httpErr{"HTTPError / URLError?"}
    raiseHttp["raise RuntimeError('模型请求失败')"]
    jsonParse["json.loads(responseText)"]
    parse["openaiAdapter.parseAssistantPayload(payload)"]
    choicesCheck{"choices[0].message 合法?"}
    raiseShape["raise RuntimeError('模型响应缺少 choices/message')"]
    rawToolCalls["遍历 rawMessage.tool_calls"]
    argParse["json.loads(function.arguments)"]
    argErr{"arguments JSON 合法?"}
    raiseArg["raise RuntimeError('tool_call.arguments 不是合法 JSON')"]
    chatMsg["返回 chatMessage(role='assistant',<br/>content=content,<br/>toolCalls=parsedToolCalls)"]

    build --> loadConfig --> yamlExists
    yamlExists -- 是 --> yamlLoad --> readYaml --> validateYaml --> apiTypeCheck
    apiTypeCheck -- 否 --> apiTypeErr
    apiTypeCheck -- 是 --> apiKeyRule
    apiKeyRule -- 环境变量引用 --> apiKeyEnvRef --> yamlMissingKey
    apiKeyRule -- 直接密钥 --> apiKeyInline --> yamlMissingKey
    yamlMissingKey -- 否 --> configErr
    yamlMissingKey -- 是 --> configObj

    yamlExists -- 否 --> envLoad --> envModel --> envMissing
    envLoad --> envBase --> envMissing
    envLoad --> envKeyName --> envKeyValue --> envMissing
    envMissing -- 是 --> configErr
    envMissing -- 否 --> configObj

    configObj --> adapter --> complete --> apiKeyCheck
    apiKeyCheck -- 是 --> raiseApiKey
    apiKeyCheck -- 否 --> convertMessages --> payload --> request --> httpErr
    httpErr -- 是 --> raiseHttp
    httpErr -- 否 --> jsonParse --> parse --> choicesCheck
    choicesCheck -- 否 --> raiseShape
    choicesCheck -- 是 --> rawToolCalls --> argParse --> argErr
    argErr -- 是 --> raiseArg
    argErr -- 否 --> chatMsg

    classDef actionStyle fill:#eff6ff,stroke:#60a5fa,color:#1e3a8a;
    classDef decisionStyle fill:#fef9c3,stroke:#facc15,color:#713f12;
    classDef errorStyle fill:#fef2f2,stroke:#f87171,color:#7f1d1d;
    classDef successStyle fill:#ecfdf5,stroke:#34d399,color:#064e3b;

    class build,loadConfig,yamlLoad,readYaml,validateYaml,apiKeyEnvRef,apiKeyInline,envLoad,envModel,envBase,envKeyName,envKeyValue,adapter,complete,convertMessages,payload,request,jsonParse,parse,rawToolCalls,argParse actionStyle;
    class yamlExists,apiTypeCheck,apiKeyRule,yamlMissingKey,envMissing,apiKeyCheck,httpErr,choicesCheck,argErr decisionStyle;
    class apiTypeErr,configErr,raiseApiKey,raiseHttp,raiseShape,raiseArg errorStyle;
    class configObj,chatMsg successStyle;
```

### 配置加载细节

| 分支 | 文件 | 函数 / 类 | 规则 |
| --- | --- | --- | --- |
| 主入口 | `flamingoAgents/models/registry.py` | `loadModelConfig()` | 先检查默认路径 `config/models.yaml`；存在则读 YAML，不存在才读环境变量 |
| YAML 配置 | `flamingoAgents/models/registry.py` | `loadModelConfigFromYaml()` | 默认 providerId 为 `101`，默认选择 provider 下第一个 model；只支持 `api: openai-completions` |
| API key 解析 | `flamingoAgents/models/registry.py` | `loadModelConfigFromYaml()` | `apiKey` 为 `${ENV}` 或 `$ENV` 时读取对应环境变量；普通字符串会写入自动生成的环境变量名 |
| 环境变量 fallback | `flamingoAgents/models/registry.py` | `loadModelConfigFromEnv()` | 校验 `FLAMINGO_AGENTS_MODEL`、`FLAMINGO_AGENTS_BASE_URL` 和 API key 环境变量 |
| 配置自检 | `flamingoAgents/models/registry.py` | `testModelConfig()` | 使用当前配置请求 `/chat/completions`，确认模型能返回 assistant 内容 |

### 数据转换细节

| 转换对象 | 文件 | 函数 / 类 | 规则 |
| --- | --- | --- | --- |
| 内部消息 → OpenAI 消息 | `flamingoAgents/models/openai.py` | `openaiAdapter.convertMessage()` | `role='tool'` 转为 `{role, tool_call_id, content}`；assistant 工具调用转为 `tool_calls` 数组 |
| 工具 schema | `flamingoAgents/tools/registry.py` | `registry.listModelTools()` | 每个 `toolSpec` 转为 OpenAI function tool schema |
| 请求 payload | `flamingoAgents/models/openai.py` | `openaiAdapter.complete()` | 包含 `model`、`messages`、`tools`、`tool_choice='auto'` |
| 响应 choices | `flamingoAgents/models/openai.py` | `openaiAdapter.parseAssistantPayload()` | 取 `choices[0].message` |
| tool_calls | `flamingoAgents/models/openai.py` | `openaiAdapter.parseAssistantPayload()` | 解析 `function.name` 为 `toolName`，`function.arguments` JSON 为 `arguments` |
| assistant 结果 | `flamingoAgents/core/types.py` | `chatMessage` | 返回 `chatMessage(role='assistant', content=..., toolCalls=...)` |

---

## 3.6 工具注册与路由流程

### Mermaid

```mermaid
flowchart TD
    createRegistry["createDefaultRegistry()<br/>tools/registry.py"]
    newRegistry["registry.__init__()<br/>self.tools = {}"]
    registerRead["register(toolSpec name='read'<br/>execute=executeRead)"]
    registerWrite["register(toolSpec name='write'<br/>execute=executeWrite)"]
    registerEdit["register(toolSpec name='edit'<br/>execute=executeEdit)"]
    registerBash["register(toolSpec name='bash'<br/>execute=executeBash)"]
    listModelTools["模型调用前<br/>registry.listModelTools()"]
    schemas["输出 OpenAI-compatible tools schema"]

    toolCall["模型返回 toolCall<br/>types.py::toolCall"]
    agentGuard["Agent 先做删除检查<br/>guard.py::checkToolCall()"]
    router["router.executeTool(call, approvedDeletion=False)"]
    routerDebug["debugConsole.debug('路由工具调用')"]
    getDef["definition = registry.get(call.toolName)"]
    unknown{"definition is None?"}
    unknownResult["toolResult(isError=True,<br/>content='未知工具')"]
    argsCheck{"call.arguments 是 dict?"}
    invalidArgs["toolResult(isError=True,<br/>content='toolCall.arguments 必须是对象。')"]
    routerGuard["Router 二次检查<br/>checkToolCall(call)"]
    needsConfirm{"requiresConfirmation 且<br/>not approvedDeletion?"}
    raiseConfirm["raise confirmationNeeded(reason)"]
    execute["definition.execute(call.arguments, context)"]
    exception{"执行异常?"}
    exceptionResult["toolResult(isError=True,<br/>content='工具执行异常')"]
    patchIds["补齐 result.toolCallId / result.toolName"]
    returnResult["返回 toolResult"]

    createRegistry --> newRegistry --> registerRead --> registerWrite --> registerEdit --> registerBash --> listModelTools --> schemas
    toolCall --> agentGuard --> router --> routerDebug --> getDef --> unknown
    unknown -- 是 --> unknownResult --> returnResult
    unknown -- 否 --> argsCheck
    argsCheck -- 否 --> invalidArgs --> returnResult
    argsCheck -- 是 --> routerGuard --> needsConfirm
    needsConfirm -- 是 --> raiseConfirm
    needsConfirm -- 否 --> execute --> exception
    exception -- 是 --> exceptionResult --> returnResult
    exception -- 否 --> patchIds --> returnResult
```

### 默认工具清单

| 工具名 | schema 定义位置 | 执行函数 | 参数 | 行为概述 |
| --- | --- | --- | --- | --- |
| `read` | `tools/registry.py::createDefaultRegistry()` | `tools/file.py::executeRead()` | `path` 必填，`offset`/`limit` 可选 | 读取本地文本文件片段 |
| `write` | `tools/registry.py::createDefaultRegistry()` | `tools/file.py::executeWrite()` | `path`、`content` 必填 | 创建或完整覆盖文本文件 |
| `edit` | `tools/registry.py::createDefaultRegistry()` | `tools/file.py::executeEdit()` | `path`、`edits` 必填 | 按唯一 `oldText` 精确替换，返回 diff 预览 |
| `bash` | `tools/registry.py::createDefaultRegistry()` | `tools/bash.py::executeBash()` | `command` 必填，`timeout` 可选 | 在 `workDir` 执行 bash 命令，捕获 stdout/stderr |

---

## 3.7 文件工具流程：read / write / edit

### Mermaid

```mermaid
flowchart TD
    entry{"文件工具类型"}

    subgraph readFlow["read 工具：executeRead()"]
        readStart["读取 arguments<br/>path / offset / limit"]
        readValidate{"path 非空字符串<br/>offset/limit > 0?"}
        readArgErr["toolResult(isError=True)<br/>参数错误"]
        readNormalize["normalizePath(path, workDir)<br/>~ 展开；相对路径拼到 workDir"]
        readExists{"path.exists() and path.is_file()?"}
        readFileErr["toolResult(isError=True)<br/>文件不存在或不是普通文件"]
        readText["path.read_text(encoding='utf-8')"]
        readSlice["按 offset/limit 切行"]
        readPreview["makePreview(selectedText)"]
        readResult["toolResult(toolName='read', isError=False)<br/>details=path/offset/limit/totalLines/truncated"]
    end

    subgraph writeFlow["write 工具：executeWrite()"]
        writeStart["读取 arguments<br/>path / content"]
        writeValidate{"path 非空字符串<br/>content 是字符串?"}
        writeArgErr["toolResult(isError=True)<br/>参数错误"]
        writeNormalize["normalizePath(path, workDir)<br/>~ 展开；相对路径拼到 workDir"]
        writeMkdir["path.parent.mkdir(parents=True, exist_ok=True)"]
        writeText["path.write_text(content, encoding='utf-8')"]
        writePreview["makePreview(content)"]
        writeResult["toolResult(toolName='write', isError=False)<br/>details=path/bytes/contentPreview/truncated"]
    end

    subgraph editFlow["edit 工具：executeEdit()"]
        editStart["读取 arguments<br/>path / edits"]
        editValidate{"path 非空字符串<br/>edits 非空数组?"}
        editArgErr["toolResult(isError=True)<br/>参数错误"]
        editNormalize["normalizePath(path, workDir)<br/>~ 展开；相对路径拼到 workDir"]
        editExists{"path.exists() and path.is_file()?"}
        editFileErr["toolResult(isError=True)<br/>文件不存在或不是普通文件"]
        original["读取 originalContent"]
        eachEdit["逐个 edit 校验<br/>oldText 非空 / newText 字符串"]
        unique{"每个 oldText<br/>在原文中唯一匹配?"}
        uniqueErr["toolResult(isError=True)<br/>匹配数不是 1"]
        overlap["按 startIndex 排序<br/>检查替换区间不重叠"]
        overlapErr["toolResult(isError=True)<br/>多个 edits 不能重叠"]
        replace["倒序执行文本替换"]
        diff["difflib.unified_diff()"]
        writeUpdated["path.write_text(updatedContent, encoding='utf-8')"]
        diffPreview["makePreview(diffText)"]
        editResult["toolResult(toolName='edit', isError=False)<br/>content=diff 或 文件内容未发生变化"]
    end

    entry -- read --> readStart --> readValidate
    readValidate -- 否 --> readArgErr
    readValidate -- 是 --> readNormalize --> readExists
    readExists -- 否 --> readFileErr
    readExists -- 是 --> readText --> readSlice --> readPreview --> readResult

    entry -- write --> writeStart --> writeValidate
    writeValidate -- 否 --> writeArgErr
    writeValidate -- 是 --> writeNormalize --> writeMkdir --> writeText --> writePreview --> writeResult

    entry -- edit --> editStart --> editValidate
    editValidate -- 否 --> editArgErr
    editValidate -- 是 --> editNormalize --> editExists
    editExists -- 否 --> editFileErr
    editExists -- 是 --> original --> eachEdit --> unique
    unique -- 否 --> uniqueErr
    unique -- 是 --> overlap
    overlap -. 发现重叠 .-> overlapErr
    overlap -- 不重叠 --> replace --> diff --> writeUpdated --> diffPreview --> editResult

    classDef actionStyle fill:#eff6ff,stroke:#60a5fa,color:#1e3a8a;
    classDef decisionStyle fill:#fef9c3,stroke:#facc15,color:#713f12;
    classDef errorStyle fill:#fef2f2,stroke:#f87171,color:#7f1d1d;
    classDef successStyle fill:#ecfdf5,stroke:#34d399,color:#064e3b;

    class readStart,readNormalize,readText,readSlice,readPreview,writeStart,writeNormalize,writeMkdir,writeText,writePreview,editStart,editNormalize,original,eachEdit,overlap,replace,diff,writeUpdated,diffPreview actionStyle;
    class entry,readValidate,readExists,writeValidate,editValidate,editExists,unique decisionStyle;
    class readArgErr,readFileErr,writeArgErr,editArgErr,editFileErr,uniqueErr,overlapErr errorStyle;
    class readResult,writeResult,editResult successStyle;
```

### 文件工具函数明细

| 函数 | 文件 | 输入 | 输出 | 重要约束 |
| --- | --- | --- | --- | --- |
| `normalizePath()` | `flamingoAgents/tools/file.py` | `pathValue: str`、`workDir: Path` | 绝对 `Path` | 相对路径以 `workDir` 为基准；支持 `~` 展开 |
| `executeRead()` | `flamingoAgents/tools/file.py` | `arguments`、`toolContext` | `toolResult` | `offset` 与 `limit` 必须大于 0；只读普通文件；返回预览 |
| `executeWrite()` | `flamingoAgents/tools/file.py` | `arguments`、`toolContext` | `toolResult` | `content` 必须是字符串；自动创建父目录；完整覆盖 |
| `executeEdit()` | `flamingoAgents/tools/file.py` | `arguments`、`toolContext` | `toolResult` | `edits` 非空；每个 `oldText` 在原文件中必须唯一；多个替换不能重叠 |

---

## 3.8 Bash 工具流程

### Mermaid

```mermaid
flowchart TD
    bashStart["executeBash(arguments, context)<br/>tools/bash.py"]
    commandCheck{"command 是非空字符串?"}
    commandErr["toolResult(isError=True,<br/>content='bash.command 必须是非空字符串。')"]
    timeoutRead["timeout = int(arguments.get('timeout', 30))"]
    timeoutClamp["限制 timeout<br/><1 用默认 30<br/>>120 截断为 120"]
    debug["debugConsole.debug('执行 bash：...')"]
    run["subprocess.run(['bash','-lc',command],<br/>cwd=context.workDir,<br/>capture_output=True,<br/>text=True,<br/>timeout=timeout,<br/>check=False)"]
    timedOut{"TimeoutExpired?"}
    timeoutPreview["提取 error.stdout/error.stderr<br/>makePreview()"]
    timeoutResult["toolResult(isError=True,<br/>content='命令超时，已终止。',<br/>details.timeoutExpired=True)"]
    stdoutPreview["makePreview(completedProcess.stdout)"]
    stderrPreview["makePreview(completedProcess.stderr)"]
    exitCode["isError = returncode != 0"]
    normalResult["toolResult(toolName='bash',<br/>content=exitCode/stdout/stderr,<br/>details=command/timeout/exitCode/previews)"]

    bashStart --> commandCheck
    commandCheck -- 否 --> commandErr
    commandCheck -- 是 --> timeoutRead --> timeoutClamp --> debug --> run --> timedOut
    timedOut -- 是 --> timeoutPreview --> timeoutResult
    timedOut -- 否 --> stdoutPreview --> stderrPreview --> exitCode --> normalResult
```

### 关键规则

| 规则 | 文件 | 函数 / 常量 | 说明 |
| --- | --- | --- | --- |
| 默认超时 | `flamingoAgents/tools/bash.py` | `defaultTimeoutSeconds = 30` | 未传或非法小于 1 时使用 30 秒 |
| 最大超时 | `flamingoAgents/tools/bash.py` | `maxTimeoutSeconds = 120` | 超过 120 秒会被截断 |
| 工作目录 | `flamingoAgents/tools/bash.py` | `executeBash()` | `cwd=str(context.workDir)` |
| 输出捕获 | `flamingoAgents/tools/bash.py` | `executeBash()` | 捕获 stdout/stderr，返回预览文本 |
| 错误判定 | `flamingoAgents/tools/bash.py` | `executeBash()` | `returncode != 0` 即 `isError=True` |
| 超时处理 | `flamingoAgents/tools/bash.py` | `executeBash()` | 捕获 `subprocess.TimeoutExpired`，返回 `timeoutExpired=True` |

---

## 3.9 会话与 JSONL 日志流程

### Mermaid

```mermaid
flowchart TD
    getConversation["agent.getConversation(sessionId)<br/>core/agent.py"]
    exists{"self.conversations 中已存在?"}
    returnExisting["返回已有 conversation"]
    makePath["dateText = datetime.now().strftime('%Y%m%d')<br/>logPath = logDir / f'{dateText}_{sessionId}.jsonl'"]
    newConv["conversation(sessionId, logPath, systemPrompt)<br/>core/conversation.py"]
    loggerCtor["jsonlLog(logPath)<br/>utils/jsonl.py"]
    mkdir["logPath.parent.mkdir(parents=True, exist_ok=True)"]
    addSystem["conversation.addMessage(chatMessage(role='system',<br/>content=systemPrompt))"]
    store["self.conversations[sessionId] = newConversation"]

    addMessage["conversation.addMessage(message)"]
    appendMem["self.messages.append(message)"]
    assistantWithTools{"message.role == 'assistant' 且有 toolCalls?"}
    logAssistantContent["若 assistant 有 content<br/>logger.logEvent(type='message')"]
    logEachToolCall["遍历 toolCalls<br/>makePreview(call.arguments)<br/>logger.logEvent(type='toolCall')"]
    logNormalMsg["普通消息<br/>logger.logEvent(type='message', role/content/toolCallId/name)"]

    addToolResult["conversation.addToolResult(result)"]
    previewResult["makePreview(result.content)<br/>makePreview(result.details)"]
    logToolResult["logger.logEvent(type='toolResult')"]
    appendToolMsg["self.messages.append(chatMessage(role='tool',<br/>content=result.content,<br/>toolCallId=result.toolCallId,<br/>name=result.toolName))"]

    logEvent["jsonlLog.logEvent(event)"]
    toJson["toJsonable(event)"]
    redact["redactText(eventText)"]
    writeLine["append safeText + '\\n' 到 .jsonl"]

    getConversation --> exists
    exists -- 是 --> returnExisting
    exists -- 否 --> makePath --> newConv --> loggerCtor --> mkdir --> addSystem --> store
    addMessage --> appendMem --> assistantWithTools
    assistantWithTools -- 是 --> logAssistantContent --> logEachToolCall --> logEvent
    assistantWithTools -- 否 --> logNormalMsg --> logEvent
    addToolResult --> previewResult --> logToolResult --> appendToolMsg
    logToolResult --> logEvent
    logEvent --> toJson --> redact --> writeLine
```

### 日志事件类型

| 事件类型 | 写入位置 | 文件 | 函数 | 内容 |
| --- | --- | --- | --- | --- |
| `message` | system/user/assistant 普通消息 | `core/conversation.py` | `conversation.addMessage()` | `role`、`content`、`toolCallId`、`name` |
| `toolCall` | assistant 发起工具调用 | `core/conversation.py` | `conversation.addMessage()` | `toolCallId`、`toolName`、`argumentsPreview`、`argumentsTruncated` |
| `toolResult` | 工具执行结果 | `core/conversation.py` | `conversation.addToolResult()` | `toolCallId`、`toolName`、`isError`、content/details 预览 |
| `modelError` | 模型调用异常 | `core/agent.py` | `agent.continueModelLoop()` | `errorType`、`message` |
| 自定义预览事件 | 目前未在主链路调用 | `utils/jsonl.py` | `jsonlLog.logPreviewEvent()` | `payloadPreview`、`truncated` |

### 脱敏与预览

| 函数 / 常量 | 文件 | 作用 |
| --- | --- | --- |
| `previewLimit = 4000` | `flamingoAgents/utils/jsonl.py` | 默认预览长度限制 |
| `secretPatterns` | `flamingoAgents/utils/jsonl.py` | 识别 `api_key/token/secret/password`、`Bearer ...`、`sk-...` |
| `redactText()` | `flamingoAgents/utils/jsonl.py` | 替换敏感文本为 `<redacted>` 或 `sk-<redacted>` |
| `toJsonable()` | `flamingoAgents/utils/jsonl.py` | dataclass、Path、dict、list 转 JSON 可序列化对象 |
| `makePreview()` | `flamingoAgents/utils/jsonl.py` | 先 JSON 化/转字符串，再脱敏，再按长度截断 |
| `jsonlLog.logEvent()` | `flamingoAgents/utils/jsonl.py` | 加 UTC `timestamp`，脱敏后追加写入 `.jsonl` |

---

## 3.10 数据结构流转

### Mermaid

```mermaid
flowchart LR
    modelConfig["modelConfig<br/>core/types.py<br/>provider/model/baseUrl/apiKeyEnv/apiType/supportsToolCalling"]
    adapter["openaiAdapter<br/>models/openai.py"]

    toolSpec["toolSpec<br/>name/description/parameters/execute"]
    registry["registry<br/>tools/registry.py<br/>tools: dict[str, toolSpec]"]
    modelTools["OpenAI tools schema<br/>registry.listModelTools()"]

    chatMessage["chatMessage<br/>role/content/toolCalls/toolCallId/name"]
    toolCall["toolCall<br/>id/toolName/arguments"]
    toolResult["toolResult<br/>toolCallId/toolName/isError/content/details"]
    toolContext["toolContext<br/>workDir/debugConsole"]

    pendingConfirm["pendingConfirm<br/>sessionId/confirmationId/reason/toolCall"]
    runResult["runResult<br/>sessionId/status/message/confirmationId/reason/commandPreview/toolCall"]

    conversation["conversation<br/>messages: list[chatMessage]"]
    agent["agent<br/>conversations/pendingConfirms"]
    router["router<br/>registry/context"]

    modelConfig --> adapter
    toolSpec --> registry --> modelTools --> adapter
    chatMessage --> adapter
    adapter --> chatMessage
    chatMessage --> toolCall
    toolCall --> pendingConfirm
    toolCall --> router
    toolContext --> router
    router --> toolResult
    toolResult --> chatMessage
    chatMessage --> conversation
    conversation --> agent
    pendingConfirm --> agent
    agent --> runResult
```

### 数据结构表

| 类型 | 文件 | 字段 | 用途 |
| --- | --- | --- | --- |
| `toolCall` | `flamingoAgents/core/types.py` | `id`、`toolName`、`arguments` | 表示模型要求执行的单个工具调用 |
| `chatMessage` | `flamingoAgents/core/types.py` | `role`、`content`、`toolCalls`、`toolCallId`、`name` | 内部统一消息格式 |
| `toolResult` | `flamingoAgents/core/types.py` | `toolCallId`、`toolName`、`isError`、`content`、`details` | 工具执行结果，随后转为 `role='tool'` 的消息 |
| `toolContext` | `flamingoAgents/core/types.py` | `workDir`、`debugConsole` | 工具执行依赖的上下文 |
| `toolSpec` | `flamingoAgents/core/types.py` | `name`、`description`、`parameters`、`execute` | 工具定义与执行函数绑定 |
| `modelConfig` | `flamingoAgents/core/types.py` | `provider`、`model`、`baseUrl`、`apiKeyEnv`、`apiType`、`supportsToolCalling` | 模型适配器配置 |
| `runResult` | `flamingoAgents/core/types.py` | `sessionId`、`status`、`message`、`confirmationId`、`reason`、`commandPreview`、`toolCall` | Agent 对入口层返回的统一结果 |
| `pendingConfirm` | `flamingoAgents/core/types.py` | `sessionId`、`confirmationId`、`reason`、`toolCall` | HTTP 删除确认暂存数据 |
| `guardDecision` | `flamingoAgents/tools/guard.py` | `allowed`、`requiresConfirmation`、`reason` | 删除风险判断结果 |

---

## 3.11 Debug 输出流程

### Mermaid

```mermaid
flowchart TD
    args["入口参数 --debug<br/>cli.py::main() / server.py::main()"]
    ctor["debugConsole(debugEnabled)<br/>utils/debug.py"]
    store["传入 Agent / openaiAdapter / toolContext"]
    callDebug["各层调用 debugConsole.debug(message)"]
    isDebug{"debugConsole.isDebug?"}
    print["打印 [debug HH:MM:SS] message<br/>flush=True"]
    silent["不输出"]

    args --> ctor --> store --> callDebug --> isDebug
    isDebug -- True --> print
    isDebug -- False --> silent
```

### 主要调用位置

| 位置 | 文件 | 函数 | 输出内容 |
| --- | --- | --- | --- |
| CLI 启动 | `flamingoAgents/app/cli.py` | `main()` | `CLI 启动 workDir=... sessionId=...` |
| HTTP POST | `flamingoAgents/app/server.py` | `agentHttpHandler.do_POST()` | 请求路径 |
| HTTP chat/confirm | `flamingoAgents/app/server.py` | `handleChat()`、`handleConfirm()` | sessionId、confirmationId、approved 等 |
| 用户消息 | `flamingoAgents/core/agent.py` | `agent.runUserMessage()` | sessionId 与消息字符数 |
| 模型循环 | `flamingoAgents/core/agent.py` | `agent.continueModelLoop()` | step、sessionId、消息数、工具数 |
| 工具执行 | `flamingoAgents/core/agent.py`、`tools/router.py`、`tools/bash.py`、`tools/file.py` | 多处 | 工具名、callId、读写路径、bash 命令等 |
| 模型请求 | `flamingoAgents/models/openai.py` | `openaiAdapter.complete()` | provider、model、消息数、工具数、URL |

---

## 3.12 手动验证流程

### Mermaid

```mermaid
flowchart TD
    start["uv run python manualChecks.py <check> [--debug]<br/>manualChecks.py::main()"]
    parse["argparse<br/>choices=all/fileTools/bash/guard/logger/adapter/agent/http"]
    isAll{"args.check == 'all'?"}

    allSequence["all：按代码顺序执行<br/>fileTools → bash → guard → logger → adapter → agent → http"]
    chooseOne{"单项 check"}

    fileTools["runFileToolCheck()<br/>write/read/edit 文件工具"]
    bash["runBashCheck()<br/>bash 正常输出与 timeout"]
    guard["runGuardCheck()<br/>删除命令识别；grep 不误判"]
    logger["runLoggerCheck()<br/>JSONL secret 脱敏"]
    adapter["runAdapterParseCheck()<br/>OpenAI tool_calls 解析"]
    agent["runAgentCheck()<br/>fakeModel + Agent 核心链路"]
    http["runHttpCheck()<br/>makeHttpHandler() /chat /confirm"]

    done["对应检查成功后<br/>printPass(name) 输出 PASS xxx"]
    allDone["all 全部成功<br/>依次输出 7 个 PASS"]
    fail["任一 expect(False, message)<br/>raise RuntimeError 并停止"]

    start --> parse --> isAll
    isAll -- 是 --> allSequence --> allDone
    isAll -- 否 --> chooseOne
    chooseOne -- fileTools --> fileTools --> done
    chooseOne -- bash --> bash --> done
    chooseOne -- guard --> guard --> done
    chooseOne -- logger --> logger --> done
    chooseOne -- adapter --> adapter --> done
    chooseOne -- agent --> agent --> done
    chooseOne -- http --> http --> done

    allSequence -. 任一子检查失败 .-> fail
    fileTools -. 校验失败 .-> fail
    bash -. 校验失败 .-> fail
    guard -. 校验失败 .-> fail
    logger -. 校验失败 .-> fail
    adapter -. 校验失败 .-> fail
    agent -. 校验失败 .-> fail
    http -. 校验失败 .-> fail

    classDef actionStyle fill:#eff6ff,stroke:#60a5fa,color:#1e3a8a;
    classDef decisionStyle fill:#fef9c3,stroke:#facc15,color:#713f12;
    classDef successStyle fill:#ecfdf5,stroke:#34d399,color:#064e3b;
    classDef errorStyle fill:#fef2f2,stroke:#f87171,color:#7f1d1d;

    class start,parse,allSequence,fileTools,bash,guard,logger,adapter,agent,http actionStyle;
    class isAll,chooseOne decisionStyle;
    class done,allDone successStyle;
    class fail errorStyle;
```

### 验证覆盖点

| 检查函数 | 文件 | 覆盖模块 | 重点断言 |
| --- | --- | --- | --- |
| `fakeModel.complete()` | `manualChecks.py` | Agent 模型交互替身 | 根据最后一条消息返回普通回复、工具调用或失败说明 |
| `runFileToolCheck()` | `manualChecks.py` | `tools/file.py` | write/read/edit 无错误且文件内容变化正确 |
| `runBashCheck()` | `manualChecks.py` | `tools/bash.py` | `printf hello` 成功；`sleep 2` 在 1 秒 timeout 下超时 |
| `runGuardCheck()` | `manualChecks.py` | `tools/guard.py` | 删除命令命中；`grep` 不应误判 |
| `runLoggerCheck()` | `manualChecks.py` | `utils/jsonl.py` | `sk-...` 被脱敏，原文不泄露 |
| `runAdapterParseCheck()` | `manualChecks.py` | `models/openai.py` | `tool_calls` 的 name 与 arguments 能正确解析 |
| `buildFakeAgent()` | `manualChecks.py` | `core/agent.py` | 使用 `fakeModel` 和默认工具注册表构造 Agent |
| `runAgentCheck()` | `manualChecks.py` | 核心 Agent 链路 | 读文件完成、删除需要确认、拒绝后文件仍存在、curl 失败不绕过 |
| `runHttpCheck()` | `manualChecks.py` | HTTP 链路 | `/chat` 返回 `confirmationRequired`，`/confirm` 拒绝后完成且文件仍存在 |

---

## 4. 完整调用链清单

### 4.1 CLI 完整链路

```text
pyproject.toml
└── flamingo-agents = flamingoAgents.app.cli:main
    └── flamingoAgents/app/cli.py::main()
        ├── argparse.ArgumentParser(...)
        ├── buildAgent(debugEnabled, workDir)
        │   ├── utils/debug.py::debugConsole(debugEnabled)
        │   ├── models/registry.py::loadModelConfig()
        │   ├── models/openai.py::openaiAdapter(config, printer)
        │   ├── tools/registry.py::createDefaultRegistry()
        │   │   └── registry.register(toolSpec(...)) x 4
        │   └── core/agent.py::agent(..., confirmDeletion=askDeletionConfirmation)
        ├── input('你> ')
        ├── /exit / /help / 空输入处理
        └── core/agent.py::agent.runUserMessage(userInput, sessionId)
            └── core/agent.py::agent.continueModelLoop(sessionId)
```

### 4.2 HTTP 完整链路

```text
pyproject.toml
└── flamingo-agents-server = flamingoAgents.app.server:main
    └── flamingoAgents/app/server.py::main()
        ├── argparse.ArgumentParser(...)
        ├── buildAgent(debugEnabled, workDir)
        │   ├── utils/debug.py::debugConsole(debugEnabled)
        │   ├── models/registry.py::loadModelConfig()
        │   ├── models/openai.py::openaiAdapter(config, printer)
        │   ├── tools/registry.py::createDefaultRegistry()
        │   └── core/agent.py::agent(..., confirmDeletion=None)
        ├── makeHttpHandler(agent)
        │   └── class agentHttpHandler(BaseHTTPRequestHandler)
        │       ├── do_POST()
        │       ├── handleChat()
        │       │   └── agent.runUserMessage(message, sessionId)
        │       ├── handleConfirm()
        │       │   └── agent.continueConfirmation(sessionId, confirmationId, approved)
        │       ├── readJson()
        │       ├── respondJson(statusCode, payload)
        │       └── log_message(format, *args)
        └── ThreadingHTTPServer(...).serve_forever()
```

### 4.3 Agent + 模型 + 工具完整链路

```text
core/agent.py::agent.runUserMessage()
├── createSessionId()                       # sessionId 为空时
├── getConversation(sessionId)
│   └── core/conversation.py::conversation.__init__()
│       ├── utils/jsonl.py::jsonlLog(logPath)
│       └── conversation.addMessage(system message)
├── conversation.addMessage(user message)
└── continueModelLoop(sessionId)
    ├── createRouter()
    │   └── tools/router.py::router(registry, toolContext(...))
    ├── registry.listModelTools()
    ├── modelAdapter.complete(messages, tools)
    │   ├── openaiAdapter.convertMessage(message) x N
    │   ├── urllib.request.urlopen(.../chat/completions)
    │   └── openaiAdapter.parseAssistantPayload(payload)
    ├── conversation.addMessage(assistantMessage)
    ├── 如果没有 toolCalls：return runResult(status='completed')
    └── 如果存在 toolCalls：for call in assistantMessage.toolCalls
        ├── tools/guard.py::checkToolCall(call)
        ├── 如果需要确认：
        │   ├── CLI：app/cli.py::askDeletionConfirmation(call, reason)
        │   └── HTTP：保存 pendingConfirm 并 return confirmationRequired
        ├── tools/router.py::router.executeTool(call, approvedDeletion=...)
        │   ├── registry.get(call.toolName)
        │   ├── checkToolCall(call)          # 二次保护
        │   ├── definition.execute(arguments, context)
        │   │   ├── tools/file.py::executeRead()
        │   │   ├── tools/file.py::executeWrite()
        │   │   ├── tools/file.py::executeEdit()
        │   │   └── tools/bash.py::executeBash()
        │   └── 返回 toolResult
        ├── conversation.addToolResult(result)
        └── 回到下一轮 modelAdapter.complete(...)
```

---

## 5. 文件与函数索引

### 5.1 `flamingoAgents/app/cli.py`

| 函数 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `askDeletionConfirmation(call, reason)` | 21 | CLI 删除命令确认 | 被 `agent.continueModelLoop()` 通过回调调用；读取 `toolCall.arguments['command']` |
| `buildAgent(debugEnabled, workDir)` | 30 | CLI Agent 装配 | 调 `debugConsole()`、`loadModelConfig()`、`openaiAdapter()`、`createDefaultRegistry()`、`agent()` |
| `main()` | 45 | CLI 主入口 | 解析参数，循环读取输入，调用 `agent.runUserMessage()` |

### 5.2 `flamingoAgents/app/server.py`

| 函数 / 方法 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `resultToDict(result)` | 23 | `runResult` 转 HTTP JSON dict | 被 `handleChat()`、`handleConfirm()` 调用 |
| `makeHttpHandler(agent)` | 38 | 创建绑定 Agent 的 `BaseHTTPRequestHandler` 子类 | 被 `main()` 调用 |
| `agentHttpHandler.do_POST()` | 42 | HTTP POST 路由 | 调 `handleChat()`、`handleConfirm()`、`respondJson()` |
| `agentHttpHandler.handleChat()` | 53 | 处理 `/chat` | 调 `readJson()`、`agent.runUserMessage()`、`resultToDict()`、`respondJson()` |
| `agentHttpHandler.handleConfirm()` | 69 | 处理 `/confirm` | 调 `readJson()`、`agent.continueConfirmation()`、`resultToDict()`、`respondJson()` |
| `agentHttpHandler.readJson()` | 89 | 读取 JSON body | 被 `handleChat()`、`handleConfirm()` 调用 |
| `agentHttpHandler.respondJson()` | 98 | 写 HTTP JSON 响应 | 被各 handler 调用 |
| `agentHttpHandler.log_message()` | 106 | 控制访问日志输出 | debug 时调用父类日志 |
| `buildAgent(debugEnabled, workDir)` | 113 | HTTP Agent 装配 | 与 CLI 类似，但 `confirmDeletion=None` |
| `main()` | 128 | HTTP 服务入口 | 创建 `ThreadingHTTPServer` 并 `serve_forever()` |

### 5.3 `flamingoAgents/core/agent.py`

| 函数 / 方法 / 常量 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `systemPrompt` | 29 | Agent 系统提示词 | `conversation.__init__()` 通过 `getConversation()` 获得 |
| `agent.__init__()` | 33 | 保存核心依赖与状态 | 被 CLI/HTTP `buildAgent()` 调用 |
| `agent.runUserMessage(message, sessionId)` | 53 | 接收用户消息并启动循环 | 被 CLI `main()`、HTTP `handleChat()` 调用 |
| `agent.continueConfirmation(sessionId, confirmationId, approved)` | 64 | HTTP 删除确认后续跑 | 被 HTTP `handleConfirm()` 调用；调 `router.executeTool()`、`makeBlockedToolResult()`、`continueModelLoop()` |
| `agent.continueModelLoop(sessionId)` | 80 | 模型-工具循环核心 | 调 `modelAdapter.complete()`、`conversation.addMessage()`、`checkToolCall()`、`router.executeTool()`、`conversation.addToolResult()` |
| `agent.getConversation(sessionId)` | 144 | 获取或创建会话 | 被 `runUserMessage()`、`continueConfirmation()`、`continueModelLoop()` 调用 |
| `agent.createRouter()` | 154 | 创建工具路由器 | 被 `continueConfirmation()`、`continueModelLoop()` 调用 |
| `agent.createSessionId()` | 158 | 生成 session id | 被 `runUserMessage()` 调用 |

### 5.4 `flamingoAgents/core/conversation.py`

| 函数 / 方法 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `conversation.__init__(sessionId, logPath, systemPrompt)` | 17 | 初始化会话、日志器、系统消息 | 被 `agent.getConversation()` 调用 |
| `conversation.addMessage(message)` | 23 | 追加消息并写 JSONL 日志 | 被 `agent.runUserMessage()`、`agent.continueModelLoop()`、`conversation.__init__()` 调用 |
| `conversation.addToolResult(result)` | 52 | 记录工具结果并追加 tool 消息 | 被 `agent.continueModelLoop()`、`agent.continueConfirmation()` 调用 |

### 5.5 `flamingoAgents/core/types.py`

| 类型 / 别名 | 行号 | 作用 | 主要使用位置 |
| --- | ---: | --- | --- |
| `messageRole` | 14 | 消息角色 Literal | `chatMessage.role` |
| `agentStatus` | 15 | Agent 返回状态 Literal | `runResult.status` |
| `toolCall` | 19 | 工具调用数据 | 模型解析、guard、router、pendingConfirm |
| `chatMessage` | 26 | 统一聊天消息 | conversation、openaiAdapter、manualChecks fakeModel |
| `toolResult` | 35 | 工具执行结果 | router、tool implementations、conversation |
| `toolContext` | 44 | 工具上下文 | router、file/bash tools |
| `toolSpec` | 50 | 工具定义 | registry |
| `modelConfig` | 58 | 模型配置 | model registry、openaiAdapter |
| `runResult` | 68 | 入口层统一响应 | CLI/HTTP/agent |
| `pendingConfirm` | 79 | HTTP 删除确认暂存 | agent.pendingConfirms |

### 5.6 `flamingoAgents/models/registry.py`

| 函数 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `loadModelConfig()` | 25 | 模型配置主入口 | 被 CLI/HTTP `buildAgent()` 调用；优先读 `config/models.yaml`，缺失时 fallback 到环境变量 |
| `testModelConfig()` | 31 | 测试当前模型配置是否可请求 `/chat/completions` | 调 `loadModelConfig()`、`urllib.request.urlopen()`；返回 assistant 内容 |
| `loadModelConfigFromEnv()` | 82 | 从环境变量加载模型配置并校验 | 被 `loadModelConfig()` fallback 调用；返回 `modelConfig` |
| `loadModelConfigFromYaml()` | 108 | 从 YAML provider/model 配置加载模型配置 | 被 `loadModelConfig()` 默认调用；校验 provider、model、api 与 apiKey |

### 5.7 `flamingoAgents/models/openai.py`

| 函数 / 方法 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `openaiAdapter.__init__(config, debugConsole)` | 20 | 保存配置和 debug 控制台 | 被 CLI/HTTP `buildAgent()` 调用 |
| `openaiAdapter.complete(messages, tools)` | 24 | 发起 OpenAI 兼容 chat completions 请求 | 被 `agent.continueModelLoop()` 调用；调 `convertMessage()`、`parseAssistantPayload()` |
| `openaiAdapter.convertMessage(message)` | 63 | 内部消息转 OpenAI 消息 | 被 `complete()` 调用 |
| `openaiAdapter.parseAssistantPayload(payload)` | 88 | 解析模型响应为 `chatMessage` | 被 `complete()` 与 `manualChecks.runAdapterParseCheck()` 调用 |

### 5.8 `flamingoAgents/tools/registry.py`

| 函数 / 方法 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `registry.__init__()` | 16 | 初始化工具字典 | 被 `createDefaultRegistry()` 调用 |
| `registry.register(definition)` | 19 | 注册一个 `toolSpec` | 被 `createDefaultRegistry()` 调用 |
| `registry.get(name)` | 22 | 按名称取工具定义 | 被 `router.executeTool()` 调用 |
| `registry.listDefinitions()` | 25 | 返回全部工具定义 | 被 `listModelTools()` 与 debug 日志使用 |
| `registry.listModelTools()` | 28 | 生成 OpenAI function tools schema | 被 `agent.continueModelLoop()` 调用 |
| `createDefaultRegistry()` | 42 | 创建并注册默认四个工具 | 被 CLI/HTTP `buildAgent()` 与 `manualChecks.buildFakeAgent()` 调用 |

### 5.9 `flamingoAgents/tools/router.py`

| 函数 / 方法 / 类 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `confirmationNeeded` | 15 | Router 层删除确认异常 | `router.executeTool()` 在未批准删除时抛出 |
| `confirmationNeeded.__init__(reason)` | 16 | 保存确认原因 | Router 内部使用 |
| `router.__init__(registry, context)` | 22 | 保存工具表与上下文 | 被 `agent.createRouter()` 调用 |
| `router.executeTool(call, approvedDeletion=False)` | 26 | 校验、路由、执行工具 | 被 `agent.continueModelLoop()`、`agent.continueConfirmation()` 调用 |

### 5.10 `flamingoAgents/tools/guard.py`

| 函数 / 类型 / 常量 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `guardDecision` | 17 | 删除风险判断结果 | `checkToolCall()` 返回 |
| `deletePatterns` | 23 | 删除命令正则列表 | 被 `detectDeletionCommand()` 使用 |
| `detectDeletionCommand(command)` | 34 | 判断命令是否疑似删除 | 被 `checkToolCall()` 与 `manualChecks.runGuardCheck()` 调用 |
| `checkToolCall(call)` | 41 | 判断工具调用是否需要确认 | 被 `agent.continueModelLoop()` 与 `router.executeTool()` 调用 |
| `makeBlockedToolResult(call, reason)` | 54 | 构造用户拒绝后的工具结果 | 被 `agent.continueModelLoop()`、`agent.continueConfirmation()` 调用 |

### 5.11 `flamingoAgents/tools/file.py`

| 函数 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `normalizePath(pathValue, workDir)` | 18 | 规范化相对/绝对路径 | 被 `executeRead()`、`executeWrite()`、`executeEdit()` 调用 |
| `executeRead(arguments, context)` | 25 | 读取文本文件片段 | 被 `router.executeTool()` 间接调用 |
| `executeWrite(arguments, context)` | 63 | 写入文本文件 | 被 `router.executeTool()` 间接调用 |
| `executeEdit(arguments, context)` | 91 | 精确替换文本并生成 diff | 被 `router.executeTool()` 间接调用 |

### 5.12 `flamingoAgents/tools/bash.py`

| 函数 / 常量 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `maxTimeoutSeconds` | 16 | bash 最大超时秒数 | `executeBash()` 使用 |
| `defaultTimeoutSeconds` | 17 | bash 默认超时秒数 | `executeBash()` 使用 |
| `executeBash(arguments, context)` | 20 | 执行 bash 命令并返回输出预览 | 被 `router.executeTool()` 间接调用；调 `subprocess.run()`、`makePreview()` |

### 5.13 `flamingoAgents/utils/debug.py`

| 函数 / 方法 / 类 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `debugConsole` | 15 | Debug 输出控制器 | CLI/HTTP build、Agent、工具、模型使用 |
| `debugConsole.debug(message)` | 18 | debug 模式打印时间戳消息 | 被多模块调用 |
| `debugConsole.visible(message)` | 23 | 无条件打印消息 | 当前主链路未使用 |

### 5.14 `flamingoAgents/utils/jsonl.py`

| 函数 / 方法 / 常量 | 行号 | 作用 | 被谁调用 / 调谁 |
| --- | ---: | --- | --- |
| `previewLimit` | 17 | 默认预览长度 4000 | `makePreview()` 使用 |
| `secretPatterns` | 18 | 敏感信息正则 | `redactText()` 使用 |
| `redactText(text)` | 25 | 脱敏文本 | `makePreview()`、`jsonlLog.logEvent()` 调用 |
| `makePreview(value, limit=previewLimit)` | 33 | 生成安全预览并标记截断 | conversation、file/bash tools、jsonlLog 调用 |
| `toJsonable(value)` | 44 | 转 JSON 可序列化结构 | `makePreview()`、`jsonlLog.logEvent()` 调用 |
| `jsonlLog.__init__(logPath)` | 57 | 创建日志目录 | `conversation.__init__()` 调用 |
| `jsonlLog.logEvent(event)` | 61 | 写一行 JSONL 审计事件 | conversation、agent 调用 |
| `jsonlLog.logPreviewEvent(eventType, payload)` | 71 | 写预览事件 | 当前主链路未调用 |

### 5.15 `manualChecks.py`

| 函数 / 类 | 行号 | 作用 |
| --- | ---: | --- |
| `fakeModel` | 32 | 模拟模型适配器，根据最后一条消息返回固定响应或工具调用 |
| `fakeModel.complete()` | 33 | fake model 的核心分支逻辑 |
| `expect(condition, message)` | 70 | 断言失败时抛 `RuntimeError` |
| `printPass(name)` | 75 | 打印 `PASS xxx` |
| `printDebug(debugEnabled, message)` | 79 | 手动检查脚本的 debug 输出 |
| `runFileToolCheck(debugEnabled)` | 84 | 检查文件工具 |
| `runBashCheck(debugEnabled)` | 101 | 检查 bash 工具 |
| `runGuardCheck()` | 112 | 检查删除命令识别 |
| `runLoggerCheck()` | 128 | 检查 JSONL 脱敏 |
| `runAdapterParseCheck()` | 139 | 检查 OpenAI adapter 响应解析 |
| `buildFakeAgent(workDir, debugEnabled)` | 165 | 用 fake model 创建 Agent |
| `runAgentCheck(debugEnabled)` | 176 | 检查 Agent 读文件、删除确认、curl 失败链路 |
| `runHttpCheck(debugEnabled)` | 196 | 检查 HTTP `/chat` 与 `/confirm` 链路 |
| `main()` | 230 | 手动检查入口 |

---

## 6. 运行与验证建议

### 6.1 手动检查命令

```bash
uv run python manualChecks.py all
```

如需查看详细调试输出：

```bash
uv run python manualChecks.py all --debug
```

### 6.2 CLI 启动命令

当前仓库存在 `config/models.yaml`，默认会优先使用该配置：

```bash
uv run flamingo-agents --debug --work-dir . --session-id cliSession
```

如果移除 `config/models.yaml` 或改为环境变量模式，需要提供：

```bash
FLAMINGO_AGENTS_MODEL=<model> \
FLAMINGO_AGENTS_BASE_URL=<base-url> \
OPENAI_API_KEY=<api-key> \
uv run flamingo-agents --debug --work-dir . --session-id cliSession
```

### 6.3 HTTP 启动命令

当前仓库存在 `config/models.yaml`，默认会优先使用该配置：

```bash
uv run flamingo-agents-server --debug --host 127.0.0.1 --port 8765 --work-dir .
```

如果移除 `config/models.yaml` 或改为环境变量模式，需要提供：

```bash
FLAMINGO_AGENTS_MODEL=<model> \
FLAMINGO_AGENTS_BASE_URL=<base-url> \
OPENAI_API_KEY=<api-key> \
uv run flamingo-agents-server --debug --host 127.0.0.1 --port 8765 --work-dir .
```

### 6.4 HTTP 调用示例

```bash
curl -sS http://127.0.0.1:8765/chat \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"demo","message":"请读取 sample.txt"}'
```

若返回 `confirmationRequired`：

```bash
curl -sS http://127.0.0.1:8765/confirm \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"demo","confirmationId":"confirm_xxx","approved":false}'
```

---

## 7. 重要边界与注意事项

| 主题 | 当前行为 | 相关代码 |
| --- | --- | --- |
| HTTP 只处理 POST | `do_POST()` 只分发 `/chat` 与 `/confirm`，其他路径 404 | `flamingoAgents/app/server.py::agentHttpHandler.do_POST()` |
| HTTP JSON 解析失败 | `readJson()` 返回 `{}`，随后由业务校验返回 400 | `flamingoAgents/app/server.py::readJson()` |
| CLI 删除确认同步 | CLI 不返回 `confirmationRequired` 给用户，而是在工具执行前阻塞询问 | `cli.py::askDeletionConfirmation()`、`agent.py::continueModelLoop()` |
| HTTP 删除确认异步 | HTTP 首次 `/chat` 返回 `confirmationRequired`，客户端再 `/confirm` 续跑 | `agent.py::pendingConfirms`、`server.py::handleConfirm()` |
| Router 有二次 guard | 即使 Agent 层漏检，Router 未传 `approvedDeletion=True` 也会拦截删除命令 | `tools/router.py::router.executeTool()` |
| 文件路径未限制在 workDir 内 | 相对路径基于 `workDir`；绝对路径会直接使用 | `tools/file.py::normalizePath()` |
| Bash 真实执行 shell | 使用 `bash -lc` 在 `workDir` 执行，非删除危险命令不会额外确认 | `tools/bash.py::executeBash()` |
| 模型循环上限 | 默认 8 轮，超过返回错误 | `core/agent.py::agent.__init__()`、`continueModelLoop()` |
| 日志脱敏是正则脱敏 | 覆盖常见 key/token/secret/password/Bearer/sk- 模式，不保证覆盖所有秘密格式 | `utils/jsonl.py::secretPatterns` |
| 当前无测试框架 | 项目使用 `manualChecks.py` 做框架外手动验证 | `manualChecks.py` |
| 模型配置优先级 | `loadModelConfig()` 优先读取 `config/models.yaml`；配置文件不存在时才读取 `FLAMINGO_AGENTS_*` 环境变量 | `models/registry.py::loadModelConfig()` |
