'''
Author: wilbur
Version: 1.2
Date: 2026-08-14
Description: Executes callable tool definitions through shared argument validation and toolResult wrapping.
             v1.2（stopResponsivenessPlan L3.5）：modelInterruptedError 直通，不包装成 toolResult 错误。
'''

from __future__ import annotations

from typing import Any

from flamingoAgents.core.types import modelInterruptedError, toolCall, toolContext, toolResult
from flamingoAgents.tools.toolDefinition import toolDefinition


def executeToolCall(definition: toolDefinition, call: toolCall, context: toolContext) -> toolResult:
    if context.debugConsole:
        context.debugConsole.debug(f'执行工具调用开始 tool={definition.name} callId={call.id}')
    arguments = call.arguments
    if not isinstance(arguments, dict):
        return toolResult(call.id, definition.name, True, 'toolCall.arguments 必须是对象。', {'invalidArguments': True})

    try:
        if definition.prepareArguments is not None:
            if context.debugConsole:
                context.debugConsole.debug(f'预处理工具参数 tool={definition.name} callId={call.id}')
            arguments = definition.prepareArguments(arguments)
            if not isinstance(arguments, dict):
                return toolResult(call.id, definition.name, True, '工具参数预处理结果必须是对象。', {'invalidPreparedArguments': True})
    except Exception as error:
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content=f'工具参数预处理异常：{type(error).__name__}: {error}',
            details={'exceptionType': type(error).__name__},
        )

    schemaError = validateArguments(definition.parameters, arguments)
    if schemaError:
        return toolResult(call.id, definition.name, True, f'工具参数不符合 schema：{schemaError}', {'schemaError': schemaError})

    try:
        if context.debugConsole:
            context.debugConsole.debug(f'调用工具函数 tool={definition.name} callId={call.id}')
        output = definition.execute(arguments, context)
        result = toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=output.isError,
            content=output.content,
            details=output.details,
        )
        if context.debugConsole:
            context.debugConsole.debug(f'执行工具调用完成 tool={definition.name} callId={call.id} isError={result.isError}')
        return result
    except modelInterruptedError:
        raise
    except Exception as error:
        return toolResult(
            toolCallId=call.id,
            toolName=definition.name,
            isError=True,
            content=f'工具执行异常：{type(error).__name__}: {error}',
            details={'exceptionType': type(error).__name__},
        )


def validateArguments(parameters: dict[str, Any], arguments: dict[str, Any]) -> str:
    return validateObject(parameters, arguments, 'arguments')


def validateObject(schema: dict[str, Any], value: Any, path: str) -> str:
    if schema.get('type') != 'object':
        return f'{path} schema.type 必须是 object'
    if not isinstance(value, dict):
        return f'{path} 必须是对象'

    properties = schema.get('properties') or {}
    if not isinstance(properties, dict):
        return f'{path}.properties 必须是对象'

    required = schema.get('required') or []
    if not isinstance(required, list):
        return f'{path}.required 必须是数组'
    for key in required:
        if key not in value:
            return f'{path}.{key} 是必填字段'

    if schema.get('additionalProperties') is False:
        allowedKeys = set(properties.keys())
        for key in value.keys():
            if key not in allowedKeys:
                return f'{path}.{key} 不允许出现'

    for key, itemValue in value.items():
        itemSchema = properties.get(key)
        if isinstance(itemSchema, dict):
            itemError = validateValue(itemSchema, itemValue, f'{path}.{key}')
            if itemError:
                return itemError
    return ''


def validateValue(schema: dict[str, Any], value: Any, path: str) -> str:
    expectedType = schema.get('type')
    if expectedType == 'string':
        if not isinstance(value, str):
            return f'{path} 必须是字符串'
        return ''
    if expectedType == 'integer':
        if not isinstance(value, int) or isinstance(value, bool):
            return f'{path} 必须是整数'
        minimum = schema.get('minimum')
        maximum = schema.get('maximum')
        if isinstance(minimum, int) and value < minimum:
            return f'{path} 必须大于等于 {minimum}'
        if isinstance(maximum, int) and value > maximum:
            return f'{path} 必须小于等于 {maximum}'
        return ''
    if expectedType == 'array':
        if not isinstance(value, list):
            return f'{path} 必须是数组'
        minItems = schema.get('minItems')
        if isinstance(minItems, int) and len(value) < minItems:
            return f'{path} 至少需要 {minItems} 项'
        itemSchema = schema.get('items')
        if isinstance(itemSchema, dict):
            for index, itemValue in enumerate(value):
                itemError = validateValue(itemSchema, itemValue, f'{path}[{index}]')
                if itemError:
                    return itemError
        return ''
    if expectedType == 'object':
        return validateObject(schema, value, path)
    return ''
