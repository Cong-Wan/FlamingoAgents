'''
Author: wilbur
Version: 1.3
Date: 2026-07-02
Description: Maintains per-session conversation state (messages, session lock, pending confirmation) and mirrors key events to JSONL logs using shared preview helpers.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock

from flamingoAgents.core.types import chatMessage, pendingConfirm, toolResult
from flamingoAgents.utils.jsonl import jsonlLog
from flamingoAgents.utils.preview import makePreview


class conversation:
    def __init__(self, sessionId: str, logPath: Path, systemPrompt: str):
        self.sessionId = sessionId
        self.logger = jsonlLog(logPath)
        self.messages: list[chatMessage] = []
        self.lock = RLock()
        self.pending: pendingConfirm | None = None
        self.addMessage(chatMessage(role='system', content=systemPrompt))

    def hasPending(self) -> bool:
        return self.pending is not None

    def setPending(self, pending: pendingConfirm) -> None:
        self.pending = pending

    def takePending(self) -> pendingConfirm | None:
        pending = self.pending
        self.pending = None
        return pending

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
