'''
Author: wilbur
Version: 1.4
Date: 2026-07-26
Description: 导入 Flamingo，以事件流方式向大模型发起请求：正文/思维链逐字打印、工具事件可见、需确认工具走 input() 交互续跑（docs/streamOutputPlan.md §6.6 步骤 4 演示）。
            v1.4 从一次性 runResult 改为消费 runUserMessageStream / continueConfirmationStream 事件流。
'''

import argparse
from pathlib import Path

from flamingoAgents import (
    completedEvent,
    confirmationRequiredEvent,
    createAgent,
    errorEvent,
    reasoningDeltaEvent,
    textDeltaEvent,
    toolCallEndEvent,
    toolCallStartEvent,
)


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='以事件流方式向大模型发起请求。')
    parser.add_argument('--debug', action='store_true', default=True, help='开启诊断输出。')
    return parser.parse_args()


def consumeStream(stream) -> str | None:
    # 消费一条事件流：逐字打印正文/思维链、打印工具事件；遇终态事件返回 confirmationId（如需确认）。
    # 契约：中途退出必须 close()；此处迭代到终态自然结束，锁已释放（docs/streamOutputPlan.md §6.4）。
    try:
        for event in stream:
            if isinstance(event, reasoningDeltaEvent):
                print(f'\033[2m{event.text}\033[0m', end='', flush=True)
            elif isinstance(event, textDeltaEvent):
                print(event.text, end='', flush=True)
            elif isinstance(event, toolCallStartEvent):
                print(f'\n⏳ 调用工具 {event.toolCall.toolName}：{event.preview}')
            elif isinstance(event, toolCallEndEvent):
                status = '失败' if event.toolResult.isError else '完成'
                print(f'{"❌" if event.toolResult.isError else "✅"} 工具 {event.toolResult.toolName} {status}')
            elif isinstance(event, confirmationRequiredEvent):
                print(f'\n⚠️ 工具 {event.toolCall.toolName} 需要确认：{event.reason}')
                print(f'   预览：{event.commandPreview}')
                return event.confirmationId
            elif isinstance(event, completedEvent):
                print()
                return None
            elif isinstance(event, errorEvent):
                print(f'\n🚨 错误（{event.errorType}）：{event.message}')
                return None
    finally:
        stream.close()
    return None


def main() -> None:
    args = parseArgs()
    projectDir = Path(__file__).resolve().parent
    flamingo = createAgent(projectDir, providerId='volcano', debug=args.debug)
    flamingo.maxModelSteps = 20

    sessionId = flamingo.createSessionId()
    prompt = '阅读 @/Users/wilbur/project/FlamingoAgents/docs/addCallableToolFunction.md 这个文件并总结'
    confirmationId = consumeStream(flamingo.runUserMessageStream(prompt, sessionId))
    while confirmationId:
        answer = input('是否批准执行？[y/n] ').strip().lower()
        confirmationId = consumeStream(
            flamingo.continueConfirmationStream(sessionId, confirmationId, approved=(answer == 'y'))
        )


if __name__ == '__main__':
    main()
