'''
Author: wilbur
Version: 1.5
Date: 2026-07-09
Description: Maintains per-session conversation state (messages, session lock, pending confirmation). Messages are appended as atomic log events (systemMessage/userMessage/assistantMessage/toolResult); in-memory list is kept for the next model request.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock

from flamingoAgents.core.types import chatMessage, pendingConfirm, toolResult
from flamingoAgents.utils.jsonl import jsonlLog


class conversation:
    def __init__(self, sessionId: str, logPath: Path, systemPrompt: str, debugConsole=None):
        self.sessionId = sessionId
        self.logger = jsonlLog(logPath)
        self.messages: list[chatMessage] = []
        self.lock = RLock()
        self.pending: pendingConfirm | None = None
        self.debugConsole = debugConsole
        self.appendSystemMessage(systemPrompt)

    def hasPending(self) -> bool:
        return self.pending is not None

    def setPending(self, pending: pendingConfirm) -> None:
        self.pending = pending

    def takePending(self) -> pendingConfirm | None:
        pending = self.pending
        self.pending = None
        return pending

    def appendSystemMessage(self, content: str) -> None:
        if self.debugConsole:
            self.debugConsole.debug(f'记录 systemMessage chars={len(content)}')
        self.logger.logEvent({'type': 'systemMessage', 'content': content})
        self.messages.append(chatMessage(role='system', content=content))

    def appendUserMessage(self, content: str) -> None:
        if self.debugConsole:
            self.debugConsole.debug(f'记录 userMessage chars={len(content)}')
        self.logger.logEvent({'type': 'userMessage', 'content': content})
        self.messages.append(chatMessage(role='user', content=content))

    def appendAssistantMessage(self, message: chatMessage, responsePayload: dict) -> None:
        toolCallCount = len(message.toolCalls)
        if self.debugConsole:
            self.debugConsole.debug(
                f'记录 assistantMessage contentChars={len(message.content)} '
                f'toolCalls={toolCallCount} model={responsePayload.get("model")}'
            )
        self.logger.logEvent({
            'type': 'assistantMessage',
            'model': responsePayload.get('model'),
            'content': message.content,
            'toolCalls': message.toolCalls,
            'usage': responsePayload.get('usage'),
            'timings': responsePayload.get('timings'),
        })
        self.messages.append(message)

    def addToolResult(self, result: toolResult) -> None:
        self.logger.logEvent({
            'type': 'toolResult',
            'toolCallId': result.toolCallId,
            'toolName': result.toolName,
            'isError': result.isError,
            'content': result.content,
            'details': result.details,
        })
        self.messages.append(chatMessage(
            role='tool',
            content=result.content,
            toolCallId=result.toolCallId,
            name=result.toolName,
        ))
