'''
Author: wilbur
Version: 1.6
Date: 2026-08-13
Description: Defines shared lower-camel-case data structures for messages, tools, runtime context, confirmations, agent results, and callable tool outputs. v1.4 adds streaming structures (docs/streamOutputPlan.md §6.2): 3 adapter-layer chunks (textChunk/reasoningChunk/finalChunk) and 7 agent event classes (textDeltaEvent/reasoningDeltaEvent/toolCallStartEvent/toolCallEndEvent/confirmationRequiredEvent/completedEvent/errorEvent) plus the terminalEventTypes tuple.
             v1.5 新增 retryNoticeEvent（模型调用重试非终态事件，不进 terminalEventTypes）。
             v1.6 新增 modelInterruptedError（用户中断信号：shutdown 唤醒/退避打断专用，非模型错误，非 modelRequestError 子类）。
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


# ---------- 流式 chunk（适配器层，docs/streamOutputPlan.md §6.2） ----------


@dataclass
class textChunk:
    text: str


@dataclass
class reasoningChunk:
    text: str


@dataclass
class finalChunk:
    # completion 为 models.chatCompletions.modelCompletion，此处用 Any 避免 core → models 的循环导入。
    completion: Any


# 用户中断信号：shutdown 唤醒/退避打断专用，非模型错误
class modelInterruptedError(Exception):
    pass


# ---------- agent 事件流 ----------


@dataclass
class textDeltaEvent:
    text: str


@dataclass
class reasoningDeltaEvent:
    text: str


@dataclass
class toolCallStartEvent:
    toolCall: toolCall
    preview: str


@dataclass
class toolCallEndEvent:
    toolResult: toolResult


@dataclass
class confirmationRequiredEvent:
    confirmationId: str
    reason: str
    commandPreview: str
    toolCall: toolCall


@dataclass
class completedEvent:
    message: str


@dataclass
class errorEvent:
    message: str
    errorType: str


@dataclass
class retryNoticeEvent:
    message: str
    attempt: int
    retryAfterMs: int
    status: str


# 终态事件：消费者收到时会话锁必然已释放（docs/streamOutputPlan.md §6.4）。
terminalEventTypes = (completedEvent, confirmationRequiredEvent, errorEvent)
