'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Provides the command-line chat entrypoint backed by the shared agentCore.
'''

from __future__ import annotations

import argparse
from pathlib import Path

from agentCore import agentCore
from agentTypes import toolCall
from debugPrinter import debugPrinter
from modelRegistry import loadModelConfigFromEnv
from openaiAdapter import openaiCompatibleAdapter
from toolRegistry import createDefaultToolRegistry


def askDeletionConfirmation(call: toolCall, reason: str) -> bool:
    command = str(call.arguments.get('command', ''))
    print('Agent 想执行一个删除相关命令：')
    print(command)
    print(f'原因：{reason}')
    answer = input('是否允许？[y/N] ').strip().lower()
    return answer in {'y', 'yes'}


def buildAgent(debugEnabled: bool, workDir: Path) -> agentCore:
    printer = debugPrinter(debugEnabled)
    config = loadModelConfigFromEnv()
    adapter = openaiCompatibleAdapter(config, printer)
    registry = createDefaultToolRegistry()
    return agentCore(
        modelAdapter=adapter,
        registry=registry,
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugPrinter=printer,
        confirmDeletion=askDeletionConfirmation,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='本地系统工具对话 Agent CLI')
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    parser.add_argument('--session-id', default='cliSession', help='CLI 会话 ID')
    parser.add_argument('--work-dir', default='.', help='工具执行工作目录')
    args = parser.parse_args()

    workDir = Path(args.work_dir).resolve()
    agent = buildAgent(args.debug, workDir)
    sessionId = args.session_id
    print('系统工具对话 Agent 已启动。输入 /help 查看命令，输入 /exit 退出。')
    while True:
        userInput = input('你> ').strip()
        if not userInput:
            continue
        if userInput == '/exit':
            print('已退出。')
            return
        if userInput == '/help':
            print('/exit 退出；/help 查看帮助；其他输入会发送给 Agent。')
            continue
        result = agent.runUserMessage(userInput, sessionId=sessionId)
        if result.status == 'completed':
            print(f'Agent> {result.message}')
        elif result.status == 'error':
            print(f'Agent 错误> {result.message}')
        else:
            print(f'Agent 需要确认但 CLI 已配置交互确认，当前状态异常：{result.reason}')


if __name__ == '__main__':
    main()
