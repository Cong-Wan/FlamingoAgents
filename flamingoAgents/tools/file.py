'''
Author: wilbur
Version: 1.1
Date: 2026-07-01
Description: Implements read, write, and edit tools for local text files with debug logs.
'''

from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from flamingoAgents.core.types import toolContext, toolResult
from flamingoAgents.utils.jsonl import makePreview


def normalizePath(pathValue: str, workDir: Path) -> Path:
    path = Path(pathValue).expanduser()
    if not path.is_absolute():
        path = workDir / path
    return path


def executeRead(arguments: dict[str, Any], context: toolContext) -> toolResult:
    pathValue = arguments.get('path')
    if not isinstance(pathValue, str) or not pathValue.strip():
        return toolResult('', 'read', True, 'read.path 必须是非空字符串。')

    offset = int(arguments.get('offset', 1))
    limit = int(arguments.get('limit', 200))
    if offset < 1 or limit < 1:
        return toolResult('', 'read', True, 'read.offset 和 read.limit 必须大于 0。')

    path = normalizePath(pathValue, context.workDir)
    if context.debugConsole:
        context.debugConsole.debug(f'读取文件 path={path} offset={offset} limit={limit}')
    if not path.exists() or not path.is_file():
        return toolResult('', 'read', True, f'文件不存在或不是普通文件：{path}')

    content = path.read_text(encoding='utf-8')
    lines = content.splitlines(keepends=True)
    startIndex = offset - 1
    selectedLines = lines[startIndex:startIndex + limit]
    truncated = startIndex + limit < len(lines)
    selectedText = ''.join(selectedLines)
    previewText, previewTruncated = makePreview(selectedText)
    return toolResult(
        toolCallId='',
        toolName='read',
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


def executeWrite(arguments: dict[str, Any], context: toolContext) -> toolResult:
    pathValue = arguments.get('path')
    content = arguments.get('content')
    if not isinstance(pathValue, str) or not pathValue.strip():
        return toolResult('', 'write', True, 'write.path 必须是非空字符串。')
    if not isinstance(content, str):
        return toolResult('', 'write', True, 'write.content 必须是字符串。')

    path = normalizePath(pathValue, context.workDir)
    if context.debugConsole:
        context.debugConsole.debug(f'写入文件 path={path} bytes={len(content.encode("utf-8"))}')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')
    previewText, truncated = makePreview(content)
    return toolResult(
        toolCallId='',
        toolName='write',
        isError=False,
        content=f'已写入文件：{path}',
        details={
            'path': str(path),
            'bytes': len(content.encode('utf-8')),
            'contentPreview': previewText,
            'truncated': truncated,
        },
    )


def executeEdit(arguments: dict[str, Any], context: toolContext) -> toolResult:
    pathValue = arguments.get('path')
    edits = arguments.get('edits')
    if not isinstance(pathValue, str) or not pathValue.strip():
        return toolResult('', 'edit', True, 'edit.path 必须是非空字符串。')
    if not isinstance(edits, list) or not edits:
        return toolResult('', 'edit', True, 'edit.edits 必须是非空数组。')

    path = normalizePath(pathValue, context.workDir)
    if context.debugConsole:
        context.debugConsole.debug(f'编辑文件 path={path} editCount={len(edits)}')
    if not path.exists() or not path.is_file():
        return toolResult('', 'edit', True, f'文件不存在或不是普通文件：{path}')

    originalContent = path.read_text(encoding='utf-8')
    replacements: list[tuple[int, int, str, str]] = []
    for index, editItem in enumerate(edits):
        if not isinstance(editItem, dict):
            return toolResult('', 'edit', True, f'第 {index + 1} 个 edit 必须是对象。')
        oldText = editItem.get('oldText')
        newText = editItem.get('newText')
        if not isinstance(oldText, str) or oldText == '':
            return toolResult('', 'edit', True, f'第 {index + 1} 个 oldText 必须是非空字符串。')
        if not isinstance(newText, str):
            return toolResult('', 'edit', True, f'第 {index + 1} 个 newText 必须是字符串。')
        matchCount = originalContent.count(oldText)
        if matchCount != 1:
            return toolResult('', 'edit', True, f'第 {index + 1} 个 oldText 必须精确且唯一匹配，当前匹配数：{matchCount}。')
        startIndex = originalContent.index(oldText)
        endIndex = startIndex + len(oldText)
        replacements.append((startIndex, endIndex, oldText, newText))

    replacements.sort(key=lambda item: item[0])
    previousEnd = -1
    for startIndex, endIndex, oldText, newText in replacements:
        if startIndex < previousEnd:
            return toolResult('', 'edit', True, '多个 edits 不能重叠。')
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
        toolCallId='',
        toolName='edit',
        isError=False,
        content=previewText or '文件内容未发生变化。',
        details={
            'path': str(path),
            'editCount': len(edits),
            'diffTruncated': truncated,
        },
    )
