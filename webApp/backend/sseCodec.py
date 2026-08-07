'''
Author: wilbur
Version: 1.0
Date: 2026-08-05
Description: 库 7 种事件 dataclass → SSE 文本帧（ensure_ascii=False 单行 JSON），以及只消费泵队列的 SSE 生成器（15s 空闲发 keep-alive 注释帧）。
'''

from __future__ import annotations

import json
import queue

from flamingoAgents.core.types import (
    completedEvent,
    confirmationRequiredEvent,
    errorEvent,
    reasoningDeltaEvent,
    textDeltaEvent,
    toolCallEndEvent,
    toolCallStartEvent,
)

keepAliveIntervalSeconds = 15


def toolCallToDict(call) -> dict:
    return {'id': call.id, 'toolName': call.toolName, 'arguments': call.arguments}


def eventToFrame(event) -> tuple[str, dict]:
    # 与契约 §4.3 事件集一一对应。
    if isinstance(event, textDeltaEvent):
        return 'textDelta', {'text': event.text}
    if isinstance(event, reasoningDeltaEvent):
        return 'reasoningDelta', {'text': event.text}
    if isinstance(event, toolCallStartEvent):
        return 'toolCallStart', {'toolCall': toolCallToDict(event.toolCall), 'preview': event.preview}
    if isinstance(event, toolCallEndEvent):
        result = event.toolResult
        return 'toolCallEnd', {
            'toolResult': {
                'toolCallId': result.toolCallId,
                'toolName': result.toolName,
                'isError': result.isError,
                'content': result.content,
                'details': result.details,
            }
        }
    if isinstance(event, confirmationRequiredEvent):
        return 'confirmationRequired', {
            'confirmationId': event.confirmationId,
            'reason': event.reason,
            'commandPreview': event.commandPreview,
            'toolCall': toolCallToDict(event.toolCall),
        }
    if isinstance(event, completedEvent):
        return 'completed', {'message': event.message}
    if isinstance(event, errorEvent):
        return 'error', {'message': event.message, 'errorType': event.errorType}
    # 泵线程兜底异常已被包装为 errorEvent；走到这里属于未知事件，按 error 帧兜底。
    return 'error', {'message': f'未知事件类型：{type(event).__name__}', 'errorType': type(event).__name__}


def encodeSse(event) -> str:
    eventName, data = eventToFrame(event)
    payload = json.dumps(data, ensure_ascii=False)
    return f'event: {eventName}\ndata: {payload}\n\n'


def sseGen(pump):
    # SSE 生成器只从泵队列取（带 timeout 轮询）；泵结束放 None 哨兵后返回关闭连接。
    while True:
        try:
            event = pump.eventQueue.get(timeout=keepAliveIntervalSeconds)
        except queue.Empty:
            yield ': keep-alive\n\n'
            continue
        if event is None:
            return
        yield encodeSse(event)
