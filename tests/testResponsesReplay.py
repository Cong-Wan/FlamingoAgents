'''
Author: wilbur
Version: 1.1
Date: 2026-09-02
Description: Verifies Responses item whitelist replay, encrypted reasoning JSONL round-trip, exact canonical matching, cross-model call/output pairing, orphan degradation, and legacy log compatibility. v1.1 covers reasoning.summary always present on persist/replay even when empty.
'''

from __future__ import annotations

import json
from pathlib import Path

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.types import chatMessage, toolCall, toolResult
from flamingoAgents.models.modelAuth import modelAuth
from flamingoAgents.models.modelConfig import modelConfig
from flamingoAgents.models.responsesAdapter import responsesAdapter, serializeResponseItem


class staticResolver:
    def resolve(self, forceRefresh=False, staleAccess=None):
        return modelAuth(authorizationHeader='Bearer hidden', accessToken='hidden')


def makeConfig(model: str = 'gpt-one', configProviderId: str = 'aliasA') -> modelConfig:
    return modelConfig(
        provider=configProviderId,
        configProviderId=configProviderId,
        model=model,
        baseUrl='https://chatgpt.com/backend-api',
        apiType='openai-codex-responses',
        authType='oauth',
        authProvider='openai-codex',
        reasoning=True,
    )


def exactAssistant() -> chatMessage:
    return chatMessage(
        role='assistant',
        content='answer',
        toolCalls=[toolCall(
            id='call_1', toolName='read', arguments={'path': 'a'}, providerData={'itemId': 'fc_1'},
        )],
        providerData={
            'api': 'openai-codex-responses',
            'authProvider': 'openai-codex',
            'configProviderId': 'aliasA',
            'model': 'gpt-one',
            'responseItems': [
                {
                    'type': 'reasoning', 'id': 'rs_1',
                    'summary': [{'type': 'summary_text', 'text': 'thought'}],
                    'encrypted_content': 'cipher', 'status': 'completed', 'future_field': 'drop',
                },
                {
                    'type': 'message', 'id': 'msg_1', 'role': 'assistant',
                    'content': [{'type': 'output_text', 'text': 'answer', 'annotations': []}],
                    'status': 'completed', 'future_field': 'drop',
                },
                {
                    'type': 'function_call', 'id': 'fc_1', 'call_id': 'call_1',
                    'name': 'read', 'arguments': '{"path":"a"}', 'status': 'completed',
                },
            ],
        },
    )


def testSerializerDropsResponseOnlyAndUnknownFields() -> None:
    item = serializeResponseItem({
        'type': 'message', 'id': 'msg_1', 'role': 'assistant', 'status': 'completed',
        'future': 'drop',
        'content': [{'type': 'output_text', 'text': 'ok', 'annotations': [{'x': 1}], 'future': True}],
    }, forReplay=True)

    assert item == {
        'type': 'message', 'id': 'msg_1', 'role': 'assistant',
        'content': [{'type': 'output_text', 'text': 'ok'}],
    }
    assert serializeResponseItem({
        'type': 'reasoning', 'id': 'rs_empty', 'summary': [], 'encrypted_content': '',
    }, forReplay=True) is None


def testReasoningSummaryKeyAlwaysPresentEvenIfEmpty() -> None:
    persisted = serializeResponseItem({
        'type': 'reasoning', 'id': 'rs_nosummary', 'encrypted_content': 'cipher',
    })
    assert persisted == {
        'type': 'reasoning',
        'id': 'rs_nosummary',
        'summary': [],
        'encrypted_content': 'cipher',
    }
    replayed = serializeResponseItem({
        'type': 'reasoning', 'id': 'rs_nosummary', 'encrypted_content': 'cipher',
    }, forReplay=True)
    assert replayed is not None
    assert replayed['summary'] == []


def testExactReplayUsesCanonicalProviderNotConfigAlias() -> None:
    assistant = exactAssistant()
    assistant.providerData['configProviderId'] = 'differentAlias'
    adapter = responsesAdapter(makeConfig(configProviderId='aliasB'), staticResolver())
    messages = [
        chatMessage(role='system', content='system'),
        assistant,
        chatMessage(role='tool', content='result', toolCallId='call_1', name='read'),
    ]

    items = adapter.convertMessages(messages)

    assert [item['type'] for item in items] == ['reasoning', 'message', 'function_call', 'function_call_output']
    assert all('status' not in item and 'future_field' not in item for item in items)
    assert items[-1] == {'type': 'function_call_output', 'call_id': 'call_1', 'output': 'result'}


def testCrossModelRebuildsOnlyPairedCallAndNeverOrphanOutput() -> None:
    adapter = responsesAdapter(makeConfig(model='gpt-two'), staticResolver())
    paired = exactAssistant()
    messages = [paired, chatMessage(role='tool', content='paired-result', toolCallId='call_1', name='read')]

    items = adapter.convertMessages(messages)

    functionCall = next(item for item in items if item.get('type') == 'function_call')
    assert 'id' not in functionCall
    assert functionCall['call_id'] == 'call_1'
    assert any(item.get('type') == 'function_call_output' and item['call_id'] == 'call_1' for item in items)
    assert not any(item.get('type') == 'reasoning' for item in items)

    orphanItems = adapter.convertMessages([
        chatMessage(role='tool', content='orphan-result', toolCallId='missing', name='read'),
    ])
    assert not any(item.get('type') == 'function_call_output' for item in orphanItems)
    assert '缺少前置 function_call' in orphanItems[0]['content'][0]['text']

    missingResult = adapter.convertMessages([paired])
    assert not any(item.get('type') == 'function_call' for item in missingResult)
    assert any('历史工具调用未回放' in part.get('text', '') for item in missingResult for part in item.get('content', []))


def testJsonlResumeKeepsProviderDataAndSecondPayloadStable(tmp_path: Path) -> None:
    logPath = tmp_path / 'session.jsonl'
    initial = conversation('session', logPath, 'system')
    assistant = exactAssistant()
    initial.appendAssistantMessage(assistant, {
        'model': 'gpt-one',
        'usage': {'prompt_tokens': 1, 'completion_tokens': 1},
        'reasoning': 'thought',
    })
    initial.addToolResult(toolResult(
        toolCallId='call_1', toolName='read', isError=False, content='result',
    ))
    adapter = responsesAdapter(makeConfig(), staticResolver())
    firstPayload = adapter.buildRequestPayload(initial.messages, [], sessionId='session')

    resumed = conversation('session', logPath, 'ignored-new-system', resume=True)
    secondPayload = adapter.buildRequestPayload(resumed.messages, [], sessionId='session')

    assert secondPayload == firstPayload
    restoredAssistant = next(message for message in resumed.messages if message.role == 'assistant')
    assert restoredAssistant.providerData['responseItems'][0]['encrypted_content'] == 'cipher'
    assert restoredAssistant.toolCalls[0].providerData == {'itemId': 'fc_1'}


def testLegacyJsonlWithoutProviderDataResumesAsEmptyObject(tmp_path: Path) -> None:
    logPath = tmp_path / 'legacy.jsonl'
    events = [
        {'type': 'systemMessage', 'content': 'legacy system'},
        {'type': 'assistantMessage', 'content': 'legacy answer', 'toolCalls': [{
            'id': 'legacy-call', 'toolName': 'read', 'arguments': {'path': 'x'},
        }]},
        {'type': 'toolResult', 'toolCallId': 'legacy-call', 'toolName': 'read', 'content': 'ok'},
    ]
    logPath.write_text('\n'.join(json.dumps(event) for event in events) + '\n', encoding='utf-8')

    resumed = conversation('legacy', logPath, 'ignored', resume=True)
    assistant = next(message for message in resumed.messages if message.role == 'assistant')

    assert assistant.providerData == {}
    assert assistant.toolCalls[0].providerData == {}
