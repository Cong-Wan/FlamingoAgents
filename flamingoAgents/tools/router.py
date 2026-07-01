'''
Author: wilbur
Version: 1.1
Date: 2026-07-01
Description: Routes validated tool calls through guard checks and concrete tool implementations.
'''

from __future__ import annotations

from flamingoAgents.core.types import toolCall, toolContext, toolResult
from flamingoAgents.tools.guard import checkToolCall
from flamingoAgents.tools.registry import registry


class confirmationNeeded(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class router:
    def __init__(self, registry: registry, context: toolContext):
        self.registry = registry
        self.context = context

    def executeTool(self, call: toolCall, approvedDeletion: bool = False) -> toolResult:
        if self.context.debugConsole:
            self.context.debugConsole.debug(f'路由工具调用 tool={call.toolName} callId={call.id}')
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
            raise confirmationNeeded(guard.reason)

        try:
            result = definition.execute(call.arguments, self.context)
            result.toolCallId = call.id
            result.toolName = call.toolName
            if self.context.debugConsole:
                self.context.debugConsole.debug(f'工具返回 tool={call.toolName} callId={call.id} isError={result.isError}')
            return result
        except Exception as error:
            return toolResult(
                toolCallId=call.id,
                toolName=call.toolName,
                isError=True,
                content=f'工具执行异常：{type(error).__name__}: {error}',
                details={'exceptionType': type(error).__name__},
            )
