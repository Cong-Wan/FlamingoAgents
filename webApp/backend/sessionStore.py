'''
Author: wilbur
Version: 1.2
Date: 2026-08-05
Description: webData/sessions.json 会话索引 CRUD：进程内锁 + 临时文件 rename 原子写；updatedAt/usage/标题的刷新时机对齐契约 §2.1。
            v1.1 随包改名调整：webDataDir 因目录加深一级改为 parents[2]。
            v1.2 迭代二（方案 §3.3/§3.6）：updateUsage 增加可选 contextTokens 字段回写；新增 updateSessionModel（/model 指令）。
'''

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

webDataDir = Path(__file__).resolve().parents[2] / 'webData'
sessionLogsDir = webDataDir / 'sessionLogs'
indexPath = webDataDir / 'sessions.json'

indexLock = threading.RLock()

emptyUsage = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}


def nowIso() -> str:
    return datetime.now(timezone.utc).isoformat()


def loadIndex() -> dict[str, dict]:
    with indexLock:
        if not indexPath.exists():
            return {}
        try:
            raw = json.loads(indexPath.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return {}
        sessions = raw.get('sessions') if isinstance(raw, dict) else None
        if not isinstance(sessions, list):
            return {}
        return {item['sessionId']: item for item in sessions if isinstance(item, dict) and item.get('sessionId')}


def saveIndex(sessions: dict[str, dict]) -> None:
    # 原子写：同目录临时文件 + rename，避免中途崩溃写坏索引。
    with indexLock:
        webDataDir.mkdir(parents=True, exist_ok=True)
        tempPath = indexPath.with_suffix('.json.tmp')
        payload = json.dumps({'sessions': list(sessions.values())}, ensure_ascii=False, indent=2)
        tempPath.write_text(payload, encoding='utf-8')
        os.replace(tempPath, indexPath)


def listSessions() -> list[dict]:
    sessions = loadIndex()
    return sorted(sessions.values(), key=lambda item: item.get('updatedAt', ''), reverse=True)


def getSession(sessionId: str) -> dict | None:
    return loadIndex().get(sessionId)


def createSession(workDir: str, providerId: str, modelId: str) -> dict:
    with indexLock:
        sessions = loadIndex()
        timestamp = nowIso()
        session = {
            'sessionId': 'session_' + uuid4().hex[:12],
            'title': '新会话',
            'workDir': workDir,
            'providerId': providerId,
            'modelId': modelId,
            'createdAt': timestamp,
            'updatedAt': timestamp,
            'usage': dict(emptyUsage),
        }
        sessions[session['sessionId']] = session
        saveIndex(sessions)
        return session


def renameSession(sessionId: str, title: str) -> dict | None:
    with indexLock:
        sessions = loadIndex()
        session = sessions.get(sessionId)
        if session is None:
            return None
        session['title'] = title
        session['updatedAt'] = nowIso()
        saveIndex(sessions)
        return session


def setDefaultTitle(sessionId: str, title: str) -> None:
    # 仅当标题仍为默认「新会话」时改为首条消息前 20 字（契约 §2.1）。
    with indexLock:
        sessions = loadIndex()
        session = sessions.get(sessionId)
        if session is None or session.get('title') != '新会话':
            return
        session['title'] = title
        session['updatedAt'] = nowIso()
        saveIndex(sessions)


def touchSession(sessionId: str) -> None:
    with indexLock:
        sessions = loadIndex()
        session = sessions.get(sessionId)
        if session is None:
            return
        session['updatedAt'] = nowIso()
        saveIndex(sessions)


def updateUsage(sessionId: str, usage: dict, contextTokens: int | None = None) -> None:
    # 泵线程结束时回写累计用量并刷新 updatedAt（契约 §2.1）；contextTokens 为最近一轮 prompt+completion（迭代二 §3.6）。
    with indexLock:
        sessions = loadIndex()
        session = sessions.get(sessionId)
        if session is None:
            return
        session['usage'] = {
            'promptTokens': int(usage.get('promptTokens', 0) or 0),
            'cachedTokens': int(usage.get('cachedTokens', 0) or 0),
            'completionTokens': int(usage.get('completionTokens', 0) or 0),
        }
        if contextTokens is not None:
            session['contextTokens'] = int(contextTokens)
        session['updatedAt'] = nowIso()
        saveIndex(sessions)


def updateSessionModel(sessionId: str, providerId: str, modelId: str) -> dict | None:
    # /model 指令（迭代二 §3.3）：改写索引的 providerId/modelId。
    with indexLock:
        sessions = loadIndex()
        session = sessions.get(sessionId)
        if session is None:
            return None
        session['providerId'] = providerId
        session['modelId'] = modelId
        session['updatedAt'] = nowIso()
        saveIndex(sessions)
        return session


def deleteSession(sessionId: str) -> bool:
    with indexLock:
        sessions = loadIndex()
        if sessionId not in sessions:
            return False
        del sessions[sessionId]
        saveIndex(sessions)
        return True
