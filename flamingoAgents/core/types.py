'''
Author: wilbur
Version: 1.3
Date: 2026-07-08
Description: Defines shared lower-camel-case data structures for messages, tools, runtime context, confirmations, agent results, and callable tool outputs.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

messageRole = Literal['system', 'user', 'assistant', 'tool']
agentStatus = Literal['completed', 'confirmationRequired', 'error']


@dataclass
class toolCall:
    id: str
    toolName: str
    arguments: dict[str, Any]


@dataclass
class chatMessage:
    role: messageRole
    content: str
    toolCalls: list[toolCall] = field(default_factory=list)
    toolCallId: str | None = None
    name: str | None = None


@dataclass
class toolOutput:
    content: str
    isError: bool = False
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class toolResult:
    toolCallId: str
    toolName: str
    isError: bool
    content: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class toolContext:
    workDir: Path
    debugConsole: Any | None = None


@dataclass
class runResult:
    sessionId: str
    status: agentStatus
    message: str = ''
    confirmationId: str | None = None
    reason: str | None = None
    commandPreview: str | None = None
    toolCall: toolCall | None = None


@dataclass
class pendingConfirm:
    sessionId: str
    confirmationId: str
    reason: str
    toolCalls: list[toolCall]
    currentIndex: int
