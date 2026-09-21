'''
Author: wilbur
Version: 1.25
Date: 2026-09-21
Description: Coordinates event-stream Agent sessions, tool execution, retry, interruption, persistence, and confirmation state. v1.25 每个物理 attempt 在请求前生成 usageKey，合法 terminal usage 先写 JSONL usageRecord 再幂等落入统一账本。
'''

from __future__ import annotations

import threading
import time
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Iterator
from uuid import uuid4

from flamingoAgents.core.conversation import conversation
from flamingoAgents.core.imageInput import (
    checkMessageImageBudget,
    hydrateMessageImages,
    imageInputError,
    imageRefMeta,
    redactImageData,
    requireImageCapability,
    storeImages,
    validateImageBytes,
)
from flamingoAgents.core.ports import modelAdapterPort
from flamingoAgents.core.types import (
    completedEvent,
    confirmationRequiredEvent,
    errorEvent,
    finalChunk,
    inputImage,
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
    usageUpdateEvent,
)
from flamingoAgents.utils.logPaths import newSessionId
from flamingoAgents.utils import usageLedger
from flamingoAgents.tools.toolDefinition import toolDefinition
from flamingoAgents.tools.toolPolicy import evaluateToolCall
from flamingoAgents.tools.toolRegistry import toolRegistry
from flamingoAgents.tools.toolRuntime import executeToolCall as executeCallableToolCall
from flamingoAgents.tools.toolSchema import buildModelTools

MODEL_RETRY_MAX_ATTEMPTS = 3        # 最多重试 3 次（即最多 4 次尝试）
MODEL_RETRY_BACKOFF_BASE_SECONDS = 1.0
MODEL_RETRY_BACKOFF_MAX_SECONDS = 8.0
MODEL_RETRY_RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
MODEL_RETRYABLE_STATUS_CODES = MODEL_RETRY_RETRYABLE_STATUS_CODES
CANCELLED_TOOL_CONTENTS = {
    'userStopped': '该工具调用因用户停止未完成；停止前可能已产生文件或命令副作用。',
    'crashRecovered': '会话恢复时发现该工具调用未完成；为避免重复副作用未重新执行，停止前可能已产生文件或命令副作用。',
    'preflightRepair': '检测到该工具调用缺少结果（协议自愈补齐）；停止前可能已产生文件或命令副作用。',
    'streamClosed': '该工具调用因事件流关闭未完成；关闭前可能已产生文件或命令副作用。',
    'batchFailed': '该工具调用因批次异常未完成；异常前可能已产生文件或命令副作用。',
}


class conversationIntegrityError(RuntimeError):
    pass


@dataclass
class toolBatchEntry:
    index: int
    call: toolCall
    state: str = 'notStarted'
    result: toolResult | None = None


@dataclass
class toolBatchLedger:
    toolCalls: list[toolCall]
    startIndex: int
    entries: list[toolBatchEntry]
    suspendedForConfirmation: bool = False
    assistantPersisted: bool = False
    finished: bool = False


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
        parallelToolNames: frozenset[str] | None = None,
        maxParallelTools: int = 1,
        usageSource: str = 'library',
        parentSessionId: str | None = None,
    ):
        self.modelAdapter = modelAdapter
        self.toolRegistry = toolRegistry(toolDefinitions, debugConsole=debugConsole)
        self.workDir = workDir
        self.logDir = logDir
        self.systemPrompt = systemPrompt
        self.debugConsole = debugConsole
        self.usageSource = usageSource or 'library'
        self.parentSessionId = parentSessionId
        # None 或 <=0：不限制模型循环步数；>0：硬上限
        self.maxModelSteps = maxModelSteps
        if isinstance(maxParallelTools, bool) or not isinstance(maxParallelTools, int) or maxParallelTools < 1 or maxParallelTools > 32:
            raise RuntimeError('maxParallelTools 必须是整数 1..32。')
        self.parallelToolNames = frozenset(parallelToolNames or ())
        self.maxParallelTools = maxParallelTools
        self.conversations: dict[str, conversation] = {}
        self.sessionLocks: dict[str, RLock] = {}
        self.sessionLocksGuard = RLock()
        self.activeRunEvents: dict[str, threading.Event] = {}

    def registerRunEvent(self, sessionId: str, runEvent: threading.Event) -> None:
        with self.sessionLocksGuard:
            self.activeRunEvents[sessionId] = runEvent

    def unregisterRunEvent(self, sessionId: str, runEvent: threading.Event) -> None:
        with self.sessionLocksGuard:
            if self.activeRunEvents.get(sessionId) is runEvent:
                self.activeRunEvents.pop(sessionId, None)

    def getActiveRunEvent(self, sessionId: str) -> threading.Event | None:
        with self.sessionLocksGuard:
            return self.activeRunEvents.get(sessionId)

    def interruptActiveStreams(self, sessionId: str):
        event = self.getActiveRunEvent(sessionId)
        if event is not None:
            event.set()
        interruptFn = getattr(self.modelAdapter, 'interruptActiveStreams', None)
        if interruptFn is None:
            return
        try:
            interruptFn()
        except Exception:
            pass

    # ---------- 事件流 API（docs/streamOutputPlan.md §6.3） ----------

    def runUserMessageStream(
        self,
        message: str,
        sessionId: str,
        *,
        images=None,
        committedImages=None,
        runEvent: threading.Event | None = None,
    ) -> Iterator:
        cleanMessage = message.strip()
        incomingImages = list(images or [])
        if not cleanMessage and not incomingImages:
            yield errorEvent(message='消息不能为空。', errorType='emptyMessage')
            return
        ownedEvent = runEvent if runEvent is not None else threading.Event()
        terminal = None
        inner = None
        try:
            with self.getSessionLock(sessionId):
                self.registerRunEvent(sessionId, ownedEvent)
                try:
                    inner = self.driveUserMessage(sessionId, cleanMessage, incomingImages, committedImages, ownedEvent)
                    try:
                        for event in inner:
                            if isinstance(event, terminalEventTypes):
                                terminal = event
                                break
                            yield event
                    finally:
                        if inner is not None:
                            inner.close()
                finally:
                    self.unregisterRunEvent(sessionId, ownedEvent)
            if self.revokeStoppedConfirmationIfNeeded(sessionId, ownedEvent, terminal):
                return
            if terminal is not None:
                try:
                    yield terminal
                finally:
                    self.revokeStoppedConfirmationIfNeeded(sessionId, ownedEvent, terminal)
        except GeneratorExit:
            self.revokeStoppedConfirmationIfNeeded(sessionId, ownedEvent, terminal)
            raise

    def continueConfirmationStream(
        self,
        sessionId: str,
        confirmationId: str,
        approved: bool,
        *,
        runEvent: threading.Event | None = None,
    ) -> Iterator:
        ownedEvent = runEvent if runEvent is not None else threading.Event()
        terminal = None
        inner = None
        try:
            with self.getSessionLock(sessionId):
                self.registerRunEvent(sessionId, ownedEvent)
                try:
                    inner = self.driveConfirmation(sessionId, confirmationId, approved, ownedEvent)
                    try:
                        for event in inner:
                            if isinstance(event, terminalEventTypes):
                                terminal = event
                                break
                            yield event
                    finally:
                        if inner is not None:
                            inner.close()
                finally:
                    self.unregisterRunEvent(sessionId, ownedEvent)
            if self.revokeStoppedConfirmationIfNeeded(sessionId, ownedEvent, terminal):
                return
            if terminal is not None:
                try:
                    yield terminal
                finally:
                    self.revokeStoppedConfirmationIfNeeded(sessionId, ownedEvent, terminal)
        except GeneratorExit:
            self.revokeStoppedConfirmationIfNeeded(sessionId, ownedEvent, terminal)
            raise

    def driveUserMessage(
        self,
        sessionId: str,
        cleanMessage: str,
        incomingImages=None,
        committedImages=None,
        runEvent: threading.Event | None = None,
    ) -> Iterator:
        # 调用前提：已持有会话锁。
        activeEvent = runEvent if runEvent is not None else threading.Event()
        if self.hasPendingConfirmation(sessionId):
            yield errorEvent(
                message='当前会话有待确认工具调用，请先调用 continueConfirmation。',
                errorType='pendingConfirmationExists',
            )
            return
        if activeEvent.is_set():
            return
        if self.debugConsole:
            self.debugConsole.debug(f'收到用户消息 sessionId={sessionId} chars={len(cleanMessage)}')
        currentConversation = self.getConversation(sessionId)
        try:
            found = self.findUnclosedTailCallIndex(currentConversation)
        except conversationIntegrityError as error:
            yield errorEvent(message=str(error), errorType='conversationIntegrityError')
            return
        if found is not None:
            calls, position = found
            yield from self.closeUnfinishedToolCalls(currentConversation, calls, position, 'preflightRepair')
        storedImages = []
        try:
            requireImageCapability(
                getattr(self.modelAdapter, 'config', None),
                currentConversation.messages,
                extraImages=incomingImages,
            )
            if incomingImages:
                prepared = []
                for image in incomingImages:
                    raw = image.data if isinstance(image.data, (bytes, bytearray)) else b''
                    if not raw:
                        raise imageInputError(f'图片数据为空：{image.name}')
                    mimeType, size = validateImageBytes(raw, image.name)
                    prepared.append(inputImage(
                        name=image.name or 'image',
                        mimeType=mimeType,
                        data=bytes(raw),
                        bytes=size,
                    ))
                checkMessageImageBudget(prepared)
                storedImages = storeImages(currentConversation.logger.logPath, prepared)
                if committedImages is not None:
                    committedImages.clear()
                    committedImages.extend(imageRefMeta(image) for image in storedImages)
        except imageInputError as error:
            yield errorEvent(message=str(error), errorType=error.errorType)
            return
        if activeEvent.is_set():
            return
        currentConversation.appendUserMessage(cleanMessage, images=storedImages)
        yield from self.driveModelLoop(sessionId, activeEvent)

    def driveConfirmation(
        self,
        sessionId: str,
        confirmationId: str,
        approved: bool,
        runEvent: threading.Event | None = None,
    ) -> Iterator:
        # 调用前提：已持有会话锁。
        activeEvent = runEvent if runEvent is not None else threading.Event()
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
        ledger = self.makeToolBatchLedger(pending.toolCalls, pending.currentIndex)
        ledger.assistantPersisted = True
        currentEntry = ledger.entries[0]
        batchError = None
        try:
            if activeEvent.is_set():
                return
            if approved:
                definition = self.toolRegistry.get(currentCall.toolName)
                preview = self.buildToolPreview(definition, currentCall) if definition else str(currentCall.arguments)
                yield toolCallStartEvent(toolCall=currentCall, preview=preview)
                if activeEvent.is_set():
                    return
                try:
                    result = self.executeToolCall(currentCall, sessionId, interruptEvent=activeEvent)
                except modelInterruptedError:
                    return
                currentEntry.result = result
                currentEntry.state = 'completed'
                if activeEvent.is_set():
                    return
                currentConversation.addToolResult(result)
                currentEntry.state = 'persisted'
                yield toolCallEndEvent(toolResult=result)
            else:
                result = self.buildBlockedToolResult(currentCall, pending.reason)
                currentEntry.result = result
                currentConversation.addToolResult(result)
                currentEntry.state = 'persisted'
                yield toolCallEndEvent(toolResult=result)
            if activeEvent.is_set():
                return
            terminated = yield from self.driveToolBatch(
                sessionId,
                pending.toolCalls,
                pending.currentIndex + 1,
                activeEvent,
                ledger,
            )
            if terminated:
                return
            ledger.finished = True
            yield from self.driveModelLoop(sessionId, activeEvent)
        except BaseException as error:
            batchError = error
            raise
        finally:
            self.finalizeToolBatchLedger(currentConversation, ledger, activeEvent, batchError)

    def driveModelLoop(self, sessionId: str, runEvent: threading.Event) -> Iterator:
        # 调用前提：已持有会话锁。承载原 continueModelLoop 逻辑。
        # maxModelSteps 为 None 或 <=0 时不限制步数。
        currentConversation = self.getConversation(sessionId)
        stepIndex = 0
        while True:
            if runEvent.is_set():
                return
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
            usageTotalKeys = ('promptTokens', 'cachedTokens', 'completionTokens')
            stepStart = {
                key: int(currentConversation.usageTotal.get(key, 0) or 0)
                for key in usageTotalKeys
            }
            completion = None
            attemptSnapshot = None
            for attempt in range(MODEL_RETRY_MAX_ATTEMPTS + 1):
                if runEvent.is_set():
                    return
                chunkSeen = False
                attemptSnapshot = {
                    'usageKey': usageLedger.newUsageKey(),
                    'startedAt': usageLedger.utcNowIso(),
                    'providerId': self.currentProviderId(),
                    'modelId': self.currentModelId(),
                    'costFields': usageLedger.snapshotModelCost(self.currentProviderId(), self.currentModelId()),
                }
                try:
                    try:
                        currentConversation.logger.logEvent({
                            'type': 'modelRequestStart',
                            'sessionId': sessionId,
                            'attempt': attempt + 1,
                            'usageKey': attemptSnapshot['usageKey'],
                            'providerId': attemptSnapshot['providerId'],
                            'modelId': attemptSnapshot['modelId'],
                            'messageCount': len(currentConversation.messages),
                            'contextTokens': currentConversation.lastTurnTokens,
                        })
                    except Exception:
                        pass
                    try:
                        hydrateMessageImages(currentConversation.logger.logPath, currentConversation.messages)
                    except imageInputError as error:
                        yield errorEvent(message=str(error), errorType=error.errorType)
                        return
                    for chunk in self.modelAdapter.completeStream(
                        currentConversation.messages,
                        modelTools,
                        stopEvent=runEvent,
                        sessionId=sessionId,
                    ):
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
                        if runEvent.is_set():
                            return
                        raise RuntimeError('模型流式响应缺少最终结果。')
                    break
                except modelInterruptedError:
                    return
                except imageInputError as error:
                    yield errorEvent(message=str(error), errorType=error.errorType)
                    return
                except Exception as error:
                    statusCode = getattr(error, 'statusCode', None)
                    hasStatusAttr = hasattr(error, 'statusCode')
                    retryableOverride = getattr(error, 'retryable', None)
                    isRetryable = (
                        retryableOverride
                        if isinstance(retryableOverride, bool)
                        else hasStatusAttr and (statusCode in MODEL_RETRYABLE_STATUS_CODES or statusCode is None)
                    )
                    willRetry = (not chunkSeen) and isRetryable and attempt < MODEL_RETRY_MAX_ATTEMPTS
                    backoff = 0.0
                    backoffMs = None
                    if willRetry:
                        backoff = min(
                            MODEL_RETRY_BACKOFF_MAX_SECONDS,
                            MODEL_RETRY_BACKOFF_BASE_SECONDS * (2 ** attempt),
                        )
                        retryAfterSeconds = getattr(error, 'retryAfterSeconds', None)
                        if retryAfterSeconds is not None:
                            backoff = max(backoff, float(retryAfterSeconds))
                        backoffMs = int(backoff * 1000)
                    self.logModelError(
                        currentConversation,
                        error,
                        attempt=attempt + 1,
                        willRetry=willRetry,
                        backoffMs=backoffMs,
                    )
                    if not willRetry:
                        yield errorEvent(
                            message=f'模型调用失败（已重试{attempt}次）：{error}',
                            errorType=type(error).__name__,
                        )
                        return
                    yield retryNoticeEvent(
                        message=str(error),
                        attempt=attempt + 1,
                        retryAfterMs=backoffMs,
                        status='waiting',
                    )
                    remaining = backoff
                    while remaining > 0:
                        sliceSeconds = min(0.1, remaining)
                        time.sleep(sliceSeconds)
                        remaining -= sliceSeconds
                        if runEvent.is_set():
                            return
                        yield retryNoticeEvent(
                            message=str(error),
                            attempt=attempt + 1,
                            retryAfterMs=int(remaining * 1000),
                            status='waiting',
                        )

            responsePayload = getattr(completion, 'responsePayload', None)
            assistantMessage = completion.message
            safePayload = responsePayload if isinstance(responsePayload, dict) else {}
            persistedUsageKey = self.persistAttemptUsage(
                currentConversation,
                sessionId,
                safePayload,
                attemptSnapshot,
            )
            hasTerminalUsage = persistedUsageKey is not None
            if assistantMessage.toolCalls:
                idError = self.validateToolCallIds(assistantMessage.toolCalls)
                if idError is not None:
                    yield errorEvent(message=idError, errorType='invalidToolCallIds')
                    return
                ledger = self.makeToolBatchLedger(assistantMessage.toolCalls, 0)
                batchError = None
                try:
                    currentConversation.appendAssistantMessage(assistantMessage, safePayload, usageKey=persistedUsageKey)
                    ledger.assistantPersisted = True
                    if hasTerminalUsage:
                        usageNow = {
                            key: int(currentConversation.usageTotal.get(key, 0) or 0)
                            for key in usageTotalKeys
                        }
                        yield usageUpdateEvent(
                            usage=usageNow,
                            stepUsage={key: max(0, usageNow[key] - stepStart[key]) for key in usageTotalKeys},
                            contextTokens=int(currentConversation.lastTurnTokens or 0),
                        )
                    if runEvent.is_set():
                        return
                    terminated = yield from self.driveToolBatch(
                        sessionId,
                        assistantMessage.toolCalls,
                        0,
                        runEvent,
                        ledger,
                    )
                    if terminated:
                        return
                    ledger.finished = True
                except BaseException as error:
                    batchError = error
                    raise
                finally:
                    self.finalizeToolBatchLedger(currentConversation, ledger, runEvent, batchError)
                if runEvent.is_set():
                    return
                stepIndex += 1
                continue

            currentConversation.appendAssistantMessage(assistantMessage, safePayload, usageKey=persistedUsageKey)
            if hasTerminalUsage:
                usageNow = {
                    key: int(currentConversation.usageTotal.get(key, 0) or 0)
                    for key in usageTotalKeys
                }
                yield usageUpdateEvent(
                    usage=usageNow,
                    stepUsage={key: max(0, usageNow[key] - stepStart[key]) for key in usageTotalKeys},
                    contextTokens=int(currentConversation.lastTurnTokens or 0),
                )
            if self.debugConsole:
                self.debugConsole.debug(f'模型循环完成 sessionId={sessionId} contentChars={len(assistantMessage.content)}')
            yield completedEvent(message=assistantMessage.content)
            return

    def driveToolBatch(
        self,
        sessionId: str,
        toolCalls: list[toolCall],
        startIndex: int,
        runEvent: threading.Event | None = None,
        ledger: toolBatchLedger | None = None,
    ) -> Iterator:
        # 调用前提：已持有会话锁。返回 True 表示已产出终态事件（confirmationRequired）或批次已中断。
        activeEvent = runEvent if runEvent is not None else threading.Event()
        currentConversation = self.getConversation(sessionId)
        ownsLedger = ledger is None
        if ledger is None:
            ledger = self.makeToolBatchLedger(toolCalls, startIndex)
            ledger.assistantPersisted = True
        batchError = None
        try:
            index = startIndex
            while index < len(toolCalls):
                if activeEvent.is_set():
                    return True
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
                for call, definition in prefix:
                    preview = str(call.arguments) if definition is None else self.buildToolPreview(definition, call)
                    yield toolCallStartEvent(toolCall=call, preview=preview)
                cursor = index
                for segment in self.splitExecutableSegments(prefix):
                    if activeEvent.is_set():
                        return True
                    entries = [self.ledgerEntry(ledger, cursor + offset) for offset in range(len(segment))]
                    pooled = self.isPooledCall(segment[0][0], segment[0][1]) if segment else False
                    if pooled and len(segment) >= 2:
                        interrupted = self.executeConcurrentSegment(entries, sessionId, activeEvent)
                        if interrupted or activeEvent.is_set():
                            return True
                        self.persistLedgerEntries(currentConversation, ledger, entries)
                        for entry in entries:
                            yield toolCallEndEvent(toolResult=entry.result)
                    else:
                        for offset, (call, definition) in enumerate(segment):
                            if activeEvent.is_set():
                                return True
                            entry = entries[offset]
                            try:
                                result = (
                                    self.makeUnknownToolResult(call)
                                    if definition is None
                                    else self.executeToolCall(call, sessionId, interruptEvent=activeEvent)
                                )
                            except modelInterruptedError:
                                return True
                            entry.result = result
                            entry.state = 'completed'
                            if activeEvent.is_set():
                                return True
                            currentConversation.addToolResult(result)
                            entry.state = 'persisted'
                            yield toolCallEndEvent(toolResult=result)
                    cursor += len(segment)
                index += len(prefix)
                if activeEvent.is_set():
                    return True
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
                    ledger.suspendedForConfirmation = True
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
        except BaseException as error:
            batchError = error
            raise
        finally:
            if ownsLedger:
                self.finalizeToolBatchLedger(currentConversation, ledger, activeEvent, batchError)

    # ---------- 同步 API（事件流的薄包装，§6.5） ----------

    def runUserMessage(
        self,
        message: str,
        sessionId: str | None = None,
        onDelta: Callable[[str], None] | None = None,
        onReasoning: Callable[[str], None] | None = None,
        *,
        images=None,
    ) -> runResult:
        # sessionId 必须包装层预生成：事件流不带 sessionId，否则耗尽后无从构造 runResult。
        realSessionId = sessionId or self.createSessionId()
        stream = self.runUserMessageStream(message, realSessionId, images=images)
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
            return runResult(
                sessionId=sessionId,
                status='error',
                message=terminal.message,
                errorType=terminal.errorType,
            )
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

    def executeToolCall(
        self,
        call: toolCall,
        sessionId: str | None = None,
        interruptEvent: threading.Event | None = None,
    ) -> toolResult:
        definition = self.toolRegistry.get(call.toolName)
        if definition is None:
            return self.makeUnknownToolResult(call)
        activeEvent = interruptEvent
        if activeEvent is None and sessionId:
            activeEvent = self.getActiveRunEvent(sessionId)
        context = toolContext(
            workDir=self.workDir,
            debugConsole=self.debugConsole,
            interruptEvent=activeEvent,
            sessionId=sessionId,
        )
        return executeCallableToolCall(definition, call, context)

    def currentProviderId(self) -> str:
        config = getattr(self.modelAdapter, 'config', None)
        providerId = getattr(config, 'configProviderId', None) or getattr(config, 'provider', None)
        return str(providerId or 'unknown')

    def currentModelId(self) -> str:
        config = getattr(self.modelAdapter, 'config', None)
        modelId = getattr(config, 'model', None)
        return str(modelId or 'unknown')

    def persistAttemptUsage(
        self,
        currentConversation: conversation,
        sessionId: str,
        payload: dict,
        attemptSnapshot: dict | None,
    ) -> str | None:
        tokens = usageLedger.parseExactUsage(payload.get('usage') if isinstance(payload, dict) else None)
        if not isinstance(attemptSnapshot, dict):
            return None
        usageKey = attemptSnapshot.get('usageKey')
        if tokens is None or not usageKey:
            return None
        providerId = str(attemptSnapshot.get('providerId') or self.currentProviderId())
        modelId = str(attemptSnapshot.get('modelId') or self.currentModelId())
        record = usageLedger.makeExactRecord(
            usageKey=usageKey,
            source=self.usageSource,
            sessionId=sessionId,
            parentSessionId=self.parentSessionId,
            providerId=providerId,
            modelId=modelId,
            requestStartedAt=attemptSnapshot.get('startedAt') or usageLedger.utcNowIso(),
            occurredAt=usageLedger.utcNowIso(),
            tokens=tokens,
            costFields=attemptSnapshot.get('costFields'),
        )
        outboxOk = False
        try:
            currentConversation.appendUsageRecord(record)
            outboxOk = True
        except Exception as error:
            if self.debugConsole:
                self.debugConsole.debug(f'usageRecord outbox 写入失败 usageKey={usageKey} error={error}')
        dbOk = False
        try:
            usageLedger.insertUsageEvent(record)
            dbOk = True
        except Exception as error:
            if self.debugConsole:
                self.debugConsole.debug(f'usageEvents 写入失败 usageKey={usageKey} error={error}')
            if outboxOk:
                usageLedger.scheduleRetry(record)
        if not outboxOk and not dbOk:
            try:
                currentConversation.logger.logEvent({
                    'type': 'usageRecordError',
                    'usageKey': usageKey,
                    'sessionId': sessionId,
                    'message': 'outbox 与账本同时写入失败',
                })
            except Exception:
                pass
            return None
        if not outboxOk and dbOk:
            currentConversation.usageTotal['promptTokens'] += tokens['promptTokens']
            currentConversation.usageTotal['cachedTokens'] += tokens['cachedTokens']
            currentConversation.usageTotal['completionTokens'] += tokens['completionTokens']
            currentConversation.lastTurnTokens = tokens['promptTokens'] + tokens['completionTokens']
        return usageKey

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

    def buildCancelledToolResult(self, call: toolCall, reason: str) -> toolResult:
        return toolResult(
            toolCallId=call.id,
            toolName=call.toolName,
            isError=True,
            content=CANCELLED_TOOL_CONTENTS.get(reason, CANCELLED_TOOL_CONTENTS['streamClosed']),
            details={'cancelled': True, 'reason': reason},
        )

    def logStopRequestedOnce(self, currentConversation: conversation, phase: str, unclosedCallIds: list[str]) -> None:
        if currentConversation._stopRequestedLogged:
            return
        currentConversation._stopRequestedLogged = True
        currentConversation.logger.logEvent({
            'type': 'stopRequested',
            'phase': phase,
            'sessionId': currentConversation.sessionId,
            'unclosedCallIds': unclosedCallIds,
        })

    def makeToolBatchLedger(self, toolCalls: list[toolCall], startIndex: int) -> toolBatchLedger:
        entries = [
            toolBatchEntry(index=startIndex + offset, call=call)
            for offset, call in enumerate(toolCalls[startIndex:])
        ]
        return toolBatchLedger(toolCalls=toolCalls, startIndex=startIndex, entries=entries)

    def ledgerEntry(self, ledger: toolBatchLedger, callIndex: int) -> toolBatchEntry:
        return ledger.entries[callIndex - ledger.startIndex]

    def poolEnabled(self) -> bool:
        return self.maxParallelTools > 1 and bool(self.parallelToolNames)

    def isPooledCall(self, call: toolCall, definition: toolDefinition | None) -> bool:
        return self.poolEnabled() and definition is not None and call.toolName in self.parallelToolNames

    def splitExecutableSegments(
        self,
        prefix: list[tuple[toolCall, toolDefinition | None]],
    ) -> list[list[tuple[toolCall, toolDefinition | None]]]:
        segments: list[list[tuple[toolCall, toolDefinition | None]]] = []
        current: list[tuple[toolCall, toolDefinition | None]] = []
        currentPooled = False
        for item in prefix:
            pooled = self.isPooledCall(item[0], item[1])
            if current and (pooled != currentPooled or not pooled):
                segments.append(current)
                current = []
            current.append(item)
            currentPooled = pooled
        if current:
            segments.append(current)
        return segments

    def validateToolCallIds(self, toolCalls: list[toolCall]) -> str | None:
        seen: set[str] = set()
        for call in toolCalls:
            if not isinstance(call.id, str) or not call.id:
                return '模型返回的 tool call ID 为空。'
            if call.id in seen:
                return f'模型返回重复的 tool call ID：{call.id}'
            seen.add(call.id)
        return None

    def runPooledToolCall(self, call: toolCall, sessionId: str, runEvent: threading.Event) -> toolResult:
        if runEvent.is_set():
            raise modelInterruptedError('用户已停止')
        return self.executeToolCall(call, sessionId, interruptEvent=runEvent)

    def harvestPooledFuture(self, entry: toolBatchEntry, future) -> str | None:
        try:
            result = future.result()
        except modelInterruptedError:
            return 'interrupted'
        except CancelledError:
            return 'cancelled'
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as error:
            result = toolResult(
                toolCallId=entry.call.id,
                toolName=entry.call.toolName,
                isError=True,
                content=f'工具执行异常：{type(error).__name__}: {error}',
                details={'exceptionType': type(error).__name__},
            )
        entry.result = result
        entry.state = 'completed'
        return None

    def executeConcurrentSegment(
        self,
        entries: list[toolBatchEntry],
        sessionId: str,
        runEvent: threading.Event,
    ) -> bool:
        executor = ThreadPoolExecutor(
            max_workers=min(self.maxParallelTools, len(entries)),
            thread_name_prefix='flamingoTool',
        )
        futures = {}
        interrupted = False
        try:
            for entry in entries:
                if runEvent.is_set():
                    interrupted = True
                    break
                futures[executor.submit(self.runPooledToolCall, entry.call, sessionId, runEvent)] = entry
            pending = set(futures)
            while pending:
                done, pending = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
                for future in done:
                    status = self.harvestPooledFuture(futures[future], future)
                    if status == 'interrupted':
                        interrupted = True
                if interrupted or runEvent.is_set():
                    for future in pending:
                        future.cancel()
                    interrupted = True
            return interrupted or runEvent.is_set()
        finally:
            for future in futures:
                future.cancel()
            executor.shutdown(wait=True, cancel_futures=True)
            for future, entry in futures.items():
                if entry.result is None and future.done():
                    try:
                        self.harvestPooledFuture(entry, future)
                    except (KeyboardInterrupt, SystemExit):
                        raise
                    except Exception:
                        pass

    def persistLedgerEntries(
        self,
        currentConversation: conversation,
        ledger: toolBatchLedger,
        entries: list[toolBatchEntry],
    ) -> None:
        self.assertClosedPrefix(currentConversation, ledger.toolCalls)
        for entry in entries:
            if entry.state == 'persisted':
                continue
            if entry.result is None:
                raise RuntimeError(f'工具结果缺失，无法按原序落盘：{entry.call.id}')
            currentConversation.addToolResult(entry.result)
            entry.state = 'persisted'

    def finalizeToolBatchLedger(
        self,
        currentConversation: conversation,
        ledger: toolBatchLedger,
        runEvent: threading.Event | None,
        error: BaseException | None = None,
    ) -> None:
        if not ledger.assistantPersisted or ledger.finished or ledger.suspendedForConfirmation:
            return
        if error is not None and not isinstance(error, (GeneratorExit, modelInterruptedError)):
            reason = 'batchFailed'
        elif runEvent is not None and runEvent.is_set():
            reason = 'userStopped'
        else:
            reason = 'streamClosed'
        self.persistUnclosedResults(currentConversation, ledger, reason)

    def revokeStoppedConfirmationIfNeeded(self, sessionId: str, runEvent: threading.Event, terminal) -> bool:
        if not runEvent.is_set():
            return False
        confirmationId = terminal.confirmationId if isinstance(terminal, confirmationRequiredEvent) else None
        with self.getSessionLock(sessionId):
            pending = self.getConversation(sessionId).pending
            if confirmationId is not None:
                self.revokeStoppedConfirmation(sessionId, confirmationId)
                return True
            if terminal is None and pending is not None:
                self.revokeStoppedConfirmation(sessionId, pending.confirmationId)
                return True
        return False

    def revokeStoppedConfirmation(self, sessionId: str, confirmationId: str) -> None:
        currentConversation = self.getConversation(sessionId)
        pending = currentConversation.pending
        if pending is None or pending.confirmationId != confirmationId:
            return
        currentConversation.takePending()
        ledger = self.makeToolBatchLedger(pending.toolCalls, pending.currentIndex)
        ledger.assistantPersisted = True
        self.persistUnclosedResults(currentConversation, ledger, 'userStopped')

    def persistUnclosedResults(
        self,
        currentConversation: conversation,
        ledger: toolBatchLedger,
        reason: str,
    ) -> list[toolResult]:
        prefixLen = self.assertClosedPrefix(currentConversation, ledger.toolCalls)
        if prefixLen < ledger.startIndex:
            raise conversationIntegrityError(
                f'会话完整性错误：assistant tool batch 在 startIndex={ledger.startIndex} 前存在缺口。'
            )
        missingIds = [call.id for call in ledger.toolCalls[prefixLen:]]
        if reason == 'userStopped' and missingIds:
            self.logStopRequestedOnce(currentConversation, 'toolExecution', missingIds)
        persisted: list[toolResult] = []
        entryByIndex = {entry.index: entry for entry in ledger.entries}
        for callIndex, call in enumerate(ledger.toolCalls):
            if callIndex < prefixLen:
                entry = entryByIndex.get(callIndex)
                if entry is not None:
                    entry.state = 'persisted'
                continue
            entry = entryByIndex.get(callIndex)
            if entry is not None and entry.state == 'persisted' and entry.result is not None:
                continue
            if entry is not None and entry.result is not None:
                result = entry.result
            else:
                result = self.buildCancelledToolResult(call, reason)
            currentConversation.addToolResult(result)
            persisted.append(result)
            if entry is not None:
                entry.result = result
                entry.state = 'persisted'
        return persisted

    def closeUnfinishedToolCalls(
        self,
        currentConversation: conversation,
        toolCalls: list[toolCall],
        startIndex: int,
        reason: str,
    ) -> Iterator:
        prefixLen = self.assertClosedPrefix(currentConversation, toolCalls)
        if prefixLen > startIndex:
            startIndex = prefixLen
        ledger = self.makeToolBatchLedger(toolCalls, startIndex)
        ledger.assistantPersisted = True
        results = self.persistUnclosedResults(currentConversation, ledger, reason)
        for result in results:
            yield toolCallEndEvent(toolResult=result)

    def findUnclosedTailCallIndex(self, currentConversation: conversation) -> tuple[list[toolCall], int] | None:
        messages = currentConversation.messages
        assistantIndex = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == 'assistant' and messages[i].toolCalls:
                assistantIndex = i
                break
            if messages[i].role == 'assistant':
                return None
        if assistantIndex is None:
            return None
        calls = messages[assistantIndex].toolCalls
        prefixLen = self.closedPrefixLength(currentConversation, assistantIndex, calls)
        if prefixLen == len(calls):
            return None
        return (calls, prefixLen)

    def findAssistantIndexForCalls(self, currentConversation: conversation, toolCalls: list[toolCall]) -> int:
        messages = currentConversation.messages
        for i in range(len(messages) - 1, -1, -1):
            message = messages[i]
            if message.role == 'assistant' and message.toolCalls is toolCalls:
                return i
        expected = [call.id for call in toolCalls]
        for i in range(len(messages) - 1, -1, -1):
            message = messages[i]
            if message.role == 'assistant' and [call.id for call in message.toolCalls] == expected:
                return i
        raise conversationIntegrityError('会话完整性错误：找不到对应的 assistant tool batch。')

    def assertClosedPrefix(self, currentConversation: conversation, toolCalls: list[toolCall]) -> int:
        assistantIndex = self.findAssistantIndexForCalls(currentConversation, toolCalls)
        return self.closedPrefixLength(currentConversation, assistantIndex, toolCalls)

    def closedPrefixLength(
        self,
        currentConversation: conversation,
        assistantIndex: int,
        calls: list[toolCall],
    ) -> int:
        closed = currentConversation.consecutiveToolMessagesAfter(assistantIndex)
        seenIds: set[str] = set()
        for position, message in enumerate(closed):
            callId = message.toolCallId
            if not isinstance(callId, str) or not callId:
                raise conversationIntegrityError('会话完整性错误：tool result 缺少 call ID。')
            if callId in seenIds:
                raise conversationIntegrityError(f'会话完整性错误：重复 tool result ID {callId}。')
            seenIds.add(callId)
            if position >= len(calls) or calls[position].id != callId:
                raise conversationIntegrityError('会话完整性错误：tool result 乱序或出现非前缀缺口。')
        prefixLen = len(closed)
        nextIndex = assistantIndex + 1 + prefixLen
        if prefixLen < len(calls) and nextIndex < len(currentConversation.messages):
            raise conversationIntegrityError('会话完整性错误：未闭合 tool call 不是原序后缀缺口。')
        return prefixLen

    def logModelError(
        self,
        currentConversation: conversation,
        error: Exception,
        attempt: int | None = None,
        willRetry: bool | None = None,
        backoffMs: int | None = None,
    ) -> None:
        event: dict[str, Any] = {
            'type': 'modelError',
            'errorType': type(error).__name__,
            'message': str(error),
        }
        requestPayload = getattr(error, 'requestPayload', None)
        if isinstance(requestPayload, dict):
            event['request'] = redactImageData(requestPayload)
        statusCode = getattr(error, 'statusCode', None)
        if isinstance(statusCode, int):
            event['status'] = statusCode
        if attempt is not None:
            event['attempt'] = attempt
        if willRetry is not None:
            event['willRetry'] = willRetry
        if willRetry and backoffMs is not None:
            event['backoffMs'] = backoffMs
        diag = getattr(error, 'diag', None)
        if isinstance(diag, dict):
            for key in (
                'stage', 'durationMs', 'ttfbMs', 'chunks', 'textChars', 'reasoningChars',
                'exceptionName', 'errno', 'requestId', 'responseHeaders', 'requestBytesLen',
                'api', 'baseUrl', 'authRefresh',
            ):
                if key in diag and diag[key] is not None:
                    event[key] = diag[key]
        try:
            currentConversation.logger.logEvent(event)
        except Exception:
            pass

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
        return newSessionId()
