'''
Author: wilbur
Version: 1.10
Date: 2026-07-26
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state. v1.10 adds the event-stream API (docs/streamOutputPlan.md §6, v2.3 定稿): runUserMessageStream/continueConfirmationStream generators yield 7 event types with real-time text/reasoning deltas; terminal events (completed/confirmationRequired/error) are yielded only after the session lock is released; legacy sync APIs runUserMessage/continueConfirmation are kept as thin wrappers that drain the stream, map terminal events back to runResult, and accept optional onDelta/onReasoning callbacks.
'''

from __future__ import annotations

from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator
from uuid import uuid4

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.ports import modelAdapterPort
from flamingoAgents.core.types import (
    completedEvent,
    confirmationRequiredEvent,
    errorEvent,
    finalChunk,
    pendingConfirm,
    reasoningChunk,
    reasoningDeltaEvent,
    runResult,
    terminalEventTypes,
    textChunk,
    textDeltaEvent,
    toolCall,
    toolCallEndEvent,
    toolCallStartEvent,
    toolContext,
    toolResult,
)
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

    # ---------- 事件流 API（docs/streamOutputPlan.md §6.3） ----------

    def runUserMessageStream(self, message: str, sessionId: str) -> Iterator:
        cleanMessage = message.strip()
        if not cleanMessage:
            yield errorEvent(message='消息不能为空。', errorType='emptyMessage')
            return
        terminal = None
        with self.getSessionLock(sessionId):
            for event in self.driveUserMessage(sessionId, cleanMessage):
                if isinstance(event, terminalEventTypes):
                    terminal = event
                    break
                yield event
        # 终态事件在锁释放之后再 yield：消费者收到时锁必然已释放（§6.4）。
        if terminal is not None:
            yield terminal

    def continueConfirmationStream(self, sessionId: str, confirmationId: str, approved: bool) -> Iterator:
        terminal = None
        with self.getSessionLock(sessionId):
            for event in self.driveConfirmation(sessionId, confirmationId, approved):
                if isinstance(event, terminalEventTypes):
                    terminal = event
                    break
                yield event
        if terminal is not None:
            yield terminal

    def driveUserMessage(self, sessionId: str, cleanMessage: str) -> Iterator:
        # 调用前提：已持有会话锁。
        if self.hasPendingConfirmation(sessionId):
            yield errorEvent(
                message='当前会话有待确认工具调用，请先调用 continueConfirmation。',
                errorType='pendingConfirmationExists',
            )
            return
        if self.debugConsole:
            self.debugConsole.debug(f'收到用户消息 sessionId={sessionId} chars={len(cleanMessage)}')
        currentConversation = self.getConversation(sessionId)
        dangling = currentConversation.takeDanglingToolCalls()
        if dangling:
            currentConversation.setQueuedUserMessage(cleanMessage)
            terminated = yield from self.driveToolBatch(sessionId, dangling, 0)
            if terminated:
                return
            queued = currentConversation.takeQueuedUserMessage()
            if queued:
                currentConversation.appendUserMessage(queued)
            yield from self.driveModelLoop(sessionId)
            return
        currentConversation.appendUserMessage(cleanMessage)
        yield from self.driveModelLoop(sessionId)

    def driveConfirmation(self, sessionId: str, confirmationId: str, approved: bool) -> Iterator:
        # 调用前提：已持有会话锁。
        currentConversation = self.getConversation(sessionId)
        pending = currentConversation.takePending()
        if pending is None or pending.confirmationId != confirmationId:
            if pending is not None:
                currentConversation.setPending(pending)
            yield errorEvent(
                message='确认请求不存在或 confirmationId 不匹配。',
                errorType='confirmationMismatch',
            )
            return
        currentCall = pending.toolCalls[pending.currentIndex]
        if self.debugConsole:
            self.debugConsole.debug(
                f'继续确认 sessionId={sessionId} confirmationId={confirmationId} '
                f'approved={approved} tool={currentCall.toolName} callId={currentCall.id}'
            )
        if approved:
            definition = self.toolRegistry.get(currentCall.toolName)
            preview = self.buildToolPreview(definition, currentCall) if definition else str(currentCall.arguments)
            yield toolCallStartEvent(toolCall=currentCall, preview=preview)
            result = self.executeToolCall(currentCall)
        else:
            # 拒绝路径只发 End（配对不变式例外，§6.2）。
            result = self.buildBlockedToolResult(currentCall, pending.reason)
        currentConversation.addToolResult(result)
        yield toolCallEndEvent(toolResult=result)
        terminated = yield from self.driveToolBatch(sessionId, pending.toolCalls, pending.currentIndex + 1)
        if terminated:
            return
        queued = currentConversation.takeQueuedUserMessage()
        if queued:
            currentConversation.appendUserMessage(queued)
        yield from self.driveModelLoop(sessionId)

    def driveModelLoop(self, sessionId: str) -> Iterator:
        # 调用前提：已持有会话锁。承载原 continueModelLoop 逻辑。
        currentConversation = self.getConversation(sessionId)
        for stepIndex in range(self.maxModelSteps):
            modelTools = buildModelTools(self.toolRegistry.list())
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(currentConversation.messages)} tools={len(modelTools)}'
                )
            try:
                completion = None
                for chunk in self.modelAdapter.completeStream(currentConversation.messages, modelTools):
                    if isinstance(chunk, textChunk):
                        yield textDeltaEvent(text=chunk.text)
                    elif isinstance(chunk, reasoningChunk):
                        yield reasoningDeltaEvent(text=chunk.text)
                    elif isinstance(chunk, finalChunk):
                        completion = chunk.completion
                if completion is None:
                    raise RuntimeError('模型流式响应缺少最终结果。')
            except Exception as error:
                self.logModelError(currentConversation, error)
                yield errorEvent(message=f'模型调用失败：{error}', errorType=type(error).__name__)
                return

            responsePayload = getattr(completion, 'responsePayload', None)
            assistantMessage = completion.message
            currentConversation.appendAssistantMessage(
                assistantMessage,
                responsePayload if isinstance(responsePayload, dict) else {},
            )
            if not assistantMessage.toolCalls:
                if self.debugConsole:
                    self.debugConsole.debug(f'模型循环完成 sessionId={sessionId} contentChars={len(assistantMessage.content)}')
                yield completedEvent(message=assistantMessage.content)
                return

            terminated = yield from self.driveToolBatch(sessionId, assistantMessage.toolCalls, 0)
            if terminated:
                return

        yield errorEvent(
            message=f'模型循环超过最大步数：{self.maxModelSteps}',
            errorType='maxStepsExceeded',
        )

    def driveToolBatch(self, sessionId: str, toolCalls: list[toolCall], startIndex: int) -> Iterator:
        # 调用前提：已持有会话锁。承载原 processToolBatch 逻辑；返回 True 表示已产出终态事件（confirmationRequired）。
        currentConversation = self.getConversation(sessionId)
        for index in range(startIndex, len(toolCalls)):
            call = toolCalls[index]
            definition = self.toolRegistry.get(call.toolName)
            if definition is None:
                # 未知工具不执行，但 Start/End 都发，保持配对（§6.2）。
                yield toolCallStartEvent(toolCall=call, preview=str(call.arguments))
                result = self.makeUnknownToolResult(call)
                currentConversation.addToolResult(result)
                yield toolCallEndEvent(toolResult=result)
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
                yield confirmationRequiredEvent(
                    confirmationId=confirmationId,
                    reason=decision.reason,
                    commandPreview=self.buildToolPreview(definition, call),
                    toolCall=call,
                )
                return True
            yield toolCallStartEvent(toolCall=call, preview=self.buildToolPreview(definition, call))
            result = self.executeToolCall(call)
            currentConversation.addToolResult(result)
            yield toolCallEndEvent(toolResult=result)
        return False

    # ---------- 同步 API（事件流的薄包装，§6.5） ----------

    def runUserMessage(
        self,
        message: str,
        sessionId: str | None = None,
        onDelta: Callable[[str], None] | None = None,
        onReasoning: Callable[[str], None] | None = None,
    ) -> runResult:
        # sessionId 必须包装层预生成：事件流不带 sessionId，否则耗尽后无从构造 runResult。
        realSessionId = sessionId or self.createSessionId()
        stream = self.runUserMessageStream(message, realSessionId)
        try:
            terminal = None
            for event in stream:
                if isinstance(event, textDeltaEvent):
                    self.safeCallback(onDelta, event.text)
                elif isinstance(event, reasoningDeltaEvent):
                    self.safeCallback(onReasoning, event.text)
                elif isinstance(event, terminalEventTypes):
                    terminal = event
        finally:
            stream.close()
        return self.toRunResult(realSessionId, terminal)

    def continueConfirmation(
        self,
        sessionId: str,
        confirmationId: str,
        approved: bool,
        onDelta: Callable[[str], None] | None = None,
        onReasoning: Callable[[str], None] | None = None,
    ) -> runResult:
        stream = self.continueConfirmationStream(sessionId, confirmationId, approved)
        try:
            terminal = None
            for event in stream:
                if isinstance(event, textDeltaEvent):
                    self.safeCallback(onDelta, event.text)
                elif isinstance(event, reasoningDeltaEvent):
                    self.safeCallback(onReasoning, event.text)
                elif isinstance(event, terminalEventTypes):
                    terminal = event
        finally:
            stream.close()
        return self.toRunResult(sessionId, terminal)

    def toRunResult(self, sessionId: str, terminal) -> runResult:
        if isinstance(terminal, completedEvent):
            return runResult(sessionId=sessionId, status='completed', message=terminal.message)
        if isinstance(terminal, confirmationRequiredEvent):
            return runResult(
                sessionId=sessionId,
                status='confirmationRequired',
                confirmationId=terminal.confirmationId,
                reason=terminal.reason,
                commandPreview=terminal.commandPreview,
                toolCall=terminal.toolCall,
            )
        if isinstance(terminal, errorEvent):
            return runResult(sessionId=sessionId, status='error', message=terminal.message)
        return runResult(sessionId=sessionId, status='error', message='事件流未产生终态事件。')

    def safeCallback(self, callback: Callable[[str], None] | None, text: str) -> None:
        # 回调异常静默吞掉（仅 debug 日志），不阻断流式拼接（§3 方案 B 边界约定）。
        if callback is None:
            return
        try:
            callback(text)
        except Exception as error:
            if self.debugConsole:
                self.debugConsole.debug(f'流式回调异常已忽略 error={type(error).__name__}: {error}')

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
