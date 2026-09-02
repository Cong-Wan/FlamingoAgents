'''
Author: wilbur
Version: 1.12
Date: 2026-09-01
Description: Maintains and resumes per-session JSONL conversations. v1.12 persists JSON-safe assistant/toolCall providerData for Responses opaque replay, restores malformed/legacy values as empty objects, and keeps reasoning display text separate from model content.
'''

from __future__ import annotations

import json
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
        self.lastTurnTokens: int = 0  # 最近一次调用的 prompt+completion（下一请求上下文规模估计）
        self._stopRequestedLogged: bool = False
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
            if eventType == 'stopRequested':
                self._stopRequestedLogged = True
                continue
            if eventType == 'userMessage':
                self._closeOrphanToolCalls(openCallIds)
                self.messages.append(chatMessage(role='user', content=event.get('content', '')))
            elif eventType == 'assistantMessage':
                self._closeOrphanToolCalls(openCallIds)
                toolCalls = [
                    toolCall(
                        id=tc.get('id', ''),
                        toolName=tc.get('toolName', ''),
                        arguments=tc.get('arguments', {}) if isinstance(tc.get('arguments'), dict) else {},
                        providerData=normalizeProviderData(tc.get('providerData')),
                    )
                    for tc in (event.get('toolCalls') or [])
                    if isinstance(tc, dict)
                ]
                self.messages.append(chatMessage(
                    role='assistant',
                    content=event.get('content', ''),
                    toolCalls=toolCalls,
                    providerData=normalizeProviderData(event.get('providerData')),
                ))
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
        if openCallIds:
            reason = 'userStopped' if self._stopRequestedLogged else 'crashRecovered'
            content = (
                '该工具调用因用户停止未完成；停止前可能已产生文件或命令副作用。'
                if reason == 'userStopped'
                else '会话恢复时发现该工具调用未完成；为避免重复副作用未重新执行，停止前可能已产生文件或命令副作用。'
            )
            remaining = set(openCallIds)
            danglingCalls: list[toolCall] = []
            for message in reversed(self.messages):
                if message.role == 'assistant' and message.toolCalls:
                    danglingCalls = [tc for tc in message.toolCalls if tc.id in remaining]
                    break
            for call in danglingCalls:
                self.addToolResult(toolResult(
                    toolCallId=call.id,
                    toolName=call.toolName,
                    isError=True,
                    content=content,
                    details={'cancelled': True, 'reason': reason},
                ))
        if self.debugConsole:
            self.debugConsole.debug(
                f'resume 重放完成 sessionId={self.sessionId} events={len(events)} '
                f'messages={len(self.messages)} unclosed={len(openCallIds)} usage={self.usageTotal}'
            )

    def _accumulateUsage(self, usage: dict | None) -> None:
        if not isinstance(usage, dict):
            return
        promptTokens = int(usage.get('prompt_tokens', 0) or 0)
        completionTokens = int(usage.get('completion_tokens', 0) or 0)
        self.usageTotal['promptTokens'] += promptTokens
        details = usage.get('prompt_tokens_details') or {}
        self.usageTotal['cachedTokens'] += int(details.get('cached_tokens', 0) or 0)
        self.usageTotal['completionTokens'] += completionTokens
        self.lastTurnTokens = promptTokens + completionTokens

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
        message.providerData = normalizeProviderData(message.providerData)
        for call in message.toolCalls:
            call.providerData = normalizeProviderData(call.providerData)
        toolCallCount = len(message.toolCalls)
        reasoning = responsePayload.get('reasoning')
        if self.debugConsole:
            self.debugConsole.debug(
                f'记录 assistantMessage contentChars={len(message.content)} '
                f'toolCalls={toolCallCount} model={responsePayload.get("model")}'
            )
        event = {
            'type': 'assistantMessage',
            'model': responsePayload.get('model'),
            'content': message.content,
            'toolCalls': message.toolCalls,
            'providerData': message.providerData,
            'usage': responsePayload.get('usage'),
            'timings': responsePayload.get('timings'),
        }
        if reasoning:
            event['reasoning'] = reasoning
        self.logger.logEvent(event)
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


def normalizeProviderData(value) -> dict:
    if not isinstance(value, dict):
        return {}
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        normalized = json.loads(serialized)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return normalized if isinstance(normalized, dict) else {}
