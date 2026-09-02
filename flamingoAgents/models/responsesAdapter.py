'''
Author: wilbur
Version: 1.1
Date: 2026-09-01
Description: Adapts Flamingo messages/tools to ChatGPT Codex and xAI Responses SSE, including dynamic auth, safe opaque replay, terminal-authoritative item merging, usage normalization, stop, and one zero-output OAuth 401 refresh retry. v1.1 relaxes the urlopen socket timeout 60s→300s：thinking 模型静默期可达数分钟，60s 会误杀长思考。
'''

from __future__ import annotations

import http.client
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.request
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

from flamingoAgents.core.types import chatMessage, finalChunk, modelInterruptedError, reasoningChunk, textChunk, toolCall
from flamingoAgents.models.chatCompletions import modelCompletion, modelRequestError
from flamingoAgents.models.modelAuth import modelAuth, modelAuthResolver
from flamingoAgents.models.modelConfig import modelConfig

userAgent = 'FlamingoAgents/0.1.0'
supportedItemTypes = frozenset({'reasoning', 'message', 'function_call', 'function_call_output'})


class unsupportedResponseItem(RuntimeError):
    pass


class responsesAdapter:
    def __init__(self, config: modelConfig, authResolver: modelAuthResolver, debugConsole=None):
        self.config = config
        self.authResolver = authResolver
        self.debugConsole = debugConsole
        self.activeResponses: set = set()
        self.activeResponsesLock = threading.Lock()

    def interruptActiveStreams(self) -> None:
        with self.activeResponsesLock:
            responses = list(self.activeResponses)
        for response in responses:
            try:
                setattr(response, '_flamingoInterrupted', True)
            except Exception:
                pass
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    def buildRequestPayload(
        self,
        messages: list[chatMessage],
        tools: list[dict[str, Any]],
        sessionId: str | None = None,
    ) -> dict[str, Any]:
        cleanSessionId = (sessionId or '')[:64] or None
        systemMessages = [message.content for message in messages if message.role == 'system' and message.content]
        inputItems = self.convertMessages(messages)
        if self.config.apiType == 'openai-codex-responses':
            inputItems = [item for item in inputItems if item.get('role') not in ('system', 'developer')]
        requestPayload: dict[str, Any] = {
            'model': self.config.model,
            'input': inputItems,
            'stream': True,
            'store': False,
            'tools': self.convertTools(tools),
            'tool_choice': 'auto',
            'parallel_tool_calls': True,
            'include': ['reasoning.encrypted_content'],
        }
        if self.config.apiType == 'openai-codex-responses':
            requestPayload['instructions'] = '\n\n'.join(systemMessages).strip() or 'You are a helpful assistant.'
            requestPayload['text'] = {'verbosity': 'low'}
        if cleanSessionId:
            requestPayload['prompt_cache_key'] = cleanSessionId
        if self.config.reasoning or self.config.reasoningEffort:
            requestPayload['reasoning'] = {
                'effort': self.config.reasoningEffort or 'high',
                'summary': 'auto',
            }
        return requestPayload

    def convertTools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted = []
        for rawTool in tools:
            if not isinstance(rawTool, dict) or rawTool.get('type') != 'function':
                continue
            functionValue = rawTool.get('function')
            if not isinstance(functionValue, dict):
                continue
            name = functionValue.get('name')
            parameters = functionValue.get('parameters')
            if not isinstance(name, str) or not isinstance(parameters, dict):
                continue
            item: dict[str, Any] = {
                'type': 'function',
                'name': name,
                'parameters': parameters,
            }
            description = functionValue.get('description')
            if isinstance(description, str):
                item['description'] = description
            converted.append(item)
        return converted

    def convertMessages(self, messages: list[chatMessage]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        replayableCallIds: set[str] = set()
        pairedCallIds = self._findPairedToolCalls(messages)
        for message in messages:
            if message.role == 'system':
                if self.config.apiType != 'openai-codex-responses' and message.content:
                    converted.append({'role': 'system', 'content': message.content})
                continue
            if message.role == 'user':
                converted.append({
                    'role': 'user',
                    'content': [{'type': 'input_text', 'text': message.content}],
                })
                continue
            if message.role == 'assistant':
                exactItems = self._exactReplayItems(message, pairedCallIds)
                if exactItems:
                    converted.extend(exactItems)
                    replayableCallIds.update(
                        item['call_id'] for item in exactItems
                        if item.get('type') == 'function_call' and isinstance(item.get('call_id'), str)
                    )
                    continue
                skippedNames = [call.toolName for call in message.toolCalls if call.id not in pairedCallIds]
                assistantText = message.content or ''
                if skippedNames:
                    note = '[历史工具调用未回放：缺少可可靠配对的结果] ' + ', '.join(skippedNames)
                    assistantText = (assistantText + '\n\n' + note).strip()
                if assistantText:
                    converted.append({
                        'type': 'message',
                        'role': 'assistant',
                        'content': [{'type': 'output_text', 'text': assistantText}],
                    })
                for call in message.toolCalls:
                    if call.id not in pairedCallIds:
                        continue
                    converted.append({
                        'type': 'function_call',
                        'call_id': call.id,
                        'name': call.toolName,
                        'arguments': json.dumps(call.arguments, ensure_ascii=False),
                    })
                    replayableCallIds.add(call.id)
                continue
            if message.role == 'tool':
                callId = message.toolCallId or ''
                if callId and callId in replayableCallIds:
                    converted.append({
                        'type': 'function_call_output',
                        'call_id': callId,
                        'output': message.content or '(no tool output)',
                    })
                else:
                    converted.append({
                        'role': 'user',
                        'content': [{
                            'type': 'input_text',
                            'text': '[历史工具结果未回放：缺少前置 function_call]\n' + (message.content or ''),
                        }],
                    })
        return converted

    def _findPairedToolCalls(self, messages: list[chatMessage]) -> set[str]:
        paired: set[str] = set()
        for index, message in enumerate(messages):
            if message.role != 'assistant' or not message.toolCalls:
                continue
            followingResultIds: set[str] = set()
            nextIndex = index + 1
            while nextIndex < len(messages) and messages[nextIndex].role == 'tool':
                if messages[nextIndex].toolCallId:
                    followingResultIds.add(messages[nextIndex].toolCallId)
                nextIndex += 1
            paired.update(call.id for call in message.toolCalls if call.id in followingResultIds)
        return paired

    def _exactReplayItems(self, message: chatMessage, pairedCallIds: set[str]) -> list[dict[str, Any]]:
        data = message.providerData
        if not isinstance(data, dict):
            return []
        if (
            data.get('api') != self.config.apiType
            or data.get('authProvider') != self.config.authProvider
            or data.get('model') != self.config.model
        ):
            return []
        rawItems = data.get('responseItems')
        if not isinstance(rawItems, list):
            return []
        result = []
        replayedCallIds: set[str] = set()
        for rawItem in rawItems:
            item = serializeResponseItem(rawItem, forReplay=True)
            if item is None:
                continue
            if item.get('type') == 'function_call':
                callId = item.get('call_id')
                if callId not in pairedCallIds:
                    continue
                replayedCallIds.add(callId)
            elif item.get('type') == 'function_call_output' and item.get('call_id') not in replayedCallIds:
                continue
            result.append(item)
        return result

    def requestUrl(self) -> str:
        baseUrl = self.config.baseUrl.rstrip('/')
        if self.config.apiType == 'openai-codex-responses':
            if baseUrl.endswith('/codex/responses'):
                return baseUrl
            if baseUrl.endswith('/codex'):
                return baseUrl + '/responses'
            return baseUrl + '/codex/responses'
        if baseUrl.endswith('/responses'):
            return baseUrl
        return baseUrl + '/responses'

    def requestHeaders(self, auth: modelAuth, sessionId: str | None = None) -> dict[str, str]:
        cleanSessionId = (sessionId or '')[:64] or None
        headers = {'User-Agent': userAgent}
        headers.update(self.config.headers or {})
        headers.update(auth.headers)
        headers['Authorization'] = auth.authorizationHeader
        headers['Accept'] = 'text/event-stream'
        headers['Content-Type'] = 'application/json'
        if self.config.apiType == 'openai-codex-responses':
            headers['OpenAI-Beta'] = 'responses=experimental'
            headers['originator'] = 'pi'
            if cleanSessionId:
                headers['session-id'] = cleanSessionId
                headers['x-client-request-id'] = cleanSessionId
        return headers

    def openRequest(
        self,
        requestPayload: dict[str, Any],
        auth: modelAuth,
        sessionId: str | None = None,
    ):
        requestBytes = json.dumps(requestPayload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(
            self.requestUrl(),
            data=requestBytes,
            method='POST',
            headers=self.requestHeaders(auth, sessionId),
        )
        if self.debugConsole:
            self.debugConsole.debug(f'Source Responses request:\n{requestBytes.decode("utf-8")}\n')
        try:
            # 读超时 300s：thinking 模型静默期可达 1~3 分钟，60s 会误杀长思考；
            # 保持有限值（非 None）兜底真死连接，避免永久占用会话锁。
            return urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as error:
            errorText = error.read(65536).decode('utf-8', errors='replace')
            safeText = redactSecret(errorText, auth.accessToken)
            raise modelRequestError(
                message=f'模型请求失败：status={error.code} body={safeText[:1000]}',
                requestPayload=requestPayload,
                statusCode=error.code,
                responseBody=safeText,
                retryAfterSeconds=parseRetryAfter(error.headers),
            ) from error
        except urllib.error.URLError as error:
            reasonText = redactSecret(str(error.reason), auth.accessToken)
            raise modelRequestError(
                message=f'模型请求失败：{reasonText}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=reasonText,
            ) from error

    def complete(
        self,
        messages: list[chatMessage],
        tools: list[dict[str, Any]],
        sessionId: str | None = None,
    ) -> modelCompletion:
        completion = None
        for chunk in self.completeStream(messages, tools, sessionId=sessionId):
            if isinstance(chunk, finalChunk):
                completion = chunk.completion
        if completion is None:
            raise RuntimeError('Responses 流缺少最终结果。')
        return completion

    def completeStream(
        self,
        messages: list[chatMessage],
        tools: list[dict[str, Any]],
        stopEvent=None,
        sessionId: str | None = None,
    ) -> Iterator:
        requestPayload = self.buildRequestPayload(messages, tools, sessionId=sessionId)
        yield from self.consumeSseStream(requestPayload, stopEvent=stopEvent, sessionId=sessionId)

    def consumeSseStream(
        self,
        requestPayload: dict[str, Any],
        stopEvent=None,
        sessionId: str | None = None,
    ) -> Iterator:
        auth = self.authResolver.resolve()
        try:
            response = self.openRequest(requestPayload, auth, sessionId=sessionId)
        except modelRequestError as error:
            if error.statusCode != 401 or self.config.authType != 'oauth':
                raise
            auth = self.authResolver.resolve(forceRefresh=True, staleAccess=auth.accessToken)
            response = self.openRequest(requestPayload, auth, sessionId=sessionId)

        state = responseState(self.config, requestPayload)
        try:
            with response:
                if stopEvent is not None and stopEvent.is_set():
                    raise modelInterruptedError('用户已停止')
                with self.activeResponsesLock:
                    self.activeResponses.add(response)
                try:
                    for dataPayload in self.iterSseData(response, stopEvent=stopEvent):
                        if dataPayload == '[DONE]':
                            continue
                        event = self.parseEvent(dataPayload, requestPayload)
                        for outputChunk in state.processEvent(event):
                            yield outputChunk
                    if self._isStreamInterrupted(response, stopEvent):
                        raise modelInterruptedError('用户已停止')
                finally:
                    with self.activeResponsesLock:
                        self.activeResponses.discard(response)
        except modelInterruptedError:
            raise
        except modelRequestError:
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            if self._isStreamInterrupted(response, stopEvent):
                raise modelInterruptedError('用户已停止') from error
            raise modelRequestError(
                message=f'模型流式响应中断：{error}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=str(error),
            ) from error

        completion = state.buildCompletion()
        if self.debugConsole:
            self.debugConsole.debug(f'\nSource Responses response:\n{completion.responsePayload}')
        yield finalChunk(completion=completion)

    def _isStreamInterrupted(self, response, stopEvent) -> bool:
        if stopEvent is not None and stopEvent.is_set():
            return True
        return bool(getattr(response, '_flamingoInterrupted', False))

    def iterSseData(self, response, stopEvent=None) -> Iterator[str]:
        buffer = b''
        dataLines: list[bytes] = []
        readChunk = getattr(response, 'read1', None)
        if not callable(readChunk):
            readChunk = response.read
        while True:
            try:
                data = readChunk(4096)
            except Exception:
                if self._isStreamInterrupted(response, stopEvent):
                    raise modelInterruptedError('用户已停止')
                raise
            if not data:
                break
            buffer += data
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                line = line.rstrip(b'\r')
                if not line:
                    if dataLines:
                        yield b'\n'.join(dataLines).decode('utf-8', errors='replace')
                        dataLines = []
                    continue
                stripped = line.strip()
                if stripped.startswith(b':'):
                    continue
                if stripped.startswith(b'data:'):
                    dataLines.append(stripped[5:].lstrip())
        tail = buffer.strip()
        if tail.startswith(b'data:'):
            dataLines.append(tail[5:].lstrip())
        if dataLines:
            yield b'\n'.join(dataLines).decode('utf-8', errors='replace')
        if self._isStreamInterrupted(response, stopEvent):
            raise modelInterruptedError('用户已停止')

    def parseEvent(self, dataPayload: str, requestPayload: dict[str, Any]) -> dict[str, Any]:
        try:
            event = json.loads(dataPayload)
        except json.JSONDecodeError as error:
            safePayload = redactSecret(dataPayload, None)
            requestError = modelRequestError(
                message=f'Responses SSE 不是合法 JSON：{safePayload[:200]}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=safePayload[:1000],
            )
            requestError.retryable = False
            raise requestError from error
        if not isinstance(event, dict):
            requestError = modelRequestError(
                message='Responses SSE 事件必须是 JSON 对象。',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=redactSecret(dataPayload[:1000], None),
            )
            requestError.retryable = False
            raise requestError
        return event


class responseState:
    def __init__(self, config: modelConfig, requestPayload: dict[str, Any]):
        self.config = config
        self.requestPayload = requestPayload
        self.slots: dict[int, dict[str, Any]] = {}
        self.responseId: str | None = None
        self.responseModel: str | None = None
        self.usage: dict[str, Any] | None = None
        self.stopReason = 'stop'
        self.terminalSeen = False

    def processEvent(self, event: dict[str, Any]) -> Iterator:
        eventType = event.get('type')
        if eventType == 'response.created':
            response = event.get('response')
            if isinstance(response, dict):
                if isinstance(response.get('id'), str):
                    self.responseId = response['id']
                if isinstance(response.get('model'), str):
                    self.responseModel = response['model']
            return
        if eventType == 'response.output_item.added':
            self._getOrCreateSlot(self._outputIndex(event), event.get('item'))
            return
        if eventType in ('response.reasoning_summary_text.delta', 'response.reasoning_text.delta'):
            slot = self._slotFor(event, 'reasoning')
            delta = event.get('delta')
            if slot is not None and isinstance(delta, str) and delta:
                slot['emitted'] += delta
                slot['reasoningText'] += delta
                yield reasoningChunk(text=delta)
            return
        if eventType == 'response.reasoning_summary_part.done':
            slot = self._slotFor(event, 'reasoning')
            if slot is not None:
                slot['emitted'] += '\n\n'
                slot['reasoningText'] += '\n\n'
                yield reasoningChunk(text='\n\n')
            return
        if eventType in ('response.output_text.delta', 'response.refusal.delta'):
            slot = self._slotFor(event, 'message')
            delta = event.get('delta')
            if slot is not None and isinstance(delta, str) and delta:
                slot['emitted'] += delta
                slot['text'] += delta
                yield textChunk(text=delta)
            return
        if eventType == 'response.function_call_arguments.delta':
            slot = self._slotFor(event, 'function_call')
            delta = event.get('delta')
            if slot is not None and isinstance(delta, str):
                slot['argumentsText'] += delta
            return
        if eventType == 'response.function_call_arguments.done':
            slot = self._slotFor(event, 'function_call')
            arguments = event.get('arguments')
            if slot is not None and isinstance(arguments, str):
                slot['argumentsText'] = arguments
                if isinstance(slot.get('item'), dict):
                    slot['item'] = serializeResponseItem({**slot['item'], 'arguments': arguments})
            return
        if eventType in ('response.custom_tool_call_input.delta', 'response.custom_tool_call_input.done'):
            raise self._protocolError('unsupportedResponseItem: custom tool 暂不支持。')
        if eventType == 'response.output_item.done':
            for chunk in self._applyFinalItem(self._outputIndex(event), event.get('item')):
                yield chunk
            return
        if eventType in ('response.completed', 'response.done', 'response.incomplete'):
            response = event.get('response')
            if not isinstance(response, dict):
                raise self._protocolError('Responses terminal 事件缺少 response。')
            if eventType == 'response.done' and not isinstance(response.get('status'), str):
                response = {**response, 'status': 'completed'}
            for chunk in self._finalizeTerminal(response):
                yield chunk
            return
        if eventType == 'response.failed':
            response = event.get('response')
            error = response.get('error') if isinstance(response, dict) else None
            code = error.get('code') if isinstance(error, dict) else None
            message = error.get('message') if isinstance(error, dict) else None
            raise self._protocolError(f'Responses failed：{code or "unknown"} {message or ""}'.strip())
        if eventType == 'error' or event.get('error'):
            error = event.get('error')
            code = event.get('code')
            message = event.get('message')
            if isinstance(error, dict):
                code = error.get('code') or code
                message = error.get('message') or message
            raise self._protocolError(f'Responses error：{code or "unknown"} {message or ""}'.strip())

    def _outputIndex(self, event: dict[str, Any]) -> int:
        value = event.get('output_index', 0)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _getOrCreateSlot(self, outputIndex: int, rawItem: Any) -> dict[str, Any] | None:
        existing = self.slots.get(outputIndex)
        if existing is not None:
            return existing
        if not isinstance(rawItem, dict):
            return None
        itemType = rawItem.get('type')
        if itemType in ('custom_tool_call', 'custom_tool_call_output'):
            raise self._protocolError('unsupportedResponseItem: custom tool 暂不支持。')
        if itemType not in supportedItemTypes:
            return None
        slot = {
            'type': itemType,
            'item': serializeResponseItem(rawItem),
            'text': '',
            'reasoningText': '',
            'argumentsText': rawItem.get('arguments') if isinstance(rawItem.get('arguments'), str) else '',
            'emitted': '',
        }
        self.slots[outputIndex] = slot
        return slot

    def _slotFor(self, event: dict[str, Any], expectedType: str) -> dict[str, Any] | None:
        slot = self.slots.get(self._outputIndex(event))
        return slot if slot is not None and slot.get('type') == expectedType else None

    def _applyFinalItem(self, outputIndex: int, rawItem: Any) -> list:
        if not isinstance(rawItem, dict):
            return []
        itemType = rawItem.get('type')
        if itemType in ('custom_tool_call', 'custom_tool_call_output'):
            raise self._protocolError('unsupportedResponseItem: custom tool 暂不支持。')
        if itemType not in supportedItemTypes:
            return []
        slot = self.slots.get(outputIndex)
        if slot is None or slot.get('type') != itemType:
            slot = {
                'type': itemType,
                'item': None,
                'text': '',
                'reasoningText': '',
                'argumentsText': '',
                'emitted': '',
            }
            self.slots[outputIndex] = slot
        chunks = []
        if itemType == 'message':
            finalText = extractMessageText(rawItem)
            chunks.extend(self._emitMissing(slot, finalText, text=True))
            slot['text'] = finalText
        elif itemType == 'reasoning':
            finalReasoning = extractReasoningText(rawItem) or slot['reasoningText']
            chunks.extend(self._emitMissing(slot, finalReasoning, text=False))
            slot['reasoningText'] = finalReasoning
        elif itemType == 'function_call':
            arguments = rawItem.get('arguments')
            if isinstance(arguments, str):
                slot['argumentsText'] = arguments
        normalizedItem = serializeResponseItem(rawItem)
        if (
            itemType == 'function_call'
            and isinstance(normalizedItem, dict)
            and not normalizedItem.get('arguments')
            and slot['argumentsText']
        ):
            normalizedItem['arguments'] = slot['argumentsText']
        slot['item'] = normalizedItem
        return chunks

    def _emitMissing(self, slot: dict[str, Any], finalText: str, *, text: bool) -> list:
        emitted = slot['emitted']
        if not finalText or finalText == emitted:
            return []
        if not emitted:
            missing = finalText
        elif finalText.startswith(emitted):
            missing = finalText[len(emitted):]
        else:
            return []
        if not missing:
            return []
        slot['emitted'] += missing
        return [textChunk(text=missing) if text else reasoningChunk(text=missing)]

    def _finalizeTerminal(self, response: dict[str, Any]) -> list:
        self.terminalSeen = True
        if isinstance(response.get('id'), str):
            self.responseId = response['id']
        if isinstance(response.get('model'), str):
            self.responseModel = response['model']
        terminalOutput = response.get('output')
        chunks = []
        if isinstance(terminalOutput, list):
            idToIndex = {
                itemId: index
                for index, slot in self.slots.items()
                for itemId in [slot.get('item', {}).get('id') if isinstance(slot.get('item'), dict) else None]
                if isinstance(itemId, str) and itemId
            }
            nextIndex = max(self.slots.keys(), default=-1) + 1
            for rawItem in terminalOutput:
                if not isinstance(rawItem, dict):
                    continue
                itemId = rawItem.get('id')
                index = idToIndex.get(itemId) if isinstance(itemId, str) else None
                if index is None and rawItem.get('type') == 'function_call':
                    callId = rawItem.get('call_id')
                    index = next((
                        slotIndex for slotIndex, slot in self.slots.items()
                        if slot.get('type') == 'function_call'
                        and isinstance(slot.get('item'), dict)
                        and slot['item'].get('call_id') == callId
                    ), None)
                if index is None:
                    index = nextIndex
                    nextIndex += 1
                chunks.extend(self._applyFinalItem(index, rawItem))
        rawUsage = response.get('usage')
        if isinstance(rawUsage, dict):
            self.usage = normalizeUsage(rawUsage)
        status = response.get('status')
        if status == 'incomplete':
            details = response.get('incomplete_details')
            reason = details.get('reason') if isinstance(details, dict) else None
            self.stopReason = 'length' if reason in ('max_output_tokens', 'max_tokens') else 'error'
        elif status in ('failed', 'cancelled'):
            self.stopReason = 'error'
        else:
            self.stopReason = 'stop'
        return chunks

    def buildCompletion(self) -> modelCompletion:
        if not self.terminalSeen:
            raise self._protocolError('Responses 流在 terminal 事件前结束。')
        contentParts = []
        reasoningParts = []
        parsedToolCalls: list[toolCall] = []
        responseItems = []
        for _, slot in sorted(self.slots.items()):
            item = slot.get('item')
            persistedItem = serializeResponseItem(item) if isinstance(item, dict) else None
            if persistedItem is not None:
                responseItems.append(persistedItem)
            if slot['type'] == 'message':
                contentParts.append(slot['text'])
            elif slot['type'] == 'reasoning':
                reasoningParts.append(slot['reasoningText'])
            elif slot['type'] == 'function_call':
                if not isinstance(item, dict):
                    raise self._protocolError('function_call 缺少终态 item。')
                callId = item.get('call_id')
                name = item.get('name')
                itemArguments = item.get('arguments')
                argumentsText = slot['argumentsText'] or (itemArguments if isinstance(itemArguments, str) else '') or '{}'
                if not isinstance(callId, str) or not callId or not isinstance(name, str) or not name:
                    raise self._protocolError('function_call 缺少 call_id/name。')
                try:
                    arguments = json.loads(argumentsText or '{}')
                except json.JSONDecodeError as error:
                    raise self._protocolError('function_call arguments 不是合法 JSON。') from error
                if not isinstance(arguments, dict):
                    raise self._protocolError('function_call arguments 必须是 JSON 对象。')
                itemId = item.get('id')
                providerData = {'itemId': itemId} if isinstance(itemId, str) and itemId else {}
                parsedToolCalls.append(toolCall(
                    id=callId,
                    toolName=name,
                    arguments=arguments,
                    providerData=providerData,
                ))

        providerData = {
            'api': self.config.apiType,
            'authProvider': self.config.authProvider,
            'configProviderId': self.config.configProviderId or self.config.provider,
            'model': self.config.model,
            'responseItems': responseItems,
        }
        assistantMessage = chatMessage(
            role='assistant',
            content=''.join(contentParts),
            toolCalls=parsedToolCalls,
            providerData=providerData,
        )
        messagePayload: dict[str, Any] = {
            'role': 'assistant',
            'content': assistantMessage.content,
        }
        if parsedToolCalls:
            messagePayload['tool_calls'] = [
                {
                    'id': call.id,
                    'type': 'function',
                    'function': {
                        'name': call.toolName,
                        'arguments': json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in parsedToolCalls
            ]
        responsePayload: dict[str, Any] = {
            'id': self.responseId,
            'model': self.responseModel or self.config.model,
            'choices': [{
                'index': 0,
                'message': messagePayload,
                'finish_reason': 'tool_calls' if parsedToolCalls else self.stopReason,
            }],
        }
        if self.usage is not None:
            responsePayload['usage'] = self.usage
        reasoningText = ''.join(reasoningParts)
        if reasoningText:
            responsePayload['reasoning'] = reasoningText
        return modelCompletion(
            message=assistantMessage,
            requestPayload=self.requestPayload,
            responsePayload=responsePayload,
        )

    def _protocolError(self, message: str) -> modelRequestError:
        requestError = modelRequestError(
            message=redactSecret(message, None),
            requestPayload=self.requestPayload,
            statusCode=None,
            responseBody='',
        )
        requestError.retryable = False
        return requestError


def serializeResponseItem(rawItem: Any, forReplay: bool = False) -> dict[str, Any] | None:
    if not isinstance(rawItem, dict):
        return None
    itemType = rawItem.get('type')
    if itemType == 'reasoning':
        itemId = rawItem.get('id')
        if not isinstance(itemId, str) or not itemId:
            return None
        encryptedContent = rawItem.get('encrypted_content')
        if forReplay and (not isinstance(encryptedContent, str) or not encryptedContent):
            return None
        result: dict[str, Any] = {'type': 'reasoning', 'id': itemId}
        summary = sanitizeTextParts(rawItem.get('summary'), {'summary_text', 'text'})
        if summary:
            result['summary'] = summary
        if isinstance(encryptedContent, str) and encryptedContent:
            result['encrypted_content'] = encryptedContent
        return result
    if itemType == 'message':
        role = rawItem.get('role')
        if role != 'assistant':
            return None
        content = sanitizeTextParts(rawItem.get('content'), {'output_text', 'refusal'})
        result = {'type': 'message', 'role': 'assistant', 'content': content}
        itemId = rawItem.get('id')
        if isinstance(itemId, str) and itemId:
            result['id'] = itemId[:64]
        phase = rawItem.get('phase')
        if phase in ('commentary', 'final_answer'):
            result['phase'] = phase
        return result
    if itemType == 'function_call':
        callId = rawItem.get('call_id')
        name = rawItem.get('name')
        arguments = rawItem.get('arguments')
        if not all(isinstance(value, str) and value for value in (callId, name)) or not isinstance(arguments, str):
            return None
        result = {
            'type': 'function_call',
            'call_id': callId,
            'name': name,
            'arguments': arguments,
        }
        itemId = rawItem.get('id')
        if isinstance(itemId, str) and itemId:
            result['id'] = itemId[:64]
        return result
    if itemType == 'function_call_output':
        callId = rawItem.get('call_id')
        output = rawItem.get('output')
        if not isinstance(callId, str) or not callId or not isinstance(output, (str, list)):
            return None
        return {'type': 'function_call_output', 'call_id': callId, 'output': output}
    return None


def sanitizeTextParts(rawParts: Any, allowedTypes: set[str]) -> list[dict[str, str]]:
    if not isinstance(rawParts, list):
        return []
    result = []
    for rawPart in rawParts:
        if not isinstance(rawPart, dict) or rawPart.get('type') not in allowedTypes:
            continue
        partType = rawPart['type']
        if partType == 'refusal':
            text = rawPart.get('refusal')
            if isinstance(text, str):
                result.append({'type': 'refusal', 'refusal': text})
        else:
            text = rawPart.get('text')
            if isinstance(text, str):
                result.append({'type': partType, 'text': text})
    return result


def extractMessageText(item: dict[str, Any]) -> str:
    parts = item.get('content')
    if not isinstance(parts, list):
        return ''
    result = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get('type') == 'output_text' and isinstance(part.get('text'), str):
            result.append(part['text'])
        elif part.get('type') == 'refusal' and isinstance(part.get('refusal'), str):
            result.append(part['refusal'])
    return ''.join(result)


def extractReasoningText(item: dict[str, Any]) -> str:
    summary = item.get('summary')
    if isinstance(summary, list):
        texts = [part.get('text') for part in summary if isinstance(part, dict) and isinstance(part.get('text'), str)]
        if texts:
            return '\n\n'.join(texts)
    content = item.get('content')
    if isinstance(content, list):
        texts = [part.get('text') for part in content if isinstance(part, dict) and isinstance(part.get('text'), str)]
        return '\n\n'.join(texts)
    return ''


def normalizeUsage(usage: dict[str, Any]) -> dict[str, Any]:
    inputDetails = usage.get('input_tokens_details')
    outputDetails = usage.get('output_tokens_details')
    normalized: dict[str, Any] = {
        'prompt_tokens': int(usage.get('input_tokens') or 0),
        'completion_tokens': int(usage.get('output_tokens') or 0),
        'total_tokens': int(usage.get('total_tokens') or 0),
        'prompt_tokens_details': {
            'cached_tokens': int(inputDetails.get('cached_tokens') or 0) if isinstance(inputDetails, dict) else 0,
        },
        'completion_tokens_details': {
            'reasoning_tokens': int(outputDetails.get('reasoning_tokens') or 0) if isinstance(outputDetails, dict) else 0,
        },
    }
    if not normalized['total_tokens']:
        normalized['total_tokens'] = normalized['prompt_tokens'] + normalized['completion_tokens']
    return normalized


def parseRetryAfter(headers) -> float | None:
    retryAfter = headers.get('Retry-After') if headers else None
    if retryAfter is None:
        return None
    try:
        return max(0.0, float(retryAfter))
    except (TypeError, ValueError):
        try:
            seconds = parsedate_to_datetime(retryAfter).timestamp() - time.time()
            return max(0.0, seconds)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None


def redactSecret(text: str, secret: str | None) -> str:
    safeText = text.replace(secret, '<redacted>') if secret else text
    safeText = re.sub(
        r'(?i)Bearer\s+[A-Za-z0-9._~+\-/]+=*',
        'Bearer_<redacted>',
        safeText,
    )
    return re.sub(
        r'(?i)(access[_ -]?token|refresh[_ -]?token|authorization|code[_ -]?verifier)'
        r'\s*[:=]\s*([^\s,;&]+)',
        lambda match: f'{match.group(1)}=<redacted>',
        safeText,
    )
