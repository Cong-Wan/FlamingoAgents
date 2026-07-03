'''
Author: wilbur
Version: 1.0
Date: 2026-07-02
Description: Executes config-driven file and shell tool runtimes with schema validation and workDir sandboxing.
'''

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

from flamingoAgents.core.types import toolContext, toolResult
from flamingoAgents.tools.toolConfig import toolDefinition
from flamingoAgents.utils.preview import makePreview

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30


def executeTool(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str = '') -> toolResult:
    if context.debugConsole:
        context.debugConsole.debug(f'执行工具 runtime tool={definition.name} runtimeType={definition.runtime.get("type")} callId={toolCallId}')
    if not isinstance(arguments, dict):
        return toolResult(toolCallId, definition.name, True, 'toolCall.arguments 必须是对象。', {'invalidArguments': True})

    schemaError = validateArguments(definition.parameters, arguments)
    if schemaError:
        return toolResult(toolCallId, definition.name, True, f'工具参数不符合 schema：{schemaError}', {'schemaError': schemaError})

    runtimeType = definition.runtime.get('type')
    try:
        if runtimeType == 'file':
            return executeFileRuntime(definition, arguments, context, toolCallId)
        if runtimeType == 'shell':
            return executeShellRuntime(definition, arguments, context, toolCallId)
        return toolResult(toolCallId, definition.name, True, f'未知 runtime.type：{runtimeType}', {'unknownRuntime': runtimeType})
    except Exception as error:
        return toolResult(
            toolCallId=toolCallId,
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
        if not isinstance(value, int):
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


def executeFileRuntime(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    operation = definition.runtime.get('operation')
    if operation == 'read':
        return executeFileRead(definition, arguments, context, toolCallId)
    if operation == 'write':
        return executeFileWrite(definition, arguments, context, toolCallId)
    if operation == 'edit':
        return executeFileEdit(definition, arguments, context, toolCallId)
    return toolResult(toolCallId, definition.name, True, f'未知 file operation：{operation}', {'unknownFileOperation': operation})


def resolveSafePath(pathValue: str, workDir: Path) -> Path:
    if pathValue.strip().startswith('~'):
        raise ValueError(f'路径不能使用 ~：{pathValue}')
    rawPath = Path(pathValue)
    if rawPath.is_absolute():
        raise ValueError(f'路径必须是工作目录内的相对路径：{pathValue}')
    root = workDir.resolve()
    resolved = (root / rawPath).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError(f'路径超出工作目录：{pathValue}') from error
    return resolved


def executeFileRead(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    pathField = str(definition.runtime.get('pathField', 'path'))
    offsetField = str(definition.runtime.get('offsetField', 'offset'))
    limitField = str(definition.runtime.get('limitField', 'limit'))
    path = resolveSafePath(str(arguments[pathField]), context.workDir)
    offset = int(arguments.get(offsetField, 1))
    limit = int(arguments.get(limitField, 200))
    if offset < 1 or limit < 1:
        return toolResult(toolCallId, definition.name, True, 'read.offset 和 read.limit 必须大于 0。')
    if context.debugConsole:
        context.debugConsole.debug(f'读取文件 path={path} offset={offset} limit={limit}')
    if not path.exists() or not path.is_file():
        return toolResult(toolCallId, definition.name, True, f'文件不存在或不是普通文件：{path}', {'path': str(path)})
    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedText = ''.join(lines[startIndex:startIndex + limit])
    truncated = startIndex + limit < len(lines)
    previewText, previewTruncated = makePreview(selectedText)
    return toolResult(
        toolCallId=toolCallId,
        toolName=definition.name,
        isError=False,
        content=previewText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': truncated or previewTruncated,
        },
    )


def executeFileWrite(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    pathField = str(definition.runtime.get('pathField', 'path'))
    contentField = str(definition.runtime.get('contentField', 'content'))
    path = resolveSafePath(str(arguments[pathField]), context.workDir)
    content = arguments[contentField]
    if context.debugConsole:
        context.debugConsole.debug(f'写入文件 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    previewText, truncated = makePreview(content)
    return toolResult(
        toolCallId=toolCallId,
        toolName=definition.name,
        isError=False,
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
            'contentPreview': previewText,
            'truncated': truncated,
        },
    )


def executeFileEdit(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    pathField = str(definition.runtime.get('pathField', 'path'))
    editsField = str(definition.runtime.get('editsField', 'edits'))
    path = resolveSafePath(str(arguments[pathField]), context.workDir)
    edits = arguments[editsField]
    if context.debugConsole:
        context.debugConsole.debug(f'编辑文件 path={path} editCount={len(edits)}')
    if not path.exists() or not path.is_file():
        return toolResult(toolCallId, definition.name, True, f'文件不存在或不是普通文件：{path}', {'path': str(path)})

    originalContent = path.read_text(encoding='utf-8')
    replacements: list[tuple[int, int, str, str]] = []
    for index, editItem in enumerate(edits):
        oldText = editItem['oldText']
        newText = editItem['newText']
        matchCount = originalContent.count(oldText)
        if matchCount != 1:
            return toolResult(toolCallId, definition.name, True, f'第 {index + 1} 个 oldText 必须精确且唯一匹配，当前匹配数：{matchCount}。')
        startIndex = originalContent.index(oldText)
        endIndex = startIndex + len(oldText)
        replacements.append((startIndex, endIndex, oldText, newText))

    replacements.sort(key=lambda item: item[0])
    previousEnd = -1
    for startIndex, endIndex, oldText, newText in replacements:
        if startIndex < previousEnd:
            return toolResult(toolCallId, definition.name, True, '多个 edits 不能重叠。')
        previousEnd = endIndex

    updatedContent = originalContent
    for startIndex, endIndex, oldText, newText in sorted(replacements, key=lambda item: item[0], reverse=True):
        updatedContent = updatedContent[:startIndex] + newText + updatedContent[endIndex:]

    diffText = ''.join(difflib.unified_diff(
        originalContent.splitlines(keepends=True),
        updatedContent.splitlines(keepends=True),
        fromfile=str(path) + ':before',
        tofile=str(path) + ':after',
        n=3,
    ))
    path.write_text(updatedContent, encoding='utf-8')
    previewText, truncated = makePreview(diffText)
    return toolResult(
        toolCallId=toolCallId,
        toolName=definition.name,
        isError=False,
        content=previewText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits), 'diffTruncated': truncated},
    )


def executeShellRuntime(definition: toolDefinition, arguments: dict[str, Any], context: toolContext, toolCallId: str) -> toolResult:
    commandField = str(definition.runtime.get('commandField', 'command'))
    timeoutField = str(definition.runtime.get('timeoutField', 'timeout'))
    command = arguments.get(commandField)
    if not isinstance(command, str) or not command.strip():
        return toolResult(toolCallId, definition.name, True, 'bash.command 必须是非空字符串。')
    timeout = int(arguments.get(timeoutField, defaultTimeoutSeconds))
    if timeout < 1:
        timeout = defaultTimeoutSeconds
    if timeout > maxTimeoutSeconds:
        timeout = maxTimeoutSeconds
    if context.debugConsole:
        context.debugConsole.debug(f'执行 shell command={command} timeout={timeout} cwd={context.workDir}')
    try:
        completedProcess = subprocess.run(
            ['bash', '-lc', command],
            cwd=str(context.workDir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdoutPreview, stdoutTruncated = makePreview(completedProcess.stdout)
        stderrPreview, stderrTruncated = makePreview(completedProcess.stderr)
        isError = completedProcess.returncode != 0
        return toolResult(
            toolCallId=toolCallId,
            toolName=definition.name,
            isError=isError,
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
            details={
                'command': command,
                'timeout': timeout,
                'exitCode': completedProcess.returncode,
                'stdoutPreview': stdoutPreview,
                'stderrPreview': stderrPreview,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
    except subprocess.TimeoutExpired as error:
        stdoutText = error.stdout if isinstance(error.stdout, str) else (error.stdout or b'').decode('utf-8', errors='replace')
        stderrText = error.stderr if isinstance(error.stderr, str) else (error.stderr or b'').decode('utf-8', errors='replace')
        stdoutPreview, stdoutTruncated = makePreview(stdoutText)
        stderrPreview, stderrTruncated = makePreview(stderrText)
        return toolResult(
            toolCallId=toolCallId,
            toolName=definition.name,
            isError=True,
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutPreview}\n'
                f'stderr:\n{stderrPreview}'
            ),
            details={
                'command': command,
                'timeout': timeout,
                'timeoutExpired': True,
                'stdoutPreview': stdoutPreview,
                'stderrPreview': stderrPreview,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
