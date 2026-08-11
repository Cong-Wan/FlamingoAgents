'''
Author: wilbur
Version: 1.1
Date: 2026-08-11
Description: 库 7 种事件 dataclass → SSE 文本帧（ensure_ascii=False 单行 JSON），以及只消费订阅队列的 SSE 生成器（15s 空闲发 keep-alive 注释帧）。
            v1.1 多窗口并行（multiWindowStreamingPlan §4.2）：sseGen 签名改 (eventQueue, meta, pump)——attach 订阅首发
            streamResume 帧（baseCount/userMessage）；finally 经 pump.unsubscribe 反注册死订阅。
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


def encodeResumeFrame(meta: dict) -> str:
    # attach 订阅首帧（multiWindowStreamingPlan §4.2）：baseCount=泵启动前消息水位线；userMessage=本次流用户消息（confirm 流为 None）。
    payload = json.dumps(
        {'baseCount': meta.get('baseCount', 0), 'userMessage': meta.get('userMessage')},
        ensure_ascii=False,
    )
    return f'event: streamResume\ndata: {payload}\n\n'


def sseGen(eventQueue, meta=None, pump=None):
    # SSE 生成器只从订阅队列取（带 timeout 轮询）；泵结束放 None 哨兵后返回关闭连接。
    # meta 非 None（attach 订阅）时先发 streamResume 帧；pump 非 None 时 finally 反注册订阅（客户端断连清理死订阅）。
    try:
        if meta is not None:
            yield encodeResumeFrame(meta)
        while True:
            try:
                event = eventQueue.get(timeout=keepAliveIntervalSeconds)
            except queue.Empty:
                yield ': keep-alive\n\n'
                continue
            if event is None:
                return
            yield encodeSse(event)
    finally:
        if pump is not None:
            pump.unsubscribe(eventQueue)
