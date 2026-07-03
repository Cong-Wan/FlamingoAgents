'''
Author: wilbur
Version: 2.0
Date: 2026-07-02
Description: Framework-free manual validation entrypoint for the pure-library Flamingo Agents runtime, with --debug controlled output.
'''

from __future__ import annotations

import argparse
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import chatMessage, toolCall, toolContext
from flamingoAgents.models.chatCompletions import chatCompletionsAdapter, modelCompletion
from flamingoAgents.models.modelAuth import createModelAuth
from flamingoAgents.models.modelConfig import loadModelConfigFromYaml, modelConfig
from flamingoAgents.tools.toolConfig import loadToolConfig
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRuntime import executeTool
from flamingoAgents.tools.toolSchema import buildModelTools
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.utils.jsonl import jsonlLog


class fakeModel:
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> modelCompletion:
        last = messages[-1]
        if last.role == 'user':
            if 'read sample' in last.content:
                return modelCompletion(
                    message=chatMessage(role='assistant', content='', toolCalls=[toolCall('call_read', 'read', {'path': 'sample.txt'})]),
                    requestPayload={},
                    responsePayload={},
                )
            if 'delete sample' in last.content:
                return modelCompletion(
                    message=chatMessage(role='assistant', content='', toolCalls=[toolCall('call_delete', 'bash', {'command': 'rm sample.txt'})]),
                    requestPayload={},
                    responsePayload={},
                )
            if 'batch' in last.content:
                return modelCompletion(
                    message=chatMessage(role='assistant', content='', toolCalls=[
                        toolCall('c1', 'read', {'path': 'sample.txt'}),
                        toolCall('c2', 'bash', {'command': 'rm sample.txt'}),
                        toolCall('c3', 'read', {'path': 'sample.txt'}),
                    ]),
                    requestPayload={},
                    responsePayload={},
                )
        if last.role == 'tool' and 'alpha sample' in last.content:
            return modelCompletion(
                message=chatMessage(role='assistant', content='sample content: alpha sample'),
                requestPayload={},
                responsePayload={},
            )
        return modelCompletion(message=chatMessage(role='assistant', content='done'), requestPayload={}, responsePayload={})


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def printPass(name: str) -> None:
    print(f'PASS {name}')


def printDebug(debugEnabled: bool, message: str) -> None:
    if debugEnabled:
        print(f'[manual debug] {message}', flush=True)


def byName(definitions, name):
    return next(definition for definition in definitions if definition.name == name)


def runToolConfigCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 tool config 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadToolConfig(debugConsole=printer)
    expect({definition.name for definition in definitions} == {'read', 'write', 'edit', 'bash'}, '工具名集合不正确')
    modelTools = buildModelTools(definitions)
    expect(modelTools[0]['type'] == 'function', '模型工具 schema 包装不正确')
    expect(all('permissions' not in tool['function'] for tool in modelTools), '模型 schema 泄漏了 permissions')
    printPass('tool config')


def runPermissionCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 permission policy 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadToolConfig(debugConsole=printer)
    bashDefinition = byName(definitions, 'bash')
    readDefinition = byName(definitions, 'read')
    expect(evaluateToolCall(bashDefinition, toolCall('a', 'bash', {'command': 'rm file'}), debugConsole=printer).requiresApproval is True, 'rm 未触发确认')
    expect(evaluateToolCall(bashDefinition, toolCall('b', 'bash', {'command': 'grep keyword file'}), debugConsole=printer).requiresApproval is False, 'grep 被误判')
    expect(evaluateToolCall(bashDefinition, toolCall('c', 'bash', {'command': 'find . -delete'}), debugConsole=printer).requiresApproval is True, 'find -delete 未触发确认')
    expect(evaluateToolCall(readDefinition, toolCall('d', 'read', {'path': 'sample.txt'}), debugConsole=printer).requiresApproval is False, 'read 不应触发确认')
    printPass('permission policy')


def runToolRuntimeCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 tool runtime 检查')
    printer = debugConsole(debugEnabled)
    definitions = loadToolConfig(debugConsole=printer)
    with TemporaryDirectory() as tempDir:
        context = toolContext(workDir=Path(tempDir), debugConsole=printer)
        writeDefinition = byName(definitions, 'write')
        readDefinition = byName(definitions, 'read')
        editDefinition = byName(definitions, 'edit')
        bashDefinition = byName(definitions, 'bash')

        writeResult = executeTool(writeDefinition, {'path': 'sample.txt', 'content': 'alpha\nbeta\n'}, context, 'call_write')
        expect(not writeResult.isError, writeResult.content)
        readResult = executeTool(readDefinition, {'path': 'sample.txt', 'offset': 1, 'limit': 1}, context, 'call_read')
        expect('alpha' in readResult.content, readResult.content)
        editResult = executeTool(editDefinition, {'path': 'sample.txt', 'edits': [{'oldText': 'beta', 'newText': 'gamma'}]}, context, 'call_edit')
        expect(not editResult.isError, editResult.content)

        for escapePath in ['../outside.txt', '/tmp/outside.txt', '~/secret.txt']:
            escapeResult = executeTool(readDefinition, {'path': escapePath}, context, 'call_escape')
            expect(escapeResult.isError, f'路径逃逸没有被阻止：{escapePath}')

        bashResult = executeTool(bashDefinition, {'command': 'printf hello', 'timeout': 5}, context, 'call_bash')
        expect(not bashResult.isError and 'hello' in bashResult.content, bashResult.content)
        failResult = executeTool(bashDefinition, {'command': 'exit 7', 'timeout': 5}, context, 'call_fail')
        expect(failResult.isError and failResult.details.get('exitCode') == 7, '非零退出码未被标记为错误')
        timeoutResult = executeTool(bashDefinition, {'command': 'sleep 2', 'timeout': 1}, context, 'call_timeout')
        expect(timeoutResult.isError and timeoutResult.details.get('timeoutExpired') is True, '超时未被捕获')
        clampedResult = executeTool(bashDefinition, {'command': 'printf clamp', 'timeout': 999}, context, 'call_clamp')
        expect(not clampedResult.isError and clampedResult.details.get('timeout') == 120, 'timeout 未被限制到 120')
    printPass('tool runtime')


def runLoggerCheck() -> None:
    with TemporaryDirectory() as tempDir:
        logPath = Path(tempDir) / 'agent.jsonl'
        logger = jsonlLog(logPath)
        logger.logEvent({'type': 'sample', 'token': 'sk-12345678901234567890', 'content': 'x' * 4100})
        logText = logPath.read_text(encoding='utf-8')
        expect('<redacted>' in logText, 'secret 未脱敏')
        expect('12345678901234567890' not in logText, 'secret 原文泄露')
    printPass('jsonl logger')


def runAdapterParseCheck() -> None:
    adapter = chatCompletionsAdapter(
        modelConfig('manual-provider', 'manual-model', 'http://127.0.0.1:9/v1', 'openaiCompatible'),
        createModelAuth('manual-key'),
    )
    parsed = adapter.parseAssistantPayload({
        'choices': [{
            'message': {
                'role': 'assistant',
                'content': '',
                'tool_calls': [{
                    'id': 'call_1',
                    'type': 'function',
                    'function': {'name': 'read', 'arguments': '{"path":"sample.txt"}'},
                }],
            },
        }],
    })
    expect(parsed.toolCalls[0].toolName == 'read', 'tool_call name 解析失败')
    expect(parsed.toolCalls[0].arguments['path'] == 'sample.txt', 'tool_call arguments 解析失败')
    for badArguments in ['[]', '"abc"', '{bad json']:
        try:
            adapter.parseAssistantPayload({
                'choices': [{
                    'message': {
                        'role': 'assistant',
                        'content': '',
                        'tool_calls': [{
                            'id': 'call_bad',
                            'type': 'function',
                            'function': {'name': 'read', 'arguments': badArguments},
                        }],
                    },
                }],
            })
            raise RuntimeError('非法 arguments 没有被拒绝')
        except RuntimeError:
            pass
    printPass('adapter parse')


def runModelAuthCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 model config / auth 检查')
    with TemporaryDirectory() as tempDir:
        inlinePath = Path(tempDir) / 'inline.yaml'
        inlinePath.write_text(
            'providers:\n'
            '  "abc":\n'
            '    baseUrl: http://127.0.0.1:9/v1\n'
            '    api: openai-completions\n'
            '    apiKey: inline-key\n'
            '    models:\n'
            '      - id: model-a\n',
            encoding='utf-8')
        before = os.environ.get('FLAMINGO_AGENTS_ABC_API_KEY')
        resolved = loadModelConfigFromYaml(providerId='abc', configPath=inlinePath, debugConsole=debugConsole(debugEnabled))
        after = os.environ.get('FLAMINGO_AGENTS_ABC_API_KEY')
        expect(resolved.apiKey == 'inline-key', 'inline apiKey 解析失败')
        expect(before == after, '配置加载不应写 os.environ')

        os.environ['TEST_API_KEY'] = 'env-secret'
        envPath = Path(tempDir) / 'env.yaml'
        envPath.write_text(
            'providers:\n'
            '  "envp":\n'
            '    baseUrl: http://127.0.0.1:9/v1\n'
            '    api: openai-completions\n'
            '    apiKey: ${TEST_API_KEY}\n'
            '    models:\n'
            '      - id: model-b\n',
            encoding='utf-8')
        envResolved = loadModelConfigFromYaml(providerId='envp', configPath=envPath)
        expect(envResolved.apiKey == 'env-secret', '${ENV} apiKey 解析失败')

        missingPath = Path(tempDir) / 'missing.yaml'
        missingPath.write_text(
            'providers:\n'
            '  "missp":\n'
            '    baseUrl: http://127.0.0.1:9/v1\n'
            '    api: openai-completions\n'
            '    apiKey: ${MISSING_KEY_NOT_SET}\n'
            '    models:\n'
            '      - id: model-c\n',
            encoding='utf-8')
        try:
            loadModelConfigFromYaml(providerId='missp', configPath=missingPath)
            raise RuntimeError('缺失环境变量没有被拒绝')
        except RuntimeError:
            pass

    auth = createModelAuth('abc123')
    expect(auth.authorizationHeader == 'Bearer abc123', 'Authorization header 生成失败')
    sourceText = Path('flamingoAgents/models/chatCompletions.py').read_text(encoding='utf-8')
    expect('os.getenv' not in sourceText, 'adapter 不应包含 os.getenv')
    expect('jsonlLog' not in sourceText, 'adapter 不应依赖 jsonlLog')
    printPass('model config auth adapter')


def buildFakeAgent(workDir: Path, debugEnabled: bool) -> agent:
    return agent(
        modelAdapter=fakeModel(),
        toolDefinitions=loadToolConfig(debugConsole=debugConsole(debugEnabled)),
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugConsole=debugConsole(debugEnabled),
    )


def runAgentStateCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始 agent 状态机检查')
    with TemporaryDirectory() as tempDir:
        workDir = Path(tempDir)
        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        testAgent = buildFakeAgent(workDir, debugEnabled)

        readResult = testAgent.runUserMessage('please read sample', sessionId='readSession')
        expect(readResult.status == 'completed', readResult.message)
        expect('alpha sample' in readResult.message, readResult.message)

        confirmResult = testAgent.runUserMessage('please delete sample', sessionId='deleteSession')
        expect(confirmResult.status == 'confirmationRequired', confirmResult.message)
        expect((workDir / 'sample.txt').exists(), '需要确认时不应执行 rm')

        rejectResult = testAgent.continueConfirmation('deleteSession', confirmResult.confirmationId or '', approved=False)
        expect(rejectResult.status == 'completed', rejectResult.message)
        expect((workDir / 'sample.txt').exists(), '拒绝删除后文件不应消失')

        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        batchResult = testAgent.runUserMessage('batch', sessionId='batchSession')
        expect(batchResult.status == 'confirmationRequired', batchResult.message)

        pendingNewMessage = testAgent.runUserMessage('again', sessionId='batchSession')
        expect(pendingNewMessage.status == 'error', 'pending 期间新消息应被拒绝')

        wrongSession = testAgent.continueConfirmation('wrongSession', batchResult.confirmationId or '', approved=True)
        expect(wrongSession.status == 'error', '错误 sessionId 不应消费 pending')
        expect(testAgent.hasPendingConfirmation('batchSession'), '错误 sessionId 不应清掉真实 pending')

        approvedBatch = testAgent.continueConfirmation('batchSession', batchResult.confirmationId or '', approved=True)
        expect(approvedBatch.status == 'completed', approvedBatch.message)
    printPass('agent state machine')


def runPureLibraryApiCheck(debugEnabled: bool) -> None:
    printDebug(debugEnabled, '开始纯库 API 检查')
    from flamingoAgents import createAgent

    builtAgent = createAgent(Path('.'), debug=debugEnabled)
    expect(type(builtAgent).__name__ == 'agent', 'createAgent 未返回 agent')
    expect(not Path('flamingoAgents/app').exists(), 'app 目录仍然存在')
    pyproject = Path('pyproject.toml').read_text(encoding='utf-8')
    expect('[project.scripts]' not in pyproject, 'pyproject 仍包含命令入口')
    manualSource = Path('manualChecks.py').read_text(encoding='utf-8')
    appLayerNeedle = 'flamingoAgents' + '.app'
    expect(appLayerNeedle not in manualSource, 'manualChecks 仍依赖 app 层')
    printPass('pure library api')


def main() -> None:
    parser = argparse.ArgumentParser(description='运行无测试框架的手动验证')
    parser.add_argument('check', choices=[
        'all', 'toolConfig', 'permission', 'runtime', 'logger', 'adapter', 'modelAuth', 'agent', 'pureLibrary',
    ])
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    args = parser.parse_args()

    if args.check in {'all', 'toolConfig'}:
        runToolConfigCheck(args.debug)
    if args.check in {'all', 'permission'}:
        runPermissionCheck(args.debug)
    if args.check in {'all', 'runtime'}:
        runToolRuntimeCheck(args.debug)
    if args.check in {'all', 'logger'}:
        runLoggerCheck()
    if args.check in {'all', 'adapter'}:
        runAdapterParseCheck()
    if args.check in {'all', 'modelAuth'}:
        runModelAuthCheck(args.debug)
    if args.check in {'all', 'agent'}:
        runAgentStateCheck(args.debug)
    if args.check in {'all', 'pureLibrary'}:
        runPureLibraryApiCheck(args.debug)


if __name__ == '__main__':
    main()
