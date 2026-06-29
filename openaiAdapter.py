'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Converts internal messages and tools to OpenAI-compatible chat completion requests.
'''

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from agentTypes import chatMessage, modelConfig, toolCall


class openaiCompatibleAdapter:
    def __init__(self, config: modelConfig, debugPrinter=None):
        self.config = config
        self.debugPrinter = debugPrinter

    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]]) -> chatMessage:
        apiKey = os.getenv(self.config.apiKeyEnv, '').strip()
        if not apiKey:
            raise RuntimeError(f'环境变量缺失：{self.config.apiKeyEnv}')

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
                'Authorization': f'Bearer {apiKey}',
                'Content-Type': 'application/json',
            },
        )
        if self.debugPrinter:
            self.debugPrinter.debug(f'调用模型：provider={self.config.provider} model={self.config.model}')
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                responseText = response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            errorText = error.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'模型请求失败：status={error.code} body={errorText[:1000]}') from error
        except urllib.error.URLError as error:
            raise RuntimeError(f'模型请求失败：{error.reason}') from error

        payload = json.loads(responseText)
        return self.parseAssistantPayload(payload)

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
        for index, rawCall in enumerate(rawToolCalls):
            functionValue = rawCall.get('function') or {}
            argumentsText = functionValue.get('arguments') or '{}'
            try:
                arguments = json.loads(argumentsText)
            except json.JSONDecodeError as error:
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 不是合法 JSON。') from error
            parsedToolCalls.append(toolCall(
                id=rawCall.get('id') or f'call_{index + 1}',
                toolName=functionValue.get('name') or '',
                arguments=arguments,
            ))

        content = rawMessage.get('content') or ''
        return chatMessage(role='assistant', content=content, toolCalls=parsedToolCalls)
