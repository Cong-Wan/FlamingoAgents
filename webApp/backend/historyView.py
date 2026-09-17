'''
Author: wilbur
Version: 1.5
Date: 2026-09-15
Description: 会话 jsonl 事件 → GET messages 的 UI 消息 DTO：过滤 systemMessage/重试中 modelError/timings，usage 按契约 §2.2-M1 嵌套字段归一化，tool.details 原样透传。
            v1.1 随包改名调整 import（webApp.backend.*）。v1.2（fixPlan Phase2）：assistant DTO 透传 event.reasoning（非空才带，供前端 thinking 历史渲染）。
            v1.3 按 sessions 索引 workDir 定位 ~/.flamingo/logs/webData/<folder>/{sessionId}.jsonl；不存在则直接返回 []（不构造 jsonlLog，零 mkdir）。
            v1.4（imageInputPlan）：user DTO 增加 images 引用元数据（无 base64）；严格读取 JSONL。
            v1.5（chatUxImprovePlan）：终态 modelError（willRetry 非 true）下发 kind=error，文案对齐 SSE；request/traceback/diag 不下发。
'''

from __future__ import annotations

from pathlib import Path

from flamingoAgents.core.imageInput import imageRefMeta, restoreImagesFromEvent
from flamingoAgents.utils.jsonl import jsonlLog
from flamingoAgents.utils.logPaths import resolveSessionLogDir

from webApp.backend import sessionStore


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
    meta = sessionStore.getSession(sessionId)
    if meta is None:
        return []
    logPath = resolveSessionLogDir('webData', Path(meta['workDir'])) / f'{sessionId}.jsonl'
    if not logPath.exists():
        return []
    events = jsonlLog(logPath).readEvents(strict=True)
    messages: list[dict] = []
    for event in events:
        eventType = event.get('type')
        timestamp = event.get('timestamp')
        if eventType == 'userMessage':
            images = [imageRefMeta(image) for image in restoreImagesFromEvent(event.get('images'))]
            messages.append({
                'kind': 'user',
                'content': event.get('content', ''),
                'timestamp': timestamp,
                'images': images,
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
        elif eventType == 'modelError':
            # 终态失败下发 kind=error；重试中的 willRetry=true 仍不下发（契约 §2.2-M2）。
            if event.get('willRetry') is True:
                continue
            rawMessage = event.get('message') or ''
            try:
                attemptNum = int(event.get('attempt') or 1)
            except (TypeError, ValueError):
                attemptNum = 1
            retries = max(0, attemptNum - 1)
            messages.append({
                'kind': 'error',
                'content': f'模型调用失败（已重试{retries}次）：{rawMessage}',
                'errorType': event.get('errorType') or '',
                'attempt': attemptNum,
                'timestamp': timestamp,
            })
        # systemMessage 不下发；重试中 modelError 已跳过；assistantMessage.timings 不下发。
    return messages
