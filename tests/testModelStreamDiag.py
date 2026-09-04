'''
Author: wilbur
Version: 1.1
Date: 2026-09-02
Description: Covers model stream diagnosis: adapter diag stages/timings, agent modelRequestStart and modelError retry fields, pumpError/sseGenError, resume ignoring unknown types, None headers safety, and diagnosis callbacks that must not mask the original exception.
'''

from __future__ import annotations

import asyncio
import email.message
import io
import json
import queue
import threading
import urllib.error
import urllib.request
from typing import Any

import pytest
from starlette.requests import ClientDisconnect

from flamingoAgents.core.agent import agent
from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.types import chatMessage, errorEvent, finalChunk, retryNoticeEvent, textChunk, textDeltaEvent
from flamingoAgents.models.chatCompletions import (
    chatCompletionsAdapter,
    mergeErrorDiag,
    modelCompletion,
    modelRequestError,
    pickDiagHeaders,
)
from flamingoAgents.models.modelAuth import createModelAuth, modelAuth
from flamingoAgents.models.modelConfig import modelConfig
from flamingoAgents.models.responsesAdapter import responsesAdapter
from flamingoAgents.utils.jsonl import jsonlLog
from webApp.backend.agentManager import streamPump
from webApp.backend.sseCodec import sseGen
from webApp.backend import server as webServer


class fakeSseResponse:
    def __init__(self, body: bytes, headers=None, raiseOnRead=None):
        self.body = body
        self.headers = headers
        self.raiseOnRead = raiseOnRead
        self.offset = 0

    def read1(self, size):
        if self.offset >= len(self.body):
            if self.raiseOnRead is not None:
                raise self.raiseOnRead
            return b''
        chunk = self.body[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk

    def read(self, size=-1):
        if self.raiseOnRead is not None:
            raise self.raiseOnRead
        if size is None or size < 0:
            data = self.body[self.offset:]
            self.offset = len(self.body)
            return data
        return self.read1(size)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class fakeResolver:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def resolve(self, forceRefresh=False, staleAccess=None):
        self.calls.append({'forceRefresh': forceRefresh, 'staleAccess': staleAccess})
        token = 'fresh-token' if forceRefresh else 'stale-token'
        return modelAuth(
            authorizationHeader='Bearer ' + token,
            accessToken=token,
            authProvider='openai-codex',
            headers={'chatgpt-account-id': 'acct-1'},
        )


def chatConfig() -> modelConfig:
    return modelConfig(
        provider='openai',
        model='gpt-test',
        baseUrl='https://api.openai.com/v1',
        apiType='openai-completions',
        authType='api-key',
    )


def codexConfig() -> modelConfig:
    return modelConfig(
        provider='codexAlias',
        configProviderId='codexAlias',
        model='gpt-test',
        baseUrl='https://chatgpt.com/backend-api',
        apiType='openai-codex-responses',
        authType='oauth',
        authProvider='openai-codex',
        reasoning=True,
        reasoningEffort='high',
    )


def makeChatAdapter() -> chatCompletionsAdapter:
    return chatCompletionsAdapter(chatConfig(), createModelAuth('sk-test'))


def makeHeaders(**values) -> email.message.Message:
    headers = email.message.Message()
    for key, value in values.items():
        headers[key] = value
    return headers


def chatSseBody(*payloads: str) -> bytes:
    parts = []
    for payload in payloads:
        parts.append('data: ' + payload + '\n\n')
    return ''.join(parts).encode('utf-8')


def responsesSseBody(events: list[dict]) -> bytes:
    return ''.join('data: ' + json.dumps(event, ensure_ascii=False) + '\n\n' for event in events).encode('utf-8')


def testPickDiagHeadersWhitelistAndTruncate() -> None:
    headers = makeHeaders(**{
        'x-request-id': 'req-1',
        'cf-ray': 'ray-1',
        'x-ratelimit-remaining': '9',
        'server': 'cloudflare',
        'date': 'x' * 400,
    })
    picked = pickDiagHeaders(headers)
    assert picked['x-request-id'] == 'req-1'
    assert picked['cf-ray'] == 'ray-1'
    assert picked['x-ratelimit-remaining'] == '9'
    assert 'server' not in picked
    assert len(picked['date'].encode('utf-8')) <= 256
    assert pickDiagHeaders(None) == {}


def testChatConnectHttpErrorHasStageAndRequestId(monkeypatch) -> None:
    headers = makeHeaders(**{'x-request-id': 'req-502', 'cf-ray': 'ray-502'})
    httpError = urllib.error.HTTPError(
        'https://api.openai.com/v1/chat/completions',
        502,
        'Bad Gateway',
        headers,
        io.BytesIO(b'upstream_error'),
    )

    def fakeUrlopen(*args, **kwargs):
        raise httpError

    monkeypatch.setattr(urllib.request, 'urlopen', fakeUrlopen)
    adapter = makeChatAdapter()
    with pytest.raises(modelRequestError) as raised:
        list(adapter.consumeSseStream({'model': 'gpt-test', 'messages': []}))
    error = raised.value
    assert error.statusCode == 502
    assert error.diag['stage'] == 'connect'
    assert error.diag['exceptionName'] == 'HTTPError'
    assert error.diag['requestId'] == 'req-502'
    assert error.diag['durationMs'] >= 0
    assert error.diag['api'] == 'openai-completions'


def testChatFirstByteTimeout(monkeypatch) -> None:
    response = fakeSseResponse(b'', headers=makeHeaders(**{'x-request-id': 'req-tt'}), raiseOnRead=TimeoutError('timed out'))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: response)
    adapter = makeChatAdapter()
    with pytest.raises(modelRequestError) as raised:
        list(adapter.consumeSseStream({'model': 'gpt-test'}))
    diag = raised.value.diag
    assert diag['stage'] == 'firstByte'
    assert diag['chunks'] == 0
    assert diag['exceptionName'] == 'TimeoutError'
    assert 'ttfbMs' not in diag
    assert diag['requestId'] == 'req-tt'


def testChatStreamReadTimeoutAfterDelta(monkeypatch) -> None:
    body = chatSseBody('{"choices":[{"delta":{"content":"Hi"}}]}')
    response = fakeSseResponse(body, headers=makeHeaders(**{'x-request-id': 'req-sr'}), raiseOnRead=TimeoutError('timed out'))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: response)
    adapter = makeChatAdapter()
    chunks = []
    with pytest.raises(modelRequestError) as raised:
        for chunk in adapter.consumeSseStream({'model': 'gpt-test'}):
            chunks.append(chunk)
    diag = raised.value.diag
    assert any(isinstance(chunk, textChunk) and chunk.text == 'Hi' for chunk in chunks)
    assert diag['stage'] == 'streamRead'
    assert diag['chunks'] >= 1
    assert diag['textChars'] == 2
    assert diag['ttfbMs'] >= 0
    assert diag['exceptionName'] == 'TimeoutError'


def testChatDecodeInvalidJson(monkeypatch) -> None:
    response = fakeSseResponse(chatSseBody('{not-json'), headers=makeHeaders(**{'x-request-id': 'req-dec'}))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: response)
    adapter = makeChatAdapter()
    with pytest.raises(modelRequestError) as raised:
        list(adapter.consumeSseStream({'model': 'gpt-test'}))
    diag = raised.value.diag
    assert diag['stage'] == 'decode'
    assert diag['exceptionName'] == 'JSONDecodeError'


def testChatSuccessTimingsSawDone(monkeypatch) -> None:
    body = chatSseBody('{"choices":[{"delta":{"content":"Hi"}}]}', '[DONE]')
    response = fakeSseResponse(body, headers=makeHeaders(**{'x-request-id': 'req-ok', 'server': 'ignore'}))
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: response)
    adapter = makeChatAdapter()
    chunks = list(adapter.consumeSseStream({'model': 'gpt-test'}))
    completion = next(chunk.completion for chunk in chunks if isinstance(chunk, finalChunk))
    timings = completion.responsePayload['timings']
    assert timings['durationMs'] >= 0
    assert timings['textChars'] == 2
    assert timings['reasoningChars'] == 0
    assert timings['sawDone'] is True
    assert 'responseHeaders' not in completion.responsePayload
    assert 'requestId' not in timings


def testChatSuccessWithoutDone(monkeypatch) -> None:
    body = chatSseBody('{"choices":[{"delta":{"content":"Yo"}}]}')
    response = fakeSseResponse(body, headers=None)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda *args, **kwargs: response)
    adapter = makeChatAdapter()
    chunks = list(adapter.consumeSseStream({'model': 'gpt-test'}))
    completion = next(chunk.completion for chunk in chunks if isinstance(chunk, finalChunk))
    assert completion.message.content == 'Yo'
    assert completion.responsePayload['timings']['sawDone'] is False
    assert completion.responsePayload['timings']['durationMs'] >= 0


def testResponsesStreamEndBeforeTerminal(monkeypatch) -> None:
    response = fakeSseResponse(responsesSseBody([
        {'type': 'response.created', 'response': {'id': 'resp-1', 'model': 'gpt-test'}},
    ]), headers=makeHeaders(**{'x-request-id': 'req-end'}))
    adapter = responsesAdapter(codexConfig(), fakeResolver())
    monkeypatch.setattr(adapter, 'openRequest', lambda payload, auth, sessionId=None: response)
    with pytest.raises(modelRequestError) as raised:
        list(adapter.consumeSseStream({'model': 'gpt-test'}))
    diag = raised.value.diag
    assert diag['stage'] == 'streamEnd'
    assert diag['chunks'] >= 1
    assert diag.get('authRefresh') is None


def testResponsesFailedEventIsDecode(monkeypatch) -> None:
    response = fakeSseResponse(responsesSseBody([
        {'type': 'response.failed', 'response': {'error': {'code': 'quota', 'message': 'denied'}}},
    ]))
    adapter = responsesAdapter(codexConfig(), fakeResolver())
    monkeypatch.setattr(adapter, 'openRequest', lambda payload, auth, sessionId=None: response)
    with pytest.raises(modelRequestError) as raised:
        list(adapter.consumeSseStream({'model': 'gpt-test'}))
    assert raised.value.diag['stage'] == 'decode'


def testResponsesAuthRefreshOnlyOnFailedRetry(monkeypatch) -> None:
    adapter = responsesAdapter(codexConfig(), fakeResolver())

    def fakeOpen(payload, auth, sessionId=None):
        raise modelRequestError('unauthorized', payload, statusCode=401)

    monkeypatch.setattr(adapter, 'openRequest', fakeOpen)
    with pytest.raises(modelRequestError) as raised:
        list(adapter.consumeSseStream({'model': 'gpt-test'}))
    assert raised.value.diag['authRefresh'] is True
    assert raised.value.diag['stage'] == 'connect'


def testResponsesSuccessHasNoAuthRefresh(monkeypatch) -> None:
    resolver = fakeResolver()
    adapter = responsesAdapter(codexConfig(), resolver)
    calls = 0
    response = fakeSseResponse(responsesSseBody([
        {'type': 'response.completed', 'response': {'id': 'resp', 'status': 'completed', 'output': []}},
    ]), headers=None)

    def fakeOpen(payload, auth, sessionId=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise modelRequestError('unauthorized', payload, statusCode=401)
        return response

    monkeypatch.setattr(adapter, 'openRequest', fakeOpen)
    chunks = list(adapter.consumeSseStream({'model': 'gpt-test'}))
    completion = next(chunk.completion for chunk in chunks if isinstance(chunk, finalChunk))
    timings = completion.responsePayload['timings']
    assert timings['sawDone'] is True
    assert timings['durationMs'] >= 0
    assert 'authRefresh' not in timings


class scriptedAdapter:
    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)

    def completeStream(self, messages, tools, stopEvent=None, sessionId=None):
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        if callable(outcome):
            outcome = outcome()
        yield from outcome


def runAgent(tmpPath, outcomes, monkeypatch, sessionId='sess-diag'):
    monkeypatch.setattr('flamingoAgents.core.agent.time.sleep', lambda seconds: None)
    currentAgent = agent(
        modelAdapter=scriptedAdapter(outcomes),
        toolDefinitions=[],
        workDir=tmpPath,
        logDir=tmpPath,
        systemPrompt='sys',
    )
    events = list(currentAgent.runUserMessageStream('hello', sessionId))
    logEvents = jsonlLog(tmpPath / f'{sessionId}.jsonl').readEvents()
    return events, logEvents


def testAgentRetryFieldsAndStartEvents(tmp_path, monkeypatch) -> None:
    fail = modelRequestError('upstream', {'model': 'gpt-test'}, statusCode=502)
    fail.diag = {'stage': 'connect', 'durationMs': 12, 'exceptionName': 'HTTPError', 'requestId': 'req-a'}
    ok = [finalChunk(completion=modelCompletion(
        message=chatMessage(role='assistant', content='ok'),
        requestPayload={},
        responsePayload={'model': 'gpt-test', 'timings': {'durationMs': 3, 'chunks': 1, 'textChars': 2, 'reasoningChars': 0, 'sawDone': True}},
    ))]
    events, logEvents = runAgent(tmp_path, [fail, ok], monkeypatch)
    starts = [event for event in logEvents if event.get('type') == 'modelRequestStart']
    errors = [event for event in logEvents if event.get('type') == 'modelError']
    assistants = [event for event in logEvents if event.get('type') == 'assistantMessage']
    assert [event['attempt'] for event in starts] == [1, 2]
    assert errors[0]['attempt'] == 1
    assert errors[0]['willRetry'] is True
    assert errors[0]['backoffMs'] >= 1000
    assert errors[0]['stage'] == 'connect'
    assert errors[0]['requestId'] == 'req-a'
    assert assistants[0]['timings']['durationMs'] == 3
    assert any(isinstance(event, retryNoticeEvent) for event in events)


def testAgentChunkSeenDoesNotRetry(tmp_path, monkeypatch) -> None:
    cut = modelRequestError('cut', {'model': 'gpt-test'}, statusCode=502)
    cut.diag = {'stage': 'streamRead', 'chunks': 2, 'textChars': 2}

    def streamThenError():
        yield textChunk(text='Hi')
        raise cut

    events, logEvents = runAgent(tmp_path, [streamThenError()], monkeypatch)
    errors = [event for event in logEvents if event.get('type') == 'modelError']
    assert len(errors) == 1
    assert errors[0]['willRetry'] is False
    assert 'backoffMs' not in errors[0]
    assert any(isinstance(event, errorEvent) for event in events)
    assert not any(isinstance(event, retryNoticeEvent) for event in events)


def testResumeIgnoresNewDiagEvents(tmp_path) -> None:
    logPath = tmp_path / 'sess-resume.jsonl'
    logger = jsonlLog(logPath)
    logger.logEvent({'type': 'systemMessage', 'content': 'sys'})
    logger.logEvent({'type': 'modelRequestStart', 'sessionId': 'sess-resume', 'attempt': 1, 'messageCount': 1, 'contextTokens': 0})
    logger.logEvent({'type': 'modelError', 'errorType': 'modelRequestError', 'message': 'x', 'stage': 'connect', 'attempt': 1, 'willRetry': False})
    logger.logEvent({'type': 'pumpError', 'sessionId': 'sess-resume', 'errorType': 'RuntimeError', 'message': 'pump', 'traceback': 'tb'})
    logger.logEvent({'type': 'sseGenError', 'sessionId': 'sess-resume', 'errorType': 'RuntimeError', 'message': 'sse', 'traceback': 'tb'})
    logger.logEvent({
        'type': 'assistantMessage',
        'content': 'hi',
        'toolCalls': [],
        'timings': {'durationMs': 4, 'chunks': 1, 'textChars': 2, 'reasoningChars': 0, 'sawDone': True},
    })
    restored = conversation(sessionId='sess-resume', logPath=logPath, systemPrompt='unused', resume=True)
    assert [message.role for message in restored.messages] == ['system', 'assistant']
    assert restored.messages[1].content == 'hi'


def testPumpErrorWritesJsonl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(streamPump, '_currentUsage', lambda self: {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0})
    monkeypatch.setattr(streamPump, '_recordUsage', lambda self: None)
    logPath = tmp_path / 'sess-pump.jsonl'
    currentConversation = conversation(sessionId='sess-pump', logPath=logPath, systemPrompt='sys')

    class fakeAgent:
        def __init__(self):
            self.sessionLocksGuard = threading.RLock()
            self.conversations = {'sess-pump': currentConversation}

    class boomStream:
        def __iter__(self):
            raise RuntimeError('pump boom')

        def close(self):
            pass

    pump = streamPump('sess-pump', fakeAgent(), boomStream())
    pump._pump()
    events = jsonlLog(logPath).readEvents()
    pumpErrors = [event for event in events if event.get('type') == 'pumpError']
    assert len(pumpErrors) == 1
    assert pumpErrors[0]['errorType'] == 'RuntimeError'
    assert 'pump boom' in pumpErrors[0]['message']
    assert 'traceback' in pumpErrors[0]


class fakePump:
    def __init__(self):
        self.errors = []
        self.unsubscribed = False

    def logSseGenError(self, error):
        self.errors.append(error)

    def unsubscribe(self, eventQueue):
        self.unsubscribed = True


def testSseGenLogsUnexpectedError(monkeypatch) -> None:
    eventQueue = queue.Queue()
    eventQueue.put(textDeltaEvent(text='hi'))
    pump = fakePump()
    monkeypatch.setattr('webApp.backend.sseCodec.encodeSse', lambda event: (_ for _ in ()).throw(RuntimeError('sse boom')))
    gen = sseGen(eventQueue, pump=pump)
    with pytest.raises(RuntimeError, match='sse boom'):
        next(gen)
    assert len(pump.errors) == 1
    assert pump.unsubscribed is True


def testSseGenSkipsClientDisconnectAndGeneratorExit() -> None:
    eventQueue = queue.Queue()
    eventQueue.put(textDeltaEvent(text='hi'))
    pump = fakePump()

    def boom(event):
        raise ClientDisconnect()

    import webApp.backend.sseCodec as sseCodec
    original = sseCodec.encodeSse
    sseCodec.encodeSse = boom
    try:
        gen = sseGen(eventQueue, pump=pump)
        with pytest.raises(ClientDisconnect):
            next(gen)
        assert pump.errors == []
        assert pump.unsubscribed is True
    finally:
        sseCodec.encodeSse = original

    eventQueue2 = queue.Queue()
    eventQueue2.put(textDeltaEvent(text='a'))
    pump2 = fakePump()
    gen2 = sseGen(eventQueue2, pump=pump2)
    assert 'textDelta' in next(gen2)
    gen2.close()
    assert pump2.errors == []
    assert pump2.unsubscribed is True


def testMergeErrorDiagFailureDoesNotMaskError(monkeypatch) -> None:
    error = modelRequestError('upstream', {'model': 'gpt-test'}, statusCode=502)

    def boom(*args, **kwargs):
        raise RuntimeError('diag boom')

    monkeypatch.setattr('flamingoAgents.models.chatCompletions.applyExceptionDiag', boom)
    mergeErrorDiag(error, {'t0': 1.0}, stage='connect', underlying=RuntimeError('x'))
    assert error.statusCode == 502


def testSseGenLogFailureDoesNotMaskOriginal() -> None:
    eventQueue = queue.Queue()
    eventQueue.put(textDeltaEvent(text='hi'))

    class explodingPump(fakePump):
        def logSseGenError(self, error):
            raise RuntimeError('log boom')

    pump = explodingPump()
    import webApp.backend.sseCodec as sseCodec
    original = sseCodec.encodeSse
    sseCodec.encodeSse = lambda event: (_ for _ in ()).throw(RuntimeError('sse boom'))
    try:
        gen = sseGen(eventQueue, pump=pump)
        with pytest.raises(RuntimeError, match='sse boom'):
            next(gen)
        assert pump.unsubscribed is True
    finally:
        sseCodec.encodeSse = original


def testFallbackErrorHandlerPrintsStack(monkeypatch) -> None:
    called = []
    monkeypatch.setattr(webServer.traceback, 'print_exc', lambda: called.append(True))
    response = asyncio.run(webServer.fallbackErrorHandler(None, RuntimeError('hidden')))
    assert called == [True]
    assert response.status_code == 500
