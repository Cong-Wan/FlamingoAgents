'''
Author: wilbur
Version: 1.6
Date: 2026-09-07
Description: ~/.flamingo/logs/webData/sessions.json 会话索引 CRUD：进程内锁 + 临时文件 rename 原子写；updatedAt/usage/标题的刷新时机对齐契约 §2.1。
            v1.1 随包改名调整：webDataDir 因目录加深一级改为 parents[2]。
            v1.2 迭代二（方案 §3.3/§3.6）：updateUsage 增加可选 contextTokens 字段回写；新增 updateSessionModel（/model 指令）。
            v1.3 状态栏口径：updateUsage 增加可选 lastUsage（最近一轮 token 增量）回写。
            v1.4 移除 sessionLogsDir（日志已迁至 ~/.flamingo/logs/webData/，本模块只保留 sessions.json 索引）。
            v1.5 新会话 sessionId 改为 YYMMDDHHmmss-xxxxxxxx（存量 session_* 仍合法）。
            v1.6 索引统一从家目录读写；新索引缺失时无损复制旧索引，新索引始终优先；损坏/不可读索引报错，防止当空索引覆盖。
'''

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from flamingoAgents.utils.logPaths import newSessionId, webLogsRoot

webDataDir = webLogsRoot
indexPath = webDataDir / 'sessions.json'
legacyIndexPath = Path(__file__).resolve().parents[2] / 'webData' / 'sessions.json'

indexLock = threading.RLock()

emptyUsage = {'promptTokens': 0, 'cachedTokens': 0, 'completionTokens': 0}


def nowIso() -> str:
    return datetime.now(timezone.utc).isoformat()


def loadIndex() -> dict[str, dict]:
    with indexLock:
        # 新索引（包括空列表）始终优先；旧索引只作为首次迁移来源，保留不删。
        sourcePath = indexPath if indexPath.exists() else legacyIndexPath
        if not sourcePath.exists():
            return {}
        try:
            raw = json.loads(sourcePath.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as error:
            raise RuntimeError(f'无法读取会话索引：{sourcePath}，请检查文件，未覆盖原数据。') from error
        sessions = raw.get('sessions') if isinstance(raw, dict) else None
        if not isinstance(sessions, list):
            raise RuntimeError(f'会话索引格式错误：{sourcePath}，sessions 必须是列表。')
        index = {}
        for item in sessions:
            sessionId = item.get('sessionId') if isinstance(item, dict) else None
            if not isinstance(sessionId, str) or not sessionId or sessionId in index:
                raise RuntimeError(f'会话索引条目错误：{sourcePath}，sessionId 必须是非空且唯一的字符串。')
            index[sessionId] = item
        if sourcePath != indexPath:
            saveIndex(index)
        return index


def saveIndex(sessions: dict[str, dict]) -> None:
    # 原子写：同目录临时文件 + rename，避免中途崩溃写坏索引。
    with indexLock:
        indexPath.parent.mkdir(parents=True, exist_ok=True)
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
            'sessionId': newSessionId(),
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


def updateUsage(
    sessionId: str,
    usage: dict,
    contextTokens: int | None = None,
    lastUsage: dict | None = None,
) -> None:
    # 泵线程结束时回写累计用量并刷新 updatedAt（契约 §2.1）；
    # contextTokens 为最近一轮 prompt+completion（迭代二 §3.6）；
    # lastUsage 为本轮 token 增量（状态栏 ↑↓⚡ 展示用，与 usageTurns 一致）。
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
        if lastUsage is not None:
            session['lastUsage'] = {
                'promptTokens': int(lastUsage.get('promptTokens', 0) or 0),
                'cachedTokens': int(lastUsage.get('cachedTokens', 0) or 0),
                'completionTokens': int(lastUsage.get('completionTokens', 0) or 0),
            }
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
