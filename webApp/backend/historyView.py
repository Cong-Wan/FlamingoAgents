'''
Author: wilbur
Version: 1.2
Date: 2026-08-08
Description: 会话 jsonl 事件 → GET messages 的 UI 消息 DTO：过滤 systemMessage/modelError/timings，usage 按契约 §2.2-M1 嵌套字段归一化，tool.details 原样透传。
            v1.1 随包改名调整 import（webApp.backend.*）。v1.2（fixPlan Phase2）：assistant DTO 透传 event.reasoning（非空才带，供前端 thinking 历史渲染）。
'''

from __future__ import annotations

from flamingoAgents.utils.jsonl import jsonlLog

from webApp.backend.sessionStore import sessionLogsDir


def normalizeUsage(usage) -> dict | None:
    # 归一化映射（审核 M1）：cachedTokens 取嵌套 prompt_tokens_details.cached_tokens；
    # usage 缺失/非对象 → 整个字段为 null。
    if not isinstance(usage, dict):
        return None
    details = usage.get('prompt_tokens_details')
    if not isinstance(details, dict):
        details = {}
    return {
        'promptTokens': int(usage.get('prompt_tokens') or 0),
        'cachedTokens': int(details.get('cached_tokens') or 0),
        'completionTokens': int(usage.get('completion_tokens') or 0),
    }


def loadMessages(sessionId: str) -> list[dict]:
    logPath = sessionLogsDir / f'{sessionId}.jsonl'
    events = jsonlLog(logPath).readEvents()
    messages: list[dict] = []
    for event in events:
        eventType = event.get('type')
        timestamp = event.get('timestamp')
        if eventType == 'userMessage':
            messages.append({
                'kind': 'user',
                'content': event.get('content', ''),
                'timestamp': timestamp,
            })
        elif eventType == 'assistantMessage':
            toolCalls = [
                {
                    'id': call.get('id', ''),
                    'toolName': call.get('toolName', ''),
                    'arguments': call.get('arguments', {}),
                }
                for call in (event.get('toolCalls') or [])
                if isinstance(call, dict)
            ]
            assistantItem = {
                'kind': 'assistant',
                'content': event.get('content', ''),
                'toolCalls': toolCalls,
                'usage': normalizeUsage(event.get('usage')),
                'model': event.get('model'),
                'timestamp': timestamp,
            }
            reasoning = event.get('reasoning')
            if reasoning:
                assistantItem['reasoning'] = reasoning
            messages.append(assistantItem)
        elif eventType == 'toolResult':
            messages.append({
                'kind': 'tool',
                'toolCallId': event.get('toolCallId', ''),
                'toolName': event.get('toolName', ''),
                'isError': bool(event.get('isError')),
                'content': event.get('content', ''),
                'details': event.get('details') or {},
                'timestamp': timestamp,
            })
        # systemMessage / modelError 不下发（契约 §2.2-M2）；assistantMessage.timings 不下发。
    return messages
