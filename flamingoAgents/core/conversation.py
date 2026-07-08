'''
Author: wilbur
Version: 1.4
Date: 2026-07-08
Description: Maintains per-session conversation state (messages, session lock, pending confirmation). Messages are kept for the next model request; only tool results are mirrored to JSONL logs.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock

from flamingoAgents.core.types import chatMessage, pendingConfirm, toolResult
from flamingoAgents.utils.jsonl import jsonlLog


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
