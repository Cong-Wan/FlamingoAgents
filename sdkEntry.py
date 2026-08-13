'''
Author: wilbur
Version: 1.5
Date: 2026-08-13
Description: SDK 入口：可编程调用 runSdk() 或 CLI 单独运行，传入 provider/model、systemPrompt、userPrompt、validTools、workDir；
             事件流逐字打印并返回完整正文；需确认工具直接拒绝（非交互）；validTools 不传则不挂任何工具。
             v1.1 新增：venv 自举（相对脚本位置定位 .venv/bin/python 重执行，跨机器迁移零配置）；
             --json 机器友好输出（stdout 仅一行 JSON，思维链/工具事件挪到 stderr），供子代理 function call 解析。
             v1.2 新增：--system 智能识别文件路径（传入存在的 .md 等文件路径则读取内容作为系统提示词，否则按纯文本）。
             v1.3 变更：--system 默认读取 config/systemPrompt.md，除非显式传入纯文本或其它文件路径。
             v1.4 变更：--model 帮助示例改为 kimi/k3。
             v1.5 变更：maxModelSteps 改为 -1，子代理不再被 20 步硬截断。
'''

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import flamingoAgents  # noqa: F401
except ModuleNotFoundError:
    # 自举：环境不对时，用项目 .venv 的解释器重执行本脚本（路径相对 __file__，不写死）。
    venvPython = Path(__file__).resolve().parent / '.venv' / 'bin' / 'python'
    if venvPython.exists() and Path(sys.executable).resolve() != venvPython.resolve():
        os.execv(str(venvPython), [str(venvPython), str(Path(__file__).resolve()), *sys.argv[1:]])
    raise

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


def consumeStream(stream, chunks: list[str], out=None) -> str | None:
    # 消费事件流：逐字打印正文/思维链、打印工具事件（输出到 out，默认 stdout），正文同时收集进 chunks；
    # 遇终态事件返回 confirmationId（如需确认）。
    if out is None:
        out = sys.stdout
    try:
        for event in stream:
            if isinstance(event, reasoningDeltaEvent):
                print(f'\033[2m{event.text}\033[0m', end='', flush=True, file=out)
            elif isinstance(event, textDeltaEvent):
                print(event.text, end='', flush=True, file=out)
                chunks.append(event.text)
            elif isinstance(event, toolCallStartEvent):
                print(f'\n⏳ 调用工具 {event.toolCall.toolName}：{event.preview}', file=out)
            elif isinstance(event, toolCallEndEvent):
                status = '失败' if event.toolResult.isError else '完成'
                print(f'{"❌" if event.toolResult.isError else "✅"} 工具 {event.toolResult.toolName} {status}', file=out)
            elif isinstance(event, confirmationRequiredEvent):
                print(f'\n⚠️ 工具 {event.toolCall.toolName} 需要确认，SDK 模式自动拒绝：{event.reason}', file=out)
                return event.confirmationId
            elif isinstance(event, completedEvent):
                print(file=out)
                return None
            elif isinstance(event, errorEvent):
                print(f'\n🚨 错误（{event.errorType}）：{event.message}', file=out)
                return None
    finally:
        stream.close()
    return None


projectDir = Path(__file__).resolve().parent
defaultSystemPromptPath = projectDir / 'config' / 'systemPrompt.md'


def resolveSystemPrompt(systemPrompt: str | None) -> str:
    # --system 智能识别：未传则读默认 config/systemPrompt.md；传入存在的文件路径则读取内容；否则按纯文本返回。
    if systemPrompt is None:
        return defaultSystemPromptPath.read_text(encoding='utf-8')
    candidate = Path(systemPrompt).expanduser()
    if candidate.is_file():
        return candidate.read_text(encoding='utf-8')
    return systemPrompt


def runSdk(
    providerModel: str,
    userPrompt: str,
    systemPrompt: str | None = None,
    validTools: list[str] | None = None,
    workDir: str | Path | None = None,
    debug: bool = False,
    quiet: bool = False,
) -> str:
    # providerModel 格式：provider/model，如 volcano/deepseek-v4-flash；返回模型完整正文。
    providerId, _, modelId = providerModel.partition('/')
    if not providerId or not modelId:
        raise ValueError(f'providerModel 格式应为 provider/model，实际：{providerModel}')
    resolvedWorkDir = Path(workDir).resolve() if workDir else Path(__file__).resolve().parent
    flamingo = createAgent(
        resolvedWorkDir,
        providerId=providerId,
        modelId=modelId,
        systemPrompt=resolveSystemPrompt(systemPrompt),
        toolNames=validTools if validTools else [],
        debug=debug,
    )
    flamingo.maxModelSteps = -1

    out = sys.stderr if quiet else sys.stdout
    sessionId = flamingo.createSessionId()
    chunks: list[str] = []
    confirmationId = consumeStream(flamingo.runUserMessageStream(userPrompt, sessionId), chunks, out)
    while confirmationId:
        confirmationId = consumeStream(
            flamingo.continueConfirmationStream(sessionId, confirmationId, approved=False),
            chunks,
            out,
        )
    return ''.join(chunks)


def parseArgs() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='FlamingoAgents SDK 入口。')
    parser.add_argument('--model', required=True, help='provider/model，如 kimi/k3')
    parser.add_argument('--system', default=None, help='系统提示词：纯文本，或存在的文件路径（如 .md 文件）则读取其内容；不传则默认 config/systemPrompt.md')
    parser.add_argument('--prompt', required=True, help='用户提示词')
    parser.add_argument('--tools', default='', help='逗号分隔的工具白名单，不传则不挂工具')
    parser.add_argument('--work-dir', default=None, help='工作目录，默认项目根目录')
    parser.add_argument('--debug', action='store_true', help='开启诊断输出')
    parser.add_argument('--json', action='store_true', help='机器友好输出：stdout 仅一行 JSON，事件流挪到 stderr')
    return parser.parse_args()


def main() -> None:
    args = parseArgs()
    validTools = [name.strip() for name in args.tools.split(',') if name.strip()]
    if not args.json:
        runSdk(args.model, args.prompt, systemPrompt=args.system, validTools=validTools, workDir=args.work_dir, debug=args.debug)
        return
    try:
        reply = runSdk(args.model, args.prompt, systemPrompt=args.system, validTools=validTools, workDir=args.work_dir, debug=args.debug, quiet=True)
        print(json.dumps({'reply': reply, 'error': None}, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'reply': None, 'error': str(exc)}, ensure_ascii=False))
        sys.exit(1)


if __name__ == '__main__':
    main()
