'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Runs framework-free manual validation checks for the system tool chat agent.
'''

from __future__ import annotations

import argparse
import http.client
import json
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agentCore import agentCore
from agentTypes import chatMessage, toolCall, toolExecutionContext
from agentTypes import modelConfig
from bashTool import executeBash
from debugPrinter import debugPrinter
from fileTools import executeEdit, executeRead, executeWrite
from httpServer import makeHttpHandler
from jsonlLogger import jsonlLogger
from openaiAdapter import openaiCompatibleAdapter
from toolGuard import detectDeletionCommand
from toolRegistry import createDefaultToolRegistry


class fakeModelAdapter:
    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> chatMessage:
        lastMessage = messages[-1]
        if lastMessage.role == 'user' and 'read sample' in lastMessage.content:
            return chatMessage(
                role='assistant',
                content='',
                toolCalls=[toolCall(id='call_read_sample', toolName='read', arguments={'path': 'sample.txt'})],
            )
        if lastMessage.role == 'user' and 'delete sample' in lastMessage.content:
            return chatMessage(
                role='assistant',
                content='',
                toolCalls=[toolCall(id='call_delete_sample', toolName='bash', arguments={'command': 'rm sample.txt'})],
            )
        if lastMessage.role == 'user' and 'bash harmless' in lastMessage.content:
            return chatMessage(
                role='assistant',
                content='',
                toolCalls=[toolCall(id='call_bash_harmless', toolName='bash', arguments={'command': 'printf harmless', 'timeout': 5})],
            )
        if lastMessage.role == 'user' and 'curl fail' in lastMessage.content:
            return chatMessage(
                role='assistant',
                content='',
                toolCalls=[toolCall(id='call_curl_fail', toolName='bash', arguments={'command': 'curl -fsSL http://127.0.0.1:1', 'timeout': 5})],
            )
        if lastMessage.role == 'tool' and '命令已被用户拒绝' in lastMessage.content:
            return chatMessage(role='assistant', content='删除已被拒绝，文件没有被删除。')
        if lastMessage.role == 'tool' and 'alpha sample' in lastMessage.content:
            return chatMessage(role='assistant', content='sample content: alpha sample')
        if lastMessage.role == 'tool' and 'harmless' in lastMessage.content:
            return chatMessage(role='assistant', content='bash result: harmless')
        if lastMessage.role == 'tool' and lastMessage.name == 'bash':
            return chatMessage(role='assistant', content='查询失败，未继续尝试绕过。')
        return chatMessage(role='assistant', content='普通对话完成。')


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def printPass(name: str) -> None:
    print(f'PASS {name}')


def runFileToolCheck(debugEnabled: bool) -> None:
    with tempfile.TemporaryDirectory() as tempDir:
        context = toolExecutionContext(workDir=Path(tempDir), debugPrinter=debugPrinter(debugEnabled))
        writeResult = executeWrite({'path': 'sample.txt', 'content': 'alpha sample\nbeta sample\n'}, context)
        expect(not writeResult.isError, writeResult.content)
        readResult = executeRead({'path': 'sample.txt', 'offset': 1, 'limit': 1}, context)
        expect('alpha sample' in readResult.content, readResult.content)
        editResult = executeEdit({
            'path': 'sample.txt',
            'edits': [{'oldText': 'beta sample', 'newText': 'gamma sample'}],
        }, context)
        expect(not editResult.isError, editResult.content)
        expect('gamma sample' in (Path(tempDir) / 'sample.txt').read_text(encoding='utf-8'), 'edit 未写入新内容')
    printPass('file tools')


def runBashCheck(debugEnabled: bool) -> None:
    with tempfile.TemporaryDirectory() as tempDir:
        context = toolExecutionContext(workDir=Path(tempDir), debugPrinter=debugPrinter(debugEnabled))
        okResult = executeBash({'command': 'printf hello', 'timeout': 5}, context)
        expect(not okResult.isError and 'hello' in okResult.content, okResult.content)
        timeoutResult = executeBash({'command': 'sleep 2', 'timeout': 1}, context)
        expect(timeoutResult.isError and timeoutResult.details.get('timeoutExpired') is True, timeoutResult.content)
    printPass('bash')


def runGuardCheck() -> None:
    deleteCommands = [
        'rm file',
        'rm -rf folder',
        'rmdir folder',
        'unlink file',
        'find . -delete',
        'python -c "import os; os.remove(\'file\')"',
        'python -c "import shutil; shutil.rmtree(\'folder\')"',
    ]
    for command in deleteCommands:
        expect(detectDeletionCommand(command), f'未识别删除命令：{command}')
    expect(not detectDeletionCommand('grep -R "keyword" .'), 'grep 被误判为删除命令')
    printPass('deletion guard')


def runLoggerCheck() -> None:
    with tempfile.TemporaryDirectory() as tempDir:
        logPath = Path(tempDir) / 'agent.jsonl'
        logger = jsonlLogger(logPath)
        logger.logEvent({'type': 'sample', 'token': 'sk-12345678901234567890', 'content': 'x' * 4100})
        logText = logPath.read_text(encoding='utf-8')
        expect('<redacted>' in logText, 'secret 未脱敏')
        expect('12345678901234567890' not in logText, 'secret 原文泄露')
    printPass('jsonl logger')


def runAdapterParseCheck() -> None:
    adapter = openaiCompatibleAdapter(modelConfig(
        provider='openaiCompatible',
        model='manual-check-model',
        baseUrl='http://127.0.0.1:9/v1',
        apiKeyEnv='OPENAI_API_KEY',
        apiType='openaiCompatible',
    ))
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
    printPass('openai adapter parse')


def buildFakeAgent(workDir: Path, debugEnabled: bool) -> agentCore:
    return agentCore(
        modelAdapter=fakeModelAdapter(),
        registry=createDefaultToolRegistry(),
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugPrinter=debugPrinter(debugEnabled),
        confirmDeletion=None,
    )


def runAgentCheck(debugEnabled: bool) -> None:
    with tempfile.TemporaryDirectory() as tempDir:
        workDir = Path(tempDir)
        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        agent = buildFakeAgent(workDir, debugEnabled)
        readResult = agent.runUserMessage('please read sample', sessionId='manualAgent')
        expect(readResult.status == 'completed', readResult.message)
        expect('alpha sample' in readResult.message, readResult.message)
        confirmResult = agent.runUserMessage('please delete sample', sessionId='manualAgent')
        expect(confirmResult.status == 'confirmationRequired', confirmResult.message)
        rejectResult = agent.continueConfirmation('manualAgent', confirmResult.confirmationId or '', approved=False)
        expect(rejectResult.status == 'completed', rejectResult.message)
        expect((workDir / 'sample.txt').exists(), '拒绝删除后文件不应消失')
        curlResult = agent.runUserMessage('please curl fail', sessionId='manualCurl')
        expect(curlResult.status == 'completed', curlResult.message)
        expect('查询失败' in curlResult.message, curlResult.message)
    printPass('agent core')


def runHttpCheck(debugEnabled: bool) -> None:
    with tempfile.TemporaryDirectory() as tempDir:
        workDir = Path(tempDir)
        (workDir / 'sample.txt').write_text('alpha sample\n', encoding='utf-8')
        agent = buildFakeAgent(workDir, debugEnabled)
        server = ThreadingHTTPServer(('127.0.0.1', 0), makeHttpHandler(agent))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            connection = http.client.HTTPConnection(host, port, timeout=5)
            connection.request('POST', '/chat', body=json.dumps({
                'sessionId': 'httpManual',
                'message': 'please delete sample',
            }), headers={'Content-Type': 'application/json'})
            response = connection.getresponse()
            payload = json.loads(response.read().decode('utf-8'))
            expect(payload['status'] == 'confirmationRequired', json.dumps(payload, ensure_ascii=False))
            connection.request('POST', '/confirm', body=json.dumps({
                'sessionId': 'httpManual',
                'confirmationId': payload['confirmationId'],
                'approved': False,
            }), headers={'Content-Type': 'application/json'})
            confirmResponse = connection.getresponse()
            confirmPayload = json.loads(confirmResponse.read().decode('utf-8'))
            expect(confirmPayload['status'] == 'completed', json.dumps(confirmPayload, ensure_ascii=False))
            expect((workDir / 'sample.txt').exists(), 'HTTP 拒绝删除后文件不应消失')
        finally:
            server.shutdown()
            server.server_close()
    printPass('http')


def main() -> None:
    parser = argparse.ArgumentParser(description='运行无测试框架的手动验证')
    parser.add_argument('check', choices=['all', 'fileTools', 'bash', 'guard', 'logger', 'adapter', 'agent', 'http'])
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    args = parser.parse_args()

    if args.check in {'all', 'fileTools'}:
        runFileToolCheck(args.debug)
    if args.check in {'all', 'bash'}:
        runBashCheck(args.debug)
    if args.check in {'all', 'guard'}:
        runGuardCheck()
    if args.check in {'all', 'logger'}:
        runLoggerCheck()
    if args.check in {'all', 'adapter'}:
        runAdapterParseCheck()
    if args.check in {'all', 'agent'}:
        runAgentCheck(args.debug)
    if args.check in {'all', 'http'}:
        runHttpCheck(args.debug)


if __name__ == '__main__':
    main()
