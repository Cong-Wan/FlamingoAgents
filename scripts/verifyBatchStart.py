'''
Author: wilbur
Version: 1.0
Date: 2026-08-11
Description: streamingLatencyFixPlan T3.4 验证：driveToolBatch 可执行前缀批量 Start 的事件序表 #1-#8。
             用 stub 工具（free / confirm / unknown）直接驱动 agent.driveToolBatch，
             断言 SSE 事件顺序与契约 §6.2 红线（需确认不发 Start、拒绝仅 End、未知 Start+合成 error End）。
'''

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from flamingoAgents.core.agent import agent
from flamingoAgents.core.types import (
    confirmationRequiredEvent,
    toolCall,
    toolCallEndEvent,
    toolCallStartEvent,
    toolOutput,
)
from flamingoAgents.tools.toolDefinition import defineTool, permissionRule


def buildAgent(logDir: Path) -> agent:
    freeTool = defineTool(
        name='freeTool',
        description='免确认工具',
        parameters={'type': 'object', 'properties': {}},
        execute=lambda args, context: toolOutput(content='ok'),
    )
    confirmTool = defineTool(
        name='confirmTool',
        description='需确认工具',
        parameters={'type': 'object', 'properties': {'command': {'type': 'string'}}},
        execute=lambda args, context: toolOutput(content='ok'),
        permissions=[permissionRule(
            id='alwaysConfirm',
            field='command',
            action='requireApproval',
            reason='危险命令',
            patterns=[re.compile(r'.')],
        )],
    )
    # modelAdapter 本验证用不到（只测 driveToolBatch），传 None
    return agent(
        modelAdapter=None,
        toolDefinitions=[freeTool, confirmTool],
        workDir=logDir,
        logDir=logDir,
        systemPrompt='test',
    )


def summarize(events) -> list[str]:
    summary = []
    for event in events:
        if isinstance(event, toolCallStartEvent):
            summary.append(f'S:{event.toolCall.toolName}#{event.toolCall.id}')
        elif isinstance(event, toolCallEndEvent):
            summary.append(f'E:{event.toolResult.toolName}#{event.toolResult.toolCallId}')
        elif isinstance(event, confirmationRequiredEvent):
            summary.append(f'C:{event.toolCall.toolName}#{event.toolCall.id}')
        else:
            summary.append(f'?:{type(event).__name__}')
    return summary


def runCase(name: str, agentInstance: agent, sessionId: str, calls: list[toolCall], expected: list[str], expectTerminated: bool):
    gen = agentInstance.driveToolBatch(sessionId, calls, 0)
    collected = []
    while True:
        try:
            collected.append(next(gen))
        except StopIteration as stop:
            terminated = stop.value
            break
    actual = summarize(collected)
    assert actual == expected, f'{name} 事件序不符\n  期望: {expected}\n  实际: {actual}'
    assert terminated == expectTerminated, f'{name} 终止标记不符：期望 {expectTerminated} 实际 {terminated}'
    print(f'{name} 通过：{actual} terminated={terminated}')


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        logDir = Path(tmp)
        agentInstance = buildAgent(logDir)

        free1 = toolCall(id='c1', toolName='freeTool', arguments={})
        free2 = toolCall(id='c2', toolName='freeTool', arguments={})
        free3 = toolCall(id='c3', toolName='freeTool', arguments={})
        confirm1 = toolCall(id='c4', toolName='confirmTool', arguments={'command': 'rm -rf x'})
        unknown1 = toolCall(id='c5', toolName='ghostTool', arguments={'a': 1})

        runCase('#1 [free]', agentInstance, 's1', [free1],
                ['S:freeTool#c1', 'E:freeTool#c1'], False)
        runCase('#2 [free, free]', agentInstance, 's2', [free1, free2],
                ['S:freeTool#c1', 'S:freeTool#c2', 'E:freeTool#c1', 'E:freeTool#c2'], False)
        runCase('#3 [free, needConfirm]', agentInstance, 's3', [free1, confirm1],
                ['S:freeTool#c1', 'E:freeTool#c1', 'C:confirmTool#c4'], True)
        runCase('#4 [needConfirm, free]', agentInstance, 's4', [confirm1, free1],
                ['C:confirmTool#c4'], True)
        runCase('#5 [free, unknown, free]', agentInstance, 's5', [free1, unknown1, free2],
                ['S:freeTool#c1', 'S:ghostTool#c5', 'S:freeTool#c2',
                 'E:freeTool#c1', 'E:ghostTool#c5', 'E:freeTool#c2'], False)

        # #6 批准后续：pending 批准后 driveConfirmation 发 Start->End，再从 currentIndex+1 续 batch
        conv = agentInstance.getConversation('s3')  # #3 的 pending 仍在
        pending = conv.takePending()
        assert pending is not None and pending.currentIndex == 1
        conv.setPending(pending)
        events = list(agentInstance.driveConfirmation('s3', pending.confirmationId, approved=True))
        # driveConfirmation 在 End 后会续 driveModelLoop（本验证 modelAdapter=None，产出 errorEvent 属预期），只校验工具事件前缀
        toolEvents = [e for e in events if isinstance(e, (toolCallStartEvent, toolCallEndEvent))]
        assert summarize(toolEvents) == ['S:confirmTool#c4', 'E:confirmTool#c4'], f'#6 批准后续事件序错: {summarize(events)}'
        starts = [e for e in events if isinstance(e, toolCallStartEvent)]
        assert len(starts) == 1, '#6 不应出现双 Start'
        print(f'#6 批准后续 通过：{summarize(toolEvents)}（无双 Start）')

        # #7 拒绝：仅 End（isError + blocked），无 Start
        list(agentInstance.driveToolBatch('s7', [confirm1], 0))  # 造 pending（生成器需耗尽）
        conv7 = agentInstance.getConversation('s7')
        pending7 = conv7.takePending()
        conv7.setPending(pending7)
        events7 = list(agentInstance.driveConfirmation('s7', pending7.confirmationId, approved=False))
        toolEvents7 = [e for e in events7 if isinstance(e, (toolCallStartEvent, toolCallEndEvent))]
        assert summarize(toolEvents7) == ['E:confirmTool#c4'], f'#7 拒绝路径事件序错: {summarize(events7)}'
        endEvent = toolEvents7[0]
        assert endEvent.toolResult.isError and endEvent.toolResult.details.get('blocked'), '#7 拒绝 End 应为 blocked isError'
        print(f'#7 拒绝 通过：{summarize(toolEvents7)}（仅 End，配对例外保持）')

        # #8 startIndex 续批（dangling/确认后续批同路径）：从中间索引起仍先批量 Start 再 End
        gen8 = agentInstance.driveToolBatch('s8', [confirm1, free1, free2], 0)
        list(gen8)  # 停在 confirmationRequired（c4）
        events8 = list(agentInstance.driveToolBatch('s8', [confirm1, free1, free2], 1))
        assert summarize(events8) == ['S:freeTool#c1', 'S:freeTool#c2', 'E:freeTool#c1', 'E:freeTool#c2'], \
            f'#8 续批事件序错: {summarize(events8)}'
        print(f'#8 续批(startIndex=1) 通过：{summarize(events8)}')

        # 未知工具合成 error End 内容
        genU = agentInstance.driveToolBatch('s9', [unknown1], 0)
        endU = [e for e in genU if isinstance(e, toolCallEndEvent)][0]
        assert endU.toolResult.isError and endU.toolResult.details.get('unknownTool'), '未知工具应合成 error End'
        print('补充 通过：未知工具 Start + 合成 error End')

    print('\nT3.4 全部通过：事件序表 #1-#8 符合 streamingLatencyFixPlan D2。')


if __name__ == '__main__':
    main()
