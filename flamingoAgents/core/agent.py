'''
Author: wilbur
Version: 1.9
Date: 2026-07-24
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state. System prompt is injected at construction; model turns are logged as atomic events (systemMessage/userMessage/assistantMessage/toolResult) instead of full request/response payloads. v1.9 wires session resume: dateless per-session log path, dangling tool-call closure on resume, and queued user-message flush after confirmation.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.ports import modelAdapterPort
from flamingoAgents.core.types import pendingConfirm, runResult, toolCall, toolContext, toolResult
from flamingoAgents.tools.toolDefinition import toolDefinition
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRegistry import toolRegistry
from flamingoAgents.tools.toolRuntime import executeToolCall as executeCallableToolCall
from flamingoAgents.tools.toolSchema import buildModelTools


class agent:
    def __init__(
        self,
        modelAdapter: modelAdapterPort,
        toolDefinitions: list[toolDefinition],
        workDir: Path,
        logDir: Path,
        systemPrompt: str,
        debugConsole=None,
        maxModelSteps: int = 8,
    ):
        self.modelAdapter = modelAdapter
        self.toolRegistry = toolRegistry(toolDefinitions, debugConsole=debugConsole)
        self.workDir = workDir
        self.logDir = logDir
        self.systemPrompt = systemPrompt
        self.debugConsole = debugConsole
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversation] = {}
        self.sessionLocks: dict[str, RLock] = {}
        self.sessionLocksGuard = RLock()

    def runUserMessage(self, message: str, sessionId: str | None = None) -> runResult:
        cleanMessage = message.strip()
        realSessionId = sessionId or self.createSessionId()
        if not cleanMessage:
            return runResult(sessionId=realSessionId, status='error', message='消息不能为空。')
        with self.getSessionLock(realSessionId):
            if self.hasPendingConfirmation(realSessionId):
                return runResult(
                    sessionId=realSessionId,
                    status='error',
                    message='当前会话有待确认工具调用，请先调用 continueConfirmation。',
                )
            if self.debugConsole:
                self.debugConsole.debug(f'收到用户消息 sessionId={realSessionId} chars={len(cleanMessage)}')
            currentConversation = self.getConversation(realSessionId)
            dangling = currentConversation.takeDanglingToolCalls()
            if dangling:
                currentConversation.setQueuedUserMessage(cleanMessage)
                batchResult = self.processToolBatch(realSessionId, dangling, 0)
                if batchResult is not None:
                    return batchResult
                queued = currentConversation.takeQueuedUserMessage()
                if queued:
                    currentConversation.appendUserMessage(queued)
                return self.continueModelLoop(realSessionId)
            currentConversation.appendUserMessage(cleanMessage)
            return self.continueModelLoop(realSessionId)

    def continueConfirmation(self, sessionId: str, confirmationId: str, approved: bool) -> runResult:
        with self.getSessionLock(sessionId):
            currentConversation = self.getConversation(sessionId)
            pending = currentConversation.takePending()
            if pending is None or pending.confirmationId != confirmationId:
                if pending is not None:
                    currentConversation.setPending(pending)
                return runResult(sessionId=sessionId, status='error', message='确认请求不存在或 confirmationId 不匹配。')
            currentCall = pending.toolCalls[pending.currentIndex]
            if self.debugConsole:
                self.debugConsole.debug(
                    f'继续确认 sessionId={sessionId} confirmationId={confirmationId} '
                    f'approved={approved} tool={currentCall.toolName} callId={currentCall.id}'
                )
            if approved:
                result = self.executeToolCall(currentCall)
            else:
                result = self.buildBlockedToolResult(currentCall, pending.reason)
            currentConversation.addToolResult(result)
            batchResult = self.processToolBatch(sessionId, pending.toolCalls, pending.currentIndex + 1)
            if batchResult is not None:
                return batchResult
            queued = currentConversation.takeQueuedUserMessage()
            if queued:
                currentConversation.appendUserMessage(queued)
            return self.continueModelLoop(sessionId)

    def continueModelLoop(self, sessionId: str) -> runResult:
        currentConversation = self.getConversation(sessionId)
        for stepIndex in range(self.maxModelSteps):
            modelTools = buildModelTools(self.toolRegistry.list())
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(currentConversation.messages)} tools={len(modelTools)}'
                )
            try:
                completion = self.modelAdapter.complete(currentConversation.messages, modelTools)
            except Exception as error:
                self.logModelError(currentConversation, error)
                return runResult(sessionId=sessionId, status='error', message=f'模型调用失败：{error}')

            responsePayload = getattr(completion, 'responsePayload', None)
            assistantMessage = completion.message
            currentConversation.appendAssistantMessage(
                assistantMessage,
                responsePayload if isinstance(responsePayload, dict) else {},
            )
            if not assistantMessage.toolCalls:
                if self.debugConsole:
                    self.debugConsole.debug(f'模型循环完成 sessionId={sessionId} contentChars={len(assistantMessage.content)}')
                return runResult(sessionId=sessionId, status='completed', message=assistantMessage.content)

            batchResult = self.processToolBatch(sessionId, assistantMessage.toolCalls, 0)
            if batchResult is not None:
                return batchResult

        return runResult(
            sessionId=sessionId,
            status='error',
            message=f'模型循环超过最大步数：{self.maxModelSteps}',
        )

    def processToolBatch(self, sessionId: str, toolCalls: list[toolCall], startIndex: int) -> runResult | None:
        currentConversation = self.getConversation(sessionId)
        for index in range(startIndex, len(toolCalls)):
            call = toolCalls[index]
            definition = self.toolRegistry.get(call.toolName)
            if definition is None:
                currentConversation.addToolResult(self.makeUnknownToolResult(call))
                continue
            decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
            if decision.requiresApproval:
                confirmationId = 'confirm_' + uuid4().hex[:12]
                currentConversation.setPending(pendingConfirm(
                    sessionId=sessionId,
                    confirmationId=confirmationId,
                    reason=decision.reason,
                    toolCalls=toolCalls,
                    currentIndex=index,
                ))
                if self.debugConsole:
                    self.debugConsole.debug(
                        f'工具需要确认 sessionId={sessionId} confirmationId={confirmationId} '
                        f'tool={call.toolName} callId={call.id} permissionId={decision.permissionId}'
                    )
                return runResult(
                    sessionId=sessionId,
                    status='confirmationRequired',
                    confirmationId=confirmationId,
                    reason=decision.reason,
                    commandPreview=self.buildToolPreview(definition, call),
                    toolCall=call,
                )
            result = self.executeToolCall(call)
            currentConversation.addToolResult(result)
        return None

    def executeToolCall(self, call: toolCall) -> toolResult:
        definition = self.toolRegistry.get(call.toolName)
        if definition is None:
            return self.makeUnknownToolResult(call)
        context = toolContext(workDir=self.workDir, debugConsole=self.debugConsole)
        return executeCallableToolCall(definition, call, context)

    def buildToolPreview(self, definition: toolDefinition, call: toolCall) -> str:
        if definition.preview is not None and isinstance(call.arguments, dict):
            try:
                preview = definition.preview(call.arguments)
                if preview:
                    return preview
            except Exception as error:
                if self.debugConsole:
                    self.debugConsole.debug(
                        f'工具预览生成失败 tool={definition.name} callId={call.id} '
                        f'error={type(error).__name__}: {error}'
                    )
        return str(call.arguments)

    def makeUnknownToolResult(self, call: toolCall) -> toolResult:
        return toolResult(
            toolCallId=call.id,
            toolName=call.toolName,
            isError=True,
            content=f'未知工具：{call.toolName}',
            details={'unknownTool': True},
        )

    def buildBlockedToolResult(self, call: toolCall, reason: str) -> toolResult:
        return toolResult(
            toolCallId=call.id,
            toolName=call.toolName,
            isError=True,
            content=f'命令已被用户拒绝：{reason}。',
            details={'blocked': True, 'reason': 'userRejectedApproval'},
        )

    def logModelError(self, currentConversation: conversation, error: Exception) -> None:
        event: dict[str, Any] = {
            'type': 'modelError',
            'errorType': type(error).__name__,
            'message': str(error),
        }
        requestPayload = getattr(error, 'requestPayload', None)
        if isinstance(requestPayload, dict):
            event['request'] = requestPayload
        statusCode = getattr(error, 'statusCode', None)
        if isinstance(statusCode, int):
            event['status'] = statusCode
        currentConversation.logger.logEvent(event)

    def hasPendingConfirmation(self, sessionId: str) -> bool:
        with self.sessionLocksGuard:
            conversation = self.conversations.get(sessionId)
        if conversation is None:
            return False
        return conversation.hasPending()

    def getSessionLock(self, sessionId: str) -> RLock:
        with self.sessionLocksGuard:
            lock = self.sessionLocks.get(sessionId)
            if lock is None:
                lock = RLock()
                self.sessionLocks[sessionId] = lock
            return lock

    def getConversation(self, sessionId: str) -> conversation:
        with self.sessionLocksGuard:
            existing = self.conversations.get(sessionId)
            if existing is not None:
                return existing
            logPath = self.logDir / f'{sessionId}.jsonl'
            newConversation = conversation(
                sessionId=sessionId,
                logPath=logPath,
                systemPrompt=self.systemPrompt,
                debugConsole=self.debugConsole,
                resume=logPath.exists(),
            )
            self.conversations[sessionId] = newConversation
            return newConversation

    def createSessionId(self) -> str:
        return 'session_' + uuid4().hex[:12]
