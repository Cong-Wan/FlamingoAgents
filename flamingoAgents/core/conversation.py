'''
Author: wilbur
Version: 1.8
Date: 2026-07-24
Description: Maintains per-session conversation state (messages, session lock, pending confirmation). Messages are appended as atomic log events (systemMessage/userMessage/assistantMessage/toolResult); in-memory list is kept for the next model request. v1.6 adds session resume: replay the JSONL log into messages, track dangling/queued tool-call state, and accumulate a session usage total. v1.7 accumulates live-turn usage in appendAssistantMessage so usageTotal covers post-resume turns too. v1.8 restores the logged systemMessage on resume (instead of re-injecting the current one) so the resumed prefix matches the original and provider prompt cache stays hit.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock

from flamingoAgents.core.types import chatMessage, pendingConfirm, toolCall, toolResult
from flamingoAgents.utils.jsonl import jsonlLog


class conversation:
    def __init__(self, sessionId: str, logPath: Path, systemPrompt: str, debugConsole=None, resume: bool = False):
        self.sessionId = sessionId
        self.logger = jsonlLog(logPath)
        self.messages: list[chatMessage] = []
        self.lock = RLock()
        self.pending: pendingConfirm | None = None
        self.debugConsole = debugConsole
        self.usageTotal: dict[str, int] = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
        self.danglingToolCalls: list[toolCall] = []
        self.queuedUserMessage: str | None = None
        if resume:
            # system（含创建时注入的时间戳）也从日志恢复，保证 resume 前缀与上次完全一致、缓存可命中。
            self._resumeFromLog()
        else:
            self.appendSystemMessage(systemPrompt)

    def hasPending(self) -> bool:
        return self.pending is not None

    def setPending(self, pending: pendingConfirm) -> None:
        self.pending = pending

    def takePending(self) -> pendingConfirm | None:
        pending = self.pending
        self.pending = None
        return pending

    def takeDanglingToolCalls(self) -> list[toolCall]:
        calls = self.danglingToolCalls
        self.danglingToolCalls = []
        return calls

    def setQueuedUserMessage(self, message: str) -> None:
        self.queuedUserMessage = message

    def takeQueuedUserMessage(self) -> str | None:
        message = self.queuedUserMessage
        self.queuedUserMessage = None
        return message

    def _resumeFromLog(self) -> None:
        events = self.logger.readEvents()
        openCallIds: list[str] = []
        for event in events:
            eventType = event.get('type')
            if eventType == 'systemMessage':
                # 恢复创建时的 system（含当时注入的时间戳），保证前缀与历史一致。
                if not any(m.role == 'system' for m in self.messages):
                    self.messages.append(chatMessage(role='system', content=event.get('content', '')))
                continue
            if eventType == 'modelError':
                continue
            if eventType == 'userMessage':
                self._closeOrphanToolCalls(openCallIds)
                self.messages.append(chatMessage(role='user', content=event.get('content', '')))
            elif eventType == 'assistantMessage':
                self._closeOrphanToolCalls(openCallIds)
                toolCalls = [
                    toolCall(id=tc.get('id', ''), toolName=tc.get('toolName', ''), arguments=tc.get('arguments', {}))
                    for tc in (event.get('toolCalls') or [])
                ]
                self.messages.append(chatMessage(role='assistant', content=event.get('content', ''), toolCalls=toolCalls))
                openCallIds.extend(tc.id for tc in toolCalls)
                self._accumulateUsage(event.get('usage'))
            elif eventType == 'toolResult':
                callId = event.get('toolCallId', '')
                if callId in openCallIds:
                    openCallIds.remove(callId)
                    self.messages.append(chatMessage(
                        role='tool',
                        content=event.get('content', ''),
                        toolCallId=callId,
                        name=event.get('toolName'),
                    ))
        self.danglingToolCalls = self._collectDanglingCalls(openCallIds)
        if self.debugConsole:
            self.debugConsole.debug(
                f'resume 重放完成 sessionId={self.sessionId} events={len(events)} '
                f'messages={len(self.messages)} dangling={len(self.danglingToolCalls)} usage={self.usageTotal}'
            )

    def _accumulateUsage(self, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        self.usageTotal['promptTokens'] += int(usage.get('prompt_tokens', 0) or 0)
        details = usage.get('prompt_tokens_details') or {}
        self.usageTotal['cachedTokens'] += int(details.get('cached_tokens', 0) or 0)
        self.usageTotal['completionTokens'] += int(usage.get('completion_tokens', 0) or 0)

    def _closeOrphanToolCalls(self, openCallIds: list[str]) -> None:
        # 崩溃兜底：assistant 发出 tool_calls 后没等到 toolResult 就来了下一条 user/assistant，补占位 toolResult 让序列合法。
        while openCallIds:
            callId = openCallIds.pop(0)
            self.messages.append(chatMessage(
                role='tool',
                content='该工具调用因会话中断未完成。',
                toolCallId=callId,
                name=None,
            ))

    def _collectDanglingCalls(self, openCallIds: list[str]) -> list[toolCall]:
        # 从尾部向前找最近一条带 toolCalls 的 assistant，收集仍未闭合的 tool_calls（批次部分执行后挂起时尾部是 toolResult 而非 assistant）。
        if not openCallIds:
            return []
        remaining = set(openCallIds)
        for message in reversed(self.messages):
            if message.role == 'assistant' and message.toolCalls:
                return [tc for tc in message.toolCalls if tc.id in remaining]
        return []

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
        self._accumulateUsage(responsePayload.get('usage'))
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
