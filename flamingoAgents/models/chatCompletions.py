'''
Author: wilbur
Version: 1.4
Date: 2026-07-02
Description: Adapts internal chat messages and tool schemas to OpenAI-compatible chat completions using injected model auth.
'''

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from flamingoAgents.core.types import chatMessage, toolCall
from flamingoAgents.models.modelAuth import modelAuth
from flamingoAgents.models.modelConfig import modelConfig


@dataclass
class modelCompletion:
    message: chatMessage
    requestPayload: dict[str, Any]
    responsePayload: dict[str, Any]


class modelRequestError(Exception):
    def __init__(self, message: str, requestPayload: dict[str, Any], statusCode: int | None = None, responseBody: str = ''):
        super().__init__(message)
        self.requestPayload = requestPayload
        self.statusCode = statusCode
        self.responseBody = responseBody


class chatCompletionsAdapter:
    def __init__(self, config: modelConfig, auth: modelAuth, debugConsole=None):
        self.config = config
        self.auth = auth
        self.debugConsole = debugConsole

    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> modelCompletion:
        requestPayload = {
            'model': self.config.model,
            'messages': [self.convertMessage(message) for message in messages],
            'tools': tools,
            'tool_choice': 'auto',
        }
        requestUrl = self.config.baseUrl.rstrip('/') + '/chat/completions'
        requestBytes = json.dumps(requestPayload).encode('utf-8')
        request = urllib.request.Request(
            requestUrl,
            data=requestBytes,
            method='POST',
            headers={
                'Authorization': self.auth.authorizationHeader,
                'Content-Type': 'application/json',
            },
        )
        if self.debugConsole:
            self.debugConsole.debug(
                f'调用模型 provider={self.config.provider} model={self.config.model} '
                f'messages={len(messages)} tools={len(tools)} url={requestUrl}'
            )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                responseText = response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            errorText = error.read().decode('utf-8', errors='replace')
            raise modelRequestError(
                message=f'模型请求失败：status={error.code} body={errorText[:1000]}',
                requestPayload=requestPayload,
                statusCode=error.code,
                responseBody=errorText,
            ) from error
        except urllib.error.URLError as error:
            raise modelRequestError(
                message=f'模型请求失败：{error.reason}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=str(error.reason),
            ) from error

        payload = json.loads(responseText)
        if self.debugConsole:
            usage = payload.get('usage') if isinstance(payload, dict) else None
            self.debugConsole.debug(f'模型响应完成 model={self.config.model} usage={usage}')
        return modelCompletion(
            message=self.parseAssistantPayload(payload),
            requestPayload=requestPayload,
            responsePayload=payload,
        )

    def convertMessage(self, message: chatMessage) -> dict[str, Any]:
        if message.role == 'tool':
            return {
                'role': 'tool',
                'tool_call_id': message.toolCallId,
                'content': message.content,
            }
        converted: dict[str, Any] = {
            'role': message.role,
            'content': message.content,
        }
        if message.role == 'assistant' and message.toolCalls:
            converted['tool_calls'] = [
                {
                    'id': call.id,
                    'type': 'function',
                    'function': {
                        'name': call.toolName,
                        'arguments': json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.toolCalls
            ]
        return converted

    def parseAssistantPayload(self, payload: dict[str, Any]) -> chatMessage:
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices:
            raise RuntimeError('模型响应缺少 choices。')
        rawMessage = choices[0].get('message')
        if not isinstance(rawMessage, dict):
            raise RuntimeError('模型响应缺少 message。')

        parsedToolCalls: list[toolCall] = []
        rawToolCalls = rawMessage.get('tool_calls') or []
        if not isinstance(rawToolCalls, list):
            raise RuntimeError('模型响应 tool_calls 必须是数组。')
        for index, rawCall in enumerate(rawToolCalls):
            if not isinstance(rawCall, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call 必须是对象。')
            functionValue = rawCall.get('function') or {}
            if not isinstance(functionValue, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.function 必须是对象。')
            argumentsText = functionValue.get('arguments') or '{}'
            if not isinstance(argumentsText, str):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是字符串。')
            try:
                arguments = json.loads(argumentsText)
            except json.JSONDecodeError as error:
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 不是合法 JSON。') from error
            if not isinstance(arguments, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是 JSON 对象。')
            parsedToolCalls.append(toolCall(
                id=rawCall.get('id') or f'call_{index + 1}',
                toolName=functionValue.get('name') or '',
                arguments=arguments,
            ))

        content = rawMessage.get('content') or ''
        return chatMessage(role='assistant', content=content, toolCalls=parsedToolCalls)
