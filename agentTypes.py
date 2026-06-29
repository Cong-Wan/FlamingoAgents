'''
Author: wilbur
Version: 1.0
Date: 2026-06-29
Description: Defines shared lower-camel-case data structures for messages, tools, models, and agent results.
'''

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

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
class toolResult:
    toolCallId: str
    toolName: str
    isError: bool
    content: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class toolExecutionContext:
    workDir: Path
    debugPrinter: Any | None = None


@dataclass
class toolDefinition:
    name: str
    description: str
    parameters: dict[str, Any]
    execute: Callable[[dict[str, Any], toolExecutionContext], toolResult]


@dataclass
class modelConfig:
    provider: str
    model: str
    baseUrl: str
    apiKeyEnv: str
    apiType: str
    supportsToolCalling: bool = True


@dataclass
class agentRunResult:
    sessionId: str
    status: agentStatus
    message: str = ''
    confirmationId: str | None = None
    reason: str | None = None
    commandPreview: str | None = None
    toolCall: toolCall | None = None


@dataclass
class pendingConfirmation:
    sessionId: str
    confirmationId: str
    reason: str
    toolCall: toolCall
