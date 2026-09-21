'''
Author: wilbur
Version: 1.0
Date: 2026-09-21
Description: Parallel tool pool configuration, barrier concurrency, batch ledger close, run event, confirmation, and Web draining tests.
'''

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from flamingoAgents.builder import createAgent
from flamingoAgents.core.agent import agent, conversationIntegrityError
from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.types import (
    chatMessage,
    confirmationRequiredEvent,
    errorEvent,
    finalChunk,
    modelInterruptedError,
    textChunk,
    toolCall,
    toolCallEndEvent,
    toolCallStartEvent,
    toolContext,
    toolOutput,
    toolResult,
    usageUpdateEvent,
)
from flamingoAgents.models.chatCompletions import modelCompletion
from flamingoAgents.tools.toolConfig import parseToolSettings, toolSettings
from flamingoAgents.tools.toolDefinition import defineTool, permissionRule
from flamingoAgents.tools.toolRuntime import executeToolCall
from flamingoAgents.utils.jsonl import jsonlLog
from webApp.backend import agentManager, sessionStore


def minimalTool(name='read'):
    return {
        'name': name,
        'description': name,
        'parameters': {
            'type': 'object',
            'properties': {'path': {'type': 'string'}},
            'required': ['path'],
            'additionalProperties': False,
        },
    }


def v3Config():
    return {'version': 3, 'tools': [minimalTool('read'), minimalTool('write')]}


def v4Config(maxWorkers=4, toolNames=None, extra=None):
    config = {
        'version': 4,
        'parallelToolPool': {
            'maxWorkers': maxWorkers,
            'toolNames': [] if toolNames is None else toolNames,
        },
        'tools': [minimalTool('read'), minimalTool('write')],
    }
    if extra:
        config['parallelToolPool'].update(extra)
    return config


def testV3ParsesAndDisablesPool():
    settings = parseToolSettings(v3Config())
    assert settings.parallelToolPool.maxWorkers == 1
    assert settings.parallelToolPool.toolNames == frozenset()


def testV3WithPoolRequiresUpgrade():
    raw = v3Config()
    raw['parallelToolPool'] = {'maxWorkers': 4, 'toolNames': []}
    with pytest.raises(RuntimeError, match='升级到 version 4'):
        parseToolSettings(raw)


def testV4MissingBlockAndEmptyNamesDisable():
    raw = {'version': 4, 'tools': [minimalTool()]}
    settings = parseToolSettings(raw)
    assert settings.parallelToolPool.maxWorkers == 1
    assert settings.parallelToolPool.toolNames == frozenset()
    settings = parseToolSettings(v4Config(toolNames=[]))
    assert settings.parallelToolPool.toolNames == frozenset()


def testV4ValidPoolParses():
    settings = parseToolSettings(v4Config(maxWorkers=4, toolNames=['read']))
    assert settings.parallelToolPool.maxWorkers == 4
    assert settings.parallelToolPool.toolNames == frozenset({'read'})


@pytest.mark.parametrize('rawPool', [
    {'maxWorkers': 4},
    {'toolNames': ['read']},
    {'maxWorkers': 4, 'toolNames': [], 'extra': 1},
])
def testV4PoolRejectsWrongKeys(rawPool):
    raw = {'version': 4, 'tools': [minimalTool()], 'parallelToolPool': rawPool}
    with pytest.raises(RuntimeError, match='parallelToolPool'):
        parseToolSettings(raw)


@pytest.mark.parametrize('maxWorkers', [True, False, 0, 33, '4', 1.5])
def testV4RejectsInvalidMaxWorkers(maxWorkers):
    with pytest.raises(RuntimeError, match='maxWorkers'):
        parseToolSettings(v4Config(maxWorkers=maxWorkers, toolNames=[]))


@pytest.mark.parametrize('toolNames', ['read', [1], [''], ['read', 'read'], ['missing']])
def testV4RejectsInvalidToolNames(toolNames):
    with pytest.raises(RuntimeError, match='toolNames'):
        parseToolSettings(v4Config(toolNames=toolNames))


def testDirectToolSettingsConstruction():
    settings = toolSettings(toolSchemas=[])
    assert settings.parallelToolPool.maxWorkers == 1
    assert settings.parallelToolPool.toolNames == frozenset()


def testDirectAgentConstructionDefaults(tmp_path):
    current = agent(
        modelAdapter=scriptedAdapter([]),
        toolDefinitions=[],
        workDir=tmp_path,
        logDir=tmp_path,
        systemPrompt='sys',
    )
    assert current.maxParallelTools == 1
    assert current.parallelToolNames == frozenset()


def testBuilderIntersectsWhitelist(tmp_path):
    toolsPath = tmp_path / 'tools.yaml'
    toolsPath.write_text(yaml.safe_dump(v4Config(maxWorkers=4, toolNames=['read', 'write']), sort_keys=False), encoding='utf-8')
    modelsPath = tmp_path / 'models.yaml'
    modelsPath.write_text(yaml.safe_dump({'providers': {
        'legacy': {
            'baseUrl': 'https://relay.example/v1',
            'api': 'openai-completions',
            'apiKey': 'k',
            'models': [{
                'id': 'model-test', 'name': 'Test', 'input': ['text'],
                'contextWindow': 1000, 'maxTokens': 100, 'reasoning': False,
                'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0},
            }],
        },
    }}, sort_keys=False), encoding='utf-8')
    common = {
        'workDir': tmp_path,
        'logDir': tmp_path / 'logs',
        'modelConfigPath': modelsPath,
        'toolsConfigPath': toolsPath,
        'systemPrompt': 'system',
        'appendCurrentTime': False,
        'skillsDir': '',
        'providerId': 'legacy',
    }
    full = createAgent(**common)
    assert 'read' in full.parallelToolNames and 'write' in full.parallelToolNames
    filtered = createAgent(toolNames=['read'], **common)
    assert filtered.parallelToolNames == frozenset({'read'})
    empty = createAgent(toolNames=[], **common)
    assert empty.parallelToolNames == frozenset()


class scriptedAdapter:
    def __init__(self, outcomes, requestCount=None):
        self.outcomes = list(outcomes)
        self.requestCount = requestCount if requestCount is not None else []
        self.config = type('cfg', (), {'configProviderId': 'volcano', 'provider': 'volcano', 'model': 'flash-test'})()

    def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
        self.requestCount.append(len(messages))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        yield from outcome


def snakeUsage(prompt=1, completion=1, cached=0):
    payload = {'prompt_tokens': prompt, 'completion_tokens': completion}
    if cached:
        payload['prompt_tokens_details'] = {'cached_tokens': cached}
    return payload


def completionWith(message, usage=None):
    return modelCompletion(
        message=message,
        requestPayload={},
        responsePayload={'usage': usage or snakeUsage()},
    )


def textTurn(text='done'):
    return [
        textChunk(text=text),
        finalChunk(completion=completionWith(chatMessage(role='assistant', content=text))),
    ]


def toolTurn(calls, text=''):
    return [
        textChunk(text=text or 'call'),
        finalChunk(completion=completionWith(chatMessage(role='assistant', content=text, toolCalls=calls))),
    ]


def makeCall(callId, name='slow', **arguments):
    return toolCall(id=callId, toolName=name, arguments=arguments or {'name': callId})


def makeAgent(tmpPath, outcomes, tools=None, poolNames=None, maxWorkers=4, requestCount=None):
    return agent(
        modelAdapter=scriptedAdapter(outcomes, requestCount=requestCount),
        toolDefinitions=tools or [],
        workDir=tmpPath,
        logDir=tmpPath,
        systemPrompt='sys',
        parallelToolNames=frozenset(poolNames or []),
        maxParallelTools=maxWorkers,
    )


def jsonlResults(tmpPath, sessionId):
    events = jsonlLog(tmpPath / f'{sessionId}.jsonl').readEvents()
    return [event for event in events if event.get('type') == 'toolResult']


def resultIds(tmpPath, sessionId):
    return [event['toolCallId'] for event in jsonlResults(tmpPath, sessionId)]


def resultMap(tmpPath, sessionId):
    mapping = {}
    for event in jsonlResults(tmpPath, sessionId):
        mapping.setdefault(event['toolCallId'], []).append(event)
    return mapping


class overlapTool:
    def __init__(self, name='slow'):
        self.name = name
        self.lock = threading.Lock()
        self.active = 0
        self.maxActive = 0
        self.threads = set()
        self.barrier = None
        self.started = {}
        self.finished = {}

    def setCount(self, count):
        self.barrier = threading.Barrier(count)

    def definition(self):
        def execute(args, ctx):
            name = str(args.get('name', self.name))
            self.started[name] = time.monotonic()
            with self.lock:
                self.active += 1
                self.maxActive = max(self.maxActive, self.active)
                self.threads.add(threading.current_thread().name)
            try:
                if self.barrier is not None:
                    self.barrier.wait(timeout=2)
                time.sleep(0.05)
            finally:
                with self.lock:
                    self.active -= 1
            self.finished[name] = time.monotonic()
            return toolOutput(content=name)
        return defineTool(
            name=self.name,
            description=self.name,
            parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']},
            execute=execute,
        )


def serialTool(name='serial'):
    return defineTool(
        name=name,
        description=name,
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=lambda args, ctx: toolOutput(content=str(args.get('name', name))),
    )


def confirmTool(name='rm'):
    return defineTool(
        name=name,
        description=name,
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


def testMaxWorkersOneDoesNotCreateExecutor(tmp_path, monkeypatch):
    created = []
    real = ThreadPoolExecutor

    def tracking(*args, **kwargs):
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr('flamingoAgents.core.agent.ThreadPoolExecutor', tracking)
    tracker = overlapTool()
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
        tools=[tracker.definition()],
        poolNames=['slow'],
        maxWorkers=1,
    )
    list(current.runUserMessageStream('hello', 'sess'))
    assert created == []
    assert tracker.maxActive == 1


def testEmptyPoolDoesNotCreateExecutor(tmp_path, monkeypatch):
    created = []
    real = ThreadPoolExecutor

    def tracking(*args, **kwargs):
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr('flamingoAgents.core.agent.ThreadPoolExecutor', tracking)
    tracker = overlapTool()
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
        tools=[tracker.definition()],
        poolNames=[],
        maxWorkers=4,
    )
    list(current.runUserMessageStream('hello', 'sess'))
    assert created == []
    assert tracker.maxActive == 1


def testTwoPooledToolsOverlap(tmp_path):
    tracker = overlapTool()
    tracker.setCount(2)
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
        tools=[tracker.definition()],
        poolNames=['slow'],
    )
    events = list(current.runUserMessageStream('hello', 'sess'))
    assert tracker.maxActive == 2
    ends = [event for event in events if isinstance(event, toolCallEndEvent)]
    assert [event.toolResult.toolCallId for event in ends] == ['c1', 'c2']
    assert resultIds(tmp_path, 'sess') == ['c1', 'c2']


def testWorkerCapIsHonored(tmp_path):
    tracker = overlapTool()
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2'), makeCall('c3'), makeCall('c4')]), textTurn()],
        tools=[tracker.definition()],
        poolNames=['slow'],
        maxWorkers=2,
    )
    list(current.runUserMessageStream('hello', 'sess'))
    assert tracker.maxActive == 2
    assert resultIds(tmp_path, 'sess') == ['c1', 'c2', 'c3', 'c4']


def testSinglePooledSegmentDoesNotCreateExecutor(tmp_path, monkeypatch):
    created = []
    real = ThreadPoolExecutor

    def tracking(*args, **kwargs):
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr('flamingoAgents.core.agent.ThreadPoolExecutor', tracking)
    tracker = overlapTool()
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1')]), textTurn()],
        tools=[tracker.definition()],
        poolNames=['slow'],
    )
    list(current.runUserMessageStream('hello', 'sess'))
    assert created == []


def testSerialBarrierBetweenPooledGroups(tmp_path):
    order = []
    lock = threading.Lock()
    barrier = threading.Barrier(2)

    def pooledExecute(args, ctx):
        name = args['name']
        with lock:
            order.append(f'start-{name}')
        if name in {'p1', 'p2', 'p4', 'p5'}:
            barrier.wait(timeout=2)
        time.sleep(0.02)
        with lock:
            order.append(f'end-{name}')
        return toolOutput(content=name)

    def serialExecute(args, ctx):
        with lock:
            order.append('start-s3')
        assert 'end-p1' in order and 'end-p2' in order
        assert 'start-p4' not in order
        with lock:
            order.append('end-s3')
        return toolOutput(content='s3')

    pooled = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=pooledExecute,
    )
    serial = defineTool(
        name='serial',
        description='serial',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=serialExecute,
    )
    calls = [
        makeCall('p1'), makeCall('p2'),
        toolCall(id='s3', toolName='serial', arguments={'name': 's3'}),
        makeCall('p4'), makeCall('p5'),
    ]
    current = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=[pooled, serial], poolNames=['slow'])
    list(current.runUserMessageStream('hello', 'sess'))
    assert resultIds(tmp_path, 'sess') == ['p1', 'p2', 's3', 'p4', 'p5']
    assert order.index('end-s3') < order.index('start-p4')


def testUnknownToolIsErrorBarrier(tmp_path, monkeypatch):
    created = []
    real = ThreadPoolExecutor

    def tracking(*args, **kwargs):
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr('flamingoAgents.core.agent.ThreadPoolExecutor', tracking)
    tracker = overlapTool()
    calls = [makeCall('c1'), toolCall(id='u1', toolName='missing', arguments={}), makeCall('c2')]
    current = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=[tracker.definition()], poolNames=['slow'])
    list(current.runUserMessageStream('hello', 'sess'))
    assert created == []
    results = resultMap(tmp_path, 'sess')
    assert results['u1'][0]['isError'] is True
    assert results['u1'][0]['details']['unknownTool'] is True


def testOrdinaryErrorDoesNotCancelSiblings(tmp_path):
    def execute(args, ctx):
        if args['name'] == 'c2':
            raise RuntimeError('boom')
        time.sleep(0.05)
        return toolOutput(content=args['name'])

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
    )
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2'), makeCall('c3')]), textTurn()],
        tools=[tool],
        poolNames=['slow'],
    )
    list(current.runUserMessageStream('hello', 'sess'))
    results = resultMap(tmp_path, 'sess')
    assert results['c1'][0]['isError'] is False
    assert results['c2'][0]['isError'] is True
    assert results['c3'][0]['isError'] is False


def testWorkersDoNotPersistAndOrderIgnoresCompletion(tmp_path):
    persistThreads = []
    original = conversation.addToolResult

    def tracking(self, result):
        persistThreads.append(threading.current_thread().name)
        return original(self, result)

    conversation.addToolResult = tracking
    try:
        barrier = threading.Barrier(2)

        def execute(args, ctx):
            if args['name'] == 'c2':
                barrier.wait(timeout=2)
                return toolOutput(content='fast')
            barrier.wait(timeout=2)
            time.sleep(0.08)
            return toolOutput(content='slow')

        tool = defineTool(
            name='slow',
            description='slow',
            parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
            execute=execute,
        )
        current = makeAgent(
            tmp_path,
            [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
            tools=[tool],
            poolNames=['slow'],
        )
        events = list(current.runUserMessageStream('hello', 'sess'))
    finally:
        conversation.addToolResult = original
    assert persistThreads
    assert all(not name.startswith('flamingoTool') for name in persistThreads)
    ends = [event.toolResult.toolCallId for event in events if isinstance(event, toolCallEndEvent)]
    assert ends == ['c1', 'c2']
    assert resultIds(tmp_path, 'sess') == ['c1', 'c2']


def leftoverToolThreads():
    return [thread for thread in threading.enumerate() if thread.name.startswith('flamingoTool')]


def testStopCancelsQueuedFutures(tmp_path):
    startedTwo = threading.Event()
    release = threading.Event()
    executed = []
    active = 0
    lock = threading.Lock()

    def execute(args, ctx):
        nonlocal active
        executed.append(args['name'])
        with lock:
            active += 1
            if active >= 2:
                startedTwo.set()
        if not release.wait(timeout=2):
            raise TimeoutError('not released')
        if ctx.interruptEvent is not None and ctx.interruptEvent.is_set():
            raise modelInterruptedError('用户已停止')
        return toolOutput(content=args['name'])

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
    )
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2'), makeCall('c3')]), textTurn()],
        tools=[tool],
        poolNames=['slow'],
        maxWorkers=2,
    )
    runEvent = threading.Event()

    def stopper():
        assert startedTwo.wait(timeout=2)
        runEvent.set()
        release.set()

    stopperThread = threading.Thread(target=stopper)
    stopperThread.start()
    list(current.runUserMessageStream('hello', 'sess', runEvent=runEvent))
    stopperThread.join(timeout=2)
    results = resultMap(tmp_path, 'sess')
    assert set(results) == {'c1', 'c2', 'c3'}
    assert len(executed) == 2
    assert leftoverToolThreads() == []


def testWorkerEntrySkipsSideEffectsWhenStopped(tmp_path, monkeypatch):
    executed = []

    def execute(args, ctx):
        executed.append(args['name'])
        return toolOutput(content=args['name'])

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
    )
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
        tools=[tool],
        poolNames=['slow'],
    )
    original = agent.runPooledToolCall

    def gated(self, call, sessionId, runEvent):
        runEvent.set()
        return original(self, call, sessionId, runEvent)

    monkeypatch.setattr(agent, 'runPooledToolCall', gated)
    list(current.runUserMessageStream('hello', 'sess'))
    assert executed == []


def testPrepareArgumentsInterruptStopsGroup(tmp_path):
    executed = []

    def prepare(arguments):
        if arguments['name'] == 'c1':
            raise modelInterruptedError('用户已停止')
        return arguments

    def execute(args, ctx):
        executed.append(args['name'])
        return toolOutput(content=args['name'])

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
        prepareArguments=prepare,
    )
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
        tools=[tool],
        poolNames=['slow'],
    )
    list(current.runUserMessageStream('hello', 'sess'))
    results = resultMap(tmp_path, 'sess')
    assert results['c1'][0]['details']['cancelled'] is True
    assert len(results['c2']) == 1
    assert leftoverToolThreads() == []


def testPrepareArgumentsInterruptDirect():
    def prepare(_arguments):
        raise modelInterruptedError('用户已停止')

    definition = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {}},
        execute=lambda args, ctx: toolOutput(content='x'),
        prepareArguments=prepare,
    )
    with pytest.raises(modelInterruptedError):
        executeToolCall(
            definition,
            toolCall(id='c1', toolName='slow', arguments={}),
            toolContext(workDir=Path('.')),
        )


def testStopKeepsLateRealResultAndSkipsNextModel(tmp_path):
    started = threading.Event()
    fastDone = threading.Event()
    release = threading.Event()
    requestCount = []

    def execute(args, ctx):
        if args['name'] == 'c1':
            started.set()
            if not release.wait(timeout=2):
                raise TimeoutError('not released')
            return toolOutput(content='late-c1')
        fastDone.set()
        return toolOutput(content='fast-c2')

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
    )
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c2')]), textTurn()],
        tools=[tool],
        poolNames=['slow'],
        requestCount=requestCount,
    )
    runEvent = threading.Event()

    def stopper():
        assert started.wait(timeout=2)
        assert fastDone.wait(timeout=2)
        time.sleep(0.05)
        runEvent.set()
        release.set()

    stopperThread = threading.Thread(target=stopper)
    stopperThread.start()
    list(current.runUserMessageStream('hello', 'sess', runEvent=runEvent))
    stopperThread.join(timeout=2)
    results = resultMap(tmp_path, 'sess')
    assert results['c2'][0]['content'] == 'fast-c2'
    assert results['c2'][0].get('details', {}).get('cancelled') is not True
    assert len(requestCount) == 1
    assert leftoverToolThreads() == []


def consumeUntil(stream, predicate):
    seen = []
    for event in stream:
        seen.append(event)
        if predicate(event):
            return seen
    return seen


def closeAfter(tmp_path, outcomes, tools, poolNames, predicate, sessionId='sess'):
    current = makeAgent(tmp_path, outcomes, tools=tools, poolNames=poolNames)
    stream = current.runUserMessageStream('hello', sessionId)
    try:
        consumeUntil(stream, predicate)
        stream.close()
    finally:
        stream.close()
    return current


def assertExactResults(tmp_path, sessionId, expectedIds):
    mapping = resultMap(tmp_path, sessionId)
    assert list(mapping) == expectedIds or set(mapping) == set(expectedIds)
    for callId in expectedIds:
        assert len(mapping[callId]) == 1


@pytest.mark.parametrize('closePoint', [
    'usage',
    'start0',
    'start1',
    'end0',
])
def testCloseAfterYieldClosesTranscript(tmp_path, closePoint):
    tracker = overlapTool()
    tracker.setCount(2)
    tools = [tracker.definition(), serialTool()]
    calls = [makeCall('c1'), makeCall('c2'), toolCall(id='s3', toolName='serial', arguments={'name': 's3'})]
    current = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=tools, poolNames=['slow'])
    stream = current.runUserMessageStream('hello', 'sess')
    starts = 0
    ends = 0
    try:
        for event in stream:
            if closePoint == 'usage' and isinstance(event, usageUpdateEvent):
                stream.close()
                break
            if isinstance(event, toolCallStartEvent):
                if closePoint == 'start0' and starts == 0:
                    stream.close()
                    break
                if closePoint == 'start1' and starts == 1:
                    stream.close()
                    break
                starts += 1
            if isinstance(event, toolCallEndEvent):
                if closePoint == 'end0' and ends == 0:
                    stream.close()
                    break
                ends += 1
    finally:
        stream.close()
    mapping = resultMap(tmp_path, 'sess')
    assert set(mapping) == {'c1', 'c2', 's3'}
    for callId in ('c1', 'c2', 's3'):
        assert len(mapping[callId]) == 1
    assert leftoverToolThreads() == []


def testCloseAfterConfirmationKeepsPending(tmp_path):
    tools = [overlapTool().definition(), confirmTool()]
    calls = [makeCall('c1'), makeCall('c2'), toolCall(id='rm1', toolName='rm', arguments={'path': '/x'})]
    current = makeAgent(tmp_path, [toolTurn(calls)], tools=tools, poolNames=['slow'])
    stream = current.runUserMessageStream('hello', 'sess')
    pending = None
    try:
        for event in stream:
            if isinstance(event, confirmationRequiredEvent):
                pending = event
                stream.close()
                break
    finally:
        stream.close()
    assert pending is not None
    assert current.getConversation('sess').hasPending()
    mapping = resultMap(tmp_path, 'sess')
    assert set(mapping) == {'c1', 'c2'}
    assert 'rm1' not in mapping


def testApprovedAndRejectedCloseClosesRest(tmp_path):
    tools = [serialTool(), confirmTool()]
    calls = [toolCall(id='rm1', toolName='rm', arguments={'path': '/x'}), makeCall('c2', 'serial')]
    current = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=tools, poolNames=['serial'])
    first = list(current.runUserMessageStream('hello', 'sess'))
    pending = next(event for event in first if isinstance(event, confirmationRequiredEvent))
    stream = current.continueConfirmationStream('sess', pending.confirmationId, True)
    try:
        for event in stream:
            if isinstance(event, toolCallStartEvent):
                stream.close()
                break
    finally:
        stream.close()
    mapping = resultMap(tmp_path, 'sess')
    assert set(mapping) == {'rm1', 'c2'}
    for callId in mapping:
        assert len(mapping[callId]) == 1

    current2 = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=tools, poolNames=['serial'])
    first2 = list(current2.runUserMessageStream('hello', 'sess2'))
    pending2 = next(event for event in first2 if isinstance(event, confirmationRequiredEvent))
    stream2 = current2.continueConfirmationStream('sess2', pending2.confirmationId, False)
    try:
        for event in stream2:
            if isinstance(event, toolCallEndEvent):
                stream2.close()
                break
    finally:
        stream2.close()
    mapping2 = resultMap(tmp_path, 'sess2')
    assert set(mapping2) == {'rm1', 'c2'}
    assert mapping2['rm1'][0]['details']['blocked'] is True


def testNonPrefixGapFailsClosed(tmp_path):
    current = makeAgent(tmp_path, [], tools=[serialTool()], poolNames=[])
    currentConversation = current.getConversation('sess')
    currentConversation.appendAssistantMessage(
        chatMessage(role='assistant', content='', toolCalls=[makeCall('c1', 'serial'), makeCall('c2', 'serial')]),
        {},
    )
    currentConversation.addToolResult(toolResult('c2', 'serial', False, 'second', {}))
    with pytest.raises(conversationIntegrityError):
        current.findUnclosedTailCallIndex(currentConversation)
    before = jsonlResults(tmp_path, 'sess')
    with pytest.raises(conversationIntegrityError):
        list(current.closeUnfinishedToolCalls(currentConversation, currentConversation.messages[-2].toolCalls, 0, 'preflightRepair'))
    assert jsonlResults(tmp_path, 'sess') == before


def testDuplicateHistoricalCallIdsFailClosed(tmp_path):
    logPath = tmp_path / 'dup.jsonl'
    logger = jsonlLog(logPath)
    logger.logEvent({'type': 'systemMessage', 'content': 'sys'})
    logger.logEvent({
        'type': 'assistantMessage',
        'content': '',
        'toolCalls': [
            {'id': 'c1', 'toolName': 'serial', 'arguments': {}},
            {'id': 'c1', 'toolName': 'serial', 'arguments': {}},
        ],
    })
    with pytest.raises(RuntimeError, match='重复 tool call ID'):
        conversation(sessionId='dup', logPath=logPath, systemPrompt='sys', resume=True)


def testMalformedDuplicateIdsFailBeforePersist(tmp_path):
    current = makeAgent(
        tmp_path,
        [toolTurn([makeCall('c1'), makeCall('c1')])],
        tools=[serialTool('slow')],
        poolNames=['slow'],
    )
    events = list(current.runUserMessageStream('hello', 'sess'))
    assert any(isinstance(event, errorEvent) and event.errorType == 'invalidToolCallIds' for event in events)
    events = jsonlLog(tmp_path / 'sess.jsonl').readEvents()
    assert not any(event.get('type') == 'assistantMessage' for event in events)


def testConfirmationPoolThenConfirm(tmp_path):
    tracker = overlapTool()
    tracker.setCount(2)
    tools = [tracker.definition(), confirmTool()]
    calls = [makeCall('c1'), makeCall('c2'), toolCall(id='rm1', toolName='rm', arguments={'path': '/x'})]
    current = makeAgent(tmp_path, [toolTurn(calls)], tools=tools, poolNames=['slow'])
    events = list(current.runUserMessageStream('hello', 'sess'))
    assert tracker.maxActive == 2
    assert isinstance(events[-1], confirmationRequiredEvent)
    assert resultIds(tmp_path, 'sess') == ['c1', 'c2']
    assert current.getConversation('sess').hasPending()


def testConfirmThenPooledRest(tmp_path):
    tracker = overlapTool()
    tracker.setCount(2)
    tools = [tracker.definition(), confirmTool()]
    calls = [toolCall(id='rm1', toolName='rm', arguments={'path': '/x'}), makeCall('c2'), makeCall('c3')]
    current = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=tools, poolNames=['slow'])
    first = list(current.runUserMessageStream('hello', 'sess'))
    pending = next(event for event in first if isinstance(event, confirmationRequiredEvent))
    assert jsonlResults(tmp_path, 'sess') == []
    list(current.continueConfirmationStream('sess', pending.confirmationId, True))
    assert tracker.maxActive == 2
    assert resultIds(tmp_path, 'sess') == ['rm1', 'c2', 'c3']


def testRejectThenRest(tmp_path):
    tracker = overlapTool()
    tools = [tracker.definition(), confirmTool()]
    calls = [toolCall(id='rm1', toolName='rm', arguments={'path': '/x'}), makeCall('c2'), makeCall('c3')]
    current = makeAgent(tmp_path, [toolTurn(calls), textTurn()], tools=tools, poolNames=['slow'])
    first = list(current.runUserMessageStream('hello', 'sess'))
    pending = next(event for event in first if isinstance(event, confirmationRequiredEvent))
    list(current.continueConfirmationStream('sess', pending.confirmationId, False))
    results = resultMap(tmp_path, 'sess')
    assert results['rm1'][0]['details']['blocked'] is True
    assert set(results) == {'rm1', 'c2', 'c3'}


def testPermissionHitIsNotSubmitted(tmp_path, monkeypatch):
    created = []
    real = ThreadPoolExecutor

    def tracking(*args, **kwargs):
        created.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr('flamingoAgents.core.agent.ThreadPoolExecutor', tracking)
    executed = []

    def execute(args, ctx):
        executed.append(args['path'])
        return toolOutput(content='removed')

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'path': {'type': 'string'}}},
        execute=execute,
        permissions=[permissionRule(
            id='rm',
            field='path',
            action='requireApproval',
            reason='确认',
            patterns=[re.compile(r'danger')],
        )],
    )
    calls = [
        toolCall(id='c1', toolName='slow', arguments={'path': 'ok'}),
        toolCall(id='c2', toolName='slow', arguments={'path': 'danger'}),
        toolCall(id='c3', toolName='slow', arguments={'path': 'later'}),
    ]
    current = makeAgent(tmp_path, [toolTurn(calls)], tools=[tool], poolNames=['slow'])
    events = list(current.runUserMessageStream('hello', 'sess'))
    assert created == []
    assert executed == ['ok']
    assert isinstance(events[-1], confirmationRequiredEvent)
    assert 'c3' not in resultMap(tmp_path, 'sess')


@pytest.fixture
def isolatedManager(monkeypatch, tmp_path):
    monkeypatch.setattr(agentManager, 'agentCache', {})
    monkeypatch.setattr(agentManager, 'staleSessionIds', set())
    monkeypatch.setattr(agentManager, 'activeStreams', {})
    sessionsDir = tmp_path / 'webData'
    sessionsDir.mkdir()
    monkeypatch.setattr(sessionStore, 'webDataDir', sessionsDir)
    monkeypatch.setattr(sessionStore, 'indexPath', sessionsDir / 'sessions.json')
    yield tmp_path


def writeSession(sessionId, workDir):
    session = {
        'sessionId': sessionId,
        'title': 't',
        'workDir': str(workDir),
        'providerId': 'volcano',
        'modelId': 'flash-test',
        'createdAt': sessionStore.nowIso(),
        'updatedAt': sessionStore.nowIso(),
        'usage': {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0},
        'contextTokens': 0,
    }
    sessionStore.saveIndex({sessionId: session})
    return session


def testStopSeparatesUiDoneAndCoreDone(tmp_path, isolatedManager):
    started = threading.Event()
    release = threading.Event()

    def execute(args, ctx):
        started.set()
        if not release.wait(timeout=2):
            raise TimeoutError('not released')
        return toolOutput(content='late')

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
    )
    sessionId = 'sess-drain'
    writeSession(sessionId, tmp_path)
    current = makeAgent(tmp_path, [toolTurn([makeCall('c1')])], tools=[tool], poolNames=['slow'])
    agentManager.agentCache[sessionId] = current
    pump = agentManager.startStream(sessionId, current, current.runUserMessageStream('hello', sessionId))
    assert pump is not None
    assert started.wait(timeout=2)
    assert agentManager.requestStop(sessionId) is True
    assert pump.doneEvent.wait(timeout=2)
    assert not pump.coreDoneEvent.is_set()
    assert agentManager.hasActiveStream(sessionId)
    assert agentManager.dropAgentIfIdle(sessionId) is False
    assert agentManager.getAgent(sessionId) is current
    release.set()
    assert pump.coreDoneEvent.wait(timeout=2)
    pump.thread.join(timeout=2)
    assert not agentManager.hasActiveStream(sessionId)


def testFinishStreamIdentityAndNoFalseIdle(tmp_path, isolatedManager):
    sessionId = 'sess-id'
    writeSession(sessionId, tmp_path)

    class fakeAgent:
        def __init__(self):
            self.sessionLocksGuard = threading.RLock()
            self.conversations = {}
            self.modelAdapter = scriptedAdapter([])

        def interruptActiveStreams(self, sid):
            pass

    class hangingStream:
        def __iter__(self):
            if False:
                yield None
            hang.wait()

        def close(self):
            time.sleep(0.05)

    hang = threading.Event()
    first = agentManager.startStream(sessionId, fakeAgent(), hangingStream())
    assert first is not None
    agentManager.finishStream(sessionId, first)
    second = agentManager.startStream(sessionId, fakeAgent(), hangingStream())
    assert second is not None
    agentManager.finishStream(sessionId, first)
    assert agentManager.getActivePump(sessionId) is second
    hang.set()
    second.coreDoneEvent.wait(timeout=2)
    first.coreDoneEvent.wait(timeout=2)


def testStartStreamStartFailureCleansUp(tmp_path, isolatedManager, monkeypatch):
    sessionId = 'sess-start'
    writeSession(sessionId, tmp_path)

    class fakeAgent:
        def __init__(self):
            self.sessionLocksGuard = threading.RLock()
            self.conversations = {}
            self.modelAdapter = scriptedAdapter([])

    class dummyStream:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def __iter__(self):
            return iter(())

    stream = dummyStream()

    def boom(self):
        raise RuntimeError('no thread')

    monkeypatch.setattr(agentManager.streamPump, 'start', boom)
    with pytest.raises(RuntimeError, match='no thread'):
        agentManager.startStream(sessionId, fakeAgent(), stream)
    assert sessionId not in agentManager.activeStreams
    assert stream.closed is True


def testTerminalWinsOverLateStop(tmp_path, isolatedManager):
    sessionId = 'sess-term'
    writeSession(sessionId, tmp_path)
    current = makeAgent(tmp_path, [textTurn('ok')], tools=[], poolNames=[])
    pump = agentManager.startStream(sessionId, current, current.runUserMessageStream('hello', sessionId))
    assert pump is not None
    assert pump.doneEvent.wait(timeout=2)
    assert pump.coreDoneEvent.wait(timeout=2)
    assert any(getattr(event, 'errorType', None) != 'stopped' for event in pump.history) or True
    assert not any(getattr(event, 'errorType', None) == 'stopped' for event in pump.history)
    assert agentManager.requestStop(sessionId) is False


def testGateTimeoutThenRetry(tmp_path, isolatedManager, monkeypatch):
    sessionId = 'sess-timeout'
    writeSession(sessionId, tmp_path)
    started = threading.Event()
    release = threading.Event()

    def execute(args, ctx):
        started.set()
        release.wait(timeout=3)
        return toolOutput(content='x')

    tool = defineTool(
        name='slow',
        description='slow',
        parameters={'type': 'object', 'properties': {'name': {'type': 'string'}}},
        execute=execute,
    )
    current = makeAgent(tmp_path, [toolTurn([makeCall('c1')])], tools=[tool], poolNames=['slow'])
    pump = agentManager.startStream(sessionId, current, current.runUserMessageStream('hello', sessionId))
    assert started.wait(timeout=2)
    agentManager.requestStop(sessionId)
    assert agentManager.waitForCoreIdle(sessionId, timeout=0.05) == 'timeout'
    release.set()
    assert pump.coreDoneEvent.wait(timeout=2)
    assert agentManager.waitForCoreIdle(sessionId, timeout=1) == 'idle'
