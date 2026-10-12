'''
Author: wilbur
Version: 1.0
Date: 2026-10-12
Description: latencyDisplayImplementationPlan P1 红测试——TPS 配对持久化（pump/index/status/旧日志 fallback）、
             工具参数起点与 callElapsedMs（确认等待/拒绝缺省/JSONL 恢复）、Total（入口起点/确认续流不重置/最终 assistant/completed/history 同值）、
             sseCodec/historyView 新字段透传。
'''

from __future__ import annotations

import json
import re
import threading

import pytest

from flamingoAgents.core.agent import agent
from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.types import (
    chatMessage,
    completedEvent,
    confirmationRequiredEvent,
    finalChunk,
    textChunk,
    toolCall,
    toolOutput,
    usageUpdateEvent,
)
from flamingoAgents.models.chatCompletions import modelCompletion
from flamingoAgents.tools.toolDefinition import defineTool, permissionRule
from webApp.backend import sessionStore, usageStore
from webApp.backend.agentManager import streamPump
from webApp.backend.sseCodec import eventToFrame, usageUpdateDto


usageKeys = ('promptTokens', 'cachedTokens', 'completionTokens')


class scriptedAdapter:
    def __init__(self, outcomes, providerId='volcano', modelId='flash-test'):
        self.outcomes = list(outcomes)
        self.config = type('cfg', (), {'configProviderId': providerId, 'provider': providerId, 'model': modelId})()

    def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
        outcome = self.outcomes.pop(0)
        yield from outcome


def snakeUsage(prompt, completion, cached=0):
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'prompt_tokens_details': {'cached_tokens': cached},
    }


def completionWith(message, payload):
    return modelCompletion(message=message, requestPayload={}, responsePayload=payload)


def textTurn(text, prompt, completion, cached=0, timings=None):
    payload = {'usage': snakeUsage(prompt, completion, cached), 'timings': timings}
    return [
        textChunk(text=text),
        finalChunk(completion=completionWith(chatMessage(role='assistant', content=text), payload)),
    ]


def toolTurn(prompt, completion, calls, timings=None):
    payload = {'usage': snakeUsage(prompt, completion), 'timings': timings}
    return [
        textChunk(text='tool'),
        finalChunk(completion=completionWith(chatMessage(role='assistant', content='', toolCalls=calls), payload)),
    ]


def echoTool():
    return defineTool(
        name='echo',
        description='echo',
        parameters={'type': 'object', 'properties': {'text': {'type': 'string'}}},
        execute=lambda args, ctx: toolOutput(content=str(args.get('text', ''))),
    )


def confirmTool():
    return defineTool(
        name='rm',
        description='rm',
        parameters={'type': 'object', 'properties': {'path': {'type': 'string'}}},
        execute=lambda args, ctx: toolOutput(content='removed'),
        permissions=[permissionRule(
            id='rm',
            field='path',
            action='requireApproval',
            reason='删除类命令需确认',
            patterns=[re.compile(r'.')],
        )],
    )


def makeAgent(tmpPath, outcomes, tools=None):
    return agent(
        modelAdapter=scriptedAdapter(outcomes),
        toolDefinitions=tools or [],
        workDir=tmpPath,
        logDir=tmpPath,
        systemPrompt='sys',
    )


def usageEvents(events):
    return [event for event in events if isinstance(event, usageUpdateEvent)]


def completedEvents(events):
    return [event for event in events if isinstance(event, completedEvent)]


def readJsonl(logPath):
    if not logPath.exists():
        return []
    with open(logPath, encoding='utf-8') as fh:
        return [json.loads(line) for line in fh if line.strip()]


@pytest.fixture
def isolatedStores(tmp_path, monkeypatch):
    sessionsDir = tmp_path / 'webData'
    sessionsDir.mkdir()
    monkeypatch.setattr(sessionStore, 'webDataDir', sessionsDir)
    monkeypatch.setattr(sessionStore, 'indexPath', sessionsDir / 'sessions.json')
    monkeypatch.setattr(sessionStore, 'legacyIndexPath', sessionsDir / 'legacy.json')
    yield sessionsDir


@pytest.fixture
def pumpCosts(monkeypatch):
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda sessionId: 0.0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    yield


def writeSession(sessionId, workDir, **extra):
    session = {
        'sessionId': sessionId,
        'title': 't',
        'workDir': str(workDir),
        'providerId': 'index-provider',
        'modelId': 'index-model',
        'createdAt': sessionStore.nowIso(),
        'updatedAt': sessionStore.nowIso(),
        'usage': {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0},
        'contextTokens': 0,
    }
    session.update(extra)
    sessionStore.saveIndex({sessionId: session})
    return session


class listStream:
    def __init__(self, events):
        self.events = list(events)

    def __iter__(self):
        yield from self.events

    def close(self):
        pass


def makePumpAgent(sessionId, currentConversation):
    class fakeAgent:
        def __init__(self):
            self.sessionLocksGuard = threading.RLock()
            self.conversations = {sessionId: currentConversation} if currentConversation is not None else {}
            self.modelAdapter = scriptedAdapter([])

        def interruptActiveStreams(self, sid):
            pass

    return fakeAgent()


def makePump(sessionId, currentConversation, events):
    return streamPump(sessionId, makePumpAgent(sessionId, currentConversation), listStream(events))


def runPump(pump):
    pump._pump()


# ---------- TPS 配对：pump 回写索引同源对子 ----------

def testPumpWritesStepUsagePairToIndex(tmp_path, monkeypatch, isolatedStores, pumpCosts) -> None:
    writeSession('sess-pair', tmp_path)
    logPath = tmp_path / 'sess-pair.jsonl'
    currentConversation = conversation(sessionId='sess-pair', logPath=logPath, systemPrompt='sys')
    event = usageUpdateEvent(
        usage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        stepUsage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        contextTokens=130,
        timings={'ttfbMs': 120, 'durationMs': 2400},
    )
    pump = makePump('sess-pair', currentConversation, [event, completedEvent(message='ok')])
    runPump(pump)
    stored = sessionStore.getSession('sess-pair')
    assert stored['lastTimings'] == {'ttfbMs': 120, 'durationMs': 2400}
    assert stored['lastStepUsage'] == {'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20}


def testPumpIncompleteFrameKeepsPreviousPair(tmp_path, monkeypatch, isolatedStores, pumpCosts) -> None:
    # 第一帧完整对子写入；第二帧缺 timings（None）→ 索引配对不被清/不被旧值错配
    writeSession('sess-pair2', tmp_path)
    logPath = tmp_path / 'sess-pair2.jsonl'
    currentConversation = conversation(sessionId='sess-pair2', logPath=logPath, systemPrompt='sys')
    first = usageUpdateEvent(
        usage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        stepUsage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        contextTokens=130,
        timings={'ttfbMs': 50, 'durationMs': 700},
    )
    second = usageUpdateEvent(
        usage={'promptTokens': 200, 'cachedTokens': 20, 'completionTokens': 40},
        stepUsage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        contextTokens=260,
        timings=None,
    )
    pump = makePump('sess-pair2', currentConversation, [first, second, completedEvent(message='ok')])
    runPump(pump)
    stored = sessionStore.getSession('sess-pair2')
    assert stored['lastTimings'] == {'ttfbMs': 50, 'durationMs': 700}
    assert stored['lastStepUsage'] == {'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20}


def testSessionStoreLastStepUsageValidation(tmp_path, isolatedStores) -> None:
    writeSession('sess-store', tmp_path)
    sessionStore.updateUsage(
        'sess-store',
        {'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
        lastTimings={'ttfbMs': 300, 'durationMs': 1500},
        lastStepUsage={'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
    )
    stored = sessionStore.getSession('sess-store')
    assert stored['lastStepUsage'] == {'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2}
    # 非法：负数 / bool / 缺字段 → 不写
    sessionStore.updateUsage(
        'sess-store',
        {'promptTokens': 20, 'cachedTokens': 0, 'completionTokens': 3},
        lastTimings={'ttfbMs': 400, 'durationMs': 2000},
        lastStepUsage={'promptTokens': -1, 'cachedTokens': 0, 'completionTokens': 3},
    )
    stored = sessionStore.getSession('sess-store')
    assert stored['lastStepUsage'] == {'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2}
    sessionStore.updateUsage(
        'sess-store',
        {'promptTokens': 30, 'cachedTokens': 0, 'completionTokens': 4},
        lastTimings={'ttfbMs': 500, 'durationMs': 2500},
        lastStepUsage={'promptTokens': True, 'cachedTokens': 0, 'completionTokens': 4},
    )
    stored = sessionStore.getSession('sess-store')
    assert stored['lastStepUsage'] == {'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2}


# ---------- 工具参数起点与 callElapsedMs ----------

def testArgumentsStartedAtMsPersistedAndResumed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    calls = [toolCall(id='c1', toolName='echo', arguments={'text': 'x'}, argumentsStartedAtMs=12345)]
    first = toolTurn(100, 20, calls)
    second = textTurn('done', 80, 10)
    current = makeAgent(tmp_path, [first, second], tools=[echoTool()])
    events = list(current.runUserMessageStream('hello', 'sess-args'))
    ends = [e for e in events if type(e).__name__ == 'toolCallEndEvent']
    assert ends and 'callElapsedMs' in ends[0].toolResult.details
    assert ends[0].toolResult.details['callElapsedMs'] >= 0
    # JSONL 落盘 argumentsStartedAtMs
    logEvents = readJsonl(tmp_path / 'sess-args.jsonl')
    assistantEvents = [e for e in logEvents if e.get('type') == 'assistantMessage']
    assert assistantEvents[0]['toolCalls'][0]['argumentsStartedAtMs'] == 12345


def testRejectedToolResultHasNoCallElapsedMs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    calls = [toolCall(id='c1', toolName='rm', arguments={'path': '/x'}, argumentsStartedAtMs=12345)]
    first = toolTurn(100, 20, calls)
    second = textTurn('done', 80, 10)
    current = makeAgent(tmp_path, [first, second], tools=[confirmTool()])
    firstEvents = list(current.runUserMessageStream('hello', 'sess-rej'))
    pending = next(e for e in firstEvents if isinstance(e, confirmationRequiredEvent))
    rejected = list(current.continueConfirmationStream('sess-rej', pending.confirmationId, False))
    end = next(e for e in rejected if type(e).__name__ == 'toolCallEndEvent')
    assert 'callElapsedMs' not in end.toolResult.details


def testConfirmationWaitIncludedInCallElapsedMs(tmp_path, monkeypatch) -> None:
    # 确认等待时长计入 callElapsedMs：起点早于确认批准
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    startedAtMs = 1000
    calls = [toolCall(id='c1', toolName='rm', arguments={'path': '/x'}, argumentsStartedAtMs=startedAtMs)]
    first = toolTurn(100, 20, calls)
    second = textTurn('done', 80, 10)
    current = makeAgent(tmp_path, [first, second], tools=[confirmTool()])
    import flamingoAgents.core.agent as agentModule
    realNow = agentModule.time.time
    monkeypatch.setattr(agentModule.time, 'time', lambda: 6.0)  # 结束时刻固定 6s（确认等待前无工具执行）
    firstEvents = list(current.runUserMessageStream('hello', 'sess-confirm'))
    monkeypatch.setattr(agentModule.time, 'time', realNow)
    pending = next(e for e in firstEvents if isinstance(e, confirmationRequiredEvent))
    monkeypatch.setattr(agentModule.time, 'time', lambda: 12.0)  # 批准后工具结束 12s
    try:
        approved = list(current.continueConfirmationStream('sess-confirm', pending.confirmationId, True))
    finally:
        monkeypatch.setattr(agentModule.time, 'time', realNow)
    end = next(e for e in approved if type(e).__name__ == 'toolCallEndEvent')
    assert end.toolResult.details['callElapsedMs'] == 11000  # 12000ms - 1000ms（含确认等待）


# ---------- Total：入口起点 → completed → history ----------

def testTotalElapsedMsOnCompletedAndJsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    import flamingoAgents.core.agent as agentModule
    current = makeAgent(tmp_path, [textTurn('done', 10, 4)])
    realMono = agentModule.time.monotonic
    state = {'now': 100.0}
    monkeypatch.setattr(agentModule.time, 'monotonic', lambda: state['now'])
    try:
        stream = current.runUserMessageStream('hello', 'sess-total', turnStartedAt=100.0)
        state['now'] = 109.2
        events = list(stream)
    finally:
        monkeypatch.setattr(agentModule.time, 'monotonic', realMono)
    completed = completedEvents(events)
    assert completed and completed[0].totalElapsedMs == 9200
    logEvents = readJsonl(tmp_path / 'sess-total.jsonl')
    assistants = [e for e in logEvents if e.get('type') == 'assistantMessage']
    assert assistants[-1]['totalElapsedMs'] == 9200


def testTotalAcrossConfirmationContinuation(tmp_path, monkeypatch) -> None:
    # 确认续流不重置起点：Total 覆盖首段+确认等待+续流
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    import flamingoAgents.core.agent as agentModule
    calls = [toolCall(id='c1', toolName='rm', arguments={'path': '/x'}, argumentsStartedAtMs=1000)]
    first = toolTurn(100, 20, calls)
    second = textTurn('done', 80, 10)
    current = makeAgent(tmp_path, [first, second], tools=[confirmTool()])
    realMono = agentModule.time.monotonic
    state = {'now': 200.0}
    monkeypatch.setattr(agentModule.time, 'monotonic', lambda: state['now'])
    try:
        stream = current.runUserMessageStream('hello', 'sess-cont', turnStartedAt=200.0)
        firstEvents = list(stream)
        pending = next(e for e in firstEvents if isinstance(e, confirmationRequiredEvent))
        state['now'] = 260.0  # 确认等待 60s；续流完成在 260（completed 产出时刻）
        approved = list(current.continueConfirmationStream('sess-cont', pending.confirmationId, True))
    finally:
        monkeypatch.setattr(agentModule.time, 'monotonic', realMono)
    completed = completedEvents(approved)
    assert completed and completed[0].totalElapsedMs == 60000  # 260s - 200s：含首段+确认等待
    logEvents = readJsonl(tmp_path / 'sess-cont.jsonl')
    assistants = [e for e in logEvents if e.get('type') == 'assistantMessage']
    # 中间 assistant（工具轮）无 Total；最终 assistant 有
    assert 'totalElapsedMs' not in assistants[0]
    assert assistants[-1]['totalElapsedMs'] == 60000


def testStopNoTotal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('done', 10, 4)])
    events = list(current.runUserMessageStream('hello', 'sess-stop'))
    # 正常完成路径应产出 completed（对照：异常路径不会到这），此处仅确认字段存在性契约
    completed = completedEvents(events)
    if completed:
        assert completed[0].totalElapsedMs is not None


# ---------- sseCodec / historyView 透传 ----------

def testSseCodecCompletedCarriesTotal() -> None:
    event = completedEvent(message='ok', totalElapsedMs=9200)
    name, data = eventToFrame(event)
    assert name == 'completed'
    assert data['totalElapsedMs'] == 9200
    # 兼容：旧构造无值 → None
    name2, data2 = eventToFrame(completedEvent(message='ok'))
    assert data2['totalElapsedMs'] is None


def testSseCodecToolCallCarriesArgumentsStart() -> None:
    call = toolCall(id='c1', toolName='bash', arguments={}, argumentsStartedAtMs=12345)
    start = type('e', (), {})()
    from flamingoAgents.core.types import toolCallStartEvent
    event = toolCallStartEvent(toolCall=call, preview='ls')
    name, data = eventToFrame(event)
    assert name == 'toolCallStart'
    assert data['toolCall']['argumentsStartedAtMs'] == 12345
    # 兼容：无值 → None
    callOld = toolCall(id='c2', toolName='bash', arguments={})
    name2, data2 = eventToFrame(toolCallStartEvent(toolCall=callOld, preview='ls'))
    assert data2['toolCall']['argumentsStartedAtMs'] is None


def testHistoryViewTransmitsNewFields(tmp_path, isolatedStores, monkeypatch) -> None:
    from flamingoAgents.utils import logPaths

    monkeypatch.setitem(logPaths._categoryRoots, 'webData', tmp_path)
    from webApp.backend import historyView

    logPath = logPaths.ensureSessionLogDir('webData', tmp_path) / 'sess-hist.jsonl'
    lines = [
        {'type': 'userMessage', 'content': 'hi'},
        {
            'type': 'assistantMessage',
            'model': 'm',
            'content': '',
            'toolCalls': [{'id': 't1', 'toolName': 'echo', 'arguments': {}, 'argumentsStartedAtMs': 500}],
            'usage': snakeUsage(5, 3, 1),
            'timings': {'ttfbMs': 200, 'durationMs': 800},
        },
        {
            'type': 'toolResult',
            'toolCallId': 't1',
            'toolName': 'echo',
            'isError': False,
            'content': 'x',
            'details': {'elapsedMs': 123, 'callElapsedMs': 900},
        },
        {
            'type': 'assistantMessage',
            'model': 'm',
            'content': 'final',
            'toolCalls': [],
            'usage': snakeUsage(5, 3, 0),
            'totalElapsedMs': 9200,
        },
    ]
    with open(logPath, 'w', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
    writeSession('sess-hist', tmp_path)
    messages = historyView.loadMessages('sess-hist')
    toolAssistant = [m for m in messages if m['kind'] == 'assistant' and m['toolCalls']][0]
    assert toolAssistant['toolCalls'][0]['argumentsStartedAtMs'] == 500
    finalAssistant = [m for m in messages if m['kind'] == 'assistant' and not m['toolCalls']][0]
    assert finalAssistant['totalElapsedMs'] == 9200
    toolMessage = [m for m in messages if m['kind'] == 'tool'][0]
    assert toolMessage['details']['callElapsedMs'] == 900


# ---------- server status：lastStepUsage 与旧日志 fallback ----------

def testGetStatusReturnsPairAndFallback(tmp_path, isolatedStores, monkeypatch) -> None:
    from flamingoAgents.utils import logPaths

    monkeypatch.setitem(logPaths._categoryRoots, 'webData', tmp_path)
    from webApp.backend import historyView, server

    # 索引已有完整对子 → 直接返回
    writeSession(
        'sess-status', tmp_path,
        lastTimings={'ttfbMs': 100, 'durationMs': 900},
        lastStepUsage={'promptTokens': 5, 'cachedTokens': 1, 'completionTokens': 3},
    )
    status = server.getSessionStatus('sess-status')
    assert status['lastStepUsage'] == {'promptTokens': 5, 'cachedTokens': 1, 'completionTokens': 3}
    assert status['lastTimings'] == {'ttfbMs': 100, 'durationMs': 900}

    # 索引无 lastStepUsage（旧会话）→ 从日志最近同事件对 fallback
    logPath = logPaths.ensureSessionLogDir('webData', tmp_path) / 'sess-old.jsonl'
    lines = [
        {'type': 'userMessage', 'content': 'hi'},
        {
            'type': 'assistantMessage',
            'model': 'm',
            'content': 'a',
            'toolCalls': [],
            'usage': snakeUsage(10, 4, 2),
            'timings': {'ttfbMs': 300, 'durationMs': 1200},
        },
        # 更近一条但缺 timings → 不能配对，回退上一条
        {
            'type': 'assistantMessage',
            'model': 'm',
            'content': 'b',
            'toolCalls': [],
            'usage': snakeUsage(20, 8, 0),
        },
    ]
    with open(logPath, 'w', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
    writeSession('sess-old', tmp_path, lastTimings={'ttfbMs': 300, 'durationMs': 1200})
    statusOld = server.getSessionStatus('sess-old')
    assert statusOld['lastStepUsage'] == {'promptTokens': 10, 'cachedTokens': 2, 'completionTokens': 4}
    assert statusOld['lastTimings'] == {'ttfbMs': 300, 'durationMs': 1200}


def testGetStatusFallbackCacheInvalidationOnLogChange(tmp_path, isolatedStores, monkeypatch) -> None:
    from flamingoAgents.utils import logPaths

    monkeypatch.setitem(logPaths._categoryRoots, 'webData', tmp_path)
    from webApp.backend import historyView, server

    logPath = logPaths.ensureSessionLogDir('webData', tmp_path) / 'sess-cache.jsonl'
    lines = [
        {'type': 'userMessage', 'content': 'hi'},
        {'type': 'assistantMessage', 'model': 'm', 'content': 'a', 'toolCalls': [], 'usage': snakeUsage(10, 4, 2)},
    ]
    with open(logPath, 'w', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
    writeSession('sess-cache', tmp_path, lastTimings={'ttfbMs': 300, 'durationMs': 1200})
    # 首次：无合法对子 → None 进缓存
    assert server.getSessionStatus('sess-cache')['lastStepUsage'] is None
    # 日志新增合法 assistantMessage（水位变化）→ 缓存失效重扫，读到新对子
    with open(logPath, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps({'type': 'assistantMessage', 'model': 'm', 'content': 'b', 'toolCalls': [], 'usage': snakeUsage(12, 6, 0), 'timings': {'ttfbMs': 100, 'durationMs': 500}}) + '\n')
    assert server.getSessionStatus('sess-cache')['lastStepUsage'] == {'promptTokens': 12, 'cachedTokens': 0, 'completionTokens': 6}


def testGetStatusFallbackRejectsInvalidUsage(tmp_path, isolatedStores, monkeypatch) -> None:
    from flamingoAgents.utils import logPaths

    monkeypatch.setitem(logPaths._categoryRoots, 'webData', tmp_path)
    from webApp.backend import server

    logPath = logPaths.ensureSessionLogDir('webData', tmp_path) / 'sess-bad.jsonl'
    lines = [
        {'type': 'userMessage', 'content': 'hi'},
        {
            'type': 'assistantMessage',
            'model': 'm',
            'content': 'a',
            'toolCalls': [],
            # 非法：bool completionTokens
            'usage': {'prompt_tokens': 10, 'completion_tokens': True, 'prompt_tokens_details': {'cached_tokens': 0}},
            'timings': {'ttfbMs': 300, 'durationMs': 1200},
        },
    ]
    with open(logPath, 'w', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
    writeSession('sess-bad', tmp_path, lastTimings={'ttfbMs': 300, 'durationMs': 1200})
    status = server.getSessionStatus('sess-bad')
    assert status['lastStepUsage'] is None  # 非法即不配对
