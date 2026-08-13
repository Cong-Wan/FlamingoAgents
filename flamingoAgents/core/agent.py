'''
Author: wilbur
Version: 1.16
Date: 2026-08-13
Description: Coordinates pure Agent sessions using a callable tool registry and per-session confirmation state. v1.10 adds the event-stream API (docs/streamOutputPlan.md §6, v2.3 定稿): runUserMessageStream/continueConfirmationStream generators yield 7 event types with real-time text/reasoning deltas; terminal events (completed/confirmationRequired/error) are yielded only after the session lock is released; legacy sync APIs runUserMessage/continueConfirmation are kept as thin wrappers that drain the stream, map terminal events back to runResult, and accept optional onDelta/onReasoning callbacks. v1.11 调整 maxModelSteps 默认值 8 -> 32。v1.12（streamingLatencyFixPlan Phase3/D2）：driveToolBatch 改为「可执行前缀批量 Start」——从 startIndex 起连续可执行（未知或免确认）的工具先全部 yield toolCallStart，再串行 exec + toolCallEnd；遇需确认工具不发 Start，直接 setPending + confirmationRequired 终态（契约 §6.2 红线不变）。v1.13 模型调用重试：连接建立期(chunkSeen=False)可重试错误 3 次指数退避，分片可中断，retryNotice 通知前端。v1.14 maxModelSteps 支持 None/<=0 表示不限制模型循环步数。v1.15（stopResponsivenessPlan L3）：interruptEvent + interruptActiveStreams 薄封装；driveModelLoop 透传 stopEvent、except modelInterruptedError 直通 return、completion is None 先查中断、退避片末尾检查。v1.16 中断事件改按会话存储（interruptEvents dict + getInterruptEvent），修复验收发现的「一会在飞 + 他会话新流 clear 误杀在飞中断」竞态。
'''

from __future__ import annotations

import threading
import time
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
    modelInterruptedError,
    pendingConfirm,
    reasoningChunk,
    reasoningDeltaEvent,
    retryNoticeEvent,
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

MODEL_RETRY_MAX_ATTEMPTS = 3        # 最多重试 3 次（即最多 4 次尝试）
MODEL_RETRY_BACKOFF_BASE_SECONDS = 1.0
MODEL_RETRY_BACKOFF_MAX_SECONDS = 8.0
MODEL_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)


class agent:
    def __init__(
        self,
        modelAdapter: modelAdapterPort,
        toolDefinitions: list[toolDefinition],
        workDir: Path,
        logDir: Path,
        systemPrompt: str,
        debugConsole=None,
        maxModelSteps: int | None = None,
    ):
        self.modelAdapter = modelAdapter
        self.toolRegistry = toolRegistry(toolDefinitions, debugConsole=debugConsole)
        self.workDir = workDir
        self.logDir = logDir
        self.systemPrompt = systemPrompt
        self.debugConsole = debugConsole
        # None 或 <=0：不限制模型循环步数；>0：硬上限
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversation] = {}
        self.sessionLocks: dict[str, RLock] = {}
        self.sessionLocksGuard = RLock()
        # 按会话的中断事件（Task A 验收修复）：单 Event 放实例上会在「一会在飞 + 他会话新流 clear」时被误清。
        self.interruptEvents: dict[str, threading.Event] = {}

    def getInterruptEvent(self, sessionId: str) -> threading.Event:
        # 与会话锁同锁获取，保证 driveModelLoop 的 clear 与 requestStop 的 set 不会互相覆盖。
        with self.sessionLocksGuard:
            event = self.interruptEvents.get(sessionId)
            if event is None:
                event = threading.Event()
                self.interruptEvents[sessionId] = event
            return event

    def interruptActiveStreams(self, sessionId: str):
        self.getInterruptEvent(sessionId).set()
        interruptFn = getattr(self.modelAdapter, 'interruptActiveStreams', None)
        if interruptFn is None:
            return
        try:
            interruptFn()
        except Exception:
            pass

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
        # maxModelSteps 为 None 或 <=0 时不限制步数。
        interruptEvent = self.getInterruptEvent(sessionId)
        interruptEvent.clear()
        currentConversation = self.getConversation(sessionId)
        stepIndex = 0
        while True:
            if self.maxModelSteps is not None and self.maxModelSteps > 0 and stepIndex >= self.maxModelSteps:
                yield errorEvent(
                    message=f'模型循环超过最大步数：{self.maxModelSteps}',
                    errorType='maxStepsExceeded',
                )
                return
            modelTools = buildModelTools(self.toolRegistry.list())
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(currentConversation.messages)} tools={len(modelTools)}'
                )
            completion = None
            for attempt in range(MODEL_RETRY_MAX_ATTEMPTS + 1):
                chunkSeen = False
                try:
                    for chunk in self.modelAdapter.completeStream(currentConversation.messages, modelTools, stopEvent=interruptEvent):
                        if isinstance(chunk, textChunk):
                            chunkSeen = True
                            yield textDeltaEvent(text=chunk.text)
                        elif isinstance(chunk, reasoningChunk):
                            chunkSeen = True
                            yield reasoningDeltaEvent(text=chunk.text)
                        elif isinstance(chunk, finalChunk):
                            chunkSeen = True
                            completion = chunk.completion
                    if completion is None:
                        if interruptEvent.is_set():
                            return
                        raise RuntimeError('模型流式响应缺少最终结果。')
                    break
                except modelInterruptedError:
                    return
                except Exception as error:
                    self.logModelError(currentConversation, error)
                    statusCode = getattr(error, 'statusCode', None)
                    hasStatusAttr = hasattr(error, 'statusCode')
                    isRetryable = hasStatusAttr and (
                        statusCode in MODEL_RETRYABLE_STATUS_CODES or statusCode is None
                    )
                    if chunkSeen or not isRetryable or attempt >= MODEL_RETRY_MAX_ATTEMPTS:
                        yield errorEvent(
                            message=f'模型调用失败（已重试{attempt}次）：{error}',
                            errorType=type(error).__name__,
                        )
                        return
                    backoff = min(
                        MODEL_RETRY_BACKOFF_MAX_SECONDS,
                        MODEL_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt),
                    )
                    retryAfterSeconds = getattr(error, 'retryAfterSeconds', None)
                    if retryAfterSeconds is not None:
                        backoff = max(backoff, float(retryAfterSeconds))
                    yield retryNoticeEvent(
                        message=str(error),
                        attempt=attempt + 1,
                        retryAfterMs=int(backoff * 1000),
                        status='waiting',
                    )
                    remaining = backoff
                    while remaining > 0:
                        sliceSeconds = min(0.1, remaining)
                        time.sleep(sliceSeconds)
                        remaining -= sliceSeconds
                        if interruptEvent.is_set():
                            return
                        yield retryNoticeEvent(
                            message=str(error),
                            attempt=attempt + 1,
                            retryAfterMs=int(remaining * 1000),
                            status='waiting',
                        )

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
            stepIndex += 1

    def driveToolBatch(self, sessionId: str, toolCalls: list[toolCall], startIndex: int) -> Iterator:
        # 调用前提：已持有会话锁。返回 True 表示已产出终态事件（confirmationRequired）。
        # 可执行前缀批量 Start（streamingLatencyFixPlan D2）：从 startIndex 起连续可执行（未知或免确认）的工具
        # 先全部 yield Start，再串行 exec + End；遇需确认工具停止扩展前缀，不发 Start，直接 confirmationRequired。
        currentConversation = self.getConversation(sessionId)
        index = startIndex
        while index < len(toolCalls):
            # 1) 收集可执行前缀：unknown 或免确认；遇 requiresApproval 停止扩展
            prefix: list[tuple[toolCall, toolDefinition | None]] = []
            while index + len(prefix) < len(toolCalls):
                call = toolCalls[index + len(prefix)]
                definition = self.toolRegistry.get(call.toolName)
                if definition is None:
                    prefix.append((call, None))
                    continue
                decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
                if decision.requiresApproval:
                    break
                prefix.append((call, definition))
            # 2) 前缀全部 Start（泵在每个 yield 后即 broadcast，多张卡先进入 running 语义）
            for call, definition in prefix:
                preview = str(call.arguments) if definition is None else self.buildToolPreview(definition, call)
                yield toolCallStartEvent(toolCall=call, preview=preview)
            # 3) 前缀串行 exec + End；jsonl 仍只按执行顺序写 toolResult，落盘语义不变
            for call, definition in prefix:
                result = self.makeUnknownToolResult(call) if definition is None else self.executeToolCall(call)
                currentConversation.addToolResult(result)
                yield toolCallEndEvent(toolResult=result)
            index += len(prefix)
            # 4) 下一项需确认（prefix 为空即首项需确认）：不 Start，直接终态
            if index < len(toolCalls):
                call = toolCalls[index]
                definition = self.toolRegistry.get(call.toolName)
                decision = evaluateToolCall(definition, call, debugConsole=self.debugConsole)
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
