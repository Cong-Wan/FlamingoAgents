'''
Author: wilbur
Version: 1.2
Date: 2026-07-01
Description: Provides the command-line chat entrypoint for Flamingo Agents with unified model config loading.
'''

from __future__ import annotations

import argparse
from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import toolCall
from flamingoAgents.utils.debug import debugConsole
from flamingoAgents.models.registry import loadModelConfig
from flamingoAgents.models.openai import openaiAdapter
from flamingoAgents.tools.registry import createDefaultRegistry


def askDeletionConfirmation(call: toolCall, reason: str) -> bool:
    command = str(call.arguments.get('command', ''))
    print('Agent 想执行一个删除相关命令：')
    print(command)
    print(f'原因：{reason}')
    answer = input('是否允许？[y/N] ').strip().lower()
    return answer in {'y', 'yes'}


def buildAgent(debugEnabled: bool, workDir: Path) -> agent:
    printer = debugConsole(debugEnabled)
    config = loadModelConfig()
    adapter = openaiAdapter(config, printer)
    registry = createDefaultRegistry()
    return agent(
        modelAdapter=adapter,
        registry=registry,
        workDir=workDir,
        logDir=workDir / '.agentLogs',
        debugConsole=printer,
        confirmDeletion=askDeletionConfirmation,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description='Flamingo Agents CLI')
    parser.add_argument('--debug', action='store_true', help='启用详细调试输出')
    parser.add_argument('--session-id', default='cliSession', help='CLI 会话 ID')
    parser.add_argument('--work-dir', default='.', help='工具执行工作目录')
    args = parser.parse_args()

    workDir = Path(args.work_dir).resolve()
    agent = buildAgent(args.debug, workDir)
    sessionId = args.session_id
    printer = agent.debugConsole
    printer.debug(f'CLI 启动 workDir={workDir} sessionId={sessionId}')
    print('Flamingo Agents 已启动。输入 /help 查看命令，输入 /exit 退出。')
    while True:
        userInput = input('你> ').strip()
        if not userInput:
            continue
        if userInput == '/exit':
            print('已退出。')
            return
        if userInput == '/help':
            print('/exit 退出；/help 查看帮助；其他输入会发送给 Flamingo Agents。')
            continue
        if args.debug:
            print(f'[debug input] sessionId={sessionId} chars={len(userInput)}', flush=True)
        result = agent.runUserMessage(userInput, sessionId=sessionId)
        if result.status == 'completed':
            print(f'Agent> {result.message}')
        elif result.status == 'error':
            print(f'Agent 错误> {result.message}')
        else:
            print(f'Agent 需要确认但 CLI 已配置交互确认，当前状态异常：{result.reason}')


if __name__ == '__main__':
    main()
