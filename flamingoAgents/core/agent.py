'''
Author: wilbur
Version: 1.1
Date: 2026-07-01
Description: Coordinates model calls, tool execution, confirmation handling, sessions, and JSONL-backed conversations.
'''

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from flamingoAgents.core.types import (
    runResult,
    chatMessage,
    pendingConfirm,
    toolCall,
    toolContext,
)
from flamingoAgents.core.conversation import conversation
from flamingoAgents.tools.guard import checkToolCall, makeBlockedToolResult
from flamingoAgents.tools.registry import registry
from flamingoAgents.tools.router import router

confirmationHandler = Callable[[toolCall, str], bool]

systemPrompt = '''你是 Flamingo Agents。你可以正常聊天，也可以调用 read、write、edit、bash 工具。联网查询只能通过 bash 中的 curl 等简单 shell 命令完成。如果 curl 因反爬、登录墙、验证码、403 或空结果失败，你必须诚实说明失败，不尝试绕过。删除相关 bash 命令必须先得到用户确认。'''


class agent:
    def __init__(
        self,
        modelAdapter: Any,
        registry: registry,
        workDir: Path,
        logDir: Path,
        debugConsole=None,
        confirmDeletion: confirmationHandler | None = None,
        maxModelSteps: int = 8,
    ):
        self.modelAdapter = modelAdapter
        self.registry = registry
        self.workDir = workDir
        self.logDir = logDir
        self.debugConsole = debugConsole
        self.confirmDeletion = confirmDeletion
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversation] = {}
        self.pendingConfirms: dict[str, pendingConfirm] = {}

    def runUserMessage(self, message: str, sessionId: str | None = None) -> runResult:
        cleanMessage = message.strip()
        if not cleanMessage:
            return runResult(sessionId=sessionId or self.createSessionId(), status='error', message='消息不能为空。')
        realSessionId = sessionId or self.createSessionId()
        if self.debugConsole:
            self.debugConsole.debug(f'收到用户消息 sessionId={realSessionId} chars={len(cleanMessage)}')
        conversation = self.getConversation(realSessionId)
        conversation.addMessage(chatMessage(role='user', content=cleanMessage))
        return self.continueModelLoop(realSessionId)

    def continueConfirmation(self, sessionId: str, confirmationId: str, approved: bool) -> runResult:
        pending = self.pendingConfirms.pop(confirmationId, None)
        if pending is None or pending.sessionId != sessionId:
            return runResult(sessionId=sessionId, status='error', message='确认请求不存在或 sessionId 不匹配。')

        conversation = self.getConversation(sessionId)
        router = self.createRouter()
        if approved:
            result = router.executeTool(pending.toolCall, approvedDeletion=True)
        else:
            result = makeBlockedToolResult(pending.toolCall, pending.reason)
        if self.debugConsole:
            self.debugConsole.debug(f'工具执行完成 tool={pending.toolCall.toolName} callId={pending.toolCall.id} isError={result.isError}')
        conversation.addToolResult(result)
        return self.continueModelLoop(sessionId)

    def continueModelLoop(self, sessionId: str) -> runResult:
        conversation = self.getConversation(sessionId)
        router = self.createRouter()
        for stepIndex in range(self.maxModelSteps):
            if self.debugConsole:
                self.debugConsole.debug(
                    f'agent 模型循环 step={stepIndex + 1} sessionId={sessionId} '
                    f'messages={len(conversation.messages)} tools={len(self.registry.listDefinitions())}'
                )
            try:
                assistantMessage = self.modelAdapter.complete(conversation.messages, self.registry.listModelTools())
            except Exception as error:
                conversation.logger.logEvent({
                    'type': 'modelError',
                    'errorType': type(error).__name__,
                    'message': str(error),
                })
                return runResult(sessionId=sessionId, status='error', message=f'模型调用失败：{error}')

            conversation.addMessage(assistantMessage)
            if not assistantMessage.toolCalls:
                return runResult(sessionId=sessionId, status='completed', message=assistantMessage.content)

            for call in assistantMessage.toolCalls:
                if self.debugConsole:
                    self.debugConsole.debug(f'准备执行工具 tool={call.toolName} callId={call.id}')
                guard = checkToolCall(call)
                if guard.requiresConfirmation:
                    if self.confirmDeletion is None:
                        confirmationId = 'confirm_' + uuid4().hex[:12]
                        self.pendingConfirms[confirmationId] = pendingConfirm(
                            sessionId=sessionId,
                            confirmationId=confirmationId,
                            reason=guard.reason,
                            toolCall=call,
                        )
                        return runResult(
                            sessionId=sessionId,
                            status='confirmationRequired',
                            confirmationId=confirmationId,
                            reason=guard.reason,
                            commandPreview=str(call.arguments.get('command', '')),
                            toolCall=call,
                        )
                    approved = self.confirmDeletion(call, guard.reason)
                    if not approved:
                        result = makeBlockedToolResult(call, guard.reason)
                        if self.debugConsole:
                            self.debugConsole.debug(f'工具执行完成 tool={call.toolName} callId={call.id} isError={result.isError}')
                        conversation.addToolResult(result)
                        continue
                    result = router.executeTool(call, approvedDeletion=True)
                else:
                    result = router.executeTool(call)
                if self.debugConsole:
                    self.debugConsole.debug(f'工具执行完成 tool={call.toolName} callId={call.id} isError={result.isError}')
                conversation.addToolResult(result)

        return runResult(
            sessionId=sessionId,
            status='error',
            message=f'模型循环超过最大步数：{self.maxModelSteps}',
        )

    def getConversation(self, sessionId: str) -> conversation:
        existing = self.conversations.get(sessionId)
        if existing is not None:
            return existing
        dateText = datetime.now().strftime('%Y%m%d')
        logPath = self.logDir / f'{dateText}_{sessionId}.jsonl'
        newConversation = conversation(sessionId=sessionId, logPath=logPath, systemPrompt=systemPrompt)
        self.conversations[sessionId] = newConversation
        return newConversation

    def createRouter(self) -> router:
        context = toolContext(workDir=self.workDir, debugConsole=self.debugConsole)
        return router(self.registry, context)

    def createSessionId(self) -> str:
        return 'session_' + uuid4().hex[:12]
