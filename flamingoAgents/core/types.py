'''
Author: wilbur
Version: 1.14
Date: 2026-10-12
Description: Defines shared lower-camel-case data structures for messages, tools, runtime context, confirmations, agent results, and callable tool outputs. v1.8 adds JSON-only providerData to chatMessage/toolCall for Responses opaque item persistence and replay while preserving empty defaults for old sessions. v1.9 adds usageUpdateEvent for per-model-call live usage without Web pricing fields. v1.10（imageInputPlan §3.2）新增 inputImage 与 chatMessage.images（default []，仅 user 消息使用）及 runResult.errorType，供图片输入链路使用。v1.11 toolContext 增加可选 sessionId，供 askSubAgent 把父会话传给子进程账本。v1.12（stopDrain409FixPlan T3）toolContext 增加可选 logger，供工具把 manualStop/normalTimeout/stopProcessOverrun 诊断写入会话 jsonl。v1.13（latencyMetricsDisplayPlan T1.1）usageUpdateEvent 增加可选 timings 字段，随帧下发模型调用耗时/TTFT。v1.14（latencyDisplayImplementationPlan）：toolCall 增加可选 argumentsStartedAtMs（参数首片段到达 UNIX 毫秒，不回发模型）；completedEvent 增加可选 totalElapsedMs（发消息入口到最终 completed 轮次总耗时）。
'''

from __future__ import annotations

import threading
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
    providerData: dict[str, Any] = field(default_factory=dict)
    argumentsStartedAtMs: int | None = None  # v1.14：首个非空参数片段到达时点（UNIX 毫秒）；不回发模型


@dataclass
class inputImage:
    name: str
    mimeType: str
    data: bytes = b''          # 入口传入原始字节；恢复历史时为空，构造请求前按 ref 加载
    ref: str = ''              # 落盘后生成的存储文件名（img-<12位hex>.<ext>）
    bytes: int = 0             # 原始字节数


@dataclass
class chatMessage:
    role: messageRole
    content: str
    toolCalls: list[toolCall] = field(default_factory=list)
    toolCallId: str | None = None
    name: str | None = None
    providerData: dict[str, Any] = field(default_factory=dict)
    images: list[inputImage] = field(default_factory=list)  # 仅 user 消息携带（imageInputPlan）


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
    interruptEvent: threading.Event | None = None
    sessionId: str | None = None
    logger: Any | None = None


@dataclass
class runResult:
    sessionId: str
    status: agentStatus
    message: str = ''
    confirmationId: str | None = None
    reason: str | None = None
    commandPreview: str | None = None
    toolCall: toolCall | None = None
    errorType: str | None = None  # errorEvent.errorType 透传（imageInputPlan §3.2）


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
    totalElapsedMs: int | None = None  # v1.14：发消息入口到本 completed 的整轮耗时（latencyDisplayImplementationPlan §3.4）


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


@dataclass
class usageUpdateEvent:
    usage: dict[str, int]
    stepUsage: dict[str, int]
    contextTokens: int
    timings: dict | None = None  # 最近一次模型调用 timings（latencyMetricsDisplayPlan）：缺省 None 容旧数据


# 终态事件：消费者收到时会话锁必然已释放（docs/streamOutputPlan.md §6.4）。
# usageUpdateEvent 是非终态，不得加入此元组。
terminalEventTypes = (completedEvent, confirmationRequiredEvent, errorEvent)
