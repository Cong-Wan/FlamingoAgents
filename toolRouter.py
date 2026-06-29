'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Routes validated tool calls through guard checks and concrete tool implementations.
'''

from __future__ import annotations

from agentTypes import toolCall, toolExecutionContext, toolResult
from toolGuard import checkToolCall
from toolRegistry import toolRegistry


class deletionConfirmationNeeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class toolRouter:
    def __init__(self, registry: toolRegistry, context: toolExecutionContext):
        self.registry = registry
        self.context = context

    def executeTool(self, call: toolCall, approvedDeletion: bool = False) -> toolResult:
        definition = self.registry.get(call.toolName)
        if definition is None:
            return toolResult(
                toolCallId=call.id,
                toolName=call.toolName,
                isError=True,
                content=f'未知工具：{call.toolName}',
                details={'unknownTool': True},
            )
        if not isinstance(call.arguments, dict):
            return toolResult(
                toolCallId=call.id,
                toolName=call.toolName,
                isError=True,
                content='toolCall.arguments 必须是对象。',
                details={'invalidArguments': True},
            )

        guard = checkToolCall(call)
        if guard.requiresConfirmation and not approvedDeletion:
            raise deletionConfirmationNeeded(guard.reason)

        try:
            result = definition.execute(call.arguments, self.context)
            result.toolCallId = call.id
            result.toolName = call.toolName
            return result
        except Exception as error:
            return toolResult(
                toolCallId=call.id,
                toolName=call.toolName,
                isError=True,
                content=f'工具执行异常：{type(error).__name__}: {error}',
                details={'exceptionType': type(error).__name__},
            )
