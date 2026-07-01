'''
Author: wilbur
Version: 1.1
Date: 2026-07-01
Description: Detects deletion-related shell commands before bash execution.
'''

from __future__ import annotations

import re
from dataclasses import dataclass

from flamingoAgents.core.types import toolCall, toolResult


@dataclass
class guardDecision:
    allowed: bool
    requiresConfirmation: bool = False
    reason: str = ''


deletePatterns = [
    re.compile(r'(^|[;&|]\s*)rm\s+(-[A-Za-z]*\s+)*[^\n;&|]+', re.IGNORECASE),
    re.compile(r'(^|[;&|]\s*)rmdir\s+[^\n;&|]+', re.IGNORECASE),
    re.compile(r'(^|[;&|]\s*)unlink\s+[^\n;&|]+', re.IGNORECASE),
    re.compile(r'(^|[;&|]\s*)find\s+[^\n;&|]*\s-delete(\s|$)', re.IGNORECASE),
    re.compile(r'os\.(remove|unlink|rmdir)\s*\(', re.IGNORECASE),
    re.compile(r'shutil\.rmtree\s*\(', re.IGNORECASE),
    re.compile(r'pathlib\.[A-Za-z0-9_\.]+\.(unlink|rmdir)\s*\(', re.IGNORECASE),
]


def detectDeletionCommand(command: str) -> bool:
    commandText = command.strip()
    if not commandText:
        return False
    return any(pattern.search(commandText) for pattern in deletePatterns)


def checkToolCall(call: toolCall) -> guardDecision:
    if call.toolName != 'bash':
        return guardDecision(allowed=True)
    command = str(call.arguments.get('command', ''))
    if detectDeletionCommand(command):
        return guardDecision(
            allowed=False,
            requiresConfirmation=True,
            reason='删除命令需要用户确认',
        )
    return guardDecision(allowed=True)


def makeBlockedToolResult(call: toolCall, reason: str) -> toolResult:
    return toolResult(
        toolCallId=call.id,
        toolName=call.toolName,
        isError=True,
        content=f'命令已被用户拒绝：{reason}。',
        details={
            'blocked': True,
            'reason': 'userRejectedDeletionCommand',
        },
    )
