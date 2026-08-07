'''
Author: wilbur
Version: 1.1
Date: 2026-08-05
Description: FastAPI 应用与全部路由：认证依赖、统一异常映射（库 RuntimeError → 400 透传中文消息）、sessionId 入口校验、SSE 对话流、静态文件容忍空目录挂载。
            v1.1 随包改名调整 import（webApp.backend.*）；静态目录由 static/ 改为 webApp/frontend/，projectRoot 随目录加深改为 parents[2]。
'''

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from flamingoAgents.models.modelConfig import loadModelConfigFromYaml

from webApp.backend import agentManager, historyView, modelConfigStore, sessionStore
from webApp.backend.auth import authDependency, checkToken
from webApp.backend.sseCodec import sseGen

projectRoot = Path(__file__).resolve().parents[2]
staticDir = Path(__file__).resolve().parents[1] / 'frontend'

sessionIdPattern = re.compile(r'[A-Za-z0-9_-]+')
sseHeaders = {'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'}

app = FastAPI(title='FlamingoAgents Web')


# ---------- 统一异常映射（审核 M5）：库 RuntimeError → 400 透传中文消息 ----------

# 注册在 starlette 父类上：路由级 404 抛的是 starlette.HTTPException，端点内抛的 fastapi.HTTPException 是其子类，均可命中。
@app.exception_handler(StarletteHTTPException)
async def httpExceptionHandler(request: Request, exc: StarletteHTTPException):
    return JSONResponse(status_code=exc.status_code, content={'error': exc.detail})


@app.exception_handler(RuntimeError)
async def runtimeErrorHandler(request: Request, exc: RuntimeError):
    return JSONResponse(status_code=400, content={'error': str(exc)})


@app.exception_handler(RequestValidationError)
async def validationErrorHandler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={'error': '请求体不是合法 JSON 或结构不符。'})


@app.exception_handler(Exception)
async def fallbackErrorHandler(request: Request, exc: Exception):
    return JSONResponse(status_code=500, content={'error': f'服务器内部错误（{type(exc).__name__}）。'})


@app.middleware('http')
async def rejectPathTraversal(request: Request, callNext):
    # 纵深防御（审核 M1）：uvicorn 会先解码 %2F 导致带 ../ 的 sessionId 落到路由 404，统一拦成 400。
    if request.url.path.startswith('/api/') and '..' in request.url.path:
        return JSONResponse(status_code=400, content={'error': 'sessionId 非法：仅允许字母、数字、下划线、连字符。'})
    return await callNext(request)


def checkSessionId(value) -> str:
    if not isinstance(value, str) or not sessionIdPattern.fullmatch(value):
        raise HTTPException(status_code=400, detail='sessionId 非法：仅允许字母、数字、下划线、连字符。')
    return value


def requireSession(sessionId: str) -> dict:
    session = sessionStore.getSession(sessionId)
    if session is None:
        raise HTTPException(status_code=404, detail=f'会话不存在：{sessionId}')
    return session


def sseResponse(pump) -> StreamingResponse:
    return StreamingResponse(sseGen(pump), media_type='text/event-stream', headers=sseHeaders)


# ---------- 登录（唯一免认证接口） ----------

@app.post('/api/auth/login')
def login(body: dict = Body(...)):
    token = body.get('token') if isinstance(body, dict) else None
    if not checkToken(token):
        raise HTTPException(status_code=401, detail='token 不正确。')
    return {'ok': True}


# ---------- 认证 API 路由 ----------

authedApi = APIRouter(prefix='/api', dependencies=[authDependency])


@authedApi.get('/health')
def health():
    return {'ok': True, 'version': '0.1.0'}


@authedApi.get('/sessions')
def listSessions():
    return {'sessions': sessionStore.listSessions()}


@authedApi.post('/sessions')
def createSession(body: dict = Body(...)):
    providerId = body.get('providerId') if isinstance(body, dict) else None
    if not isinstance(providerId, str) or not providerId.strip():
        raise HTTPException(status_code=400, detail='providerId 必填。')
    providerId = providerId.strip()
    modelId = body.get('modelId')
    if modelId is not None and not isinstance(modelId, str):
        raise HTTPException(status_code=400, detail='modelId 必须是字符串。')
    workDirRaw = body.get('workDir')
    if workDirRaw is None:
        workPath = projectRoot
    elif isinstance(workDirRaw, str) and workDirRaw.strip():
        workPath = Path(workDirRaw).expanduser()
    else:
        raise HTTPException(status_code=400, detail='workDir 必须是非空字符串。')
    if not workPath.is_dir():
        raise HTTPException(status_code=400, detail=f'workDir 不存在或不是目录：{workPath}')
    # 预检（审核 M3/M4）：yaml 缺失时库会静默回退环境变量配置，必须 Web 层先行拦截。
    if not modelConfigStore.modelsYamlPath.exists():
        raise HTTPException(status_code=400, detail='config/models.yaml 不存在。')
    resolved = loadModelConfigFromYaml(providerId=providerId, modelId=modelId or None)
    return sessionStore.createSession(str(workPath.resolve()), providerId, resolved.config.model)


@authedApi.patch('/sessions/{sessionId}')
def renameSession(sessionId: str, body: dict = Body(...)):
    checkSessionId(sessionId)
    title = body.get('title') if isinstance(body, dict) else None
    if not isinstance(title, str) or not (1 <= len(title.strip()) <= 60):
        raise HTTPException(status_code=400, detail='title 需为 1–60 字。')
    session = sessionStore.renameSession(sessionId, title.strip())
    if session is None:
        raise HTTPException(status_code=404, detail=f'会话不存在：{sessionId}')
    return session


@authedApi.delete('/sessions/{sessionId}')
def deleteSession(sessionId: str):
    checkSessionId(sessionId)
    requireSession(sessionId)
    if agentManager.hasActiveStream(sessionId):
        raise HTTPException(status_code=409, detail='该会话有活跃流，无法删除。')
    sessionStore.deleteSession(sessionId)
    logPath = sessionStore.sessionLogsDir / f'{sessionId}.jsonl'
    logPath.unlink(missing_ok=True)
    agentManager.dropAgent(sessionId)
    return {'ok': True}


@authedApi.get('/sessions/{sessionId}/messages')
def getMessages(sessionId: str):
    checkSessionId(sessionId)
    requireSession(sessionId)
    return {'messages': historyView.loadMessages(sessionId)}


@authedApi.get('/sessions/{sessionId}/pending')
def getPending(sessionId: str):
    # 挂起确认查询（审核 H1）：数据源为 agent 缓存实例的 conversation.pending（仅存内存）。
    checkSessionId(sessionId)
    requireSession(sessionId)
    agentInstance = agentManager.getCachedAgent(sessionId)
    pending = None
    if agentInstance is not None:
        with agentInstance.sessionLocksGuard:
            currentConversation = agentInstance.conversations.get(sessionId)
        if currentConversation is not None:
            pending = currentConversation.pending
    if pending is None:
        return {'pending': None}
    currentCall = pending.toolCalls[pending.currentIndex]
    definition = agentInstance.toolRegistry.get(currentCall.toolName)
    commandPreview = (
        agentInstance.buildToolPreview(definition, currentCall) if definition is not None else str(currentCall.arguments)
    )
    return {
        'pending': {
            'confirmationId': pending.confirmationId,
            'reason': pending.reason,
            'commandPreview': commandPreview,
            'toolCall': {
                'id': currentCall.id,
                'toolName': currentCall.toolName,
                'arguments': currentCall.arguments,
            },
        }
    }


@authedApi.get('/usage')
def getUsage():
    sessions = sessionStore.listSessions()
    total = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}
    entries = []
    for session in sessions:
        usage = session.get('usage') or {}
        for key in total:
            total[key] += int(usage.get(key, 0) or 0)
        entries.append({
            'sessionId': session['sessionId'],
            'title': session.get('title', ''),
            'providerId': session.get('providerId', ''),
            'modelId': session.get('modelId', ''),
            'usage': usage,
            'updatedAt': session.get('updatedAt', ''),
        })
    return {'total': total, 'sessions': entries}


@authedApi.get('/models')
def getModels():
    return modelConfigStore.readModelsConfig()


@authedApi.put('/models')
def putModels(body: dict = Body(...)):
    modelConfigStore.writeModelsConfig(body)
    agentManager.invalidateAllAgents()
    return {'ok': True}


@authedApi.post('/chat/stream')
def chatStream(body: dict = Body(...)):
    sessionId = checkSessionId(body.get('sessionId') if isinstance(body, dict) else None)
    requireSession(sessionId)
    message = body.get('message')
    if not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail='message 不能为空。')
    cleanMessage = message.strip()
    agentInstance = agentManager.getAgent(sessionId)
    stream = agentInstance.runUserMessageStream(cleanMessage, sessionId)
    pump = agentManager.startStream(sessionId, agentInstance, stream)
    if pump is None:
        raise HTTPException(status_code=409, detail='该会话已有活跃流，请稍后再试。')
    # 首条用户消息发出后标题自动改为前 20 字；发消息刷新 updatedAt（契约 §2.1）。
    sessionStore.setDefaultTitle(sessionId, cleanMessage[:20])
    sessionStore.touchSession(sessionId)
    return sseResponse(pump)


@authedApi.post('/chat/confirm')
def chatConfirm(body: dict = Body(...)):
    sessionId = checkSessionId(body.get('sessionId') if isinstance(body, dict) else None)
    requireSession(sessionId)
    confirmationId = body.get('confirmationId')
    if not isinstance(confirmationId, str) or not confirmationId.strip():
        raise HTTPException(status_code=400, detail='confirmationId 不能为空。')
    approved = body.get('approved')
    if not isinstance(approved, bool):
        raise HTTPException(status_code=400, detail='approved 必须是布尔值。')
    agentInstance = agentManager.getAgent(sessionId)
    stream = agentInstance.continueConfirmationStream(sessionId, confirmationId, approved)
    pump = agentManager.startStream(sessionId, agentInstance, stream)
    if pump is None:
        raise HTTPException(status_code=409, detail='该会话已有活跃流，请稍后再试。')
    sessionStore.touchSession(sessionId)
    return sseResponse(pump)


@authedApi.post('/chat/stop')
def chatStop(body: dict = Body(...)):
    sessionId = checkSessionId(body.get('sessionId') if isinstance(body, dict) else None)
    requireSession(sessionId)
    return {'stopped': agentManager.requestStop(sessionId)}


app.include_router(authedApi)

# ---------- 静态文件：前端并行开发中，目录可能为空/不存在，check_dir=False 容忍 ----------

app.mount('/static', StaticFiles(directory=staticDir, check_dir=False), name='static')
app.mount('/', StaticFiles(directory=staticDir, html=True, check_dir=False), name='root')
