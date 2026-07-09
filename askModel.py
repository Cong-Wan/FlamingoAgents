'''
Author: wilbur
Version: 1.0
Date: 2026-07-03
Description: 导入 Flamingo，向大模型发起一次请求，让它阅读 docs/flamingoAgentsFlow.md 并打印模型的回复。
            v1.1 调大 maxModelSteps，避免大文件分多次读取时超过默认 8 步上限。
'''

from pathlib import Path

from flamingoAgents import createAgent


def main() -> None:
    projectDir = Path(__file__).resolve().parent
    flamingo = createAgent(projectDir)
    flamingo.maxModelSteps = 20

    prompt = '阅读 /Users/wilbur/project/FlamingoAgents/docs/addCallableToolFunction.md 这个文件'
    result = flamingo.runUserMessage(prompt)

    print('==== 会话状态 ====')
    print(f'sessionId: {result.sessionId}')
    print(f'status:    {result.status}')
    print('==== 模型回复 ====')
    print(result.message)


if __name__ == '__main__':
    main()
