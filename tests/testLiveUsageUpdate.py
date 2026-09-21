'''
Author: wilbur
Version: 1.5
Date: 2026-09-21
Description: Core usageUpdateEvent, pump DTO/liveCost/_recordUsage, SSE codec dual mapping, and temp JSONL/sessions/usageEvents reconciliation for live status-bar usage. v1.5 泵终态不再写 usageTurns，状态栏费用改读统一账本。
'''

from __future__ import annotations

import queue
import re
import sqlite3
import threading
from dataclasses import dataclass

import pytest

from flamingoAgents.core.agent import agent
from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.types import (
    chatMessage,
    completedEvent,
    confirmationRequiredEvent,
    errorEvent,
    finalChunk,
    modelInterruptedError,
    retryNoticeEvent,
    terminalEventTypes,
    textChunk,
    textDeltaEvent,
    toolCall,
    toolCallEndEvent,
    toolCallStartEvent,
    toolOutput,
    usageUpdateEvent,
)
from flamingoAgents.models.chatCompletions import modelCompletion, modelRequestError
from flamingoAgents.models.responsesAdapter import normalizeUsage
from flamingoAgents.tools.toolDefinition import defineTool, permissionRule
from flamingoAgents.utils.jsonl import jsonlLog
from flamingoAgents.utils import usageLedger
from webApp.backend import sessionStore, usageStore
from webApp.backend.agentManager import compactDeltas, requestStop, startStream, streamPump, unregisterStream
from webApp.backend.sseCodec import eventToFrame, usageUpdateDto


usageKeys = ('promptTokens', 'cachedTokens', 'completionTokens')


class scriptedAdapter:
    def __init__(self, outcomes: list, providerId='volcano', modelId='flash-test'):
        self.outcomes = list(outcomes)
        self.config = type('cfg', (), {'configProviderId': providerId, 'provider': providerId, 'model': modelId})()

    def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        yield from outcome


class listStream:
    def __init__(self, events):
        self.events = list(events)
        self.closed = False

    def __iter__(self):
        return iter(self.events)

    def close(self):
        self.closed = True


def snakeUsage(prompt, completion, cached=0):
    payload = {'prompt_tokens': prompt, 'completion_tokens': completion}
    if cached:
        payload['prompt_tokens_details'] = {'cached_tokens': cached}
    return payload


def completionWith(message, usage=None, payload=None):
    responsePayload = payload if payload is not None else {'usage': usage}
    return modelCompletion(message=message, requestPayload={}, responsePayload=responsePayload)


def textTurn(text, prompt, completion, cached=0):
    return [
        textChunk(text=text),
        finalChunk(completion=completionWith(
            chatMessage(role='assistant', content=text),
            snakeUsage(prompt, completion, cached),
        )),
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


def makeAgent(tmpPath, outcomes, tools=None, providerId='volcano', modelId='flash-test'):
    return agent(
        modelAdapter=scriptedAdapter(outcomes, providerId=providerId, modelId=modelId),
        toolDefinitions=tools or [],
        workDir=tmpPath,
        logDir=tmpPath,
        systemPrompt='sys',
    )


def eventTypes(events):
    return [type(event).__name__ for event in events]


def usageEvents(events):
    return [event for event in events if isinstance(event, usageUpdateEvent)]


@pytest.fixture
def isolatedStores(tmp_path, monkeypatch):
    sessionsDir = tmp_path / 'webData'
    sessionsDir.mkdir()
    monkeypatch.setattr(sessionStore, 'webDataDir', sessionsDir)
    monkeypatch.setattr(sessionStore, 'indexPath', sessionsDir / 'sessions.json')
    monkeypatch.setattr(usageLedger, 'dbPath', tmp_path / 'usage.db')
    monkeypatch.setattr(usageLedger, 'logsRoot', tmp_path)
    usageLedger.resetState()
    try:
        yield tmp_path
    finally:
        usageLedger.resetState()


def writeSession(sessionId, workDir, providerId='index-provider', modelId='index-model', lastUsage=None):
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
    if lastUsage is not None:
        session['lastUsage'] = dict(lastUsage)
    sessionStore.saveIndex({sessionId: session})
    return session


def makePumpAgent(sessionId, currentConversation, providerId='volcano', modelId='flash-test'):
    class fakeAgent:
        def __init__(self):
            self.sessionLocksGuard = threading.RLock()
            self.conversations = {sessionId: currentConversation} if currentConversation is not None else {}
            self.modelAdapter = scriptedAdapter([], providerId=providerId, modelId=modelId)

        def interruptActiveStreams(self, sid):
            pass

    return fakeAgent()


def makePump(sessionId, currentConversation, events, **kwargs):
    pump = streamPump(sessionId, makePumpAgent(sessionId, currentConversation, **kwargs), listStream(events))
    return pump


def drainQueue(subscriber):
    items = []
    while True:
        try:
            items.append(subscriber.get_nowait())
        except queue.Empty:
            break
    return items


def finishPump(pump, timeout=5):
    assert pump.doneEvent.wait(timeout=timeout), 'pump did not finish'
    pump.thread.join(timeout=timeout)
    assert not pump.thread.is_alive()


def testUsageUpdateIsNotTerminal() -> None:
    assert usageUpdateEvent not in terminalEventTypes
    assert not issubclass(usageUpdateEvent, terminalEventTypes)


def testCorePlainTextOrder(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('hi', 10, 4, 2)])
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    assert eventTypes(events) == ['textDeltaEvent', 'usageUpdateEvent', 'completedEvent']
    update = usageEvents(events)[0]
    assert update.usage == {'promptTokens': 10, 'cachedTokens': 2, 'completionTokens': 4}
    assert update.stepUsage == {'promptTokens': 10, 'cachedTokens': 2, 'completionTokens': 4}
    assert update.contextTokens == 14
    assert set(update.usage) == set(usageKeys)
    assert 'prompt_tokens' not in update.usage


def testCoreTwoStepsAndValueCopyBaseline(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    first = [
        textChunk(text='call'),
        finalChunk(completion=completionWith(
            chatMessage(role='assistant', content='', toolCalls=[toolCall(id='c1', toolName='echo', arguments={'text': 'x'})]),
            snakeUsage(100, 20, 60),
        )),
    ]
    second = textTurn('done', 80, 10, 10)
    current = makeAgent(tmp_path, [first, second], tools=[echoTool()])
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    updates = usageEvents(events)
    assert len(updates) == 2
    assert updates[0].usage == {'promptTokens': 100, 'cachedTokens': 60, 'completionTokens': 20}
    assert updates[0].stepUsage == {'promptTokens': 100, 'cachedTokens': 60, 'completionTokens': 20}
    assert updates[0].stepUsage['promptTokens'] != 0
    assert updates[1].usage == {'promptTokens': 180, 'cachedTokens': 70, 'completionTokens': 30}
    assert updates[1].stepUsage == {'promptTokens': 80, 'cachedTokens': 10, 'completionTokens': 10}
    names = eventTypes(events)
    assert names.index('usageUpdateEvent') < names.index('toolCallStartEvent')
    assert names[-2:] == ['usageUpdateEvent', 'completedEvent']


def testCoreRetrySharesBaselineAndOneEvent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    fail = modelRequestError('upstream', {'model': 'x'}, statusCode=502)
    current = makeAgent(tmp_path, [fail, textTurn('ok', 12, 3, 1)])
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    updates = usageEvents(events)
    assert len(updates) == 1
    assert any(isinstance(event, retryNoticeEvent) for event in events)
    assert updates[0].stepUsage == {'promptTokens': 12, 'cachedTokens': 1, 'completionTokens': 3}


def testCoreInvalidUsageDoesNotEmit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    cases = [
        None,
        {},
        {'total_tokens': 9},
        {'prompt_tokens': 1},
        {'prompt_tokens': None, 'completion_tokens': 1},
        {'prompt_tokens': '1', 'completion_tokens': 1},
        {'prompt_tokens': 1.5, 'completion_tokens': 1},
        {'prompt_tokens': -1, 'completion_tokens': 1},
        {'prompt_tokens': True, 'completion_tokens': 1},
    ]
    for index, usage in enumerate(cases):
        current = makeAgent(tmp_path, [[finalChunk(completion=completionWith(
            chatMessage(role='assistant', content='x'),
            usage,
        ))]])
        events = list(current.runUserMessageStream('hello', f'sess-{index}'))
        assert usageEvents(events) == []
        assert any(isinstance(event, completedEvent) for event in events)


def testCoreIllegalStringUsageDoesNotEmit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [[finalChunk(completion=completionWith(
        chatMessage(role='assistant', content='x'),
        {'prompt_tokens': 'bad', 'completion_tokens': 1},
    ))]])
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    assert usageEvents(events) == []
    assert any(isinstance(event, completedEvent) for event in events)


def testCoreNonDictPayloadSafeAndNoEvent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [[finalChunk(completion=completionWith(
        chatMessage(role='assistant', content='x'),
        payload='not-a-dict',
    ))]])
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    assert usageEvents(events) == []
    assert any(isinstance(event, completedEvent) for event in events)


def testCoreLegalZeroUsageEmits(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('hi', 0, 0, 0)])
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    update = usageEvents(events)[0]
    assert update.usage == {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}


def testResponsesEmptyUsageNormalizesToLegalZero() -> None:
    normalized = normalizeUsage({})
    assert normalized['prompt_tokens'] == 0
    assert normalized['completion_tokens'] == 0
    assert isinstance(normalized['prompt_tokens'], int)
    assert not isinstance(normalized['prompt_tokens'], bool)


def testCoreCachedIsSubsetNotSubtracted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('hi', 100, 5, 80)])
    update = usageEvents(list(current.runUserMessageStream('hello', 'sess-live')))[0]
    assert update.usage['promptTokens'] == 100
    assert update.usage['cachedTokens'] == 80


def testCoreNoFinalChunkNoUsage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)

    class emptyAdapter(scriptedAdapter):
        def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
            if False:
                yield None
            return

    current = agent(
        modelAdapter=emptyAdapter([]),
        toolDefinitions=[],
        workDir=tmp_path,
        logDir=tmp_path,
        systemPrompt='sys',
    )
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    assert usageEvents(events) == []
    assert any(isinstance(event, errorEvent) for event in events)


def testCoreConfirmationSequences(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    first = [
        textChunk(text='ask'),
        finalChunk(completion=completionWith(
            chatMessage(role='assistant', content='', toolCalls=[toolCall(id='c1', toolName='rm', arguments={'path': '/x'})]),
            snakeUsage(10, 2, 1),
        )),
    ]
    second = textTurn('done', 20, 4, 2)
    current = makeAgent(tmp_path, [first, second], tools=[confirmTool()])
    firstEvents = list(current.runUserMessageStream('hello', 'sess-live'))
    assert [type(event).__name__ for event in firstEvents if isinstance(event, (usageUpdateEvent, confirmationRequiredEvent))] == [
        'usageUpdateEvent',
        'confirmationRequiredEvent',
    ]
    pending = next(event for event in firstEvents if isinstance(event, confirmationRequiredEvent))
    approved = list(current.continueConfirmationStream('sess-live', pending.confirmationId, True))
    assert isinstance(approved[0], toolCallStartEvent)
    assert isinstance(approved[1], toolCallEndEvent)
    approvedUpdates = usageEvents(approved)
    assert len(approvedUpdates) == 1
    assert approvedUpdates[0].stepUsage == {'promptTokens': 20, 'cachedTokens': 2, 'completionTokens': 4}
    assert approvedUpdates[0].usage == {'promptTokens': 30, 'cachedTokens': 3, 'completionTokens': 6}

    current2 = makeAgent(tmp_path, [first, textTurn('no', 7, 1, 0)], tools=[confirmTool()])
    firstReject = list(current2.runUserMessageStream('hello', 'sess-rej'))
    pending2 = next(event for event in firstReject if isinstance(event, confirmationRequiredEvent))
    rejected = list(current2.continueConfirmationStream('sess-rej', pending2.confirmationId, False))
    assert isinstance(rejected[0], toolCallEndEvent)
    assert rejected[0].toolResult.isError is True
    assert usageEvents(rejected)[0].stepUsage == {'promptTokens': 7, 'cachedTokens': 0, 'completionTokens': 1}


def testCodecCoreAndDtoAndUnknown() -> None:
    coreEvent = usageUpdateEvent(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=3,
    )
    name, data = eventToFrame(coreEvent)
    assert name == 'usageUpdate'
    assert data['usage']['promptTokens'] == 1
    assert 'cost' not in data
    dto = usageUpdateDto(usage=coreEvent.usage, stepUsage=coreEvent.stepUsage, contextTokens=3, cost=0.5)
    name2, data2 = eventToFrame(dto)
    assert name2 == 'usageUpdate'
    assert data2['cost'] == 0.5
    nullDto = usageUpdateDto(usage=coreEvent.usage, stepUsage=coreEvent.stepUsage, contextTokens=3, cost=None)
    assert eventToFrame(nullDto)[1]['cost'] is None
    errName, errData = eventToFrame({'foo': 1})
    assert errName == 'error'
    assert errData['errorType'] == 'dict'

    @dataclass
    class anonymous:
        usage: dict

    errName2, _ = eventToFrame(anonymous(usage={}))
    assert errName2 == 'error'


def testPumpInitHasNoCostIo(tmp_path, monkeypatch, isolatedStores) -> None:
    calls = []
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: calls.append('query') or 0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: calls.append('load') or {})
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    pump = makePump('sess-live', currentConversation, [])
    assert calls == []
    assert pump.liveCostState == 'pending'
    assert pump.pumpProviderId == 'volcano'
    assert pump.pumpModelId == 'flash-test'


def testPumpConvertsDtoAndLazyCost(tmp_path, monkeypatch, isolatedStores) -> None:
    order = []
    writeSession('sess-live', tmp_path, lastUsage={'promptTokens': 9, 'cachedTokens': 1, 'completionTokens': 2})
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    currentConversation.usageTotal = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}

    realUpdate = sessionStore.updateUsage

    def updateUsage(sessionId, usage, contextTokens=None, lastUsage=None):
        order.append(('update', lastUsage, dict(usage), contextTokens))
        return realUpdate(sessionId, usage, contextTokens=contextTokens, lastUsage=lastUsage)

    def query(sessionId):
        order.append(('query', sessionId))
        return 0.01

    def load():
        order.append(('load',))
        return {'volcano/flash-test': {'input': 1.0, 'cacheRead': 0.1, 'output': 2.0}}

    monkeypatch.setattr(sessionStore, 'updateUsage', updateUsage)
    monkeypatch.setattr(usageStore, 'querySessionCost', query)
    monkeypatch.setattr(usageStore, 'loadCostMap', load)

    first = usageUpdateEvent(
        usage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        stepUsage={'promptTokens': 100, 'cachedTokens': 10, 'completionTokens': 20},
        contextTokens=120,
    )
    second = usageUpdateEvent(
        usage={'promptTokens': 250, 'cachedTokens': 30, 'completionTokens': 50},
        stepUsage={'promptTokens': 150, 'cachedTokens': 20, 'completionTokens': 30},
        contextTokens=180,
    )
    pump = makePump('sess-live', currentConversation, [first, second, completedEvent(message='ok')])
    pump.startUsage = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
    currentConversation.usageTotal = {'promptTokens': 250, 'cachedTokens': 30, 'completionTokens': 50}
    currentConversation.lastTurnTokens = 180
    pump._pump()
    assert order[0][0] == 'update'
    assert order[0][1] is None
    assert ('query', 'sess-live') in order
    assert order.count(('load',)) == 0
    dtos = [event for event in pump.history if isinstance(event, usageUpdateDto)]
    assert len(dtos) == 2
    assert not any(isinstance(event, usageUpdateEvent) for event in pump.history)
    assert dtos[0].cost == pytest.approx(0.01)
    assert dtos[1].cost == pytest.approx(0.01)
    stored = sessionStore.getSession('sess-live')
    assert stored['usage']['promptTokens'] == 250
    assert stored['lastUsage'] == {'promptTokens': 250, 'cachedTokens': 30, 'completionTokens': 50}


def testPumpStopPresetSkipsDto(tmp_path, monkeypatch, isolatedStores) -> None:
    updates = []
    queries = []

    def countingUpdate(sessionId, usage, contextTokens=None, lastUsage=None):
        if lastUsage is None:
            updates.append(1)

    monkeypatch.setattr(sessionStore, 'updateUsage', countingUpdate)
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: queries.append(1) or 0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    monkeypatch.setattr(usageStore, 'writeUsageTurn', lambda *a, **k: None)
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    event = usageUpdateEvent(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
        contextTokens=2,
    )
    pump = makePump('sess-live', currentConversation, [event, completedEvent(message='x')])
    pump.stopFlag.set()
    pump._pump()
    assert updates == []
    assert queries == []
    assert not any(isinstance(event, usageUpdateDto) for event in pump.history)


def testPumpCostFailureAndMissingPrice(tmp_path, monkeypatch, isolatedStores) -> None:
    writeSession('sess-live', tmp_path)
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    event = usageUpdateEvent(
        usage={'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
        stepUsage={'promptTokens': 10, 'cachedTokens': 0, 'completionTokens': 2},
        contextTokens=12,
    )

    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db')))
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    monkeypatch.setattr(usageStore, 'writeUsageTurn', lambda *a, **k: None)
    pump = makePump('sess-live', currentConversation, [event, completedEvent(message='x')])
    pump._pump()
    dto = next(item for item in pump.history if isinstance(item, usageUpdateDto))
    assert dto.cost is None
    assert pump.liveCostState == 'unavailable'

    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: 0.2)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    pump2 = makePump('sess-live', currentConversation, [event, completedEvent(message='x')])
    pump2._pump()
    dto2 = next(item for item in pump2.history if isinstance(item, usageUpdateDto))
    assert pump2.liveCostState == 'ready'
    assert dto2.cost == pytest.approx(0.2)


def testPumpIndexWriteFailureStillBroadcasts(tmp_path, monkeypatch, isolatedStores) -> None:
    monkeypatch.setattr(sessionStore, 'updateUsage', lambda *a, **k: (_ for _ in ()).throw(OSError('disk')))
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: 0.0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {'volcano/flash-test': {'input': 1, 'cacheRead': 1, 'output': 1}})
    monkeypatch.setattr(usageStore, 'writeUsageTurn', lambda *a, **k: None)
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    event = usageUpdateEvent(
        usage={'promptTokens': 4, 'cachedTokens': 0, 'completionTokens': 1},
        stepUsage={'promptTokens': 4, 'cachedTokens': 0, 'completionTokens': 1},
        contextTokens=5,
    )
    pump = makePump('sess-live', currentConversation, [event, completedEvent(message='x')])
    pump._pump()
    assert any(isinstance(item, usageUpdateDto) for item in pump.history)
    assert any(isinstance(item, completedEvent) for item in pump.history)


def testCompactDeltasKeepsDtoOrder() -> None:
    dto = usageUpdateDto(
        usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
        stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
        contextTokens=2,
        cost=0.1,
    )
    events = [textDeltaEvent(text='a'), textDeltaEvent(text='b'), dto, textDeltaEvent(text='c')]
    compacted = compactDeltas(events)
    assert len(compacted) == 3
    assert compacted[0].text == 'ab'
    assert compacted[1] is dto
    assert compacted[2].text == 'c'


def testSubscribeReplaysDto(tmp_path, monkeypatch, isolatedStores) -> None:
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: 0.0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    monkeypatch.setattr(usageStore, 'writeUsageTurn', lambda *a, **k: None)
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    event = usageUpdateEvent(
        usage={'promptTokens': 3, 'cachedTokens': 0, 'completionTokens': 1},
        stepUsage={'promptTokens': 3, 'cachedTokens': 0, 'completionTokens': 1},
        contextTokens=4,
    )
    pump = makePump('sess-live', currentConversation, [event, completedEvent(message='ok')])
    pump._pump()
    queue = pump.subscribe()
    replayed = []
    while True:
        item = queue.get_nowait()
        replayed.append(item)
        if item is None:
            break
    assert isinstance(replayed[0], usageUpdateDto)
    assert isinstance(replayed[1], completedEvent)
    assert replayed[-1] is None


def testRecordUsageUsesPumpModelAndOnce(tmp_path, monkeypatch, isolatedStores) -> None:
    updates = []
    monkeypatch.setattr(sessionStore, 'updateUsage', lambda *a, **k: updates.append((a, k)))
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    currentConversation.usageTotal = {'promptTokens': 5, 'cachedTokens': 1, 'completionTokens': 2}
    pump = makePump('sess-live', currentConversation, [], providerId='volcano', modelId='actual-model')
    pump.startUsage = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
    assert updates == []
    pump._recordUsage()
    pump._recordUsage()
    assert len(updates) == 1
    assert updates[0][1]['lastUsage'] == {'promptTokens': 5, 'cachedTokens': 1, 'completionTokens': 2}


def testRecordUsageIoErrorAndMissingConversation(tmp_path, monkeypatch, isolatedStores) -> None:
    monkeypatch.setattr(usageStore, 'writeUsageTurn', lambda *a, **k: (_ for _ in ()).throw(OSError('db')))
    monkeypatch.setattr(sessionStore, 'updateUsage', lambda *a, **k: None)
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    pump = makePump('sess-live', currentConversation, [])
    pump._recordUsage()
    assert pump.usageRecordDone.is_set()

    pump2 = makePump('sess-missing', None, [])
    pump2._recordUsage()
    assert pump2.usageRecordDone.is_set()


@pytest.mark.parametrize('owner', ['stop', 'pump'])
@pytest.mark.parametrize('failure', ['none', 'db', 'sessions', 'missing'])
def testRecordUsageOwnerWaiterRace(tmp_path, monkeypatch, isolatedStores, owner, failure) -> None:
    streamStart = threading.Event()
    streamHold = threading.Event()
    ownerGate = threading.Event()
    waiterEntered = threading.Event()
    ownerEntered = threading.Event()
    dtoSeen = threading.Event()
    writes = []
    sessionAttempts = []
    stopResult = {'ok': None}

    def countingUpdate(*args, **kwargs):
        if kwargs.get('lastUsage') is not None:
            if failure != 'missing':
                ownerEntered.set()
                assert ownerGate.wait(timeout=5), 'ownerGate not released for write'
            sessionAttempts.append(kwargs)
            writes.append(args)
            if failure in ('db', 'sessions'):
                raise OSError(failure)

    class gatedConversations:
        def __init__(self, inner):
            self._inner = inner

        def get(self, key, default=None):
            ownerEntered.set()
            assert ownerGate.wait(timeout=5), 'ownerGate not released for conversations.get'
            return self._inner.get(key, default)

    class gatedStream:
        def __iter__(self):
            assert streamStart.wait(timeout=5), 'streamStart not released'
            yield usageUpdateEvent(
                usage={'promptTokens': 8, 'cachedTokens': 0, 'completionTokens': 1},
                stepUsage={'promptTokens': 8, 'cachedTokens': 0, 'completionTokens': 1},
                contextTokens=9,
            )
            if owner == 'stop':
                assert streamHold.wait(timeout=5), 'streamHold not released'
                return
            yield completedEvent(message='ok')

        def close(self):
            pass

    monkeypatch.setattr(sessionStore, 'updateUsage', countingUpdate)
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: 0)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: {})
    originalBroadcast = streamPump._broadcast

    def trackingBroadcast(self, event):
        originalBroadcast(self, event)
        if isinstance(event, usageUpdateDto):
            dtoSeen.set()

    monkeypatch.setattr(streamPump, '_broadcast', trackingBroadcast)

    sessionId = f'sess-{owner}-{failure}'
    currentConversation = None
    if failure != 'missing':
        currentConversation = conversation(sessionId=sessionId, logPath=tmp_path / f'{sessionId}.jsonl', systemPrompt='sys')
        currentConversation.usageTotal = {'promptTokens': 8, 'cachedTokens': 0, 'completionTokens': 1}

    pump = startStream(sessionId, makePumpAgent(sessionId, currentConversation), gatedStream())
    assert pump is not None
    stopper = None
    try:
        originalWait = pump.usageRecordDone.wait

        def wrappedWait(*args, **kwargs):
            waiterEntered.set()
            return originalWait(*args, **kwargs)

        monkeypatch.setattr(pump.usageRecordDone, 'wait', wrappedWait)
        if failure == 'missing':
            pump.agent.conversations = gatedConversations(pump.agent.conversations)
        subscriber = pump.subscribe()

        def stopTarget():
            stopResult['ok'] = requestStop(sessionId)

        stopper = threading.Thread(target=stopTarget, name=f'stopper-{owner}-{failure}', daemon=True)
        streamStart.set()
        if owner == 'stop':
            assert dtoSeen.wait(timeout=5), 'usage DTO not seen before stop owner'
            stopper.start()
            assert ownerEntered.wait(timeout=5), 'stop owner did not enter I/O'
            streamHold.set()
            assert waiterEntered.wait(timeout=5), 'pump waiter did not enter wait'
        else:
            assert ownerEntered.wait(timeout=5), 'pump owner did not enter I/O'
            stopper.start()
            assert waiterEntered.wait(timeout=5), 'stop waiter did not enter wait'
        before = drainQueue(subscriber)
        assert None not in before
        ownerGate.set()
        stopper.join(timeout=5)
        assert not stopper.is_alive()
        finishPump(pump)
        rest = drainQueue(subscriber)
        combined = before + rest
        assert combined.count(None) == 1
        noneAt = combined.index(None)
        assert not any(isinstance(item, usageUpdateDto) for item in combined[noneAt + 1:])
        assert stopResult['ok'] is True
        assert pump.usageRecordDone.is_set()
        assert pump.closed
        assert pump.doneEvent.is_set()
        if failure == 'missing':
            assert writes == []
            assert sessionAttempts == []
        else:
            assert len(writes) == 1
            assert len(sessionAttempts) == 1
    finally:
        streamStart.set()
        streamHold.set()
        ownerGate.set()
        if stopper is not None and stopper.is_alive():
            pump.usageRecordDone.set()
            stopper.join(timeout=2)
        unregisterStream(sessionId)
        if pump.thread.is_alive():
            pump.doneEvent.set()
            pump.usageRecordDone.set()
            pump.thread.join(timeout=2)


def testLongToolBroadcastsBeforeDbWrite(tmp_path, monkeypatch, isolatedStores) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    sessionId = 'sess-tool'
    writeSession(sessionId, tmp_path, lastUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 0})
    released = threading.Event()

    def blockingExecute(args, ctx):
        if not released.wait(timeout=5):
            raise TimeoutError('tool not released')
        return toolOutput(content='done')

    blocking = defineTool(
        name='echo',
        description='echo',
        parameters={'type': 'object', 'properties': {'text': {'type': 'string'}}},
        execute=blockingExecute,
    )
    first = [
        textChunk(text='call'),
        finalChunk(completion=completionWith(
            chatMessage(role='assistant', content='', toolCalls=[toolCall(id='c1', toolName='echo', arguments={'text': 'x'})]),
            snakeUsage(40, 8, 12),
        )),
    ]
    second = textTurn('done', 20, 4, 2)
    current = makeAgent(tmp_path, [first, second], tools=[blocking], providerId='volcano', modelId='flash-test')
    dtoSeen = threading.Event()
    originalBroadcast = streamPump._broadcast

    def trackingBroadcast(self, event):
        originalBroadcast(self, event)
        if isinstance(event, usageUpdateDto):
            dtoSeen.set()

    monkeypatch.setattr(streamPump, '_broadcast', trackingBroadcast)
    pump = startStream(sessionId, current, current.runUserMessageStream('hello', sessionId))
    assert pump is not None
    subscriber = pump.subscribe()
    try:
        assert dtoSeen.wait(timeout=5)
        assert not released.is_set()
        stored = sessionStore.getSession(sessionId)
        assert stored['usage'] == {'promptTokens': 40, 'cachedTokens': 12, 'completionTokens': 8}
        assert stored['lastUsage'] == {'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 0}
        dbPath = tmp_path / 'usage.db'
        if dbPath.exists():
            connection = sqlite3.connect(dbPath)
            try:
                count = connection.execute('SELECT COUNT(*) FROM usageTurns').fetchone()[0]
            finally:
                connection.close()
            assert count == 0
        items = drainQueue(subscriber)
        assert any(isinstance(item, usageUpdateDto) for item in items)
        assert None not in items
        released.set()
        finishPump(pump)
        stored = sessionStore.getSession(sessionId)
        assert stored['usage'] == {'promptTokens': 60, 'cachedTokens': 14, 'completionTokens': 12}
        assert stored['lastUsage'] == {'promptTokens': 60, 'cachedTokens': 14, 'completionTokens': 12}
        connection = sqlite3.connect(dbPath)
        try:
            rows = connection.execute(
                'SELECT providerId, modelId, promptTokens, cachedTokens, completionTokens FROM usageEvents WHERE sessionId = ? ORDER BY occurredAt, createdAt',
                (sessionId,),
            ).fetchall()
        finally:
            connection.close()
        assert rows == [('volcano', 'flash-test', 40, 12, 8), ('volcano', 'flash-test', 20, 2, 4)]
        mapped = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
        for event in jsonlLog(tmp_path / f'{sessionId}.jsonl').readEvents():
            if event.get('type') != 'usageRecord':
                continue
            mapped['promptTokens'] += int(event.get('promptTokens', 0) or 0)
            mapped['cachedTokens'] += int(event.get('cachedTokens', 0) or 0)
            mapped['completionTokens'] += int(event.get('completionTokens', 0) or 0)
        assert mapped == stored['usage']
        rest = drainQueue(subscriber)
        assert rest[-1] is None
    finally:
        released.set()
        unregisterStream(sessionId)
        if pump.thread.is_alive():
            pump.doneEvent.set()
            pump.thread.join(timeout=2)


def testConfirmPumpsRecordOwnIncrements(tmp_path, monkeypatch, isolatedStores) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    sessionId = 'sess-confirm'
    writeSession(sessionId, tmp_path)
    first = [
        textChunk(text='ask'),
        finalChunk(completion=completionWith(
            chatMessage(role='assistant', content='', toolCalls=[toolCall(id='c1', toolName='rm', arguments={'path': '/x'})]),
            snakeUsage(10, 2, 1),
        )),
    ]
    second = textTurn('done', 20, 4, 2)
    current = makeAgent(tmp_path, [first, second], tools=[confirmTool()])
    pump1 = startStream(sessionId, current, current.runUserMessageStream('hello', sessionId))
    assert pump1 is not None
    try:
        finishPump(pump1)
    finally:
        unregisterStream(sessionId)
    pending = next(event for event in pump1.history if isinstance(event, confirmationRequiredEvent))
    connection = sqlite3.connect(tmp_path / 'usage.db')
    try:
        rows1 = connection.execute(
            'SELECT promptTokens, cachedTokens, completionTokens FROM usageEvents WHERE sessionId = ? ORDER BY occurredAt, createdAt',
            (sessionId,),
        ).fetchall()
    finally:
        connection.close()
    assert rows1 == [(10, 1, 2)]

    pump2 = startStream(sessionId, current, current.continueConfirmationStream(sessionId, pending.confirmationId, True))
    assert pump2 is not None
    try:
        finishPump(pump2)
    finally:
        unregisterStream(sessionId)
    connection = sqlite3.connect(tmp_path / 'usage.db')
    try:
        rows2 = connection.execute(
            'SELECT promptTokens, cachedTokens, completionTokens FROM usageEvents WHERE sessionId = ? ORDER BY occurredAt, createdAt',
            (sessionId,),
        ).fetchall()
    finally:
        connection.close()
    assert rows2 == [(10, 1, 2), (20, 2, 4)]

    rejectId = 'sess-rej'
    writeSession(rejectId, tmp_path)
    current2 = makeAgent(tmp_path, [first, textTurn('no', 7, 1, 0)], tools=[confirmTool()])
    pumpReject1 = startStream(rejectId, current2, current2.runUserMessageStream('hello', rejectId))
    assert pumpReject1 is not None
    try:
        finishPump(pumpReject1)
    finally:
        unregisterStream(rejectId)
    pendingReject = next(event for event in pumpReject1.history if isinstance(event, confirmationRequiredEvent))
    pumpReject2 = startStream(rejectId, current2, current2.continueConfirmationStream(rejectId, pendingReject.confirmationId, False))
    assert pumpReject2 is not None
    try:
        finishPump(pumpReject2)
    finally:
        unregisterStream(rejectId)
    connection = sqlite3.connect(tmp_path / 'usage.db')
    try:
        rowsReject = connection.execute(
            'SELECT promptTokens, cachedTokens, completionTokens FROM usageEvents WHERE sessionId = ? ORDER BY occurredAt, createdAt',
            (rejectId,),
        ).fetchall()
    finally:
        connection.close()
    assert rowsReject == [(10, 1, 2), (7, 0, 1)]


def testLiveCostInitFailsOnlyOnceForMultipleSteps(tmp_path, monkeypatch, isolatedStores) -> None:
    writeSession('sess-live', tmp_path)
    logPath = tmp_path / 'sess-live.jsonl'
    currentConversation = conversation(sessionId='sess-live', logPath=logPath, systemPrompt='sys')
    events = [
        usageUpdateEvent(
            usage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
            stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
            contextTokens=2,
        ),
        usageUpdateEvent(
            usage={'promptTokens': 2, 'cachedTokens': 0, 'completionTokens': 2},
            stepUsage={'promptTokens': 1, 'cachedTokens': 0, 'completionTokens': 1},
            contextTokens=3,
        ),
        completedEvent(message='x'),
    ]
    queries = []
    loads = []
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: queries.append(1) or (_ for _ in ()).throw(RuntimeError('db')))
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: loads.append(1) or {})
    monkeypatch.setattr(usageStore, 'writeUsageTurn', lambda *a, **k: None)
    pump = makePump('sess-live', currentConversation, events)
    pump._pump()
    assert queries == [1]
    assert loads == []
    assert pump.liveCostState == 'unavailable'
    dtos = [item for item in pump.history if isinstance(item, usageUpdateDto)]
    assert len(dtos) == 2
    assert dtos[0].cost is None and dtos[1].cost is None

    queries.clear()
    loads.clear()
    monkeypatch.setattr(usageStore, 'querySessionCost', lambda *a, **k: queries.append(1) or 0.2)
    monkeypatch.setattr(usageStore, 'loadCostMap', lambda: loads.append(1) or (_ for _ in ()).throw(RuntimeError('yaml')))
    pump2 = makePump('sess-live', currentConversation, events)
    pump2._pump()
    assert queries == [1, 1, 1]
    assert loads == []
    assert pump2.liveCostState == 'ready'
    dtos2 = [item for item in pump2.history if isinstance(item, usageUpdateDto)]
    assert len(dtos2) == 2
    assert dtos2[0].cost == pytest.approx(0.2) and dtos2[1].cost == pytest.approx(0.2)


def testLiveCostInitializationCounts(tmp_path, monkeypatch, isolatedStores) -> None:
    sessionId = 'sess-cost-counts'
    lastUsage = {'promptTokens': 10, 'cachedTokens': 2, 'completionTokens': 3}
    writeSession(sessionId, tmp_path, lastUsage=lastUsage)
    currentConversation = conversation(sessionId=sessionId, logPath=tmp_path / f'{sessionId}.jsonl', systemPrompt='sys')
    currentConversation.usageTotal = dict(lastUsage)
    calls = {'query': 0}

    def queryCost(sid):
        calls['query'] += 1
        return 0.0042

    monkeypatch.setattr(usageStore, 'querySessionCost', queryCost)
    firstUsage = {'promptTokens': 30, 'cachedTokens': 4, 'completionTokens': 6}
    finalUsage = {'promptTokens': 60, 'cachedTokens': 6, 'completionTokens': 11}
    pump = makePump(sessionId, currentConversation, [
        usageUpdateEvent(firstUsage, {'promptTokens': 20, 'cachedTokens': 2, 'completionTokens': 3}, 26),
        usageUpdateEvent(finalUsage, {'promptTokens': 30, 'cachedTokens': 2, 'completionTokens': 5}, 35),
        completedEvent(message='done'),
    ])
    assert calls == {'query': 0}
    currentConversation.usageTotal = dict(finalUsage)
    currentConversation.lastTurnTokens = 35
    originalBroadcast = pump._broadcast

    def checkBroadcast(event):
        if isinstance(event, usageUpdateDto):
            assert sessionStore.getSession(sessionId)['lastUsage'] == lastUsage
        originalBroadcast(event)

    monkeypatch.setattr(pump, '_broadcast', checkBroadcast)
    pump._pump()
    assert calls['query'] >= 1
    dtos = [event for event in pump.history if isinstance(event, usageUpdateDto)]
    assert [event.cost for event in dtos] == pytest.approx([0.0042, 0.0042])


def testCoreInterruptedAndFailureEmitNoUsage(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)

    class interruptAdapter(scriptedAdapter):
        def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
            raise modelInterruptedError()

    interrupted = agent(
        modelAdapter=interruptAdapter([]),
        toolDefinitions=[],
        workDir=tmp_path,
        logDir=tmp_path,
        systemPrompt='sys',
    )
    interruptedEvents = list(interrupted.runUserMessageStream('hello', 'sess-int'))
    assert usageEvents(interruptedEvents) == []
    assert not any(isinstance(event, errorEvent) for event in interruptedEvents)
    assert not any(isinstance(event, completedEvent) for event in interruptedEvents)

    class failAdapter(scriptedAdapter):
        def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
            raise RuntimeError('provider down')

    failed = agent(
        modelAdapter=failAdapter([]),
        toolDefinitions=[],
        workDir=tmp_path,
        logDir=tmp_path,
        systemPrompt='sys',
    )
    failedEvents = list(failed.runUserMessageStream('hello', 'sess-fail'))
    assert usageEvents(failedEvents) == []
    assert any(isinstance(event, errorEvent) for event in failedEvents)


def eventByType(path, eventType):
    return [event for event in jsonlLog(path).readEvents() if event.get('type') == eventType]


def testCoreWritesUsageRecordAndEvent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('hi', 10, 4, 2)], providerId='volcano', modelId='flash-test')
    events = list(current.runUserMessageStream('hello', 'sess-live'))
    assert usageEvents(events)[0].usage == {'promptTokens': 10, 'cachedTokens': 2, 'completionTokens': 4}
    records = eventByType(tmp_path / 'sess-live.jsonl', 'usageRecord')
    starts = eventByType(tmp_path / 'sess-live.jsonl', 'modelRequestStart')
    assistants = eventByType(tmp_path / 'sess-live.jsonl', 'assistantMessage')
    assert len(records) == 1
    assert starts[0]['usageKey'] == records[0]['usageKey'] == assistants[0]['usageKey']
    assert records[0]['source'] == 'library'
    assert records[0]['providerId'] == 'volcano'
    assert records[0]['modelId'] == 'flash-test'
    connection = sqlite3.connect(usageLedger.dbPath)
    try:
        rows = connection.execute('SELECT usageKey, promptTokens, cachedTokens, completionTokens FROM usageEvents').fetchall()
    finally:
        connection.close()
    assert rows == [(records[0]['usageKey'], 10, 2, 4)]


def testRetryAttemptsUseDifferentKeys(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    fail = modelRequestError('upstream', {'model': 'x'}, statusCode=502)
    current = makeAgent(tmp_path, [fail, textTurn('ok', 12, 3, 1)])
    list(current.runUserMessageStream('hello', 'sess-retry'))
    starts = eventByType(tmp_path / 'sess-retry.jsonl', 'modelRequestStart')
    records = eventByType(tmp_path / 'sess-retry.jsonl', 'usageRecord')
    assert len(starts) == 2
    assert starts[0]['usageKey'] != starts[1]['usageKey']
    assert len(records) == 1
    assert records[0]['usageKey'] == starts[1]['usageKey']


def testResumeDoesNotDoubleCountUsageRecord(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('hi', 8, 2, 1)])
    list(current.runUserMessageStream('hello', 'sess-resume'))
    resumed = conversation(sessionId='sess-resume', logPath=tmp_path / 'sess-resume.jsonl', systemPrompt='sys', resume=True)
    assert resumed.usageTotal == {'promptTokens': 8, 'cachedTokens': 1, 'completionTokens': 2}


def testResumeAssistantUsageKeyWithoutRecordStillCounts(tmp_path) -> None:
    logPath = tmp_path / 'sess-orphan.jsonl'
    logger = jsonlLog(logPath)
    logger.logEvent({'type': 'systemMessage', 'content': 'sys'})
    logger.logEvent({
        'type': 'assistantMessage',
        'content': 'hi',
        'usageKey': 'missing-outbox',
        'usage': {'prompt_tokens': 9, 'completion_tokens': 2, 'prompt_tokens_details': {'cached_tokens': 1}},
    })
    resumed = conversation(sessionId='sess-orphan', logPath=logPath, systemPrompt='sys', resume=True)
    assert resumed.usageTotal == {'promptTokens': 9, 'cachedTokens': 1, 'completionTokens': 2}


def testOutboxFailureStillWritesLedger(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    current = makeAgent(tmp_path, [textTurn('hi', 6, 2, 1)])
    original = conversation.appendUsageRecord

    def failOutbox(self, record):
        raise OSError('disk')

    monkeypatch.setattr(conversation, 'appendUsageRecord', failOutbox)
    events = list(current.runUserMessageStream('hello', 'sess-outbox'))
    monkeypatch.setattr(conversation, 'appendUsageRecord', original)
    assert usageEvents(events)[0].usage == {'promptTokens': 6, 'cachedTokens': 1, 'completionTokens': 2}
    assert eventByType(tmp_path / 'sess-outbox.jsonl', 'usageRecord') == []
    connection = sqlite3.connect(usageLedger.dbPath)
    try:
        rows = connection.execute('SELECT promptTokens, cachedTokens, completionTokens FROM usageEvents').fetchall()
    finally:
        connection.close()
    assert rows == [(6, 1, 2)]
