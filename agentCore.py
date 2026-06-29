'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Coordinates model calls, tool execution, confirmation handling, sessions, and JSONL-backed conversations.
'''

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from agentTypes import (
    agentRunResult,
    chatMessage,
    pendingConfirmation,
    toolCall,
    toolExecutionContext,
)
from conversationManager import conversationManager
from toolGuard import checkToolCall, makeBlockedToolResult
from toolRegistry import toolRegistry
from toolRouter import toolRouter

confirmationHandler = Callable[[toolCall, str], bool]

systemPrompt = '''你是本地系统工具对话 Agent。你可以正常聊天，也可以调用 read、write、edit、bash 工具。联网查询只能通过 bash 中的 curl 等简单 shell 命令完成。如果 curl 因反爬、登录墙、验证码、403 或空结果失败，你必须诚实说明失败，不尝试绕过。删除相关 bash 命令必须先得到用户确认。'''


class agentCore:
    def __init__(
        self,
        modelAdapter: Any,
        registry: toolRegistry,
        workDir: Path,
        logDir: Path,
        debugPrinter=None,
        confirmDeletion: confirmationHandler | None = None,
        maxModelSteps: int = 8,
    ):
        self.modelAdapter = modelAdapter
        self.registry = registry
        self.workDir = workDir
        self.logDir = logDir
        self.debugPrinter = debugPrinter
        self.confirmDeletion = confirmDeletion
        self.maxModelSteps = maxModelSteps
        self.conversations: dict[str, conversationManager] = {}
        self.pendingConfirmations: dict[str, pendingConfirmation] = {}

    def runUserMessage(self, message: str, sessionId: str | None = None) -> agentRunResult:
        cleanMessage = message.strip()
        if not cleanMessage:
            return agentRunResult(sessionId=sessionId or self.createSessionId(), status='error', message='消息不能为空。')
        realSessionId = sessionId or self.createSessionId()
        conversation = self.getConversation(realSessionId)
        conversation.addMessage(chatMessage(role='user', content=cleanMessage))
        return self.continueModelLoop(realSessionId)

    def continueConfirmation(self, sessionId: str, confirmationId: str, approved: bool) -> agentRunResult:
        pending = self.pendingConfirmations.pop(confirmationId, None)
        if pending is None or pending.sessionId != sessionId:
            return agentRunResult(sessionId=sessionId, status='error', message='确认请求不存在或 sessionId 不匹配。')

        conversation = self.getConversation(sessionId)
        router = self.createRouter()
        if approved:
            result = router.executeTool(pending.toolCall, approvedDeletion=True)
        else:
            result = makeBlockedToolResult(pending.toolCall, pending.reason)
        conversation.addToolResult(result)
        return self.continueModelLoop(sessionId)

    def continueModelLoop(self, sessionId: str) -> agentRunResult:
        conversation = self.getConversation(sessionId)
        router = self.createRouter()
        for stepIndex in range(self.maxModelSteps):
            if self.debugPrinter:
                self.debugPrinter.debug(f'agentCore 模型循环 step={stepIndex + 1} sessionId={sessionId}')
            try:
                assistantMessage = self.modelAdapter.complete(conversation.messages, self.registry.listModelTools())
            except Exception as error:
                conversation.logger.logEvent({
                    'type': 'modelError',
                    'errorType': type(error).__name__,
                    'message': str(error),
                })
                return agentRunResult(sessionId=sessionId, status='error', message=f'模型调用失败：{error}')

            conversation.addMessage(assistantMessage)
            if not assistantMessage.toolCalls:
                return agentRunResult(sessionId=sessionId, status='completed', message=assistantMessage.content)

            for call in assistantMessage.toolCalls:
                guard = checkToolCall(call)
                if guard.requiresConfirmation:
                    if self.confirmDeletion is None:
                        confirmationId = 'confirm_' + uuid4().hex[:12]
                        self.pendingConfirmations[confirmationId] = pendingConfirmation(
                            sessionId=sessionId,
                            confirmationId=confirmationId,
                            reason=guard.reason,
                            toolCall=call,
                        )
                        return agentRunResult(
                            sessionId=sessionId,
                            status='confirmationRequired',
                            confirmationId=confirmationId,
                            reason=guard.reason,
                            commandPreview=str(call.arguments.get('command', '')),
                            toolCall=call,
                        )
                    approved = self.confirmDeletion(call, guard.reason)
                    if not approved:
                        conversation.addToolResult(makeBlockedToolResult(call, guard.reason))
                        continue
                    result = router.executeTool(call, approvedDeletion=True)
                else:
                    result = router.executeTool(call)
                conversation.addToolResult(result)

        return agentRunResult(
            sessionId=sessionId,
            status='error',
            message=f'模型循环超过最大步数：{self.maxModelSteps}',
        )

    def getConversation(self, sessionId: str) -> conversationManager:
        existing = self.conversations.get(sessionId)
        if existing is not None:
            return existing
        dateText = datetime.now().strftime('%Y%m%d')
        logPath = self.logDir / f'{dateText}_{sessionId}.jsonl'
        conversation = conversationManager(sessionId=sessionId, logPath=logPath, systemPrompt=systemPrompt)
        self.conversations[sessionId] = conversation
        return conversation

    def createRouter(self) -> toolRouter:
        context = toolExecutionContext(workDir=self.workDir, debugPrinter=self.debugPrinter)
        return toolRouter(self.registry, context)

    def createSessionId(self) -> str:
        return 'session_' + uuid4().hex[:12]
