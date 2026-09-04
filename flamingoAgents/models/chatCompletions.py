'''
Author: wilbur
Version: 1.20
Date: 2026-09-02
Description: Adapts internal messages/tools to OpenAI-compatible Chat Completions. v1.19 adds stack-local stream diagnosis: modelRequestError.diag, connect/firstByte/streamRead/decode stages, response-header whitelist, and success timings including sawDone. v1.20 swallows mergeErrorDiag failures so diagnosis cannot replace the original modelRequestError.
'''

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Iterator

from flamingoAgents.core.types import chatMessage, finalChunk, modelInterruptedError, reasoningChunk, textChunk, toolCall
from flamingoAgents.models.modelAuth import modelAuth
from flamingoAgents.models.modelConfig import modelConfig

diagHeaderNames = frozenset({
    'x-request-id',
    'cf-ray',
    'x-served-by',
    'retry-after',
    'date',
})


@dataclass
class modelCompletion:
    message: chatMessage
    requestPayload: dict[str, Any]
    responsePayload: dict[str, Any]


class modelRequestError(Exception):
    def __init__(self, message: str, requestPayload: dict[str, Any], statusCode: int | None = None, responseBody: str = '', retryAfterSeconds: float | None = None, diag: dict | None = None):
        super().__init__(message)
        self.requestPayload = requestPayload
        self.statusCode = statusCode
        self.responseBody = responseBody
        self.retryAfterSeconds = retryAfterSeconds
        self.diag = diag


def pickDiagHeaders(headers) -> dict[str, str]:
    picked: dict[str, str] = {}
    if headers is None:
        return picked
    try:
        items = headers.items()
    except Exception:
        return picked
    try:
        for rawKey, rawValue in items:
            key = str(rawKey).lower()
            if key not in diagHeaderNames and not key.startswith('x-ratelimit-'):
                continue
            try:
                encoded = str(rawValue).encode('utf-8')[:256]
                picked[key] = encoded.decode('utf-8', errors='ignore')
            except Exception:
                continue
    except Exception:
        return picked
    return picked


def pickRequestId(headersDict: dict[str, str]) -> str | None:
    return headersDict.get('x-request-id') or headersDict.get('cf-ray')


def exceptionErrno(error) -> int | None:
    errno = getattr(error, 'errno', None)
    if isinstance(errno, int):
        return errno
    reason = getattr(error, 'reason', None)
    reasonErrno = getattr(reason, 'errno', None)
    return reasonErrno if isinstance(reasonErrno, int) else None


def elapsedMs(t0: float) -> int:
    value = int((time.monotonic() - t0) * 1000)
    return value if value >= 0 else 0


def applyHeaderDiag(diag: dict[str, Any], headers) -> None:
    try:
        picked = pickDiagHeaders(headers)
    except Exception:
        return
    if not picked:
        return
    diag['responseHeaders'] = picked
    requestId = pickRequestId(picked)
    if requestId:
        diag['requestId'] = requestId


def applyExceptionDiag(diag: dict[str, Any], error) -> None:
    if error is None:
        return
    diag['exceptionName'] = type(error).__name__
    errno = exceptionErrno(error)
    if errno is not None:
        diag['errno'] = errno
    else:
        diag.pop('errno', None)


def connectDiagFromError(error, *, config, requestBytes: bytes) -> dict[str, Any]:
    diag: dict[str, Any] = {
        'stage': 'connect',
        'exceptionName': type(error).__name__,
        'api': config.apiType,
        'baseUrl': config.baseUrl,
        'requestBytesLen': len(requestBytes),
    }
    errno = exceptionErrno(error)
    if errno is not None:
        diag['errno'] = errno
    applyHeaderDiag(diag, getattr(error, 'headers', None))
    return diag


def newStreamDiag(config, requestPayload: dict[str, Any]) -> dict[str, Any]:
    diag: dict[str, Any] = {
        't0': time.monotonic(),
        'chunks': 0,
        'textChars': 0,
        'reasoningChars': 0,
        'api': config.apiType,
        'baseUrl': config.baseUrl,
    }
    try:
        diag['requestBytesLen'] = len(json.dumps(requestPayload, ensure_ascii=False).encode('utf-8'))
    except Exception:
        pass
    return diag


def mergeErrorDiag(error, diag: dict[str, Any], *, stage: str | None = None, underlying=None) -> None:
    try:
        existing = getattr(error, 'diag', None)
        if isinstance(existing, dict):
            for key, value in existing.items():
                if key != 't0':
                    diag[key] = value
        if stage:
            diag['stage'] = stage
        if underlying is not None:
            applyExceptionDiag(diag, underlying)
        t0 = diag.get('t0')
        if isinstance(t0, (int, float)):
            diag['durationMs'] = elapsedMs(t0)
        error.diag = diag
    except Exception:
        try:
            error.diag = diag
        except Exception:
            pass


def successTimings(diag: dict[str, Any], *, textChars: int, reasoningChars: int, sawDone: bool) -> dict[str, Any]:
    t0 = diag.get('t0')
    durationMs = elapsedMs(t0) if isinstance(t0, (int, float)) else 0
    return {
        'ttfbMs': diag.get('ttfbMs'),
        'durationMs': durationMs,
        'chunks': int(diag.get('chunks') or 0),
        'textChars': textChars,
        'reasoningChars': reasoningChars,
        'sawDone': sawDone,
    }


class chatCompletionsAdapter:
    def __init__(self, config: modelConfig, auth: modelAuth, debugConsole=None):
        self.config = config
        self.auth = auth
        self.debugConsole = debugConsole
        self.activeResponses: set = set()
        self.activeResponsesLock = threading.Lock()

    def interruptActiveStreams(self):
        with self.activeResponsesLock:
            responses = list(self.activeResponses)
        for response in responses:
            # 先打标再 shutdown：调用方若先 interrupt 后 set stopEvent，
            # read1 可能在 set 之前就以空字节/异常返回，必须靠标记识别中断。
            try:
                setattr(response, '_flamingoInterrupted', True)
            except Exception:
                pass
            try:
                response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass

    def buildRequestPayload(self, messages: list[chatMessage], tools: list[dict[str, Any]], stream: bool) -> dict[str, Any]:
        requestPayload: dict[str, Any] = {
            'model': self.config.model,
            'messages': [self.convertMessage(message) for message in messages],
            'tools': tools,
            'tool_choice': 'auto',
        }
        if self.config.thinking:
            requestPayload['thinking'] = self.config.thinking
        if self.config.reasoningEffort:
            requestPayload['reasoning_effort'] = self.config.reasoningEffort
        if stream:
            requestPayload['stream'] = True
            # 流式默认不下发 token 用量，必须显式要求，服务端才会在 [DONE] 前单独发一个携带 usage 的 chunk。
            requestPayload['stream_options'] = {'include_usage': True}
        return requestPayload

    def openRequest(self, requestPayload: dict[str, Any]):
        requestUrl = self.config.baseUrl.rstrip('/') + '/chat/completions'
        requestBytes = json.dumps(requestPayload, ensure_ascii=False).encode('utf-8')
        # 默认伪装 OpenAI 官方 JS SDK UA，避免 urllib 自动带上 Python-urllib/x.y；
        # models.yaml 自定义 headers 覆盖同名键；Authorization/Content-Type 始终由系统覆盖。
        requestHeaders = {'User-Agent': 'OpenAI/JS 6.26.0'}
        requestHeaders.update(self.config.headers or {})
        requestHeaders['Authorization'] = self.auth.authorizationHeader
        requestHeaders['Content-Type'] = 'application/json'
        request = urllib.request.Request(
            requestUrl,
            data=requestBytes,
            method='POST',
            headers=requestHeaders,
        )
        if self.debugConsole:
            self.debugConsole.debug(f"Source request:\n{requestBytes.decode('utf-8')}\n")
        try:
            # 读超时 300s：thinking 模型静默期可达 1~3 分钟，60s 会误杀长思考；
            # 保持有限值（非 None）兜底真死连接，避免永久占用会话锁。
            return urllib.request.urlopen(request, timeout=300)
        except urllib.error.HTTPError as error:
            errorText = error.read().decode('utf-8', errors='replace')
            retryAfterSeconds = None
            retryAfterValue = error.headers.get('Retry-After') if error.headers else None
            if retryAfterValue is not None:
                try:
                    retryAfterSeconds = float(retryAfterValue)
                except (TypeError, ValueError):
                    try:
                        retryAfterSeconds = parsedate_to_datetime(retryAfterValue).timestamp() - time.time()
                        if retryAfterSeconds < 0:
                            retryAfterSeconds = None
                    except (TypeError, ValueError, IndexError, OverflowError):
                        retryAfterSeconds = None
            requestError = modelRequestError(
                message=f'模型请求失败：status={error.code} body={errorText[:1000]}',
                requestPayload=requestPayload,
                statusCode=error.code,
                responseBody=errorText,
                retryAfterSeconds=retryAfterSeconds,
            )
            try:
                requestError.diag = connectDiagFromError(error, config=self.config, requestBytes=requestBytes)
            except Exception:
                pass
            raise requestError from error
        except urllib.error.URLError as error:
            requestError = modelRequestError(
                message=f'模型请求失败：{error.reason}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=str(error.reason),
            )
            try:
                requestError.diag = connectDiagFromError(error, config=self.config, requestBytes=requestBytes)
            except Exception:
                pass
            raise requestError from error

    def complete(self, messages: list[chatMessage], tools: list[dict[str, Any]], sessionId: str | None = None) -> modelCompletion:
        requestPayload = self.buildRequestPayload(messages, tools, stream=False)
        diag = newStreamDiag(self.config, requestPayload)
        try:
            with self.openRequest(requestPayload) as response:
                applyHeaderDiag(diag, getattr(response, 'headers', None))
                responseText = response.read().decode('utf-8')
        except modelRequestError as error:
            mergeErrorDiag(error, diag, stage='connect', underlying=error.__cause__)
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            requestError = modelRequestError(
                message=f'模型请求失败：{error}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=str(error),
            )
            mergeErrorDiag(requestError, diag, stage='firstByte', underlying=error)
            raise requestError from error

        payload = json.loads(responseText)
        # stream=False 回退归一化：把 choices[0].message.reasoning_content 提升到顶层 reasoning
        # （仅顶层，不删原字段、不入 chatMessage；D2 红线：reasoning 不得进入发往模型的 messages）
        msg = ((payload.get('choices') or [{}])[0]).get('message') or {}
        if msg.get('reasoning_content'):
            payload['reasoning'] = msg['reasoning_content']
        if self.debugConsole:
            self.debugConsole.debug(f"\nSource response:\n{payload}")
        message = self.parseAssistantPayload(payload)
        payload['timings'] = successTimings(
            diag,
            textChars=len(message.content or ''),
            reasoningChars=len(payload.get('reasoning') or ''),
            sawDone=True,
        )
        return modelCompletion(
            message=message,
            requestPayload=requestPayload,
            responsePayload=payload,
        )

    def completeStream(
        self,
        messages: list[chatMessage],
        tools: list[dict[str, Any]],
        stopEvent=None,
        sessionId: str | None = None,
    ) -> Iterator:
        # sessionId 由 Responses 用于缓存/请求头；Chat Completions 为兼容统一端口而接收但忽略。
        if not self.config.stream:
            yield finalChunk(completion=self.complete(messages, tools, sessionId=sessionId))
            return
        requestPayload = self.buildRequestPayload(messages, tools, stream=True)
        yield from self.consumeSseStream(requestPayload, stopEvent=stopEvent)

    def consumeSseStream(self, requestPayload: dict[str, Any], stopEvent=None) -> Iterator:
        contentParts: list[str] = []
        toolCallAccum: dict[int, dict[str, Any]] = {}
        reasoningParts: list[str] = []
        responseModel: str | None = None
        usage: dict[str, Any] | None = None
        chunkCount = 0
        ssePayloadSeen = False
        sawDone = False
        opened = False
        diag = newStreamDiag(self.config, requestPayload)
        try:
            with self.openRequest(requestPayload) as response:
                opened = True
                applyHeaderDiag(diag, getattr(response, 'headers', None))
                with self.activeResponsesLock:
                    self.activeResponses.add(response)
                try:
                    for dataPayload in self.iterSseData(response, stopEvent=stopEvent):
                        if dataPayload == '[DONE]':
                            sawDone = True
                            break
                        ssePayloadSeen = True
                        chunkCount += 1
                        diag['chunks'] = chunkCount
                        try:
                            for event in self.processSseData(dataPayload, requestPayload, contentParts, toolCallAccum, reasoningParts):
                                if isinstance(event, dict):
                                    if event.get('model'):
                                        responseModel = event['model']
                                    if event.get('usage') is not None:
                                        usage = event['usage']
                                else:
                                    self._noteStreamYield(diag, event)
                                    yield event
                        except modelRequestError as error:
                            mergeErrorDiag(error, diag, stage='decode', underlying=error.__cause__)
                            raise
                    if self._isStreamInterrupted(response, stopEvent):
                        raise modelInterruptedError('用户已停止')
                finally:
                    with self.activeResponsesLock:
                        self.activeResponses.discard(response)
        except modelInterruptedError:
            raise
        except modelRequestError as error:
            if not opened:
                mergeErrorDiag(error, diag, stage='connect', underlying=error.__cause__)
            elif getattr(error, 'diag', None) is not diag:
                mergeErrorDiag(error, diag, underlying=error.__cause__)
            else:
                t0 = diag.get('t0')
                if isinstance(t0, (int, float)):
                    diag['durationMs'] = elapsedMs(t0)
            raise
        except (urllib.error.URLError, http.client.HTTPException, OSError) as error:
            requestError = modelRequestError(
                message=f'模型流式响应中断：{error}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=str(error),
            )
            mergeErrorDiag(
                requestError,
                diag,
                stage='streamRead' if ssePayloadSeen else 'firstByte',
                underlying=error,
            )
            raise requestError from error

        synthesizedToolCalls = [
            {
                'id': accum['id'] or f'call_{index + 1}',
                'type': 'function',
                'function': {
                    'name': accum['name'],
                    'arguments': ''.join(accum['argumentsParts']),
                },
            }
            for index, accum in sorted(toolCallAccum.items())
        ]
        messagePayload: dict[str, Any] = {'role': 'assistant', 'content': ''.join(contentParts)}
        if synthesizedToolCalls:
            messagePayload['tool_calls'] = synthesizedToolCalls
        responsePayload: dict[str, Any] = {
            'model': responseModel or self.config.model,
            'choices': [{'index': 0, 'message': messagePayload}],
        }
        if usage is not None:
            responsePayload['usage'] = usage
        reasoningText = ''.join(reasoningParts)
        if reasoningText:
            responsePayload['reasoning'] = reasoningText
        if self.debugConsole:
            self.debugConsole.debug(f"\nSource response (streamed, chunks={chunkCount}):\n{responsePayload}")
        responsePayload['timings'] = successTimings(
            diag,
            textChars=len(''.join(contentParts)),
            reasoningChars=len(''.join(reasoningParts)),
            sawDone=sawDone,
        )
        yield finalChunk(completion=modelCompletion(
            message=self.parseAssistantPayload(responsePayload),
            requestPayload=requestPayload,
            responsePayload=responsePayload,
        ))

    def _noteStreamYield(self, diag: dict[str, Any], event) -> None:
        if isinstance(event, textChunk):
            diag['textChars'] = int(diag.get('textChars') or 0) + len(event.text or '')
        elif isinstance(event, reasoningChunk):
            diag['reasoningChars'] = int(diag.get('reasoningChars') or 0) + len(event.text or '')
        else:
            return
        if 'ttfbMs' not in diag:
            t0 = diag.get('t0')
            if isinstance(t0, (int, float)):
                diag['ttfbMs'] = elapsedMs(t0)

    def _isStreamInterrupted(self, response, stopEvent) -> bool:
        if stopEvent is not None and stopEvent.is_set():
            return True
        return bool(getattr(response, '_flamingoInterrupted', False))

    def iterSseData(self, response, stopEvent=None) -> Iterator[str]:
        # 按字节缓冲半行，凑满一行再 decode（多字节 UTF-8 可能跨 chunk 切断）；\n 是 ASCII，不会出现在 UTF-8 多字节序列内。
        buffer = b''
        # read(amt) 在 chunked 响应上会阻塞凑满 amt 才返回，把匀速小增量攒成大批量；read1 有数据即返回（streamingLatencyFixPlan D1）。
        readChunk = getattr(response, 'read1', None)
        if not callable(readChunk):
            readChunk = response.read
        while True:
            try:
                data = readChunk(4096)
            except Exception:
                if self._isStreamInterrupted(response, stopEvent):
                    raise modelInterruptedError('用户已停止')
                raise
            if not data:
                break
            buffer += data
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n', 1)
                payload = self.parseSseLine(line)
                if payload is not None:
                    yield payload
        if self._isStreamInterrupted(response, stopEvent):
            raise modelInterruptedError('用户已停止')
        tail = self.parseSseLine(buffer)
        if tail is not None:
            yield tail

    def parseSseLine(self, line: bytes) -> str | None:
        line = line.strip()
        # 跳过空行（事件分隔符）与 : 开头的注释/心跳行。
        if not line or line.startswith(b':'):
            return None
        if not line.startswith(b'data:'):
            return None
        return line[5:].strip().decode('utf-8', errors='replace')

    def processSseData(
        self,
        dataPayload: str,
        requestPayload: dict[str, Any],
        contentParts: list[str],
        toolCallAccum: dict[int, dict[str, Any]],
        reasoningParts: list[str],
    ) -> Iterator:
        try:
            data = json.loads(dataPayload)
        except json.JSONDecodeError as error:
            raise modelRequestError(
                message=f'模型流式响应不是合法 JSON：{dataPayload[:200]}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=dataPayload,
            ) from error
        if not isinstance(data, dict):
            return
        # GLM 等 provider 在 HTTP 200 后以 data 事件内嵌 error 下发，必须识别并转为 modelRequestError。
        if data.get('error'):
            raise modelRequestError(
                message=f'模型流式响应错误：{json.dumps(data["error"], ensure_ascii=False)[:500]}',
                requestPayload=requestPayload,
                statusCode=None,
                responseBody=dataPayload,
            )
        meta: dict[str, Any] = {}
        if data.get('model'):
            meta['model'] = data['model']
        if data.get('usage') is not None:
            meta['usage'] = data['usage']
        if meta:
            yield meta
        choices = data.get('choices') or []
        if not choices or not isinstance(choices[0], dict):
            return
        delta = choices[0].get('delta') or {}
        if not isinstance(delta, dict):
            return
        text = delta.get('content')
        if text:
            contentParts.append(text)
            yield textChunk(text=text)
        reasoning = delta.get('reasoning_content')
        if reasoning:
            reasoningParts.append(reasoning)
            yield reasoningChunk(text=reasoning)
        for rawToolCall in delta.get('tool_calls') or []:
            if not isinstance(rawToolCall, dict):
                continue
            index = rawToolCall.get('index', 0)
            accum = toolCallAccum.setdefault(index, {'id': '', 'name': '', 'argumentsParts': []})
            # 首个 chunk 才带 id/name，后续只有 arguments 片段，不得直接覆盖。
            if rawToolCall.get('id'):
                accum['id'] = rawToolCall['id']
            functionValue = rawToolCall.get('function') or {}
            if isinstance(functionValue, dict):
                if functionValue.get('name'):
                    accum['name'] = functionValue['name']
                if functionValue.get('arguments'):
                    accum['argumentsParts'].append(functionValue['arguments'])

    def convertMessage(self, message: chatMessage) -> dict[str, Any]:
        if message.role == 'tool':
            return {
                'role': 'tool',
                'tool_call_id': message.toolCallId,
                'content': message.content,
            }
        content = message.content
        # 仅请求构造：无 toolCalls 的空 assistant 发 '.'，避免 provider 400；不写回 message。
        if message.role == 'assistant' and not message.toolCalls and not (content or '').strip():
            content = '.'
        converted: dict[str, Any] = {
            'role': message.role,
            'content': content,
        }
        if message.role == 'assistant' and message.toolCalls:
            converted['tool_calls'] = [
                {
                    'id': call.id,
                    'type': 'function',
                    'function': {
                        'name': call.toolName,
                        'arguments': json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.toolCalls
            ]
        return converted

    def parseAssistantPayload(self, payload: dict[str, Any]) -> chatMessage:
        choices = payload.get('choices')
        if not isinstance(choices, list) or not choices:
            raise RuntimeError('模型响应缺少 choices。')
        rawMessage = choices[0].get('message')
        if not isinstance(rawMessage, dict):
            raise RuntimeError('模型响应缺少 message。')

        parsedToolCalls: list[toolCall] = []
        rawToolCalls = rawMessage.get('tool_calls') or []
        if not isinstance(rawToolCalls, list):
            raise RuntimeError('模型响应 tool_calls 必须是数组。')
        for index, rawCall in enumerate(rawToolCalls):
            if not isinstance(rawCall, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call 必须是对象。')
            functionValue = rawCall.get('function') or {}
            if not isinstance(functionValue, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.function 必须是对象。')
            argumentsText = functionValue.get('arguments') or '{}'
            if not isinstance(argumentsText, str):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是字符串。')
            try:
                arguments = json.loads(argumentsText)
            except json.JSONDecodeError as error:
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 不是合法 JSON。') from error
            if not isinstance(arguments, dict):
                raise RuntimeError(f'第 {index + 1} 个 tool_call.arguments 必须是 JSON 对象。')
            parsedToolCalls.append(toolCall(
                id=rawCall.get('id') or f'call_{index + 1}',
                toolName=functionValue.get('name') or '',
                arguments=arguments,
            ))

        content = rawMessage.get('content') or ''
        return chatMessage(role='assistant', content=content, toolCalls=parsedToolCalls)
