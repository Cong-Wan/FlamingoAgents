'''
Author: wilbur
Version: 1.2
Date: 2026-09-01
Description: Tests Codex/xAI Responses payloads and headers, SSE event accumulation, arguments.done-only completion, terminal-authoritative recovery, usage, non-retryable protocol/custom-item failures, and one pre-output OAuth 401 refresh retry.
'''

from __future__ import annotations

import json
from typing import Any

import pytest

from flamingoAgents.core.types import chatMessage, finalChunk, reasoningChunk, textChunk
from flamingoAgents.models.chatCompletions import modelRequestError
from flamingoAgents.models.modelAuth import modelAuth
from flamingoAgents.models.modelConfig import modelConfig
from flamingoAgents.models.responsesAdapter import redactSecret, responseState, responsesAdapter


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


class fakeSseResponse:
    def __init__(self, events: list[dict]):
        self.payload = ''.join(
            'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
            for event in events
        ).encode()
        self.done = False

    def read1(self, size):
        if self.done:
            return b''
        self.done = True
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


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


def xaiConfig() -> modelConfig:
    return modelConfig(
        provider='xaiAlias',
        configProviderId='xaiAlias',
        model='grok-test',
        baseUrl='https://api.x.ai/v1',
        apiType='openai-responses',
        authType='oauth',
        authProvider='xai',
        reasoning=True,
        reasoningEffort='high',
    )


def testCodexAndXaiPayloadUrlAndHeaders() -> None:
    resolver = fakeResolver()
    codex = responsesAdapter(codexConfig(), resolver)
    messages = [chatMessage(role='system', content='System'), chatMessage(role='user', content='Hello')]
    tools = [{'type': 'function', 'function': {
        'name': 'read', 'description': 'Read', 'parameters': {'type': 'object', 'properties': {}},
    }}]

    payload = codex.buildRequestPayload(messages, tools, sessionId='s' * 100)
    headers = codex.requestHeaders(resolver.resolve(), sessionId='session-1')

    assert codex.requestUrl() == 'https://chatgpt.com/backend-api/codex/responses'
    assert payload['instructions'] == 'System'
    assert not any(item.get('role') == 'system' for item in payload['input'])
    assert payload['prompt_cache_key'] == 's' * 64
    assert payload['store'] is False
    assert payload['include'] == ['reasoning.encrypted_content']
    assert payload['tools'][0] == {
        'type': 'function', 'name': 'read', 'description': 'Read',
        'parameters': {'type': 'object', 'properties': {}},
    }
    assert headers['chatgpt-account-id'] == 'acct-1'
    assert headers['OpenAI-Beta'] == 'responses=experimental'
    assert headers['originator'] == 'pi'
    assert headers['session-id'] == 'session-1'

    xai = responsesAdapter(xaiConfig(), resolver)
    xaiPayload = xai.buildRequestPayload(messages, [], sessionId='session-2')
    assert xai.requestUrl() == 'https://api.x.ai/v1/responses'
    assert xaiPayload['input'][0] == {'role': 'system', 'content': 'System'}
    assert 'instructions' not in xaiPayload


def testTerminalOutputIsAuthoritativeForAllSupportedItems() -> None:
    state = responseState(codexConfig(), {'model': 'gpt-test'})
    chunks = []
    events = [
        {'type': 'response.created', 'response': {'id': 'resp-1', 'model': 'gpt-test'}},
        {'type': 'response.output_item.added', 'output_index': 0, 'item': {'type': 'reasoning', 'id': 'rs_1', 'summary': []}},
        {'type': 'response.reasoning_summary_text.delta', 'output_index': 0, 'delta': 'Think'},
        {'type': 'response.output_item.done', 'output_index': 0, 'item': {
            'type': 'reasoning', 'id': 'rs_1', 'summary': [{'type': 'summary_text', 'text': 'Think'}], 'status': 'completed',
        }},
        {'type': 'response.output_item.added', 'output_index': 1, 'item': {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': []}},
        {'type': 'response.output_text.delta', 'output_index': 1, 'delta': 'Hi'},
        {'type': 'response.output_item.added', 'output_index': 2, 'item': {
            'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'read', 'arguments': '',
        }},
        {'type': 'response.function_call_arguments.delta', 'output_index': 2, 'delta': '{"path"'},
        {'type': 'response.function_call_arguments.done', 'output_index': 2, 'arguments': '{"path":"partial"}'},
        {'type': 'response.completed', 'response': {
            'id': 'resp-1', 'model': 'gpt-test', 'status': 'completed',
            'output': [
                {'type': 'reasoning', 'id': 'rs_1', 'summary': [{'type': 'summary_text', 'text': 'Think'}], 'encrypted_content': 'encrypted', 'status': 'completed'},
                {'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'Hi!', 'annotations': []}], 'status': 'completed'},
                {'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1', 'name': 'read', 'arguments': '{"path":"final"}', 'status': 'completed'},
            ],
            'usage': {
                'input_tokens': 10,
                'input_tokens_details': {'cached_tokens': 4},
                'output_tokens': 5,
                'output_tokens_details': {'reasoning_tokens': 2},
                'total_tokens': 15,
            },
        }},
    ]
    for event in events:
        chunks.extend(state.processEvent(event))

    completion = state.buildCompletion()

    assert ''.join(chunk.text for chunk in chunks if isinstance(chunk, textChunk)) == 'Hi!'
    assert ''.join(chunk.text for chunk in chunks if isinstance(chunk, reasoningChunk)) == 'Think'
    assert completion.message.content == 'Hi!'
    assert completion.message.toolCalls[0].id == 'call_1'
    assert completion.message.toolCalls[0].arguments == {'path': 'final'}
    responseItems = completion.message.providerData['responseItems']
    assert next(item for item in responseItems if item['type'] == 'reasoning')['encrypted_content'] == 'encrypted'
    assert all('status' not in item for item in responseItems)
    assert completion.responsePayload['usage']['prompt_tokens_details']['cached_tokens'] == 4
    assert completion.responsePayload['usage']['completion_tokens_details']['reasoning_tokens'] == 2


def testTerminalOnlyMessageAndFunctionCallAreRecovered() -> None:
    state = responseState(xaiConfig(), {})
    chunks = list(state.processEvent({
        'type': 'response.completed',
        'response': {
            'status': 'completed',
            'output': [
                {'type': 'message', 'id': 'msg_terminal', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'terminal text'}]},
                {'type': 'function_call', 'id': 'fc_terminal', 'call_id': 'call_terminal', 'name': 'bash', 'arguments': '{"command":"pwd"}'},
            ],
        },
    }))
    completion = state.buildCompletion()

    assert [chunk.text for chunk in chunks if isinstance(chunk, textChunk)] == ['terminal text']
    assert completion.message.content == 'terminal text'
    assert completion.message.toolCalls[0].id == 'call_terminal'


def testFunctionArgumentsDoneSurvivesWithoutLaterItemCopy() -> None:
    state = responseState(codexConfig(), {})
    events = [
        {'type': 'response.output_item.added', 'output_index': 0, 'item': {
            'type': 'function_call', 'id': 'fc_done', 'call_id': 'call_done',
            'name': 'read', 'arguments': '',
        }},
        {'type': 'response.function_call_arguments.done', 'output_index': 0, 'arguments': '{"path":"from-done"}'},
        {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
    ]
    for event in events:
        list(state.processEvent(event))

    completion = state.buildCompletion()

    assert completion.message.toolCalls[0].arguments == {'path': 'from-done'}
    functionItem = next(
        item for item in completion.message.providerData['responseItems']
        if item['type'] == 'function_call'
    )
    assert functionItem['arguments'] == '{"path":"from-done"}'


def testCustomToolAndFailedEventsAreExplicitErrors() -> None:
    state = responseState(codexConfig(), {})
    with pytest.raises(modelRequestError, match='unsupportedResponseItem') as customError:
        list(state.processEvent({
            'type': 'response.output_item.added', 'output_index': 0,
            'item': {'type': 'custom_tool_call', 'id': 'ctc_1'},
        }))
    assert customError.value.retryable is False
    assert 'bearer-secret' not in redactSecret('authorization=Bearer bearer-secret', None)
    with pytest.raises(modelRequestError, match='quota'):
        list(state.processEvent({
            'type': 'response.failed',
            'response': {'error': {'code': 'quota', 'message': 'denied'}},
        }))


def testOauth401RefreshesOnceBeforeAnySseOutput(monkeypatch) -> None:
    resolver = fakeResolver()
    adapter = responsesAdapter(codexConfig(), resolver)
    calls = 0
    response = fakeSseResponse([{
        'type': 'response.completed',
        'response': {'id': 'resp', 'status': 'completed', 'output': []},
    }])

    def fakeOpen(payload, auth, sessionId=None):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise modelRequestError('unauthorized', payload, statusCode=401)
        return response

    monkeypatch.setattr(adapter, 'openRequest', fakeOpen)
    chunks = list(adapter.consumeSseStream({'model': 'gpt-test'}))

    assert calls == 2
    assert resolver.calls == [
        {'forceRefresh': False, 'staleAccess': None},
        {'forceRefresh': True, 'staleAccess': 'stale-token'},
    ]
    assert len([chunk for chunk in chunks if isinstance(chunk, finalChunk)]) == 1
