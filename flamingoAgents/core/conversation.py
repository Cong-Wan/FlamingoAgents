'''
Author: wilbur
Version: 1.1
Date: 2026-07-01
Description: Maintains in-memory conversation state and mirrors key events to JSONL logs.
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.types import chatMessage, toolResult
from flamingoAgents.utils.jsonl import jsonlLog, makePreview


class conversation:
    def __init__(self, sessionId: str, logPath: Path, systemPrompt: str):
        self.sessionId = sessionId
        self.logger = jsonlLog(logPath)
        self.messages: list[chatMessage] = []
        self.addMessage(chatMessage(role='system', content=systemPrompt))

    def addMessage(self, message: chatMessage) -> None:
        self.messages.append(message)
        if message.role == 'assistant' and message.toolCalls:
            if message.content:
                self.logger.logEvent({
                    'type': 'message',
                    'role': message.role,
                    'content': message.content,
                })
            for call in message.toolCalls:
                argumentsPreview, argumentsTruncated = makePreview(call.arguments)
                self.logger.logEvent({
                    'type': 'toolCall',
                    'role': 'assistant',
                    'toolCallId': call.id,
                    'toolName': call.toolName,
                    'argumentsPreview': argumentsPreview,
                    'argumentsTruncated': argumentsTruncated,
                })
            return

        self.logger.logEvent({
            'type': 'message',
            'role': message.role,
            'content': message.content,
            'toolCallId': message.toolCallId,
            'name': message.name,
        })

    def addToolResult(self, result: toolResult) -> None:
        resultPreview, resultTruncated = makePreview(result.content)
        detailsPreview, detailsTruncated = makePreview(result.details)
        self.logger.logEvent({
            'type': 'toolResult',
            'toolCallId': result.toolCallId,
            'toolName': result.toolName,
            'isError': result.isError,
            'contentPreview': resultPreview,
            'contentTruncated': resultTruncated,
            'detailsPreview': detailsPreview,
            'detailsTruncated': detailsTruncated,
        })
        self.messages.append(chatMessage(
            role='tool',
            content=result.content,
            toolCallId=result.toolCallId,
            name=result.toolName,
        ))
