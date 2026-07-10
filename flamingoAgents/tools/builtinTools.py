'''
Author: wilbur
Version: 1.3
Date: 2026-07-09
Description: Provides executable handlers (execute/preview) for built-in tools and a name-keyed registry mapping them to schema-driven tool definitions. Schemas and permissions come from config/tools.yaml.
'''

from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from typing import Any

from flamingoAgents.core.types import toolContext, toolOutput
from flamingoAgents.tools.toolConfig import toolSchemaSpec
from flamingoAgents.tools.toolDefinition import defineTool, toolDefinition, toolExecuteFunction, toolPreviewFunction

maxTimeoutSeconds = 120
defaultTimeoutSeconds = 30


# --- read ---

def previewReadTool(arguments: dict[str, Any]) -> str:
    path = str(arguments.get('path', ''))
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    return f'{path} offset={offset} limit={limit}'


def readTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 2000))
    if context.debugConsole:
        context.debugConsole.debug(f'读取工具开始 path={path} offset={offset} limit={limit}')
    if offset < 1 or limit < 1:
        return toolOutput(content='read.offset 和 read.limit 必须大于 0。', isError=True)
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedText = ''.join(lines[startIndex:startIndex + limit])
    truncated = startIndex + limit < len(lines)
    if context.debugConsole:
        context.debugConsole.debug(
            f'读取工具完成 path={path} totalLines={len(lines)} '
            f'returnedChars={len(selectedText)} truncated={truncated}'
        )
    return toolOutput(
        content=selectedText,
        details={
            'path': str(path),
            'offset': offset,
            'limit': limit,
            'totalLines': len(lines),
            'truncated': truncated,
        },
    )


# --- write ---

def previewWriteTool(arguments: dict[str, Any]) -> str:
    content = str(arguments.get('content', ''))
    return f"{arguments.get('path', '')} bytes={len(content.encode('utf-8'))}"


def writeTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    content = str(arguments['content'])
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具开始 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    if context.debugConsole:
        context.debugConsole.debug(f'写入工具完成 path={path}')
    return toolOutput(
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
        },
    )


# --- edit ---

def previewEditTool(arguments: dict[str, Any]) -> str:
    edits = arguments.get('edits', [])
    editCount = len(edits) if isinstance(edits, list) else 0
    return f"{arguments.get('path', '')} edits={editCount}"


def editTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    rawPath = Path(arguments['path']).expanduser()
    path = rawPath if rawPath.is_absolute() else (context.workDir / rawPath)
    edits = arguments['edits']
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具开始 path={path} editCount={len(edits)}')
    if not path.exists() or not path.is_file():
        return toolOutput(content=f'文件不存在或不是普通文件：{path}', isError=True, details={'path': str(path)})

    originalContent = path.read_text(encoding='utf-8')
    replacements: list[tuple[int, int, str]] = []
    for index, editItem in enumerate(edits):
        oldText = editItem['oldText']
        newText = editItem['newText']
        matchCount = originalContent.count(oldText)
        if matchCount != 1:
            return toolOutput(content=f'第 {index + 1} 个 oldText 必须精确且唯一匹配，当前匹配数：{matchCount}。', isError=True)
        startIndex = originalContent.index(oldText)
        endIndex = startIndex + len(oldText)
        replacements.append((startIndex, endIndex, newText))

    replacements.sort(key=lambda item: item[0])
    previousEnd = -1
    for startIndex, endIndex, newText in replacements:
        if startIndex < previousEnd:
            return toolOutput(content='多个 edits 不能重叠。', isError=True)
        previousEnd = endIndex

    updatedContent = originalContent
    for startIndex, endIndex, newText in sorted(replacements, key=lambda item: item[0], reverse=True):
        updatedContent = updatedContent[:startIndex] + newText + updatedContent[endIndex:]

    diffText = ''.join(difflib.unified_diff(
        originalContent.splitlines(keepends=True),
        updatedContent.splitlines(keepends=True),
        fromfile=str(path) + ':before',
        tofile=str(path) + ':after',
        n=3,
    ))
    path.write_text(updatedContent, encoding='utf-8')
    if context.debugConsole:
        context.debugConsole.debug(f'编辑工具完成 path={path} diffChars={len(diffText)}')
    return toolOutput(
        content=diffText or '文件内容未发生变化。',
        details={'path': str(path), 'editCount': len(edits)},
    )


# --- bash ---

def previewBashTool(arguments: dict[str, Any]) -> str:
    return str(arguments.get('command', ''))


def bashTool(arguments: dict[str, Any], context: toolContext) -> toolOutput:
    command = arguments.get('command')
    if not isinstance(command, str) or not command.strip():
        return toolOutput(content='bash.command 必须是非空字符串。', isError=True)

    timeout = int(arguments.get('timeout', defaultTimeoutSeconds))
    if timeout < 1:
        timeout = defaultTimeoutSeconds
    if timeout > maxTimeoutSeconds:
        timeout = maxTimeoutSeconds
    maxOutput = int(arguments.get('maxOutput', 2000))
    if context.debugConsole:
        context.debugConsole.debug(f'bash 工具开始 command={command} timeout={timeout} maxOutput={maxOutput} cwd={context.workDir}')

    def clip(text: str) -> tuple[str, bool]:
        if maxOutput < 0 or len(text) <= maxOutput:
            return text, False
        return text[:maxOutput] + '\n<truncated>', True

    try:
        completedProcess = subprocess.run(
            ['bash', '-lc', command],
            cwd=str(context.workDir),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdoutText, stdoutTruncated = clip(completedProcess.stdout)
        stderrText, stderrTruncated = clip(completedProcess.stderr)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具完成 exitCode={completedProcess.returncode}')
        return toolOutput(
            content=(
                f'exitCode: {completedProcess.returncode}\n'
                f'stdout:\n{stdoutText}\n'
                f'stderr:\n{stderrText}'
            ),
            isError=completedProcess.returncode != 0,
            details={
                'command': command,
                'timeout': timeout,
                'maxOutput': maxOutput,
                'exitCode': completedProcess.returncode,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )
    except subprocess.TimeoutExpired as error:
        stdoutText = decodeProcessText(error.stdout)
        stderrText = decodeProcessText(error.stderr)
        stdoutText, stdoutTruncated = clip(stdoutText)
        stderrText, stderrTruncated = clip(stderrText)
        if context.debugConsole:
            context.debugConsole.debug(f'bash 工具超时 command={command} timeout={timeout}')
        return toolOutput(
            content=(
                f'命令超时，已终止。timeout: {timeout}\n'
                f'stdout:\n{stdoutText}\n'
                f'stderr:\n{stderrText}'
            ),
            isError=True,
            details={
                'command': command,
                'timeout': timeout,
                'maxOutput': maxOutput,
                'timeoutExpired': True,
                'stdoutTruncated': stdoutTruncated,
                'stderrTruncated': stderrTruncated,
            },
        )


def decodeProcessText(value: str | bytes | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode('utf-8', errors='replace')
    return ''


# --- schema-driven assembly ---

executableMap: dict[str, tuple[toolExecuteFunction, toolPreviewFunction]] = {
    'read': (readTool, previewReadTool),
    'write': (writeTool, previewWriteTool),
    'edit': (editTool, previewEditTool),
    'bash': (bashTool, previewBashTool),
}


def createBuiltinTools(toolSchemas: list[toolSchemaSpec], debugConsole=None) -> list[toolDefinition]:
    definitions: list[toolDefinition] = []
    for schema in toolSchemas:
        handlers = executableMap.get(schema.name)
        if handlers is None:
            raise RuntimeError(f'未知工具实现：{schema.name}')
        execute, preview = handlers
        definition = defineTool(
            name=schema.name,
            description=schema.description,
            parameters=schema.parameters,
            execute=execute,
            permissions=schema.permissions,
            preview=preview,
        )
        definitions.append(definition)
        if debugConsole:
            debugConsole.debug(
                f'绑定工具实现 tool={schema.name} '
                f'permissions={len(schema.permissions)}'
            )
    if debugConsole:
        debugConsole.debug(f'工具定义装配完成 count={len(definitions)}')
    return definitions
