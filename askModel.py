'''
Author: wilbur
Version: 1.3
Date: 2026-07-25
Description: 导入 Flamingo，向大模型发起一次请求，让它阅读 docs/flamingoAgentsFlow.md 并打印模型的回复。
            v1.1 调大 maxModelSteps，避免大文件分多次读取时超过默认 8 步上限。
            v1.2 解析 --debug 并传入 createAgent，使诊断输出真正生效。
            v1.3 指定 providerId='glm'，并改为带 sessionId 的两轮对话，验证会话恢复。
'''

import argparse
from pathlib import Path

from flamingoAgents import createAgent


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='向大模型发起一次请求并打印回复。')
    parser.add_argument('--debug', action='store_true', default=True, help='开启诊断输出。')
    return parser.parse_args()


def main() -> None:
    args = parseArgs()
    projectDir = Path(__file__).resolve().parent
    flamingo = createAgent(projectDir, providerId='glm', debug=args.debug)
    flamingo.maxModelSteps = 20

################## loop 1 ################## 
    # prompt = '阅读 @/Users/wilbur/project/FlamingoAgents/docs/addCallableToolFunction.md 这个文件'
    # result = flamingo.runUserMessage(prompt, sessionId="test111")

################## loop 2 ################## 
    prompt = '你认为这份文档写的合理吗？'
    result = flamingo.runUserMessage(prompt, sessionId="test111")

if __name__ == '__main__':
    main()
