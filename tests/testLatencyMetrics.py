'''
Author: wilbur
Version: 1.0
Date: 2026-10-10
Description: latencyMetricsDisplayPlan T1.9 后端链路单测——usageUpdateEvent.timings 透传、executeToolCall elapsedMs 打点、
             sessionStore.updateUsage lastTimings 白名单、pump 缓存与回写、sseCodec 双分支 timings 编码、status/history DTO 透传。
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
    finalChunk,
    textChunk,
    textDeltaEvent,
    toolCall,
    toolCallEndEvent,
    toolOutput,
    usageUpdateEvent,
)
from flamingoAgents.models.chatCompletions import modelCompletion
from flamingoAgents.tools.toolDefinition import defineTool
from webApp.backend import sessionStore, usageStore
from webApp.backend.agentManager import streamPump
from webApp.backend.sseCodec import eventToFrame, usageUpdateDto, encodeSse


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


def textTurnWithTimings(text, prompt, completion, cached=0, timings=None):
    payload = {'usage': snakeUsage(prompt, completion, cached), 'timings': timings}
    return [
        textChunk(text=text),
        finalChunk(completion=completionWith(chatMessage(role='assistant', content=text), payload)),
    ]


def echoTool():
    return defineTool(
        name='echo',
        description='echo',
        parameters={'type': 'object', 'properties': {'text': {'type': 'string'}}},
        execute=lambda args, ctx: toolOutput(content=str(args.get('text', ''))),
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
    # pump 成本链路隔离：querySessionCost/loadCostMap 不落真库（对齐 testLiveUsageUpdate 同名模式）
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda sessionId: 0.0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    yield


def writeSession(sessionId, workDir, providerId='index-provider', modelId='index-model'):
    session = {
        'sessionId': sessionId,
        'title': 't',
        'workDir': str(workDir),
        'providerId': providerId,
        'modelId': modelId,
        'createdAt': sessionStore.nowIso(),
        'updatedAt': sessionStore.nowIso(),
        'usage': {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0},
        'contextTokens': 0,
    }
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
    # 同步驱动泵（对齐 testLiveUsageUpdate）：不走 start()（claimTerminal 依赖 activeStreams 注册）
    pump._pump()


# ---------- T1.3：Core usageUpdateEvent 携带 timings ----------

def testCoreUsageUpdateCarriesTimings(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    timings = {'ttfbMs': 820, 'durationMs': 5300, 'chunks': 42, 'sawDone': True}
    current = makeAgent(tmp_path, [textTurnWithTimings('hi', 10, 4, 2, timings=timings)])
    events = list(current.runUserMessageStream('hello', 'sess-lat'))
    updates = usageEvents(events)
    assert len(updates) == 1
    assert updates[0].timings == timings


def testCoreUsageUpdateTimingsMissingIsNone(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurnWithTimings('hi', 10, 4, 2)])
    events = list(current.runUserMessageStream('hello', 'sess-lat'))
    updates = usageEvents(events)
    assert updates[0].timings is None


# ---------- T1.2：executeToolCall 打点 elapsedMs ----------

def testExecuteToolCallRecordsElapsedMs(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    first = [
        textChunk(text='call'),
        finalChunk(completion=completionWith(
            chatMessage(role='assistant', content='', toolCalls=[toolCall(id='c1', toolName='echo', arguments={'text': 'x'})]),
            {'usage': snakeUsage(100, 20, 60), 'timings': {'ttfbMs': 100, 'durationMs': 900}},
        )),
    ]
    second = [
        textChunk(text='done'),
        finalChunk(completion=completionWith(chatMessage(role='assistant', content='done'), {'usage': snakeUsage(80, 10, 10), 'timings': None})),
    ]
    current = makeAgent(tmp_path, [first, second], tools=[echoTool()])
    events = list(current.runUserMessageStream('hello', 'sess-lat'))
    ends = [event for event in events if isinstance(event, toolCallEndEvent)]
    assert len(ends) == 1
    elapsed = ends[0].toolResult.details.get('elapsedMs')
    assert isinstance(elapsed, int)
    assert elapsed >= 0
    # 打点随 jsonl 落盘：toolResult 事件含 elapsedMs
    logEvents = json.loads('[]')
    logPath = tmp_path / 'sess-lat.jsonl'
    if logPath.exists():
        with open(logPath, encoding='utf-8') as fh:
            logEvents = [json.loads(line) for line in fh if line.strip()]
    toolEvents = [e for e in logEvents if e.get('type') == 'toolResult']
    assert toolEvents and isinstance(toolEvents[0]['details'].get('elapsedMs'), int)


def testExecuteToolCallKeepsExistingDetailsKeys(tmp_path) -> None:
    def executeWithDetails(args, ctx):
        return toolOutput(content='ok', details={'userStopped': True})

    tool = defineTool(
        name='stub',
        description='stub',
        parameters={'type': 'object', 'properties': {}},
        execute=executeWithDetails,
    )
    current = makeAgent(tmp_path, [], tools=[tool])
    result = current.executeToolCall(toolCall(id='k1', toolName='stub', arguments={}), 'sess-lat')
    assert result.details.get('userStopped') is True
    assert isinstance(result.details.get('elapsedMs'), int)


def testUnknownToolResultHasNoElapsedMs(tmp_path) -> None:
    current = makeAgent(tmp_path, [])
    result = current.executeToolCall(toolCall(id='k2', toolName='notRegistered', arguments={}), 'sess-lat')
    assert result.isError is True
    assert 'elapsedMs' not in (result.details or {})


# ---------- T1.4：sseCodec 双分支 timings ----------

def testSseCodecEventBranchCarriesTimings() -> None:
    event = usageUpdateEvent(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=3,
        timings={'ttfbMs': 5, 'durationMs': 40},
    )
    name, data = eventToFrame(event)
    assert name == 'usageUpdate'
    assert data['timings'] == {'ttfbMs': 5, 'durationMs': 40}


def testSseCodecEventBranchTimingsDefaultNone() -> None:
    event = usageUpdateEvent(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=3,
    )
    _, data = eventToFrame(event)
    assert 'timings' in data and data['timings'] is None


def testSseCodecDtoBranchCarriesTimings() -> None:
    dto = usageUpdateDto(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=3,
        cost=0.5,
        timings={'ttfbMs': 5, 'durationMs': 40},
    )
    name, data = eventToFrame(dto)
    assert name == 'usageUpdate'
    assert data['timings'] == {'ttfbMs': 5, 'durationMs': 40}


# ---------- T1.5：pump 透传/缓存/回写 ----------

def testPumpDtoCarriesAndCachesTimings(tmp_path, monkeypatch, isolatedStores, pumpCosts) -> None:
    writeSession('sess-lat', tmp_path)
    logPath = tmp_path / 'sess-lat.jsonl'
    currentConversation = conversation(sessionId='sess-lat', logPath=logPath, systemPrompt='sys')
    currentConversation.usageTotal = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
    monkeypatch.setattr(sessionStore, 'updateUsage', lambda *a, **k: None)
    timings = {'ttfbMs': 120, 'durationMs': 2400, 'chunks': 9}
    event = usageUpdateEvent(
        usage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        stepUsage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        contextTokens=130,
        timings=timings,
    )
    pump = makePump('sess-lat', currentConversation, [event, completedEvent(message='ok')])
    runPump(pump)
    dtos = [e for e in pump.history if isinstance(e, usageUpdateDto)]
    assert len(dtos) == 1
    assert dtos[0].timings == timings
    assert pump.lastTimings == timings


def testPumpNoneTimingsDoesNotOverwriteCache(tmp_path, monkeypatch, isolatedStores, pumpCosts) -> None:
    writeSession('sess-lat', tmp_path)
    logPath = tmp_path / 'sess-lat.jsonl'
    currentConversation = conversation(sessionId='sess-lat', logPath=logPath, systemPrompt='sys')
    currentConversation.usageTotal = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
    captured = {}

    pairWrites = []

    def fakeUpdate(sessionId, usage, contextTokens=None, lastUsage=None, lastTimings=None, lastStepUsage=None):
        captured['lastTimings'] = lastTimings
        captured['lastStepUsage'] = lastStepUsage
        if lastTimings is not None and lastStepUsage is not None:
            pairWrites.append((dict(lastTimings), dict(lastStepUsage)))
        return None

    monkeypatch.setattr(sessionStore, 'updateUsage', fakeUpdate)
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
    pump = makePump('sess-lat', currentConversation, [first, second, completedEvent(message='ok')])
    runPump(pump)
    assert pump.lastTimings == {'ttfbMs': 50, 'durationMs': 700}
    # v1.16 新语义：第一帧完整对子 → 帧内即时写索引；第二帧缺 timings 不构成新对子（pairWrites 不增）；
    # 终态回写在仍有完整缓存对子时再次成对写入（幂等同值）。
    assert len(pairWrites) >= 1 and all(
        timings == {'ttfbMs': 50, 'durationMs': 700}
        and step == {'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20}
        for timings, step in pairWrites
    )


# ---------- T1.6：sessionStore lastTimings 白名单 ----------

def testUpdateUsageLastTimingsWhitelist(tmp_path, isolatedStores) -> None:
    writeSession('sess-wh', tmp_path)
    sessionStore.updateUsage(
        'sess-wh',
        {'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=12,
        lastUsage={'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
        lastTimings={'ttfbMs': 300, 'durationMs': 1500, 'chunks': 77, 'junk': 'x'},
    )
    stored = sessionStore.getSession('sess-wh')
    assert stored['lastTimings'] == {'ttfbMs': 300, 'durationMs': 1500}


def testUpdateUsageLastTimingsInvalidSkips(tmp_path, isolatedStores) -> None:
    writeSession('sess-wh', tmp_path)
    sessionStore.updateUsage(
        'sess-wh',
        {'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
        lastTimings={'ttfbMs': 'fast', 'durationMs': -5},
    )
    stored = sessionStore.getSession('sess-wh')
    assert 'lastTimings' not in stored


def testUpdateUsageLastTimingsNoneKeepsOld(tmp_path, isolatedStores) -> None:
    writeSession('sess-wh', tmp_path)
    sessionStore.updateUsage(
        'sess-wh',
        {'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
        lastTimings={'ttfbMs': 10, 'durationMs': 20},
    )
    sessionStore.updateUsage(
        'sess-wh',
        {'promptTokens': 2, 'cachedTokens': 0, 'completionTokens': 2},
        lastTimings=None,
    )
    stored = sessionStore.getSession('sess-wh')
    assert stored['lastTimings'] == {'ttfbMs': 10, 'durationMs': 20}


# ---------- T1.8：historyView timings 透传 ----------

def testHistoryViewAssistantTimings(tmp_path, isolatedStores, monkeypatch) -> None:
    from flamingoAgents.utils import logPaths

    monkeypatch.setitem(logPaths._categoryRoots, 'webData', tmp_path)
    from webApp.backend import historyView

    logPath = logPaths.ensureSessionLogDir('webData', tmp_path) / 'sess-hist.jsonl'
    lines = [
        {'type': 'userMessage', 'content': 'hi'},
        {
            'type': 'assistantMessage',
            'model': 'm',
            'content': 'ok',
            'toolCalls': [],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 3, 'prompt_tokens_details': {'cached_tokens': 1}},
            'timings': {'ttfbMs': 200, 'durationMs': 800},
        },
        {
            'type': 'toolResult',
            'toolCallId': 't1',
            'toolName': 'echo',
            'isError': False,
            'content': 'x',
            'details': {'elapsedMs': 123},
        },
    ]
    with open(logPath, 'w', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
    writeSession('sess-hist', tmp_path)
    messages = historyView.loadMessages('sess-hist')
    assistant = [m for m in messages if m['kind'] == 'assistant'][0]
    assert assistant['timings'] == {'ttfbMs': 200, 'durationMs': 800}
    tool = [m for m in messages if m['kind'] == 'tool'][0]
    assert tool['details']['elapsedMs'] == 123


def testHistoryViewOldAssistantTimingsNone(tmp_path, isolatedStores, monkeypatch) -> None:
    from flamingoAgents.utils import logPaths

    monkeypatch.setitem(logPaths._categoryRoots, 'webData', tmp_path)
    from webApp.backend import historyView

    logPath = logPaths.ensureSessionLogDir('webData', tmp_path) / 'sess-old.jsonl'
    lines = [
        {'type': 'userMessage', 'content': 'hi'},
        {'type': 'assistantMessage', 'model': 'm', 'content': 'ok', 'toolCalls': [], 'usage': None},
    ]
    with open(logPath, 'w', encoding='utf-8') as fh:
        for line in lines:
            fh.write(json.dumps(line, ensure_ascii=False) + '\n')
    writeSession('sess-old', tmp_path)
    messages = historyView.loadMessages('sess-old')
    assistant = [m for m in messages if m['kind'] == 'assistant'][0]
    assert assistant['timings'] is None


# ---------- encodeSse 全帧 JSON ----------

def testEncodeSseUsageFrameWithTimings() -> None:
    dto = usageUpdateDto(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=3,
        cost=0.0,
        timings={'ttfbMs': 5, 'durationMs': 40},
    )
    frame = encodeSse(dto)
    assert frame.startswith('event: usageUpdate\n')
    payload = json.loads(frame.split('data: ', 1)[1])
    assert payload['timings'] == {'ttfbMs': 5, 'durationMs': 40}


# ---------- thinkingPhaseMetricsPlan T1：reasoning 打点 ----------

def testSuccessTimingsReasoningWindow() -> None:
    from flamingoAgents.models.chatCompletions import successTimings

    diag = {'t0': 1000.0}
    # 模拟思考流：首个 chunk 在 t0+4386ms，最后一个在 t0+45900ms
    diag['reasoningStartMs'] = 4386
    diag['reasoningEndMs'] = 45900
    timings = successTimings(diag, textChars=865, reasoningChars=5052, sawDone=True)
    assert timings['reasoningStartMs'] == 4386
    assert timings['reasoningEndMs'] == 45900
    assert timings['reasoningChars'] == 5052


def testNoteStreamYieldRecordsReasoningWindow() -> None:
    # 直接驱动 _noteStreamYield：首末 reasoning chunk 时点被记录
    from flamingoAgents.models.chatCompletions import chatCompletionsAdapter
    from flamingoAgents.core.types import reasoningChunk, textChunk

    adapter = chatCompletionsAdapter.__new__(chatCompletionsAdapter)
    t0 = 100.0
    diag = {'t0': t0, 'textChars': 0, 'reasoningChars': 0}

    class fakeClock:
        def __init__(self):
            self.now = t0

        def elapsed(self, base):
            return int((self.now - base) * 1000)

    clock = fakeClock()
    import flamingoAgents.models.chatCompletions as cc
    original = cc.elapsedMs
    cc.elapsedMs = lambda base: clock.elapsed(base)
    try:
        clock.now = t0 + 4.5
        adapter._noteStreamYield(diag, reasoningChunk(text='思考'))
        assert diag['reasoningStartMs'] == 4500
        assert diag['reasoningEndMs'] == 4500

        clock.now = t0 + 45.9
        adapter._noteStreamYield(diag, reasoningChunk(text='继续'))
        assert diag['reasoningStartMs'] == 4500  # 首个不变
        assert diag['reasoningEndMs'] == 45900  # 持续更新

        clock.now = t0 + 52.976
        adapter._noteStreamYield(diag, textChunk(text='正文'))
        assert diag['reasoningEndMs'] == 45900  # textChunk 不影响思考窗口
    finally:
        cc.elapsedMs = original


def testResponsesAdapterNoteStreamYieldReasoningWindow() -> None:
    from flamingoAgents.models.responsesAdapter import responsesAdapter
    from flamingoAgents.core.types import reasoningChunk

    adapter = responsesAdapter.__new__(responsesAdapter)
    t0 = 100.0
    diag = {'t0': t0, 'textChars': 0, 'reasoningChars': 0}
    import flamingoAgents.models.responsesAdapter as ra
    original = ra.elapsedMs  # from-import 独立绑定，须 patch 本模块

    state = {'now': t0}
    ra.elapsedMs = lambda base: int((state['now'] - base) * 1000)
    try:
        state['now'] = t0 + 1.0
        adapter._noteStreamYield(diag, reasoningChunk(text='r'))
        state['now'] = t0 + 2.5
        adapter._noteStreamYield(diag, reasoningChunk(text='r2'))
        assert diag['reasoningStartMs'] == 1000
        assert diag['reasoningEndMs'] == 2500
    finally:
        ra.elapsedMs = original


def testUpdateUsageLastTimingsReasoningWhitelist(tmp_path, isolatedStores) -> None:
    writeSession('sess-think', tmp_path)
    sessionStore.updateUsage(
        'sess-think',
        {'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
        lastTimings={'ttfbMs': 4386, 'durationMs': 52976, 'reasoningStartMs': 4386, 'reasoningEndMs': 45900, 'chunks': 99},
    )
    stored = sessionStore.getSession('sess-think')
    assert stored['lastTimings'] == {'ttfbMs': 4386, 'durationMs': 52976, 'reasoningStartMs': 4386, 'reasoningEndMs': 45900}
    # 无 reasoning 打点的旧帧：两键缺省不写入
    sessionStore.updateUsage(
        'sess-think',
        {'promptTokens': 2, 'cachedTokens': 0, 'completionTokens': 2},
        lastTimings={'ttfbMs': 100, 'durationMs': 900},
    )
    stored = sessionStore.getSession('sess-think')
    assert 'reasoningStartMs' not in stored['lastTimings']
    assert 'reasoningEndMs' not in stored['lastTimings']
