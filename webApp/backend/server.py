'''
Author: wilbur
Version: 1.14
Date: 2026-09-01
Description: FastAPI application and authenticated REST/SSE routes. v1.14 adds no-store subscription model-candidate discovery with credential-generation race rejection and structured secret-free errors.
'''

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Body, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from flamingoAgents.models.modelConfig import loadModelConfigFromYaml
from flamingoAgents.models.subscriptionModels import discoverSubscriptionModels, modelDiscoveryError
from flamingoAgents.utils.logPaths import resolveSessionLogDir

from webApp.backend import agentManager, fileBrowser, historyView, modelAuthManager, modelConfigStore, sessionStore, skillStore, usageStore
from webApp.backend.piModelsImport import convertPiDocument
from webApp.backend.auth import authDependency, checkToken
from webApp.backend.sseCodec import sseGen

projectRoot = Path(__file__).resolve().parents[2]
staticDir = Path(__file__).resolve().parents[1] / 'frontend'

sessionIdPattern = re.compile(r'[A-Za-z0-9_-]+')
skillNamePattern = re.compile(r'[A-Za-z0-9_-]+')
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


def sseResponse(pump, meta=None) -> StreamingResponse:
    # 统一走订阅队列（multiWindowStreamingPlan §4.2）：meta 非 None（attach）时首帧 streamResume；pump 传入供断连反注册。
    return StreamingResponse(sseGen(pump.subscribe(), meta=meta, pump=pump), media_type='text/event-stream', headers=sseHeaders)


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


def nearestWritableAncestor(path: Path) -> Path | None:
    # 多级创建判定（workDirPickerPlan §2.3 合同，probe/create 共用）：
    # 1. 不考虑 path 自身，从 path.parent 往上走；
    # 2. 遇到第一个 exists() 的节点：仅当 is_dir() 且 access(W_OK|X_OK) 才返回它；否则返回 None
    #    （存在但是文件 / 不可写目录都是 None，禁止跨过去继续往上找--否则 mkdir(parents=True) 无法穿过文件，先探后建被破坏）；
    # 3. 走到根仍没有 -> None。
    current = path.parent
    while True:
        try:
            if current.exists():
                if current.is_dir() and os.access(current, os.W_OK | os.X_OK):
                    return current
                return None
        except OSError:
            return None
        parent = current.parent
        if parent == current:
            return None
        current = parent


@authedApi.post('/fs/listDir')
def fsListDir(body: dict = Body(...)):
    # workDir 补全（workDirPickerPlan §2.1）：服务器绝对路径列目录；路由只做入参校验与异常映射，scandir 逻辑在 fileBrowser.listAbsDirs。
    pathRaw = body.get('path') if isinstance(body, dict) else None
    if not isinstance(pathRaw, str) or not pathRaw.strip():
        raise HTTPException(status_code=400, detail='path 必须是非空字符串。')
    result = fileBrowser.listAbsDirs(pathRaw.strip())  # RuntimeError -> 统一异常映射 400 透传，返回 { path, entries, truncated }
    return result


@authedApi.post('/sessions/probeWorkDir')
def probeWorkDir(body: dict = Body(...)):
    # 无副作用探测（契约 §3.4）：前端「先探后建」，判定只用 creatable/willCreate 两个布尔。
    workDirRaw = body.get('workDir') if isinstance(body, dict) else None
    if not isinstance(workDirRaw, str) or not workDirRaw.strip():
        raise HTTPException(status_code=400, detail='workDir 必须是非空字符串。')
    workPath = Path(workDirRaw).expanduser().resolve()
    result = {
        'resolvedPath': str(workPath),
        'defaultWorkDir': str(projectRoot),
    }
    if workPath.is_dir():
        writable = os.access(workPath, os.R_OK | os.W_OK | os.X_OK)
        result.update(
            exists=True,
            writable=writable,
            creatable=writable,
            willCreate=False,
            message='目录可用。' if writable else f'目录不可读写：{workPath}',
        )
    elif workPath.exists():
        result.update(exists=False, writable=False, creatable=False, willCreate=False,
                      message=f'路径已存在且不是目录：{workPath}')
    else:
        # 多级创建判定（workDirPickerPlan §2.3）：最近存在祖先是可写目录即可建（不再要求父目录已存在）。
        ancestor = nearestWritableAncestor(workPath)
        if ancestor is not None:
            result.update(exists=False, writable=True, creatable=True, willCreate=True,
                          message=f'目录不存在，将创建：{workPath}')
        else:
            result.update(exists=False, writable=False, creatable=False, willCreate=True,
                          message=f'无法创建：{workPath}（最近存在的祖先不是可写目录）')
    return result


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
    if not isinstance(workDirRaw, str) or not workDirRaw.strip():
        raise HTTPException(status_code=400, detail='workDir 必须是非空字符串。')
    allowCreate = body.get('allowCreate', False)
    if not isinstance(allowCreate, bool):
        raise HTTPException(status_code=400, detail='allowCreate 必须是布尔值。')
    workPath = Path(workDirRaw).expanduser()
    # 处理顺序（审核中 6）：先 providerId/modelId 预检（失败 400 不留孤儿目录），再处理目录。
    # 预检（审核 M3/M4）：yaml 缺失时库会静默回退环境变量配置，必须 Web 层先行拦截。
    if not modelConfigStore.modelsYamlPath.exists():
        raise HTTPException(status_code=400, detail='config/models.yaml 不存在。')
    resolved = loadModelConfigFromYaml(providerId=providerId, modelId=modelId or None)
    if workPath.is_dir():
        # 行为变更（审核中 9）：已存在目录除 is_dir 外增加可读写进入校验。
        if not os.access(workPath, os.R_OK | os.W_OK | os.X_OK):
            raise HTTPException(status_code=400, detail=f'目录不可读写：{workPath}')
    elif workPath.exists():
        raise HTTPException(status_code=400, detail=f'路径已存在且不是目录：{workPath}')
    else:
        if not allowCreate:
            raise HTTPException(status_code=400, detail=f'workDir 不存在：{workPath}')
        # 多级创建（workDirPickerPlan §2.3）：不再要求父目录已存在，最近存在祖先是可写目录即可；
        # parents=True 后 FileExistsError 可能是中间级被文件占，不能一律视为成功，mkdir 后双保险复验。
        if nearestWritableAncestor(workPath) is None:
            raise HTTPException(status_code=400, detail=f'无法创建：{workPath}（最近存在的祖先不是可写目录）')
        try:
            workPath.mkdir(parents=True, exist_ok=True)
        except FileNotFoundError:
            raise HTTPException(status_code=400, detail=f'祖先目录已被删除，请重试：{workPath}')
        except PermissionError:
            raise HTTPException(status_code=400, detail=f'无权限创建目录：{workPath}')
        except OSError as error:
            raise HTTPException(status_code=400, detail=f'创建目录失败：{error}')
        if not workPath.is_dir():
            raise HTTPException(status_code=400, detail=f'路径已存在且不是目录：{workPath}')
        if not os.access(workPath, os.R_OK | os.W_OK | os.X_OK):
            raise HTTPException(status_code=400, detail=f'目录不可读写：{workPath}')
    return sessionStore.createSession(str(workPath.resolve()), providerId, resolved.config.model)


@authedApi.get('/sessions/{sessionId}/status')
def getSessionStatus(sessionId: str):
    # 状态栏聚合（迭代二 §3.2）：usage/contextTokens 单一数据源为 sessions 索引；yaml 异常时 cost=0、contextWindow=null 降级（评审 M1）。
    checkSessionId(sessionId)
    session = requireSession(sessionId)
    workDir = session.get('workDir', '')
    gitBranch = None
    try:
        result = subprocess.run(
            ['git', '-C', workDir, 'rev-parse', '--abbrev-ref', 'HEAD'],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            gitBranch = result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        gitBranch = None
    providerId = session.get('providerId', '')
    modelId = session.get('modelId', '')
    contextWindow = None
    try:
        raw = modelConfigStore.readRawYaml()
        providers = raw.get('providers') if isinstance(raw, dict) else None
        provider = providers.get(providerId) if isinstance(providers, dict) else None
        models = provider.get('models') if isinstance(provider, dict) else None
        for model in models or []:
            if isinstance(model, dict) and model.get('id') == modelId and isinstance(model.get('contextWindow'), int):
                contextWindow = model['contextWindow']
                break
    except RuntimeError:
        contextWindow = None
    contextTokens = int(session.get('contextTokens', 0) or 0)
    usedPercent = None
    if contextWindow:
        usedPercent = round(max(0.0, min(100.0, (contextTokens / contextWindow) * 100)), 1)
    lastUsage = session.get('lastUsage')
    if not isinstance(lastUsage, dict):
        # 索引无 lastUsage（升级前会话或尚未经新泵回写）：回退 usageTurns 最近一轮增量，保证重启后仍显示「带历史的最近增量」。
        lastUsage = usageStore.queryLastUsageTurn(sessionId) or dict(sessionStore.emptyUsage)
    else:
        lastUsage = {
            'promptTokens': int(lastUsage.get('promptTokens', 0) or 0),
            'cachedTokens': int(lastUsage.get('cachedTokens', 0) or 0),
            'completionTokens': int(lastUsage.get('completionTokens', 0) or 0),
        }
    return {
        'workDir': workDir,
        'gitBranch': gitBranch,
        'providerId': providerId,
        'modelId': modelId,
        'usage': session.get('usage') or dict(sessionStore.emptyUsage),
        'lastUsage': lastUsage,
        'cost': usageStore.querySessionCost(sessionId),
        'contextWindow': contextWindow,
        'contextTokens': contextTokens,
        'contextUsedPercent': usedPercent,
    }


@authedApi.patch('/sessions/{sessionId}/model')
def updateSessionModel(sessionId: str, body: dict = Body(...)):
    # /model 指令（迭代二 §3.3）：先 yaml 预检再落索引，最后单锁丢弃 agent（活跃流 → 409，索引已生效、下轮起新模型）。
    checkSessionId(sessionId)
    requireSession(sessionId)
    providerId = body.get('providerId') if isinstance(body, dict) else None
    modelId = body.get('modelId') if isinstance(body, dict) else None
    if not isinstance(providerId, str) or not providerId.strip():
        raise HTTPException(status_code=400, detail='providerId 必填。')
    if not isinstance(modelId, str) or not modelId.strip():
        raise HTTPException(status_code=400, detail='modelId 必填。')
    providerId = providerId.strip()
    modelId = modelId.strip()
    if not modelConfigStore.modelsYamlPath.exists():
        raise HTTPException(status_code=400, detail='config/models.yaml 不存在。')
    loadModelConfigFromYaml(providerId=providerId, modelId=modelId)  # 预检，失败 RuntimeError → 400
    session = sessionStore.updateSessionModel(sessionId, providerId, modelId)
    if session is None:
        raise HTTPException(status_code=404, detail=f'会话不存在：{sessionId}')
    if not agentManager.dropAgentIfIdle(sessionId):
        raise HTTPException(status_code=409, detail='该会话有活跃流，本轮仍跑旧模型，下轮起新模型生效。')
    return session


@authedApi.get('/sessions/{sessionId}/files')
def listFiles(sessionId: str, path: str = ''):
    checkSessionId(sessionId)
    session = requireSession(sessionId)
    return fileBrowser.listDir(session['workDir'], path or None)


@authedApi.get('/sessions/{sessionId}/fileContent')
def getFileContent(sessionId: str, path: str = ''):
    checkSessionId(sessionId)
    session = requireSession(sessionId)
    if not path:
        raise HTTPException(status_code=400, detail='path 不能为空。')
    return fileBrowser.readTextFile(session['workDir'], path)


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
    session = requireSession(sessionId)
    if agentManager.hasActiveStream(sessionId):
        raise HTTPException(status_code=409, detail='该会话有活跃流，无法删除。')
    sessionStore.deleteSession(sessionId)
    logPath = resolveSessionLogDir('webData', Path(session['workDir'])) / f'{sessionId}.jsonl'
    try:
        logPath.unlink(missing_ok=True)
    except OSError as error:
        print(f'warning: 删除会话日志失败 {logPath}: {error}')
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


@authedApi.get('/usage/series')
def getUsageSeries(granularity: str = 'day'):
    # 时/天/月粒度用量序列（契约 §3.10）：数据源 usageTurns（账单口径，删会话不删账）。
    if granularity not in ('hour', 'day', 'month'):
        raise HTTPException(status_code=400, detail=f'granularity 非法：{granularity}（仅允许 hour/day/month）。')
    return usageStore.querySeries(granularity)


@authedApi.get('/models')
def getModels():
    return modelConfigStore.readModelsConfig()


@authedApi.put('/models')
def putModels(body: dict = Body(...)):
    modelConfigStore.writeModelsConfig(body)
    agentManager.invalidateAllAgents()
    return {'ok': True}


@authedApi.get('/modelAuth')
def getModelAuth():
    return modelAuthManager.defaultModelAuthManager.getAuthStatus()


@authedApi.post('/modelAuth/{provider}/login')
def startModelLogin(provider: str, body: dict | None = Body(default=None)):
    method = body.get('method') if isinstance(body, dict) else None
    if method is not None and not isinstance(method, str):
        raise HTTPException(status_code=400, detail='method 必须是字符串。')
    try:
        return modelAuthManager.defaultModelAuthManager.startLogin(provider, method)
    except modelAuthManager.loginConflictError as error:
        raise HTTPException(status_code=409, detail=str(error))


@authedApi.get('/modelAuth/logins/{loginId}')
def getModelLogin(loginId: str):
    try:
        return modelAuthManager.defaultModelAuthManager.getLogin(loginId)
    except modelAuthManager.loginNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error))


@authedApi.post('/modelAuth/logins/{loginId}/manualCode')
def submitModelLoginCode(loginId: str, body: dict = Body(...)):
    code = body.get('code') if isinstance(body, dict) else None
    if not isinstance(code, str) or not code.strip():
        raise HTTPException(status_code=400, detail='code 必须是非空字符串。')
    try:
        return modelAuthManager.defaultModelAuthManager.submitManualCode(loginId, code)
    except modelAuthManager.loginNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error))


@authedApi.delete('/modelAuth/logins/{loginId}')
def cancelModelLogin(loginId: str):
    try:
        return modelAuthManager.defaultModelAuthManager.cancelLogin(loginId)
    except modelAuthManager.loginNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error))


@authedApi.post('/modelAuth/{provider}/discover')
def discoverModelCandidates(provider: str):
    manager = modelAuthManager.defaultModelAuthManager
    if provider not in ('openai-codex', 'xai'):
        error = modelDiscoveryError(provider, 'unsupported_provider')
        return JSONResponse(status_code=400, content=error.toPublic(), headers={'Cache-Control': 'no-store'})
    try:
        generationBefore = manager.getCredentialGeneration(provider)
        result = discoverSubscriptionModels(provider, store=manager.store)
        generationAfter = manager.getCredentialGeneration(provider)
        if generationAfter != generationBefore:
            raise modelDiscoveryError(provider, 'account_changed')
        result['credentialGeneration'] = generationAfter
        return JSONResponse(content=result, headers={'Cache-Control': 'no-store'})
    except modelDiscoveryError as error:
        statusCodes = {
            'unsupported_provider': 400,
            'not_logged_in': 409,
            'reauth_required': 409,
            'credential_error': 409,
            'account_changed': 409,
            'rate_limited': 429,
        }
        return JSONResponse(
            status_code=statusCodes.get(error.code, 502),
            content=error.toPublic(),
            headers={'Cache-Control': 'no-store'},
        )


@authedApi.delete('/modelAuth/{provider}')
def logoutModelAuth(provider: str):
    modelAuthManager.defaultModelAuthManager.logout(provider)
    return {'ok': True}


@authedApi.get('/skills')
def getSkills():
    return skillStore.listSkills()


@authedApi.get('/skills/{name}')
def getSkillBody(name: str):
    if not skillNamePattern.fullmatch(name):
        raise HTTPException(status_code=400, detail='skill 名非法。')
    try:
        return skillStore.getSkillForEdit(name)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))


@authedApi.put('/skills/{name}')
def putSkill(name: str, body: dict = Body(...)):
    if not skillNamePattern.fullmatch(name):
        raise HTTPException(status_code=400, detail='skill 名非法。')
    try:
        return skillStore.saveSkill(name, body)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error))


@authedApi.post('/models/importPi')
def importPiModels(body: dict = Body(...)):
    rawText = body.get('rawText') if isinstance(body, dict) else None
    if not isinstance(rawText, str) or not rawText.strip():
        raise HTTPException(status_code=400, detail='请上传 models.json 文件。')
    try:
        parsed = json.loads(rawText)
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail=f'models.json 不是合法 JSON：{error}')
    if not isinstance(parsed, dict) or not isinstance(parsed.get('providers'), dict):
        raise HTTPException(status_code=400, detail='models.json 必须是包含 providers 对象的 JSON。')
    providers, report = convertPiDocument(parsed)
    return {'providers': providers, 'report': report}


@authedApi.post('/chat/stream')
def chatStream(body: dict = Body(...)):
    sessionId = checkSessionId(body.get('sessionId') if isinstance(body, dict) else None)
    requireSession(sessionId)
    message = body.get('message')
    if not isinstance(message, str):
        raise HTTPException(status_code=400, detail='message 必须是字符串。')
    attachments = body.get('attachments') or []
    if not isinstance(attachments, list):
        raise HTTPException(status_code=400, detail='attachments 必须是数组。')
    cleanMessage = message.strip()
    if not cleanMessage and not attachments:
        raise HTTPException(status_code=400, detail='message 与 attachments 不能同时为空。')
    session = requireSession(sessionId)
    if attachments:
        # 后端拼接附件块（迭代二 §3.7）：落 jsonl 与发模型的都是拼接后文本，resume 上下文一致。
        cleanMessage = fileBrowser.buildAttachmentMessage(cleanMessage, session['workDir'], attachments)
    agentInstance = agentManager.getAgent(sessionId)
    stream = agentInstance.runUserMessageStream(cleanMessage, sessionId)
    # baseCount 水位线（multiWindowStreamingPlan §4.3）：生成器惰性，appendUserMessage 在泵线程首次迭代才发生，采样必然先于写盘。
    baseCount = len(historyView.loadMessages(sessionId))
    streamMeta = {'baseCount': baseCount, 'userMessage': cleanMessage}
    pump = agentManager.startStream(sessionId, agentInstance, stream, meta=streamMeta)
    if pump is None:
        # 宽容闸（stopResponsivenessPlan §4.1.A）：旧泵已 stopping 且会话锁空闲，
        # 说明收尾只差泵 finally 的毫秒级簿记 → wait(2) 后重试一次 startStream。
        # 探测成功必须立即 release，只作空闲性读数，绝不持锁出临界区。
        oldPump = agentManager.getActivePump(sessionId)
        if oldPump is not None and oldPump.stopFlag.is_set():
            sessionLock = agentInstance.getSessionLock(sessionId)
            if sessionLock.acquire(blocking=False):
                sessionLock.release()
                if oldPump.doneEvent.wait(2):
                    pump = agentManager.startStream(sessionId, agentInstance, stream, meta=streamMeta)
        if pump is None:
            raise HTTPException(status_code=409, detail='该会话已有活跃流，请稍后再试。')
    # 首条用户消息发出后标题自动改为前 20 字；发消息刷新 updatedAt（契约 §2.1）。
    # 纯附件发送（D8）时标题取第一个附件名，同样截断前 20 字（评审 L3）。
    titleSource = message.strip()
    if not titleSource and attachments:
        firstPath = attachments[0].get('path') if isinstance(attachments[0], dict) else ''
        titleSource = f'📄 {firstPath}'
    sessionStore.setDefaultTitle(sessionId, titleSource[:20])
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
    baseCount = len(historyView.loadMessages(sessionId))
    pump = agentManager.startStream(
        sessionId, agentInstance, stream,
        meta={'baseCount': baseCount, 'userMessage': None},
    )
    if pump is None:
        raise HTTPException(status_code=409, detail='该会话已有活跃流，请稍后再试。')
    sessionStore.touchSession(sessionId)
    return sseResponse(pump)


@authedApi.post('/chat/attach')
def chatAttach(body: dict = Body(...)):
    # attach 回放式重连（multiWindowStreamingPlan §4.3）：无活跃流 → 404（前端静默保持历史态）；
    # 否则首帧 streamResume（meta）+ history 压缩回放 + 实时事件。waitingConfirm 非活跃流，走 GET pending 恢复。
    sessionId = checkSessionId(body.get('sessionId') if isinstance(body, dict) else None)
    requireSession(sessionId)
    pump = agentManager.getActivePump(sessionId)
    if pump is None:
        raise HTTPException(status_code=404, detail='该会话无活跃流。')
    return sseResponse(pump, meta=pump.meta)


@authedApi.post('/chat/stop')
def chatStop(body: dict = Body(...)):
    sessionId = checkSessionId(body.get('sessionId') if isinstance(body, dict) else None)
    requireSession(sessionId)
    return {'stopped': agentManager.requestStop(sessionId)}


app.include_router(authedApi)

# ---------- 静态文件：前端并行开发中，目录可能为空/不存在，check_dir=False 容忍 ----------

app.mount('/static', StaticFiles(directory=staticDir, check_dir=False), name='static')
app.mount('/', StaticFiles(directory=staticDir, html=True, check_dir=False), name='root')
